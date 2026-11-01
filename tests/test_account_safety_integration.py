"""PK-12 integration scenarios and concurrency: the account safety gate in
the full confirm path, next to every earlier safety guarantee (PK-1 claim,
PK-2 UNKNOWN recovery, PK-4 revalidation, PK-8 exit lifecycle, PK-10
Emergency Exit, PK-11 mode binding).

Races use barriers, never sleeps; nothing reaches the network."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
import requests
from kiteconnect.exceptions import NetworkException

from conftest import approval_body, TERMINAL_ORIGIN, displayed_mode, set_mode, terminal_client, ticket_row
from test_account_safety import (  # noqa: F401 — live is a fixture
    _assert_untouched_pending, _closed_trade, _code, _codes, _confirm, _entry, _ignore_live0_unknown_gate, _lock,
    _record_failures, _risk, _session, _status, _sys_events, live,
)
from test_baseline_api import _insert_ticket
from test_live_safety import (
    _call, _live_position, _pending_exit, _precondition, _run_background_cycles, _unknown_ticket,
)


def _race_entries(env, monkeypatch, tickets_to_confirm):
    """Every confirm passes the account-safety PRE-check (all see a free slot)
    and waits at a barrier until all have; only then do they race for the
    claim — the one place the daily limit is binding."""
    from backend import account_safety
    from backend.models import TicketConfirmRequest
    from backend.routes import tickets
    barrier = threading.Barrier(len(tickets_to_confirm), timeout=5)
    real = account_safety.precheck

    def synchronised(*args, **kwargs):
        decision = real(*args, **kwargs)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return decision
    monkeypatch.setattr(account_safety, "precheck", synchronised)

    def confirm(tid):
        return _call(tickets.confirm_ticket, tid, req=TicketConfirmRequest(**approval_body(env.db, tid)), kite=env.kite)

    with ThreadPoolExecutor(max_workers=len(tickets_to_confirm)) as pool:
        results = [f.result(timeout=30) for f in [pool.submit(confirm, t) for t in tickets_to_confirm]]
    _precondition(not barrier.broken, "every confirm should have passed the pre-check before any claim")
    return results


# ================================================================= concurrency (PK-12C)

def test_concurrent_approvals_cannot_both_take_the_last_daily_slot(live, monkeypatch):
    _risk(live, max_new_entries_per_day=1)
    a, b = _entry(live), _entry(live)
    results = _race_entries(live, monkeypatch, [a, b])

    assert sorted(code for code, _ in results) == [200, 409]
    loser = next(body for code, body in results if code == 409)
    assert loser["error_code"] == "DAILY_ENTRY_LIMIT"
    assert len(live.kite.calls_to("place_order")) == 1
    statuses = sorted(ticket_row(live.db, t)["status"] for t in (a, b))
    assert statuses == ["executed", "pending_confirmation"]
    loser_id = a if ticket_row(live.db, a)["status"] == "pending_confirmation" else b
    _assert_untouched_pending(live, loser_id)


def test_three_way_race_for_two_slots_places_exactly_two_orders(live, monkeypatch):
    _risk(live, max_new_entries_per_day=2)
    ids = [_entry(live) for _ in range(3)]
    results = _race_entries(live, monkeypatch, ids)
    assert sorted(code for code, _ in results) == [200, 200, 409]
    assert len(live.kite.calls_to("place_order")) == 2
    assert _status(live)["entries_today"] == 2


def test_race_with_an_unknown_submission_already_holding_a_slot(live, monkeypatch):
    from backend import execution
    from test_live_safety import _unknown_ticket
    _ignore_live0_unknown_gate(monkeypatch)  # the daily-slot accounting of an UNKNOWN claim is under test
    _risk(live, max_new_entries_per_day=2)
    _unknown_ticket(live, submitted_at=execution.utc_now_sql())
    results = _race_entries(live, monkeypatch, [_entry(live), _entry(live)])
    assert sorted(code for code, _ in results) == [200, 409]
    assert len(live.kite.calls_to("place_order")) == 1


def test_race_while_an_entry_is_in_flight_counts_the_in_flight_claim(live, monkeypatch):
    """Entry A is claimed and inside place_order when B and C race for the
    one remaining slot: A's 'submitting' claim already holds its slot."""
    _risk(live, max_new_entries_per_day=2)
    a = _entry(live)
    b, c = _entry(live), _entry(live)
    results = {}

    def while_a_is_at_the_broker(_kwargs):
        live.kite.on_call("place_order", None)
        results["bc"] = _race_entries(live, monkeypatch, [b, c])

    live.kite.on_call("place_order", while_a_is_at_the_broker)
    r = _confirm(live, a)
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"
    assert sorted(code for code, _ in results["bc"]) == [200, 409]
    assert len(live.kite.calls_to("place_order")) == 2


