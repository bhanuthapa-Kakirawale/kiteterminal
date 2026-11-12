"""PK-8 (P0-7): an explicit exit's lifecycle. Placing an exit order is not
exiting — until Kite reports the full quantity filled, the position stays
open, keeps its protective GTT and is not journaled. order_monitor is the
only thing that closes a live explicit exit's position.

Letters follow the PK-8 brief (A-N). No sleeps; broker outcomes are driven
through FakeKite; nothing reaches Zerodha or the network.
"""

import asyncio

import pytest
import requests
from kiteconnect.exceptions import NetworkException

from conftest import approval_body, displayed_mode, account_id, set_mode, ticket_row
from test_live_safety import (  # noqa: F401 — market_open / market_closed / unsafe_client are fixtures
    _assert_unknown_response, _closed_trades_for, _details, _event_types, _live_position, _monitor, _pending_exit,
    _precondition, _rows, _sells, _urgent_updates, market_closed, market_open, unsafe_client,
)


def _tick():
    from backend import trailing_engine
    asyncio.run(trailing_engine._tick_once())


def _exiting_position(env, **exit_overrides) -> tuple[int, int, int, str]:
    """A live protected position whose exit Kite accepted but has not
    filled: (position id, GTT id, exit ticket id, exit order id)."""
    set_mode(env.db, "live")
    buy_id, gtt_id = _live_position(env)
    sell_id = _pending_exit(env, buy_id, **exit_overrides)
    env.kite.auto_fill = False
    r = env.client.post(f"/api/tickets/{sell_id}/confirm")
    _precondition(r.status_code == 200 and r.json()["data"]["status"] == "executed", f"exit not placed: {r.text}")
    return buy_id, gtt_id, sell_id, r.json()["data"]["kite_order_id"]


def _fill(env, order_id, price=3120.5, filled=5, when_ist="2026-09-25 10:15:00", status="COMPLETE"):
    env.kite.order_book[order_id]["exchange_timestamp"] = when_ist
    env.kite.set_order_state(order_id, status, filled_quantity=filled, average_price=price)


def _still_open_and_protected(env, buy_id, gtt_id) -> list[str]:
    """Every way the position could have been treated as closed."""
    violations = []
    buy = ticket_row(env.db, buy_id)
    if buy["closed_at"] is not None:
        violations.append(f"closed_at={buy['closed_at']}")
    if buy["kite_gtt_id"] != gtt_id:
        violations.append(f"kite_gtt_id={buy['kite_gtt_id']} (was {gtt_id})")
    if env.kite.gtt_book[gtt_id]["status"] != "active":
        violations.append(f"GTT {gtt_id} is {env.kite.gtt_book[gtt_id]['status']}")
    if env.kite.calls_to("delete_gtt"):
        violations.append("delete_gtt was called")
    if _closed_trades_for(env, buy_id):
        violations.append(f"journal rows: {_closed_trades_for(env, buy_id)}")
    if "closed" in _event_types(env, buy_id):
        violations.append("a 'closed' event was logged")
    return violations


def _open_positions(env):
    from backend import risk_engine
    conn = env.db.get_connection()
    try:
        return ([p["id"] for p in risk_engine.list_open_positions(conn, account_id(env.db))],
                risk_engine._open_position_count(conn, account_id(env.db)),
                risk_engine._open_risk_sum(conn, account_id(env.db)))
    finally:
        conn.close()


# ---------------------------------------------------------------- A / B: accepted, not filled

def test_a_open_limit_exit_is_exiting_not_closed(env, market_open):
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env, entry_order_type="LIMIT")
    for _ in range(3):
        _monitor(env)
        _tick()

    assert env.kite.order_book[order_id]["status"] == "OPEN"
    assert not _still_open_and_protected(env, buy_id, gtt_id)
    exit_row = ticket_row(env.db, sell_id)
    assert (exit_row["status"], exit_row["entry_fill_status"], exit_row["submission_state"]) == (
        "executed", "pending", "acknowledged")
    assert exit_row["filled_price"] is None and exit_row["filled_at"] is None
    # The UI sees EXITING on the exit and "exit in flight" on the position.
    listed = {t["id"]: t for t in env.client.get("/api/tickets").json()["data"]}
    assert listed[sell_id]["exit_state"] == "exiting"
    assert listed[buy_id]["exit_in_flight_ticket_id"] == sell_id and listed[buy_id]["closed_at"] is None
    assert "EXITING" in _details(env, sell_id)[-1]
    # Still an open position for every risk figure.
    assert _open_positions(env) == ([buy_id], 1, 500.0)


