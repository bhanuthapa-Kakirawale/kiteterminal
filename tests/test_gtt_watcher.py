"""PK-6: the GTT lifecycle watcher covers every live protected position.

trailing_engine used to notice a fired or vanished protective GTT only for
trailing positions, so a fixed stop + target (OCO) position stayed open in
the app after Kite had sold it (P0-5). The watcher now runs for every live,
filled, unclosed position that has a GTT; the ratchet still runs only for
trailing ones. Each cycle runs off the event loop, and one position's
failure no longer stops the others being checked (P1-9).

These tests pin WHO is watched. WHAT evidence closes a position (P0-6,
PK-7) is pinned in test_gtt_closure_proof.py; here closure outcomes are
asserted only where a real fill exists, and otherwise a non-trailing
position is compared with a trailing one.

No sleeps: threads hand over through Events whose timeouts are only safety
bounds. Nothing here reaches Zerodha or the network.
"""

import asyncio
import json
import logging
import threading

import pytest
import requests
from kiteconnect.exceptions import NetworkException, TokenException

from conftest import displayed_mode, set_mode, ticket_row
from test_baseline_api import _insert_ticket
from test_live_safety import (  # noqa: F401 — unsafe_client is a fixture
    _assert_unknown_response, _call, _closed_trades_for, _event_types, _infy_position, _live_position,
    _open_live_entry, _pending_exit, _precondition, _protective_oco_gtt, _rows, _sells, unsafe_client,
)

TRAILING = dict(trailing_enabled=1, trailing_trigger="after_target_1", trailing_distance=50.0)


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


def _tick():
    from backend import trailing_engine
    asyncio.run(trailing_engine._tick_once())


def _exits(env, position_id):
    return _rows(env, "SELECT * FROM trade_tickets WHERE closes_ticket_id = ? ORDER BY id", position_id)


def _flags(env, ticket_id):
    return _rows(env, "SELECT check_type, actual FROM reconciliation_log WHERE ticket_id = ? ORDER BY id", ticket_id)


def _errors_for(env, ticket_id):
    return [b for b in env.broadcasts if b.get("type") == "trailing_error" and b.get("ticket_id") == ticket_id]


def _reliance_position(env, **overrides) -> tuple[int, int]:
    """A third filled live position: BUY 5 RELIANCE, protected by an OCO GTT."""
    gtt_id = _protective_oco_gtt(env, symbol="RELIANCE", stop=2400.0, target=2800.0)
    fields = dict(symbol="RELIANCE", instrument_token=738561, entry_price=2500.0, stop_loss=2400.0,
                  target_1=2800.0, kite_gtt_id=gtt_id, executed_at="2026-09-01 04:00:00",
                  entry_fill_status="filled", filled_price=2500.0, current_stop_loss=2400.0)
    fields.update(overrides)
    buy_id = _insert_ticket(env.db, **fields)
    env.kite.set_holding("RELIANCE", 5, average_price=2500.0)
    return buy_id, gtt_id


def _assert_closed_by_fill(env, position_id, fill_id):
    fill = env.kite.order_book[fill_id]
    assert ticket_row(env.db, position_id)["closed_at"] is not None
    [exit_] = _exits(env, position_id)
    assert (exit_["status"], exit_["transaction_type"], exit_["quantity"]) == ("executed", "SELL", 5)
    assert (exit_["entry_fill_status"], exit_["filled_price"]) == ("filled", fill["average_price"])
    assert json.loads(exit_["rule_checklist"])["gtt_auto_trigger"]["matched_order_id"] == fill_id
    [trade] = _closed_trades_for(env, position_id)
    assert (trade["sell_ticket_id"], trade["exit_price"]) == (exit_["id"], fill["average_price"])
    return exit_


# ---------------------------------------------------------------- A / B / C

