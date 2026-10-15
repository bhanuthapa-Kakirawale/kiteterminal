"""LIVE-0: the live trading pre-flight (backend/live_preflight.py), its
routes, and the narrow confirm-time gate (account_safety.
check_live_prerequisites).

The `ready` fixture builds a state in which every blocking check passes;
each test then breaks exactly one thing. Nothing reaches the network: the
broker is FakeKite, the outbound-IP echo is monkeypatched.
"""

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from kiteconnect.exceptions import TokenException

from conftest import (
    IST, TEST_STATIC_IP, account_id, add_watch, clear_live_prerequisites, displayed_mode, make_candles, seed_ohlc,
    set_mode, ticket_row,
)
from test_baseline_api import _manual_ticket

MUTATING_BROKER_CALLS = ("place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt")
READ_ONLY_BROKER_CALLS = {"profile", "holdings", "positions", "margins", "orders", "get_gtts", "ltp"}
PUBLIC_IP = "93.184.216.34"
OTHER_PUBLIC_IP = "93.184.216.35"


# ---------------------------------------------------------------- helpers

def _exec(env, sql, *args):
    conn = env.db.get_connection()
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _rows(env, sql, *args):
    conn = env.db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _latest_session():
    from backend import trading_calendar
    return trading_calendar.latest_completed_session()


def _healthy_workers():
    from backend import worker_health
    worker_health.reset()
    for name in ("order_monitor", "order_monitor_paper", "trailing", "reconciliation"):
        with worker_health.cycle(name):
            pass


def _reconciled():
    from backend import reconciliation
    reconciliation.last_cycle.clear()
    reconciliation.last_cycle.update(checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                                     gtt_book="ok", gtt_error=None, holdings="ok", holdings_error=None,
                                     active_gtts=0, new_findings=0, failed_checks=[])


@pytest.fixture
def ready(env, monkeypatch):
    """Every blocking pre-flight check passes (the conftest seeds the notional
    cap, the rupee loss limit and a confirmed static IP)."""
    from backend import crypto, diagnostics, live_preflight, reconciliation, worker_health, workers
    _exec(env, "UPDATE accounts SET access_token_enc = ?", crypto.encrypt("fake-access-token"))
    _exec(env, "UPDATE sessions SET login_time = ?", datetime.now(IST).isoformat())
    monkeypatch.setattr(workers.supervisor, "is_running", lambda name: True)
    _healthy_workers()
    _reconciled()
    diagnostics.reset_cache()
    live_preflight.reset_cache()
    seed_ohlc(env.db, 2953217, "TCS", make_candles(_latest_session(), 60, 3000.0))
    add_watch(env.db, "TCS", 2953217)
    yield env
    worker_health.reset()
    reconciliation.last_cycle.clear()
    live_preflight.reset_cache()


def _evaluate(broker=True, **kwargs):
    from backend import live_preflight
    return live_preflight.evaluate(broker=broker, refresh=True, **kwargs)


def _check(result, code):
    matches = [c for c in result["checks"] if c["code"] == code]
    assert matches, f"no {code} check in {[c['code'] for c in result['checks']]}"
    return matches[0]


def _blocking(result):
    return set(result["blocking_codes"])


def _db_digest(env) -> str:
    """Every row of every table: proves the pre-flight wrote nothing."""
    conn = sqlite3.connect(env.db.DB_PATH)
    try:
        h = hashlib.sha256()
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for t in tables:
            for row in conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid'):
                h.update(repr(row).encode())
        return h.hexdigest()
    finally:
        conn.close()


def _live_entry(env, **overrides) -> int:
    t = env.client.post("/api/tickets", json=_manual_ticket(**overrides)).json()["data"]
    assert t["status"] == "pending_confirmation", t
    return t["id"]


def _confirm(env, tid, **body):
    return env.client.post(f"/api/tickets/{tid}/confirm", json={**displayed_mode(env.db), **body})


def _broker_mutations(env):
    return [name for name, _ in env.kite.calls if name in MUTATING_BROKER_CALLS]


# ---------------------------------------------------------------- the ready baseline

def test_ready_baseline_is_ready(ready):
    result = _evaluate()
    assert result["overall_status"] == "READY", result["blocking_codes"]
    assert result["disclaimer"] == "Operational readiness only. It does not assess investment merit."
    assert {s["key"] for s in result["sections"]} == {
        "database", "security", "broker", "network", "static_ip", "market_data", "workers", "account_safety",
        "reconciliation", "trading_lock"}  # NET-1 added "network" (Kite REST route)
    assert all(c["section"] in {s["key"] for s in result["sections"]} for c in result["checks"])


# ---------------------------------------------------------------- DATABASE (1-4)

def test_01_current_schema_passes(ready):
    assert _check(_evaluate(), "DB_SCHEMA_CURRENT")["status"] == "PASS"


