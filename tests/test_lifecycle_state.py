"""PK-19: the server-derived position lifecycle (backend/lifecycle.py).

The server decides WHAT STATE a trade is in; these tests drive each state
through the real flows (create -> confirm -> order monitor / GTT watcher /
reconciliation, all on FakeKite) wherever the flow exists, and seed rows
directly only for states no flow reaches on demand. Every lifecycle is read
back through the API a browser uses. No sleeps, nothing reaches Zerodha.
"""

import json

import pytest
import requests

from conftest import account_id, displayed_mode, set_mode, ticket_row
from test_baseline_api import _insert_ticket, _manual_ticket
from test_live_safety import _closed_trades_for, _monitor, _rows, market_open  # noqa: F401 — market_open: fixture
from test_partial_fills import (
    _exit_ticket, _fill, _held_position, _partial_cancelled_entry, _place_entry, _place_exit, _reconcile, _tick,
)


# ---------------------------------------------------------------- helpers

def _ticket(env, ticket_id) -> dict:
    r = env.client.get(f"/api/tickets/{ticket_id}")
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _lc(env, ticket_id) -> dict:
    return _ticket(env, ticket_id)["lifecycle"]


def _positions(env, view="active") -> dict[int, dict]:
    r = env.client.get(f"/api/app-positions?view={view}")
    assert r.status_code == 200, r.text
    return {p["position_id"]: p for p in r.json()["data"]}


def _state(env, ticket_id) -> str:
    return _lc(env, ticket_id)["lifecycle_state"]


def _unknown_exit(env, buy_id, quantity) -> int:
    """A live exit whose place_order failed without proof of rejection: the
    order may or may not exist (PK-2)."""
    set_mode(env.db, "live")
    env.kite.auto_fill = False
    sell_id = _exit_ticket(env, buy_id, quantity)
    env.kite.raise_on_next("place_order", requests.ReadTimeout("Read timed out."))
    r = env.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200, r.text
    assert (r.json()["data"]["status"], r.json()["data"]["submission_state"]) == ("confirmed", "unknown")
    return sell_id


# ======================================================== 1-4: before the position

def test_1_draft_entry_is_pending_review(env):
    tid = _insert_ticket(env.db, status="draft", mode=None)
    lc = _lc(env, tid)
    assert lc["lifecycle_state"] == "PENDING_REVIEW"
    assert lc["is_position"] is False and lc["protection_status"] == "NOT_APPLICABLE"
    assert lc["allowed_actions"] == ["VIEW_DETAIL"]
    assert tid not in _positions(env) and tid not in _positions(env, "closed")


def test_2_pending_confirmation_is_ready_to_approve(env):
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    assert t["status"] == "pending_confirmation"
    assert t["ticket_state"]["label"] == "awaiting approval"
    lc = _lc(env, t["id"])
    assert lc["lifecycle_state"] == "READY_TO_APPROVE" and lc["open_quantity"] is None
    assert t["id"] not in _positions(env)


def test_3_submitted_live_entry_is_entry_open(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env, 100)
    t = _ticket(env, buy_id)
    lc = t["lifecycle"]
    assert t["status"] == "executed"  # the ticket executed ...
    assert t["ticket_state"]["code"] == "ORDER_WORKING"
    assert lc["lifecycle_state"] == "ENTRY_OPEN"  # ... but nothing is owned yet
    assert (lc["requested_quantity"], lc["filled_quantity"], lc["open_quantity"]) == (100, 0, 0)
    assert lc["protection_status"] == "PROTECTED" and lc["protection_quantity"] == 100
    assert "EXIT" not in lc["allowed_actions"]
    assert {"CANCEL_ORDER", "EMERGENCY_EXIT"} <= set(lc["allowed_actions"])
    assert buy_id in _positions(env)


def test_4_unknown_entry_is_submission_unknown_never_generic(env, market_open):
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    env.kite.raise_on_next("place_order", requests.ReadTimeout("Read timed out."))
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))
    assert r.json()["data"]["submission_state"] == "unknown"

    data = _ticket(env, t["id"])
    lc = data["lifecycle"]
    assert data["ticket_state"]["code"] == "UNKNOWN" and "DO NOT RESUBMIT" in data["ticket_state"]["label"]
    assert lc["lifecycle_state"] == "SUBMISSION_UNKNOWN" and lc["unknown_scope"] == "entry"
    assert "DO NOT RESUBMIT" in lc["lifecycle_label"]
    assert lc["review_required"] is True and lc["severity"] == "critical"
    assert lc["protection_status"] == "UNKNOWN"
    assert lc["allowed_actions"] == ["VIEW_DETAIL"]  # nothing that could send a second order
    assert _positions(env)[t["id"]]["lifecycle_state"] == "SUBMISSION_UNKNOWN"