@pytest.mark.parametrize("leg, closer_to", [(0, "stop_loss"), (1, "target")], ids=["stop", "target"])
def test_non_trailing_oco_leg_fill_is_closed_and_journaled(env, market_open, leg, closer_to):
    row = _open_live_entry(env)  # manual ticket, trailing off (the form default)
    _precondition(not row["trailing_enabled"], "expected a non-trailing position")
    _precondition(env.kite.gtt_book[row["kite_gtt_id"]]["type"] == env.kite.GTT_TYPE_OCO, "expected an OCO GTT")

    fill_id = env.kite.trigger_gtt(row["kite_gtt_id"], leg=leg, status="COMPLETE")
    env.kite.set_holding("TCS", 0)
    _tick()
    _tick()  # a second cycle changes nothing: the position is no longer watched

    exit_ = _assert_closed_by_fill(env, row["id"], fill_id)
    assert json.loads(exit_["rule_checklist"])["gtt_auto_trigger"]["closer_to"] == closer_to
    assert "closed" in _event_types(env, row["id"])
    assert {"type": "tickets_changed", "reason": "gtt_closed", "ticket_id": row["id"]} in env.broadcasts
    assert env.kite.calls_to("modify_gtt") == []  # an OCO is never ratcheted


def test_rsi_mean_reversion_position_oco_closure_is_detected(env, market_open):
    # Built by the real strategy ticket builder from the real module, so the
    # ticket is exactly what an RSI signal produces: no trailing, OCO GTT.
    from backend import order_monitor, strategy_engine
    from backend.accounts import get_default_account
    from backend.strategies import rsi_mean_reversion as rsi

    params = env.client.get("/api/strategies/rsi_mean_reversion").json()["data"]["params"]
    candidate = {"symbol": "TCS", "instrument_token": 2953217, "close": 3000.0, "atr_14": 40.0,
                 "swing_low": 2950.0, "checklist": {"rsi_condition": {"pass": True}}, "conditions": [],
                 "markers": [], "signal_date": "2026-09-25"}
    _precondition(rsi.compute_entry_levels(candidate, params)["trailing_distance"] is None,
                  "rsi_mean_reversion should not trail")
    conn = env.db.get_connection()
    try:
        tid = strategy_engine._create_entry_ticket(rsi, conn, get_default_account(conn), env.kite, candidate, params)
    finally:
        conn.close()
    t = ticket_row(env.db, tid)
    _precondition((t["strategy_name"], t["trailing_enabled"], t["status"]) ==
                  ("rsi_mean_reversion", 0, "pending_confirmation"), f"unexpected RSI ticket: {t}")

    set_mode(env.db, "live")
    done = env.client.post(f"/api/tickets/{tid}/confirm", json=displayed_mode(env.db)).json()["data"]
    _precondition(done["status"] == "executed" and done["kite_gtt_id"], f"RSI entry not placed: {done}")
    order_monitor.check_pending_orders(env.kite)
    row = ticket_row(env.db, tid)
    _precondition(row["entry_fill_status"] == "filled", "RSI entry should be filled")
    _precondition(env.kite.gtt_book[row["kite_gtt_id"]]["type"] == env.kite.GTT_TYPE_OCO, "expected an OCO GTT")

    fill_id = env.kite.trigger_gtt(row["kite_gtt_id"], leg=0, status="COMPLETE")
    env.kite.set_holding("TCS", 0)
    _tick()

    after = ticket_row(env.db, tid)
    assert after["closed_at"] is not None
    [exit_] = _exits(env, tid)
    assert exit_["quantity"] == row["quantity"] and exit_["strategy_name"] == "rsi_mean_reversion"
    assert exit_["filled_price"] == env.kite.order_book[fill_id]["average_price"]
    assert len(_closed_trades_for(env, tid)) == 1


# ---------------------------------------------------------------- parity with trailing

_UNTOUCHED = (False, False, [], 0, 0, 0)


def _apply_gtt_event(env, gtt_id, event):
    if event == "filled":
        env.kite.trigger_gtt(gtt_id, leg=0, status="COMPLETE")
    elif event == "triggered_unfilled":
        env.kite.trigger_gtt(gtt_id, leg=0, status="OPEN")
    elif event == "deleted_no_fill":
        env.kite.set_gtt_status(gtt_id, "deleted")
    elif event == "vanished":
        del env.kite.gtt_book[gtt_id]


def _outcome(env, position_id):
    row = ticket_row(env.db, position_id)
    return (row["closed_at"] is not None, row["kite_gtt_id"] is None,
            [(e["status"], e["entry_fill_status"]) for e in _exits(env, position_id)],
            len(_closed_trades_for(env, position_id)), len(_flags(env, position_id)),
            len(_errors_for(env, position_id)))


