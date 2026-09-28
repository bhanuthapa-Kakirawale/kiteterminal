"""PK-0: live-safety characterisation tests for the eight P0 findings in
docs/OPUS55_AUDIT_REPORT.md.

Tests that pin a known defect are xfail(strict=True, raises=AssertionError):
the suite stays green today, and the moment the defect is fixed the test
XPASSes and fails the run, forcing it to be turned into a plain regression
test. Only AssertionError counts as the expected failure, so an unrelated
crash still fails the suite. Setup facts that must hold both before and
after a fix go through _precondition(), which raises a different exception,
so a broken setup can never hide behind an xfail.

Every xfail asserts only properties the audit's recommended fix satisfies,
so a correct fix turns it XPASS rather than leaving it silently xfailing.
Tests without a marker document behaviour that is already safe and must
stay that way.

No sleeps: races are forced with barriers/events, broker outcomes are
injected through FakeKite. Nothing here reaches Zerodha or the network.
"""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from fastapi.testclient import TestClient
from kiteconnect.exceptions import NetworkException

from conftest import displayed_mode, TERMINAL_ORIGIN, account_id, set_mode, terminal_client, ticket_row
from test_baseline_api import _insert_ticket, _manual_ticket
from test_review_changes import _breakeven_proposal


class PreconditionError(Exception):
    """Scenario setup did not produce the state the test relies on."""


def _precondition(cond, msg):
    if not cond:
        raise PreconditionError(msg)


def _xfail(finding, why):
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"{finding}: {why}")


# ---------------------------------------------------------------- helpers

@pytest.fixture
def market_open(monkeypatch):
    from backend import market
    monkeypatch.setattr(market, "is_market_open", lambda at=None: True)


@pytest.fixture
def market_closed(monkeypatch):
    from backend import market
    monkeypatch.setattr(market, "is_market_open", lambda at=None: False)


@pytest.fixture
def unsafe_client(env):
    """Returns a 500 response instead of re-raising, for paths where an
    unhandled exception IS the defect being demonstrated."""
    from backend.main import app
    return terminal_client(app, raise_server_exceptions=False)


def _rows(env, sql, *args):
    conn = env.db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _sells(env):
    return [c for c in env.kite.calls_to("place_order") if c["transaction_type"] == "SELL"]


def _held(env, symbol):
    return sum(h["quantity"] + h.get("t1_quantity", 0) for h in env.kite._holdings if h["tradingsymbol"] == symbol)


def _closed_trades_for(env, buy_id):
    return _rows(env, "SELECT * FROM closed_trades WHERE buy_ticket_id = ?", buy_id)


def _event_types(env, ticket_id):
    return [e["event_type"] for e in _rows(
        env, "SELECT event_type FROM ticket_events WHERE ticket_id = ? ORDER BY id", ticket_id)]


def _displayed(env):
    """The confirm body of a freshly loaded tab (PK-11), for tests that call
    the confirm route function directly rather than over HTTP."""
    from backend.models import TicketConfirmRequest
    return TicketConfirmRequest(**displayed_mode(env.db))


def _call(fn, *args, **kwargs):
    """Calls a route function directly: (200, data) or (status, detail)."""
    from fastapi import HTTPException
    try:
        return 200, fn(*args, **kwargs)["data"]
    except HTTPException as e:
        return e.status_code, e.detail


def _run_background_cycles(env):
    """One cycle of every background loop that watches live positions, so a
    test does not depend on which loop a future fix puts the logic in."""
    from backend import order_monitor, reconciliation, trailing_engine
    asyncio.run(trailing_engine._tick_once())
    order_monitor.check_pending_orders(env.kite)
    reconciliation._run_cycle()


def _protective_oco_gtt(env, symbol="TCS", quantity=5, stop=2900.0, target=3300.0):
    legs = [
        {"transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT", "product": "CNC",
         "price": round(stop * 0.995, 2)},
        {"transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT", "product": "CNC", "price": target},
    ]
    return env.kite.place_gtt(env.kite.GTT_TYPE_OCO, symbol, "NSE", [stop, target], 3000.0, legs)["trigger_id"]


def _live_position(env, **overrides) -> tuple[int, int]:
    """A filled live BUY 5 TCS, protected by an OCO GTT, held at the broker."""
    gtt_id = _protective_oco_gtt(env)
    fields = dict(source="strategy", strategy_name="momentum_breakout", kite_gtt_id=gtt_id,
                  executed_at="2026-09-01 04:00:00", entry_fill_status="filled", filled_price=3000.0,
                  current_stop_loss=2900.0)
    fields.update(overrides)
    buy_id = _insert_ticket(env.db, **fields)
    env.kite.set_holding("TCS", 5, average_price=3000.0)
    return buy_id, gtt_id


def _pending_exit(env, buy_id, **overrides) -> int:
    """A strategy exit awaiting approval, exactly as run_exit_scan creates it."""
    fields = dict(source="strategy", strategy_name="momentum_breakout_exit", transaction_type="SELL",
                  entry_order_type="MARKET", entry_price=3100.0, status="pending_confirmation", mode=None,
                  closes_ticket_id=buy_id, rule_checklist=json.dumps({"time_exit": {"pass": True}}))
    fields.update(overrides)
    return _insert_ticket(env.db, **fields)


def _open_live_entry(env, **ticket_overrides) -> dict:
    """A live entry taken through the real create -> confirm -> fill path."""
    from backend import order_monitor
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket(**ticket_overrides)).json()["data"]
    _precondition(t["status"] == "pending_confirmation", f"ticket not approvable: {t['status']}")
    done = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db)).json()["data"]
    _precondition(done["status"] == "executed" and done["kite_gtt_id"], f"live entry not placed: {done}")
    order_monitor.check_pending_orders(env.kite)
    row = ticket_row(env.db, t["id"])
    _precondition(row["entry_fill_status"] == "filled", f"entry not filled: {row['entry_fill_status']}")
    env.kite.set_holding("TCS", row["quantity"], average_price=row["entry_price"])
    return row


def _is_stranded(row: dict) -> bool:
    """'confirmed' with no order id and no unknown-outcome marker: no
    background loop ever looks at such a ticket again."""
    return (row["status"] == "confirmed" and row["kite_order_id"] is None
            and row["entry_fill_status"] is None and row.get("submission_state") is None)


def _ambiguous_outcome_violations(response, row) -> list[str]:
    violations = []
    if response.status_code >= 500:
        violations.append(f"confirm returned HTTP {response.status_code} instead of a handled UNKNOWN outcome")
    else:
        data = response.json()["data"]
        if data.get("success") is not False or "unknown" not in (data.get("execution_message") or "").lower():
            violations.append(f"response does not flag the outcome as unknown: {data.get('execution_message')!r}")
    if _is_stranded(row):
        violations.append(f"ticket {row['id']} stranded: status={row['status']!r}, kite_order_id=None, "
                          f"entry_fill_status=None — no loop will ever look at it again")
    return violations


# ================================================================ P0-1
# Safety property: one approval produces at most one broker order, however
# many confirm requests arrive for the same ticket.

def test_p0_1_second_confirm_while_first_in_flight_is_rejected(env, market_open):
    # Already safe: once the first confirm has committed 'confirmed' (it does
    # so before calling the broker), a later confirm is refused with 409.
    from backend.routes import tickets
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]

    entered, release = threading.Event(), threading.Event()

    def hold_inside_place_order(_kwargs):
        entered.set()
        release.wait(timeout=10)

    env.kite.on_call("place_order", hold_inside_place_order)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(tickets.confirm_ticket, t["id"], req=_displayed(env), kite=env.kite)
            assert entered.wait(timeout=10), "first confirm never reached place_order"
            second = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))
            release.set()
            first_result = first.result(timeout=10)
    finally:
        release.set()
        env.kite.on_call("place_order", None)

    assert second.status_code == 409
    assert first_result["data"]["status"] == "executed"
    assert len(env.kite.calls_to("place_order")) == 1


def test_p0_1_sequential_second_confirm_is_rejected(env, market_open):
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    first = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))
    second = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))

    assert first.status_code == 200 and first.json()["data"]["status"] == "executed"
    assert second.status_code == 409
    assert second.json()["detail"]["error_code"] == "CONFLICT_INVALID_STATE"
    assert len(env.kite.calls_to("place_order")) == 1
    assert _event_types(env, t["id"]).count("confirmed") == 1


def test_p0_1_concurrent_confirms_place_exactly_one_order(env, market_open, monkeypatch):
    from backend import execution
    from backend.routes import tickets
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]

    # get_execution_mode() runs after the status check and before the
    # conditional UPDATE that claims the ticket. Holding both threads there
    # means both have passed the check, so both genuinely race the UPDATE —
    # the race is certain rather than lucky.
    barrier = threading.Barrier(2, timeout=5)
    real_get_mode = execution.get_execution_mode

    def synchronised_get_mode():
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return real_get_mode()

    monkeypatch.setattr(execution, "get_execution_mode", synchronised_get_mode)

    def confirm():
        return _call(tickets.confirm_ticket, t["id"], req=_displayed(env), kite=env.kite)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=30) for f in [pool.submit(confirm), pool.submit(confirm)]]

    _precondition(not barrier.broken, "both confirms should have met at the barrier")
    orders = env.kite.calls_to("place_order")
    gtts = env.kite.calls_to("place_gtt")
    row = ticket_row(env.db, t["id"])
    violations = []
    if len(orders) != 1:
        untracked = [o for o in env.kite.order_book if o != row["kite_order_id"]]
        violations.append(f"{len(orders)} BUY orders placed for one ticket; ticket tracks "
                          f"{row['kite_order_id']}, untracked at broker: {untracked}")
    if list(env.kite.order_book) != [row["kite_order_id"]]:
        violations.append(f"broker order book {list(env.kite.order_book)} != the one order the ticket "
                          f"tracks ({row['kite_order_id']})")
    if len(gtts) != 1 or list(env.kite.gtt_book) != [row["kite_gtt_id"]]:
        violations.append(f"{len(gtts)} protective GTTs placed {list(env.kite.gtt_book)}; ticket tracks "
                          f"{row['kite_gtt_id']}")
    if sorted(code for code, _ in results) != [200, 409]:
        violations.append(f"responses were {sorted(code for code, _ in results)}, expected one 200 and one 409")
    elif next(body for code, body in results if code == 409)["error_code"] != "CONFLICT_INVALID_STATE":
        violations.append(f"loser got {results}, expected CONFLICT_INVALID_STATE")
    if row["status"] != "executed":
        violations.append(f"ticket ended {row['status']!r}, expected 'executed'")
    if _event_types(env, t["id"]).count("confirmed") != 1:
        violations.append(f"ticket events {_event_types(env, t['id'])} should hold exactly one 'confirmed'")
    assert not violations, violations


@pytest.mark.parametrize("status", ["risk_rejected", "rejected", "confirmed", "executed", "failed", "cancelled"])
def test_p0_1_confirm_only_from_pending_confirmation(env, market_open, status):
    set_mode(env.db, "live")
    tid = _insert_ticket(env.db, status=status, mode=None)
    r = env.client.post(f"/api/tickets/{tid}/confirm", json=displayed_mode(env.db))

    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "CONFLICT_INVALID_STATE"
    assert ticket_row(env.db, tid)["status"] == status
    assert _event_types(env, tid) == []
    assert env.kite.calls_to("place_order") == [] and env.kite.calls_to("place_gtt") == []


# The two races below run the competing request inside tickets._transition,
# i.e. after the loser has read and checked the ticket but before its
# conditional UPDATE — the exact window a read-check-then-write has. The
# competitor runs on its own connection, to completion, on the same thread,
# so the interleaving is fixed rather than timed.

def _run_first_inside_transition(monkeypatch, to_status, competitor):
    """The first time any request tries to move a ticket to to_status, run
    competitor() to completion before letting that UPDATE execute."""
    from backend.routes import tickets
    real_transition = tickets._transition
    outcome = {}

    def racing_transition(conn, ticket_id, account_id, from_statuses, target, **columns):
        if target == to_status and "result" not in outcome:
            outcome["result"] = competitor()
        return real_transition(conn, ticket_id, account_id, from_statuses, target, **columns)

    monkeypatch.setattr(tickets, "_transition", racing_transition)
    return outcome


