"""PK-7: a protective GTT leaving 'active' is not proof that the position was
sold (P0-6).

When a GTT fires, Kite only PLACES the leg's LIMIT order. A gap through its
price leaves it unfilled or part-filled. A GTT deleted in Kite, expired, or
missing from get_gtts() sold nothing at all. The watcher therefore closes a
position only on broker evidence that its full quantity was sold, at the
broker's own fill price and time. Anything less keeps the position OPEN,
records what was found in reconciliation_log, and raises an urgent alert.
It never takes a price from the LTP and never sends an order.

Every rule is exercised for both kinds of watched position, a trailing stop
(single-leg GTT) and a fixed stop + target (OCO GTT), because PK-6 routes
both through the same watcher.

No sleeps. Nothing here reaches Zerodha or the network.
"""

import json

import pytest
import requests
from kiteconnect.exceptions import NetworkException

from conftest import set_mode, ticket_row
from test_baseline_api import _insert_ticket
from test_gtt_watcher import TRAILING, _assert_closed_by_fill, _errors_for, _exits, _flags, _tick
from test_live_safety import (  # noqa: F401 — unsafe_client is a fixture
    _assert_unknown_response, _closed_trades_for, _event_types, _infy_position, _live_position, _pending_exit,
    _precondition, _rows, _sells, unsafe_client,
)

KINDS = ["oco", "trailing"]
STOP_LIMIT = 2885.5  # buffered_stop_price(2900) — the stop leg's LIMIT price


@pytest.fixture
def market_open(monkeypatch):
    from backend import market
    monkeypatch.setattr(market, "is_market_open", lambda at=None: True)


@pytest.fixture(autouse=True)
def _fresh_failure_counters():
    from backend import trailing_engine
    trailing_engine._consecutive_errors.clear()
    trailing_engine._consecutive_failures.clear()
    yield
    trailing_engine._consecutive_errors.clear()
    trailing_engine._consecutive_failures.clear()


def _position(env, kind, **overrides) -> tuple[int, int]:
    """A filled live BUY 5 TCS held at the broker. 'oco': fixed stop 2900 +
    target 3300 in one OCO GTT. 'trailing': a single-leg stop GTT, exactly
    as execution places it for a trailing ticket."""
    if kind == "oco":
        return _live_position(env, **overrides)
    leg = {"transaction_type": "SELL", "quantity": 5, "order_type": "LIMIT", "product": "CNC", "price": STOP_LIMIT}
    gtt_id = env.kite.place_gtt(env.kite.GTT_TYPE_SINGLE, "TCS", "NSE", [2900.0], 3000.0, [leg])["trigger_id"]
    fields = dict(TRAILING, source="strategy", strategy_name="momentum_breakout", kite_gtt_id=gtt_id,
                  executed_at="2026-09-01 04:00:00", entry_fill_status="filled", filled_price=3000.0,
                  current_stop_loss=2900.0)
    fields.update(overrides)
    buy_id = _insert_ticket(env.db, **fields)
    env.kite.set_holding("TCS", 5, average_price=3000.0)
    return buy_id, gtt_id


def _assert_open_and_unrecorded(env, position_id):
    row = ticket_row(env.db, position_id)
    assert row["closed_at"] is None
    assert _exits(env, position_id) == []
    assert _closed_trades_for(env, position_id) == []
    assert _sells(env) == []                  # nothing was sent to the broker
    assert env.kite.calls_to("ltp") == []     # no price was looked up to guess with
    return row


def _assert_open_and_partly_sold(env, position_id, sold):
    """PK-13: a partial GTT fill is recorded as ONE 'partial' synthetic exit
    of exactly the shares sold — never a close, never a journal row, never
    an order sent, never a guessed price."""
    row = ticket_row(env.db, position_id)
    assert row["closed_at"] is None
    [partial] = _exits(env, position_id)
    assert (partial["entry_fill_status"], partial["filled_quantity"], partial["filled_price"]) == (
        "partial", sold, STOP_LIMIT)
    assert _closed_trades_for(env, position_id) == []
    assert _sells(env) == []
    assert env.kite.calls_to("ltp") == []
    return row


def _messages(env, position_id):
    return [e["message"] for e in _errors_for(env, position_id)]


# ---------------------------------------------------------------- A: gap-down, SELL LIMIT unfilled

