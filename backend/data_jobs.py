import asyncio
import logging
from datetime import datetime, timedelta, timezone

from backend import config, indicators, kite_client, ohlc, strategy_engine
from backend.accounts import get_default_account
from backend.watchlist import symbols_in_holdings_or_watchlist

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Every historical-data call in the app — ad-hoc (watchlist-add) or scheduled
# (daily refresh) — goes through this single queue and its single consumer,
# so no two triggers can ever combine to exceed Kite's rate limit: the
# worker only ever has one Kite historical_data() call in flight at a time,
# with a fixed delay after each before starting the next.
_job_queue: asyncio.Queue = asyncio.Queue()


async def enqueue_backfill(instrument_token: int, symbol: str) -> None:
    await _job_queue.put(("backfill", instrument_token, symbol))


async def enqueue_refresh(instrument_token: int, symbol: str) -> None:
    await _job_queue.put(("refresh", instrument_token, symbol))


async def run_job_worker() -> None:
    while True:
        job_type, instrument_token, symbol = await _job_queue.get()
        try:
            kite = kite_client.get_kite()
            if job_type == "backfill":
                await ohlc.backfill_history(kite, instrument_token, symbol)
            else:
                await ohlc.refresh_latest(kite, instrument_token, symbol)
            indicators.compute_and_store(instrument_token, symbol)
        except kite_client.NoActiveSessionError:
            logger.info("Skipping %s job for %s: no active Kite session", job_type, symbol)
        except Exception:
            logger.exception("Data job failed: %s %s (%d)", job_type, symbol, instrument_token)
        finally:
            _job_queue.task_done()
        await asyncio.sleep(config.HISTORICAL_RATE_LIMIT_DELAY_SECONDS)


async def wait_for_idle() -> None:
    """Blocks until every job enqueued so far has been fully processed
    (historical data fetched AND indicators recomputed — run_job_worker only
    calls task_done() after both steps). sync_all_symbols() itself only
    enqueues and returns immediately; Phase 5's daily scan is the first
    caller that needs "indicators are actually up to date" as a hard
    guarantee rather than an assumption, so this is called between the two."""
    await _job_queue.join()


async def sync_all_symbols() -> int:
    """Enqueues a backfill-or-refresh job for every symbol currently in
    holdings or the watchlist. Called once at startup (catches an existing
    watchlist/holdings on first run) and once daily after market close.
    refresh_latest itself falls back to a full backfill when a symbol has no
    stored history yet, so callers don't need to know which case applies."""
    try:
        kite = kite_client.get_kite()
    except kite_client.NoActiveSessionError:
        logger.info("Skipping data sync: no active Kite session")
        return 0

    account = get_default_account()
    pairs = symbols_in_holdings_or_watchlist(kite, account["id"])
    for instrument_token, symbol in pairs:
        await enqueue_refresh(instrument_token, symbol)
    logger.info("Enqueued %d symbols for OHLC sync", len(pairs))
    return len(pairs)


def _seconds_until_next_daily_refresh() -> float:
    now = datetime.now(IST)
    target = now.replace(hour=config.DAILY_REFRESH_HOUR_IST, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def run_daily_refresh_loop() -> None:
    while True:
        delay = _seconds_until_next_daily_refresh()
        await asyncio.sleep(delay)
        try:
            await sync_all_symbols()
            await wait_for_idle()
            await _run_strategy_scans()
        except Exception:
            logger.exception("Daily OHLC refresh / strategy scan failed")


async def _run_strategy_scans() -> None:
    # Entry scan -> exit scan -> breakeven check, in that order, only for
    # accounts/strategies with enabled=True (checked inside run_daily_scans
    # itself). Runs after wait_for_idle() guarantees indicators reflect
    # today's just-refreshed OHLC.
    try:
        kite = kite_client.get_kite()
    except kite_client.NoActiveSessionError:
        logger.info("Skipping strategy scans: no active Kite session")
        return
    account = get_default_account()
    result = await asyncio.to_thread(strategy_engine.run_daily_scans, account, kite)
    if result.get("enabled"):
        logger.info(
            "Strategy scan: %d entry, %d exit, %d breakeven proposal(s)",
            len(result.get("entry_tickets", [])), len(result.get("exit_tickets", [])),
            len(result.get("breakeven_proposals", [])),
        )