@pytest.mark.parametrize("event", ["filled", "triggered_unfilled", "deleted_no_fill", "vanished"])
def test_non_trailing_position_gets_the_same_gtt_handling_as_a_trailing_one(env, market_open, event):
    # Relative on purpose: it holds whatever each event does, as long as
    # both kinds of position are treated alike (PK-7 changed the outcome
    # of triggered_unfilled and vanished for both at once).
    oco_id, oco_gtt = _live_position(env)
    trl_id, trl_gtt = _infy_position(env, **TRAILING)
    _apply_gtt_event(env, oco_gtt, event)
    _apply_gtt_event(env, trl_gtt, event)

    _tick()

    trailing = _outcome(env, trl_id)
    _precondition(trailing != _UNTOUCHED, f"the trailing position should react to '{event}'")
    assert _outcome(env, oco_id) == trailing


def test_non_trailing_gtt_deleted_without_fill_is_flagged_unprotected_not_closed(env, market_open):
    buy_id, gtt_id = _live_position(env)
    env.kite.set_gtt_status(gtt_id, "deleted")

    _tick()
    _tick()  # kite_gtt_id is now NULL, so the position is not re-flagged

    row = ticket_row(env.db, buy_id)
    assert row["closed_at"] is None and row["kite_gtt_id"] is None
    assert _exits(env, buy_id) == [] and _closed_trades_for(env, buy_id) == []
    [error] = _errors_for(env, buy_id)
    assert "still OPEN but UNPROTECTED" in error["message"]
    assert [f["actual"] for f in _flags(env, buy_id)] == ["GTT deleted"]


# ---------------------------------------------------------------- D

def test_trailing_ratchets_and_oco_is_left_alone_in_the_same_cycle(env, market_open):
    gtt_id = env.kite.place_gtt("single", "TCS", "NSE", [2900.0], 3000.0, [])["trigger_id"]
    trl_id = _insert_ticket(env.db, trailing_active=1, current_stop_loss=2900.0, kite_gtt_id=gtt_id,
                            executed_at="2026-09-01 04:00:00", entry_fill_status="filled", **TRAILING)
    oco_id, oco_gtt = _infy_position(env)
    env.kite.ltp_map["NSE:TCS"] = 3100.0
    env.kite.ltp_map["NSE:INFY"] = 1700.0  # beyond target_1: would activate trailing if this position trailed
    oco_before = ticket_row(env.db, oco_id)

    _tick()

    [call] = env.kite.calls_to("modify_gtt")
    assert call["trigger_id"] == gtt_id and call["trigger_values"] == [3050.0]
    assert ticket_row(env.db, trl_id)["current_stop_loss"] == 3050.0
    assert ticket_row(env.db, oco_id) == oco_before  # no activation, no check stamp, no stop change
    assert _event_types(env, oco_id) == []
    assert env.kite.gtt_book[oco_gtt]["status"] == "active"
    assert [c["keys"] for c in env.kite.calls_to("ltp")] == [["NSE:TCS"]]


def test_oco_is_not_ratcheted_when_a_trailing_position_shares_its_symbol(env, market_open):
    # The trailing position's LTP is fetched, so the OCO position has one
    # too: only the trailing_enabled check keeps it out of the ratchet.
    gtt_id = env.kite.place_gtt("single", "TCS", "NSE", [2900.0], 3000.0, [])["trigger_id"]
    trl_id = _insert_ticket(env.db, trailing_active=1, current_stop_loss=2900.0, kite_gtt_id=gtt_id,
                            executed_at="2026-09-01 04:00:00", entry_fill_status="filled", **TRAILING)
    oco_id, oco_gtt = _live_position(env)  # TCS too, target_1 3300
    env.kite.ltp_map["NSE:TCS"] = 3400.0
    oco_before = ticket_row(env.db, oco_id)

    _tick()

    assert [c["trigger_id"] for c in env.kite.calls_to("modify_gtt")] == [gtt_id]
    assert ticket_row(env.db, trl_id)["current_stop_loss"] == 3350.0
    assert ticket_row(env.db, oco_id) == oco_before
    assert _event_types(env, oco_id) == []


# ---------------------------------------------------------------- E / F / G