@pytest.mark.parametrize("kind", KINDS)
def test_gap_down_unfilled_stop_keeps_position_open_until_the_exit_fills(env, kind):
    buy_id, gtt_id = _position(env, kind)
    order_id = env.kite.trigger_gtt(gtt_id, leg=0, status="OPEN")  # the market opened below the limit

    _tick()
    _tick()  # the same state again: no second alert

    row = _assert_open_and_unrecorded(env, buy_id)
    assert row["kite_gtt_id"] == gtt_id       # still watched: the order may yet fill
    [message] = _messages(env, buy_id)
    assert "GTT TRIGGERED — EXIT NOT FILLED — POSITION MAY BE UNPROTECTED" in message
    assert f"{order_id} OPEN 0/5 filled" in message
    assert [f["actual"] for f in _flags(env, buy_id)] == [f"exit order {order_id} OPEN 0/5 filled"]

    # The market comes back and the LIMIT fills: now, and only now, it closes.
    env.kite.set_order_state(order_id, "COMPLETE", filled_quantity=5, average_price=STOP_LIMIT)
    env.kite.set_holding("TCS", 0)
    _tick()
    _tick()

    _assert_closed_by_fill(env, buy_id, order_id)
    assert len(_messages(env, buy_id)) == 1


# ---------------------------------------------------------------- B: partial protective fill

@pytest.mark.parametrize("kind", KINDS)
def test_partial_fill_is_not_a_close_and_each_change_is_flagged(env, kind):
    buy_id, gtt_id = _position(env, kind)
    order_id = env.kite.trigger_gtt(gtt_id, leg=0, status="OPEN", filled_quantity=2, average_price=STOP_LIMIT)
    env.kite.set_holding("TCS", 3, average_price=3000.0)

    _tick()
    _tick()

    # PK-13: the 2 sold are recorded (a 'partial' synthetic exit), so the
    # position is 3 shares — but it is not closed, and nothing is journaled.
    row = _assert_open_and_partly_sold(env, buy_id, 2)
    assert row["kite_gtt_id"] == gtt_id
    [first] = _messages(env, buy_id)
    assert "GTT EXIT PARTIALLY FILLED — POSITION NOT CLOSED" in first
    assert f"Filled so far: 2 @ avg {STOP_LIMIT}" in first and "3 share(s) are still held" in first

    env.kite.set_order_state(order_id, "OPEN", filled_quantity=3)
    _tick()
    assert ticket_row(env.db, buy_id)["closed_at"] is None
    assert f"{order_id} OPEN 3/5 filled" in _messages(env, buy_id)[-1]

    # The rest is cancelled: 2 shares are left with no protection at all.
    env.kite.set_order_state(order_id, "CANCELLED")
    _tick()
    row = _assert_open_and_partly_sold(env, buy_id, 3)
    assert row["kite_gtt_id"] is None         # spent GTT, finished order: nothing left to watch
    assert "2 share(s) are still held and UNPROTECTED" in _messages(env, buy_id)[-1]
    assert [f["actual"] for f in _flags(env, buy_id)] == [
        f"exit order {order_id} OPEN 2/5 filled", f"exit order {order_id} OPEN 3/5 filled",
        f"exit order {order_id} CANCELLED 3/5 filled",
    ]
    _tick()
    assert len(_messages(env, buy_id)) == 3


# ---------------------------------------------------------------- C / D: full stop or target fill

@pytest.mark.parametrize("kind, leg, closer_to", [("oco", 0, "stop_loss"), ("oco", 1, "target"),
                                                  ("trailing", 0, "stop_loss")],
                         ids=["oco_stop", "oco_target", "trailing_stop"])
def test_full_fill_closes_at_the_broker_fill_price_and_time(env, kind, leg, closer_to):
    buy_id, gtt_id = _position(env, kind)
    order_id = env.kite.trigger_gtt(gtt_id, leg=leg, status="COMPLETE")
    env.kite.order_book[order_id]["exchange_timestamp"] = "2026-09-25 10:15:00"  # IST
    env.kite.set_holding("TCS", 0)

    _tick()
    _tick()  # closed: no longer watched, nothing recorded twice

    exit_ = _assert_closed_by_fill(env, buy_id, order_id)
    checklist = json.loads(exit_["rule_checklist"])["gtt_auto_trigger"]
    assert (checklist["closer_to"], checklist["evidence"]) == (closer_to, "gtt_order_link")
    assert exit_["executed_at"] == exit_["filled_at"] == "2026-09-25 04:45:00"  # stored in UTC
    assert ticket_row(env.db, buy_id)["closed_at"] == "2026-09-25 04:45:00"
    [trade] = _closed_trades_for(env, buy_id)
    assert trade["exit_date"] == "2026-09-25" and trade["quantity"] == 5
    assert env.kite.calls_to("ltp") == [] and _errors_for(env, buy_id) == []