def test_b_amo_exit_waits_overnight_and_closes_on_next_day_fill(env, market_closed, monkeypatch):
    from backend import order_monitor
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env)
    _precondition(env.kite.order_book[order_id]["status"] == "AMO REQ RECEIVED", "the exit should be an AMO")
    _precondition(env.kite.order_book[order_id]["variety"] == "amo", "the exit should be an AMO")

    for now in ("2026-09-24 16:00:00", "2026-09-24 22:00:00", "2026-09-25 02:00:00"):  # the evening, the night
        monkeypatch.setattr(order_monitor, "_utc_now", lambda now=now: order_monitor.datetime.strptime(
            now, "%Y-%m-%d %H:%M:%S").replace(tzinfo=order_monitor.timezone.utc))
        _monitor(env)
        _tick()
        assert not _still_open_and_protected(env, buy_id, gtt_id)
        assert ticket_row(env.db, sell_id)["entry_fill_status"] == "pending"

    _fill(env, order_id, price=3090.0, when_ist="2026-09-25 09:15:07")  # the market opens
    _monitor(env)

    assert ticket_row(env.db, buy_id)["closed_at"] == "2026-09-25 03:45:07"
    [trade] = _closed_trades_for(env, buy_id)
    assert (trade["exit_price"], trade["exit_date"]) == (3090.0, "2026-09-25")


# ---------------------------------------------------------------- C / M: COMPLETE

def test_c_m_complete_closes_once_at_the_broker_fill(env, market_open):
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env)
    _fill(env, order_id, price=3120.5, when_ist="2026-09-25 10:15:00")

    for _ in range(4):  # M: repeated cycles see the same COMPLETE order
        _monitor(env)
        _tick()

    buy, exit_row = ticket_row(env.db, buy_id), ticket_row(env.db, sell_id)
    assert buy["closed_at"] == "2026-09-25 04:45:00"  # the fill time, IST -> UTC
    assert (exit_row["entry_fill_status"], exit_row["filled_price"], exit_row["filled_at"]) == (
        "filled", 3120.5, "2026-09-25 04:45:00")
    [trade] = _closed_trades_for(env, buy_id)
    assert trade["sell_ticket_id"] == sell_id
    assert (trade["exit_price"], trade["exit_date"]) == (3120.5, "2026-09-25")
    assert trade["realized_pnl"] == pytest.approx((3120.5 - 3000.0) * 5)
    assert env.kite.gtt_book[gtt_id]["status"] == "deleted" and buy["kite_gtt_id"] is None
    assert len(env.kite.calls_to("delete_gtt")) == 1
    assert _event_types(env, buy_id).count("closed") == 1
    assert len(_sells(env)) == 1
    assert _open_positions(env) == ([], 0, 0)
    listed = {t["id"]: t for t in env.client.get("/api/tickets").json()["data"]}
    assert listed[sell_id]["exit_state"] == "exited" and listed[buy_id]["exit_in_flight_ticket_id"] is None


def test_complete_without_a_usable_fill_price_is_never_journaled_at_the_reference_price(env, market_open):
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env)
    env.kite.set_order_state(order_id, "COMPLETE", filled_quantity=5, average_price=0)
    _monitor(env)
    _monitor(env)

    assert not _still_open_and_protected(env, buy_id, gtt_id)
    assert ticket_row(env.db, sell_id)["entry_fill_status"] == "pending"
    assert len([u for u in _urgent_updates(env) if "COMPLETE but reports" in u["message"]]) == 1

    _fill(env, order_id, price=3105.0)  # the broker fills in the price
    _monitor(env)
    assert [t["exit_price"] for t in _closed_trades_for(env, buy_id)] == [3105.0]