def test_p0_1_reject_cannot_overwrite_ticket_confirmed_after_its_read(env, market_open, monkeypatch):
    from backend.routes import tickets
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    confirm = _run_first_inside_transition(
        monkeypatch, "rejected", lambda: _call(tickets.confirm_ticket, t["id"], req=_displayed(env), kite=env.kite))

    r = env.client.post(f"/api/tickets/{t['id']}/reject", json={"reason": "too late"})

    _precondition(confirm["result"][0] == 200, f"competing confirm should win: {confirm}")
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "CONFLICT_INVALID_STATE"
    row = ticket_row(env.db, t["id"])
    assert row["status"] == "executed" and row["kite_order_id"] is not None
    assert len(env.kite.calls_to("place_order")) == 1
    assert _event_types(env, t["id"]).count("rejected") == 0


def test_p0_1_confirm_cannot_execute_ticket_rejected_after_its_read(env, market_open, monkeypatch):
    from backend.models import TicketRejectRequest
    from backend.routes import tickets
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    reject = _run_first_inside_transition(
        monkeypatch, "confirmed",
        lambda: _call(tickets.reject_ticket, t["id"], TicketRejectRequest(reason="no"), kite=env.kite))

    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))

    _precondition(reject["result"][0] == 200, f"competing reject should win: {reject}")
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "CONFLICT_INVALID_STATE"
    assert ticket_row(env.db, t["id"])["status"] == "rejected"
    assert env.kite.calls_to("place_order") == [] and env.kite.calls_to("place_gtt") == []
    assert _event_types(env, t["id"]).count("confirmed") == 0


def test_p0_1_cancel_all_pending_skips_ticket_confirmed_concurrently(env, market_open, monkeypatch):
    from backend.routes import tickets
    set_mode(env.db, "live")
    tcs = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    infy = env.client.post("/api/tickets", json=_manual_ticket(
        symbol="INFY", entry_price=1500.0, stop_loss=1450.0, target_1=1650.0)).json()["data"]
    _precondition(tcs["status"] == infy["status"] == "pending_confirmation", "both tickets must be pending")
    confirm = _run_first_inside_transition(
        monkeypatch, "rejected", lambda: _call(tickets.confirm_ticket, tcs["id"], req=_displayed(env), kite=env.kite))

    r = env.client.post("/api/tickets/cancel-all-pending")

    _precondition(confirm["result"][0] == 200, f"competing confirm should win: {confirm}")
    assert r.status_code == 200
    assert r.json()["data"] == {"cancelled_ticket_ids": [infy["id"]], "count": 1, "symbols": ["INFY"]}
    assert ticket_row(env.db, tcs["id"])["status"] == "executed"
    assert _event_types(env, tcs["id"]).count("rejected") == 0
    assert ticket_row(env.db, infy["id"])["status"] == "rejected"
    assert _event_types(env, infy["id"]).count("rejected") == 1
    logged = _rows(env, "SELECT detail FROM system_events WHERE event_type = 'cancel_all_pending'")
    assert [e["detail"] for e in logged] == ["Cancelled 1 ticket(s): INFY"]


def test_p0_1_second_reject_is_rejected_without_a_second_event(env):
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    first = env.client.post(f"/api/tickets/{t['id']}/reject", json={"reason": "no"})
    second = env.client.post(f"/api/tickets/{t['id']}/reject", json={"reason": "still no"})

    assert first.status_code == 200 and second.status_code == 409
    assert _event_types(env, t["id"]).count("rejected") == 1


# ================================================================ P0-2
# Safety property: an ambiguous broker failure (the order may or may not be
# live) is reported as UNKNOWN and leaves the ticket in a state something
# will follow up, never as a 500 with a ticket nothing will look at again.

def test_p0_2_kite_network_exception_reports_unknown(env, market_open):
    # Already safe (audit S5): kiteconnect's own NetworkException is handled.
    set_mode(env.db, "live")
    env.kite.raise_on_next("place_order", NetworkException("Gateway timed out"))
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["success"] is False and "UNKNOWN" in data["execution_message"]
    assert not _is_stranded(ticket_row(env.db, t["id"]))


# PK-2 turned the four strict xfails below into regression tests. The two
# "strands_ticket" cases were renamed ..._is_unknown_not_stranded.

_RAW_NETWORK_ERRORS = pytest.mark.parametrize(
    "exc", [requests.ReadTimeout("Read timed out. (read timeout=7)"), requests.ConnectionError("Connection aborted.")],
    ids=["ReadTimeout", "ConnectionError"])

# A fixed, mid-session submission time (09:30 IST), so window arithmetic
# never depends on the wall clock or on crossing midnight.
_SUBMITTED_AT = "2026-01-05 04:00:00"


def _monitor(env):
    from backend import order_monitor
    return order_monitor.check_pending_orders(env.kite)


def _pin_clock(monkeypatch, utc_text):
    from datetime import datetime, timezone
    from backend import order_monitor
    when = datetime.strptime(utc_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    monkeypatch.setattr(order_monitor, "_utc_now", lambda: when)


def _set_submitted_at(env, ticket_id, value=_SUBMITTED_AT):
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE trade_tickets SET submitted_at = ? WHERE id = ?", (value, ticket_id))
        conn.commit()
    finally:
        conn.close()


def _urgent_updates(env):
    return [b for b in env.broadcasts if b.get("type") == "order_update" and b.get("severity") == "urgent"]


def _confirm_unknown_entry(env, client, inject, exc, **ticket):
    """Creates a live entry and confirms it while place_order fails as
    injected ('raise_on_next' = never reached the broker, 'accept_then_raise'
    = accepted, response lost)."""
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket(**ticket)).json()["data"]
    _precondition(t["status"] == "pending_confirmation", f"ticket not approvable: {t['status']}")
    getattr(env.kite, inject)("place_order", exc)
    return t["id"], client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))


def _unknown_ticket(env, submission_state="unknown", submitted_at=_SUBMITTED_AT, **overrides) -> int:
    """A ticket exactly as the database holds it after an unknown submission,
    written directly, as a freshly started process would find it."""
    from backend import execution
    fields = dict(status="confirmed", mode="live", submission_state=submission_state, submitted_at=submitted_at)
    fields.update(overrides)
    tid = _insert_ticket(env.db, **fields)
    row = ticket_row(env.db, tid)
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE trade_tickets SET order_tag = ? WHERE id = ?",
                     (execution.order_tag_for(tid, row["created_at"]), tid))
        conn.commit()
    finally:
        conn.close()
    return tid


def _assert_unknown_response(r):
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["success"] is False
    assert "UNKNOWN — DO NOT RESUBMIT" in data["execution_message"]
    assert data["status"] == "confirmed" and data["submission_state"] == "unknown"


@_RAW_NETWORK_ERRORS
def test_p0_2_entry_raw_network_error_is_unknown_not_stranded(env, unsafe_client, market_open, exc):
    tid, r = _confirm_unknown_entry(env, unsafe_client, "raise_on_next", exc)
    _run_background_cycles(env)
    row = ticket_row(env.db, tid)
    assert not _ambiguous_outcome_violations(r, row)
    assert row["status"] == "confirmed" and row["submission_state"] == "unknown"
    assert row["order_tag"] and row["submitted_at"]
    assert len(env.kite.calls_to("place_order")) == 1  # never retried
    assert "submission_unknown" in _event_types(env, tid)


@_RAW_NETWORK_ERRORS
def test_p0_2_entry_accepted_but_response_lost(env, unsafe_client, market_open, exc):
    # B (ReadTimeout) and C (ConnectionError): accepted, then the response is lost.
    tid, r = _confirm_unknown_entry(env, unsafe_client, "accept_then_raise", exc)
    _precondition(len(env.kite.order_book) == 1, "the broker should hold the accepted BUY order")
    _assert_unknown_response(r)
    assert env.client.post(f"/api/tickets/{tid}/confirm", json=displayed_mode(env.db)).status_code == 409  # not reopened

    _run_background_cycles(env)
    row = ticket_row(env.db, tid)
    assert not _ambiguous_outcome_violations(r, row)
    [broker_order] = env.kite.order_book
    assert row["kite_order_id"] == broker_order
    assert row["status"] == "executed" and row["submission_state"] == "adopted"
    assert len(env.kite.calls_to("place_order")) == 1
    assert env.kite.calls_to("place_gtt") == []  # no protection invented
    assert "protection_failed" in _event_types(env, tid)
    assert any("NO protective GTT" in u["message"] for u in _urgent_updates(env))


def test_p0_2_exit_accepted_but_response_lost(env, unsafe_client, market_open):
    # F: unknown exit, later found at the broker. P0-7 semantics (PK-8) are
    # asserted in test_exit_lifecycle.py (cases G and H).
    set_mode(env.db, "live")
    buy_id, gtt_id = _live_position(env)
    sell_id = _pending_exit(env, buy_id)
    env.kite.accept_then_raise("place_order", requests.ReadTimeout("Read timed out. (read timeout=7)"))
    r = unsafe_client.post(f"/api/tickets/{sell_id}/confirm")
    _precondition(len(_sells(env)) == 1, "the broker should hold the accepted SELL order")
    _assert_unknown_response(r)
    # While the outcome is unknown the position keeps its only protection.
    assert env.kite.gtt_book[gtt_id]["status"] == "active"
    assert ticket_row(env.db, buy_id)["closed_at"] is None

    _run_background_cycles(env)
    row = ticket_row(env.db, sell_id)
    assert not _ambiguous_outcome_violations(r, row)
    [sell] = [o for o in env.kite.order_book.values() if o["transaction_type"] == "SELL"]
    assert row["kite_order_id"] == sell["order_id"]
    assert row["status"] == "executed" and row["submission_state"] == "adopted"
    assert len(_sells(env)) == 1  # no duplicate exit


# ---------------------------------------------------------------- PK-2

def test_p0_2_orders_carry_deterministic_kite_tag(env, market_open):
    import re
    from backend import execution
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))
    buy_id, _ = _live_position(env)
    sell_id = _pending_exit(env, buy_id)
    env.client.post(f"/api/tickets/{sell_id}/confirm")

    placed = env.kite.calls_to("place_order")
    assert [o["transaction_type"] for o in placed] == ["BUY", "SELL"]
    for order, tid in zip(placed, [t["id"], sell_id]):
        row = ticket_row(env.db, tid)
        assert order["tag"] == row["order_tag"] == execution.order_tag_for(tid, row["created_at"])
        assert re.fullmatch(r"kt\d+x[0-9a-z]+", order["tag"]) and len(order["tag"]) <= 20
        assert row["submission_state"] == "acknowledged" and row["submitted_at"]


def _kite_exc(name):
    from kiteconnect import exceptions as kex
    if name in ("ReadTimeout", "ConnectionError"):
        return getattr(requests, name)("boom")
    if name == "KeyError":
        return KeyError("order_id")  # malformed response body
    return getattr(kex, name)("boom")


@pytest.mark.parametrize("exc_name, outcome", [
    ("OrderException", "rejected"), ("InputException", "rejected"), ("PermissionException", "rejected"),
    ("NetworkException", "unknown"), ("TokenException", "unknown"), ("GeneralException", "unknown"),
    ("DataException", "unknown"), ("ReadTimeout", "unknown"), ("ConnectionError", "unknown"),
    ("KeyError", "unknown"),
])
def test_p0_2_broker_exception_classification(env, market_open, exc_name, outcome):
    tid, r = _confirm_unknown_entry(env, env.client, "raise_on_next", _kite_exc(exc_name))
    assert r.status_code == 200
    row = ticket_row(env.db, tid)
    if outcome == "rejected":
        assert (row["status"], row["submission_state"]) == ("failed", "rejected")
        assert "rejected by Kite" in r.json()["data"]["execution_message"]
    else:
        assert (row["status"], row["submission_state"]) == ("confirmed", "unknown")
        _assert_unknown_response(r)
    assert len(env.kite.calls_to("place_order")) == 1


