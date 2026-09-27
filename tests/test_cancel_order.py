"""PK-9 (P1-1): a manual broker-order cancel from Orders & Trades.

Kite ACCEPTING a cancel request is not a cancellation: the order may fill in
the same second, or be partly filled already. The cancel route therefore
never changes the linked ticket's state; order_monitor stays the only
authority on the order's terminal state and resolves the ticket from broker
evidence. No sleeps; broker outcomes are driven through FakeKite.
"""

import pytest
import requests
from kiteconnect.exceptions import NetworkException, OrderException, TokenException

from conftest import set_mode, ticket_row
from test_baseline_api import _manual_ticket
from test_exit_lifecycle import _exiting_position, _fill, _open_positions, _still_open_and_protected
from test_live_safety import (  # noqa: F401 — market_open / unsafe_client are fixtures
    _closed_trades_for, _monitor, _precondition, _rows, market_open, unsafe_client,
)


def _open_entry(env) -> tuple[int, str, int]:
    """A live BUY placed at the broker and still OPEN (unfilled), with its
    protective GTT: (ticket id, order id, GTT id)."""
    set_mode(env.db, "live")
    env.kite.auto_fill = False
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    done = env.client.post(f"/api/tickets/{t['id']}/confirm", json={"expected_mode": "live"}).json()["data"]
    _precondition(done["status"] == "executed" and done["entry_fill_status"] == "pending",
                  f"entry not left OPEN at the broker: {done}")
    _precondition(env.kite.order_book[done["kite_order_id"]]["status"] == "OPEN", "entry order should be OPEN")
    return t["id"], done["kite_order_id"], done["kite_gtt_id"]


def _cancel(client, order_id):
    return client.post(f"/api/orders/{order_id}/cancel?variety=regular")


def _cancel_events(env, ticket_id):
    return [e["detail"] for e in _rows(env, "SELECT detail FROM ticket_events WHERE ticket_id = ? ORDER BY id",
                                       ticket_id) if "Cancellation requested" in e["detail"]]


def _system_events(env):
    return _rows(env, "SELECT event_type, detail FROM system_events WHERE event_type = 'order_cancelled_manually'")


# ---------------------------------------------------------------- entry

def test_cancel_open_entry_leaves_ticket_for_the_monitor(env, market_open):
    ticket_id, order_id, gtt_id = _open_entry(env)
    before = ticket_row(env.db, ticket_id)

    r = _cancel(env.client, order_id)

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["cancel_requested"] is True and "cancelled" not in data
    assert data["linked_ticket_id"] == ticket_id
    # Not cancelled early: the ticket is exactly as it was — still an
    # unresolved broker order, still protected.
    assert ticket_row(env.db, ticket_id) == before
    assert (before["status"], before["entry_fill_status"], before["kite_gtt_id"]) == ("executed", "pending", gtt_id)
    assert [c["order_id"] for c in env.kite.calls_to("cancel_order")] == [order_id]
    assert len(_cancel_events(env, ticket_id)) == 1
    [event] = _system_events(env)
    assert "cancel requested" in event["detail"] and order_id in event["detail"]


def test_broker_cancelled_entry_is_resolved_by_the_monitor(env, market_open):
    ticket_id, order_id, gtt_id = _open_entry(env)
    _cancel(env.client, order_id)
    _precondition(env.kite.order_book[order_id]["status"] == "CANCELLED", "FakeKite should report CANCELLED")

    _monitor(env)

    row = ticket_row(env.db, ticket_id)
    assert (row["status"], row["entry_fill_status"], row["kite_gtt_id"]) == ("cancelled", "cancelled", None)
    assert [c["trigger_id"] for c in env.kite.calls_to("delete_gtt")] == [gtt_id]
    assert _open_positions(env)[0] == []


def test_entry_fill_during_cancel_race_wins(env, market_open):
    ticket_id, order_id, gtt_id = _open_entry(env)
    _cancel(env.client, order_id)
    # The exchange filled the order before the cancel took effect.
    env.kite.set_order_state(order_id, "COMPLETE", filled_quantity=5, average_price=2998.0)

    _monitor(env)

    row = ticket_row(env.db, ticket_id)
    assert (row["status"], row["entry_fill_status"], row["filled_price"]) == ("executed", "filled", 2998.0)
    assert row["kite_gtt_id"] == gtt_id and env.kite.calls_to("delete_gtt") == []
    assert _open_positions(env)[0] == [ticket_id]


def test_partially_filled_entry_cancel_is_not_reported_as_cancelled(env, market_open):
    ticket_id, order_id, gtt_id = _open_entry(env)
    env.kite.set_order_state(order_id, "OPEN", filled_quantity=2, average_price=2999.0)

    _cancel(env.client, order_id)
    assert ticket_row(env.db, ticket_id)["status"] == "executed"
    _monitor(env)

    row = ticket_row(env.db, ticket_id)
    assert row["status"] != "cancelled"
    assert (row["status"], row["entry_fill_status"], row["kite_gtt_id"]) == ("executed", "partial", gtt_id)
    assert env.kite.calls_to("delete_gtt") == []


