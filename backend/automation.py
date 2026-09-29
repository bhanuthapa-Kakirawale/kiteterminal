"""Autopilot: runs the complete post-close pipeline for every NSE session
without any user action —

    sync market data -> check data freshness -> expire stale signals
    -> run every enabled strategy (entries, exits, breakeven proposals)
    -> queue the resulting tickets for approval

Approving or rejecting those tickets is the only manual step. After an
approval, execution (order + protective GTT), fill tracking and trailing
stops are automatic too (execution.py, order_monitor.py, trailing_engine.py).

A run is "due" once the scheduled time has passed for the most recent
completed session and no successful/skipped run exists for it yet — so a
server that was off (or not logged in) at the scheduled time catches up on
its next start or login instead of silently missing the day.
"""

import asyncio
import json
import logging
from datetime import datetime, time, timedelta, timezone

from backend import config, data_jobs, execution, kite_client, market, risk_engine, strategy_engine, worker_health
from backend.accounts import get_default_account
from backend.db import get_connection
from backend.ticker import ticker_manager

logger = logging.getLogger(__name__)

_running = False
_RECENT_RUNS = 10
_MIN_CANDLES_FOR_TREND_FILTER = 200


def is_running() -> bool:
    return _running


def scheduled_at(session_date) -> datetime:
    return datetime.combine(session_date, time(config.DAILY_REFRESH_HOUR_IST), market.IST)