def test_02_pending_migration_fails(ready):
    _exec(ready, "DELETE FROM schema_version WHERE version = (SELECT MAX(version) FROM schema_version)")
    result = _evaluate()
    check = _check(result, "DB_SCHEMA_CURRENT")
    assert check["status"] == "FAIL" and "pending" in check["message"]
    assert result["overall_status"] == "NOT_READY"


def test_03_newer_schema_fails(ready):
    _exec(ready, "INSERT INTO schema_version (version, migration_name) VALUES (99, 'from_the_future')")
    result = _evaluate()
    assert _check(result, "DB_SCHEMA_CURRENT")["status"] == "FAIL"
    assert "DB_SCHEMA_CURRENT" in _blocking(result)


def test_04_integrity_failure_blocks(ready, monkeypatch):
    from backend import diagnostics
    monkeypatch.setattr(diagnostics, "integrity_status",
                        lambda refresh=False: {"ok": False, "result": ["row 7 missing from index"],
                                               "checked_at": "2026-09-30 10:00:00", "duration_ms": 1.0})
    result = _evaluate()
    assert _check(result, "DB_INTEGRITY")["status"] == "FAIL"
    assert result["overall_status"] == "NOT_READY"


# ---------------------------------------------------------------- BROKER (5-7)

def test_05_valid_read_only_session_passes(ready):
    result = _evaluate()
    assert _check(result, "BROKER_SESSION")["status"] == "PASS"
    identity = _check(result, "ACCOUNT_IDENTITY")
    assert identity["status"] == "PASS" and identity["detail"]["account"] == "DT••01"
    assert "DT0001" not in str(result)                    # the account id is only ever shown masked
    assert _check(result, "BROKER_PAYLOAD_SHAPE")["status"] == "PASS"


def test_06_invalid_session_blocks(ready):
    ready.kite.profile = lambda: (_ for _ in ()).throw(TokenException("Incorrect `api_key` or `access_token`."))
    result = _evaluate()
    assert _check(result, "BROKER_SESSION")["status"] == "FAIL"
    assert "BROKER_SESSION" in _blocking(result)


def test_06b_session_from_an_earlier_day_blocks_without_a_broker_call(ready):
    _exec(ready, "UPDATE sessions SET login_time = '2026-01-02T09:00:00+05:30'")
    mark = len(ready.kite.calls)
    result = _evaluate(broker=False)
    assert _check(result, "BROKER_SESSION")["status"] == "FAIL"
    assert ready.kite.calls[mark:] == []


def test_06c_account_mismatch_blocks(ready):
    ready.kite.profile = lambda: {"user_id": "ZZ9999", "user_name": "Someone Else"}
    check = _check(_evaluate(), "ACCOUNT_IDENTITY")
    assert check["status"] == "FAIL" and "ZZ••99" in check["message"] and "ZZ9999" not in check["message"]


def test_07_read_only_diagnostics_never_place_an_order(ready):
    mark = len(ready.kite.calls)
    _evaluate()
    names = {name for name, _ in ready.kite.calls[mark:]}
    assert names <= READ_ONLY_BROKER_CALLS
    assert _broker_mutations(ready) == []


@pytest.mark.parametrize("method", MUTATING_BROKER_CALLS)
def test_07b_the_read_only_proxy_refuses_every_mutation(ready, method):
    from backend import live_preflight
    proxy = live_preflight.ReadOnlyKite(ready.kite)
    with pytest.raises(live_preflight.PreflightReadOnlyViolation):
        getattr(proxy, method)
    assert _broker_mutations(ready) == []


def test_07c_without_broker_true_no_broker_call_is_made(ready):
    mark = len(ready.kite.calls)
    result = _evaluate(broker=False)
    assert ready.kite.calls[mark:] == []
    assert _check(result, "BROKER_SESSION")["status"] == "USER_ACTION"
    assert _check(result, "MARKET_DATA_LTP")["status"] == "USER_ACTION"
    assert result["overall_status"] == "NOT_READY" and result["broker_checked"] is False


def test_07d_one_snapshot_one_call_per_read_and_it_is_reused(ready):
    from backend import live_preflight
    mark = len(ready.kite.calls)
    live_preflight.evaluate(broker=True, refresh=True)
    first = [name for name, _ in ready.kite.calls[mark:]]
    assert sorted(first) == sorted(["holdings", "positions", "margins", "orders", "get_gtts", "ltp"])
    mark = len(ready.kite.calls)
    live_preflight.evaluate(broker=True)                   # inside the cache window
    assert ready.kite.calls[mark:] == []


# ---------------------------------------------------------------- STATIC IP (8-10)

def test_08_verified_static_ip_passes(ready, monkeypatch):
    from backend import live_preflight
    r = ready.client.post("/api/live-readiness/static-ip", json={"ip": PUBLIC_IP, "confirm": True})
    assert r.status_code == 200 and r.json()["data"]["status"] == "USER_CONFIRMED"
    monkeypatch.setattr(live_preflight, "_fetch_egress_ip", lambda: PUBLIC_IP)
    r = ready.client.post("/api/live-readiness/egress-check")
    assert r.json()["data"]["status"] == "VERIFIED"
    check = _check(_evaluate(), "STATIC_IP")
    assert check["status"] == "PASS" and check["detail"]["state"] == "VERIFIED"


