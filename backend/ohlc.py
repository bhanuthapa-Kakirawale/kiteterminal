import asyncio
import logging
from datetime import date, timedelta

from kiteconnect import KiteConnect

from backend import config, market, ohlc_integrity
from backend.db import get_connection

logger = logging.getLogger(__name__)


class HistoryRebuildError(RuntimeError):
    pass


def _final_rows(instrument_token: int, symbol: str, candles: list[dict]) -> list[tuple]:
    """Candles newer than the last completed session are dropped: a
    still-forming daily bar has a provisional close and a fraction of the
    day's volume, and a strategy scan must never treat it as final."""
    last_final = market.last_complete_session_date()
    return [
        (
            instrument_token,
            symbol,
            c["date"].strftime("%Y-%m-%d"),
            c["open"],
            c["high"],
            c["low"],
            c["close"],
            c["volume"],
        )
        for c in candles
        if c["date"].date() <= last_final
    ]


_UPSERT = """
    INSERT INTO ohlc_daily
        (instrument_token, symbol, date, open, high, low, close, volume)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(instrument_token, date) DO UPDATE SET
        symbol = excluded.symbol, open = excluded.open, high = excluded.high,
        low = excluded.low, close = excluded.close, volume = excluded.volume
"""


def _store_candles(instrument_token: int, symbol: str, candles: list[dict]) -> int:
    """Upserts — a re-fetched candle overwrites the stored one, which is how
    a candle captured mid-session gets corrected after the close."""
    rows = _final_rows(instrument_token, symbol, candles)
    if not rows:
        return 0
    conn = get_connection()
    try:
        conn.executemany(_UPSERT, rows)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def _fetch_chunk(kite: KiteConnect, instrument_token: int, from_date: date, to_date: date) -> list[dict]:
    # Kite omits market holidays/non-trading days from the response entirely
    # rather than erroring — nothing extra needed here to "skip" them.
    return kite.historical_data(instrument_token, from_date, to_date, "day")


def _history_requested_from(instrument_token: int) -> date | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT history_requested_from FROM ohlc_meta WHERE instrument_token = ?", (instrument_token,)
        ).fetchone()
    finally:
        conn.close()
    return date.fromisoformat(row["history_requested_from"]) if row else None


def _record_history_requested_from(instrument_token: int, from_date: date) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO ohlc_meta (instrument_token, history_requested_from) VALUES (?, ?)
            ON CONFLICT(instrument_token) DO UPDATE SET
                history_requested_from = MIN(history_requested_from, excluded.history_requested_from)
            """,
            (instrument_token, from_date.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


# ---- PK-22D: integrity bookkeeping (see backend/ohlc_integrity.py) -------------

def _check_provider_history(instrument_token: int, symbol: str, candles: list[dict]) -> dict | None:
    """Before re-fetched candles overwrite stored ones: do the provider's
    bars for dates we already hold still agree with ours? A consistent
    disagreement means Kite back-adjusted the history (split/bonus) and ours
    is stale: NEEDS_REFRESH. Also records a tradingsymbol change."""
    rows = _final_rows(instrument_token, symbol, candles)
    if not rows:
        return None
    conn = get_connection()
    try:
        stored = dict(conn.execute(
            "SELECT date, close FROM ohlc_daily WHERE instrument_token = ? AND date BETWEEN ? AND ?",
            (instrument_token, rows[0][2], rows[-1][2]),
        ).fetchall())
        final = [c for c in candles if c["date"].date() <= market.last_complete_session_date()]
        evidence = ohlc_integrity.detect_back_adjustment(stored, final)
        if evidence is not None:
            ohlc_integrity.mark_needs_refresh(conn, instrument_token, symbol, evidence)
        ohlc_integrity.record_symbol(conn, instrument_token, symbol, "refresh")
        conn.commit()
        return evidence
    finally:
        conn.close()


def _update_integrity(instrument_token: int, symbol: str, full_download: bool) -> None:
    conn = get_connection()
    try:
        ohlc_integrity.summarize(conn, instrument_token, symbol, full_download=full_download,
                                 source="backfill" if full_download else "refresh")
        conn.commit()
    finally:
        conn.close()


async def backfill_history(
    kite: KiteConnect, instrument_token: int, symbol: str, target_days: int = config.BACKFILL_TARGET_DAYS,
    from_date: date | None = None, to_date: date | None = None,
) -> int:
    """Backfill in <= MAX_DAYS_PER_HISTORICAL_REQUEST windows, rate-limited
    between requests. Defaults to the full BACKFILL_TARGET_DAYS window ending
    at the last completed session."""
    to_date = to_date or market.last_complete_session_date()
    from_date = from_date or (to_date - timedelta(days=target_days))

    total_stored = 0
    chunk_start = from_date
    first = True
    while chunk_start <= to_date:
        if not first:
            await asyncio.sleep(config.HISTORICAL_RATE_LIMIT_DELAY_SECONDS)
        first = False

        chunk_end = min(chunk_start + timedelta(days=config.MAX_DAYS_PER_HISTORICAL_REQUEST), to_date)
        candles = await asyncio.to_thread(_fetch_chunk, kite, instrument_token, chunk_start, chunk_end)
        total_stored += await asyncio.to_thread(_store_candles, instrument_token, symbol, candles)
        chunk_start = chunk_end + timedelta(days=1)

    await asyncio.to_thread(_record_history_requested_from, instrument_token, from_date)
    await asyncio.to_thread(_update_integrity, instrument_token, symbol, True)
    logger.info("Backfilled %s (%d) %s..%s: %d candles stored", symbol, instrument_token, from_date, to_date, total_stored)
    return total_stored


async def refresh_latest(kite: KiteConnect, instrument_token: int, symbol: str) -> int:
    """Brings one instrument up to date:
    1. deepens history back to BACKFILL_TARGET_DAYS once, if it was
       originally backfilled with a shorter window (older installs kept
       only ~205 candles — too few for a 200-day SMA);
    2. re-downloads the last REFRESH_OVERLAP_DAYS plus anything newer and
       upserts it, correcting any candle stored mid-session — first checking
       that the provider's bars for the dates already held still agree with
       ours (PK-22D: a back-adjusted history marks the symbol NEEDS_REFRESH).
    Falls back to a full backfill if this instrument has never been stored."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT MIN(date) AS earliest, MAX(date) AS latest FROM ohlc_daily WHERE instrument_token = ?",
            (instrument_token,),
        ).fetchone()
    finally:
        conn.close()

    if row["latest"] is None:
        return await backfill_history(kite, instrument_token, symbol)

    last_final = market.last_complete_session_date()
    target_start = last_final - timedelta(days=config.BACKFILL_TARGET_DAYS)
    stored = 0

    requested_from = _history_requested_from(instrument_token)
    earliest = date.fromisoformat(row["earliest"])
    if (requested_from is None or requested_from > target_start) and earliest > target_start:
        stored += await backfill_history(
            kite, instrument_token, symbol, from_date=target_start, to_date=earliest - timedelta(days=1),
        )
        await asyncio.sleep(config.HISTORICAL_RATE_LIMIT_DELAY_SECONDS)
    elif requested_from is None:
        _record_history_requested_from(instrument_token, earliest)

    from_date = date.fromisoformat(row["latest"]) - timedelta(days=config.REFRESH_OVERLAP_DAYS)
    if from_date <= last_final:
        candles = await asyncio.to_thread(_fetch_chunk, kite, instrument_token, from_date, last_final)
        await asyncio.to_thread(_check_provider_history, instrument_token, symbol, candles)
        stored += await asyncio.to_thread(_store_candles, instrument_token, symbol, candles)
    await asyncio.to_thread(_update_integrity, instrument_token, symbol, False)
    logger.info("Refreshed %s (%d): %d candle(s) upserted", symbol, instrument_token, stored)
    return stored


