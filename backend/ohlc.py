import asyncio
import logging
from datetime import date, timedelta

from kiteconnect import KiteConnect

from backend import config
from backend.db import get_connection

logger = logging.getLogger(__name__)


def _store_candles(instrument_token: int, symbol: str, candles: list[dict]) -> int:
    if not candles:
        return 0
    conn = get_connection()
    try:
        conn.executemany(
            """
            INSERT OR IGNORE INTO ohlc_daily
                (instrument_token, symbol, date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
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
            ],
        )
        conn.commit()
        return conn.total_changes
    finally:
        conn.close()


def _fetch_chunk(kite: KiteConnect, instrument_token: int, from_date: date, to_date: date) -> list[dict]:
    # Kite omits market holidays/non-trading days from the response entirely
    # rather than erroring — nothing extra needed here to "skip" them.
    return kite.historical_data(instrument_token, from_date, to_date, "day")


async def backfill_history(
    kite: KiteConnect, instrument_token: int, symbol: str, target_days: int = config.BACKFILL_TARGET_DAYS
) -> int:
    """Full backfill, chunked into <= MAX_DAYS_PER_HISTORICAL_REQUEST windows
    (see config.py for why — Kite's per-request day-range limit for the
    'day' interval wasn't confirmable from current docs, so this is a
    conservative default rather than a known exact maximum)."""
    to_date = date.today()
    from_date = to_date - timedelta(days=target_days)

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

    logger.info("Backfilled %s (%d): %d candles stored across the requested window", symbol, instrument_token, total_stored)
    return total_stored


async def refresh_latest(kite: KiteConnect, instrument_token: int, symbol: str) -> int:
    """Fetches only candles newer than what's already stored. Falls back to
    a full backfill if this instrument has never been backfilled."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT MAX(date) AS latest FROM ohlc_daily WHERE instrument_token = ?", (instrument_token,)
        ).fetchone()
    finally:
        conn.close()

    if row["latest"] is None:
        return await backfill_history(kite, instrument_token, symbol)

    last_date = date.fromisoformat(row["latest"])
    from_date = last_date + timedelta(days=1)
    to_date = date.today()
    if from_date > to_date:
        return 0  # already up to date (e.g. job ran twice in the same day)

    candles = await asyncio.to_thread(_fetch_chunk, kite, instrument_token, from_date, to_date)
    stored = _store_candles(instrument_token, symbol, candles)
    logger.info("Refreshed %s (%d): %d new candle(s) from %s", symbol, instrument_token, stored, from_date)
    return stored