def test_p0_2_timeout_before_acceptance_reaches_not_found_after_window(env, market_open, monkeypatch):
    # A: the request never reached Kite. Searched, never resubmitted, then a
    # safe final state once the window has passed on the same day.
    tid, r = _confirm_unknown_entry(env, env.client, "raise_on_next", requests.ReadTimeout("timed out"))
    _assert_unknown_response(r)
    _set_submitted_at(env, tid)

    for minutes_later in ("04:01:00", "04:10:00", "04:14:59"):
        _pin_clock(monkeypatch, f"2026-01-05 {minutes_later}")
        _monitor(env)
        assert ticket_row(env.db, tid)["submission_state"] == "unknown"
    orders_calls = len(env.kite.calls_to("orders"))
    _precondition(orders_calls >= 3, "the monitor should have searched the order book each cycle")

    _pin_clock(monkeypatch, "2026-01-05 04:15:00")
    _monitor(env)
    row = ticket_row(env.db, tid)
    assert (row["status"], row["submission_state"]) == ("failed", "not_found")
    assert row["kite_order_id"] is None
    assert len(env.kite.calls_to("place_order")) == 1
    assert env.client.post(f"/api/tickets/{tid}/confirm", json=displayed_mode(env.db)).status_code == 409  # not back to approval
    events = _rows(env, "SELECT event_type, detail FROM ticket_events WHERE ticket_id = ? ORDER BY id", tid)
    assert events[-1]["event_type"] == "execution_failed" and "NOT placed" in events[-1]["detail"]


def test_p0_2_unknown_from_an_earlier_day_needs_review_not_not_found(env, market_open, monkeypatch):
    tid = _unknown_ticket(env)
    _pin_clock(monkeypatch, "2026-01-06 04:00:00")  # the next IST day: old order book is gone
    _monitor(env)
    row = ticket_row(env.db, tid)
    assert (row["status"], row["submission_state"]) == ("confirmed", "needs_review")
    assert _urgent_updates(env)
    assert env.kite.calls_to("place_order") == []


@pytest.mark.parametrize("state", ["unknown", "submitting"])
def test_p0_2_restart_adopts_tagged_order_from_database_alone(env, market_open, state):
    # D: nothing in memory — only the row and the broker's order book. A
    # stale 'submitting' row is what a crash inside place_order leaves.
    tid = _unknown_ticket(env, submission_state=state)
    tag = ticket_row(env.db, tid)["order_tag"]
    env.kite.add_broker_order("INFY", "BUY", 5, tag="kt999x1")  # unrelated, differently tagged
    order_id = env.kite.add_broker_order("TCS", "BUY", 5, tag=tag, status="OPEN")

    _monitor(env)
    row = ticket_row(env.db, tid)
    assert (row["status"], row["submission_state"], row["kite_order_id"]) == ("executed", "adopted", order_id)
    assert row["entry_fill_status"] == "pending"
    assert env.kite.calls_to("place_order") == [] and env.kite.calls_to("place_gtt") == []


def test_p0_2_in_flight_submission_is_left_alone(env, market_open):
    # A 'submitting' claim younger than SUBMISSION_STALE_SECONDS may still be
    # inside place_order: the monitor must not touch it.
    from backend import execution
    tid = _unknown_ticket(env, submission_state="submitting", submitted_at=execution.utc_now_sql())
    env.kite.add_broker_order("TCS", "BUY", 5, tag=ticket_row(env.db, tid)["order_tag"])
    _monitor(env)
    row = ticket_row(env.db, tid)
    assert (row["status"], row["submission_state"], row["kite_order_id"]) == ("confirmed", "submitting", None)


def test_p0_2_adopted_entry_fill_continues_lifecycle_and_flags_missing_protection(env, market_open):
    # E
    env.kite.auto_fill = False
    tid, _ = _confirm_unknown_entry(env, env.client, "accept_then_raise", requests.ReadTimeout("timed out"))
    _monitor(env)
    row = ticket_row(env.db, tid)
    assert (row["status"], row["submission_state"], row["entry_fill_status"]) == ("executed", "adopted", "pending")

    env.kite.set_order_state(row["kite_order_id"], "COMPLETE", filled_quantity=5, average_price=2998.5)
    _monitor(env)
    row = ticket_row(env.db, tid)
    assert row["entry_fill_status"] == "filled" and row["filled_price"] == 2998.5
    assert row["kite_gtt_id"] is None  # never marked protected
    assert any("FILLED with NO active protective GTT" in u["message"] for u in _urgent_updates(env))
    mismatches = _rows(env, "SELECT check_type FROM reconciliation_log WHERE ticket_id = ?", tid)
    assert {"check_type": "position_state"} in mismatches
    assert len(env.kite.calls_to("place_order")) == 1


def test_p0_2_unknown_ticket_stays_non_actionable_while_searching(env, market_open):
    # G: no match yet — nothing can reopen, edit, reject, delete or re-place it.
    tid, _ = _confirm_unknown_entry(env, env.client, "raise_on_next", requests.ConnectionError("reset"))
    for _ in range(3):
        _run_background_cycles(env)
        assert ticket_row(env.db, tid)["submission_state"] == "unknown"
    assert env.client.post(f"/api/tickets/{tid}/confirm", json=displayed_mode(env.db)).status_code == 409
    assert env.client.post(f"/api/tickets/{tid}/reject", json={"reason": "x"}).status_code == 409
    assert env.client.put(f"/api/tickets/{tid}", json=_manual_ticket()).status_code == 409
    assert env.client.delete(f"/api/tickets/{tid}").status_code == 409
    assert env.client.post("/api/tickets/cancel-all-pending").json()["data"]["count"] == 0
    row = ticket_row(env.db, tid)
    assert (row["status"], row["submission_state"], row["archived"]) == ("confirmed", "unknown", 0)
    assert len(env.kite.calls_to("place_order")) == 1


@pytest.mark.parametrize("case", ["two_tagged_orders", "tagged_order_mismatch"])
def test_p0_2_ambiguous_tagged_orders_need_review(env, market_open, case):
    # H: never guess between broker orders.
    tid = _unknown_ticket(env)
    tag = ticket_row(env.db, tid)["order_tag"]
    if case == "two_tagged_orders":
        ids = [env.kite.add_broker_order("TCS", "BUY", 5, tag=tag), env.kite.add_broker_order("TCS", "BUY", 5, tag=tag)]
    else:
        ids = [env.kite.add_broker_order("INFY", "BUY", 5, tag=tag)]

    for _ in range(2):
        _monitor(env)
    row = ticket_row(env.db, tid)
    assert (row["status"], row["submission_state"], row["kite_order_id"]) == ("confirmed", "needs_review", None)
    [logged] = _rows(env, "SELECT check_type, actual FROM reconciliation_log WHERE ticket_id = ?", tid)
    assert logged["check_type"] == "order_status" and all(i in logged["actual"] for i in ids)
    assert len(_urgent_updates(env)) == 1
    assert _event_types(env, tid).count("submission_unknown") == 1
    assert env.kite.calls_to("place_order") == [] and env.kite.calls_to("cancel_order") == []
    assert env.client.post(f"/api/tickets/{tid}/confirm", json=displayed_mode(env.db)).status_code == 409


def _run_first_inside_update(monkeypatch, competitor):
    """The first time anyone calls execution.update_ticket_if, run
    competitor() to completion (own connection, same thread) before that
    write executes — the PK-1 technique, applied to post-claim writes."""
    from backend import execution
    real = execution.update_ticket_if
    outcome = {}

    def racing(conn, ticket_id, **kwargs):
        if "result" not in outcome:
            outcome["result"] = None  # nested calls from the competitor pass through
            outcome["result"] = competitor()
        return real(conn, ticket_id, **kwargs)

    monkeypatch.setattr(execution, "update_ticket_if", racing)
    return outcome


def test_p0_2_race_monitor_adopts_before_request_records_unknown(env, market_open, monkeypatch):
    # I(1): the monitor resolves the claim first; the request's later
    # 'unknown' write must not move the ticket backwards.
    from backend import config
    monkeypatch.setattr(config, "SUBMISSION_STALE_SECONDS", 0)
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    env.kite.auto_fill = False
    env.kite.accept_then_raise("place_order", requests.ReadTimeout("timed out"))
    monitor = _run_first_inside_update(monkeypatch, lambda: _monitor(env))

    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))

    _precondition(monitor["result"]["updated"] >= 1, f"the monitor should have resolved first: {monitor}")
    assert r.status_code == 200
    row = ticket_row(env.db, t["id"])
    [broker_order] = env.kite.order_book
    assert (row["status"], row["submission_state"], row["kite_order_id"]) == ("executed", "adopted", broker_order)
    assert r.json()["data"]["submission_state"] == "adopted"
    assert len(env.kite.calls_to("place_order")) == 1


def test_p0_2_race_request_result_after_adoption_attaches_gtt_without_overwriting(env, market_open, monkeypatch):
    # I(2): the request got its order id and GTT, but the monitor had adopted
    # the same order already. Nothing is overwritten; the GTT is attached.
    from backend import config
    monkeypatch.setattr(config, "SUBMISSION_STALE_SECONDS", 0)
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    env.kite.auto_fill = False
    _run_first_inside_update(monkeypatch, lambda: _monitor(env))

    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))

    assert r.status_code == 200
    row = ticket_row(env.db, t["id"])
    [broker_order] = env.kite.order_book
    [gtt_id] = env.kite.gtt_book
    assert (row["status"], row["submission_state"]) == ("executed", "adopted")
    assert (row["kite_order_id"], row["kite_gtt_id"]) == (broker_order, gtt_id)
    assert len(env.kite.calls_to("place_order")) == 1 and len(env.kite.calls_to("place_gtt")) == 1
    assert _event_types(env, t["id"]).count("executed") == 1  # the adoption, not a second one
    details = [e["detail"] for e in _rows(env, "SELECT detail FROM ticket_events WHERE ticket_id = ?", t["id"])]
    assert any(f"Protective GTT {gtt_id}" in d and "attached" in d for d in details)


@pytest.mark.parametrize("request_outcome", ["unknown", "order_id_returned"])
def test_p0_2_race_request_result_cannot_clear_needs_review(env, market_open, monkeypatch, request_outcome):
    # I(4): the monitor flags the claim needs_review (status stays
    # 'confirmed'); the request's late write — unknown, or even a returned
    # order id — must not overwrite that. Only the submission_state
    # condition distinguishes these states.
    from backend import config, execution
    monkeypatch.setattr(config, "SUBMISSION_STALE_SECONDS", 0)
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    # An order carrying the same tag already exists (e.g. an earlier duplicate).
    existing = env.kite.add_broker_order(
        "TCS", "BUY", 5, tag=execution.order_tag_for(t["id"], ticket_row(env.db, t["id"])["created_at"]))
    env.kite.auto_fill = False
    if request_outcome == "unknown":
        env.kite.accept_then_raise("place_order", requests.ReadTimeout("timed out"))
    monitor = _run_first_inside_update(monkeypatch, lambda: _monitor(env))

    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))

    _precondition(monitor["result"]["updated"] >= 1, f"the monitor should have resolved first: {monitor}")
    assert r.status_code == 200
    row = ticket_row(env.db, t["id"])
    assert (row["status"], row["submission_state"], row["kite_order_id"]) == ("confirmed", "needs_review", None)
    assert len(env.kite.calls_to("place_order")) == 1
    if request_outcome == "order_id_returned":
        [placed] = [o for o in env.kite.order_book if o != existing]
        [logged] = [m for m in _rows(env, "SELECT actual FROM reconciliation_log WHERE ticket_id = ?", t["id"])
                    if placed in m["actual"] and "confirming request" in m["actual"]]
        assert logged


def test_p0_2_race_two_monitors_adopt_exactly_once(env, market_open, monkeypatch):
    # I(3): e.g. two app processes. Only one adoption may happen.
    tid = _unknown_ticket(env)
    env.kite.add_broker_order("TCS", "BUY", 5, tag=ticket_row(env.db, tid)["order_tag"])
    _run_first_inside_update(monkeypatch, lambda: _monitor(env))

    _monitor(env)
    row = ticket_row(env.db, tid)
    assert (row["status"], row["submission_state"]) == ("executed", "adopted")
    assert _event_types(env, tid).count("executed") == 1
    assert _event_types(env, tid).count("protection_failed") == 1