# ============================================================ 5-9: entries and holds

def test_5_zero_fill_cancelled_entry_is_cancelled_and_not_a_position(env, market_open):
    buy_id, order_id, _ = _place_entry(env, 100)
    env.kite.set_order_state(order_id, "CANCELLED", filled_quantity=0)
    _monitor(env)
    lc = _lc(env, buy_id)
    assert lc["lifecycle_state"] == "CANCELLED" and lc["is_terminal"]
    assert buy_id not in _positions(env) and buy_id not in _positions(env, "closed")


def test_5b_broker_rejected_entry_is_rejected(env, market_open):
    buy_id, order_id, _ = _place_entry(env, 100)
    env.kite.set_order_state(order_id, "REJECTED", filled_quantity=0)
    _monitor(env)
    assert _state(env, buy_id) == "REJECTED"


def test_6_partial_fill_then_cancel_is_an_open_40_share_position(env, market_open):
    buy_id, _, _ = _partial_cancelled_entry(env)
    lc = _lc(env, buy_id)
    assert lc["lifecycle_state"] == "OPEN" and lc["entry_partial"] is True
    assert (lc["requested_quantity"], lc["filled_quantity"], lc["open_quantity"]) == (100, 40, 40)
    assert "EXIT" in lc["allowed_actions"]


def test_6b_entry_still_working_with_a_partial_fill_is_entry_partial(env, market_open):
    buy_id, order_id, _ = _place_entry(env, 100)
    _fill(env, order_id, 40)
    _monitor(env)
    t = _ticket(env, buy_id)
    assert t["lifecycle"]["lifecycle_state"] == "ENTRY_PARTIAL"
    assert t["ticket_state"]["code"] == "ORDER_PARTIAL" and "40/100" in t["ticket_state"]["label"]
    assert (t["lifecycle"]["filled_quantity"], t["lifecycle"]["open_quantity"]) == (40, 40)
    assert "EXIT" not in t["lifecycle"]["allowed_actions"]  # the entry can still fill further


def test_7_8_fully_filled_protected_position_is_open(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env, 100)
    _fill(env, order_id, 100, status="COMPLETE")
    _monitor(env)
    lc = _lc(env, buy_id)
    assert lc["lifecycle_state"] == "OPEN" and lc["severity"] == "ok"
    assert (lc["filled_quantity"], lc["open_quantity"], lc["exited_quantity"]) == (100, 100, 0)
    assert lc["protection_status"] == "PROTECTED" and lc["kite_gtt_id"] == gtt_id
    assert lc["review_required"] is False and lc["entry_partial"] is False
    assert {"EXIT", "EMERGENCY_EXIT", "VIEW_DETAIL"} <= set(lc["allowed_actions"])
    assert lc["active_exit"] is None and lc["entry_fill_price"] == 300.0


def test_9_open_position_without_its_gtt_is_unprotected(env):
    buy_id, _ = _held_position(env, 40, kite_gtt_id=None)
    lc = _lc(env, buy_id)
    assert lc["lifecycle_state"] == "OPEN"  # a flag, not a separate state
    assert lc["protection_status"] == "MISSING" and lc["protection_label"] == "UNPROTECTED"
    assert lc["review_required"] is True and lc["severity"] == "critical"
    assert "PROTECTION_MISSING" in lc["attention_reasons"]
    assert "EXIT" in lc["allowed_actions"]  # exiting an unprotected position stays possible


def test_9b_ticket_that_asked_for_no_gtt_is_not_requested(env):
    buy_id, _ = _held_position(env, 40, kite_gtt_id=None, use_gtt=0)
    assert _lc(env, buy_id)["protection_status"] == "NOT_REQUESTED"


# ================================================================ 10-15: exits