@pytest.mark.parametrize("submission_state", [None, "adopted"], ids=["placed", "adopted_after_unknown"])
def test_entry_awaiting_its_fill_is_not_watched(env, market_open, submission_state):
    buy_id, gtt_id = _live_position(env, entry_fill_status="pending", filled_price=None,
                                    kite_order_id="250000000999", submission_state=submission_state)
    env.kite.trigger_gtt(gtt_id, leg=0, status="COMPLETE")  # a matching COMPLETE SELL now exists

    _tick()

    assert ticket_row(env.db, buy_id)["closed_at"] is None
    assert _exits(env, buy_id) == [] and _closed_trades_for(env, buy_id) == []
    assert env.kite.calls_to("get_gtts") == []
    from backend import trailing_engine
    assert trailing_engine._last_active_count == 0


def test_position_without_a_gtt_is_ignored(env, market_open):
    buy_id, gtt_id = _live_position(env, kite_gtt_id=None)  # e.g. protection_failed at execution
    env.kite.trigger_gtt(gtt_id, leg=0, status="COMPLETE")

    _tick()

    row = ticket_row(env.db, buy_id)
    assert row["closed_at"] is None and _exits(env, buy_id) == []
    assert env.kite.calls_to("get_gtts") == [] and _errors_for(env, buy_id) == []


@pytest.mark.parametrize("state", [
    dict(closed_at="2026-09-02 04:00:00"),
    dict(mode="dry_run"),
    dict(status="cancelled", entry_fill_status="cancelled"),
    dict(status="confirmed", submission_state="unknown", entry_fill_status=None),
    dict(status="confirmed", submission_state="needs_review", entry_fill_status=None),
], ids=["closed", "dry_run", "entry_cancelled", "entry_unknown", "entry_needs_review"])
def test_ineligible_ticket_with_a_historical_gtt_id_is_ignored(env, market_open, state):
    buy_id, gtt_id = _live_position(env, **state)
    before = ticket_row(env.db, buy_id)
    env.kite.trigger_gtt(gtt_id, leg=0, status="COMPLETE")

    _tick()

    assert ticket_row(env.db, buy_id) == before
    assert _exits(env, buy_id) == [] and _closed_trades_for(env, buy_id) == []
    assert env.kite.calls_to("get_gtts") == []


# ---------------------------------------------------------------- H / I (PK-4 ownership)

def test_explicit_exit_confirmed_mid_cycle_is_not_duplicated(env, market_open):
    # The watcher has read the open position; before it looks at the GTT
    # book, the user's exit is approved and placed. PK-8: placing it removes
    # nothing — the GTT stays active, so the watcher has nothing to judge.
    # The order monitor closes the position on the fill and removes the GTT;
    # the watcher must never record a second exit.
    from backend import order_monitor
    from backend.routes import tickets
    set_mode(env.db, "live")
    buy_id, gtt_id = _live_position(env)
    exit_id = _pending_exit(env, buy_id)
    raced = {}

    def approve_exit_now(_kwargs):
        if not raced:
            raced["started"] = True
            raced["result"] = _call(tickets.confirm_ticket, exit_id, kite=env.kite)

    env.kite.on_call("get_gtts", approve_exit_now)
    try:
        _tick()
    finally:
        env.kite.on_call("get_gtts", None)
    _precondition(raced["result"][0] == 200 and raced["result"][1]["status"] == "executed",
                  f"the racing exit should execute: {raced['result']}")
    assert env.kite.gtt_book[gtt_id]["status"] == "active"  # EXITING: still protected
    assert ticket_row(env.db, buy_id)["closed_at"] is None

    _tick()
    order_monitor.check_pending_orders(env.kite)
    _tick()

    assert [e["id"] for e in _exits(env, buy_id)] == [exit_id]
    assert len(_sells(env)) == 1
    [trade] = _closed_trades_for(env, buy_id)
    assert trade["sell_ticket_id"] == exit_id
    assert env.kite.gtt_book[gtt_id]["status"] == "deleted"
    assert _flags(env, buy_id) == [] and _errors_for(env, buy_id) == []