def test_p0_2_race_two_monitors_resolve_a_fill_exactly_once(env, market_open, monkeypatch):
    # The monitor's own resolution writes are conditional too: a second
    # writer that read the same pending order records nothing twice.
    from backend import order_monitor
    tid = _insert_ticket(env.db, executed_at="2026-01-05 04:00:00", entry_fill_status="pending",
                         kite_order_id=env.kite.add_broker_order("TCS", "BUY", 5, status="COMPLETE"))
    real = order_monitor._claim_pending
    raced = {}

    def racing(conn, ticket, assignments, params):
        if "done" not in raced:
            raced["done"] = None  # the competitor's own writes pass straight through
            raced["done"] = order_monitor.check_pending_orders(env.kite)
        return real(conn, ticket, assignments, params)

    monkeypatch.setattr(order_monitor, "_claim_pending", racing)
    outer = order_monitor.check_pending_orders(env.kite)

    _precondition(raced["done"]["updated"] == 1, f"the competing cycle should record the fill: {raced}")
    assert outer["updated"] == 0
    assert ticket_row(env.db, tid)["entry_fill_status"] == "filled"
    fills = [e for e in _rows(env, "SELECT detail FROM ticket_events WHERE ticket_id = ?", tid)
             if "entry filled" in e["detail"]]
    assert len(fills) == 1


def test_p0_2_emergency_exit_does_not_resubmit_an_unknown_exit(env, unsafe_client, market_open):
    set_mode(env.db, "live")
    buy_id, gtt_id = _live_position(env)
    sell_id = _pending_exit(env, buy_id)
    env.kite.accept_then_raise("place_order", requests.ReadTimeout("timed out"))
    _assert_unknown_response(unsafe_client.post(f"/api/tickets/{sell_id}/confirm"))

    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    assert r.status_code == 200
    [result] = r.json()["data"]["results"]
    assert result["status"] == "exit_outcome_unknown" and result["ticket_id"] == sell_id
    assert len(_sells(env)) == 1
    assert env.kite.gtt_book[gtt_id]["status"] == "active"


# ================================================================ P0-3
# Safety property: a live position is only ever recorded as closed/adjusted
# by a real broker action. The global dry-run switch governs new entries,
# never the exit or stop of a position that already exists.
#
# PK-3 turned the three strict xfails of this section into the regression
# tests A, B and D below (renamed for what they now prove) and added C, E,
# F, G and the mismatch / creation-stamping tests. `_pending_exit` still
# creates its exit with mode=None — the shape every exit had before PK-3,
# which the claim resolves from the position.

def _mode_leak_violations(env, buy_id, gtt_id) -> list[str]:
    buy = ticket_row(env.db, buy_id)
    if buy["closed_at"] is None or _sells(env):
        return []
    exits = _rows(env, "SELECT id, mode, status FROM trade_tickets WHERE closes_ticket_id = ?", buy_id)
    return [f"live position {buy_id} marked closed at {buy['closed_at']} but no SELL reached the broker; "
            f"exit ticket(s) {exits}; protective GTT {gtt_id} still {env.kite.gtt_book[gtt_id]['status']!r}; "
            f"broker still holds {_held(env, 'TCS')} shares; "
            f"journal rows: {len(_closed_trades_for(env, buy_id))}"]


def _dry_run_position(env, **overrides) -> int:
    """A simulated (dry-run) BUY 5 TCS: filled instantly, no broker order, no GTT."""
    fields = dict(source="strategy", strategy_name="momentum_breakout", mode="dry_run", kite_gtt_id=None,
                  executed_at="2026-09-01 04:00:00", entry_fill_status="filled", filled_price=3000.0,
                  current_stop_loss=2900.0)
    fields.update(overrides)
    return _insert_ticket(env.db, **fields)


def _broker_calls(env, since: int = 0) -> list[str]:
    """Every state-changing broker call after call number `since` (the
    scenario's own setup) — a simulation may make none."""
    mutating = {"place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt"}
    return [name for name, _ in env.kite.calls[since:] if name in mutating]


def _db_account(env):
    conn = env.db.get_connection()
    try:
        return conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id(env.db),)).fetchone()
    finally:
        conn.close()


def _strategy_exit_for(env, buy_id) -> int:
    """An exit ticket created by the real strategy exit-scan code path."""
    from backend import strategy_engine
    from backend.strategies import momentum_breakout
    conn = env.db.get_connection()
    try:
        position = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (buy_id,)).fetchone()
        return strategy_engine._create_exit_ticket(
            momentum_breakout, conn, _db_account(env), env.kite, position,
            {"close": 3100.0, "rationale": "time exit", "checklist": {"time_exit": {"pass": True}}})
    finally:
        conn.close()


def _toggle_mode(env, mode):
    """Flips the global switch the way the UI does."""
    r = env.client.post("/api/execution/mode", json={"mode": mode, "confirm_real_money": mode == "live"})
    _precondition(r.status_code == 200 and r.json()["data"]["mode"] == mode, f"mode toggle failed: {r.text}")


def _confirmed_detail(env, ticket_id) -> str:
    [event] = _rows(env, "SELECT detail FROM ticket_events WHERE ticket_id = ? AND event_type = 'confirmed'",
                    ticket_id)
    return event["detail"]


# A
def test_p0_3_exit_ticket_confirmed_in_dry_run_sells_live_position(env, market_open):
    buy_id, gtt_id = _live_position(env)
    sell_id = _pending_exit(env, buy_id)
    set_mode(env.db, "dry_run")

    r = env.client.post(f"/api/tickets/{sell_id}/confirm")

    assert r.status_code == 200, r.text
    [sell] = _sells(env)
    assert (sell["tradingsymbol"], sell["quantity"]) == ("TCS", 5)
    row = ticket_row(env.db, sell_id)
    assert (row["mode"], row["status"]) == ("live", "executed")
    assert row["kite_order_id"] in env.kite.order_book
    # PK-2 submission tracking applies to the inherited live exit unchanged.
    assert row["submission_state"] == "acknowledged" and row["order_tag"] == sell["tag"]
    assert "Simulated" not in r.json()["data"]["execution_message"]
    assert not _mode_leak_violations(env, buy_id, gtt_id)
    assert _confirmed_detail(env, sell_id) == (
        f"Confirmed in live mode — inherited from position #{buy_id} (assigned at claim)")


# B
def test_p0_3_emergency_exit_in_dry_run_sells_live_position(env, market_open):
    buy_id, gtt_id = _live_position(env)
    set_mode(env.db, "dry_run")

    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})

    _precondition(r.status_code == 200 and r.json()["data"]["count"] == 1, f"emergency exit did not run: {r.text}")
    [result] = r.json()["data"]["results"]
    [sell] = _sells(env)
    assert (sell["tradingsymbol"], sell["quantity"]) == ("TCS", 5)
    exit_row = ticket_row(env.db, result["ticket_id"])
    assert (exit_row["mode"], exit_row["closes_ticket_id"], exit_row["status"]) == ("live", buy_id, "executed")
    assert exit_row["kite_order_id"] in env.kite.order_book
    assert "Simulated" not in result["message"]
    assert not _mode_leak_violations(env, buy_id, gtt_id)
    assert _confirmed_detail(env, exit_row["id"]).endswith("(stamped at creation)")
    [logged] = _rows(env, "SELECT detail FROM system_events WHERE event_type = 'emergency_exit_all'")
    assert "each in its own execution mode (live: 1, dry_run: 0)" in logged["detail"]


def test_p0_3_emergency_exit_in_dry_run_cancels_unfilled_live_entry_at_broker(env, market_open):
    # The unfilled-entry branch was gated on the global mode too.
    order_id = env.kite.add_broker_order("TCS", "BUY", 5, status="OPEN")
    buy_id, _ = _live_position(env, entry_fill_status="pending", filled_price=None, kite_order_id=order_id,
                               order_variety="regular")
    set_mode(env.db, "dry_run")

    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})

    [result] = r.json()["data"]["results"]
    assert (result["status"], result["ticket_id"]) == ("entry_cancelled", buy_id)
    assert [c["order_id"] for c in env.kite.calls_to("cancel_order")] == [order_id]
    assert env.kite.order_book[order_id]["status"] == "CANCELLED"
    assert _sells(env) == []


def test_p0_3_emergency_exit_leaves_dry_run_positions_alone(env, market_open):
    # Emergency Exit covers live positions only, whatever the toggle says.
    dry_id = _dry_run_position(env)
    buy_id, _ = _live_position(env)
    set_mode(env.db, "live")

    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    _monitor(env)  # PK-8: the fill, not the order, closes the live position

    assert r.json()["data"]["count"] == 1
    assert len(_sells(env)) == 1
    assert ticket_row(env.db, buy_id)["closed_at"] is not None
    assert ticket_row(env.db, dry_id)["closed_at"] is None
    assert _rows(env, "SELECT id FROM trade_tickets WHERE closes_ticket_id = ?", dry_id) == []


# C
@pytest.mark.parametrize("exit_mode", ["dry_run", None], ids=["stamped_at_creation", "unset_legacy"])
def test_p0_3_dry_run_position_exit_under_global_live_sends_nothing(env, market_open, exit_mode):
    buy_id = _dry_run_position(env)
    sell_id = _pending_exit(env, buy_id, mode=exit_mode)
    set_mode(env.db, "live")

    r = env.client.post(f"/api/tickets/{sell_id}/confirm")

    assert r.status_code == 200, r.text
    assert _broker_calls(env) == []
    row = ticket_row(env.db, sell_id)
    assert (row["mode"], row["status"], row["kite_order_id"]) == ("dry_run", "executed", None)
    assert row["submission_state"] is None and row["order_tag"] is None  # no live submission metadata
    assert "Simulated" in r.json()["data"]["execution_message"]
    assert [t["mode"] for t in _closed_trades_for(env, buy_id)] == ["dry_run"]


@pytest.mark.parametrize("position_mode, exit_mode", [("live", "dry_run"), ("dry_run", "live")])
def test_p0_3_exit_whose_mode_differs_from_its_position_is_refused(env, market_open, position_mode, exit_mode):
    # The backstop: no code path creates such a ticket, but one must never
    # execute — not even when the global switch agrees with the wrong stamp.
    if position_mode == "live":
        buy_id, gtt_id = _live_position(env)
    else:
        buy_id, gtt_id = _dry_run_position(env), None
    sell_id = _pending_exit(env, buy_id, mode=exit_mode)
    set_mode(env.db, exit_mode)
    setup_calls = len(env.kite.calls)

    r = env.client.post(f"/api/tickets/{sell_id}/confirm")

    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "EXIT_MODE_MISMATCH"
    row = ticket_row(env.db, sell_id)
    assert (row["status"], row["mode"], row["submission_state"], row["order_tag"]) == (
        "pending_confirmation", exit_mode, None, None)
    assert _event_types(env, sell_id) == []
    assert _broker_calls(env, since=setup_calls) == []
    assert ticket_row(env.db, buy_id)["closed_at"] is None
    if gtt_id is not None:
        assert env.kite.gtt_book[gtt_id]["status"] == "active"


def test_p0_3_exit_whose_position_mode_is_unknown_is_refused(env, market_open):
    never_executed = _insert_ticket(env.db, status="pending_confirmation", mode=None)
    sell_id = _pending_exit(env, never_executed)
    set_mode(env.db, "live")

    r = env.client.post(f"/api/tickets/{sell_id}/confirm")

    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "EXIT_MODE_UNRESOLVED"
    assert ticket_row(env.db, sell_id)["status"] == "pending_confirmation"
    assert _broker_calls(env) == []


def test_p0_3_exit_tickets_inherit_position_mode_at_creation(env, market_open):
    buy_id, _ = _live_position(env)
    set_mode(env.db, "dry_run")

    strategy_exit = _strategy_exit_for(env, buy_id)
    alert_exit = env.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "TCS", "quantity": 5, "entry_order_type": "MARKET", "entry_price": 3000.0,
        "rationale": "cut loss", "alert_type": "breach_loss",
    }).json()["data"]

    for tid in (strategy_exit, alert_exit["id"]):
        row = ticket_row(env.db, tid)
        assert (row["status"], row["closes_ticket_id"], row["mode"]) == ("pending_confirmation", buy_id, "live")