def _replace_series(instrument_token: int, symbol: str, rows: list[tuple], from_date: date) -> None:
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM ohlc_daily WHERE instrument_token = ?", (instrument_token,))
        conn.executemany(_UPSERT, rows)
        ohlc_integrity.record_symbol(conn, instrument_token, symbol, "rebuild")
        ohlc_integrity.mark_rebuilt(conn, instrument_token, symbol)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    _record_history_requested_from(instrument_token, from_date)


async def rebuild_history(kite: KiteConnect, instrument_token: int, symbol: str) -> int:
    """PK-22D: the explicit fix for a flagged symbol. Re-downloads its whole
    history (at least as far back as what is stored) into memory first, and
    only then replaces the stored series in ONE transaction — a failed or
    empty download leaves the old data untouched. Never run automatically."""
    last_final = market.last_complete_session_date()
    conn = get_connection()
    try:
        earliest = conn.execute(
            "SELECT MIN(date) AS d FROM ohlc_daily WHERE instrument_token = ?", (instrument_token,)
        ).fetchone()["d"]
    finally:
        conn.close()
    candidates = [last_final - timedelta(days=config.BACKFILL_TARGET_DAYS), _history_requested_from(instrument_token)]
    if earliest:
        candidates.append(date.fromisoformat(earliest))
    from_date = min(d for d in candidates if d is not None)

    candles: list[dict] = []
    chunk_start = from_date
    while chunk_start <= last_final:
        if candles:
            await asyncio.sleep(config.HISTORICAL_RATE_LIMIT_DELAY_SECONDS)
        chunk_end = min(chunk_start + timedelta(days=config.MAX_DAYS_PER_HISTORICAL_REQUEST), last_final)
        candles.extend(await asyncio.to_thread(_fetch_chunk, kite, instrument_token, chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)

    rows = sorted({r[2]: r for r in _final_rows(instrument_token, symbol, candles)}.values(), key=lambda r: r[2])
    if not rows:
        raise HistoryRebuildError(
            f"The provider returned no daily candles for {symbol} ({instrument_token}); the stored history was "
            "left unchanged."
        )
    await asyncio.to_thread(_replace_series, instrument_token, symbol, rows, from_date)
    logger.info("event=history.rebuilt instrument_token=%s symbol=%s bars=%d from=%s", instrument_token, symbol,
                len(rows), rows[0][2])
    return len(rows)