# ---------------------------------------------------------------- E / F: GTT deleted, expired, missing

@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("gtt_state", ["deleted", "expired", "cancelled", "disabled", "rejected", "missing"])
def test_gtt_gone_without_a_fill_is_protection_missing_not_a_close(env, kind, gtt_state):
    buy_id, gtt_id = _position(env, kind)
    if gtt_state == "missing":
        del env.kite.gtt_book[gtt_id]
    else:
        env.kite.set_gtt_status(gtt_id, gtt_state)

    _tick()
    _tick()  # kite_gtt_id is now NULL: not watched, not re-flagged

    row = _assert_open_and_unrecorded(env, buy_id)
    assert row["kite_gtt_id"] is None
    [message] = _messages(env, buy_id)
    assert "PROTECTION MISSING — POSITION STILL OPEN" in message and "still OPEN but UNPROTECTED" in message
    assert "Kite shows 5 TCS held" in message
    assert [f["actual"] for f in _flags(env, buy_id)] == [f"GTT {gtt_state}"]


def test_gtt_deleted_and_the_position_sold_by_hand_in_kite_closes_at_that_fill(env):
    # A SELL of the whole position placed directly in Kite is real broker
    # evidence of the exit, even though no GTT placed it.
    buy_id, gtt_id = _position(env, "oco")
    env.kite.set_gtt_status(gtt_id, "deleted")
    sold = env.kite.add_broker_order("TCS", "SELL", 5, status="COMPLETE", order_type="MARKET", price=3050.0)

    _tick()

    exit_ = _assert_closed_by_fill(env, buy_id, sold)
    assert json.loads(exit_["rule_checklist"])["gtt_auto_trigger"]["evidence"] == "order_match"


# ---------------------------------------------------------------- G: several plausible SELL orders

@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("gtt_state", ["missing", "triggered_without_link"])
def test_several_plausible_sell_orders_are_never_guessed_between(env, kind, gtt_state):
    buy_id, gtt_id = _position(env, kind)
    if gtt_state == "missing":
        del env.kite.gtt_book[gtt_id]
        first = env.kite.add_broker_order("TCS", "SELL", 5, status="COMPLETE", price=2950.0)
    else:
        first = env.kite.trigger_gtt(gtt_id, leg=0, status="COMPLETE", expose_result=False)
    second = env.kite.add_broker_order("TCS", "SELL", 5, status="COMPLETE", price=STOP_LIMIT)

    _tick()
    _tick()

    row = _assert_open_and_unrecorded(env, buy_id)
    assert row["kite_gtt_id"] == gtt_id
    [message] = _messages(env, buy_id)
    assert "CLOSURE NEEDS REVIEW" in message and "2 SELL orders could each be its exit" in message
    assert first in message and second in message


# ---------------------------------------------------------------- H: two app positions in one symbol

def _second_tcs_position(env) -> tuple[int, int]:
    leg = {"transaction_type": "SELL", "quantity": 5, "order_type": "LIMIT", "product": "CNC", "price": 2785.0}
    gtt_id = env.kite.place_gtt(env.kite.GTT_TYPE_SINGLE, "TCS", "NSE", [2800.0], 3000.0, [leg])["trigger_id"]
    buy_id = _insert_ticket(env.db, stop_loss=2800.0, current_stop_loss=2800.0, kite_gtt_id=gtt_id,
                            executed_at="2026-09-02 04:00:00", entry_fill_status="filled", filled_price=3000.0)
    env.kite.set_holding("TCS", 10, average_price=3000.0)
    return buy_id, gtt_id


