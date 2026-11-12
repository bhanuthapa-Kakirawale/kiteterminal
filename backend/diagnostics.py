"""Operational diagnostics (PK-22H): read-only, non-secret status.

  health()     — liveness: the process is up and the database answers.
  readiness()  — "safe enough to trade": everything that must hold before an
                 order may be approved, with the reason for each failure. The
                 market does NOT have to be open.
  report()     — the full picture the System Diagnostics view shows.

None of these calls the broker, and none returns a credential: no access
token, API secret or key, request token, encryption key, or raw broker
payload (the PK-18 route crawl covers these routes automatically; a PK-22
test checks sentinel secrets explicitly). Broker login status is derived
from the stored session row's timestamp alone.
"""

import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone

from backend import (
    account_safety, db, instance_lock, kite_client, obs, ohlc_integrity, reconciliation, startup_reconciliation,
    trading_calendar, version, worker_health, workers,
)
from backend.accounts import get_default_account

_INTEGRITY_CACHE_SECONDS = 600
_integrity_lock = threading.Lock()
_integrity: dict = {}


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def integrity_status(refresh: bool = False) -> dict:
    """PRAGMA quick_check, cached for _INTEGRITY_CACHE_SECONDS (it reads the
    whole file)."""
    with _integrity_lock:
        fresh = _integrity and time.monotonic() - _integrity["_at"] < _INTEGRITY_CACHE_SECONDS
        if refresh or not fresh:
            started = time.perf_counter()
            conn = db.get_connection()
            try:
                rows = [r[0] for r in conn.execute("PRAGMA quick_check").fetchall()]
            finally:
                conn.close()
            _integrity.clear()
            _integrity.update({
                "_at": time.monotonic(), "ok": rows == ["ok"], "result": rows[:5],
                "checked_at": _now_utc(), "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            })
        return {k: v for k, v in _integrity.items() if not k.startswith("_")}


def reset_cache() -> None:
    with _integrity_lock:
        _integrity.clear()


def _broker_session(account) -> dict:
    row = kite_client.get_active_session(account["id"])
    token_stored = bool(account["access_token_enc"])
    fresh = kite_client.is_session_fresh(row)
    if row is None or not token_stored:
        state = "MISSING"
    elif not fresh:
        state = "STALE"   # Kite tokens expire daily; a login from an earlier IST day is not usable
    else:
        state = "VALID"
    return {
        "state": state,
        "login_time": row["login_time"] if row is not None else None,
        "user_name": row["user_name"] if row is not None else None,
        "token_stored": token_stored,
        "verified_with_broker": False,  # derived from the stored login time; no broker call is made
    }


def _workers() -> dict:
    snapshot = worker_health.snapshot()
    for name in (*workers.CRITICAL_WORKERS, "data_jobs", "automation"):
        snapshot.setdefault(name, {"name": name, "task_state": "not_started", "cycles": 0, "failing": False,
                                   "consecutive_failures": 0, "last_ok_at": None, "last_error": None})
    for name, entry in snapshot.items():
        entry["critical"] = name in workers.CRITICAL_WORKERS
        entry["task_alive"] = workers.supervisor.is_running(name)
    return snapshot


def _paper_broker(conn: sqlite3.Connection, account_id: int) -> dict:
    orders = conn.execute(
        "SELECT status, COUNT(*) AS n FROM paper_orders WHERE account_id = ? GROUP BY status", (account_id,)
    ).fetchall()
    gtts = conn.execute(
        "SELECT status, COUNT(*) AS n FROM paper_gtts WHERE account_id = ? GROUP BY status", (account_id,)
    ).fetchall()
    return {"orders_by_status": {r["status"]: r["n"] for r in orders},
            "gtts_by_status": {r["status"]: r["n"] for r in gtts}}


def _security() -> dict:
    from backend import server
    from backend.security import allowed_hostnames
    try:
        runtime = server.detect_runtime(os.environ, sys.argv)
    except Exception as e:  # malformed runtime settings: report, never raise
        return {"error": type(e).__name__}
    return {
        "bind_host": runtime.host, "port": runtime.port, "remote_exposure": runtime.remote_bind,
        "reload": runtime.reload, "workers": runtime.workers, "started_by": runtime.source,
        "trusted_proxies": list(runtime.trusted_proxy_ips), "allowed_hosts": sorted(allowed_hostnames()),
    }


def health() -> dict:
    try:
        conn = db.get_connection()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        database = "ok"
    except Exception as e:
        database = f"error: {type(e).__name__}"
    return {"status": "ok", "database": database, "version": version.APP_VERSION,
            "commit": version.commit_short(), "time_utc": _now_utc()}


def readiness(now: datetime | None = None) -> dict:
    """Blockers make ready=false; warnings do not. The market may be closed."""
    blockers: list[dict] = []
    warnings: list[dict] = []

    conn = db.get_connection()
    try:
        schema = db.schema_status(conn)
        account = get_default_account(conn)
        safety = account_safety.get_state(conn, account["id"]) if account["account_type"] == "trading" else None
        history_counts = ohlc_integrity.health(conn)["counts"] if _has_table(conn, "ohlc_history_status") else {}
        mode = conn.execute("SELECT mode FROM execution_settings WHERE id = 1").fetchone()["mode"]
    finally:
        conn.close()

    if not schema["supported"]:
        blockers.append({"code": "SCHEMA_UNSUPPORTED", "message": schema["error"]})
    elif not schema["up_to_date"]:
        blockers.append({"code": "SCHEMA_NOT_CURRENT",
                         "message": f"Schema v{schema['current_version']} of v{schema['latest_version']}: "
                                    "migrations are pending."})
    integrity = integrity_status()
    if not integrity["ok"]:
        blockers.append({"code": "DB_INTEGRITY_FAILED", "message": "; ".join(integrity["result"])})

    for name, entry in _workers().items():
        if entry["critical"] and not entry["task_alive"]:
            blockers.append({"code": f"WORKER_NOT_RUNNING:{name}",
                             "message": f"The {name} worker is {entry['task_state']}: positions and orders are "
                                        "not being followed."})
        elif entry.get("failing"):
            warnings.append({"code": f"WORKER_FAILING:{name}",
                             "message": f"{entry['consecutive_failures']} consecutive failed cycles; last error: "
                                        f"{entry.get('last_error')}"})

    session = _broker_session(account)
    if session["state"] != "VALID":
        blockers.append({"code": f"BROKER_SESSION_{session['state']}",
                         "message": "No valid Kite session for today: log in before trading."})

    if safety is not None:
        if safety["trading_locked"]:
            blockers.append({"code": "TRADING_LOCKED", "message": safety["trading_lock_reason"] or "Trading is locked."})
        if safety["breaker_open"]:
            blockers.append({"code": "BREAKER_OPEN", "message": safety["breaker_reason"] or "Broker circuit breaker is open."})
    else:
        blockers.append({"code": "NOT_A_TRADING_ACCOUNT", "message": "The default account is not the trading account."})

    calendar = trading_calendar.calendar_status(now)
    if calendar["uncovered_years"]:
        warnings.append({"code": "CALENDAR_YEAR_UNCOVERED",
                         "message": f"No NSE holiday list for {calendar['uncovered_years']}: every weekday is "
                                    "assumed to trade. Update backend/calendar_data/nse_equity_holidays.json."})
    if history_counts.get(ohlc_integrity.NEEDS_REFRESH):
        warnings.append({"code": "HISTORY_NEEDS_REFRESH",
                         "message": f"{history_counts[ohlc_integrity.NEEDS_REFRESH]} symbol(s) have stale "
                                    "(pre-adjustment) history: rebuild them from System Diagnostics."})
    if not obs.file_logging_status()["enabled"]:
        warnings.append({"code": "LOG_FILE_DISABLED", "message": "Logs go to the console only."})

    # Phase 2: database ownership is a blocker (nothing can be written without it); the startup
    # restriction is a warning here (it refuses only new live entries, at confirmation).
    code = instance_lock.refusal_code()
    if code is not None:
        blockers.append({"code": code, "message": instance_lock.refusal_message(code)})
    for warning in instance_lock.status()["warnings"]:
        warnings.append({"code": "DATABASE_IN_SYNCED_FOLDER", "message": warning})
    restriction = _restriction(account["id"])
    if restriction["state"] != startup_reconciliation.NORMAL:
        warnings.append({"code": "ACCOUNT_RESTRICTED",
                         "message": f"RESTRICTED ({startup_reconciliation.describe(restriction['reasons'])}): no new "
                                    "live entry until every reason clears. Exits are not affected."})

    return {"ready": not blockers, "execution_mode": mode, "blockers": blockers, "warnings": warnings,
            "checked_at": _now_utc(), "market_open_required": False}


def _restriction(account_id: int) -> dict:
    try:
        return startup_reconciliation.status(account_id)
    except Exception as e:  # diagnostics never raise; an unevaluable restriction is RESTRICTED
        return {"state": startup_reconciliation.RESTRICTED, "reasons": [f"UNEVALUABLE: {type(e).__name__}"],
                "last_run": None}


def _instance(account_id: int) -> dict:
    """Phase 2: who owns the database, the fencing epoch, the heartbeat, and the startup restriction."""
    return {**instance_lock.status(), "restriction": _restriction(account_id)}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def report(refresh_integrity: bool = False, now: datetime | None = None) -> dict:
    conn = db.get_connection()
    try:
        schema = db.schema_status(conn)
        account = get_default_account(conn)
        is_trading = account["account_type"] == "trading"
        safety = account_safety.status(conn, account["id"]) if is_trading else None
        paper = _paper_broker(conn, account["id"]) if _has_table(conn, "paper_orders") else None
        history = ohlc_integrity.health(conn) if _has_table(conn, "ohlc_history_status") else None
        mode = conn.execute("SELECT mode FROM execution_settings WHERE id = 1").fetchone()["mode"]
    finally:
        conn.close()
    worker_view = _workers()
    return {
        "app": {"version": version.APP_VERSION, "commit": version.commit(), "python": sys.version.split()[0],
                "time_utc": _now_utc()},
        "readiness": readiness(now),
        "execution_mode": mode,
        "schema": schema,
        "database_integrity": integrity_status(refresh=refresh_integrity),
        "startup": dict(db.last_init),
        "instance": _instance(account["id"]),
        "workers": worker_view,
        "last_cycles": {name: worker_view.get(name, {}).get("last_ok_at")
                        for name in ("order_monitor", "reconciliation", "trailing", "data_jobs", "automation")},
        "reconciliation": reconciliation.cycle_status(),
        "paper_broker": paper,
        "broker_session": _broker_session(account),
        "account_safety": None if safety is None else {
            k: safety[k] for k in ("trading_locked", "trading_lock_reason", "breaker_open", "breaker_reason",
                                   "breaker_recent_failures", "entries_today", "daily_loss_limit_hit")
        },
        "market": trading_calendar.calendar_status(now),
        "historical_data": None if history is None else {
            "counts": history["counts"],
            "flagged": [i for i in history["instruments"] if i["status"] != ohlc_integrity.OK],
            "symbol_transitions": history["symbol_transitions"],
        },
        "logging": obs.file_logging_status(),
        "security": _security(),
    }
