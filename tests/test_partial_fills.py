"""PK-13 (P1-3): broker partial fills. trade_tickets.quantity is what was
REQUESTED; filled_quantity is what the broker actually filled; the open
quantity (filled, less what exits sold) is what every position operation —
protection, trailing, risk, Emergency Exit, exits, reconciliation, journal —
now uses.

Numbers follow the PK-13 brief (100 requested, 40 filled, 15 sold, ...). No
sleeps; broker outcomes are driven through FakeKite; nothing reaches Zerodha.
"""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from kiteconnect.exceptions import InputException

from conftest import approval_body, account_id, as_pre_pk22_database, displayed_mode, set_mode, ticket_row
from test_baseline_api import _insert_ticket, _manual_ticket
from test_live_safety import (  # noqa: F401 — market_open is a fixture
    _closed_trades_for, _monitor, _precondition, _protective_oco_gtt, _rows, _sells, _urgent_updates, market_open,
)

PRICE, STOP, TARGET = 300.0, 290.0, 330.0


# ---------------------------------------------------------------- helpers

def _tick():
    from backend import trailing_engine
    asyncio.run(trailing_engine._tick_once())


def _reconcile():
    from backend import reconciliation
    reconciliation._run_cycle()


def _conn(env):
    return env.db.get_connection()


def _open(env, position_id):
    from backend import position_quantity
    conn = _conn(env)
    try:
        return position_quantity.open_quantity(conn, position_id)
    finally:
        conn.close()


def _risk(env):
    """(open risk, TCS exposure) as the new-entry limits see them, with
    nothing held at the broker (PK-15: the context's broker map replaced
    holdings_by_symbol; the app's own position still counts)."""
    from backend import risk_engine
    conn = _conn(env)
    try:
        ctx = SimpleNamespace(broker_by_symbol={})
        return (risk_engine._open_risk_sum(conn, account_id(env.db)),
                risk_engine._symbol_exposure(conn, account_id(env.db), "TCS", ctx))
    finally:
        conn.close()


def _open_positions(env):
    from backend import risk_engine
    conn = _conn(env)
    try:
        return {p["id"]: p["open_quantity"] for p in risk_engine.list_open_positions(conn, account_id(env.db))}
    finally:
        conn.close()


def _legs(env, gtt_id) -> set[int]:
    return {leg["quantity"] for leg in env.kite.gtt_book[gtt_id]["orders"]}


def _modifies(env):
    return env.kite.calls_to("modify_gtt")


def _messages(env, needle):
    return [b["message"] for b in env.broadcasts if needle in (b.get("message") or "")]


def _emergency_exit(env):
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    assert r.status_code == 200, r.text
    return r.json()["data"]["results"]


def _place_entry(env, quantity=100, trailing=False) -> tuple[int, str, int]:
    """A live BUY taken through the real create -> confirm path, accepted by
    Kite but not filled: (ticket id, order id, GTT id). The GTT is placed
    with the order, for the requested quantity."""
    set_mode(env.db, "live")
    env.kite.ltp_map["NSE:TCS"] = PRICE
    env.kite.auto_fill = False
    body = _manual_ticket(quantity=quantity, entry_price=PRICE, stop_loss=STOP, target_1=TARGET)
    if trailing:
        body.update(trailing_enabled=True, trailing_trigger="after_target_1", trailing_distance=5.0)
    t = env.client.post("/api/tickets", json=body).json()["data"]
    _precondition(t["status"] == "pending_confirmation", f"ticket not approvable: {t['status']} {t}")
    done = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t['id'])).json()["data"]
    _precondition(done["status"] == "executed" and done["kite_gtt_id"], f"entry not placed: {done}")
    _precondition(_legs(env, done["kite_gtt_id"]) == {quantity}, "GTT should be placed for the order")
    return t["id"], done["kite_order_id"], done["kite_gtt_id"]


def _fill(env, order_id, filled, status="OPEN", price=PRICE):
    env.kite.set_order_state(order_id, status, filled_quantity=filled, average_price=price)


