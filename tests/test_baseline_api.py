"""Baseline coverage of every pre-existing API flow. Written against the
original code first, then kept green through the review changes, so any
regression in existing functionality shows up here."""

import asyncio
import json
from datetime import date, timedelta

import pytest
from kiteconnect.exceptions import InputException, OrderException

from conftest import approval_body, displayed_mode, account_id, add_watch, last_weekday, make_candles, seed_ohlc, set_mode, ticket_row


def _manual_ticket(**overrides):
    body = {
        "symbol": "TCS", "exchange": "NSE", "transaction_type": "BUY", "quantity": 5,
        "entry_price": 3000.0, "entry_order_type": "LIMIT", "stop_loss": 2900.0,
        "target_1": 3300.0, "target_2": None, "trailing_enabled": False,
        "trailing_trigger": None, "trailing_distance": None, "product_type": "CNC",
        "use_gtt": True, "rationale": "baseline test",
    }
    body.update(overrides)
    return body


def _insert_ticket(db, **fields) -> int:
    base = {
        "account_id": account_id(db), "source": "manual", "strategy_name": None, "symbol": "TCS",
        "exchange": "NSE", "instrument_token": 2953217, "transaction_type": "BUY", "quantity": 5,
        "entry_price": 3000.0, "entry_order_type": "LIMIT", "stop_loss": 2900.0, "target_1": 3300.0,
        "trailing_enabled": 0, "product_type": "CNC", "use_gtt": 1, "risk_amount": 500.0,
        "risk_reward_ratio": 3.0, "rationale": "seeded", "status": "executed", "mode": "live",
    }
    base.update(fields)
    cols = ", ".join(base)
    conn = db.get_connection()
    try:
        cur = conn.execute(f"INSERT INTO trade_tickets ({cols}) VALUES ({', '.join('?' * len(base))})",
                           tuple(base.values()))
        if not any(name in fields for name in _REVIEW_FIELDS):
            _stamp_current_review(conn, cur.lastrowid)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# SEC-1 P1-D: a row seeded straight into the database is, by default, a
# ticket the user has just reviewed — exactly what POST /api/tickets stamps
# (now, the active Kite session, the digest of the stored row), the same way
# conftest seeds a healthy live state. Only manual / Quick-Action BUY entries
# that can still be approved are stamped. Tests of the review gate itself
# pass any review field explicitly (e.g. reviewed_at=None) to opt out.
_REVIEW_FIELDS = ("reviewed_at", "reviewed_session_id", "review_digest")
_REVIEWABLE_STATUSES = ("draft", "pending_confirmation", "rejected", "risk_rejected")


def _stamp_current_review(conn, ticket_id: int) -> None:
    from backend import account_safety
    row = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (ticket_id,)).fetchone()
    if row["status"] not in _REVIEWABLE_STATUSES or not account_safety.is_review_subject(row):
        return
    columns = account_safety.review_stamp(conn, row["account_id"])
    columns["review_digest"] = account_safety.ticket_review_digest(row)
    conn.execute("UPDATE trade_tickets SET reviewed_at = ?, reviewed_session_id = ?, review_digest = ? WHERE id = ?",
                 (columns["reviewed_at"], columns["reviewed_session_id"], columns["review_digest"], ticket_id))


# ---------------------------------------------------------------- session / portfolio

def test_session_and_portfolio_endpoints(env):
    c = env.client
    status = c.get("/api/session/status").json()
    assert status["connected"] is True and status["account_type"] == "trading"
    assert c.get("/api/config").status_code == 200
    assert c.get("/api/profile").json()["data"]["user_name"] == "Demo Trader"
    assert c.get("/api/holdings").json()["data"][0]["tradingsymbol"] == "RELIANCE"
    assert c.get("/api/margins").json()["data"]["equity"]["net"] == 1_000_000.0
    assert c.get("/api/positions").status_code == 200
    assert c.get("/api/mf_holdings").json()["data"] == []
    assert c.get("/").status_code == 200
    assert c.get("/static/app.js").status_code == 200