def test_an_unlinked_sell_is_not_attributed_when_app_positions_share_the_symbol(env):
    a_id, a_gtt = _position(env, "oco")
    b_id, b_gtt = _second_tcs_position(env)
    del env.kite.gtt_book[a_gtt]
    sold = env.kite.add_broker_order("TCS", "SELL", 5, status="COMPLETE", price=2950.0)
    env.kite.set_holding("TCS", 5, average_price=3000.0)  # 5 of the 10 are gone — but whose?

    _tick()

    _assert_open_and_unrecorded(env, a_id)
    _assert_open_and_unrecorded(env, b_id)
    assert ticket_row(env.db, b_id)["kite_gtt_id"] == b_gtt
    [message] = _messages(env, a_id)
    assert "CLOSURE NEEDS REVIEW" in message and sold in message
    assert f"open app position(s) {b_id} hold the same symbol" in message
    assert _errors_for(env, b_id) == []


def test_the_gtt_link_closes_exactly_the_right_one_of_two_same_symbol_positions(env):
    a_id, a_gtt = _position(env, "oco")
    b_id, b_gtt = _second_tcs_position(env)
    b_fill = env.kite.trigger_gtt(b_gtt, leg=0, status="COMPLETE")
    env.kite.set_holding("TCS", 5, average_price=3000.0)

    _tick()

    _assert_closed_by_fill(env, b_id, b_fill)
    a = ticket_row(env.db, a_id)
    assert a["closed_at"] is None and a["kite_gtt_id"] == a_gtt
    assert _exits(env, a_id) == [] and _errors_for(env, a_id) == []


# ---------------------------------------------------------------- I: broker holds none, no identifiable order

@pytest.mark.parametrize("kind", KINDS)
def test_broker_holding_nothing_without_an_identifiable_order_is_review_not_a_close(env, kind):
    # Chosen rule: a quantity drop alone never closes a position. It cannot
    # say which order sold, or at what price, and the journal has no way to
    # record a close without a price — so it is flagged for a human.
    buy_id, gtt_id = _position(env, kind)
    env.kite.set_gtt_status(gtt_id, "triggered")  # no leg result, no order in the book
    env.kite.set_holding("TCS", 0)

    _tick()
    _tick()

    row = _assert_open_and_unrecorded(env, buy_id)
    assert row["kite_gtt_id"] == gtt_id
    [message] = _messages(env, buy_id)
    assert "CLOSURE NEEDS REVIEW" in message and "Kite now shows no TCS held" in message
    assert [f["actual"] for f in _flags(env, buy_id)] == [
        "GTT triggered; closure unproven: no SELL order at Kite matches this position; held 0"]


def test_linked_exit_order_missing_from_the_book_is_review_not_a_close(env):
    buy_id, gtt_id = _position(env, "oco")
    order_id = env.kite.trigger_gtt(gtt_id, leg=0, status="COMPLETE")
    del env.kite.order_book[order_id]  # e.g. the order book has rolled over to a new session
    env.kite.set_holding("TCS", 0)

    _tick()

    _assert_open_and_unrecorded(env, buy_id)
    [message] = _messages(env, buy_id)
    assert "CLOSURE NEEDS REVIEW" in message and f"its exit order {order_id} is not in Kite's order book" in message


def test_linked_exit_order_found_through_order_history_closes(env, monkeypatch):
    buy_id, gtt_id = _position(env, "oco")
    order_id = env.kite.trigger_gtt(gtt_id, leg=0, status="COMPLETE")
    monkeypatch.setattr(env.kite, "orders", lambda: [])  # listed nowhere but its own history

    _tick()

    _assert_closed_by_fill(env, buy_id, order_id)


# ---------------------------------------------------------------- the GTT fired but Kite could not place its order

@pytest.mark.parametrize("kind", KINDS)
def test_gtt_whose_exit_order_kite_could_not_place_leaves_the_position_open_and_unprotected(env, kind):
    buy_id, gtt_id = _position(env, kind)
    reason = "Your order price is lower than the current lower circuit limit"
    _precondition(env.kite.trigger_gtt(gtt_id, leg=0, placement="failed", rejection_reason=reason) is None,
                  "a failed placement creates no order")

    _tick()
    _tick()

    row = _assert_open_and_unrecorded(env, buy_id)
    assert row["kite_gtt_id"] is None
    [message] = _messages(env, buy_id)
    assert "GTT TRIGGERED BUT ITS EXIT ORDER FAILED" in message and reason in message
    assert "still OPEN but UNPROTECTED" in message


# ---------------------------------------------------------------- which orders may be matched at all