def _parse_utc(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _trading_account_id(conn) -> int | None:
    account = get_default_account(conn)
    return account["id"] if account["account_type"] == "trading" else None


def is_due(at: datetime | None = None) -> bool:
    at = market.as_ist(at)
    session = market.last_complete_session_date(at)
    if at < scheduled_at(session):
        return False
    conn = get_connection()
    try:
        account_id = _trading_account_id(conn)
        if account_id is None:
            return False
        runs = conn.execute(
            "SELECT status, started_at FROM automation_runs WHERE account_id = ? AND run_date = ? ORDER BY id DESC",
            (account_id, session.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    if any(r["status"] in ("success", "skipped") for r in runs):
        return False
    failures = [r for r in runs if r["status"] == "failed"]
    if len(failures) >= config.AUTOMATION_MAX_ATTEMPTS_PER_DAY:
        return False
    if failures and at - _parse_utc(failures[0]["started_at"]) < timedelta(minutes=config.AUTOMATION_RETRY_MINUTES):
        return False
    return True


def next_run_at(at: datetime | None = None) -> datetime:
    at = market.as_ist(at)
    if is_due(at):
        return at
    d = at.date()
    for _ in range(14):
        if market.is_trading_day(d) and scheduled_at(d) > at:
            return scheduled_at(d)
        d += timedelta(days=1)
    return scheduled_at(d)


def _session_available() -> bool:
    try:
        kite_client.get_kite()
        return True
    except kite_client.NoActiveSessionError:
        return False


def _mark_interrupted_runs() -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE automation_runs SET status = 'failed', error = 'Interrupted — the server stopped mid-run', "
            "finished_at = datetime('now') WHERE status = 'running'"
        )
        conn.commit()
    finally:
        conn.close()


def _due_now() -> bool:
    return is_due() and _session_available()


async def run_automation_loop() -> None:
    # PK-22F: the one-time "mark interrupted runs" step is inside the retry
    # loop — a database lock at startup used to end this task for good,
    # silently — and the DB reads run off the event loop.
    interrupted_marked = False
    while True:
        try:
            with worker_health.cycle("automation"):
                if not interrupted_marked:
                    await asyncio.to_thread(_mark_interrupted_runs)
                    interrupted_marked = True
                if await asyncio.to_thread(_due_now):
                    session = market.last_complete_session_date()
                    late = market.now_ist() - scheduled_at(session) > timedelta(minutes=30)
                    await run_pipeline("catch_up" if late else "scheduled")
        except Exception:
            logger.exception("Autopilot scheduler tick failed")
        await asyncio.sleep(config.AUTOMATION_POLL_SECONDS)


async def run_pipeline(trigger: str) -> dict:
    global _running
    if _running:
        return {"status": "busy"}
    _running = True
    try:
        return await _run_pipeline(trigger)
    finally:
        _running = False


async def _run_pipeline(trigger: str) -> dict:
    run_date = market.last_complete_session_date().isoformat()
    account = get_default_account()
    run_id = _start_run(account["id"], run_date, trigger)
    ticker_manager.broadcast({"type": "automation_run", "phase": "started", "run_id": run_id, "trigger": trigger})

    summary: dict = {"trigger": trigger, "run_date": run_date}
    status, error = "failed", None
    try:
        kite = kite_client.get_kite()
        summary["symbols_synced"] = await data_jobs.sync_all_symbols()
        await data_jobs.wait_for_idle()
        freshness = await asyncio.to_thread(data_freshness, account["id"], run_date)
        summary["data"] = freshness
        if freshness["watchlist_size"] and freshness["fresh_count"] == 0:
            status = "skipped"
            summary["reason"] = (f"No candle for {run_date} is available yet (exchange holiday, or data not "
                                 f"published) — nothing new to scan.")
        else:
            summary["expired_signals"] = await asyncio.to_thread(_expire_signals, account["id"], run_date)
            summary["scan"] = await asyncio.to_thread(strategy_engine.run_daily_scans, account, kite)
            status = "success"
    except kite_client.NoActiveSessionError:
        error = "Kite session is not active — log in so the autopilot can run."
    except risk_engine.RiskCheckUnavailableError as e:
        error = e.message
    except Exception as e:
        logger.exception("Autopilot pipeline failed")
        error = str(e) or e.__class__.__name__

    _finish_run(run_id, status, summary, error)
    result = {"run_id": run_id, "status": status, "error": error, "summary": summary}
    ticker_manager.broadcast({"type": "automation_run", "phase": "finished", **result})
    ticker_manager.broadcast({"type": "tickets_changed", "reason": "automation_run"})
    logger.info("Autopilot run %d (%s) for %s: %s %s", run_id, trigger, run_date, status, error or "")
    return result


def _start_run(account_id: int, run_date: str, trigger: str) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO automation_runs (account_id, run_date, trigger, status) VALUES (?, ?, ?, 'running')",
            (account_id, run_date, trigger),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _finish_run(run_id: int, status: str, summary: dict, error: str | None) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE automation_runs SET status = ?, summary = ?, error = ?, finished_at = datetime('now') WHERE id = ?",
            (status, json.dumps(summary, default=str), error, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def _expire_signals(account_id: int, run_date: str) -> list[int]:
    if not config.SIGNAL_EXPIRY_ENABLED:
        return []
    conn = get_connection()
    try:
        return strategy_engine.expire_stale_signals(conn, account_id, run_date)
    finally:
        conn.close()


def data_freshness(account_id: int, run_date: str) -> dict:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT w.symbol, MAX(o.date) AS last_date, COUNT(o.id) AS candles
            FROM watchlist w LEFT JOIN ohlc_daily o ON o.instrument_token = w.instrument_token
            WHERE w.account_id = ? GROUP BY w.id ORDER BY w.symbol
            """,
            (account_id,),
        ).fetchall()
    finally:
        conn.close()
    symbols = [dict(r) for r in rows]
    fresh = {s["symbol"] for s in symbols if s["last_date"] and s["last_date"] >= run_date}
    return {
        "watchlist_size": len(symbols),
        "fresh_count": len(fresh),
        "stale": [s["symbol"] for s in symbols if s["symbol"] not in fresh],
        "short_history": [s["symbol"] for s in symbols if (s["candles"] or 0) < _MIN_CANDLES_FOR_TREND_FILTER],
    }


def _pending_counts(conn, account_id: int | None) -> dict:
    def one(sql: str, *args) -> int:
        return conn.execute(sql, args).fetchone()[0]

    today = risk_engine._same_day_ist_cutoff_utc()
    return {
        "entries": one("SELECT COUNT(*) FROM trade_tickets WHERE account_id = ? AND status = 'pending_confirmation' "
                       "AND closes_ticket_id IS NULL", account_id),
        "exits": one("SELECT COUNT(*) FROM trade_tickets WHERE account_id = ? AND status = 'pending_confirmation' "
                     "AND closes_ticket_id IS NOT NULL", account_id),
        "adjustments": one("SELECT COUNT(*) FROM stop_adjustment_proposals sap JOIN trade_tickets t "
                           "ON t.id = sap.ticket_id WHERE t.account_id = ? AND sap.status = 'pending'", account_id),
        "blocked_today": one("SELECT COUNT(*) FROM trade_tickets WHERE account_id = ? AND status = 'risk_rejected' "
                             "AND archived = 0 AND created_at >= ?", account_id, today),
        "awaiting_fill": one("SELECT COUNT(*) FROM trade_tickets WHERE account_id = ? AND mode = 'live' "
                             "AND status = 'executed' AND entry_fill_status = 'pending'", account_id),
    }


def _serialize_run(row) -> dict:
    data = dict(row)
    data["summary"] = json.loads(data["summary"]) if data["summary"] else None
    return data


def get_status() -> dict:
    now = market.now_ist()
    session = market.last_complete_session_date(now)
    conn = get_connection()
    try:
        account_id = _trading_account_id(conn)
        runs = conn.execute(
            "SELECT * FROM automation_runs WHERE account_id = ? ORDER BY id DESC LIMIT ?", (account_id, _RECENT_RUNS)
        ).fetchall()
        enabled = [
            r["strategy_name"] for r in conn.execute(
                "SELECT strategy_name FROM strategy_configs WHERE account_id = ? AND enabled = 1 ORDER BY strategy_name",
                (account_id,),
            )
        ]
        pending = _pending_counts(conn, account_id)
    finally:
        conn.close()

    serialized = [_serialize_run(r) for r in runs]
    return {
        "now": now.isoformat(),
        "market_open": market.is_market_open(now),
        "session_connected": _session_available(),
        "execution_mode": execution.get_execution_mode(),
        "enabled_strategies": enabled,
        "schedule_time_ist": f"{config.DAILY_REFRESH_HOUR_IST:02d}:00",
        "last_session_date": session.isoformat(),
        "last_session_done": any(
            r["run_date"] == session.isoformat() and r["status"] in ("success", "skipped") for r in serialized
        ),
        "due_now": is_due(now),
        "running": _running,
        "next_run_at": next_run_at(now).isoformat(),
        "runs": serialized,
        "pending": pending,
        "pending_data_jobs": data_jobs.pending_jobs(),
        "settings": {
            "amo_when_market_closed": config.USE_AMO_WHEN_MARKET_CLOSED,
            "signal_expiry": config.SIGNAL_EXPIRY_ENABLED,
            "order_monitor_interval_seconds": config.ORDER_MONITOR_INTERVAL_SECONDS,
            "trailing_interval_seconds": config.TRAILING_CHECK_INTERVAL_SECONDS,
        },
    }
