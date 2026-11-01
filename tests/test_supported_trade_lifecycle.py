"""PK-17 (P1-11): new positions are restricted to the one lifecycle the
terminal models — a LONG equity position, BUY + CNC. A short (SELL to open),
MIS or NRML entry is refused with UNSUPPORTED_MANUAL_TRADE at every layer:
creation/edit, the risk engine, confirmation and execution itself. Closing
sells (linked exits, Emergency Exit, Quick Action sells of held shares) are
unaffected.

Every refusal is asserted to have made no broker order at all.
"""
import json

import pytest

from conftest import approval_body, account_id, displayed_mode, set_mode, ticket_row
from fake_kite import FakeKite
from test_baseline_api import _insert_ticket, _manual_ticket
from test_live_safety import _live_position, _pending_exit

CODE = "UNSUPPORTED_MANUAL_TRADE"
ORDER_CALLS = ("place_order", "place_gtt", "modify_gtt", "delete_gtt", "cancel_order")

# (transaction_type, product_type) pairs a NEW position may not use. SELL
# rows carry short-shaped levels so they pass the price check and fail only
# on the lifecycle.
UNSUPPORTED = [("SELL", "CNC"), ("BUY", "MIS"), ("SELL", "MIS"), ("BUY", "NRML"), ("SELL", "NRML")]
_IDS = [f"{t}+{p}" for t, p in UNSUPPORTED]


def _levels(transaction_type):
    if transaction_type == "SELL":
        return dict(entry_price=3000.0, stop_loss=3100.0, target_1=2700.0)
    return dict(entry_price=3000.0, stop_loss=2900.0, target_1=3300.0)


def _orders(env):
    return [name for name, _ in env.kite.calls if name in ORDER_CALLS]


def _code(r):
    return r.json()["detail"]["error_code"]


def _ticket_count(env):
    conn = env.db.get_connection()
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM trade_tickets").fetchone()["n"]
    finally:
        conn.close()


def _events(env, tid):
    conn = env.db.get_connection()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT event_type, detail FROM ticket_events WHERE ticket_id = ? ORDER BY id", (tid,)).fetchall()]
    finally:
        conn.close()


def _historical(env, transaction_type, product_type, **overrides) -> int:
    """An unsupported manual entry as a pre-PK-17 version (or a manual DB
    edit) could have left it: awaiting approval, risk checks passed."""
    fields = dict(source="manual", transaction_type=transaction_type, product_type=product_type,
                  status="pending_confirmation", mode=None, risk_check_results="[]", **_levels(transaction_type))
    fields.update(overrides)
    return _insert_ticket(env.db, **fields)


def _run_risk(env, ticket_id):
    from backend import risk_engine
    from backend.accounts import get_trading_account
    conn = env.db.get_connection()
    try:
        risk_engine.run_risk_checks_and_transition(conn, get_trading_account(conn), ticket_id, env.kite)
    finally:
        conn.close()


@pytest.fixture
def live(env, market_open):
    set_mode(env.db, "live")
    return env


# ---------------------------------------------------------------- 1. supported entry

def test_manual_buy_cnc_is_accepted_and_executes(live):
    r = live.client.post("/api/tickets", json=_manual_ticket())
    assert r.status_code == 201, r.text
    t = r.json()["data"]
    assert (t["status"], t["transaction_type"], t["product_type"]) == ("pending_confirmation", "BUY", "CNC")
    r = live.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(live.db, t['id']))
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed", r.text
    [order] = live.kite.calls_to("place_order")
    assert (order["transaction_type"], order["product"]) == ("BUY", "CNC")


# ---------------------------------------------------------------- 2-6. creation refused

@pytest.mark.parametrize("mode", ["dry_run", "live"])
@pytest.mark.parametrize("transaction_type, product_type", UNSUPPORTED, ids=_IDS)
def test_unsupported_manual_entry_is_refused_at_creation(env, mode, transaction_type, product_type):
    set_mode(env.db, mode)
    mark = len(env.kite.calls)
    r = env.client.post("/api/tickets", json=_manual_ticket(
        transaction_type=transaction_type, product_type=product_type, **_levels(transaction_type)))
    assert r.status_code == 400 and _code(r) == CODE, r.text
    detail = r.json()["detail"]
    assert (detail["transaction_type"], detail["product_type"]) == (transaction_type, product_type)
    # Refused before anything was written, and before any broker/risk work.
    assert _ticket_count(env) == 0
    assert env.kite.calls[mark:] == []


def test_unsupported_entry_is_refused_not_rewritten(env):
    r = env.client.post("/api/tickets", json=_manual_ticket(product_type="MIS"))
    assert r.status_code == 400 and _code(r) == CODE
    assert _ticket_count(env) == 0      # no silently created BUY + CNC ticket either


