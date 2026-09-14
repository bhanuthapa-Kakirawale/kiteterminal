import asyncio
import logging
from datetime import date, timedelta

from kiteconnect import KiteConnect

from backend import config, market
from backend.db import get_connection

logger = logging.getLogger(__name__)


def _store_candles(instrument_token: int, symbol: str, candles: list[dict]) -> int:
    """Upserts — a re-fetched candle overwrites the stored one, which is how
    a candle captured mid-session gets corrected after the close. Candles
    newer than the last completed session are dropped: a still-forming daily
    bar has a provisional close and a fraction of the day's volume, and a
    strategy scan must never treat it as final."""
    last_final = market.last_complete_session_date()
    rows = [
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
    if not rows:
        return 0
    conn = get_connection()
    try:
        conn.executemany(
            """
            INSERT INTO ohlc_daily
                (instrument_token, symbol, date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(instrument_token, date) DO UPDATE SET
                symbol = excluded.symbol, open = excluded.open, high = excluded.high,
                low = excluded.low, close = excluded.close, volume = excluded.volume
            """,
            rows,
        )
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
        total_stored += _store_candles(instrument_token, symbol, candles)
        chunk_start = chunk_end + timedelta(days=1)

    _record_history_requested_from(instrument_token, from_date)
    logger.info("Backfilled %s (%d) %s..%s: %d candles stored", symbol, instrument_token, from_date, to_date, total_stored)
    return total_stored


async def refresh_latest(kite: KiteConnect, instrument_token: int, symbol: str) -> int:
    """Brings one instrument up to date:
    1. deepens history back to BACKFILL_TARGET_DAYS once, if it was
       originally backfilled with a shorter window (older installs kept
       only ~205 candles — too few for a 200-day SMA);
    2. re-downloads the last REFRESH_OVERLAP_DAYS plus anything newer and
       upserts it, correcting any candle stored mid-session.
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
        stored += _store_candles(instrument_token, symbol, candles)
    logger.info("Refreshed %s (%d): %d candle(s) upserted", symbol, instrument_token, stored)
    return stored