def test_concurrent_dry_run_approvals_are_not_limited(env, market_open, monkeypatch):
    _risk(env, max_new_entries_per_day=1)
    results = _race_entries(env, monkeypatch, [_entry(env), _entry(env)])
    assert [code for code, _ in results] == [200, 200]
    assert env.kite.calls_to("place_order") == []


# ================================================================= integration scenarios

def _risk_passed_strategy_entry(env, signal_date=None) -> int:
    """A strategy entry that went through the real risk engine, as the scan
    creates it: draft -> run_risk_checks_and_transition -> pending."""
    from backend import risk_engine
    from backend.accounts import get_trading_account
    tid = _insert_ticket(env.db, source="strategy", strategy_name="momentum_breakout", status="draft", mode=None,
                         signal_date=(signal_date or _session()).isoformat())
    conn = env.db.get_connection()
    try:
        risk_engine.run_risk_checks_and_transition(conn, get_trading_account(), tid, env.kite)
    finally:
        conn.close()
    row = ticket_row(env.db, tid)
    _precondition(row["status"] == "pending_confirmation", f"strategy entry should pass risk: {row['status']}")
    return tid


def _prev_session():
    from backend import market
    d = _session() - timedelta(days=1)
    while not market.is_trading_day(d):
        d -= timedelta(days=1)
    return d


def _refused(env, r, code, tid):
    assert r.status_code in (409, 503), r.text
    assert code in [f["code"] for f in r.json()["detail"]["failures"]]
    assert env.kite.calls_to("place_order") == [] and env.kite.calls_to("place_gtt") == []
    return ticket_row(env.db, tid)


def test_s1_strategy_ticket_passing_risk_is_blocked_by_the_lock(live):
    tid = _risk_passed_strategy_entry(live)
    _lock(live)
    assert _refused(live, _confirm(live, tid), "TRADING_LOCKED", tid)["status"] == "pending_confirmation"


def test_s2_ticket_passing_risk_is_blocked_by_the_daily_count(live):
    _risk(live, max_new_entries_per_day=1)
    assert _confirm(live, _risk_passed_strategy_entry(live)).status_code == 200
    placed = len(live.kite.calls_to("place_order"))
    tid = _entry(live)
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "DAILY_ENTRY_LIMIT"
    assert len(live.kite.calls_to("place_order")) == placed


def test_s3_ticket_passing_risk_is_blocked_by_the_notional_cap(live):
    _risk(live, max_order_notional=10000.0)
    tid = _risk_passed_strategy_entry(live)
    assert _refused(live, _confirm(live, tid), "MAX_NOTIONAL_EXCEEDED", tid)["status"] == "pending_confirmation"


def test_s4_ticket_passing_risk_is_blocked_by_the_stale_signal_guard(live):
    tid = _risk_passed_strategy_entry(live, signal_date=_prev_session())
    assert _refused(live, _confirm(live, tid), "STALE_SIGNAL", tid)["status"] == "rejected"