# ---------------------------------------------------------------- D / E: nothing sold

@pytest.mark.parametrize("outcome", ["REJECTED", "CANCELLED"])
def test_d_e_rejected_or_cancelled_exit_leaves_the_position_as_it_was(env, market_open, outcome):
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env)
    env.kite.set_order_state(order_id, outcome, status_message="RMS: blocked")
    _monitor(env)
    _tick()

    assert not _still_open_and_protected(env, buy_id, gtt_id)
    exit_row = ticket_row(env.db, sell_id)
    assert (exit_row["status"], exit_row["entry_fill_status"]) == ("failed", outcome.lower())
    [alert] = [u for u in _urgent_updates(env) if u["ticket_id"] == sell_id]
    assert "still OPEN" in alert["message"] and f"GTT {gtt_id} is still active" in alert["message"]
    assert _open_positions(env)[0] == [buy_id]
    listed = {t["id"]: t for t in env.client.get("/api/tickets").json()["data"]}
    assert listed[buy_id]["exit_in_flight_ticket_id"] is None


# ---------------------------------------------------------------- F: partial

def test_f_partial_fill_in_progress_is_flagged_not_closed(env, market_open):
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env)
    env.kite.set_order_state(order_id, "OPEN", filled_quantity=2, average_price=3100.0)
    for _ in range(3):
        _monitor(env)
        _tick()

    assert not _still_open_and_protected(env, buy_id, gtt_id)
    assert ticket_row(env.db, sell_id)["entry_fill_status"] == "pending"
    alerts = [u for u in _urgent_updates(env) if "PARTIAL EXIT" in u["message"]]
    assert len(alerts) == 1 and "2/5 filled" in alerts[0]["message"]

    env.kite.set_order_state(order_id, "OPEN", filled_quantity=4, average_price=3100.0)  # more fills: alert again
    _monitor(env)
    assert len([u for u in _urgent_updates(env) if "PARTIAL EXIT" in u["message"]]) == 2


def test_f_partial_fill_then_ended_leaves_the_rest_open_protected_and_exitable(env, market_open):
    # PK-13 restates PK-8's interim rule ("the partial exit keeps the
    # position, needs review, GTT still covers all 5"): the 2 sold are
    # recorded, the other 3 stay open with the GTT resized to them, and the
    # ended exit no longer owns the position, so the remainder can be sold.
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env)
    sibling = _pending_exit(env, buy_id)  # sized for the full 5 before the partial
    env.kite.set_order_state(order_id, "CANCELLED", filled_quantity=2, average_price=3100.0,
                             status_message="circuit limit")
    for _ in range(2):
        _monitor(env)
        _tick()

    assert not _still_open_and_protected(env, buy_id, gtt_id)  # not closed, GTT kept, not journaled
    assert {leg["quantity"] for leg in env.kite.gtt_book[gtt_id]["orders"]} == {3}
    exit_row = ticket_row(env.db, sell_id)
    assert (exit_row["status"], exit_row["entry_fill_status"], exit_row["filled_quantity"]) == (
        "executed", "partial", 2)
    [alert] = [u for u in _urgent_updates(env) if u["ticket_id"] == sell_id]
    assert "PARTIAL EXIT: 2 share(s) sold" in alert["message"] and "3 share(s)" in alert["message"]
    listed = {t["id"]: t for t in env.client.get("/api/tickets").json()["data"]}
    assert listed[sell_id]["exit_state"] == "exit_partial"
    # The stale 5-share sibling can never oversell the 3 left.
    r = env.client.post(f"/api/tickets/{sibling}/confirm")
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "EXIT_QUANTITY_MISMATCH"
    ee = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    [result] = ee.json()["data"]["results"]
    assert result["status"] == "exit_submitted"
    assert [c["quantity"] for c in env.kite.calls_to("place_order")] == [5, 3]


# ---------------------------------------------------------------- G / H: UNKNOWN, adopted (PK-2)

