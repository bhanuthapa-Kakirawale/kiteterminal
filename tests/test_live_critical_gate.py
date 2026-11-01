"""SEC-1 (P0-B): the confirm-time live critical-state gate.

A live NEW entry is re-checked at confirmation, before the claim
(account_safety.precheck) and again right before the broker call
(account_safety.final_gate), against the live state its safety rests on:
- reconciliation fresh, complete and not stamped in the future;
- no unresolved critical finding;
- order monitor, trailing and reconciliation loops healthy;
- paper/live ids isolated;
- the NSE calendar covering today.

These come from the same check builders as the Live Readiness page
(live_preflight.confirm_time_blockers). Stale, missing or inconsistent state
fails closed (503 LIVE_STATE_UNVERIFIED). A known-bad finding is 409
RECONCILIATION_CRITICAL, never permanent.

Protective actions are never gated: Emergency Exit, a linked exit, an
alert-action sell and a switch back to dry run all work in every degraded
state. Emergency Exit only names the affected positions.

conftest seeds a healthy, just-reconciled process for every test; each test
here breaks exactly one thing. FakeKite and a temporary database only.
"""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import approval_body, account_id, clear_healthy_live_state, displayed_mode, set_mode, ticket_row
from test_baseline_api import _insert_ticket, _manual_ticket
from test_emergency_exit_isolation import _by_symbol, _emergency_exit, _positions, _sold
from test_live_safety import _live_position, _pending_exit, _rows, _sells, market_open  # noqa: F401 (fixture)

BROKER_ENTRY_CALLS = ("place_order", "place_gtt", "modify_gtt", "ltp")


def _exec(env, sql, *args):
    conn = env.db.get_connection()
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _sql_utc(at: datetime) -> str:
    return at.strftime("%Y-%m-%d %H:%M:%S")


def _pending_entry(env) -> int:
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    assert t["status"] == "pending_confirmation", t
    return t["id"]


def _confirm(env, tid):
    before = len(env.kite.calls)
    r = env.client.post(f"/api/tickets/{tid}/confirm", json=approval_body(env.db, tid))
    return r, [name for name, _ in env.kite.calls[before:]]


def _critical_finding(env, issue_code="BROKER_POSITION_SHORTFALL", symbol="TCS", resolved=0) -> int:
    conn = env.db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO reconciliation_log (ticket_id, check_type, expected, actual, detail, issue_code, severity, "
            "symbol, resolved) VALUES (NULL, 'position_state', '5 shares', '0 shares', 'seeded finding', ?, "
            "'urgent', ?, ?)", (issue_code, symbol, resolved))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ---------------------------------------------------------------- the degraded states

def _recon(monkeypatch, **fields):
    from backend import reconciliation
    for key, value in fields.items():
        monkeypatch.setitem(reconciliation.last_cycle, key, value)


def _now_utc():
    return datetime.now(timezone.utc)


def recon_never_ran(env, monkeypatch):
    from backend import reconciliation
    monkeypatch.setattr(reconciliation, "last_cycle", {})


def recon_stale(env, monkeypatch):
    from backend import config
    age = config.WORKER_STALE_AFTER_SECONDS["reconciliation"] + 60
    _recon(monkeypatch, checked_at=_sql_utc(_now_utc() - timedelta(seconds=age)))


def recon_in_the_future(env, monkeypatch):
    _recon(monkeypatch, checked_at=_sql_utc(_now_utc() + timedelta(hours=1)))


def recon_gtt_book_unavailable(env, monkeypatch):
    _recon(monkeypatch, gtt_book="unavailable", gtt_error="NetworkException: timeout")


def recon_holdings_unavailable(env, monkeypatch):
    _recon(monkeypatch, holdings="unavailable", holdings_error="NetworkException: timeout")


def recon_checks_failed(env, monkeypatch):
    _recon(monkeypatch, failed_checks=["position_state"])


def _worker_dead(name):
    def degrade(env, monkeypatch):
        from backend import workers
        monkeypatch.setattr(workers.supervisor, "is_running", lambda loop: loop != name)
    degrade.__name__ = f"worker_{name}_dead"
    return degrade


def _worker_stale(name):
    def degrade(env, monkeypatch):
        from backend import worker_health
        monkeypatch.setitem(worker_health._workers[name], "last_ok_at", _sql_utc(_now_utc() - timedelta(hours=1)))
    degrade.__name__ = f"worker_{name}_stale"
    return degrade


def _worker_failing(name):
    def degrade(env, monkeypatch):
        from backend import worker_health
        for _ in range(worker_health.FAILING_AFTER):
            worker_health.record_failure(name, RuntimeError("database is locked"))
    degrade.__name__ = f"worker_{name}_failing"
    return degrade