def _held_position(env, quantity=40, gtt_quantity=None, **overrides) -> tuple[int, int]:
    """A filled live BUY `quantity` TCS @ 3000, protected by an OCO GTT."""
    gtt_id = _protective_oco_gtt(env, quantity=gtt_quantity or quantity)
    fields = dict(source="strategy", strategy_name="momentum_breakout", kite_gtt_id=gtt_id, quantity=quantity,
                  executed_at="2026-09-01 04:00:00", entry_fill_status="filled", filled_price=3000.0,
                  filled_quantity=quantity, current_stop_loss=2900.0, risk_amount=100.0 * quantity)
    fields.update(overrides)
    buy_id = _insert_ticket(env.db, **fields)
    env.kite.set_holding("TCS", quantity, average_price=3000.0)
    return buy_id, gtt_id


def _exit_ticket(env, buy_id, quantity, **overrides) -> int:
    fields = dict(source="strategy", strategy_name="momentum_breakout_exit", transaction_type="SELL",
                  quantity=quantity, entry_order_type="MARKET", entry_price=3100.0, status="pending_confirmation",
                  mode=None, closes_ticket_id=buy_id, rule_checklist=json.dumps({"time_exit": {"pass": True}}))
    fields.update(overrides)
    return _insert_ticket(env.db, **fields)


def _place_exit(env, buy_id, quantity) -> tuple[int, str]:
    set_mode(env.db, "live")
    env.kite.auto_fill = False
    sell_id = _exit_ticket(env, buy_id, quantity)
    r = env.client.post(f"/api/tickets/{sell_id}/confirm")
    _precondition(r.status_code == 200 and r.json()["data"]["status"] == "executed", f"exit not placed: {r.text}")
    return sell_id, r.json()["data"]["kite_order_id"]


def _partial_cancelled_entry(env, trailing=False) -> tuple[int, str, int]:
    """Requested 100, filled 40, remainder CANCELLED."""
    buy_id, order_id, gtt_id = _place_entry(env, 100, trailing=trailing)
    _fill(env, order_id, 40, status="CANCELLED")
    _monitor(env)
    env.kite.set_holding("TCS", 40, average_price=PRICE)
    return buy_id, order_id, gtt_id


# ======================================================================= ENTRY

def test_1_entry_open_with_40_of_100_filled_is_a_40_share_position(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env)
    _fill(env, order_id, 40)
    _monitor(env)

    row = ticket_row(env.db, buy_id)
    assert (row["status"], row["entry_fill_status"], row["filled_quantity"], row["quantity"]) == (
        "executed", "pending", 40, 100)
    assert _open(env, buy_id) == 40
    assert _legs(env, gtt_id) == {40}  # protects the 40 held, not the 100 ordered
    [alert] = _messages(env, "PARTIAL FILL")
    assert "40/100" in alert and "resized from 100 to 40" in alert

    _monitor(env)  # the same fill seen again: nothing new
    assert len(_modifies(env)) == 1 and len(_messages(env, "PARTIAL FILL")) == 1


def test_2_3_entry_partial_then_cancelled_stays_an_open_40_share_position(env, market_open):
    buy_id, order_id, gtt_id = _partial_cancelled_entry(env)

    row = ticket_row(env.db, buy_id)
    assert (row["status"], row["entry_fill_status"], row["filled_quantity"]) == ("executed", "partial", 40)
    assert row["closed_at"] is None and row["kite_gtt_id"] == gtt_id
    assert env.kite.gtt_book[gtt_id]["status"] == "active" and _legs(env, gtt_id) == {40}
    assert _open_positions(env) == {buy_id: 40}
    assert not env.kite.calls_to("delete_gtt")
    listed = {t["id"]: t for t in env.client.get("/api/tickets").json()["data"]}
    assert (listed[buy_id]["quantity"], listed[buy_id]["filled_quantity"], listed[buy_id]["open_quantity"]) == (
        100, 40, 40)

    for _ in range(2):  # the watcher and the monitor leave it as it is
        _monitor(env)
        _tick()
    assert _legs(env, gtt_id) == {40} and len(_modifies(env)) == 1


def test_4_risk_counts_requested_while_the_entry_works_and_filled_once_it_ended(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env)
    _fill(env, order_id, 40)
    _monitor(env)
    # Still working: the other 60 can fill at any moment (user decision).
    assert _risk(env) == (pytest.approx(1000.0), pytest.approx(100 * PRICE))

    _fill(env, order_id, 40, status="CANCELLED")
    _monitor(env)
    assert _risk(env) == (pytest.approx(400.0), pytest.approx(40 * PRICE))