def test_10_exit_submitted_is_exiting_not_closed(env, market_open):
    buy_id, _ = _held_position(env, 40)
    sell_id, order_id = _place_exit(env, buy_id, 40)

    lc = _lc(env, buy_id)
    assert lc["lifecycle_state"] == "EXITING" and lc["closed_at"] is None
    assert lc["active_exit"]["ticket_id"] == sell_id and lc["active_exit"]["phase"] == "WORKING"
    assert (lc["exit_requested_quantity"], lc["exit_filled_quantity"], lc["remaining_quantity"]) == (40, 0, 40)
    assert lc["open_quantity"] == 40 and lc["protection_status"] == "PROTECTED"
    assert "EXIT" not in lc["allowed_actions"] and "EMERGENCY_EXIT" not in lc["allowed_actions"]
    assert "CANCEL_ORDER" in lc["allowed_actions"]
    # The exit ticket: an executed ticket whose position is EXITING.
    sell = _ticket(env, sell_id)
    assert sell["status"] == "executed" and sell["ticket_state"]["code"] == "EXITING"
    assert sell["lifecycle"]["position_id"] == buy_id and sell["lifecycle"]["lifecycle_state"] == "EXITING"
    assert buy_id in _positions(env) and buy_id not in _positions(env, "closed")


def test_11_exit_partly_filled_is_exit_partial(env, market_open):
    buy_id, _ = _held_position(env, 40)
    sell_id, order_id = _place_exit(env, buy_id, 40)
    _fill(env, order_id, 15, price=3100.0)
    _monitor(env)

    lc = _lc(env, buy_id)
    assert lc["lifecycle_state"] == "EXIT_PARTIAL" and lc["active_exit"]["phase"] == "PARTIAL"
    assert (lc["exit_requested_quantity"], lc["exit_filled_quantity"], lc["remaining_quantity"]) == (40, 15, 25)
    assert (lc["open_quantity"], lc["exited_quantity"]) == (25, 15)  # PK-13 open quantity, not the 40 requested
    assert "EXIT" not in lc["allowed_actions"]
    assert _ticket(env, sell_id)["ticket_state"]["code"] == "EXIT_WORKING_PARTIAL"


def test_11b_exit_ended_after_a_partial_fill_leaves_the_rest_exitable(env, market_open):
    buy_id, _ = _held_position(env, 40)
    sell_id, order_id = _place_exit(env, buy_id, 40)
    _fill(env, order_id, 15, status="CANCELLED", price=3100.0)
    _monitor(env)

    lc = _lc(env, buy_id)
    assert lc["lifecycle_state"] == "EXIT_PARTIAL" and lc["active_exit"] is None
    assert (lc["open_quantity"], lc["exited_quantity"]) == (25, 15)
    assert "EXIT" in lc["allowed_actions"]  # PK-13: the ended exit no longer owns the position
    assert _ticket(env, sell_id)["ticket_state"]["code"] == "EXIT_PARTIAL"


def test_12_exit_complete_is_closed_only_after_the_full_fill(env, market_open):
    buy_id, _ = _held_position(env, 40)
    sell_id, order_id = _place_exit(env, buy_id, 40)
    assert _state(env, buy_id) == "EXITING"
    _fill(env, order_id, 40, status="COMPLETE", price=3150.0)
    _monitor(env)

    lc = _lc(env, buy_id)
    assert lc["lifecycle_state"] == "CLOSED" and lc["open_quantity"] == 0 and lc["closed_at"]
    assert lc["protection_status"] == "NOT_APPLICABLE"
    assert lc["closed_trade"]["average_exit_price"] == 3150.0 and lc["closed_trade"]["quantity"] == 40
    assert "VIEW_JOURNAL" in lc["allowed_actions"] and "EXIT" not in lc["allowed_actions"]
    assert buy_id not in _positions(env)
    assert _positions(env, "closed")[buy_id]["closed_trade"]["realized_pnl"] == pytest.approx(150.0 * 40)
    assert _ticket(env, sell_id)["ticket_state"]["code"] == "EXIT_FILLED"


def test_13_unknown_exit_is_submission_unknown_on_the_position(env, market_open):
    buy_id, _ = _held_position(env, 40)
    sell_id = _unknown_exit(env, buy_id, 40)
    lc = _lc(env, buy_id)
    assert lc["lifecycle_state"] == "SUBMISSION_UNKNOWN" and lc["unknown_scope"] == "exit"
    assert lc["active_exit"]["ticket_id"] == sell_id and lc["active_exit"]["phase"] == "UNKNOWN"
    assert not {"EXIT", "EMERGENCY_EXIT", "CANCEL_ORDER"} & set(lc["allowed_actions"])
    assert lc["review_required"] is True
    assert _ticket(env, sell_id)["ticket_state"]["code"] == "UNKNOWN"