def test_instrument_search(env):
    data = env.client.get("/api/instruments/search", params={"q": "TC"}).json()["data"]
    assert any(r["tradingsymbol"] == "TCS" for r in data)


# ---------------------------------------------------------------- manual tickets

def test_create_ticket_runs_risk_checks(env):
    r = env.client.post("/api/tickets", json=_manual_ticket())
    assert r.status_code == 201, r.text
    t = r.json()["data"]
    assert t["status"] == "pending_confirmation"
    assert {c["rule"] for c in t["risk_check_results"]} >= {"min_risk_reward", "max_capital_per_trade_pct"}


def test_create_ticket_validation_errors(env):
    bad = env.client.post("/api/tickets", json=_manual_ticket(stop_loss=3100.0))
    assert bad.status_code == 400
    unknown = env.client.post("/api/tickets", json=_manual_ticket(symbol="NOSUCHSYM"))
    assert unknown.status_code == 400 and unknown.json()["detail"]["error_code"] == "INSTRUMENT_NOT_FOUND"


def test_risk_rejected_ticket(env):
    t = env.client.post("/api/tickets", json=_manual_ticket(quantity=5000)).json()["data"]
    assert t["status"] == "risk_rejected"
    assert any(not c["passed"] for c in t["risk_check_results"])


def test_reject_then_archive(env):
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/reject", json={"reason": "no"})
    assert r.json()["data"]["status"] == "rejected"
    d = env.client.delete(f"/api/tickets/{t['id']}").json()["data"]
    assert d == {"deleted": False, "archived": True}
    listed = env.client.get("/api/tickets").json()["data"]
    assert all(x["id"] != t["id"] for x in listed)
    listed_all = env.client.get("/api/tickets", params={"include_archived": True}).json()["data"]
    assert any(x["id"] == t["id"] for x in listed_all)


def test_revert_rejection_returns_to_pending_confirmation(env):
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    env.client.post(f"/api/tickets/{t['id']}/reject", json={"reason": "changed my mind"})
    r = env.client.post(f"/api/tickets/{t['id']}/revert-rejection")
    assert r.status_code == 200, r.text
    reverted = r.json()["data"]
    assert reverted["status"] == "pending_confirmation"
    assert reverted["archived"] == 0

    events = env.client.get(f"/api/tickets/{t['id']}").json()["data"]["events"]
    assert [e["event_type"] for e in events] == ["created", "rejected", "modified"]


def test_revert_rejection_re_runs_risk_checks_against_current_settings(env):
    c = env.client
    t = c.post("/api/tickets", json=_manual_ticket()).json()["data"]
    assert t["status"] == "pending_confirmation"
    c.post(f"/api/tickets/{t['id']}/reject", json={"reason": "no"})

    s = c.get("/api/risk/settings").json()["data"]
    s["min_risk_reward"] = 100.0
    body = {k: s[k] for k in ("max_risk_per_trade_pct", "max_capital_per_trade_pct", "max_open_positions",
                              "max_single_stock_exposure_pct", "max_sector_exposure_pct",
                              "max_total_portfolio_risk_pct", "min_risk_reward", "starting_capital")}
    assert c.post("/api/risk/settings", json=body).status_code == 200

    r = c.post(f"/api/tickets/{t['id']}/revert-rejection")
    assert r.json()["data"]["status"] == "risk_rejected"


def test_revert_rejection_also_unarchives(env):
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    env.client.post(f"/api/tickets/{t['id']}/reject", json={"reason": "no"})
    env.client.delete(f"/api/tickets/{t['id']}")  # archives it
    r = env.client.post(f"/api/tickets/{t['id']}/revert-rejection")
    reverted = r.json()["data"]
    assert reverted["status"] == "pending_confirmation"
    assert reverted["archived"] == 0
    listed = env.client.get("/api/tickets").json()["data"]
    assert any(x["id"] == t["id"] for x in listed)