def test_s5_ticket_passing_risk_is_blocked_when_price_moved_too_far(live):
    tid = _risk_passed_strategy_entry(live)
    live.kite.ltp_map["NSE:TCS"] = 3240.0                         # +8% since the signal
    row = _refused(live, _confirm(live, tid), "PRICE_DEVIATION_EXCEEDED", tid)
    assert row["status"] == "pending_confirmation" and row["entry_price"] == 3000.0


def test_s6_daily_loss_breached_after_a_previous_trade_closes(live):
    from backend import account_safety, order_monitor
    _risk(live, max_daily_loss_amount=1000.0)
    buy_id, _ = _live_position(live)
    sell_id = _pending_exit(live, buy_id)
    live.kite.auto_fill = False
    exit_order = live.client.post(f"/api/tickets/{sell_id}/confirm").json()["data"]["kite_order_id"]
    tid = _entry(live)                                            # approvable while the exit is working
    today = account_safety.now_ist().date().isoformat()
    live.kite.order_book[exit_order]["exchange_timestamp"] = f"{today} 10:15:00"
    live.kite.set_order_state(exit_order, "COMPLETE", filled_quantity=5, average_price=2800.0)
    order_monitor.check_pending_orders(live.kite)                 # stop-out: 5 x -200 = -1000
    _precondition(ticket_row(live.db, buy_id)["closed_at"] is not None, "the exit should have closed the position")
    assert _status(live)["realized_live_pnl_today"] == -1000.0
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "DAILY_LOSS_LIMIT"
    assert [c["transaction_type"] for c in live.kite.calls_to("place_order")] == ["SELL"]


def test_s7_mixed_broker_errors_trip_the_circuit_breaker(live, unsafe_client):
    _risk(live, max_new_entries_per_day=None)
    t1 = _entry(live)
    live.kite.raise_on_next("margins", NetworkException("Gateway timed out"))       # revalidation
    assert _code(_confirm(live, t1)) == "RISK_REVALIDATION_UNAVAILABLE"
    t2 = _entry(live)
    live.kite.raise_on_next("ltp", requests.ReadTimeout("read timeout"))             # account-safety LTP
    assert _code(_confirm(live, t2)) == "ACCOUNT_SAFETY_UNAVAILABLE"
    t3 = _entry(live)
    live.kite.raise_on_next("place_order", requests.ConnectionError("aborted"))      # UNKNOWN submission
    assert unsafe_client.post(f"/api/tickets/{t3}/confirm", json=approval_body(live.db, t3)).status_code == 200
    s = _status(live)
    assert s["breaker_open"] is True and len(_sys_events(live, "broker_breaker_tripped")) == 1
    t4 = _entry(live)
    # LIVE-0: t3's UNKNOWN outcome also blocks further live entries on its own.
    assert {"LIVE_PREREQUISITE_MISSING", "BROKER_CIRCUIT_OPEN"} <= set(_codes(_confirm(live, t4)))
    assert len(live.kite.calls_to("place_order")) == 1


def test_s8_outside_hours_approval_requires_explicit_amo(env, monkeypatch):
    from backend import market
    monkeypatch.setattr(market, "is_market_open", lambda at=None: False)
    set_mode(env.db, "live")
    tid = _risk_passed_strategy_entry(env)
    r = _confirm(env, tid)
    assert r.status_code == 409 and _code(r) == "AMO_CONFIRMATION_REQUIRED"
    _assert_untouched_pending(env, tid)
    r = _confirm(env, tid, allow_amo=True)
    assert r.status_code == 200 and r.json()["data"]["order_variety"] == "amo"