def _unknown_exit(env, client) -> tuple[int, int, int]:
    set_mode(env.db, "live")
    buy_id, gtt_id = _live_position(env)
    sell_id = _pending_exit(env, buy_id)
    env.kite.auto_fill = False
    env.kite.accept_then_raise("place_order", requests.ReadTimeout("Read timed out. (read timeout=7)"))
    _assert_unknown_response(client.post(f"/api/tickets/{sell_id}/confirm"))
    return buy_id, gtt_id, sell_id


def test_g_unknown_exit_adopted_open_stays_exiting(env, unsafe_client, market_open):
    buy_id, gtt_id, sell_id = _unknown_exit(env, unsafe_client)
    assert not _still_open_and_protected(env, buy_id, gtt_id)

    for _ in range(2):
        _monitor(env)
        _tick()

    exit_row = ticket_row(env.db, sell_id)
    [sell] = [o for o in env.kite.order_book.values() if o["transaction_type"] == "SELL"]
    assert (exit_row["status"], exit_row["submission_state"], exit_row["kite_order_id"]) == (
        "executed", "adopted", sell["order_id"])
    assert exit_row["entry_fill_status"] == "pending"
    assert not _still_open_and_protected(env, buy_id, gtt_id)
    assert ticket_row(env.db, sell_id)["status"] != "pending_confirmation"
    assert len(_sells(env)) == 1


def test_h_unknown_exit_adopted_then_complete_closes_on_the_fill(env, unsafe_client, market_open):
    buy_id, gtt_id, sell_id = _unknown_exit(env, unsafe_client)
    _monitor(env)
    _precondition(ticket_row(env.db, sell_id)["submission_state"] == "adopted", "the monitor should adopt")
    assert not _still_open_and_protected(env, buy_id, gtt_id)

    _fill(env, ticket_row(env.db, sell_id)["kite_order_id"], price=3111.0)
    _monitor(env)
    _monitor(env)

    assert ticket_row(env.db, buy_id)["closed_at"] is not None
    assert [t["exit_price"] for t in _closed_trades_for(env, buy_id)] == [3111.0]
    assert env.kite.gtt_book[gtt_id]["status"] == "deleted"
    assert len(_sells(env)) == 1


def test_h_unknown_exit_whose_order_already_filled_closes_in_the_adopting_cycle(env, unsafe_client, market_open):
    buy_id, gtt_id, sell_id = _unknown_exit(env, unsafe_client)
    [sell] = [o for o in env.kite.order_book.values() if o["transaction_type"] == "SELL"]
    _fill(env, sell["order_id"], price=3099.0)

    _monitor(env)

    assert ticket_row(env.db, sell_id)["submission_state"] == "adopted"
    assert [t["exit_price"] for t in _closed_trades_for(env, buy_id)] == [3099.0]
    assert ticket_row(env.db, buy_id)["closed_at"] is not None


# ---------------------------------------------------------------- I: explicit exit + GTT firing

def test_i_gtt_fills_while_exit_open_then_exit_rejected_closes_exactly_once(env, market_open):
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env)
    gtt_order = env.kite.trigger_gtt(gtt_id, leg=0, status="COMPLETE")
    _tick()
    _tick()

    # The explicit exit owns the position: nothing recorded, flagged once.
    assert ticket_row(env.db, buy_id)["closed_at"] is None and _closed_trades_for(env, buy_id) == []
    errors = [b for b in env.broadcasts if b.get("type") == "trailing_error" and b.get("ticket_id") == buy_id]
    assert len(errors) == 1 and "POSSIBLE DOUBLE SELL" in errors[0]["message"]

    # The broker rejects the explicit exit (the shares are gone): ownership
    # drops, and the next watcher tick attributes the GTT's fill (PK-7).
    env.kite.set_order_state(order_id, "REJECTED", status_message="Insufficient holdings")
    _monitor(env)
    _tick()
    _tick()
    _monitor(env)

    exits = _rows(env, "SELECT id, status, entry_fill_status FROM trade_tickets WHERE closes_ticket_id = ? "
                       "ORDER BY id", buy_id)
    assert [(e["status"], e["entry_fill_status"]) for e in exits] == [("failed", "rejected"), ("executed", "filled")]
    [trade] = _closed_trades_for(env, buy_id)
    assert trade["sell_ticket_id"] == exits[1]["id"]
    assert trade["exit_price"] == env.kite.order_book[gtt_order]["average_price"]
    assert ticket_row(env.db, buy_id)["closed_at"] is not None
    assert len(env.kite.calls_to("place_order")) == 1  # only the explicit exit; nothing re-sent