def test_08b_user_confirmed_without_observation_passes_with_a_recommendation(ready):
    check = _check(_evaluate(), "STATIC_IP")
    assert check["status"] == "PASS" and check["detail"]["state"] == "USER_CONFIRMED"
    assert "Optional" in check["message"]


def test_09_unknown_static_ip_is_action_required_and_blocks(ready):
    _exec(ready, "DELETE FROM live_readiness")
    result = _evaluate()
    check = _check(result, "STATIC_IP")
    assert check["status"] == "USER_ACTION" and check["blocking"] is True
    assert check["detail"]["state"] == "USER_VERIFICATION_REQUIRED"
    assert result["overall_status"] == "NOT_READY"


def test_10_failed_static_ip_blocks(ready, monkeypatch):
    from backend import live_preflight
    monkeypatch.setattr(live_preflight, "_fetch_egress_ip", lambda: OTHER_PUBLIC_IP)
    ready.client.post("/api/live-readiness/static-ip", json={"ip": PUBLIC_IP, "confirm": True})
    assert ready.client.post("/api/live-readiness/egress-check").json()["data"]["status"] == "FAILED"
    result = _evaluate()
    assert _check(result, "STATIC_IP")["status"] == "FAIL"
    assert "STATIC_IP" in _blocking(result)


def test_10b_an_old_observation_no_longer_counts(ready):
    from backend import live_preflight
    _exec(ready, "UPDATE live_readiness SET egress_ip = ?, egress_checked_at = '2026-01-01 00:00:00'", OTHER_PUBLIC_IP)
    conn = ready.db.get_connection()
    try:
        assert live_preflight.static_ip_status(conn, account_id(ready.db))["status"] == "USER_CONFIRMED"
    finally:
        conn.close()


def test_10c_egress_check_network_failure_is_recorded_not_raised(ready, monkeypatch):
    from backend import live_preflight

    def unreachable():
        raise OSError("network unreachable")
    monkeypatch.setattr(live_preflight, "_fetch_egress_ip", unreachable)
    data = ready.client.post("/api/live-readiness/egress-check").json()["data"]
    assert data["status"] == "USER_CONFIRMED" and "OSError" in data["egress_error"]


@pytest.mark.parametrize("ip", ["10.0.0.5", "127.0.0.1", "192.168.1.20", "not-an-ip", ""])
def test_10d_only_a_public_ip_can_be_confirmed(ready, ip):
    r = ready.client.post("/api/live-readiness/static-ip", json={"ip": ip, "confirm": True})
    assert r.status_code == 400 and r.json()["detail"]["error_code"] == "INVALID_STATIC_IP"


def test_10e_confirmation_must_be_explicit(ready):
    r = ready.client.post("/api/live-readiness/static-ip", json={"ip": PUBLIC_IP})
    assert r.status_code == 400 and r.json()["detail"]["error_code"] == "CONFIRMATION_REQUIRED"
    assert _rows(ready, "SELECT static_ip FROM live_readiness")[0]["static_ip"] == TEST_STATIC_IP


def test_10f_clear_withdraws_the_confirmation(ready):
    r = ready.client.post("/api/live-readiness/static-ip/clear")
    assert r.json()["data"]["status"] == "USER_VERIFICATION_REQUIRED"


def test_10g_static_ip_writes_require_the_csrf_header(ready):
    from fastapi.testclient import TestClient
    from backend.main import app
    bare = TestClient(app, base_url="http://127.0.0.1:8000")
    r = bare.post("/api/live-readiness/static-ip", json={"ip": PUBLIC_IP, "confirm": True})
    assert r.status_code == 403
    assert _rows(ready, "SELECT static_ip FROM live_readiness")[0]["static_ip"] == TEST_STATIC_IP


# ---------------------------------------------------------------- SETTINGS (11-14)

def test_11_trading_lock_blocks(ready):
    from backend import account_safety
    conn = ready.db.get_connection()
    try:
        account_safety.set_trading_lock(conn, account_id(ready.db), True, "reviewing")
    finally:
        conn.close()
    result = _evaluate()
    check = _check(result, "TRADING_LOCK")
    assert check["status"] == "USER_ACTION" and check["blocking"] is True
    assert result["overall_status"] == "NOT_READY"
    assert result["ready_except_trading_lock"] is True       # everything else passed: unlock is the last step


def test_12_max_notional_unset_blocks(ready):
    _exec(ready, "UPDATE risk_settings SET max_order_notional = NULL")
    result = _evaluate()
    assert _check(result, "MAX_ORDER_NOTIONAL_SET")["status"] == "FAIL"
    assert "MAX_ORDER_NOTIONAL_SET" in _blocking(result) and result["ready_except_trading_lock"] is False