def test_p0_3_unlinked_alert_action_sell_follows_global_mode(env, market_open):
    # Selling a holding no ticket tracks is a new action, not the exit of a
    # tracked position: it stays unstamped and takes the global mode.
    env.kite.set_holding("TCS", 10, average_price=3000.0)
    set_mode(env.db, "dry_run")
    created = env.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "TCS", "quantity": 4, "entry_order_type": "MARKET", "entry_price": 3000.0,
        "rationale": "reduce", "alert_type": "breach_concentration",
    }).json()["data"]
    _precondition(created["closes_ticket_id"] is None and created["mode"] is None, f"unexpected link: {created}")

    done = env.client.post(f"/api/tickets/{created['id']}/confirm", json=displayed_mode(env.db)).json()["data"]

    assert done["mode"] == "dry_run" and _broker_calls(env) == []


# D
def test_p0_3_breakeven_in_dry_run_modifies_live_position_gtt(env):
    legs = [{"transaction_type": "SELL", "quantity": 5, "order_type": "LIMIT", "product": "CNC", "price": 2885.5}]
    gtt_id = env.kite.place_gtt(env.kite.GTT_TYPE_SINGLE, "TCS", "NSE", [2900.0], 3000.0, legs)["trigger_id"]
    tid, pid = _breakeven_proposal(env, gtt_id)
    _precondition(ticket_row(env.db, tid)["mode"] == "live", "position must be live")
    env.kite.ltp_map["NSE:TCS"] = 3200.0
    set_mode(env.db, "dry_run")

    r = env.client.post(f"/api/strategies/stop-adjustments/{pid}/confirm")

    assert r.status_code == 200, r.text
    assert r.json()["data"]["mode"] == "live"
    [call] = env.kite.calls_to("modify_gtt")
    assert call["trigger_id"] == gtt_id
    db_stop = ticket_row(env.db, tid)["current_stop_loss"]
    broker_stop = env.kite.gtt_book[gtt_id]["condition"]["trigger_values"][0]
    assert db_stop == broker_stop == call["trigger_values"][0] == 3000.05


# E
def test_p0_3_breakeven_on_dry_run_position_under_global_live_touches_no_broker(env):
    tid = _dry_run_position(env, trailing_enabled=1, trailing_trigger="after_target_1", trailing_distance=50.0)
    conn = env.db.get_connection()
    try:
        pid = conn.execute("INSERT INTO stop_adjustment_proposals (ticket_id, current_stop, proposed_stop, reason) "
                           "VALUES (?, 2900.0, 3000.03, 'test')", (tid,)).lastrowid
        conn.commit()
    finally:
        conn.close()
    set_mode(env.db, "live")

    r = env.client.post(f"/api/strategies/stop-adjustments/{pid}/confirm")

    assert r.status_code == 200, r.text
    assert r.json()["data"]["mode"] == "dry_run"
    assert _broker_calls(env) == [] and env.kite.calls_to("ltp") == []
    assert ticket_row(env.db, tid)["current_stop_loss"] == 3000.05
    [event] = _rows(env, "SELECT detail FROM ticket_events WHERE ticket_id = ? "
                         "AND event_type = 'stop_adjusted_breakeven'", tid)
    assert "(dry_run mode)" in event["detail"]


# F
def test_p0_3_stamped_mode_survives_global_toggles(env, market_open):
    live_entry = _open_live_entry(env)  # confirmed and filled in live
    _toggle_mode(env, "dry_run")
    dry = env.client.post("/api/tickets", json=_manual_ticket(
        symbol="INFY", entry_price=1500.0, stop_loss=1450.0, target_1=1650.0)).json()["data"]
    dry = env.client.post(f"/api/tickets/{dry['id']}/confirm", json=displayed_mode(env.db)).json()["data"]
    _precondition(dry["mode"] == "dry_run" and dry["status"] == "executed", f"dry-run entry not simulated: {dry}")
    pending_exit = _strategy_exit_for(env, live_entry["id"])

    for mode in ("live", "dry_run", "live", "dry_run"):
        _toggle_mode(env, mode)
        _run_background_cycles(env)
        assert ticket_row(env.db, live_entry["id"])["mode"] == "live"
        assert ticket_row(env.db, dry["id"])["mode"] == "dry_run"
        assert ticket_row(env.db, pending_exit)["mode"] == "live"
    assert ticket_row(env.db, pending_exit)["status"] == "pending_confirmation"


# G
def test_p0_3_unknown_live_exit_recovered_after_toggle_to_dry_run_stays_live(env, unsafe_client, market_open):
    set_mode(env.db, "live")
    buy_id, _ = _live_position(env)
    sell_id = _pending_exit(env, buy_id)
    env.kite.accept_then_raise("place_order", requests.ReadTimeout("Read timed out. (read timeout=7)"))
    _assert_unknown_response(unsafe_client.post(f"/api/tickets/{sell_id}/confirm"))
    _precondition(ticket_row(env.db, sell_id)["mode"] == "live", "the unknown exit must be claimed live")

    set_mode(env.db, "dry_run")
    _run_background_cycles(env)

    row = ticket_row(env.db, sell_id)
    [sell] = [o for o in env.kite.order_book.values() if o["transaction_type"] == "SELL"]
    assert (row["status"], row["submission_state"], row["mode"]) == ("executed", "adopted", "live")
    assert row["kite_order_id"] == sell["order_id"]
    assert len(_sells(env)) == 1
    assert all(t["mode"] == "live" for t in _closed_trades_for(env, buy_id))
    assert _rows(env, "SELECT id FROM closed_trades WHERE mode = 'dry_run'") == []


# ================================================================ P0-4
# Safety property: confirmation re-validates. Only a ticket that passed its
# checks can execute, an exit can only sell a position that is still open,
# and an entry is only placed if the portfolio still has room for it.

# PK-1: only pending_confirmation is confirmable, so an unchecked draft is refused.
@pytest.mark.parametrize("origin", ["risk_check_crashed", "inserted_draft"])
def test_p0_4a_draft_ticket_cannot_be_confirmed(env, unsafe_client, market_open, origin):
    if origin == "risk_check_crashed":
        # Realistic origin: a raw requests error from holdings() is not a
        # KiteException, escapes the risk engine, and leaves the ticket 'draft'.
        env.kite.raise_on_next("holdings", requests.ReadTimeout("Read timed out. (read timeout=7)"))
        created = unsafe_client.post("/api/tickets", json=_manual_ticket())
        _precondition(created.status_code == 500, f"expected the risk check to crash, got {created.status_code}")
        tid = _rows(env, "SELECT MAX(id) AS id FROM trade_tickets")[0]["id"]
    else:
        tid = _insert_ticket(env.db, status="draft", mode=None)
    row = ticket_row(env.db, tid)
    _precondition(row["status"] == "draft" and row["risk_check_results"] is None,
                  f"expected an unchecked draft, got {row['status']}/{row['risk_check_results']}")

    set_mode(env.db, "live")
    r = unsafe_client.post(f"/api/tickets/{tid}/confirm", json=displayed_mode(env.db))
    violations = []
    if r.status_code != 409:
        violations.append(f"confirm of an unchecked draft returned {r.status_code} "
                          f"(ticket now {ticket_row(env.db, tid)['status']!r})")
    if env.kite.calls_to("place_order"):
        violations.append(f"{len(env.kite.calls_to('place_order'))} real order(s) placed without risk checks")
    if ticket_row(env.db, tid)["status"] != "draft" or "confirmed" in _event_types(env, tid):
        violations.append(f"draft was transitioned: status={ticket_row(env.db, tid)['status']!r}, "
                          f"events={_event_types(env, tid)}")
    assert not violations, violations


def test_p0_4b_stale_exit_cannot_sell_already_closed_position(env, unsafe_client, market_open):
    # PK-4: converted from xfail. The emergency exit that closed the
    # position superseded the pending strategy exit, so approving it later
    # is refused and sends nothing.
    set_mode(env.db, "live")
    buy_id, _ = _live_position(env)
    stale_exit_id = _pending_exit(env, buy_id)
    # The position is exited another way first (Emergency Exit ignores the
    # pending strategy exit), the broker fills it, and the loops catch up.
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    _precondition(r.status_code == 200 and len(_sells(env)) == 1, f"emergency exit did not sell: {r.text}")
    env.kite.set_holding("TCS", 0)
    _run_background_cycles(env)
    _precondition(ticket_row(env.db, buy_id)["closed_at"] is not None, "position should be closed by now")

    r = unsafe_client.post(f"/api/tickets/{stale_exit_id}/confirm")
    assert len(_sells(env)) == 1, (
        f"stale exit ticket {stale_exit_id} for already-closed position {buy_id} was confirmed "
        f"(HTTP {r.status_code}, status {ticket_row(env.db, stale_exit_id)['status']!r}) and sent "
        f"SELL #{len(_sells(env))} while the broker holds 0 shares"
    )
    assert r.status_code == 409
    row = ticket_row(env.db, stale_exit_id)
    assert row["status"] == "rejected" and row["submission_state"] is None
    [ee_exit] = _rows(env, "SELECT id FROM trade_tickets WHERE closes_ticket_id = ? AND status = 'executed'", buy_id)
    assert any(f"Superseded by exit ticket {ee_exit['id']}" in d for d in _details(env, stale_exit_id))
    assert "confirmed" not in _event_types(env, stale_exit_id)


def test_p0_4c_pending_entries_cannot_jointly_exceed_open_position_limit(env, unsafe_client, market_open):
    # PK-4: converted from xfail. Each ticket passed on its own; the second
    # approval re-checks the portfolio as it is now and is refused.
    from backend import risk_engine
    _set_risk(env, max_open_positions=1)
    first = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    second = env.client.post("/api/tickets", json=_manual_ticket(
        symbol="INFY", entry_price=1500.0, stop_loss=1450.0, target_1=1650.0)).json()["data"]
    _precondition(first["status"] == second["status"] == "pending_confirmation",
                  "each ticket must pass risk checks on its own")

    set_mode(env.db, "live")
    r1 = env.client.post(f"/api/tickets/{first['id']}/confirm", json=displayed_mode(env.db)).json()["data"]
    _precondition(r1["status"] == "executed", f"first entry not placed: {r1}")

    conn = env.db.get_connection()
    try:
        account = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id(env.db),)).fetchone()
        ticket = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (second["id"],)).fetchone()
        now = risk_engine.run_checks(conn, account, ticket, env.kite)
    finally:
        conn.close()
    failing = [c.rule for c in now.checks if not c.passed]
    _precondition(failing == ["max_open_positions"], f"re-run checks should now fail open positions: {failing}")

    r2 = unsafe_client.post(f"/api/tickets/{second['id']}/confirm", json=displayed_mode(env.db))
    orders = env.kite.calls_to("place_order")
    assert len(orders) == 1, (
        f"second entry confirmed (HTTP {r2.status_code}, status "
        f"{ticket_row(env.db, second['id'])['status']!r}) although it now fails {failing}; "
        f"{len(orders)} BUY orders placed against max_open_positions=1"
    )
    _assert_revalidation_refused(env, r2, second["id"], ["max_open_positions"])


# ---------------------------------------------------------------- PK-4
# Entries are re-checked against the portfolio as it is at approval; an exit
# only closes a position that is still open, unowned and of its quantity;
# background risk checks and stale reads never overwrite a newer state.

def _set_risk(env, **fields):
    from backend import risk_engine
    conn = env.db.get_connection()
    try:
        risk_engine.update_settings(conn, account_id(env.db), **fields)
    finally:
        conn.close()


def _details(env, ticket_id) -> list[str]:
    return [e["detail"] for e in _rows(env, "SELECT detail FROM ticket_events WHERE ticket_id = ? ORDER BY id",
                                       ticket_id)]


def _broker_orders(env, side) -> list[dict]:
    """Orders that actually exist at the broker (a call that raised before
    taking effect is recorded by FakeKite but created nothing)."""
    return [o for o in env.kite.order_book.values() if o["transaction_type"] == side]