def test_i_explicit_exit_and_gtt_both_fill_closes_once_and_warns_double_sell(env, market_open):
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env)
    env.kite.trigger_gtt(gtt_id, leg=0, status="COMPLETE")
    _tick()
    _fill(env, order_id, price=3100.0)

    for _ in range(3):
        _monitor(env)
        _tick()

    assert ticket_row(env.db, buy_id)["closed_at"] is not None
    [trade] = _closed_trades_for(env, buy_id)
    assert trade["sell_ticket_id"] == sell_id
    assert _rows(env, "SELECT id FROM trade_tickets WHERE closes_ticket_id = ? AND id != ?", buy_id, sell_id) == []
    [warning] = [u for u in _urgent_updates(env) if u["ticket_id"] == sell_id]
    assert "POSSIBLE DOUBLE SELL" in warning["message"]
    assert env.kite.calls_to("delete_gtt") == []  # a triggered GTT is not "removed"
    assert len(env.kite.calls_to("place_order")) == 1


# ---------------------------------------------------------------- J: sibling exits (PK-4)

@pytest.mark.parametrize("first_outcome", ["filled", "rejected"])
def test_j_sibling_exit_refused_while_exiting(env, market_open, first_outcome):
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env)
    sibling = _pending_exit(env, buy_id)

    r = env.client.post(f"/api/tickets/{sibling}/confirm")

    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "EXIT_ALREADY_IN_PROGRESS"
    assert "EXITING" in r.json()["detail"]["message"]
    assert ticket_row(env.db, sibling)["status"] == "pending_confirmation" and _event_types(env, sibling) == []
    assert len(_sells(env)) == 1

    if first_outcome == "filled":
        _fill(env, order_id)
        _monitor(env)
        assert ticket_row(env.db, sibling)["status"] == "rejected"
        assert f"Superseded by exit ticket {sell_id}" in _details(env, sibling)[0]
        assert env.client.post(f"/api/tickets/{sibling}/confirm").status_code == 409
        assert len(_sells(env)) == 1
    else:
        env.kite.set_order_state(order_id, "REJECTED")
        _monitor(env)
        # Nothing was sold: the other exit may close the position after all.
        r = env.client.post(f"/api/tickets/{sibling}/confirm")
        assert r.status_code == 200 and r.json()["data"]["exit_state"] == "exiting"
        assert len(_sells(env)) == 2 and env.kite.gtt_book[gtt_id]["status"] == "active"


def test_exit_scanner_does_not_recreate_an_exit_for_an_exiting_position(env, market_open):
    from backend import strategy_engine
    buy_id, _, _, _ = _exiting_position(env)
    conn = env.db.get_connection()
    try:
        assert strategy_engine._has_pending_exit(conn, buy_id)
    finally:
        conn.close()


# ---------------------------------------------------------------- K: Emergency Exit

def test_k_emergency_exit_submits_and_does_not_report_closed(env, market_open):
    set_mode(env.db, "live")
    buy_id, gtt_id = _live_position(env)
    env.kite.auto_fill = False

    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})

    [result] = r.json()["data"]["results"]
    assert result["status"] == "exit_submitted" and "EXITING" in result["message"]
    assert not _still_open_and_protected(env, buy_id, gtt_id)
    [preview] = env.client.get("/api/kill-switch/emergency-exit-preview").json()["data"]
    assert preview["ticket_id"] == buy_id and preview["exit_in_flight"] is True

    again = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    [second] = again.json()["data"]["results"]
    assert (second["status"], second["ticket_id"]) == ("already_exiting", result["ticket_id"])
    assert len(_sells(env)) == 1

    _fill(env, ticket_row(env.db, result["ticket_id"])["kite_order_id"])
    _monitor(env)
    assert ticket_row(env.db, buy_id)["closed_at"] is not None
    assert len(_closed_trades_for(env, buy_id)) == 1