def test_5_emergency_exit_sells_the_40_held_not_the_100_ordered(env, market_open):
    buy_id, _, _ = _partial_cancelled_entry(env)
    [result] = _emergency_exit(env)
    assert result["status"] == "exit_submitted", result
    assert [s["quantity"] for s in _sells(env)] == [40]


def test_5b_emergency_exit_on_a_working_partial_entry_cancels_the_rest_and_sells_the_fill(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env)
    _fill(env, order_id, 25)
    _monitor(env)
    _fill(env, order_id, 40)  # more filled since the monitor last looked

    [result] = _emergency_exit(env)
    assert result["status"] == "exit_submitted", result
    assert [c["order_id"] for c in env.kite.calls_to("cancel_order")] == [order_id]
    assert [s["quantity"] for s in _sells(env)] == [40]  # re-read from Kite: 40, never 100
    assert ticket_row(env.db, buy_id)["filled_quantity"] == 40


def test_6_7_trailing_ratchets_the_gtt_for_the_40_held(env, market_open):
    buy_id, _, gtt_id = _partial_cancelled_entry(env, trailing=True)
    _precondition(_legs(env, gtt_id) == {40}, "GTT resized to the fill")
    conn = _conn(env)
    try:
        conn.execute("UPDATE trade_tickets SET trailing_active = 1 WHERE id = ?", (buy_id,))
        conn.commit()
    finally:
        conn.close()
    env.kite.ltp_map["NSE:TCS"] = 340.0
    _tick()

    ratchet = _modifies(env)[-1]
    assert ratchet["trigger_values"] == [335.0]
    assert [o["quantity"] for o in ratchet["orders"]] == [40]
    assert _legs(env, gtt_id) == {40}


def test_8_journal_of_the_partial_entry_uses_the_40_filled(env, market_open):
    buy_id, _, gtt_id = _partial_cancelled_entry(env)
    sell_id, exit_order = _place_exit(env, buy_id, 40)
    _fill(env, exit_order, 40, status="COMPLETE", price=320.0)
    _monitor(env)

    assert ticket_row(env.db, buy_id)["closed_at"] is not None
    [trade] = _closed_trades_for(env, buy_id)
    assert trade["quantity"] == 40 and trade["sell_ticket_id"] == sell_id
    assert trade["realized_pnl"] == pytest.approx((320.0 - PRICE) * 40)
    assert trade["risk_amount"] == pytest.approx(400.0)  # risk of the 40 bought, not the 100 ordered
    assert env.kite.gtt_book[gtt_id]["status"] == "deleted"


# ================================================================ PROGRESSIVE

def test_9_10_progressive_fills_grow_the_protection_step_by_step(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env)
    for filled in (10, 25, 40):
        _fill(env, order_id, filled)
        _monitor(env)
        assert ticket_row(env.db, buy_id)["filled_quantity"] == filled
        assert _legs(env, gtt_id) == {filled}
        assert _open(env, buy_id) == filled
    assert [m["orders"][0]["quantity"] for m in _modifies(env)] == [10, 25, 40]
    assert len(_messages(env, "PARTIAL FILL")) == 3

    _fill(env, order_id, 100, status="COMPLETE")  # the rest fills
    _monitor(env)
    row = ticket_row(env.db, buy_id)
    assert (row["entry_fill_status"], row["filled_quantity"]) == ("filled", 100)
    assert _legs(env, gtt_id) == {100}


# ================================================================ PARTIAL EXIT