def test_a_sell_placed_before_the_position_was_entered_is_not_its_exit(env):
    buy_id, gtt_id = _position(env, "oco")  # entered 2026-09-01 04:00 UTC = 09:30 IST
    del env.kite.gtt_book[gtt_id]
    env.kite.add_broker_order("TCS", "SELL", 5, status="COMPLETE", price=2950.0, order_timestamp="2026-09-01 09:15:00")

    _tick()

    assert _assert_open_and_unrecorded(env, buy_id)["kite_gtt_id"] is None
    assert "PROTECTION MISSING" in _messages(env, buy_id)[0]


@pytest.mark.parametrize("owned_by", ["another_ticket", "app_tag", "earlier_gtt_closure"])
def test_an_order_the_app_already_accounts_for_is_not_this_positions_exit(env, owned_by):
    buy_id, gtt_id = _position(env, "oco")
    del env.kite.gtt_book[gtt_id]
    tag = "kt99x1a2b3c" if owned_by == "app_tag" else None
    order_id = env.kite.add_broker_order("TCS", "SELL", 5, status="COMPLETE", price=2950.0, tag=tag)
    if owned_by == "another_ticket":
        _insert_ticket(env.db, transaction_type="SELL", kite_order_id=order_id, closed_at="2026-09-02 04:00:00")
    elif owned_by == "earlier_gtt_closure":
        _insert_ticket(env.db, transaction_type="SELL", rule_checklist=json.dumps(
            {"gtt_auto_trigger": {"pass": True, "matched_order_id": order_id}}))

    _tick()

    assert _assert_open_and_unrecorded(env, buy_id)["kite_gtt_id"] is None
    assert "PROTECTION MISSING" in _messages(env, buy_id)[0]


def test_without_a_link_a_triggered_gtts_order_must_carry_one_of_its_leg_prices(env):
    buy_id, gtt_id = _position(env, "oco")
    gtt_order = env.kite.trigger_gtt(gtt_id, leg=0, status="OPEN", expose_result=False)
    other = env.kite.add_broker_order("TCS", "SELL", 5, status="COMPLETE", price=2950.0)  # no leg is at 2950

    _tick()

    _assert_open_and_unrecorded(env, buy_id)
    [message] = _messages(env, buy_id)
    assert "GTT TRIGGERED — EXIT NOT FILLED" in message and gtt_order in message and other not in message


def test_an_order_the_gtt_links_to_is_still_checked_against_the_position(env):
    buy_id, gtt_id = _position(env, "oco")
    order_id = env.kite.trigger_gtt(gtt_id, leg=0, status="COMPLETE")
    env.kite.order_book[order_id].update(quantity=10, filled_quantity=10)  # not this position's 5

    _tick()

    _assert_open_and_unrecorded(env, buy_id)
    [message] = _messages(env, buy_id)
    assert "CLOSURE NEEDS REVIEW" in message and "does not match this position" in message


# ---------------------------------------------------------------- the order book cannot be read

def test_order_book_outage_is_never_read_as_no_fill(env):
    buy_id, gtt_id = _position(env, "oco")
    order_id = env.kite.trigger_gtt(gtt_id, leg=0, status="COMPLETE")
    for cycle in (1, 2, 3):
        env.kite.raise_on_next("orders", NetworkException("Gateway timed out"))
        _tick()
        row = ticket_row(env.db, buy_id)
        assert row["closed_at"] is None and row["kite_gtt_id"] == gtt_id
        assert _exits(env, buy_id) == [] and _flags(env, buy_id) == []
        assert len(_errors_for(env, buy_id)) == (1 if cycle == 3 else 0)
    assert "failed 3 times in a row" in _messages(env, buy_id)[0]

    _tick()  # the broker answers again

    _assert_closed_by_fill(env, buy_id, order_id)


# ---------------------------------------------------------------- J / K: another exit owns the position (PK-4)

