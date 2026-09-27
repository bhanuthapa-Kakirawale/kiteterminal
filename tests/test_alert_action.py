"""Quick Action popup (Dashboard per-stock alert -> Sell/Buy). Covers the
source='alert_action' ticket-construction endpoints, the generalized
risk-engine exit/reduction check, and the execution/order_monitor routing
fix needed for a sell with no originating trade_ticket (closes_ticket_id
stays NULL)."""
import json

from conftest import displayed_mode, account_id, ticket_row


def _insert_position_ticket(db, **overrides) -> int:
    base = {
        "account_id": account_id(db), "source": "strategy", "strategy_name": "momentum_breakout",
        "symbol": "TCS", "exchange": "NSE", "instrument_token": 2953217, "transaction_type": "BUY",
        "quantity": 10, "entry_price": 3000.0, "entry_order_type": "LIMIT", "stop_loss": 2900.0,
        "target_1": 3300.0, "product_type": "CNC", "use_gtt": 1, "risk_amount": 1000.0,
        "risk_reward_ratio": 3.0, "rationale": "seeded", "status": "executed", "mode": "live",
        "entry_fill_status": "filled",
    }
    base.update(overrides)
    conn = db.get_connection()
    try:
        cols = ", ".join(base)
        cur = conn.execute(f"INSERT INTO trade_tickets ({cols}) VALUES ({', '.join('?' * len(base))})",
                           tuple(base.values()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _seed_holding(env, symbol="TCS", quantity=10, average_price=3000.0, last_price=3300.0, **extra):
    holding = {
        "tradingsymbol": symbol, "exchange": "NSE", "instrument_token": 2953217, "quantity": quantity,
        "t1_quantity": 0, "average_price": average_price, "last_price": last_price,
        "pnl": (last_price - average_price) * quantity, "day_change": 0.0, "day_change_percentage": 0.0,
    }
    holding.update(extra)
    env.kite._holdings = [h for h in env.kite._holdings if h["tradingsymbol"] != symbol] + [holding]
    env.kite.ltp_map[f"NSE:{symbol}"] = last_price


def _rows(env, sql, *params):
    conn = env.db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------- sell: case (b), no originating ticket

def test_alert_action_sell_no_originating_ticket_skips_position_sizing_checks(env):
    # The trickiest case: a holding that was never opened via a trade_ticket
    # in this app at all (pre-existing position, or bought directly in
    # Kite). closes_ticket_id must stay NULL, yet the ticket must still
    # reach pending_confirmation (not risk_rejected) via the generalized
    # case-(b) skip.
    _seed_holding(env, quantity=10, average_price=3000.0, last_price=2600.0)
    r = env.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "TCS", "quantity": 10, "entry_order_type": "MARKET", "entry_price": 2600.0,
        "rationale": "cut loss", "alert_type": "breach_loss",
    })
    assert r.status_code == 201, r.text
    ticket = r.json()["data"]
    assert ticket["source"] == "alert_action"
    assert ticket["closes_ticket_id"] is None
    assert ticket["status"] == "pending_confirmation"
    rules = {c["rule"] for c in ticket["risk_check_results"]}
    assert rules == {"exit_or_reduction"}
    checklist = json.loads(ticket_row(env.db, ticket["id"])["rule_checklist"])
    assert checklist["alert_action"]["trigger"] == "breach_loss"


def test_alert_action_sell_not_held_rejected(env):
    r = env.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "TCS", "quantity": 5, "entry_order_type": "MARKET", "entry_price": 3000.0,
        "rationale": "x", "alert_type": "breach_loss",
    })
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "NOT_HELD"


def test_alert_action_sell_exceeding_held_quantity_rejected_preflight(env):
    _seed_holding(env, quantity=10)
    r = env.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "TCS", "quantity": 11, "entry_order_type": "MARKET", "entry_price": 3000.0,
        "rationale": "x", "alert_type": "breach_loss",
    })
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "EXCEEDS_HELD_QUANTITY"