def test_revert_rejection_rejects_non_rejected_ticket(env):
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/revert-rejection")
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "CONFLICT_INVALID_STATE"


def test_edit_only_allowed_for_draft(env):
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    r = env.client.put(f"/api/tickets/{t['id']}", json=_manual_ticket(quantity=6))
    assert r.status_code == 409


def test_confirm_dry_run_places_nothing(env, market_open):
    set_mode(env.db, "dry_run")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t['id'])).json()["data"]
    assert r["status"] == "executed" and r["mode"] == "dry_run"
    assert env.kite.calls_to("place_order") == [] and env.kite.calls_to("place_gtt") == []


def test_confirm_live_places_order_and_oco_gtt(env, market_open):
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t['id'])).json()["data"]
    assert r["status"] == "executed" and r["kite_order_id"] and r["kite_gtt_id"]
    order = env.kite.calls_to("place_order")[0]
    assert order["transaction_type"] == "BUY" and order["quantity"] == 5 and order["order_type"] == "LIMIT"
    gtt = env.kite.calls_to("place_gtt")[0]
    assert gtt["trigger_type"] == "two-leg"
    assert gtt["trigger_values"] == [2900.0, 3300.0]
    assert all(leg["transaction_type"] == "SELL" for leg in gtt["orders"])


def test_confirm_live_trailing_uses_single_gtt(env, market_open):
    set_mode(env.db, "live")
    body = _manual_ticket(trailing_enabled=True, trailing_trigger="after_target_1", trailing_distance=50.0)
    t = env.client.post("/api/tickets", json=body).json()["data"]
    env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t['id']))
    gtt = env.kite.calls_to("place_gtt")[0]
    assert gtt["trigger_type"] == "single" and gtt["trigger_values"] == [2900.0]


def test_confirm_live_order_rejected(env, market_open):
    set_mode(env.db, "live")
    env.kite.fail_next["place_order"] = OrderException("Insufficient funds")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t['id'])).json()["data"]
    assert r["status"] == "failed" and r["success"] is False
    assert "Insufficient funds" in r["execution_message"]


def test_confirm_live_protection_failed(env, market_open):
    set_mode(env.db, "live")
    env.kite.fail_next["place_gtt"] = InputException("bad trigger")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t['id'])).json()["data"]
    assert r["status"] == "executed" and r["kite_gtt_id"] is None
    assert "UNPROTECTED" in r["execution_message"]


def test_cancel_all_pending(env):
    env.client.post("/api/tickets", json=_manual_ticket())
    env.client.post("/api/tickets", json=_manual_ticket(symbol="INFY", entry_price=1500, stop_loss=1450,
                                                        target_1=1650))
    r = env.client.post("/api/tickets/cancel-all-pending").json()["data"]
    assert r["count"] == 2


def test_start_trailing_manual(env):
    tid = _insert_ticket(env.db, trailing_enabled=1, trailing_trigger="manual", trailing_distance=40.0,
                         kite_gtt_id=123)
    r = env.client.post(f"/api/tickets/{tid}/start_trailing")
    assert r.status_code == 200 and r.json()["data"]["trailing_active"] == 1


# ---------------------------------------------------------------- exits / kill switch