def test_error_code_and_message_are_stable(env):
    r = env.client.post("/api/tickets", json=_manual_ticket(transaction_type="SELL", **_levels("SELL")))
    assert r.status_code == 400
    assert r.json()["detail"] == {
        "error_code": "UNSUPPORTED_MANUAL_TRADE",
        "message": "Unsupported new position: SELL + CNC. Manual entries currently support CNC long positions only "
                   "(BUY + CNC). Use Exit / Quick Sell to close holdings.",
        "transaction_type": "SELL",
        "product_type": "CNC",
    }


def test_alert_action_buy_with_intraday_product_is_refused(env):
    r = env.client.post("/api/tickets/alert-action/buy", json=_manual_ticket(product_type="MIS"))
    assert r.status_code == 400 and _code(r) == CODE
    assert _ticket_count(env) == 0


@pytest.mark.parametrize("transaction_type, product_type", [("SELL", "CNC"), ("BUY", "MIS"), ("BUY", "NRML")],
                         ids=["SELL+CNC", "BUY+MIS", "BUY+NRML"])
def test_draft_cannot_be_edited_into_an_unsupported_entry(env, transaction_type, product_type):
    tid = _insert_ticket(env.db, status="draft", mode=None)
    before = ticket_row(env.db, tid)
    r = env.client.put(f"/api/tickets/{tid}", json=_manual_ticket(
        transaction_type=transaction_type, product_type=product_type, **_levels(transaction_type)))
    assert r.status_code == 400 and _code(r) == CODE
    assert ticket_row(env.db, tid) == before


# ---------------------------------------------------------------- 7-10. closing sells still allowed

def test_linked_exit_sell_still_executes(live):
    buy_id, _ = _live_position(live)
    sell_id = _pending_exit(live, buy_id)
    r = live.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed", r.text
    [order] = live.kite.calls_to("place_order")
    assert (order["transaction_type"], order["product"]) == ("SELL", "CNC")


def test_emergency_exit_sell_still_allowed(live):
    _live_position(live)
    r = live.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    assert r.status_code == 200, r.text
    assert [x["status"] for x in r.json()["data"]["results"]] == ["exit_submitted"]
    assert [c["transaction_type"] for c in live.kite.calls_to("place_order")] == ["SELL"]


def test_exit_of_a_legacy_intraday_position_is_not_blocked(env):
    # Exits are never PK-17's business, whatever product the position has.
    buy_id = _insert_ticket(env.db, product_type="MIS", mode="dry_run", entry_fill_status="filled",
                            filled_price=3000.0, executed_at="2026-09-01 04:00:00")
    sell_id = _pending_exit(env, buy_id, product_type="MIS")
    r = env.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed", r.text


def test_quick_action_sell_of_held_shares_still_allowed(live):
    live.kite.set_holding("TCS", 10, average_price=3000.0)
    r = live.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "TCS", "quantity": 4, "entry_order_type": "MARKET", "entry_price": 3000.0,
        "rationale": "reduce exposure", "alert_type": "breach_concentration",
    })
    assert r.status_code == 201, r.text
    t = r.json()["data"]
    assert (t["source"], t["transaction_type"], t["closes_ticket_id"], t["status"]) == \
        ("alert_action", "SELL", None, "pending_confirmation")
    r = live.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(live.db, t['id']))
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed", r.text
    [order] = live.kite.calls_to("place_order")
    assert (order["transaction_type"], order["quantity"]) == ("SELL", 4)
    assert live.kite.calls_to("place_gtt") == []      # a reduction never arms a BUY-back GTT


def test_quick_action_sell_cannot_open_a_short(env):
    env.kite.set_holding("TCS", 3, average_price=3000.0)
    r = env.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "TCS", "quantity": 4, "entry_order_type": "MARKET", "entry_price": 3000.0,
        "rationale": "x", "alert_type": "breach_loss",
    })
    assert r.status_code == 400 and _code(r) == "EXCEEDS_HELD_QUANTITY"
    r = env.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "INFY", "quantity": 1, "entry_order_type": "MARKET", "entry_price": 1500.0,
        "rationale": "x", "alert_type": "breach_loss",
    })
    assert r.status_code == 400 and _code(r) == "NOT_HELD"
    assert _orders(env) == []


# ---------------------------------------------------------------- strategy tickets / risk engine guard

def test_strategy_buy_cnc_entry_still_passes_risk(env):
    tid = _insert_ticket(env.db, source="strategy", strategy_name="momentum_breakout", status="draft", mode=None)
    _run_risk(env, tid)
    row = ticket_row(env.db, tid)
    assert row["status"] == "pending_confirmation"
    assert "supported_lifecycle" not in {c["rule"] for c in json.loads(row["risk_check_results"])}