def _infy_position(env, **overrides) -> tuple[int, int]:
    """A second filled live position: BUY 5 INFY, protected by an OCO GTT."""
    gtt_id = _protective_oco_gtt(env, symbol="INFY", stop=1450.0, target=1650.0)
    fields = dict(source="strategy", strategy_name="momentum_breakout", symbol="INFY", instrument_token=408065,
                  entry_price=1500.0, stop_loss=1450.0, target_1=1650.0, kite_gtt_id=gtt_id,
                  executed_at="2026-09-01 04:00:00", entry_fill_status="filled", filled_price=1500.0,
                  current_stop_loss=1450.0)
    fields.update(overrides)
    buy_id = _insert_ticket(env.db, **fields)
    env.kite.set_holding("INFY", 5, average_price=1500.0)
    return buy_id, gtt_id


def _assert_revalidation_refused(env, r, ticket_id, failed_rules):
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error_code"] == "RISK_REVALIDATION_FAILED"
    assert detail["failed_checks"] == failed_rules
    row = ticket_row(env.db, ticket_id)
    # Terminal and non-executing; nothing about a submission remains, and the
    # mode stamped by the claim is never rewritten (PK-3).
    assert row["status"] == "risk_rejected" and row["kite_order_id"] is None
    assert (row["submission_state"], row["order_tag"], row["submitted_at"]) == (None, None, None)
    assert row["mode"] == "live"
    assert [c["rule"] for c in json.loads(row["risk_check_results"]) if not c["passed"]] == failed_rules
    assert _event_types(env, ticket_id)[-2:] == ["confirmed", "risk_rejected"]
    assert "Passed creation-time risk checks but failed confirmation-time revalidation" in _details(env, ticket_id)[-1]
    assert env.client.post(f"/api/tickets/{ticket_id}/confirm", json=displayed_mode(env.db)).status_code == 409


def test_p0_4_revalidation_success_is_recorded_before_the_order(env, market_open):
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))

    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"
    assert _event_types(env, t["id"])[-3:] == ["confirmed", "modified", "executed"]
    assert "Risk re-validated at confirmation" in _details(env, t["id"])[-2]
    # The broker's portfolio was re-read at confirmation, before the order.
    names = [name for name, _ in env.kite.calls]
    assert names.index("place_order") > max(i for i, n in enumerate(names) if n == "holdings")


def test_p0_4_concurrent_approval_counts_the_entry_still_in_flight(env, market_open):
    # A is claimed and inside place_order (not yet 'executed') when B is
    # approved: B must count A, or both would be placed.
    from backend.routes import tickets
    _set_risk(env, max_open_positions=1)
    a = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    b = env.client.post("/api/tickets", json=_manual_ticket(
        symbol="INFY", entry_price=1500.0, stop_loss=1450.0, target_1=1650.0)).json()["data"]
    _precondition(a["status"] == b["status"] == "pending_confirmation", "both tickets must pass on their own")
    set_mode(env.db, "live")

    entered, release = threading.Event(), threading.Event()

    def hold_inside_place_order(_kwargs):
        entered.set()
        release.wait(timeout=10)

    env.kite.on_call("place_order", hold_inside_place_order)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(_call, tickets.confirm_ticket, a["id"], req=_displayed(env), kite=env.kite)
            assert entered.wait(timeout=10), "A never reached place_order"
            r = env.client.post(f"/api/tickets/{b['id']}/confirm", json=displayed_mode(env.db))
            release.set()
            first_result = first.result(timeout=10)
    finally:
        release.set()
        env.kite.on_call("place_order", None)

    assert first_result[0] == 200 and first_result[1]["status"] == "executed"
    _assert_revalidation_refused(env, r, b["id"], ["max_open_positions"])
    assert len(env.kite.calls_to("place_order")) == 1


def test_p0_4_total_portfolio_risk_revalidated_at_confirmation(env, market_open):
    # Portfolio 1,013,000: A risks 500 (0.049%), B 250 (0.025%); together 0.074%.
    _set_risk(env, max_total_portfolio_risk_pct=0.07)
    a = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    b = env.client.post("/api/tickets", json=_manual_ticket(
        symbol="INFY", entry_price=1500.0, stop_loss=1450.0, target_1=1650.0)).json()["data"]
    _precondition(a["status"] == b["status"] == "pending_confirmation", "both tickets must pass on their own")
    set_mode(env.db, "live")

    assert env.client.post(f"/api/tickets/{a['id']}/confirm", json=displayed_mode(env.db)).json()["data"]["status"] == "executed"
    r = env.client.post(f"/api/tickets/{b['id']}/confirm", json=displayed_mode(env.db))

    _assert_revalidation_refused(env, r, b["id"], ["max_total_portfolio_risk_pct"])
    assert len(env.kite.calls_to("place_order")) == 1


@pytest.mark.parametrize("limit", ["single_stock", "sector"])
def test_p0_4_exposure_revalidated_at_confirmation(env, market_open, limit):
    from backend import sector_mapping
    if limit == "single_stock":
        # Two TCS entries of 15,000 each: 1.48% alone, 2.96% together.
        _set_risk(env, max_single_stock_exposure_pct=2.0)
        second_ticket, rule = _manual_ticket(), "max_single_stock_exposure_pct"
    else:
        # TCS 15,000 + INFY 7,500, both IT: 1.48% + 0.74% = 2.22%.
        sector_mapping.upsert_sector("TCS", "IT")
        sector_mapping.upsert_sector("INFY", "IT")
        _set_risk(env, max_sector_exposure_pct=2.0)
        second_ticket = _manual_ticket(symbol="INFY", entry_price=1500.0, stop_loss=1450.0, target_1=1650.0)
        rule = "max_sector_exposure_pct"
    a = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    b = env.client.post("/api/tickets", json=second_ticket).json()["data"]
    _precondition(a["status"] == b["status"] == "pending_confirmation", "both tickets must pass on their own")
    set_mode(env.db, "live")

    assert env.client.post(f"/api/tickets/{a['id']}/confirm", json=displayed_mode(env.db)).json()["data"]["status"] == "executed"
    r = env.client.post(f"/api/tickets/{b['id']}/confirm", json=displayed_mode(env.db))

    _assert_revalidation_refused(env, r, b["id"], [rule])
    assert len(env.kite.calls_to("place_order")) == 1


@pytest.mark.parametrize("exc", [requests.ReadTimeout("Read timed out. (read timeout=7)"),
                                 NetworkException("Gateway timed out")], ids=["ReadTimeout", "NetworkException"])
def test_p0_4_risk_unavailable_at_confirmation_fails_closed(env, unsafe_client, market_open, exc):
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    env.kite.raise_on_next("holdings", exc)

    r = unsafe_client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))

    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert detail["error_code"] == "RISK_REVALIDATION_UNAVAILABLE"
    assert "Risk could not be revalidated" in detail["message"] and "no order was sent" in detail["message"]
    row = ticket_row(env.db, t["id"])
    # Not a risk rejection (nothing was evaluated), not reopened, and nothing
    # left for the order monitor to search for.
    assert row["status"] == "rejected"
    assert (row["submission_state"], row["order_tag"], row["submitted_at"]) == (None, None, None)
    assert _event_types(env, t["id"])[-2:] == ["confirmed", "rejected"]
    assert "risk_rejected" not in _event_types(env, t["id"])
    assert env.kite.calls_to("place_order") == [] and env.kite.calls_to("place_gtt") == []
    _run_background_cycles(env)
    assert ticket_row(env.db, t["id"])["status"] == "rejected"
    assert env.kite.calls_to("place_order") == []
    # Recoverable by hand once the broker answers again: Revert re-runs the checks.
    reverted = env.client.post(f"/api/tickets/{t['id']}/revert-rejection")
    assert reverted.status_code == 200 and reverted.json()["data"]["status"] == "pending_confirmation"


@pytest.mark.parametrize("how", ["closed_at_stamped", "sibling_exit_executed"])
def test_p0_4_exit_of_closed_position_is_refused_even_with_matching_mode(env, market_open, how):
    # The exit's mode matches its live position, but the position is already
    # closed. A mode match alone must not let it execute.
    set_mode(env.db, "live")
    if how == "closed_at_stamped":
        buy_id, gtt_id = _live_position(env, closed_at="2026-09-02 04:00:00")
    else:
        buy_id, gtt_id = _live_position(env)
        _pending_exit(env, buy_id, status="executed", mode="live", kite_order_id="250000000999",
                      executed_at="2026-09-02 04:00:00", entry_fill_status="filled")
    sell_id = _pending_exit(env, buy_id, mode="live")
    since = len(env.kite.calls)

    r = env.client.post(f"/api/tickets/{sell_id}/confirm")

    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "EXIT_POSITION_CLOSED"
    assert _sells(env) == [] and _broker_calls(env, since) == []
    assert env.kite.gtt_book[gtt_id]["status"] == "active"
    row = ticket_row(env.db, sell_id)
    assert row["status"] == "rejected" and row["submission_state"] is None
    assert _event_types(env, sell_id) == ["rejected"]
    assert "Stale exit refused at confirmation (EXIT_POSITION_CLOSED)" in _details(env, sell_id)[0]
    again = env.client.post(f"/api/tickets/{sell_id}/confirm")
    assert again.status_code == 409 and again.json()["detail"]["error_code"] == "CONFLICT_INVALID_STATE"


def test_p0_4_duplicate_exit_is_superseded_and_cannot_sell_again(env, market_open):
    # Two independently created exits (a strategy exit and an emergency-style one).
    set_mode(env.db, "live")
    buy_id, _ = _live_position(env)
    first = _pending_exit(env, buy_id)
    second = _pending_exit(env, buy_id, rule_checklist=json.dumps({"emergency_exit_all": True}))

    r1 = env.client.post(f"/api/tickets/{first}/confirm")
    assert r1.status_code == 200 and r1.json()["data"]["status"] == "executed"
    # PK-8: while the first exit is EXITING, the second is refused but kept —
    # the first may still be rejected by the exchange.
    r2 = env.client.post(f"/api/tickets/{second}/confirm")
    assert r2.status_code == 409 and r2.json()["detail"]["error_code"] == "EXIT_ALREADY_IN_PROGRESS"
    assert ticket_row(env.db, second)["status"] == "pending_confirmation"

    _monitor(env)  # the first exit fills: the position closes, the sibling is superseded
    assert ticket_row(env.db, second)["status"] == "rejected"
    assert _details(env, second) == [f"Superseded by exit ticket {first} — position {buy_id} is closed; "
                                     f"no order was sent."]

    r3 = env.client.post(f"/api/tickets/{second}/confirm")
    assert r3.status_code == 409
    assert len(_sells(env)) == 1


@pytest.mark.parametrize("quantity", [6, 3], ids=["exceeds", "partial"])
def test_p0_4_exit_quantity_must_match_the_position(env, market_open, quantity):
    set_mode(env.db, "live")
    buy_id, gtt_id = _live_position(env)
    sell_id = _pending_exit(env, buy_id, quantity=quantity, mode="live")

    r = env.client.post(f"/api/tickets/{sell_id}/confirm")

    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "EXIT_QUANTITY_MISMATCH"
    assert _sells(env) == [] and env.kite.gtt_book[gtt_id]["status"] == "active"
    assert ticket_row(env.db, sell_id)["status"] == "rejected"
    assert ticket_row(env.db, buy_id)["closed_at"] is None
    # The same rule applies at (re)creation: Revert re-runs the checks.
    reverted = env.client.post(f"/api/tickets/{sell_id}/revert-rejection").json()["data"]
    assert reverted["status"] == "risk_rejected"
    assert "does not match the 5 shares" in reverted["risk_check_results"][0]["message"]