def test_exit_ticket_closes_position_and_records_trade(env):
    from backend import order_monitor
    set_mode(env.db, "live")
    buy_id = _insert_ticket(env.db, source="strategy", strategy_name="momentum_breakout", kite_gtt_id=None,
                            executed_at="2026-09-01 04:00:00", current_stop_loss=2900.0)
    sell_id = _insert_ticket(env.db, source="strategy", strategy_name="momentum_breakout_exit",
                             transaction_type="SELL", entry_order_type="MARKET", entry_price=3100.0,
                             status="pending_confirmation", mode=None, closes_ticket_id=buy_id,
                             rule_checklist=json.dumps({"time_exit": {"pass": True}}))
    r = env.client.post(f"/api/tickets/{sell_id}/confirm").json()["data"]
    assert r["status"] == "executed" and r["exit_state"] == "exiting"
    # PK-8: accepted is not filled — the order monitor closes it on COMPLETE.
    assert ticket_row(env.db, buy_id)["closed_at"] is None
    order_monitor.check_pending_orders(env.kite)
    assert ticket_row(env.db, buy_id)["closed_at"] is not None
    stats = env.client.get("/api/journal/stats", params={"mode": "live"}).json()["data"]
    assert stats["overall"]["trade_count"] == 1


def test_emergency_exit(env):
    from backend import order_monitor
    set_mode(env.db, "dry_run")
    _insert_ticket(env.db, mode="dry_run")
    preview = env.client.get("/api/kill-switch/emergency-exit-preview").json()["data"]
    assert preview == []  # dry-run positions are never "open" positions
    live_id = _insert_ticket(env.db, mode="live")
    preview = env.client.get("/api/kill-switch/emergency-exit-preview").json()["data"]
    assert [p["ticket_id"] for p in preview] == [live_id]
    bad = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "exit"})
    assert bad.status_code == 400
    set_mode(env.db, "live")
    ok = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    assert ok.status_code == 200 and ok.json()["data"]["count"] == 1
    assert ok.json()["data"]["results"][0]["status"] == "exit_submitted"
    order_monitor.check_pending_orders(env.kite)  # PK-8: closed on the broker fill
    assert ticket_row(env.db, live_id)["closed_at"] is not None


def test_execution_mode_switch(env):
    from conftest import make_live_ready
    make_live_ready(env)   # SEC-1: arming LIVE needs a passing, broker-checked Live Readiness
    c = env.client
    assert c.post("/api/execution/mode", json={"mode": "live", "confirm_real_money": False}).status_code == 400
    assert c.post("/api/execution/mode", json={"mode": "live", "confirm_real_money": True}).json()["data"]["mode"] == "live"
    assert c.get("/api/execution/mode").json()["data"]["mode"] == "live"
    assert c.post("/api/execution/mode", json={"mode": "dry_run"}).json()["data"]["mode"] == "dry_run"


# ---------------------------------------------------------------- orders / GTT books

def test_order_and_gtt_books_and_cancel(env, market_open):
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    done = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t['id'])).json()["data"]
    orders = env.client.get("/api/orders").json()["data"]
    assert orders[0]["linked_ticket_id"] == t["id"]
    gtts = env.client.get("/api/gtts").json()["data"]
    assert gtts[0]["linked_ticket_id"] == t["id"]
    assert env.client.get("/api/trades").status_code == 200
    r = env.client.post(f"/api/gtts/{done['kite_gtt_id']}/cancel").json()["data"]
    assert r["cancelled"] is True
    assert ticket_row(env.db, t["id"])["kite_gtt_id"] is None
    # PK-9: Kite accepting a cancel request is not a cancellation; the ticket
    # is left for order_monitor to resolve from the broker's final state.
    r = env.client.post(f"/api/orders/{done['kite_order_id']}/cancel").json()["data"]
    assert r["cancel_requested"] is True and r["linked_ticket_id"] == t["id"]
    assert ticket_row(env.db, t["id"])["status"] == "executed"


# ---------------------------------------------------------------- risk / sectors / prefs / panels