# ---------------------------------------------------------------- L: restart

def test_l_persisted_exiting_ticket_survives_restart_and_is_resolved_later(env, market_open):
    set_mode(env.db, "live")
    buy_id, gtt_id = _live_position(env)
    order_id = env.kite.add_broker_order("TCS", "SELL", 5, status="OPEN", order_type="MARKET", price=0)
    sell_id = _pending_exit(env, buy_id, status="executed", mode="live", kite_order_id=order_id,
                            executed_at="2026-09-24 10:00:00", entry_fill_status="pending",
                            submission_state="acknowledged")

    env.db.init_db()  # a restart: migrations and the closed_trades backfill run
    env.db.init_db()

    assert not _still_open_and_protected(env, buy_id, gtt_id)
    _monitor(env)
    assert not _still_open_and_protected(env, buy_id, gtt_id)

    _fill(env, order_id, price=3080.0)
    _monitor(env)
    assert ticket_row(env.db, buy_id)["closed_at"] is not None
    assert [t["exit_price"] for t in _closed_trades_for(env, buy_id)] == [3080.0]
    env.db.init_db()
    assert len(_closed_trades_for(env, buy_id)) == 1


def test_journal_refuses_an_unfilled_exit(env):
    from backend import journal
    buy_id, _ = _live_position(env)
    sell_id = _pending_exit(env, buy_id, status="executed", mode="live", entry_fill_status="pending")
    conn = env.db.get_connection()
    try:
        with pytest.raises(ValueError, match="not filled"):
            journal.record_closed_trade(conn, sell_id)
    finally:
        conn.close()


# ---------------------------------------------------------------- N: GTT removal fails after the fill

def test_n_gtt_delete_failure_after_fill_keeps_position_closed_and_warns(env, market_open):
    buy_id, gtt_id, sell_id, order_id = _exiting_position(env)
    _fill(env, order_id)
    env.kite.raise_on_next("delete_gtt", NetworkException("Gateway timed out"))

    _monitor(env)
    _monitor(env)

    buy = ticket_row(env.db, buy_id)
    assert buy["closed_at"] is not None and len(_closed_trades_for(env, buy_id)) == 1
    assert buy["kite_gtt_id"] == gtt_id  # evidence kept for the orphan
    assert env.kite.gtt_book[gtt_id]["status"] == "active"
    [warning] = [u for u in _urgent_updates(env) if u["ticket_id"] == sell_id]
    assert "ORPHANED GTT" in warning["message"] and "delete it in Kite NOW" in warning["message"]
    [flag] = _rows(env, "SELECT actual FROM reconciliation_log WHERE ticket_id = ?", buy_id)
    # C4: the delete's read-back still lists the GTT active: a FAILED cancellation, never "removed".
    assert "CANCEL_FAILED" in flag["actual"] and "broker status active" in flag["actual"]


# ---------------------------------------------------------------- untracked-holding sell

def test_alert_action_sell_without_position_completes_without_closing_anything(env, market_open):
    set_mode(env.db, "live")
    env.kite.set_holding("TCS", 10, average_price=3000.0)
    env.kite.auto_fill = False
    created = env.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "TCS", "quantity": 4, "entry_order_type": "MARKET", "entry_price": 3000.0,
        "rationale": "reduce", "alert_type": "breach_concentration",
    }).json()["data"]
    done = env.client.post(f"/api/tickets/{created['id']}/confirm", json=approval_body(env.db, created['id'])).json()["data"]
    _fill(env, done["kite_order_id"], filled=4, price=3010.0)

    _monitor(env)

    row = ticket_row(env.db, created["id"])
    assert (row["entry_fill_status"], row["filled_price"]) == ("filled", 3010.0)
    assert _rows(env, "SELECT id FROM closed_trades") == []