@pytest.mark.parametrize("follow_up", ["still_unknown", "owner_adopted", "owner_not_found"])
def test_p0_4_exit_is_refused_while_a_sibling_exit_outcome_is_unknown(env, unsafe_client, market_open,
                                                                      monkeypatch, follow_up):
    set_mode(env.db, "live")
    buy_id, gtt_id = _live_position(env)
    first = _pending_exit(env, buy_id)
    second = _pending_exit(env, buy_id)
    if follow_up == "owner_not_found":
        env.kite.raise_on_next("place_order", requests.ReadTimeout("timed out"))  # never reached the broker
    else:
        env.kite.accept_then_raise("place_order", requests.ReadTimeout("timed out"))
    _assert_unknown_response(unsafe_client.post(f"/api/tickets/{first}/confirm"))

    r = env.client.post(f"/api/tickets/{second}/confirm")

    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "EXIT_ALREADY_IN_PROGRESS"
    assert "DO NOT RESUBMIT" in r.json()["detail"]["message"]
    # Transient: nothing is written — the exit may become valid again.
    assert ticket_row(env.db, second)["status"] == "pending_confirmation" and _event_types(env, second) == []
    assert len(_broker_orders(env, "SELL")) == (0 if follow_up == "owner_not_found" else 1)
    assert env.kite.gtt_book[gtt_id]["status"] == "active"

    if follow_up == "owner_adopted":
        _monitor(env)
        _precondition(ticket_row(env.db, first)["submission_state"] == "adopted", "the monitor should adopt")
        assert ticket_row(env.db, second)["status"] == "rejected"
        assert f"Superseded by exit ticket {first}" in _details(env, second)[0]
        assert env.client.post(f"/api/tickets/{second}/confirm").status_code == 409
        assert len(_broker_orders(env, "SELL")) == 1
    elif follow_up == "owner_not_found":
        _set_submitted_at(env, first)
        _pin_clock(monkeypatch, "2026-01-05 04:15:00")
        _monitor(env)
        _precondition(ticket_row(env.db, first)["submission_state"] == "not_found", "no order should be found")
        # Nothing was sold, so the other exit may now close the position.
        r = env.client.post(f"/api/tickets/{second}/confirm")
        assert r.status_code == 200 and r.json()["data"]["status"] == "executed"
        assert len(_broker_orders(env, "SELL")) == 1


def test_p0_4_database_refuses_a_second_owning_exit(env, unsafe_client, market_open, monkeypatch):
    # Storage backstop: even with the application check bypassed, the claim
    # of a second exit of the same position fails at the unique index.
    import sqlite3
    from backend.routes import tickets
    set_mode(env.db, "live")
    buy_id, _ = _live_position(env)
    first = _pending_exit(env, buy_id)
    second = _pending_exit(env, buy_id)
    env.kite.accept_then_raise("place_order", requests.ReadTimeout("timed out"))
    _assert_unknown_response(unsafe_client.post(f"/api/tickets/{first}/confirm"))
    monkeypatch.setattr(tickets, "_refuse_stale_exit", lambda *a, **k: None)

    r = env.client.post(f"/api/tickets/{second}/confirm")

    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "EXIT_ALREADY_IN_PROGRESS"
    assert ticket_row(env.db, second)["status"] == "pending_confirmation"
    assert "confirmed" not in _event_types(env, second)
    assert len(_sells(env)) == 1
    conn = env.db.get_connection()
    try:
        # PK-13: an exit owns its position while its order is in flight —
        # EXITING is 'executed' + 'pending' (an ended exit no longer does).
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE trade_tickets SET status = 'executed', entry_fill_status = 'pending' WHERE id = ?",
                         (second,))
    finally:
        conn.close()


def test_p0_4_exit_owner_index_migration_is_idempotent_and_tolerates_legacy_duplicates(env, caplog):
    def index_exists():
        return bool(_rows(env, "SELECT name FROM sqlite_master WHERE type = 'index' "
                               "AND name = 'idx_trade_tickets_one_exit_owner'"))

    assert index_exists()
    env.db.init_db()
    assert index_exists()

    # A database that already holds two owning exits of one position (it
    # can't be created while the index exists) must still start up. PK-13:
    # owning = in flight, i.e. two EXITING exits ('executed' + 'pending').
    conn = env.db.get_connection()
    try:
        conn.execute("DROP INDEX idx_trade_tickets_one_exit_owner")
        conn.commit()
    finally:
        conn.close()
    buy_id, _ = _live_position(env, closed_at="2026-09-02 04:00:00")
    dup = [_pending_exit(env, buy_id, status="executed", mode="live", executed_at="2026-09-02 04:00:00",
                         entry_fill_status="pending")
           for _ in range(2)]
    env.db.init_db()
    assert not index_exists()
    assert "Not creating idx_trade_tickets_one_exit_owner" in caplog.text

    conn = env.db.get_connection()
    try:
        conn.execute("DELETE FROM closed_trades WHERE sell_ticket_id = ?", (dup[1],))
        conn.execute("UPDATE trade_tickets SET status = 'failed' WHERE id = ?", (dup[1],))
        conn.commit()
    finally:
        conn.close()
    env.db.init_db()
    assert index_exists()


@pytest.mark.parametrize("risk_outcome", ["would_pass", "would_fail"])
def test_p0_4_risk_check_cannot_resurrect_a_ticket_cancelled_mid_check(env, market_open, risk_outcome):
    # Cancel All Pending runs while the ticket's risk check is inside the
    # broker portfolio fetch; the late result must be discarded.
    from backend.routes import tickets
    if risk_outcome == "would_fail":
        _set_risk(env, min_risk_reward=10)
    raced = {}

    def cancel_all_mid_check(_kwargs):
        if "result" not in raced:
            raced["result"] = None
            raced["result"] = _call(tickets.cancel_all_pending, kite=env.kite)

    env.kite.on_call("holdings", cancel_all_mid_check)
    try:
        r = env.client.post("/api/tickets", json=_manual_ticket())
    finally:
        env.kite.on_call("holdings", None)

    _precondition(raced["result"][0] == 200 and raced["result"][1]["count"] == 1,
                  f"Cancel All should have cancelled the draft mid-check: {raced}")
    assert r.status_code == 201
    tid = r.json()["data"]["id"]
    row = ticket_row(env.db, tid)
    assert row["status"] == "rejected" and row["risk_check_results"] is None
    assert _event_types(env, tid) == ["created", "rejected", "modified"]
    assert "discarded" in _details(env, tid)[-1]
    set_mode(env.db, "live")
    assert env.client.post(f"/api/tickets/{tid}/confirm", json=displayed_mode(env.db)).status_code == 409
    assert env.kite.calls_to("place_order") == []


@pytest.mark.parametrize("risk_outcome", ["would_pass", "would_fail"])
def test_p0_4_reverted_ticket_rejected_mid_check_stays_rejected(env, market_open, risk_outcome):
    from backend.models import TicketRejectRequest
    from backend.routes import tickets
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    env.client.post(f"/api/tickets/{t['id']}/reject", json={"reason": "not now"})
    if risk_outcome == "would_fail":
        _set_risk(env, min_risk_reward=10)
    raced = {}

    def reject_mid_check(_kwargs):
        if "result" not in raced:
            raced["result"] = None
            raced["result"] = _call(tickets.reject_ticket, t["id"], TicketRejectRequest(reason="changed my mind"),
                                    kite=env.kite)

    env.kite.on_call("holdings", reject_mid_check)
    try:
        r = env.client.post(f"/api/tickets/{t['id']}/revert-rejection")
    finally:
        env.kite.on_call("holdings", None)

    _precondition(raced["result"][0] == 200, f"the reject should have won mid-check: {raced}")
    assert r.status_code == 200 and r.json()["data"]["status"] == "rejected"
    events = _event_types(env, t["id"])
    assert events[-3:] == ["modified", "rejected", "modified"] and "risk_rejected" not in events
    assert "discarded" in _details(env, t["id"])[-1]


@pytest.mark.parametrize("kind", ["linked_exit", "alert_action_reduction"])
def test_p0_4_exit_tickets_cannot_be_edited(env, kind):
    if kind == "linked_exit":
        buy_id, _ = _live_position(env)
        tid = _pending_exit(env, buy_id, status="draft", mode="live")
    else:
        tid = _insert_ticket(env.db, source="alert_action", transaction_type="SELL", status="draft", mode=None,
                             entry_order_type="MARKET")
    before = ticket_row(env.db, tid)

    r = env.client.put(f"/api/tickets/{tid}", json=_manual_ticket(quantity=50))

    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "EXIT_TICKET_NOT_EDITABLE"
    assert ticket_row(env.db, tid) == before
    assert _event_types(env, tid) == []


def test_p0_4_stale_edit_cannot_turn_a_cancelled_ticket_back_into_a_draft(env, monkeypatch):
    from backend import instruments
    from backend.routes import tickets
    tid = _insert_ticket(env.db, status="draft", mode=None)
    real = instruments.get_instrument_by_symbol
    raced = {}

    def cancel_all_first(*args, **kwargs):
        if "result" not in raced:
            raced["result"] = None
            raced["result"] = _call(tickets.cancel_all_pending, kite=env.kite)
        return real(*args, **kwargs)

    monkeypatch.setattr(instruments, "get_instrument_by_symbol", cancel_all_first)
    r = env.client.put(f"/api/tickets/{tid}", json=_manual_ticket(quantity=7))

    _precondition(raced["result"][0] == 200 and raced["result"][1]["count"] == 1, f"cancel-all should win: {raced}")
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "CONFLICT_INVALID_STATE"
    row = ticket_row(env.db, tid)
    assert (row["status"], row["quantity"]) == ("rejected", 5)
    assert _event_types(env, tid) == ["rejected"]


def _stale_first_read(monkeypatch, stale_status):
    """The handler's first read of the ticket returns it as it was a moment
    ago (stale_status); the database already holds a newer state."""
    from backend.routes import tickets
    real = tickets._get_ticket_or_404
    state = {}

    def stale(conn, ticket_id, account_id):
        row = real(conn, ticket_id, account_id)
        if "done" not in state:
            state["done"] = True
            return {**dict(row), "status": stale_status}
        return row

    monkeypatch.setattr(tickets, "_get_ticket_or_404", stale)


@pytest.mark.parametrize("action, stale_status", [
    ("revert", "rejected"), ("delete_draft", "draft"), ("archive", "rejected")])
def test_p0_4_stale_read_cannot_revert_delete_or_archive(env, monkeypatch, action, stale_status):
    tid = _insert_ticket(env.db, status="pending_confirmation", mode=None)
    conn = env.db.get_connection()
    try:
        conn.execute("INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'created', 'x')", (tid,))
        conn.commit()
    finally:
        conn.close()
    _stale_first_read(monkeypatch, stale_status)

    if action == "revert":
        r = env.client.post(f"/api/tickets/{tid}/revert-rejection")
    else:
        r = env.client.delete(f"/api/tickets/{tid}")

    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "CONFLICT_INVALID_STATE"
    row = ticket_row(env.db, tid)
    assert (row["status"], row["archived"]) == ("pending_confirmation", 0)
    assert _event_types(env, tid) == ["created"]