def test_13_daily_loss_unset_blocks(ready):
    _exec(ready, "UPDATE risk_settings SET max_daily_loss_amount = NULL, max_daily_loss_pct = 2")
    result = _evaluate()
    check = _check(result, "DAILY_LOSS_AMOUNT_SET")
    assert check["status"] == "FAIL" and "₹" in check["message"]   # a % limit alone is not enough
    assert result["overall_status"] == "NOT_READY"


def test_14_configured_settings_pass(ready):
    _exec(ready, "UPDATE risk_settings SET max_order_notional = 25000, max_daily_loss_amount = 1500, "
                 "max_new_entries_per_day = 1, max_entry_price_deviation_pct = 1.5")
    result = _evaluate()
    for code in ("MAX_ORDER_NOTIONAL_SET", "DAILY_LOSS_AMOUNT_SET", "DAILY_ENTRY_LIMIT_SET",
                 "PRICE_DEVIATION_GUARD_SET", "CIRCUIT_BREAKER"):
        assert _check(result, code)["status"] == "PASS", code
    assert "₹25,000.00" in _check(result, "MAX_ORDER_NOTIONAL_SET")["message"]
    assert result["overall_status"] == "READY"


def test_14b_daily_entry_limit_and_deviation_guard_unset_block(ready):
    _exec(ready, "UPDATE risk_settings SET max_new_entries_per_day = NULL, max_entry_price_deviation_pct = NULL")
    blocking = _blocking(_evaluate())
    assert {"DAILY_ENTRY_LIMIT_SET", "PRICE_DEVIATION_GUARD_SET"} <= blocking


def test_14c_open_breaker_blocks(ready):
    _exec(ready, "UPDATE account_safety_state SET breaker_open = 1, breaker_reason = 'three failures'")
    assert "CIRCUIT_BREAKER" in _blocking(_evaluate())


# ---------------------------------------------------------------- WORKERS (15-17)

def test_15_required_workers_healthy(ready):
    result = _evaluate()
    for code in ("WORKER_ORDER_MONITOR", "WORKER_TRAILING", "WORKER_RECONCILIATION"):
        assert _check(result, code)["status"] == "PASS", code


def test_16_dead_order_monitor_blocks(ready, monkeypatch):
    from backend import workers
    monkeypatch.setattr(workers.supervisor, "is_running", lambda name: name != "order_monitor")
    result = _evaluate()
    assert _check(result, "WORKER_ORDER_MONITOR")["status"] == "FAIL"
    assert result["overall_status"] == "NOT_READY"


def test_16b_stale_order_monitor_blocks(ready):
    from backend import config
    later = datetime.now(timezone.utc) + timedelta(seconds=config.WORKER_STALE_AFTER_SECONDS["order_monitor"] + 5)
    check = _check(_evaluate(now=later), "WORKER_ORDER_MONITOR")
    assert check["status"] == "FAIL" and "stalled" in check["message"]


def test_16c_failing_or_never_cycled_order_monitor_blocks(ready):
    from backend import worker_health
    for _ in range(worker_health.FAILING_AFTER):
        worker_health.record_failure("order_monitor", RuntimeError("database is locked"))
    assert _check(_evaluate(), "WORKER_ORDER_MONITOR")["status"] == "FAIL"
    worker_health.reset()
    assert "has not completed a cycle" in _check(_evaluate(), "WORKER_ORDER_MONITOR")["message"]


def test_17_reconciliation_worker_unavailable_blocks(ready, monkeypatch):
    from backend import reconciliation, workers
    monkeypatch.setattr(workers.supervisor, "is_running", lambda name: name != "reconciliation")
    reconciliation.last_cycle.update(gtt_book="no_session", holdings="no_session")
    result = _evaluate()
    assert _check(result, "WORKER_RECONCILIATION")["status"] == "FAIL"
    assert _check(result, "RECON_LAST_CYCLE")["status"] == "FAIL"


def test_17b_paper_worker_and_ticker_only_warn(ready):
    from backend import worker_health
    for _ in range(worker_health.FAILING_AFTER):
        worker_health.record_failure("order_monitor_paper", RuntimeError("x"))
    result = _evaluate()
    assert _check(result, "WORKER_PAPER")["status"] == "WARN"
    assert _check(result, "TICKER_CONNECTED")["status"] == "WARN"      # tests never start the ticker
    assert result["overall_status"] == "READY"


# ---------------------------------------------------------------- RECONCILIATION (18-20)

def _finding(env, code, resolved=0):
    _exec(env, "INSERT INTO reconciliation_log (check_type, detail, issue_code, severity, symbol, resolved) "
               "VALUES ('position_state', 'test finding', ?, 'urgent', 'TCS', ?)", code, resolved)