def test_gtt_firing_while_an_unknown_exit_owns_the_position_creates_no_sibling(env, unsafe_client, market_open):
    set_mode(env.db, "live")
    buy_id, gtt_id = _live_position(env)  # non-trailing
    owner = _pending_exit(env, buy_id)
    env.kite.raise_on_next("place_order", requests.ReadTimeout("timed out"))
    _assert_unknown_response(unsafe_client.post(f"/api/tickets/{owner}/confirm"))
    env.kite.trigger_gtt(gtt_id, leg=0, status="COMPLETE")

    _tick()
    _tick()

    assert [e["id"] for e in _exits(env, buy_id)] == [owner]
    assert ticket_row(env.db, owner)["submission_state"] == "unknown"
    assert ticket_row(env.db, buy_id)["closed_at"] is None
    assert _closed_trades_for(env, buy_id) == []
    [flag] = _flags(env, buy_id)
    assert f"exit ticket {owner} owns the position" in flag["actual"]
    [error] = _errors_for(env, buy_id)
    assert "POSSIBLE DOUBLE SELL" in error["message"]
    assert len(env.kite.calls_to("place_order")) == 1  # only the exit attempt itself; nothing re-sent


# ---------------------------------------------------------------- J (per-ticket isolation)

def _three_positions(env):
    """A trailing TCS position that will try to ratchet (processed first),
    and two OCO positions whose stop has filled."""
    a_id, a_gtt = _live_position(env, trailing_active=1, **TRAILING)
    b_id, b_gtt = _infy_position(env)
    c_id, c_gtt = _reliance_position(env)
    env.kite.ltp_map["NSE:TCS"] = 3100.0
    b_fill = env.kite.trigger_gtt(b_gtt, leg=0, status="COMPLETE")
    c_fill = env.kite.trigger_gtt(c_gtt, leg=0, status="COMPLETE")
    return (a_id, a_gtt), (b_id, b_fill), (c_id, c_fill)


@pytest.mark.parametrize("exc", [NetworkException("Gateway timed out"), requests.ConnectionError("Connection aborted.")],
                         ids=["NetworkException", "ConnectionError"])
def test_one_failing_position_does_not_stop_the_others(env, market_open, caplog, exc):
    (a_id, a_gtt), (b_id, b_fill), (c_id, c_fill) = _three_positions(env)
    env.kite.raise_on_next("modify_gtt", exc)

    with caplog.at_level(logging.ERROR, logger="backend.trailing_engine"):
        _tick()

    _precondition(len(env.kite.calls_to("modify_gtt")) == 1, "position A should have tried to ratchet")
    a = ticket_row(env.db, a_id)
    assert a["closed_at"] is None and a["current_stop_loss"] == 2900.0
    assert f"GTT watcher failed for ticket {a_id}" in caplog.text
    _assert_closed_by_fill(env, b_id, b_fill)
    _assert_closed_by_fill(env, c_id, c_fill)


def test_repeated_failure_of_one_position_is_escalated_then_cleared(env, market_open):
    a_id, _ = _live_position(env, trailing_active=1, **TRAILING)
    env.kite.ltp_map["NSE:TCS"] = 3100.0
    for cycle in (1, 2, 3):
        env.kite.raise_on_next("modify_gtt", NetworkException("Gateway timed out"))
        _tick()
        assert len(_errors_for(env, a_id)) == (1 if cycle == 3 else 0)
    assert "failed 3 times in a row" in _errors_for(env, a_id)[0]["message"]

    _tick()  # the broker answers again

    from backend import trailing_engine
    assert ticket_row(env.db, a_id)["current_stop_loss"] == 3050.0
    assert a_id not in trailing_engine._consecutive_errors


def test_ltp_outage_postpones_trailing_but_not_gtt_checks(env, market_open):
    (a_id, a_gtt), (b_id, b_fill), (c_id, c_fill) = _three_positions(env)
    env.kite.raise_on_next("ltp", NetworkException("Gateway timed out"))

    _tick()

    assert env.kite.calls_to("modify_gtt") == []
    assert ticket_row(env.db, a_id)["current_stop_loss"] == 2900.0
    _assert_closed_by_fill(env, b_id, b_fill)
    _assert_closed_by_fill(env, c_id, c_fill)