def test_risk_settings_roundtrip(env):
    c = env.client
    s = c.get("/api/risk/settings").json()["data"]
    s["max_open_positions"] = 7
    body = {k: s[k] for k in ("max_risk_per_trade_pct", "max_capital_per_trade_pct", "max_open_positions",
                              "max_single_stock_exposure_pct", "max_sector_exposure_pct",
                              "max_total_portfolio_risk_pct", "min_risk_reward", "starting_capital")}
    assert c.post("/api/risk/settings", json=body).json()["data"]["max_open_positions"] == 7
    assert any(h["field_changed"] == "max_open_positions" for h in c.get("/api/risk/settings/history").json()["data"])
    size = c.get("/api/risk/position-size", params={"entry_price": 1000, "stop_loss": 950}).json()["data"]
    assert size["suggested_qty"] > 0


def test_sector_mappings(env):
    c = env.client
    c.post("/api/risk/sector-mappings", json={"symbol": "tcs", "sector": "IT"})
    assert any(m["symbol"] == "TCS" for m in c.get("/api/risk/sector-mappings").json()["data"])
    assert "IT" in c.get("/api/risk/sectors").json()["data"]
    # PK-14: "unmapped" means no sector anywhere — the held RELIANCE resolves
    # through SECTOR_LOOKUP (it was listed before PK-14); a holding the
    # lookup does not know is still listed.
    assert c.get("/api/risk/unmapped-symbols").json()["data"] == []
    env.kite.set_holding("NOSUCHCO", 3, average_price=10.0, last_price=10.0)
    assert c.get("/api/risk/unmapped-symbols").json()["data"] == ["NOSUCHCO"]
    c.delete("/api/risk/sector-mappings/TCS")
    assert c.get("/api/risk/sector-mappings").json()["data"] == []


def test_dashboard_panels_and_ui_prefs(env):
    c = env.client
    panels = c.get("/api/dashboard-panels").json()["data"]
    assert {p["panel_type"] for p in panels} >= {"portfolio_summary", "holdings_table"}
    panels[0]["size"] = "large"
    saved = c.put("/api/dashboard-panels", json={"panels": panels}).json()["data"]
    assert saved[0]["size"] == "large"
    assert c.post("/api/ui-prefs", json={"sidebar_collapsed": True}).json()["data"]["sidebar_collapsed"] is True
    assert c.get("/api/ui-prefs").json()["data"]["sidebar_collapsed"] is True


def test_dashboard_panels_screen_scoping(env):
    # The Watchlist screen's configurable panel grid reuses this same table
    # and endpoint via the `screen` param — saving one screen's layout must
    # never touch the other's rows (they used to share one unscoped table).
    c = env.client
    dash_panels = c.get("/api/dashboard-panels").json()["data"]
    wl_panels = c.get("/api/dashboard-panels", params={"screen": "watchlist"}).json()["data"]
    assert {p["panel_type"] for p in wl_panels} == {"watchlist_all", "watchlist_near_breakout"}
    assert not ({p["panel_type"] for p in dash_panels} & {p["panel_type"] for p in wl_panels})

    wl_panels[0]["size"] = "small"
    c.put("/api/dashboard-panels", json={"panels": wl_panels}, params={"screen": "watchlist"})

    dash_panels_after = c.get("/api/dashboard-panels").json()["data"]
    assert {p["panel_type"] for p in dash_panels_after} == {p["panel_type"] for p in dash_panels}
    wl_panels_after = c.get("/api/dashboard-panels", params={"screen": "watchlist"}).json()["data"]
    assert next(p for p in wl_panels_after if p["panel_type"] == "watchlist_all")["size"] == "small"


# ---------------------------------------------------------------- watchlist

def test_watchlist_add_remove(env):
    c = env.client
    r = c.post("/api/watchlist", json={"symbol": "INFY", "notes": "watch"})
    assert r.status_code == 201
    dup = c.post("/api/watchlist", json={"symbol": "INFY"})
    assert dup.status_code == 409
    items = c.get("/api/watchlist").json()["data"]
    assert items[0]["symbol"] == "INFY"
    c.delete(f"/api/watchlist/{items[0]['id']}")
    assert c.get("/api/watchlist").json()["data"] == []