def test_11_to_16_partial_exit_leaves_25_open_and_protected(env, market_open):
    buy_id, gtt_id = _held_position(env, 40)
    sell_id, exit_order = _place_exit(env, buy_id, 40)
    _fill(env, exit_order, 15, price=3100.0)
    _monitor(env)

    exit_row = ticket_row(env.db, sell_id)
    assert (exit_row["entry_fill_status"], exit_row["filled_quantity"]) == ("pending", 15)
    assert _open(env, buy_id) == 25
    assert ticket_row(env.db, buy_id)["closed_at"] is None  # 12
    assert not _closed_trades_for(env, buy_id)  # 13
    assert _risk(env)[0] == pytest.approx(2500.0)  # 14: 4000 risked on 40, 25 still held
    assert _legs(env, gtt_id) == {25}  # 16
    [alert] = [u for u in _urgent_updates(env) if "PARTIAL EXIT" in u["message"]]
    assert "15/40 filled" in alert["message"] and "25 share(s)" in alert["message"]
    # The exit is still working, so it still owns the position.
    [owning] = _emergency_exit(env)
    assert owning["status"] == "already_exiting"

    _fill(env, exit_order, 15, status="CANCELLED", price=3100.0)  # the rest of the exit order ends
    _monitor(env)
    exit_row = ticket_row(env.db, sell_id)
    assert (exit_row["status"], exit_row["entry_fill_status"], exit_row["filled_quantity"]) == (
        "executed", "partial", 15)
    assert ticket_row(env.db, buy_id)["closed_at"] is None and not _closed_trades_for(env, buy_id)
    listed = {t["id"]: t for t in env.client.get("/api/tickets").json()["data"]}
    assert listed[sell_id]["exit_state"] == "exit_partial" and listed[sell_id]["remaining_quantity"] == 25
    assert listed[buy_id]["open_quantity"] == 25 and listed[buy_id]["exit_in_flight_ticket_id"] is None

    [result] = _emergency_exit(env)  # 15: the remainder, once the partial exit has ended
    assert result["status"] == "exit_submitted", result
    assert [s["quantity"] for s in _sells(env)] == [40, 25]


def test_partial_exits_journal_one_round_trip_at_the_weighted_exit_price(env, market_open):
    buy_id, gtt_id = _held_position(env, 40)
    first_id, first_order = _place_exit(env, buy_id, 40)
    _fill(env, first_order, 15, status="CANCELLED", price=3100.0)
    _monitor(env)
    second_id, second_order = _place_exit(env, buy_id, 25)
    _fill(env, second_order, 25, status="COMPLETE", price=3200.0)
    _monitor(env)

    assert ticket_row(env.db, buy_id)["closed_at"] is not None
    [trade] = _closed_trades_for(env, buy_id)
    assert (trade["quantity"], trade["sell_ticket_id"]) == (40, second_id)
    assert trade["exit_price"] == pytest.approx((15 * 3100.0 + 25 * 3200.0) / 40)
    assert trade["realized_pnl"] == pytest.approx(15 * 100.0 + 25 * 200.0)
    assert env.kite.gtt_book[gtt_id]["status"] == "deleted"


def test_an_exit_larger_than_the_open_quantity_is_refused(env, market_open):
    buy_id, _ = _held_position(env, 100, gtt_quantity=40, filled_quantity=40, entry_fill_status="partial")
    set_mode(env.db, "live")
    oversized = _exit_ticket(env, buy_id, 100)
    r = env.client.post(f"/api/tickets/{oversized}/confirm")
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "EXIT_QUANTITY_MISMATCH"
    assert not _sells(env)


# ======================================================== PROTECTIVE GTT PARTIAL

def test_17_to_19_gtt_sell_partially_filled_leaves_60_open(env, market_open):
    buy_id, gtt_id = _held_position(env, 100)
    order_id = env.kite.trigger_gtt(gtt_id, leg=0, status="OPEN", filled_quantity=40)
    _tick()

    assert ticket_row(env.db, buy_id)["closed_at"] is None  # 18
    assert not _closed_trades_for(env, buy_id)
    [synthetic] = _rows(env, "SELECT * FROM trade_tickets WHERE closes_ticket_id = ?", buy_id)
    assert (synthetic["entry_fill_status"], synthetic["filled_quantity"], synthetic["quantity"]) == (
        "partial", 40, 100)
    assert _open(env, buy_id) == 60 and _open_positions(env) == {buy_id: 60}  # 19
    assert _risk(env)[0] == pytest.approx(6000.0)
    assert ticket_row(env.db, buy_id)["kite_gtt_id"] == gtt_id  # the order still works: still watched

    env.kite.set_order_state(order_id, "CANCELLED", filled_quantity=40)
    _tick()
    assert ticket_row(env.db, buy_id)["kite_gtt_id"] is None
    [alert] = _messages(env, "UNPROTECTED: the exit order has ended")
    assert "60 share(s) are still held" in alert
    [result] = _emergency_exit(env)
    assert result["status"] == "exit_submitted" and [s["quantity"] for s in _sells(env)] == [60]