def test_p0_4_signal_expiry_cannot_overwrite_a_ticket_confirmed_meanwhile(env):
    from backend import strategy_engine
    tid = _insert_ticket(env.db, status="pending_confirmation", mode=None, source="strategy",
                         strategy_name="momentum_breakout", signal_date="2026-09-01")

    class ConfirmBeforeFirstCancel:
        """Delegates to a real connection; right before expiry's cancel
        UPDATE, the ticket is approved (claimed, outcome unknown) elsewhere."""

        def __init__(self, conn):
            self._conn, self._raced = conn, False

        def execute(self, sql, *params):
            if "SET status = 'cancelled'" in sql and not self._raced:
                self._raced = True
                other = env.db.get_connection()
                try:
                    other.execute("UPDATE trade_tickets SET status = 'confirmed', mode = 'live', "
                                  "submission_state = 'unknown' WHERE id = ?", (tid,))
                    other.commit()
                finally:
                    other.close()
            return self._conn.execute(sql, *params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    conn = env.db.get_connection()
    try:
        expired = strategy_engine.expire_stale_signals(ConfirmBeforeFirstCancel(conn), account_id(env.db),
                                                       "2026-09-02")
    finally:
        conn.close()

    assert expired == []
    row = ticket_row(env.db, tid)
    assert (row["status"], row["submission_state"]) == ("confirmed", "unknown")
    assert _event_types(env, tid) == []


def test_p0_4_gtt_firing_while_an_exit_is_unresolved_is_flagged_not_recorded(env, unsafe_client, market_open):
    from backend import trailing_engine
    set_mode(env.db, "live")
    trailing = dict(trailing_enabled=1, trailing_trigger="after_target_1", trailing_distance=50.0)
    tcs_id, tcs_gtt = _live_position(env, **trailing)
    infy_id, infy_gtt = _infy_position(env, **trailing)
    owner = _pending_exit(env, tcs_id)
    infy_exit = _pending_exit(env, infy_id, symbol="INFY", instrument_token=408065, entry_price=1600.0,
                              stop_loss=1450.0, target_1=1650.0)
    env.kite.raise_on_next("place_order", requests.ReadTimeout("timed out"))
    _assert_unknown_response(unsafe_client.post(f"/api/tickets/{owner}/confirm"))
    env.kite.set_gtt_status(tcs_gtt, "triggered")
    # INFY's stop fires and fills (PK-7: only a fill closes a position).
    env.kite.trigger_gtt(infy_gtt, status="COMPLETE")

    asyncio.run(trailing_engine._tick_once())
    asyncio.run(trailing_engine._tick_once())

    # TCS: an exit already owns it — no second exit recorded, flagged once.
    assert _rows(env, "SELECT id FROM trade_tickets WHERE closes_ticket_id = ? AND id != ?", tcs_id, owner) == []
    assert ticket_row(env.db, tcs_id)["closed_at"] is None
    [flag] = _rows(env, "SELECT actual FROM reconciliation_log WHERE ticket_id = ?", tcs_id)
    assert f"exit ticket {owner} owns the position" in flag["actual"]
    errors = [b for b in env.broadcasts if b.get("type") == "trailing_error" and b.get("ticket_id") == tcs_id]
    assert len(errors) == 1 and "POSSIBLE DOUBLE SELL" in errors[0]["message"]
    # INFY, in the same tick: closed normally; its pending exit superseded.
    assert ticket_row(env.db, infy_id)["closed_at"] is not None
    [synthetic] = _rows(env, "SELECT id FROM trade_tickets WHERE closes_ticket_id = ? AND status = 'executed'",
                        infy_id)
    assert ticket_row(env.db, infy_exit)["status"] == "rejected"
    assert f"Superseded by exit ticket {synthetic['id']}" in _details(env, infy_exit)[0]


def test_p0_4_emergency_exit_continues_past_a_refused_position(env, market_open, monkeypatch):
    from fastapi import HTTPException
    from backend.routes import tickets
    tcs_id, _ = _live_position(env)
    _infy_position(env)
    real = tickets._refuse_stale_exit

    def refuse_tcs(conn, ticket, account_id):
        if ticket["closes_ticket_id"] == tcs_id:
            raise HTTPException(409, {"error_code": "EXIT_ALREADY_IN_PROGRESS", "message": "taken meanwhile"})
        return real(conn, ticket, account_id)

    monkeypatch.setattr(tickets, "_refuse_stale_exit", refuse_tcs)
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})

    assert r.status_code == 200
    results = {res["symbol"]: res for res in r.json()["data"]["results"]}
    assert (results["TCS"]["status"], results["TCS"]["error_code"]) == ("refused", "EXIT_ALREADY_IN_PROGRESS")
    assert results["INFY"]["status"] == "exit_submitted"  # PK-8: placed, not yet filled
    assert [s["tradingsymbol"] for s in _sells(env)] == ["INFY"]


# ================================================================ P0-5
# Safety property: when a protective GTT fires and fills, the app records
# the position as closed and journals it, whether or not it trails.
# PK-6 turned this strict xfail into a regression test: the GTT watcher in
# trailing_engine now covers every live protected position. Wider coverage
# lives in test_gtt_watcher.py.

@pytest.mark.parametrize("leg", [0, 1], ids=["stop", "target"])
def test_p0_5_oco_stop_or_target_fill_closes_and_journals_position(env, market_open, leg):
    row = _open_live_entry(env)  # manual, trailing off -> OCO GTT
    gtt = env.kite.gtt_book[row["kite_gtt_id"]]
    _precondition(gtt["type"] == env.kite.GTT_TYPE_OCO, f"expected an OCO GTT, got {gtt['type']}")

    fill_id = env.kite.trigger_gtt(row["kite_gtt_id"], leg=leg, status="COMPLETE")
    env.kite.set_holding("TCS", 0)
    _run_background_cycles(env)

    after = ticket_row(env.db, row["id"])
    violations = []
    if after["closed_at"] is None:
        violations.append(f"GTT {row['kite_gtt_id']} fired and SELL {fill_id} filled "
                          f"@ {env.kite.order_book[fill_id]['average_price']}, broker holds 0, "
                          f"but position {row['id']} is still open")
    if not _closed_trades_for(env, row["id"]):
        violations.append("no closed_trades row was written")
    assert not violations, violations


# ================================================================ P0-6
# Safety property: "GTT triggered" is not "position closed". Only a broker
# fill of the full quantity closes the position.
# PK-7 turned this strict xfail into a regression test: the GTT watcher now
# closes only on broker proof. Wider coverage lives in
# test_gtt_closure_proof.py.

def test_p0_6_triggered_gtt_with_complete_fill_closes_at_fill_price(env, market_open):
    # Already correct, and must survive the P0-6 fix: a matched COMPLETE fill
    # closes the position and journals it at the real fill price.
    row = _open_live_entry(env, trailing_enabled=True, trailing_trigger="after_target_1", trailing_distance=50.0)
    fill_id = env.kite.trigger_gtt(row["kite_gtt_id"], status="COMPLETE")
    env.kite.set_holding("TCS", 0)
    _run_background_cycles(env)

    assert ticket_row(env.db, row["id"])["closed_at"] is not None
    trades = _closed_trades_for(env, row["id"])
    assert len(trades) == 1
    assert trades[0]["exit_price"] == env.kite.order_book[fill_id]["average_price"]


@pytest.mark.parametrize("filled", [0, 2], ids=["unfilled", "partial"])
def test_p0_6_triggered_gtt_without_fill_keeps_position_open(env, market_open, filled):
    row = _open_live_entry(env, trailing_enabled=True, trailing_trigger="after_target_1", trailing_distance=50.0)
    # Gap-down through the stop: Kite fires the GTT, its SELL LIMIT sits OPEN.
    order_id = env.kite.trigger_gtt(row["kite_gtt_id"], status="OPEN", filled_quantity=filled,
                                    average_price=2885.5 if filled else 0)
    env.kite.set_holding("TCS", row["quantity"] - filled, average_price=3000.0)
    _run_background_cycles(env)

    after = ticket_row(env.db, row["id"])
    synthetic = _rows(env, "SELECT id, entry_fill_status, entry_price, filled_quantity FROM trade_tickets "
                           "WHERE closes_ticket_id = ?", row["id"])
    violations = []
    if after["closed_at"] is not None:
        violations.append(f"position {row['id']} marked closed although SELL {order_id} is "
                          f"{env.kite.order_book[order_id]['status']} with {filled}/{row['quantity']} filled "
                          f"and the broker still holds {row['quantity'] - filled}")
    # PK-13: the shares a partial fill sold ARE recorded — as a 'partial'
    # synthetic exit of exactly that many shares, never as a full exit.
    if [s for s in synthetic if s["entry_fill_status"] != "partial" or s["filled_quantity"] != filled] or (
            synthetic and not filled):
        violations.append(f"synthetic exit ticket(s) created: {synthetic}")
    if _closed_trades_for(env, row["id"]):
        violations.append(f"journal row written: {_closed_trades_for(env, row['id'])}")
    assert not violations, violations


# ================================================================ P0-7
# Safety property: placing an exit order is not exiting. Until the exit
# fills, the position stays open, keeps its protective GTT and is not
# journaled.
# PK-8 turned this strict xfail into a regression test: order_monitor now
# closes an explicit exit's position only on a broker COMPLETE. Wider
# coverage lives in test_exit_lifecycle.py.

@pytest.mark.parametrize("scenario", ["limit_market_hours", "market_amo_after_hours"])
def test_p0_7_unfilled_exit_keeps_protection_and_position_open(env, monkeypatch, scenario):
    from backend import market
    market_is_open = scenario == "limit_market_hours"
    monkeypatch.setattr(market, "is_market_open", lambda at=None: market_is_open)
    set_mode(env.db, "live")
    buy_id, gtt_id = _live_position(env)
    sell_id = _pending_exit(env, buy_id, entry_order_type="LIMIT" if market_is_open else "MARKET")
    env.kite.auto_fill = False

    done = env.client.post(f"/api/tickets/{sell_id}/confirm").json()["data"]
    _precondition(done["kite_order_id"] is not None, f"exit order not placed: {done}")
    _run_background_cycles(env)
    order = env.kite.order_book[done["kite_order_id"]]
    expected_status = "OPEN" if market_is_open else "AMO REQ RECEIVED"
    _precondition(order["status"] == expected_status and order["filled_quantity"] == 0,
                  f"exit should be accepted but unfilled, got {order['status']}")
    _precondition(_held(env, "TCS") == 5, "broker must still hold the shares")

    buy = ticket_row(env.db, buy_id)
    violations = []
    if env.kite.gtt_book[gtt_id]["status"] != "active":
        violations.append(f"protective GTT {gtt_id} is {env.kite.gtt_book[gtt_id]['status']!r} while exit "
                          f"{order['order_id']} is {order['status']} and 5 shares are still held")
    if buy["closed_at"] is not None:
        violations.append(f"position {buy_id} closed_at={buy['closed_at']} before any exit fill")
    trades = _closed_trades_for(env, buy_id)
    if trades:
        violations.append(f"journal row written before the fill: exit_price={trades[0]['exit_price']}, "
                          f"pnl={trades[0]['realized_pnl']}")
    assert not violations, violations


# ================================================================ P0-8
# Safety property: a state-changing request from another origin cannot
# approve a trade or remove protection. Fixed in PK-5 (backend/security.py);
# the wider policy matrix lives in test_csrf_protection.py.

_CROSS_SITE = {"Origin": "https://evil.example", "Content-Type": "text/plain"}


@pytest.mark.parametrize("endpoint", ["confirm", "gtt_cancel"])
def test_p0_8_cross_site_post_is_rejected(env, market_open, endpoint):
    from backend.main import app
    set_mode(env.db, "live")
    if endpoint == "confirm":
        t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
        _precondition(t["status"] == "pending_confirmation", f"ticket not approvable: {t['status']}")
        url, broker_call = f"/api/tickets/{t['id']}/confirm", "place_order"
    else:
        _, gtt_id = _live_position(env)
        url, broker_call = f"/api/gtts/{gtt_id}/cancel", "delete_gtt"

    # Exactly what a foreign page's simple request carries: its own Origin,
    # a CORS-safelisted content type and no application header.
    attacker = TestClient(app, base_url=TERMINAL_ORIGIN)
    r = attacker.post(url, content=b"", headers=_CROSS_SITE)
    violations = []
    if r.status_code != 403 or r.json()["detail"]["error_code"] != "CSRF_PROTECTION_FAILED":
        violations.append(f"cross-site POST {url} answered HTTP {r.status_code}, not a CSRF 403")
    if env.kite.calls_to(broker_call):
        violations.append(f"broker {broker_call} was called on behalf of the cross-site request")
    if endpoint == "confirm" and ticket_row(env.db, t["id"])["status"] != "pending_confirmation":
        violations.append(f"ticket {t['id']} moved to {ticket_row(env.db, t['id'])['status']!r}")
    if endpoint == "gtt_cancel" and env.kite.gtt_book[gtt_id]["status"] != "active":
        violations.append(f"protective GTT {gtt_id} is {env.kite.gtt_book[gtt_id]['status']!r}")
    assert not violations, violations


def test_p0_8_json_endpoints_reject_text_plain_body(env, market_open):
    # Second barrier, independent of the PK-5 middleware: even a request that
    # passes the origin check cannot drive a JSON-body endpoint with a
    # text/plain body. The cross-site form of the same request is now 403.
    set_mode(env.db, "live")
    _live_position(env)
    body = b'{"confirmation_phrase": "EXIT ALL"}'
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", content=body,
                        headers={"Content-Type": "text/plain"})
    assert r.status_code == 422
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", content=body, headers=_CROSS_SITE)
    assert r.status_code == 403
    assert env.kite.calls_to("place_order") == []