def test_watchlist_seeds_live_price_without_a_websocket_tick(env):
    # Regression test: before this fix, last_price/day_change_percentage on
    # a freshly loaded watchlist row came ONLY from a live WS tick, so every
    # row showed blank until an unrelated add/remove forced a resubscribe.
    c = env.client
    seed_ohlc(env.db, 738561, "RELIANCE", [
        {"date": last_weekday(date.today()), "open": 1290.0, "high": 1310.0, "low": 1285.0,
         "close": 1290.0, "volume": 100000},
    ])
    env.kite.ltp_map["NSE:RELIANCE"] = 1300.0
    c.post("/api/watchlist", json={"symbol": "RELIANCE"})
    row = c.get("/api/watchlist").json()["data"][0]
    assert row["last_price"] == 1300.0
    assert row["day_change_percentage"] == pytest.approx((1300.0 - 1290.0) / 1290.0 * 100)


def test_watchlist_tags(env):
    c = env.client
    added = c.post("/api/watchlist", json={"symbol": "TCS", "tag": "Momentum candidates"}).json()["data"]
    c.post("/api/watchlist", json={"symbol": "INFY"})  # untagged
    assert added["tag"] == "Momentum candidates"

    tags = c.get("/api/watchlist/tags").json()["data"]
    assert tags == ["Momentum candidates"]

    updated = c.put(f"/api/watchlist/{added['id']}/tag", json={"tag": "Earnings this week"}).json()["data"]
    assert updated["tag"] == "Earnings this week"

    cleared = c.put(f"/api/watchlist/{added['id']}/tag", json={"tag": ""}).json()["data"]
    assert cleared["tag"] is None

    missing = c.put("/api/watchlist/999999/tag", json={"tag": "X"})
    assert missing.status_code == 404


# ---------------------------------------------------------------- strategies / backtest

def _breakout_candles(end=None):
    candles = make_candles(end or last_weekday(date.today()), 320, 1000.0, daily_drift_pct=0.12)
    prior_max = max(c["close"] for c in candles[-21:-1])
    last = candles[-1]
    last["close"] = round(prior_max * 1.03, 2)
    last["high"] = round(last["close"] * 1.004, 2)
    last["volume"] = 400_000
    return candles


def test_strategy_config_versioning(env):
    c = env.client
    names = {s["strategy_name"] for s in c.get("/api/strategies").json()["data"]}
    assert names == {"momentum_breakout", "trend_pullback", "rsi_mean_reversion"}
    cfg = c.get("/api/strategies/momentum_breakout").json()["data"]
    params = dict(cfg["params"], max_signals_per_day=4)
    saved = c.put("/api/strategies/momentum_breakout", json={"enabled": True, "params": params}).json()["data"]
    assert saved["enabled"] is True and saved["current_version"] == 2
    versions = c.get("/api/strategies/momentum_breakout/versions").json()["data"]
    assert versions[0]["diff_from_previous"] == {"max_signals_per_day": {"old": 3, "new": 4}}
    bad = c.put("/api/strategies/momentum_breakout", json={"enabled": True, "params": {"x": 1}})
    assert bad.status_code == 400
    assert c.get("/api/strategies/nope").status_code == 404


def test_scan_now_creates_breakout_signal(env):
    c = env.client
    seed_ohlc(env.db, 2953217, "TCS", _breakout_candles())
    add_watch(env.db, "TCS", 2953217)
    cfg = c.get("/api/strategies/momentum_breakout").json()["data"]
    c.put("/api/strategies/momentum_breakout", json={"enabled": True, "params": cfg["params"]})
    result = c.post("/api/strategies/scan-now").json()["data"]
    assert result["enabled"] is True
    ids = result["by_strategy"]["momentum_breakout"]["entry_tickets"]
    assert len(ids) == 1
    t = c.get(f"/api/tickets/{ids[0]}").json()["data"]
    assert t["source"] == "strategy" and t["status"] == "pending_confirmation"
    assert t["stop_loss"] < t["entry_price"] < t["target_1"]
    actions = c.get("/api/todays-actions").json()["data"]
    assert [x["id"] for x in actions["buy_candidates"]] == ids
    assert c.get("/api/strategies/stop-adjustments").json()["data"] == []