def test_gtt_partial_then_complete_closes_once(env, market_open):
    buy_id, gtt_id = _held_position(env, 100)
    order_id = env.kite.trigger_gtt(gtt_id, leg=0, status="OPEN", filled_quantity=40)
    _tick()
    env.kite.set_order_state(order_id, "COMPLETE", filled_quantity=100)
    _tick()
    _tick()

    assert ticket_row(env.db, buy_id)["closed_at"] is not None
    [synthetic] = _rows(env, "SELECT * FROM trade_tickets WHERE closes_ticket_id = ?", buy_id)
    assert (synthetic["entry_fill_status"], synthetic["filled_quantity"]) == ("filled", 100)
    [trade] = _closed_trades_for(env, buy_id)
    assert trade["quantity"] == 100 and trade["sell_ticket_id"] == synthetic["id"]


# ===================================================================== FAILURE

def test_20_gtt_resize_failure_is_an_urgent_mismatch_and_heals_later(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env)
    env.kite.raise_on_next("modify_gtt", InputException("GTT modification rejected"))
    _fill(env, order_id, 40, status="CANCELLED")
    _monitor(env)

    assert ticket_row(env.db, buy_id)["closed_at"] is None and _open(env, buy_id) == 40
    assert _legs(env, gtt_id) == {100}
    [alert] = _messages(env, "PROTECTION QUANTITY MISMATCH")
    assert "FAILED" in alert
    assert [u for u in _urgent_updates(env) if u["ticket_id"] == buy_id]
    assert _rows(env, "SELECT * FROM reconciliation_log WHERE ticket_id = ? AND actual LIKE '%resize to 40 failed%'",
                 buy_id)

    _reconcile()  # an independent check sees the oversized GTT too
    assert _rows(env, "SELECT * FROM reconciliation_log WHERE ticket_id = ? AND detail LIKE '%OVERSIZED%'", buy_id)

    _tick()  # the watcher's convergence pass resizes it
    assert _legs(env, gtt_id) == {40}


def test_21_broker_fill_above_the_order_is_never_recorded(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env)
    _fill(env, order_id, 120)
    _monitor(env)

    row = ticket_row(env.db, buy_id)
    assert (row["filled_quantity"], row["entry_fill_status"]) == (0, "pending")
    assert not _modifies(env) and _legs(env, gtt_id) == {100}
    [alert] = [u for u in _urgent_updates(env) if "BROKER FILL NEEDS REVIEW" in u["message"]]
    assert "120" in alert["message"]


def test_22_more_sold_than_bought_is_never_closed_or_sold_again(env, market_open):
    buy_id, gtt_id = _held_position(env, 40)
    _exit_ticket(env, buy_id, 50, status="executed", mode="live", entry_fill_status="partial", filled_quantity=50,
                 filled_price=3100.0)
    assert _open(env, buy_id) == -10

    [result] = _emergency_exit(env)
    assert (result["status"], result["error_code"]) == ("refused", "NOTHING_HELD")
    assert not _sells(env)
    from backend import position_quantity
    conn = _conn(env)
    try:
        closed = position_quantity.close_if_flat(conn, env.kite, buy_id, buy_id, "2026-09-25 04:00:00")
    finally:
        conn.close()
    assert not closed.closed and "NEEDS REVIEW" in closed.message
    assert ticket_row(env.db, buy_id)["closed_at"] is None and not _closed_trades_for(env, buy_id)


# ========================================================= FULL FILL REGRESSION

def test_23_full_entry_fill_changes_nothing_about_protection(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env)
    gtt_reads = len(env.kite.calls_to("get_gtts"))
    _fill(env, order_id, 100, status="COMPLETE")
    _monitor(env)

    row = ticket_row(env.db, buy_id)
    assert (row["entry_fill_status"], row["filled_quantity"]) == ("filled", 100)
    assert not _modifies(env) and len(env.kite.calls_to("get_gtts")) == gtt_reads
    assert _legs(env, gtt_id) == {100} and _open(env, buy_id) == 100