def test_check_exit_or_reduction_rejects_excess_quantity_authoritatively(env):
    # Bypasses the route's own pre-flight check to prove the risk engine's
    # check is the real (authoritative) enforcement, not just a UX nicety.
    from backend import risk_engine
    from backend.accounts import get_trading_account

    _seed_holding(env, quantity=5)
    ticket_id = _insert_position_ticket(
        env.db, source="alert_action", transaction_type="SELL", quantity=100,
        status="draft", closes_ticket_id=None, mode=None,
    )
    conn = env.db.get_connection()
    try:
        account = get_trading_account(conn)
        risk_engine.run_risk_checks_and_transition(conn, account, ticket_id, env.kite)
    finally:
        conn.close()
    row = ticket_row(env.db, ticket_id)
    assert row["status"] == "risk_rejected"
    checks = json.loads(row["risk_check_results"])
    assert checks[0]["rule"] == "exit_or_reduction"
    assert "exceeds currently held quantity" in checks[0]["message"]


# ---------------------------------------------------------------- sell: closes_ticket_id linking rules

def test_alert_action_sell_full_close_links_closes_ticket_id(env):
    _seed_holding(env, quantity=10, average_price=3000.0, last_price=3300.0)
    position_id = _insert_position_ticket(env.db, quantity=10)
    r = env.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "TCS", "quantity": 10, "entry_order_type": "MARKET", "entry_price": 3300.0,
        "rationale": "take profit", "alert_type": "breach_concentration",
    })
    ticket = r.json()["data"]
    assert ticket["closes_ticket_id"] == position_id


def test_alert_action_sell_partial_quantity_never_links_even_with_tracked_ticket(env):
    # A partial reduction (the common case for the concentration action)
    # must NOT link to a tracked ticket — journal.record_closed_trade
    # computes P&L using the referenced BUY's own quantity and
    # confirm_and_execute_ticket stamps closed_at on the whole ticket, so
    # linking a partial sell would silently corrupt both.
    _seed_holding(env, quantity=10, average_price=3000.0, last_price=3300.0)
    _insert_position_ticket(env.db, quantity=10)
    r = env.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "TCS", "quantity": 4, "entry_order_type": "MARKET", "entry_price": 3300.0,
        "rationale": "reduce", "alert_type": "breach_concentration",
    })
    ticket = r.json()["data"]
    assert ticket["closes_ticket_id"] is None
    assert ticket["status"] == "pending_confirmation"


def test_alert_action_sell_multiple_open_tickets_never_guesses(env):
    _seed_holding(env, quantity=10, average_price=3000.0, last_price=3300.0)
    _insert_position_ticket(env.db, quantity=6)
    _insert_position_ticket(env.db, quantity=4)
    r = env.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "TCS", "quantity": 10, "entry_order_type": "MARKET", "entry_price": 3300.0,
        "rationale": "x", "alert_type": "breach_loss",
    })
    ticket = r.json()["data"]
    assert ticket["closes_ticket_id"] is None
    checklist = json.loads(ticket_row(env.db, ticket["id"])["rule_checklist"])
    assert "ambiguous" in checklist["alert_action"]["linked_ticket"]


# ---------------------------------------------------------------- execution routing for case (b)

def test_alert_action_sell_live_execution_no_protective_gtt_no_journal_row(env):
    from backend.accounts import get_default_account

    _seed_holding(env, quantity=10, average_price=3000.0, last_price=2600.0)
    conn = env.db.get_connection()
    conn.execute("UPDATE execution_settings SET mode = 'live' WHERE id = 1")
    conn.commit()
    conn.close()

    created = env.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "TCS", "quantity": 10, "entry_order_type": "MARKET", "entry_price": 2600.0,
        "rationale": "cut loss", "alert_type": "breach_loss",
    }).json()["data"]
    done = env.client.post(f"/api/tickets/{created['id']}/confirm", json=displayed_mode(env.db)).json()["data"]

    assert done["status"] == "executed"
    assert done["kite_gtt_id"] is None  # no protective GTT attempted for a closing sell
    assert env.kite.calls_to("place_gtt") == []
    assert _rows(env, "SELECT * FROM closed_trades WHERE sell_ticket_id = ?", created["id"]) == []