def calendar_not_covered(env, monkeypatch):
    from backend import live_preflight
    real = live_preflight.trading_calendar.calendar_status

    def uncovered(now=None):
        status = dict(real(now))
        status["covered_years"] = []
        status["uncovered_years"] = [live_preflight.trading_calendar.as_ist(now).year]
        return status
    monkeypatch.setattr(live_preflight.trading_calendar, "calendar_status", uncovered)


UNVERIFIED_STATES = [
    recon_never_ran, recon_stale, recon_in_the_future, recon_gtt_book_unavailable, recon_holdings_unavailable,
    recon_checks_failed, calendar_not_covered,
    *[f(w) for w in ("order_monitor", "trailing", "reconciliation") for f in (_worker_dead, _worker_stale,
                                                                              _worker_failing)],
]


def _critical(issue_code):
    def degrade(env, monkeypatch):
        _critical_finding(env, issue_code=issue_code)
    degrade.__name__ = f"critical_{issue_code or 'NULL_CODE'}"
    return degrade


def paper_live_mix(env, monkeypatch):
    _insert_ticket(env.db, symbol="INFY", instrument_token=408065, kite_order_id="PAPER-1", status="executed",
                   mode="live", executed_at="2026-09-01 04:00:00", entry_fill_status="filled", filled_price=1500.0)


def _critical_states():
    from backend import lifecycle
    return [_critical(code) for code in sorted(lifecycle.CRITICAL_ISSUES)] + [_critical(None), paper_live_mix]


CRITICAL_STATES = _critical_states()


# ---------------------------------------------------------------- refusals before the claim

@pytest.mark.parametrize("degrade", UNVERIFIED_STATES, ids=lambda f: f.__name__)
def test_unverified_live_state_refuses_a_live_entry_without_any_broker_call(env, market_open, monkeypatch, degrade):
    set_mode(env.db, "live")
    tid = _pending_entry(env)
    degrade(env, monkeypatch)
    r, calls = _confirm(env, tid)
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "LIVE_STATE_UNVERIFIED" in {f["code"] for f in detail["failures"]}
    assert ticket_row(env.db, tid)["status"] == "pending_confirmation"      # still approvable, nothing claimed
    assert ticket_row(env.db, tid)["submission_state"] is None
    assert [c for c in calls if c in BROKER_ENTRY_CALLS] == []
    assert _rows(env, "SELECT 1 FROM system_events WHERE event_type = 'account_safety_refusal'")


@pytest.mark.parametrize("degrade", CRITICAL_STATES, ids=lambda f: f.__name__)
def test_critical_finding_refuses_a_live_entry_without_any_broker_call(env, market_open, monkeypatch, degrade):
    set_mode(env.db, "live")
    tid = _pending_entry(env)
    degrade(env, monkeypatch)
    r, calls = _confirm(env, tid)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error_code"] == "RECONCILIATION_CRITICAL"
    assert ticket_row(env.db, tid)["status"] == "pending_confirmation"      # never permanent
    assert [c for c in calls if c in BROKER_ENTRY_CALLS] == []


def test_fresh_process_refuses_every_live_entry(env, market_open, monkeypatch):
    # A restart (or the first minutes of a run): nothing reconciled, no loop
    # running. Every reason is named.
    set_mode(env.db, "live")
    tid = _pending_entry(env)
    clear_healthy_live_state(monkeypatch)
    r, calls = _confirm(env, tid)
    assert r.status_code == 503
    message = r.json()["detail"]["message"]
    for words in ("Reconciliation has not compared", "Order monitor", "Trailing", "not running"):
        assert words in message, message
    assert [c for c in calls if c in BROKER_ENTRY_CALLS] == []


def test_healthy_state_lets_a_live_entry_through(env, market_open):
    set_mode(env.db, "live")
    tid = _pending_entry(env)
    r, calls = _confirm(env, tid)
    assert r.status_code == 200, r.text
    assert "place_order" in calls


def test_a_reviewed_finding_no_longer_blocks(env, market_open):
    set_mode(env.db, "live")
    tid = _pending_entry(env)
    _critical_finding(env, resolved=1)
    r, calls = _confirm(env, tid)
    assert r.status_code == 200, r.text
    assert "place_order" in calls


@pytest.mark.parametrize("degrade", [recon_never_ran, _critical("BROKER_POSITION_SHORTFALL"),
                                     _worker_dead("order_monitor")], ids=lambda f: f.__name__)
def test_dry_run_entries_are_not_gated(env, market_open, monkeypatch, degrade):
    set_mode(env.db, "dry_run")
    tid = _pending_entry(env)
    degrade(env, monkeypatch)
    r, calls = _confirm(env, tid)
    assert r.status_code == 200, r.text
    assert "place_order" not in calls                                     # the paper broker, never Kite