def test_18_healthy_reconciliation_passes(ready):
    result = _evaluate()
    assert _check(result, "RECON_LAST_CYCLE")["status"] == "PASS"
    assert _check(result, "RECON_CRITICAL_UNRESOLVED")["status"] == "PASS"


def test_19_critical_unresolved_issue_blocks(ready):
    _finding(ready, "ORPHAN_GTT")
    result = _evaluate()
    check = _check(result, "RECON_CRITICAL_UNRESOLVED")
    assert check["status"] == "FAIL" and "ORPHAN_GTT" in check["message"]
    assert result["overall_status"] == "NOT_READY"


def test_19b_an_unclassified_needs_review_finding_counts_as_critical(ready):
    _exec(ready, "INSERT INTO reconciliation_log (check_type, detail) VALUES ('order_status', 'needs review')")
    assert "RECON_CRITICAL_UNRESOLVED" in _blocking(_evaluate())


def test_20_reviewed_critical_and_unresolved_noncritical_do_not_block(ready):
    _finding(ready, "BROKER_POSITION_SHORTFALL", resolved=1)       # reviewed: history only
    _finding(ready, "GTT_QUANTITY_TOO_LOW")                        # non-critical: a warning
    result = _evaluate()
    assert _check(result, "RECON_CRITICAL_UNRESOLVED")["status"] == "PASS"
    assert _check(result, "RECON_NONCRITICAL_UNRESOLVED")["status"] == "WARN"
    assert result["overall_status"] == "READY"


def test_20b_stale_or_unreadable_reconciliation_blocks(ready):
    from backend import reconciliation
    reconciliation.last_cycle.update(gtt_book="unavailable")
    assert _check(_evaluate(), "RECON_LAST_CYCLE")["status"] == "FAIL"
    _reconciled()
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert _check(_evaluate(now=later), "RECON_LAST_CYCLE")["status"] == "FAIL"


# ---------------------------------------------------------------- MARKET DATA (21-23)

def test_21_fresh_strategy_history_passes(ready):
    result = _evaluate()
    assert _check(result, "STRATEGY_HISTORY_FRESH")["status"] == "PASS"
    assert _check(result, "MARKET_DATA_LTP")["status"] == "PASS"
    assert _check(result, "CALENDAR_YEAR")["status"] == "PASS"


def test_22_stale_strategy_history_blocks(ready):
    stale_end = _latest_session() - timedelta(days=10)
    seed_ohlc(ready.db, 408065, "INFY", make_candles(stale_end, 60, 1500.0))
    add_watch(ready.db, "INFY", 408065)
    result = _evaluate()
    check = _check(result, "STRATEGY_HISTORY_FRESH")
    assert check["status"] == "FAIL" and "INFY" in check["message"]
    assert result["overall_status"] == "NOT_READY"


def test_23_stale_research_only_history_does_not_block(ready):
    stale = (_latest_session() - timedelta(days=30)).isoformat()
    _exec(ready, "INSERT INTO ohlc_history_status (instrument_token, symbol, last_date, bar_count, status) "
                 "VALUES (999001, 'RESEARCHONLY', ?, 500, 'OK')", stale)
    result = _evaluate()
    check = _check(result, "RESEARCH_HISTORY_STALE")
    assert check["status"] == "WARN" and check["blocking"] is False
    assert result["overall_status"] == "READY"


def test_23b_needs_refresh_watchlist_symbol_warns(ready):
    _exec(ready, "INSERT OR REPLACE INTO ohlc_history_status (instrument_token, symbol, last_date, bar_count, status) "
                 "VALUES (2953217, 'TCS', ?, 60, 'NEEDS_REFRESH')", _latest_session().isoformat())
    result = _evaluate()
    assert _check(result, "HISTORY_NEEDS_REFRESH")["status"] == "WARN"
    assert result["overall_status"] == "READY"


def test_23c_unreadable_price_blocks(ready):
    ready.kite.raise_on_next("ltp", TokenException("expired"))
    assert _check(_evaluate(), "MARKET_DATA_LTP")["status"] == "FAIL"


def test_23d_uncovered_calendar_year_blocks(ready):
    from backend import trading_calendar
    later = datetime(2027, 3, 1, 5, 0, tzinfo=timezone.utc)
    assert 2027 not in trading_calendar.calendar_status(later)["covered_years"]
    assert _check(_evaluate(now=later), "CALENDAR_YEAR")["status"] == "FAIL"


# ---------------------------------------------------------------- UNKNOWN (24)

def test_24_unresolved_live_unknown_blocks_readiness_and_new_live_entries(ready, market_open):
    from backend import execution
    from test_live_safety import _unknown_ticket
    unknown = _unknown_ticket(ready, submitted_at=execution.utc_now_sql())
    result = _evaluate()
    check = _check(result, "UNKNOWN_LIVE_ORDERS")
    assert check["status"] == "FAIL" and f"#{unknown}" in check["message"]
    set_mode(ready.db, "live")
    tid = _live_entry(ready)
    mark = len(ready.kite.calls)
    r = _confirm(ready, tid)
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "LIVE_PREREQUISITE_MISSING"
    assert "unknown broker outcome" in r.json()["detail"]["message"]
    assert ready.kite.calls[mark:] == []