def test_14_rejected_exit_leaves_the_position_open(env, market_open):
    buy_id, gtt_id = _held_position(env, 40)
    sell_id, order_id = _place_exit(env, buy_id, 40)
    env.kite.set_order_state(order_id, "REJECTED", filled_quantity=0)
    _monitor(env)

    lc = _lc(env, buy_id)
    assert lc["lifecycle_state"] == "OPEN" and lc["open_quantity"] == 40 and lc["active_exit"] is None
    assert lc["protection_status"] == "PROTECTED" and lc["kite_gtt_id"] == gtt_id
    assert "EXIT" in lc["allowed_actions"]
    assert lc["review_required"] is True  # the rejected exit is an unresolved finding
    assert _ticket(env, sell_id)["lifecycle"]["lifecycle_state"] == "OPEN"


def test_15_partial_gtt_fill_leaves_an_open_residual(env, market_open):
    buy_id, gtt_id = _held_position(env, 100)
    order_id = env.kite.trigger_gtt(gtt_id, leg=0, status="OPEN", filled_quantity=40)
    _tick()
    lc = _lc(env, buy_id)
    assert lc["lifecycle_state"] == "EXIT_PARTIAL" and (lc["open_quantity"], lc["exited_quantity"]) == (60, 40)

    env.kite.set_order_state(order_id, "CANCELLED", filled_quantity=40)
    _tick()
    lc = _lc(env, buy_id)
    assert lc["lifecycle_state"] == "EXIT_PARTIAL" and lc["open_quantity"] == 60
    assert lc["protection_status"] == "MISSING"  # the spent GTT no longer protects the 60
    assert lc["closed_at"] is None and buy_id in _positions(env)


# ===================================================== 16-20: flags and invariants

def test_16_reconciliation_issue_sets_review_required(env):
    buy_id, _ = _held_position(env, 40)
    env.kite.set_holding("TCS", 10, average_price=3000.0)  # the broker holds fewer than the app
    before = _lc(env, buy_id)
    assert before["review_required"] is False
    _reconcile()

    lc = _lc(env, buy_id)
    assert lc["review_required"] is True and lc["lifecycle_state"] == "OPEN"
    [issue] = lc["reconciliation_issues"]
    assert issue["code"] == "BROKER_POSITION_SHORTFALL" and issue["label"] == "Broker quantity shortfall"
    assert issue["severity"] == "critical" and "VIEW_RECONCILIATION" in lc["allowed_actions"]

    env.client.post(f"/api/reconciliation/{issue['id']}/mark-reviewed")
    assert _lc(env, buy_id)["review_required"] is False  # a reviewed finding is no longer attached


def test_17_closed_position_never_reports_open(env):
    closed_id, _ = _held_position(env, 40, closed_at="2026-09-10 05:00:00", kite_gtt_id=None)
    assert _state(env, closed_id) == "CLOSED"
    # A position closed by an exit from before fill tracking (no closed_at):
    legacy_id, _ = _held_position(env, 40, kite_gtt_id=None)
    _exit_ticket(env, legacy_id, 40, status="executed", mode="live", entry_fill_status=None, filled_quantity=40)
    lc = _lc(env, legacy_id)
    assert lc["lifecycle_state"] == "CLOSED" and lc["open_quantity"] == 0
    assert set(_positions(env)) & {closed_id, legacy_id} == set()
    assert {closed_id, legacy_id} <= set(_positions(env, "closed"))


def test_18_mode_is_the_positions_own_never_the_global_switch(env):
    live_id, _ = _held_position(env, 40)
    dry_id = _insert_ticket(env.db, mode="dry_run", entry_fill_status="filled", filled_quantity=5,
                            filled_price=3000.0)
    set_mode(env.db, "live")
    assert _lc(env, dry_id)["mode"] == "dry_run"
    set_mode(env.db, "dry_run")
    assert _lc(env, live_id)["mode"] == "live"
    dry = _positions(env)[dry_id]
    assert dry["mode"] == "dry_run" and dry["protection_status"] == "NOT_APPLICABLE"
    assert "EXIT" in dry["allowed_actions"] and "EMERGENCY_EXIT" not in dry["allowed_actions"]