def test_backtest_endpoint(env):
    c = env.client
    seed_ohlc(env.db, 2953217, "TCS", _breakout_candles())
    add_watch(env.db, "TCS", 2953217)
    r = c.post("/api/strategies/backtests", json={
        "strategy_name": "trend_pullback", "start_date": "2026-01-01", "end_date": "2026-09-11",
        "run_walk_forward": True, "window_days": 60, "run_monte_carlo": True, "mc_random_seed": 1,
    })
    assert r.status_code == 200, r.text
    run = r.json()["data"]
    assert run["walk_forward_windows"]
    assert c.get(f"/api/strategies/backtests/{run['id']}").json()["data"]["id"] == run["id"]
    assert c.get("/api/strategies/backtests").json()["data"][0]["id"] == run["id"]


# ---------------------------------------------------------------- journal / reconciliation

def test_journal_endpoints(env):
    assert env.client.get("/api/journal/mode-summary").json()["data"] == {"dry_run": 0, "live": 0}
    stats = env.client.get("/api/journal/stats", params={"mode": "dry_run"}).json()["data"]
    assert stats["overall"]["trade_count"] == 0 and stats["sample_size_warning"] is True


def test_reconciliation_list_and_review(env):
    conn = env.db.get_connection()
    conn.execute("INSERT INTO reconciliation_log (check_type, expected, actual, detail) VALUES "
                 "('position_state', '5 shares', '0 shares', 'test mismatch')")
    conn.commit()
    conn.close()
    items = env.client.get("/api/reconciliation/mismatches").json()["data"]
    assert len(items) == 1
    env.client.post(f"/api/reconciliation/{items[0]['id']}/mark-reviewed", json={"reason": "checked in Kite"})
    assert env.client.get("/api/reconciliation/mismatches").json()["data"] == []


# ---------------------------------------------------------------- trailing engine

def test_trailing_ratchet_moves_stop(env):
    from backend import trailing_engine
    gtt_id = env.kite.place_gtt("single", "TCS", "NSE", [2900.0], 3000.0, [])["trigger_id"]
    tid = _insert_ticket(env.db, trailing_enabled=1, trailing_trigger="after_target_1", trailing_distance=50.0,
                         trailing_active=1, current_stop_loss=2900.0, kite_gtt_id=gtt_id)
    env.kite.ltp_map["NSE:TCS"] = 3100.0
    asyncio.run(trailing_engine._tick_once())
    call = env.kite.calls_to("modify_gtt")[-1]
    assert call["trigger_values"] == [3050.0]
    assert ticket_row(env.db, tid)["current_stop_loss"] == 3050.0


def test_trailing_vanished_gtt_without_fill_is_flagged_not_closed(env):
    # PK-7 (P0-6): a GTT missing from Kite with no exit fill is lost
    # protection, not a close — nothing is journaled at a guessed price.
    from backend import trailing_engine
    tid = _insert_ticket(env.db, trailing_enabled=1, trailing_trigger="after_target_1", trailing_distance=50.0,
                         current_stop_loss=2900.0, kite_gtt_id=424242, executed_at="2026-09-01 04:00:00")
    asyncio.run(trailing_engine._tick_once())
    row = ticket_row(env.db, tid)
    assert row["closed_at"] is None and row["kite_gtt_id"] is None
    assert any(m["type"] == "trailing_error" and "PROTECTION MISSING" in m["message"] for m in env.broadcasts)
    stats = env.client.get("/api/journal/stats", params={"mode": "live"}).json()["data"]
    assert stats["overall"]["trade_count"] == 0