def test_24b_needs_review_and_stale_submitting_count_too(ready):
    from test_live_safety import _unknown_ticket
    _unknown_ticket(ready, submission_state="needs_review")
    _unknown_ticket(ready, submission_state="submitting", submitted_at="2026-01-01 04:00:00")
    assert "2 live order(s)" in _check(_evaluate(), "UNKNOWN_LIVE_ORDERS")["message"]


def test_24c_a_fresh_in_flight_claim_is_not_unknown(ready):
    from backend import execution
    from test_live_safety import _unknown_ticket
    _unknown_ticket(ready, submission_state="submitting", submitted_at=execution.utc_now_sql())
    assert _check(_evaluate(), "UNKNOWN_LIVE_ORDERS")["status"] == "PASS"


# ---------------------------------------------------------------- MODE (25-26)

def test_25_dry_run_still_works_while_live_is_not_ready(ready, market_open):
    clear_live_prerequisites(ready.db)
    assert _evaluate()["overall_status"] == "NOT_READY"
    set_mode(ready.db, "dry_run")
    r = _confirm(ready, _live_entry(ready))
    assert r.status_code == 200 and r.json()["data"]["mode"] == "dry_run"
    assert _broker_mutations(ready) == []


def test_26_preflight_never_switches_the_mode(ready):
    set_mode(ready.db, "dry_run")
    result = _evaluate()
    check = _check(result, "EXECUTION_MODE")
    assert check["status"] == "USER_ACTION" and check["blocking"] is False
    assert result["overall_status"] == "READY"                     # technical readiness; mode is the user's call
    assert _rows(ready, "SELECT mode FROM execution_settings")[0]["mode"] == "dry_run"
    set_mode(ready.db, "live")
    _evaluate()
    assert _rows(ready, "SELECT mode FROM execution_settings")[0]["mode"] == "live"


# ---------------------------------------------------------------- ISOLATION (27-30)