def test_19_open_quantity_is_pk13s(env, market_open):
    buy_id, _, _ = _partial_cancelled_entry(env)  # requested 100, filled 40
    sell_id, order_id = _place_exit(env, buy_id, 40)
    _fill(env, order_id, 15, status="CANCELLED", price=3100.0)
    _monitor(env)
    lc = _lc(env, buy_id)
    assert (lc["requested_quantity"], lc["filled_quantity"], lc["exited_quantity"], lc["open_quantity"]) == (
        100, 40, 15, 25)
    assert lc["protection_quantity"] == 25


def test_20_gtt_quantity_and_price_mismatch_are_reflected(env):
    buy_id, _ = _held_position(env, 40, gtt_quantity=100)  # PK-16: the GTT covers more than is held
    _reconcile()
    lc = _lc(env, buy_id)
    assert lc["protection_status"] == "QUANTITY_MISMATCH"
    assert lc["protection_label"] == "GTT QTY MISMATCH" and lc["severity"] == "critical"
    [issue] = [i for i in lc["reconciliation_issues"] if i["code"] == "GTT_QUANTITY_TOO_HIGH"]
    assert (issue["expected_quantity"], issue["broker_quantity"]) == (40, 100)

    price_id, gtt_id = _held_position(env, 5, current_stop_loss=2950.0)  # app stop 2950, GTT 2900
    _reconcile()
    _reconcile()  # PK-16 needs two consecutive sightings
    assert _lc(env, price_id)["protection_status"] == "PRICE_MISMATCH"


# ============================================================== extra states

def test_unconfirmed_entry_fill_needs_review(env):
    buy_id = _insert_ticket(env.db, entry_fill_status="unconfirmed", filled_quantity=0, kite_order_id="X1",
                            kite_gtt_id=None)
    t = _ticket(env, buy_id)
    assert t["lifecycle"]["lifecycle_state"] == "NEEDS_REVIEW" and t["lifecycle"]["severity"] == "critical"
    assert t["ticket_state"]["code"] == "FILL_UNCONFIRMED"


def test_unknown_open_quantity_needs_review(env):
    buy_id = _insert_ticket(env.db, entry_fill_status="partial", filled_quantity=None)
    lc = _lc(env, buy_id)
    assert lc["lifecycle_state"] == "NEEDS_REVIEW" and lc["open_quantity"] is None
    assert lc["protection_status"] == "UNKNOWN" and "EXIT" not in lc["allowed_actions"]


def test_not_found_submission_is_failed_not_placed(env):
    tid = _insert_ticket(env.db, status="failed", submission_state="not_found")
    t = _ticket(env, tid)
    assert t["lifecycle"]["lifecycle_state"] == "FAILED" and t["ticket_state"]["code"] == "NOT_PLACED"


def test_pending_exit_ticket_is_reviewed_not_duplicated(env):
    buy_id, _ = _held_position(env, 40)
    sell_id = _exit_ticket(env, buy_id, 40)
    lc = _lc(env, buy_id)
    assert lc["pending_exit_ticket_id"] == sell_id
    assert "REVIEW_EXIT" in lc["allowed_actions"] and "EXIT" not in lc["allowed_actions"]


def test_alert_action_reduction_of_an_untracked_holding_has_no_lifecycle(env):
    tid = _insert_ticket(env.db, source="alert_action", transaction_type="SELL", status="pending_confirmation",
                         mode=None, symbol="RELIANCE", instrument_token=738561)
    t = _ticket(env, tid)
    assert t["lifecycle"] is None and t["ticket_state"]["label"] == "awaiting approval"


def test_ticket_list_carries_the_same_lifecycle_as_the_detail(env, market_open):
    buy_id, _ = _held_position(env, 40)
    sell_id, _ = _place_exit(env, buy_id, 40)
    listed = {t["id"]: t for t in env.client.get("/api/tickets").json()["data"]}
    assert listed[buy_id]["lifecycle"] == _lc(env, buy_id)
    assert listed[sell_id]["lifecycle"]["lifecycle_state"] == "EXITING"
    assert listed[buy_id]["exit_in_flight_ticket_id"] == sell_id  # kept for compatibility
    assert listed[buy_id]["open_quantity"] == 40