def test_alert_action_sell_rejected_order_does_not_crash_order_monitor(env):
    # Regression: order_monitor's rejected-exit branch used to call
    # _log_event(conn, original_id, ...) unconditionally — with
    # original_id=None (a case-b sell) that violates ticket_events'
    # NOT NULL constraint and crashed the whole monitor cycle.
    from backend import order_monitor

    _seed_holding(env, quantity=10, average_price=3000.0, last_price=2600.0)
    conn = env.db.get_connection()
    conn.execute("UPDATE execution_settings SET mode = 'live' WHERE id = 1")
    conn.commit()
    conn.close()
    env.kite.auto_fill = False

    created = env.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "TCS", "quantity": 10, "entry_order_type": "MARKET", "entry_price": 2600.0,
        "rationale": "cut loss", "alert_type": "breach_loss",
    }).json()["data"]
    done = env.client.post(f"/api/tickets/{created['id']}/confirm", json=displayed_mode(env.db)).json()["data"]
    env.kite.set_order_state(done["kite_order_id"], "REJECTED", status_message="RMS: rejected")

    result = order_monitor.check_pending_orders(env.kite)  # must not raise
    assert result["updated"] == 1
    row = ticket_row(env.db, created["id"])
    assert row["status"] == "failed"


# ---------------------------------------------------------------- buy (average down): full risk engine

def test_alert_action_buy_runs_full_risk_engine(env):
    _seed_holding(env, quantity=10, average_price=3000.0, last_price=3300.0)
    r = env.client.post("/api/tickets/alert-action/buy", json={
        "symbol": "TCS", "transaction_type": "BUY", "quantity": 5000, "entry_price": 3300.0,
        "entry_order_type": "LIMIT", "stop_loss": 3200.0, "target_1": 3500.0,
        "product_type": "CNC", "rationale": "averaging down",
    })
    assert r.status_code == 201, r.text
    ticket = r.json()["data"]
    assert ticket["source"] == "alert_action"
    assert ticket["status"] == "risk_rejected"  # oversized buy fails max_capital_per_trade_pct
    rules = {c["rule"] for c in ticket["risk_check_results"]}
    assert rules == {
        "min_risk_reward", "max_capital_per_trade_pct", "max_single_stock_exposure_pct",
        "max_sector_exposure_pct", "max_open_positions", "max_total_portfolio_risk_pct",
    }  # all six run — no skip logic for a risk-adding buy


def test_alert_action_buy_rejects_non_buy_transaction_type(env):
    r = env.client.post("/api/tickets/alert-action/buy", json={
        "symbol": "TCS", "transaction_type": "SELL", "quantity": 5, "entry_price": 3000.0,
        "entry_order_type": "MARKET", "stop_loss": 2900.0, "target_1": 3300.0,
        "product_type": "CNC", "rationale": "x",
    })
    assert r.status_code == 400


# ---------------------------------------------------------------- concentration suggested quantity

def test_position_reduction_suggests_quantity_to_hit_target_exposure(env):
    _seed_holding(env, quantity=100, average_price=1000.0, last_price=1200.0)
    r = env.client.get("/api/risk/position-reduction", params={"symbol": "TCS"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["quantity_held"] == 100
    assert data["average_price"] == 1000.0
    # cost value = 100 * 1000 = 100,000; portfolio_value = holdings MTM
    # (100*1200=120,000) + margin (1,000,000) = 1,120,000 by default settings.
    settings = env.client.get("/api/risk/settings").json()["data"]
    target_pct = settings["max_single_stock_exposure_pct"]
    expected_current_pct = 100_000 / data["portfolio_value"] * 100
    assert data["current_exposure_pct"] == expected_current_pct
    assert data["target_exposure_pct"] == target_pct
    if expected_current_pct > target_pct:
        assert 0 < data["suggested_qty"] <= 100


# ---------------------------------------------------------------- account-type gate

def test_alert_action_endpoints_require_trading_account(env):
    conn = env.db.get_connection()
    conn.execute("UPDATE accounts SET account_type = 'investment'")
    conn.commit()
    conn.close()

    sell = env.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "TCS", "quantity": 1, "entry_order_type": "MARKET", "entry_price": 3000.0,
        "rationale": "x", "alert_type": "breach_loss",
    })
    buy = env.client.post("/api/tickets/alert-action/buy", json={
        "symbol": "TCS", "transaction_type": "BUY", "quantity": 1, "entry_price": 3000.0,
        "entry_order_type": "MARKET", "stop_loss": 2900.0, "target_1": 3300.0,
        "product_type": "CNC", "rationale": "x",
    })
    reduction = env.client.get("/api/risk/position-reduction", params={"symbol": "TCS"})

    for r in (sell, buy, reduction):
        assert r.status_code == 403
        assert r.json()["detail"]["error_code"] == "NOT_TRADING_ACCOUNT"