# ---------------------------------------------------------------- exit

def test_cancel_exiting_exit_keeps_position_open_and_protected(env, market_open):
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env, entry_order_type="LIMIT")
    exit_before = ticket_row(env.db, sell_id)

    r = _cancel(env.client, order_id)

    assert r.status_code == 200 and r.json()["data"]["linked_ticket_id"] == sell_id
    assert ticket_row(env.db, sell_id) == exit_before
    assert not _still_open_and_protected(env, buy_id, gtt_id)
    listed = {t["id"]: t for t in env.client.get("/api/tickets").json()["data"]}
    assert listed[sell_id]["exit_state"] == "exiting"
    assert listed[buy_id]["exit_in_flight_ticket_id"] == sell_id

    # The broker reports CANCELLED: the exit ends, the position was never
    # closed and keeps its GTT — nothing reopened, nothing to undo.
    _monitor(env)
    exit_row = ticket_row(env.db, sell_id)
    assert (exit_row["status"], exit_row["entry_fill_status"]) == ("failed", "cancelled")
    assert not _still_open_and_protected(env, buy_id, gtt_id)
    assert _open_positions(env)[0] == [buy_id]


def test_exit_fill_during_cancel_race_wins(env, market_open):
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env, entry_order_type="LIMIT")
    _cancel(env.client, order_id)
    _fill(env, order_id, price=3120.5, when_ist="2026-09-25 10:15:00")  # filled before the cancel landed

    _monitor(env)

    exit_row = ticket_row(env.db, sell_id)
    assert (exit_row["status"], exit_row["entry_fill_status"], exit_row["filled_price"]) == (
        "executed", "filled", 3120.5)
    assert ticket_row(env.db, buy_id)["closed_at"] == "2026-09-25 04:45:00"
    [trade] = _closed_trades_for(env, buy_id)
    assert trade["exit_price"] == 3120.5
    assert [c["trigger_id"] for c in env.kite.calls_to("delete_gtt")] == [gtt_id]


def test_partially_filled_exit_cancel_is_not_reported_as_cancelled(env, market_open):
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env, entry_order_type="LIMIT")
    env.kite.set_order_state(order_id, "OPEN", filled_quantity=2, average_price=3110.0)

    _cancel(env.client, order_id)
    _monitor(env)

    exit_row = ticket_row(env.db, sell_id)
    assert (exit_row["status"], exit_row["entry_fill_status"]) == ("executed", "partial")
    assert not _still_open_and_protected(env, buy_id, gtt_id)
    listed = {t["id"]: t for t in env.client.get("/api/tickets").json()["data"]}
    assert listed[sell_id]["exit_state"] == "exit_partial"
    assert listed[buy_id]["exit_in_flight_ticket_id"] == sell_id  # still owns the position


# ---------------------------------------------------------------- repeats and failures

def test_repeated_cancel_request_is_safe(env, market_open):
    ticket_id, order_id, _ = _open_entry(env)
    before = ticket_row(env.db, ticket_id)

    assert _cancel(env.client, order_id).status_code == 200
    assert _cancel(env.client, order_id).status_code == 200
    # Kite refuses a cancel of an order it has already cancelled.
    env.kite.raise_on_next("cancel_order", OrderException("Order cannot be cancelled as it is already cancelled"))
    third = _cancel(env.client, order_id)

    assert third.status_code == 502
    assert ticket_row(env.db, ticket_id) == before
    assert len(_cancel_events(env, ticket_id)) == 2
    _monitor(env)
    assert ticket_row(env.db, ticket_id)["status"] == "cancelled"


@pytest.mark.parametrize("exc, status", [
    (OrderException("Order cannot be cancelled"), 502),
    (NetworkException("Gateway timed out"), 502),
    (TokenException("Token expired"), 401),
    (requests.ReadTimeout("read timed out"), 500),
], ids=["order_exception", "network_exception", "token_exception", "raw_timeout"])
def test_broker_cancel_failure_leaves_ticket_untouched(env, unsafe_client, market_open, exc, status):
    ticket_id, order_id, _ = _open_entry(env)
    before = ticket_row(env.db, ticket_id)
    events_before = _rows(env, "SELECT id FROM ticket_events WHERE ticket_id = ?", ticket_id)
    env.kite.raise_on_next("cancel_order", exc)

    r = _cancel(unsafe_client, order_id)

    assert r.status_code == status
    assert ticket_row(env.db, ticket_id) == before
    assert _rows(env, "SELECT id FROM ticket_events WHERE ticket_id = ?", ticket_id) == events_before
    assert _system_events(env) == []
    assert env.kite.order_book[order_id]["status"] == "OPEN"