# ================================================================== parity

def test_row_based_quantities_equal_the_sql_ones(env, market_open):
    """lifecycle.py computes quantities from rows read once; they must be
    exactly position_quantity's SQL figures, in every state built here."""
    from backend import lifecycle, position_quantity

    ids = []
    ids.append(_place_entry(env, 100)[0])                        # working, nothing filled
    b, o, _ = _place_entry(env, 100)
    _fill(env, o, 30)
    _monitor(env)
    ids.append(b)                                                # working, partly filled
    ids.append(_partial_cancelled_entry(env)[0])                 # ended partial
    b, _ = _held_position(env, 40)
    s, o = _place_exit(env, b, 40)
    _fill(env, o, 15, status="CANCELLED", price=3100.0)
    _monitor(env)
    ids.append(b)                                                # partial exit
    ids.append(_insert_ticket(env.db, entry_fill_status="partial", filled_quantity=None))   # unknown
    ids.append(_insert_ticket(env.db, entry_fill_status=None, filled_quantity=None))        # legacy
    b, _ = _held_position(env, 10)
    _exit_ticket(env, b, 20, status="executed", mode="live", entry_fill_status="filled", filled_quantity=20)
    ids.append(b)                                                # oversold (negative)

    conn = env.db.get_connection()
    try:
        ctx = lifecycle.build_context(conn, account_id(env.db))
        for pid in ids:
            row, exits = ctx.tickets[pid], ctx.exits.get(pid, [])
            assert position_quantity.open_quantity_from_rows(row, exits) == position_quantity.open_quantity(conn, pid)
            assert (position_quantity.exits_filled_from_rows(exits)
                    == position_quantity.exits_filled(conn, pid))
            assert (position_quantity.protection_quantity_from_rows(row, exits)
                    == position_quantity.protection_quantity(conn, row))
        # The in-flight predicate is EXIT_IN_FLIGHT_SQL's twin.
        from backend import execution
        sql_ids = {r["id"] for r in conn.execute(
            f"SELECT id FROM trade_tickets WHERE closes_ticket_id IS NOT NULL AND {execution.EXIT_IN_FLIGHT_SQL}")}
        py_ids = {x["id"] for exits in ctx.exits.values() for x in exits if lifecycle.exit_in_flight(x)}
        assert sql_ids == py_ids
    finally:
        conn.close()


# ======================================================== issue codes (PK-19 write sites)

def _codes(env, ticket_id):
    return [r["issue_code"] for r in _rows(env, "SELECT issue_code FROM reconciliation_log WHERE ticket_id = ?",
                                           ticket_id)]


def test_protection_findings_now_carry_issue_codes(env):
    from backend import position_quantity
    buy_id, _ = _held_position(env, 40, kite_gtt_id=None)
    conn = env.db.get_connection()
    try:
        position_quantity.sync_protection(conn, env.kite, buy_id, "test")
        conn.commit()
    finally:
        conn.close()
    assert _codes(env, buy_id) == ["PROTECTION_MISSING"]


@pytest.mark.parametrize("case", ["delete_fails", "gtt_triggered"])
def test_gtt_removal_findings_carry_issue_codes(env, case):
    from backend import position_quantity
    buy_id, gtt_id = _held_position(env, 40)
    if case == "delete_fails":
        env.kite.raise_on_next("delete_gtt", RuntimeError("Kite down"))
        expected = "ORPHANED_GTT_AFTER_EXIT"
    else:
        env.kite.gtt_book[gtt_id]["status"] = "triggered"
        expected = "POSSIBLE_DOUBLE_SELL"
    conn = env.db.get_connection()
    try:
        position_quantity.remove_protective_gtt(conn, env.kite, None, buy_id)
        conn.commit()
    finally:
        conn.close()
    assert _codes(env, buy_id) == [expected]
    lc = _lc(env, buy_id)
    assert expected in {i["code"] for i in lc["reconciliation_issues"]} and lc["severity"] == "critical"


def test_rule_checklist_is_untouched_by_lifecycle(env):
    # The lifecycle is derived, never stored: serializing does not write.
    buy_id, _ = _held_position(env, 40, rule_checklist=json.dumps({"x": 1}))
    before = ticket_row(env.db, buy_id)
    _lc(env, buy_id)
    env.client.get("/api/app-positions")
    assert ticket_row(env.db, buy_id) == before