@pytest.mark.parametrize("transaction_type, product_type", [("SELL", "CNC"), ("BUY", "MIS"), ("BUY", "NRML")],
                         ids=["SELL+CNC", "BUY+MIS", "BUY+NRML"])
def test_misconfigured_strategy_entry_is_risk_rejected_without_broker_work(env, transaction_type, product_type):
    tid = _insert_ticket(env.db, source="strategy", strategy_name="momentum_breakout", status="draft", mode=None,
                         transaction_type=transaction_type, product_type=product_type, **_levels(transaction_type))
    mark = len(env.kite.calls)
    _run_risk(env, tid)
    row = ticket_row(env.db, tid)
    assert row["status"] == "risk_rejected"
    [check] = json.loads(row["risk_check_results"])
    assert (check["rule"], check["passed"], check["code"]) == ("supported_lifecycle", False, CODE)
    assert env.kite.calls[mark:] == []          # no holdings/margins/positions fetch, no sizing


# ---------------------------------------------------------------- 11. historical tickets

@pytest.mark.parametrize("mode", ["dry_run", "live"])
@pytest.mark.parametrize("transaction_type, product_type", UNSUPPORTED, ids=_IDS)
def test_historical_unsupported_pending_ticket_cannot_execute(env, market_open, mode, transaction_type, product_type):
    set_mode(env.db, mode)
    tid = _historical(env, transaction_type, product_type)
    r = env.client.post(f"/api/tickets/{tid}/confirm", json=approval_body(env.db, tid))
    assert r.status_code == 409 and _code(r) == CODE, r.text
    row = ticket_row(env.db, tid)
    # Rejected (it can never become valid), kept, never claimed or sent.
    assert row["status"] == "rejected"
    assert row["submission_state"] is None and row["order_tag"] is None and row["kite_order_id"] is None
    assert (row["transaction_type"], row["product_type"]) == (transaction_type, product_type)
    [event] = _events(env, tid)
    assert event["event_type"] == "rejected" and CODE in event["detail"]
    assert _orders(env) == []
    assert env.client.post(f"/api/tickets/{tid}/confirm", json=approval_body(env.db, tid)).status_code == 409


def test_reverting_a_rejected_unsupported_ticket_lands_in_risk_rejected(env):
    tid = _historical(env, "SELL", "MIS")
    assert env.client.post(f"/api/tickets/{tid}/confirm", json=approval_body(env.db, tid)).status_code == 409
    r = env.client.post(f"/api/tickets/{tid}/revert-rejection")
    assert r.status_code == 200, r.text
    t = r.json()["data"]
    assert t["status"] == "risk_rejected"
    assert [c["code"] for c in t["risk_check_results"]] == [CODE]
    assert _orders(env) == []


# ---------------------------------------------------------------- 12-13. execution backstop

@pytest.mark.parametrize("mode", ["dry_run", "live"])
@pytest.mark.parametrize("transaction_type, product_type", UNSUPPORTED, ids=_IDS)
def test_execution_layer_refuses_an_unsupported_new_position(env, mode, transaction_type, product_type):
    from backend import execution
    tid = _historical(env, transaction_type, product_type, status="confirmed", mode=mode,
                      entry_order_type="MARKET")
    kite = FakeKite(ltp={"NSE:TCS": 3000.0})
    result = execution.execute_ticket(kite, ticket_row(env.db, tid), entry_variety="regular")
    assert (result.success, result.status, result.event_type) == (False, "failed", "execution_failed")
    assert result.message.startswith(f"{CODE}: ") and "nothing was sent" in result.message
    assert result.kite_order_id is None and result.kite_gtt_id is None
    assert kite.calls == []


def test_execution_layer_still_runs_supported_entries_and_exits(env):
    from backend import execution
    base = {"id": 1, "created_at": "2026-09-01 04:00:00", "order_tag": None, "symbol": "TCS", "exchange": "NSE",
            "quantity": 5, "entry_price": 3000.0, "entry_order_type": "MARKET", "stop_loss": 2900.0,
            "target_1": 3300.0, "trailing_enabled": 0, "mode": "live"}
    entry = {**base, "transaction_type": "BUY", "product_type": "CNC", "closes_ticket_id": None, "source": "manual"}
    exit_ = {**base, "transaction_type": "SELL", "product_type": "MIS", "closes_ticket_id": 7, "source": "strategy"}
    reduction = {**base, "transaction_type": "SELL", "product_type": "CNC", "closes_ticket_id": None,
                 "source": "alert_action"}
    for ticket in (entry, exit_, reduction):
        assert execution.unsupported_new_position(ticket) is None
        kite = FakeKite(ltp={"NSE:TCS": 3000.0})
        result = execution.execute_ticket(kite, ticket, entry_variety="regular")
        assert result.success and result.kite_order_id is not None, result.message