def test_24_26_full_explicit_exit_closes_and_journals_the_whole_position(env, market_open):
    buy_id, gtt_id = _held_position(env, 40)
    sell_id, exit_order = _place_exit(env, buy_id, 40)
    _fill(env, exit_order, 40, status="COMPLETE", price=3150.0)
    _monitor(env)

    assert ticket_row(env.db, buy_id)["closed_at"] is not None
    assert ticket_row(env.db, sell_id)["filled_quantity"] == 40
    [trade] = _closed_trades_for(env, buy_id)
    assert (trade["quantity"], trade["exit_price"]) == (40, 3150.0)
    assert not _modifies(env) and len(env.kite.calls_to("delete_gtt")) == 1


def test_25_complete_gtt_close_still_works(env, market_open):
    buy_id, gtt_id = _held_position(env, 40)
    env.kite.trigger_gtt(gtt_id, leg=0, status="COMPLETE")
    _tick()

    assert ticket_row(env.db, buy_id)["closed_at"] is not None
    [trade] = _closed_trades_for(env, buy_id)
    assert trade["quantity"] == 40
    assert not _modifies(env)


def test_dry_run_entry_fills_only_through_the_paper_broker(env, market_open):
    # PK-20 (was: "filled in full at once"): approval places a paper order
    # that has filled nothing; the paper exchange fills it at an observed
    # price, and only then is the position its filled quantity.
    set_mode(env.db, "dry_run")
    t = env.client.post("/api/tickets", json=_manual_ticket(entry_order_type="MARKET")).json()["data"]
    done = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t['id'])).json()["data"]
    assert (done["status"], done["filled_quantity"], done["entry_fill_status"]) == ("executed", 0, "pending")

    from backend import order_monitor
    order_monitor.check_paper_orders(env.kite)
    listed = {x["id"]: x for x in env.client.get("/api/tickets").json()["data"]}
    assert (listed[t["id"]]["filled_quantity"], listed[t["id"]]["open_quantity"]) == (5, 5)


# ===================================================================== RESTART

def test_27_partial_state_survives_restart(env, market_open):
    buy_id, _, gtt_id = _partial_cancelled_entry(env)
    sell_id, exit_order = _place_exit(env, buy_id, 40)
    _fill(env, exit_order, 15, status="CANCELLED", price=320.0)
    _monitor(env)
    _precondition(_open(env, buy_id) == 25, "25 should remain")

    env.db.init_db()
    env.db.init_db()

    assert (ticket_row(env.db, buy_id)["filled_quantity"], ticket_row(env.db, sell_id)["filled_quantity"]) == (40, 15)
    assert _open(env, buy_id) == 25 and _legs(env, gtt_id) == {25}
    modifies = len(_modifies(env))
    _monitor(env)
    _tick()
    assert len(_modifies(env)) == modifies and not _closed_trades_for(env, buy_id)
    [result] = _emergency_exit(env)
    assert result["status"] == "exit_submitted" and _sells(env)[-1]["quantity"] == 25


# ================================================================= CONCURRENCY

def test_cumulative_fill_is_monotonic_across_racing_writers(env):
    from backend import position_quantity
    buy_id, _ = _held_position(env, 100, filled_quantity=0, entry_fill_status="pending")
    for order in ((25, 40), (40, 25)):
        conn = _conn(env)
        conn.execute("UPDATE trade_tickets SET filled_quantity = 0 WHERE id = ?", (buy_id,))
        conn.commit()
        conn.close()
        barrier = threading.Barrier(2)
        outcomes = []

        def write(value):
            c = _conn(env)
            try:
                row = c.execute("SELECT * FROM trade_tickets WHERE id = ?", (buy_id,)).fetchone()
                barrier.wait()
                outcomes.append(position_quantity.record_fill(c, row, value))
                c.commit()
            finally:
                c.close()

        threads = [threading.Thread(target=write, args=(v,)) for v in order]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert ticket_row(env.db, buy_id)["filled_quantity"] == 40, order