@pytest.mark.parametrize("owner_status", ["confirmed", "executed"])
@pytest.mark.parametrize("fired", ["unfilled", "filled"])
def test_gtt_firing_while_an_explicit_exit_owns_the_position_records_nothing(env, owner_status, fired):
    buy_id, gtt_id = _position(env, "oco")
    # An executed owner is EXITING: at the broker, fill still 'pending'
    # (PK-13: an exit whose order has ended no longer owns the position).
    owner = _pending_exit(env, buy_id, status=owner_status, mode="live",
                          submission_state="acknowledged", kite_order_id="250000000777",
                          entry_fill_status="pending" if owner_status == "executed" else None)
    env.kite.trigger_gtt(gtt_id, leg=0, status="OPEN" if fired == "unfilled" else "COMPLETE")

    _tick()
    _tick()

    assert [e["id"] for e in _exits(env, buy_id)] == [owner]
    assert ticket_row(env.db, buy_id)["closed_at"] is None
    assert _closed_trades_for(env, buy_id) == [] and _sells(env) == []
    [flag] = _flags(env, buy_id)
    assert f"exit ticket {owner} owns the position" in flag["actual"]
    [message] = _messages(env, buy_id)
    assert "POSSIBLE DOUBLE SELL" in message


def test_gtt_fired_unfilled_while_an_unknown_exit_exists_creates_no_sibling(env, unsafe_client, market_open):
    set_mode(env.db, "live")
    buy_id, gtt_id = _position(env, "oco")
    owner = _pending_exit(env, buy_id)
    env.kite.raise_on_next("place_order", requests.ReadTimeout("timed out"))
    _assert_unknown_response(unsafe_client.post(f"/api/tickets/{owner}/confirm"))
    env.kite.trigger_gtt(gtt_id, leg=0, status="OPEN")

    _tick()
    _tick()

    assert [e["id"] for e in _exits(env, buy_id)] == [owner]
    assert ticket_row(env.db, owner)["submission_state"] == "unknown"
    assert ticket_row(env.db, buy_id)["closed_at"] is None and _closed_trades_for(env, buy_id) == []
    assert "POSSIBLE DOUBLE SELL" in _messages(env, buy_id)[0]
    assert len(env.kite.calls_to("place_order")) == 1  # only the exit attempt itself


# ---------------------------------------------------------------- L: trailing and OCO follow the same rules

def _apply(env, gtt_id, scenario):
    if scenario == "filled":
        env.kite.trigger_gtt(gtt_id, leg=0, status="COMPLETE")
    elif scenario == "unfilled":
        env.kite.trigger_gtt(gtt_id, leg=0, status="OPEN")
    elif scenario == "partial":
        env.kite.trigger_gtt(gtt_id, leg=0, status="OPEN", filled_quantity=2, average_price=STOP_LIMIT)
    elif scenario == "exit_failed":
        env.kite.trigger_gtt(gtt_id, leg=0, placement="failed", rejection_reason="circuit")
    elif scenario == "triggered_no_order":
        env.kite.set_gtt_status(gtt_id, "triggered")
    elif scenario == "ambiguous":
        del env.kite.gtt_book[gtt_id]
        env.kite.add_broker_order("TCS", "SELL", 5, status="COMPLETE", price=2950.0)
        env.kite.add_broker_order("TCS", "SELL", 5, status="COMPLETE", price=2960.0)
    elif scenario == "deleted":
        env.kite.set_gtt_status(gtt_id, "deleted")
    elif scenario == "missing":
        del env.kite.gtt_book[gtt_id]


# closed, GTT still watched, exits, journal rows, flags, alerts
_EXPECTED = {
    "filled": (True, True, 1, 1, 0, 0),
    "unfilled": (False, True, 0, 0, 1, 1),
    "partial": (False, True, 1, 0, 1, 1),  # PK-13: the shares sold are recorded (a 'partial' exit), not closed
    "exit_failed": (False, False, 0, 0, 1, 1),
    "triggered_no_order": (False, True, 0, 0, 1, 1),
    "ambiguous": (False, True, 0, 0, 1, 1),
    "deleted": (False, False, 0, 0, 1, 1),
    "missing": (False, False, 0, 0, 1, 1),
}


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("scenario", list(_EXPECTED))
def test_trailing_and_oco_positions_follow_the_same_proof_rules(env, kind, scenario):
    buy_id, gtt_id = _position(env, kind)
    _apply(env, gtt_id, scenario)

    _tick()

    row = ticket_row(env.db, buy_id)
    outcome = (row["closed_at"] is not None, row["kite_gtt_id"] == gtt_id, len(_exits(env, buy_id)),
               len(_closed_trades_for(env, buy_id)), len(_flags(env, buy_id)), len(_errors_for(env, buy_id)))
    assert outcome == _EXPECTED[scenario]
    assert _sells(env) == [] and env.kite.calls_to("ltp") == []
