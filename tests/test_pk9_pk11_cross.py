"""Cross-packet checks for PK-9 (manual cancel), PK-10 (Emergency Exit
isolation) and PK-11 (approval mode binding), together with the PK-1 atomic
claim, PK-2 UNKNOWN recovery, PK-5 CSRF guard and PK-8 EXITING lifecycle.
Races are forced with barriers and broker-call hooks — no sleeps.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from conftest import TERMINAL_ORIGIN, set_mode, ticket_row
from test_baseline_api import _manual_ticket
from test_exit_lifecycle import _exiting_position, _fill, _still_open_and_protected
from test_live_safety import (  # noqa: F401 — market_open is a fixture
    _call, _closed_trades_for, _event_types, _live_position, _monitor, _pending_exit, _precondition, _rows, _sells,
    market_open,
)
from test_emergency_exit_isolation import _unknown_exit


def _emergency_exit(env):
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    assert r.status_code == 200, r.text
    return r.json()["data"]


# ---------------------------------------------------------------- PK-9 x PK-10 x PK-8

def test_cancel_requested_exit_then_emergency_exit_never_doubles_the_sell(env, market_open):
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env, entry_order_type="LIMIT")
    env.client.post(f"/api/orders/{order_id}/cancel")

    # Before the monitor has seen the cancellation, the exit still owns the
    # position: Emergency Exit must not send a sibling SELL.
    [result] = _emergency_exit(env)["results"]
    assert (result["status"], result["ticket_id"]) == ("already_exiting", sell_id)
    assert len(_sells(env)) == 1
    assert not _still_open_and_protected(env, buy_id, gtt_id)

    _monitor(env)  # Kite reports CANCELLED: the exit ends, the position is untouched
    assert ticket_row(env.db, sell_id)["status"] == "failed"
    assert not _still_open_and_protected(env, buy_id, gtt_id)

    [again] = _emergency_exit(env)["results"]
    assert again["status"] == "exit_submitted" and again["ticket_id"] != sell_id
    assert len(_sells(env)) == 2  # exactly one new SELL


def test_emergency_exit_with_unknown_exit_sends_nothing_for_it(env, market_open):
    set_mode(env.db, "live")
    buy_id, gtt_id = _live_position(env)
    unknown = _unknown_exit(env, buy_id, "TCS")

    data = _emergency_exit(env)

    assert [(r["status"], r["ticket_id"]) for r in data["results"]] == [("exit_outcome_unknown", unknown)]
    assert data["summary"]["exit_outcome_unknown"] == 1
    assert _sells(env) == [] and ticket_row(env.db, unknown)["status"] == "confirmed"
    assert env.kite.gtt_book[gtt_id]["status"] == "active"


# ---------------------------------------------------------------- PK-9 x order monitor

def test_monitor_cycle_during_the_cancel_request_resolves_consistently(env, market_open):
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env, entry_order_type="LIMIT")

    # The monitor runs INSIDE the cancel call, before Kite applies it: it
    # sees OPEN and must leave the exit EXITING.
    env.kite.on_call("cancel_order", lambda _kwargs: _monitor(env))
    try:
        r = env.client.post(f"/api/orders/{order_id}/cancel")
    finally:
        env.kite.on_call("cancel_order", None)

    assert r.status_code == 200
    exit_row = ticket_row(env.db, sell_id)
    assert (exit_row["status"], exit_row["entry_fill_status"]) == ("executed", "pending")
    _monitor(env)
    assert ticket_row(env.db, sell_id)["status"] == "failed"
    assert not _still_open_and_protected(env, buy_id, gtt_id)


def test_cancel_not_yet_applied_then_fill_closes_once(env, market_open):
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env, entry_order_type="LIMIT")
    env.client.post(f"/api/orders/{order_id}/cancel")
    env.kite.set_order_state(order_id, "OPEN")  # the exchange has not processed the cancel yet

    _monitor(env)
    assert ticket_row(env.db, sell_id)["entry_fill_status"] == "pending"
    assert not _still_open_and_protected(env, buy_id, gtt_id)

    _fill(env, order_id, price=3105.0)
    _monitor(env)
    _monitor(env)
    assert ticket_row(env.db, buy_id)["closed_at"] is not None
    assert len(_closed_trades_for(env, buy_id)) == 1
    assert [c["trigger_id"] for c in env.kite.calls_to("delete_gtt")] == [gtt_id]


# ---------------------------------------------------------------- PK-11 x PK-1 atomic claim

def _race_two_confirms(env, monkeypatch, modes):
    """Both confirms pass the status check and meet at the barrier inside the
    global-mode read, so both genuinely reach the mode check and the claim."""
    from backend import execution
    from backend.models import TicketConfirmRequest
    from backend.routes import tickets
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    barrier = threading.Barrier(2, timeout=5)
    real = execution.get_execution_mode

    def synchronised():
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return real()
    monkeypatch.setattr(execution, "get_execution_mode", synchronised)

    def confirm(mode):
        return _call(tickets.confirm_ticket, t["id"], req=TicketConfirmRequest(expected_mode=mode), kite=env.kite)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=30) for f in [pool.submit(confirm, m) for m in modes]]
    _precondition(not barrier.broken, "both confirms should have met at the barrier")
    return t["id"], results


def test_two_correct_mode_confirms_still_place_exactly_one_order(env, market_open, monkeypatch):
    set_mode(env.db, "live")
    tid, results = _race_two_confirms(env, monkeypatch, ["live", "live"])

    assert sorted(code for code, _ in results) == [200, 409]
    assert next(body for code, body in results if code == 409)["error_code"] == "CONFLICT_INVALID_STATE"
    assert len(env.kite.calls_to("place_order")) == 1
    assert _event_types(env, tid).count("confirmed") == 1


def test_stale_and_correct_confirm_race_executes_once_in_the_shown_mode(env, market_open, monkeypatch):
    set_mode(env.db, "live")
    tid, results = _race_two_confirms(env, monkeypatch, ["dry_run", "live"])

    stale, fresh = results
    assert stale[0] == 409 and stale[1]["error_code"] == "EXECUTION_MODE_CHANGED"
    assert fresh[0] == 200 and fresh[1]["status"] == "executed" and fresh[1]["mode"] == "live"
    assert len(env.kite.calls_to("place_order")) == 1
    assert _event_types(env, tid).count("confirmed") == 1


# ---------------------------------------------------------------- PK-11 x PK-5 CSRF

@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example", "X-Kite-Terminal": "1"},
    {"Origin": TERMINAL_ORIGIN},
    {},
], ids=["foreign_origin_with_header", "same_origin_without_header", "no_origin_no_header"])
def test_valid_expected_mode_never_bypasses_the_csrf_guard(env, market_open, headers):
    from backend.main import app
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    before = ticket_row(env.db, t["id"])

    r = TestClient(app, base_url=TERMINAL_ORIGIN).post(
        f"/api/tickets/{t['id']}/confirm", json={"expected_mode": "live"}, headers=headers)

    assert r.status_code == 403
    assert ticket_row(env.db, t["id"]) == before
    assert env.kite.calls_to("place_order") == []
