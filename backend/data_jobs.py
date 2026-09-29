import asyncio
import logging

from backend import config, indicators, kite_client, ohlc, worker_health
from backend.accounts import get_default_account
from backend.watchlist import symbols_in_holdings_or_watchlist

logger = logging.getLogger(__name__)

# Every historical-data call in the app — ad-hoc (watchlist-add) or scheduled
# (the autopilot's daily sync) — goes through this single queue and its
# single consumer, so no two triggers can ever combine to exceed Kite's rate
# limit: the worker only ever has one Kite historical_data() call in flight
# at a time, with a fixed delay after each before starting the next.
#
# Created lazily for the running event loop (an asyncio.Queue is bound to
# the loop it is first used on).
_job_queue: asyncio.Queue | None = None
_queue_loop: asyncio.AbstractEventLoop | None = None
# (job_type, instrument_token) of every job waiting in the queue: a symbol
# already queued is not queued again, so repeated syncs cannot grow the queue
# without bound (PK-22F).
_pending_keys: set[tuple[str, int]] = set()


def _queue() -> asyncio.Queue:
    global _job_queue, _queue_loop
    loop = asyncio.get_running_loop()
    if _job_queue is None or _queue_loop is not loop:
        _job_queue = asyncio.Queue()
        _queue_loop = loop
        _pending_keys.clear()
    return _job_queue


def pending_jobs() -> int:
    return _job_queue.qsize() if _job_queue is not None else 0


async def _enqueue(job_type: str, instrument_token: int, symbol: str) -> bool:
    queue = _queue()
    key = (job_type, instrument_token)
    if key in _pending_keys:
        return False
    _pending_keys.add(key)
    await queue.put((job_type, instrument_token, symbol))
    return True


async def enqueue_backfill(instrument_token: int, symbol: str) -> bool:
    return await _enqueue("backfill", instrument_token, symbol)


async def enqueue_refresh(instrument_token: int, symbol: str) -> bool:
    return await _enqueue("refresh", instrument_token, symbol)


async def enqueue_rebuild(instrument_token: int, symbol: str) -> bool:
    """PK-22D: the explicit, user-requested re-download of one symbol's
    history (ohlc.rebuild_history). Never enqueued automatically."""
    return await _enqueue("rebuild", instrument_token, symbol)


async def run_job_worker() -> None:
    queue = _queue()
    while True:
        job_type, instrument_token, symbol = await queue.get()
        _pending_keys.discard((job_type, instrument_token))
        try:
            with worker_health.cycle("data_jobs"):
                try:
                    kite = kite_client.get_kite()
                    if job_type == "backfill":
                        await ohlc.backfill_history(kite, instrument_token, symbol)
                    elif job_type == "rebuild":
                        await ohlc.rebuild_history(kite, instrument_token, symbol)
                    else:
                        await ohlc.refresh_latest(kite, instrument_token, symbol)
                    # Off the event loop: it reads and writes the DB (PK-22F).
                    await asyncio.to_thread(indicators.compute_and_store, instrument_token, symbol)
                except kite_client.NoActiveSessionError:
                    logger.info("Skipping %s job for %s: no active Kite session", job_type, symbol)
        except Exception:
            # One symbol's failure never stops the worker (PK-22F).
            logger.exception("event=data_job.failed job=%s symbol=%s instrument_token=%d",
                             job_type, symbol, instrument_token)
        finally:
            queue.task_done()
        await asyncio.sleep(config.HISTORICAL_RATE_LIMIT_DELAY_SECONDS)


async def wait_for_idle() -> None:
    """Blocks until every job enqueued so far has been fully processed
    (historical data fetched AND indicators recomputed — run_job_worker only
    calls task_done() after both steps). The autopilot's strategy scan needs
    "indicators are actually up to date" as a hard guarantee, so it calls
    this between syncing and scanning."""
    await _queue().join()


async def sync_all_symbols() -> int:
    """Enqueues a refresh job for every symbol currently in holdings or the
    watchlist. refresh_latest itself falls back to a full backfill when a
    symbol has no stored history yet, so callers don't need to know which
    case applies."""
    try:
        kite = kite_client.get_kite()
    except kite_client.NoActiveSessionError:
        logger.info("Skipping data sync: no active Kite session")
        return 0

    account = get_default_account()
    pairs = await asyncio.to_thread(symbols_in_holdings_or_watchlist, kite, account["id"])
    for instrument_token, symbol in pairs:
        await enqueue_refresh(instrument_token, symbol)
    logger.info("Enqueued %d symbols for OHLC sync", len(pairs))
    return len(pairs)