# ---------------------------------------------------------------- refusal at the final gate (after the claim)

@pytest.mark.parametrize("degrade,status", [(_critical("ORPHAN_GTT"), 409), (recon_checks_failed, 503)],
                         ids=["finding_appears", "cycle_fails"])
def test_state_that_changes_after_the_claim_is_caught_before_the_broker_call(env, market_open, monkeypatch,
                                                                            degrade, status):
    from backend.routes import tickets
    set_mode(env.db, "live")
    tid = _pending_entry(env)
    real = tickets._revalidate_at_confirm

    def revalidate_then_degrade(conn, account, kite, ticket):
        ctx = real(conn, account, kite, ticket)
        degrade(env, monkeypatch)            # reconciliation reports between the claim and the broker call
        return ctx
    monkeypatch.setattr(tickets, "_revalidate_at_confirm", revalidate_then_degrade)
    r, calls = _confirm(env, tid)
    assert r.status_code == status, r.text
    row = ticket_row(env.db, tid)
    assert row["status"] == "pending_confirmation" and row["submission_state"] is None   # claim withdrawn
    assert "place_order" not in calls and "place_gtt" not in calls


# ---------------------------------------------------------------- protective paths are never gated

def _degrade_everything(env, monkeypatch):
    """Every gate the confirm path has, all failing at once."""
    clear_healthy_live_state(monkeypatch)
    _critical_finding(env, issue_code="BROKER_POSITION_SHORTFALL", symbol="TCS")
    _exec(env, "UPDATE account_safety_state SET breaker_open = 1, breaker_opened_at = datetime('now'), "
               "breaker_reason = 'seeded', trading_locked = 1, trading_locked_at = datetime('now')")


def test_emergency_exit_sells_in_every_degraded_state_and_names_the_finding(env, market_open, monkeypatch):
    _positions(env, "TCS", "INFY")
    _degrade_everything(env, monkeypatch)
    set_mode(env.db, "dry_run")                                  # the global toggle never stops a live exit
    r = _emergency_exit(env.client)
    assert r.status_code == 200, r.text
    assert sorted(_sold(env)) == ["INFY", "TCS"]
    results = _by_symbol(r)
    assert "BROKER_POSITION_SHORTFALL" in results["TCS"]["warning"]
    assert "warning" not in results["INFY"]


def test_emergency_exit_warning_lookup_failure_never_changes_an_outcome(env):
    # The warnings are attached after every exit was attempted, and a failure
    # to read the findings is swallowed: the results stay exactly as they were.
    from backend.routes import kill_switch
    conn = env.db.get_connection()
    conn.close()                                                  # every query on it now raises
    results = [{"symbol": "TCS", "status": "exit_submitted", "message": "placed"}]
    kill_switch._attach_critical_finding_warnings(conn, results)
    assert results == [{"symbol": "TCS", "status": "exit_submitted", "message": "placed"}]


def test_linked_exit_confirms_in_every_degraded_state(env, market_open, monkeypatch):
    set_mode(env.db, "live")
    buy_id, _ = _live_position(env)
    sell_id = _pending_exit(env, buy_id)
    _degrade_everything(env, monkeypatch)
    r = env.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200, r.text
    assert [s["tradingsymbol"] for s in _sells(env)] == ["TCS"]


def test_alert_action_sell_confirms_in_every_degraded_state(env, market_open, monkeypatch):
    set_mode(env.db, "live")
    r = env.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "RELIANCE", "quantity": 5, "entry_order_type": "MARKET", "entry_price": 1300.0,
        "rationale": "cut loss", "alert_type": "breach_loss"})
    assert r.status_code == 201, r.text
    tid = r.json()["data"]["id"]
    _degrade_everything(env, monkeypatch)
    r = env.client.post(f"/api/tickets/{tid}/confirm", json=approval_body(env.db, tid))
    assert r.status_code == 200, r.text
    assert [s["tradingsymbol"] for s in _sells(env)] == ["RELIANCE"]


def test_switch_back_to_dry_run_works_in_every_degraded_state(env, monkeypatch):
    set_mode(env.db, "live")
    _degrade_everything(env, monkeypatch)
    r = env.client.post("/api/execution/mode", json={"mode": "dry_run"})
    assert r.status_code == 200 and r.json()["data"]["mode"] == "dry_run"


# ---------------------------------------------------------------- mark reviewed: a logged act with a reason

@pytest.mark.parametrize("body,status", [(None, 422), ({}, 422), ({"reason": ""}, 422), ({"reason": "   "}, 400)],
                         ids=["no_body", "no_reason", "empty", "blank"])