def test_a_stale_lower_broker_report_never_shrinks_the_fill(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env)
    _fill(env, order_id, 40)
    _monitor(env)
    _fill(env, order_id, 25)  # an older / lagging report
    _monitor(env)
    assert ticket_row(env.db, buy_id)["filled_quantity"] == 40
    assert _legs(env, gtt_id) == {40} and len(_modifies(env)) == 1


def test_trailing_ratchet_racing_a_fill_resize_never_writes_the_stale_quantity(env, market_open):
    from backend import position_quantity
    buy_id, _, gtt_id = _partial_cancelled_entry(env, trailing=True)
    conn = _conn(env)
    try:
        conn.execute("UPDATE trade_tickets SET trailing_active = 1 WHERE id = ?", (buy_id,))
        conn.commit()
    finally:
        conn.close()
    env.kite.ltp_map["NSE:TCS"] = 340.0

    def exit_fills_mid_cycle(_kwargs):
        # After the watcher read the position (40 held) and before it
        # ratchets: an exit sells 15 and the resize to 25 lands.
        env.kite.on_call("ltp", None)
        c = _conn(env)
        try:
            _exit_ticket(env, buy_id, 15, status="executed", mode="live", entry_fill_status="partial",
                         filled_quantity=15, filled_price=320.0)
            position_quantity.sync_protection(c, env.kite, buy_id, "racing exit fill")
            c.commit()
        finally:
            c.close()

    env.kite.on_call("ltp", exit_fills_mid_cycle)
    _tick()

    ratchet = _modifies(env)[-1]
    assert ratchet["trigger_values"] == [335.0] and [o["quantity"] for o in ratchet["orders"]] == [25]
    assert _legs(env, gtt_id) == {25}


def test_emergency_exit_racing_a_fill_update_is_refused_atomically(env, market_open, monkeypatch):
    from backend import risk_engine
    buy_id, gtt_id = _held_position(env, 40)
    stale = _exit_ticket(env, buy_id, 40)
    set_mode(env.db, "live")
    original = risk_engine.linked_exit_problem

    def check_then_fill(conn, ticket_row_):
        problem = original(conn, ticket_row_)
        # Between the check and the claim, the GTT sells 15 of the 40.
        _exit_ticket(env, buy_id, 15, status="executed", mode="live", entry_fill_status="partial",
                     filled_quantity=15, filled_price=3000.0)
        return problem

    monkeypatch.setattr(risk_engine, "linked_exit_problem", check_then_fill)
    r = env.client.post(f"/api/tickets/{stale}/confirm")
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "EXIT_QUANTITY_MISMATCH"
    assert not _sells(env) and ticket_row(env.db, stale)["status"] != "executed"


# =================================================================== MIGRATION

def test_migration_backfills_only_certain_fills_and_is_idempotent(env, monkeypatch):
    from backend import db
    conn = _conn(env)
    try:
        rows = {
            "dry": _insert_ticket(env.db, mode="dry_run", entry_fill_status="filled"),
            "legacy": _insert_ticket(env.db, entry_fill_status=None),
            "filled": _insert_ticket(env.db, entry_fill_status="filled"),
            "pending": _insert_ticket(env.db, entry_fill_status="pending"),
            "partial": _insert_ticket(env.db, entry_fill_status="partial"),
            "unconfirmed": _insert_ticket(env.db, entry_fill_status="unconfirmed"),
            "draft": _insert_ticket(env.db, status="pending_confirmation", mode=None),
        }
        # An old database: the rows exist, the column does not.
        conn.execute("ALTER TABLE trade_tickets DROP COLUMN filled_quantity")
        conn.commit()
    finally:
        conn.close()

    as_pre_pk22_database(env.db)  # PK-22: the legacy migration runs once per database
    db.init_db()
    after_first = {k: ticket_row(env.db, v)["filled_quantity"] for k, v in rows.items()}
    assert after_first == {"dry": 5, "legacy": 5, "filled": 5, "pending": 0, "partial": None,
                           "unconfirmed": None, "draft": None}
    db.init_db()
    assert {k: ticket_row(env.db, v)["filled_quantity"] for k, v in rows.items()} == after_first
    conn = _conn(env)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        with pytest.raises(Exception):  # the storage invariant: never above the order
            conn.execute("UPDATE trade_tickets SET filled_quantity = 6 WHERE id = ?", (rows["filled"],))
    finally:
        conn.close()