def _block_every_entry_control(env, monkeypatch):
    """Every PK-12 entry control (and, via the UNKNOWN ticket, the LIVE-0
    live prerequisite) is tripped at once; a new entry is refused with all of
    them, in the documented order."""
    from backend import execution, market
    _risk(env, max_new_entries_per_day=1, max_order_notional=1.0, max_daily_loss_amount=100.0,
          max_entry_price_deviation_pct=0.01)
    _closed_trade(env, -5000.0)
    _unknown_ticket(env, submitted_at=execution.utc_now_sql(), symbol="INFY", instrument_token=408065,
                    entry_price=1500.0, stop_loss=1450.0, target_1=1650.0)
    _record_failures(env, 3)
    _lock(env)
    monkeypatch.setattr(market, "is_market_open", lambda at=None: False)
    r = _confirm(env, _entry(env))
    assert r.status_code == 409
    assert [f["code"] for f in r.json()["detail"]["failures"]] == [
        "TRADING_LOCKED", "LIVE_PREREQUISITE_MISSING", "DAILY_LOSS_LIMIT", "DAILY_ENTRY_LIMIT", "MAX_NOTIONAL_EXCEEDED",
        "BROKER_CIRCUIT_OPEN", "AMO_CONFIRMATION_REQUIRED"]


def test_s9_exit_remains_possible_while_every_entry_control_is_blocking(live, monkeypatch):
    buy_id, _ = _live_position(live)
    sell_id = _pending_exit(live, buy_id)
    _block_every_entry_control(live, monkeypatch)
    r = live.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"
    assert [c["transaction_type"] for c in live.kite.calls_to("place_order")] == ["SELL"]


def test_s10_emergency_exit_remains_possible_while_every_entry_control_is_blocking(live, monkeypatch):
    _live_position(live)
    _block_every_entry_control(live, monkeypatch)
    r = live.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    results = {x["symbol"]: x["status"] for x in r.json()["data"]["results"]}
    assert results["TCS"] == "exit_submitted"
    assert [c["transaction_type"] for c in live.kite.calls_to("place_order")] == ["SELL"]


def test_s11_unknown_recovery_continues_while_the_breaker_is_open(live, unsafe_client):
    tid = _entry(live)
    live.kite.accept_then_raise("place_order", requests.ReadTimeout("Read timed out."))
    assert unsafe_client.post(f"/api/tickets/{tid}/confirm", json=approval_body(live.db, tid)).status_code == 200
    _record_failures(live, 3)
    _precondition(_status(live)["breaker_open"], "the breaker should be open")
    _run_background_cycles(live)
    row = ticket_row(live.db, tid)
    assert row["status"] == "executed" and row["submission_state"] == "adopted"
    assert len(live.kite.calls_to("place_order")) == 1           # recovered, never resubmitted


def test_s12_dry_run_is_not_mixed_into_live_safety_counters(env, market_open):
    _risk(env, max_new_entries_per_day=1, max_daily_loss_amount=1000.0)
    for _ in range(3):
        assert _confirm(env, _entry(env)).json()["data"]["mode"] == "dry_run"
    _closed_trade(env, -50000.0, mode="dry_run")
    s = _status(env)
    assert s["entries_today"] == 0 and s["realized_live_pnl_today"] == 0
    assert s["breaker_recent_failures"] == 0 and s["daily_loss_limit_hit"] is False
    set_mode(env.db, "live")
    r = _confirm(env, _entry(env))
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_mode_binding_is_checked_before_the_account_gate(live):
    # PK-11 first: a stale tab is told the mode changed, not about PK-12.
    _lock(live)
    tid = _entry(live)
    r = live.client.post(f"/api/tickets/{tid}/confirm", json=approval_body(live.db, tid, expected_mode="dry_run"))
    assert r.status_code == 409 and _code(r) == "EXECUTION_MODE_CHANGED"
    assert _sys_events(live, "account_safety_refusal") == []


def test_account_safety_endpoints_are_csrf_guarded(env):
    from fastapi.testclient import TestClient
    from backend.main import app
    for path, body in (("/api/account-safety/trading-lock", {"locked": True}),
                       ("/api/account-safety/breaker/reset", {})):
        r = TestClient(app, base_url=TERMINAL_ORIGIN).post(path, json=body,
                                                          headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
    assert _status(env)["trading_locked"] is False


@pytest.fixture
def unsafe_client(env):
    from backend.main import app
    return terminal_client(app, raise_server_exceptions=False)