def test_mark_reviewed_needs_a_reason(env, body, status):
    fid = _critical_finding(env)
    r = env.client.post(f"/api/reconciliation/{fid}/mark-reviewed", json=body)
    assert r.status_code == status, r.text
    assert _rows(env, "SELECT resolved FROM reconciliation_log WHERE id = ?", fid) == [{"resolved": 0}]
    assert not _rows(env, "SELECT 1 FROM system_events WHERE event_type = 'reconciliation_reviewed'")


def test_mark_reviewed_is_logged_and_unblocks_live_entries(env, market_open):
    set_mode(env.db, "live")
    tid = _pending_entry(env)
    fid = _critical_finding(env, issue_code="ORPHAN_GTT", symbol="TCS")
    assert _confirm(env, tid)[0].status_code == 409
    r = env.client.post(f"/api/reconciliation/{fid}/mark-reviewed", json={"reason": "GTT deleted in Kite"})
    assert r.status_code == 200
    [event] = _rows(env, "SELECT detail FROM system_events WHERE event_type = 'reconciliation_reviewed'")
    assert f"#{fid}" in event["detail"] and "ORPHAN_GTT TCS" in event["detail"]
    assert "GTT deleted in Kite" in event["detail"]
    r, calls = _confirm(env, tid)
    assert r.status_code == 200 and "place_order" in calls


def test_reviewing_twice_logs_once(env):
    fid = _critical_finding(env)
    for _ in range(2):
        assert env.client.post(f"/api/reconciliation/{fid}/mark-reviewed", json={"reason": "ok"}).status_code == 200
    assert len(_rows(env, "SELECT 1 FROM system_events WHERE event_type = 'reconciliation_reviewed'")) == 1


# ---------------------------------------------------------------- one source of truth with the readiness page

@pytest.mark.parametrize("degrade,words", [(recon_checks_failed, "did not complete"),
                                           (recon_in_the_future, "in the future")], ids=["failed_checks", "future"])
def test_readiness_page_reports_the_tightened_reconciliation_rules(env, monkeypatch, degrade, words):
    from backend import live_preflight
    degrade(env, monkeypatch)
    result = live_preflight.evaluate(broker=False)
    [check] = [c for c in result["checks"] if c["code"] == "RECON_LAST_CYCLE"]
    assert check["status"] == live_preflight.FAIL and words in check["message"]
    assert "RECON_LAST_CYCLE" in result["blocking_codes"]


def test_confirm_time_blockers_are_local_and_read_only(env, monkeypatch):
    from backend import live_preflight
    clear_healthy_live_state(monkeypatch)
    _critical_finding(env)
    conn = env.db.get_connection()
    try:
        changes = conn.total_changes
        before = len(env.kite.calls)
        codes = {b["code"] for b in live_preflight.confirm_time_blockers(conn, account_id(env.db))}
        assert conn.total_changes == changes                       # no write
        assert len(env.kite.calls) == before                       # no broker call
    finally:
        conn.close()
    assert codes == {"RECON_LAST_CYCLE", "RECON_CRITICAL_UNRESOLVED", "WORKER_ORDER_MONITOR", "WORKER_TRAILING",
                     "WORKER_RECONCILIATION"}
    assert codes <= live_preflight.CONFIRM_TIME_CRITICAL_CODES | live_preflight.CONFIRM_TIME_UNVERIFIED_CODES


def test_gate_evaluation_error_fails_closed(env, market_open, monkeypatch):
    from backend import live_preflight
    set_mode(env.db, "live")
    tid = _pending_entry(env)

    def boom(*a, **k):
        raise RuntimeError("worker registry unreadable")
    monkeypatch.setattr(live_preflight, "confirm_time_blockers", boom)
    r, calls = _confirm(env, tid)
    assert r.status_code == 503 and "could not be evaluated" in r.text
    assert [c for c in calls if c in BROKER_ENTRY_CALLS] == []


# ---------------------------------------------------------------- migration v6

def test_migration_v6_adds_the_audit_event_types_and_the_arming_column(env):
    cols = {r["name"] for r in _rows(env, "PRAGMA table_info(execution_settings)")}
    assert "live_armed_session_id" in cols
    for event_type in ("reconciliation_reviewed", "live_switch_refused", "mode_reset_on_session_change",
                       "mode_switched", "account_safety_refusal"):
        _exec(env, "INSERT INTO system_events (account_id, event_type, detail) VALUES (?, ?, 'x')",
              account_id(env.db), event_type)
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        _exec(env, "INSERT INTO system_events (account_id, event_type, detail) VALUES (?, 'not_a_type', 'x')",
              account_id(env.db))