def test_expired_session_ends_the_cycle_and_keeps_earlier_positions(env, market_open):
    # A (OCO, filled) is processed first; B's ratchet meets an expired
    # session. A stays closed; the cycle stops there, as before PK-6.
    a_id, a_gtt = _live_position(env)
    b_id, _ = _infy_position(env, trailing_active=1, **TRAILING)
    c_id, c_gtt = _reliance_position(env)
    a_fill = env.kite.trigger_gtt(a_gtt, leg=0, status="COMPLETE")
    env.kite.trigger_gtt(c_gtt, leg=0, status="COMPLETE")
    env.kite.ltp_map["NSE:INFY"] = 1600.0
    env.kite.raise_on_next("modify_gtt", TokenException("Session expired"))

    with pytest.raises(TokenException):
        _tick()

    _assert_closed_by_fill(env, a_id, a_fill)
    assert ticket_row(env.db, b_id)["current_stop_loss"] == 1450.0
    assert ticket_row(env.db, c_id)["closed_at"] is None  # picked up by the next cycle
    _tick()
    assert ticket_row(env.db, c_id)["closed_at"] is not None


# ---------------------------------------------------------------- K (event loop)

def test_broker_calls_run_off_the_event_loop(env, market_open):
    from backend import trailing_engine
    _live_position(env, trailing_active=1, **TRAILING)  # ratchets: ltp + modify_gtt
    _, infy_gtt = _infy_position(env)
    env.kite.set_gtt_status(infy_gtt, "deleted")         # closure check: orders()
    env.kite.ltp_map["NSE:TCS"] = 3100.0
    seen = []

    def record(name):
        def hook(_kwargs):
            try:
                asyncio.get_running_loop()
                on_loop = True
            except RuntimeError:
                on_loop = False
            seen.append((name, on_loop, threading.get_ident()))
        return hook

    names = ("get_gtts", "ltp", "orders", "modify_gtt")
    for name in names:
        env.kite.on_call(name, record(name))

    async def main():
        await trailing_engine._tick_once()
        return threading.get_ident()

    try:
        loop_thread = asyncio.run(main())
    finally:
        for name in names:
            env.kite.on_call(name, None)

    assert {name for name, _, _ in seen} == set(names)
    assert [(n, on_loop) for n, on_loop, _ in seen if on_loop] == []
    assert all(thread != loop_thread for _, _, thread in seen)


def test_event_loop_stays_responsive_while_a_cycle_is_blocked(env, market_open):
    # The cycle is held inside get_gtts until a coroutine ON the loop
    # releases it. If the cycle ran on the loop, that coroutine could never
    # run and the hold would time out.
    from backend import trailing_engine
    _live_position(env)
    entered, release = threading.Event(), threading.Event()
    held = {}

    def hold(_kwargs):
        entered.set()
        held["released"] = release.wait(timeout=5)

    env.kite.on_call("get_gtts", hold)

    async def main():
        tick = asyncio.create_task(trailing_engine._tick_once())
        _precondition(await asyncio.to_thread(entered.wait, 5), "the cycle never reached get_gtts")
        release.set()
        await tick

    try:
        asyncio.run(main())
    finally:
        env.kite.on_call("get_gtts", None)
    assert held["released"] is True


# ---------------------------------------------------------------- broker-call budget

def test_one_gtt_book_per_cycle_and_ltp_only_for_trailing(env, market_open):
    from backend import trailing_engine
    _live_position(env)
    _infy_position(env)
    _reliance_position(env)

    _tick()

    assert len(env.kite.calls_to("get_gtts")) == 1
    assert env.kite.calls_to("ltp") == [] and env.kite.calls_to("orders") == []
    assert trailing_engine._last_active_count == 3

    gtt_id = env.kite.place_gtt("single", "HDFCBANK", "NSE", [1500.0], 1600.0, [])["trigger_id"]
    _insert_ticket(env.db, symbol="HDFCBANK", instrument_token=341249, entry_price=1600.0, stop_loss=1500.0,
                   target_1=1800.0, current_stop_loss=1500.0, kite_gtt_id=gtt_id, entry_fill_status="filled",
                   executed_at="2026-09-01 04:00:00", **TRAILING)
    _tick()

    assert len(env.kite.calls_to("get_gtts")) == 2
    assert [c["keys"] for c in env.kite.calls_to("ltp")] == [["NSE:HDFCBANK"]]
    assert trailing_engine._last_active_count == 4