def test_27_preflight_makes_zero_order_mutation_calls(ready, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("the pre-flight called a mutating broker method")
    for name in MUTATING_BROKER_CALLS:
        monkeypatch.setattr(ready.kite, name, forbidden, raising=False)
    _evaluate()
    _evaluate(broker=False)
    ready.client.get("/api/live-readiness?broker=true&refresh=true")


def test_28_29_preflight_writes_nothing_tickets_positions_or_otherwise(ready):
    from test_live_safety import _live_position, _unknown_ticket
    _live_position(ready)
    _unknown_ticket(ready)
    _finding(ready, "ORPHAN_GTT")
    before = _db_digest(ready)
    _evaluate()
    _evaluate(broker=False)
    ready.client.get("/api/live-readiness?broker=true&refresh=true")
    assert _db_digest(ready) == before


def test_30_preflight_never_unlocks_trading(ready):
    from backend import account_safety
    conn = ready.db.get_connection()
    try:
        account_safety.set_trading_lock(conn, account_id(ready.db), True, "first live review")
    finally:
        conn.close()
    for _ in range(2):
        _evaluate()
        ready.client.get("/api/live-readiness?broker=true")
    assert _rows(ready, "SELECT trading_locked FROM account_safety_state")[0]["trading_locked"] == 1


# ---------------------------------------------------------------- the confirm-time gate

def test_gate_refuses_a_live_entry_with_every_prerequisite_missing(env, market_open):
    clear_live_prerequisites(env.db)
    set_mode(env.db, "live")
    tid = _live_entry(env)
    mark = len(env.kite.calls)
    r = _confirm(env, tid)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error_code"] == "LIVE_PREREQUISITE_MISSING"
    for text in ("max order notional is not set", "rupee daily loss limit is not set", "static IP"):
        assert text in detail["message"]
    assert env.kite.calls[mark:] == []                              # refused before any broker call
    row = ticket_row(env.db, tid)
    assert row["status"] == "pending_confirmation" and row["submission_state"] is None


def test_gate_refuses_a_live_entry_when_the_static_ip_check_failed(env, market_open, monkeypatch):
    from backend import live_preflight
    monkeypatch.setattr(live_preflight, "_fetch_egress_ip", lambda: OTHER_PUBLIC_IP)
    env.client.post("/api/live-readiness/static-ip", json={"ip": PUBLIC_IP, "confirm": True})
    env.client.post("/api/live-readiness/egress-check")
    set_mode(env.db, "live")
    r = _confirm(env, _live_entry(env))
    assert r.status_code == 409 and "static IP check FAILED" in r.json()["detail"]["message"]
    assert _broker_mutations(env) == []


def test_gate_passes_once_the_prerequisites_are_met(env, market_open):
    set_mode(env.db, "live")
    r = _confirm(env, _live_entry(env))
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_gate_never_blocks_an_exit(env):
    from test_live_safety import _live_position, _pending_exit
    set_mode(env.db, "live")
    buy_id, _ = _live_position(env)
    sell_id = _pending_exit(env, buy_id)
    clear_live_prerequisites(env.db)
    r = env.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_gate_never_blocks_emergency_exit(env):
    from test_live_safety import _live_position
    set_mode(env.db, "live")
    _live_position(env)
    clear_live_prerequisites(env.db)
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    assert {x["symbol"]: x["status"] for x in r.json()["data"]["results"]}["TCS"] == "exit_submitted"


# ---------------------------------------------------------------- routes, secrets, payload semantics

def test_route_returns_the_preflight_and_no_secret(ready):
    from backend import crypto
    token = "live0-sentinel-token-value"
    _exec(ready, "UPDATE accounts SET access_token_enc = ?", crypto.encrypt(token))
    r = ready.client.get("/api/live-readiness?broker=true&refresh=true")
    assert r.status_code == 200
    body = r.text
    assert r.json()["data"]["overall_status"] in ("READY", "NOT_READY")
    for secret in (token, "access_token", "api_secret", "test_api_secret", "fake-access-token", "DT0001"):
        assert secret not in body


def test_route_works_without_a_session(env):
    _exec(env, "UPDATE sessions SET login_time = '2026-01-02T09:00:00+05:30'")
    data = env.client.get("/api/live-readiness").json()["data"]
    assert data["overall_status"] == "NOT_READY"
    assert _check(data, "BROKER_SESSION")["status"] == "FAIL"


def test_payload_semantics_flag_what_the_exposure_maths_would_misread(ready):
    from backend import live_preflight
    snap = live_preflight.BrokerSnapshot(
        taken_at="t", holdings=[{"tradingsymbol": "X", "quantity": None, "t1_quantity": 0, "last_price": 10.0,
                                 "collateral_quantity": 3, "mtf": {"quantity": 2}, "short_quantity": 1,
                                 "discrepancy": True}],
        positions={"net": [{"product": "CNC", "quantity": 1}], "day": []}, margins={"commodity": {}})
    semantics = live_preflight.payload_semantics(snap)
    problems = " ".join(semantics["problems"])
    assert "holdings.quantity" in problems and "overnight_quantity" in problems and "equity" in problems
    review = " ".join(semantics["review"])
    for text in ("pledged", "Margin Trading Facility", "short_quantity", "discrepancy"):
        assert text in review


def test_holdings_needing_review_warn_but_do_not_block(ready):
    ready.kite.set_holding("RELIANCE", 10)
    ready.kite._holdings[0]["mtf"] = {"quantity": 4}
    result = _evaluate()
    check = _check(result, "HOLDINGS_NEED_REVIEW")
    assert check["status"] == "WARN" and "Margin Trading Facility" in check["message"]
    assert result["overall_status"] == "READY"


def test_payload_shape_is_sanitized(ready):
    from backend import live_preflight
    ready.kite.add_broker_order("INFY", "BUY", 3, tag="secret-tag")
    snap = live_preflight.take_snapshot(ready.kite, "NSE:TCS")
    shape = str(live_preflight.payload_shape(snap))
    for value in ("RELIANCE", "DT0001", "Demo Trader", "secret-tag", "1300.0", "1200.0"):
        assert value not in shape
    assert "t1_quantity" in shape and "last_price" in shape                  # structure is kept


def test_real_kite_payload_structure_carries_every_field_pk14_pk15_reads():
    """tests/fixtures/kite_payload_shapes.json: keys and type names of a real
    read-only Kite snapshot (LIVE-0C), no values. Positions had no rows that
    day, so their fields are not asserted here (see the LIVE-0 report)."""
    import json
    from pathlib import Path
    from backend import live_preflight
    shape = json.loads((Path(__file__).parent / "fixtures" / "kite_payload_shapes.json").read_text(encoding="utf-8"))
    holding = shape["holdings"]["row_keys"]
    for field in live_preflight.HOLDING_FIELDS_USED + ("close_price",):
        assert field in holding, field
    for field in ("quantity", "t1_quantity", "collateral_quantity", "used_quantity"):
        assert holding[field] == ["int"], field
    for field in ("average_price", "last_price", "close_price"):
        assert set(holding[field]) <= {"int", "float"}, field
    equity = shape["margins"]["equity"]
    assert equity["net"] in ("int", "float") and equity["available"]["collateral"] in ("int", "float")
    assert "access_token" not in json.dumps(shape) and "@" not in json.dumps(shape)


def test_broken_payload_blocks_readiness(ready):
    ready.kite.margins = lambda: {"equity": {"available": {"cash": 5000.0}}}      # no equity.net
    result = _evaluate()
    check = _check(result, "BROKER_PAYLOAD_SHAPE")
    assert check["status"] == "FAIL" and "equity.net" in check["message"]


# ---------------------------------------------------------------- LIVE-0K clean shutdown with an open tab

WS_URL = "ws://127.0.0.1:8000/ws/ticks"


def _wait_for(predicate, seconds=5.0):
    import time
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_the_tick_handler_ends_on_disconnect_while_no_tick_flows():
    """The regression proof. Before LIVE-0 the handler only awaited its
    queue: with the market closed (no ticks) it never saw the disconnect —
    nor the server's own close on shutdown — so a graceful shutdown waited on
    it until the browser went away. Driven at the ASGI level, because the
    test client cancels the handler itself when it closes."""
    import asyncio
    from backend.routes import ws
    from backend.ticker import ticker_manager
    before = ticker_manager.listener_count()

    class ClosedTab:
        async def accept(self):
            pass

        async def receive(self):
            return {"type": "websocket.disconnect", "code": 1001}

        async def send_json(self, message):
            raise AssertionError("nothing was queued, so nothing may be sent")

        async def close(self, **kwargs):
            pass

    async def run():
        await asyncio.wait_for(ws.ticks_ws(ClosedTab()), timeout=3)

    asyncio.run(run())                                  # TimeoutError here = the old hang
    assert ticker_manager.listener_count() == before


def test_a_closed_tab_ends_the_tick_socket_even_when_no_tick_flows(env):
    """End to end through the test client: the socket closes cleanly and the
    listener is released. (The client cancels the handler on close, so this
    alone cannot prove the shutdown fix; the test above does.) The session
    runs in a thread so a regression fails, not hangs."""
    import threading
    from backend.ticker import ticker_manager
    before = ticker_manager.listener_count()
    closed = threading.Event()

    def session():
        with env.client.websocket_connect(WS_URL):
            assert _wait_for(lambda: ticker_manager.listener_count() == before + 1)
        closed.set()

    thread = threading.Thread(target=session, daemon=True)
    thread.start()
    thread.join(10)
    assert closed.is_set(), "the tick socket did not end after the client closed it"
    assert _wait_for(lambda: ticker_manager.listener_count() == before)


def test_the_tick_socket_still_delivers_events_before_ticks(env, monkeypatch):
    # Pre-filled like the PK-22 overflow test: production hands messages over
    # on the event loop (call_soon_threadsafe), which a test thread cannot.
    from backend.routes import ws
    from backend.tick_queue import ClientQueue

    def prefilled():
        q = ClientQueue()
        q.put_nowait({"type": "ticks", "data": [{"instrument_token": 1, "last_price": 10.0}]})
        q.put_nowait({"type": "order_update", "n": 1})
        return q

    monkeypatch.setattr(ws, "ClientQueue", prefilled)
    with env.client.websocket_connect(WS_URL) as socket:
        assert socket.receive_json() == {"type": "order_update", "n": 1}
        assert socket.receive_json() == {"type": "ticks", "data": [{"instrument_token": 1, "last_price": 10.0}]}


# ---------------------------------------------------------------- migration v5 and the maintenance CLI

def test_migration_v5_creates_the_live_readiness_table_idempotently(env):
    from backend import db
    assert db.LATEST_SCHEMA_VERSION == 5
    names = {r["name"] for r in _rows(env, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "live_readiness" in names
    env.db.init_db()
    env.db.init_db()
    assert env.db.last_init == {"applied_migrations": [], "startup_row_changes": 0, "schema_version": 5}


def test_maintenance_migrate_applies_the_pending_migration(env, capsys):
    from backend import db_maintenance
    _exec(env, "DROP TABLE live_readiness")
    _exec(env, "DELETE FROM schema_version WHERE version = 5")
    assert db_maintenance.main(["migrate", "--db", str(env.db.DB_PATH)]) == 0
    out = capsys.readouterr().out
    assert "v4 -> v5" in out and "applied migrations:  [5]" in out
    assert "live_readiness" in {r["name"] for r in _rows(env, "SELECT name FROM sqlite_master")}
    assert list(env.tmp_path.glob("test.db.pre_schema_v5_bak*"))            # the verified automatic backup
    assert db_maintenance.main(["migrate", "--db", str(env.db.DB_PATH)]) == 0
    assert "applied migrations:  none" in capsys.readouterr().out


def test_maintenance_migrate_refuses_without_the_encryption_key(env, monkeypatch, capsys):
    from backend import crypto, db_maintenance
    monkeypatch.delenv("KITE_TERMINAL_ENCRYPTION_KEY")
    monkeypatch.setattr(crypto, "ENV_PATH", env.tmp_path / "missing.env")
    _exec(env, "DELETE FROM schema_version WHERE version = 5")
    before = _db_digest(env)
    assert db_maintenance.main(["migrate", "--db", str(env.db.DB_PATH)]) == 2
    assert "refusing to migrate" in capsys.readouterr().err
    assert _db_digest(env) == before
    assert not (env.tmp_path / "missing.env").exists()                        # no key was minted
