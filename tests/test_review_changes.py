"""Tests for the review changes: tick-size pricing, market-hours (AMO)
execution, the order-fill monitor, the autopilot pipeline, data-integrity
fixes, strategy fixes and the risk-engine bug fixes."""

import asyncio
import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from conftest import displayed_mode, account_id, add_watch, last_weekday, make_candles, seed_ohlc, set_mode, ticket_row
from test_baseline_api import _breakout_candles, _insert_ticket, _manual_ticket


def _on_tick_grid(price: float) -> bool:
    return abs(price * 20 - round(price * 20)) < 1e-6


def _conn(env):
    return env.db.get_connection()


def _rows(env, sql, *args):
    conn = _conn(env)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


@pytest.fixture
def market_closed(monkeypatch):
    from backend import market
    monkeypatch.setattr(market, "is_market_open", lambda at=None: False)


@pytest.fixture
def market_open(monkeypatch):
    from backend import market
    monkeypatch.setattr(market, "is_market_open", lambda at=None: True)


# ---------------------------------------------------------------- market clock / ticks

def test_round_to_tick():
    from backend.market import round_to_tick
    assert round_to_tick(1234.57) == 1234.55
    assert round_to_tick(1234.575) == 1234.6
    assert round_to_tick(1234.57, mode="up") == 1234.6
    assert round_to_tick(1234.57, mode="down") == 1234.55
    assert round_to_tick(2900.0, mode="up") == 2900.0
    assert round_to_tick(12.345, tick_size=0.01) == 12.35


def test_market_clock():
    from backend import market
    ist = market.IST
    monday_10 = datetime(2026, 9, 14, 10, 0, tzinfo=ist)
    assert market.is_market_open(monday_10)
    assert not market.is_market_open(datetime(2026, 9, 14, 16, 0, tzinfo=ist))
    assert not market.is_market_open(datetime(2026, 9, 12, 11, 0, tzinfo=ist))  # Saturday
    assert market.last_complete_session_date(monday_10) == date(2026, 9, 11)
    assert market.last_complete_session_date(datetime(2026, 9, 14, 15, 31, tzinfo=ist)) == date(2026, 9, 14)
    assert market.last_complete_session_date(datetime(2026, 9, 13, 12, 0, tzinfo=ist)) == date(2026, 9, 11)


# ---------------------------------------------------------------- execution

def test_live_entry_off_grid_prices_are_snapped_and_sent_as_amo(env, market_closed):
    set_mode(env.db, "live")
    body = _manual_ticket(entry_price=3001.37, stop_loss=2900.03, target_1=3300.02)
    t = env.client.post("/api/tickets", json=body).json()["data"]
    # PK-12H: an after-hours live entry is an AMO only with explicit consent.
    r = env.client.post(f"/api/tickets/{t['id']}/confirm",
                        json={**displayed_mode(env.db), "allow_amo": True}).json()["data"]
    assert r["status"] == "executed" and r["order_variety"] == "amo" and r["entry_fill_status"] == "pending"
    assert "AMO" in r["execution_message"]
    order = env.kite.calls_to("place_order")[0]
    assert order["variety"] == "amo" and order["price"] == 3001.35
    gtt = env.kite.calls_to("place_gtt")[0]
    assert gtt["trigger_values"] == [2900.05, 3300.0]
    assert all(_on_tick_grid(leg["price"]) for leg in gtt["orders"])
    assert gtt["orders"][0]["price"] < 2900.05  # stop leg buffered below the trigger


def test_live_entry_is_regular_while_market_open(env, market_open):
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))
    assert env.kite.calls_to("place_order")[0]["variety"] == "regular"


def test_dry_run_is_filled_by_the_paper_broker_at_the_observed_price(env, monkeypatch):
    # PK-20 (was: "marked filled" at the ticket price on approval).
    from backend import config, market, order_monitor
    monkeypatch.setattr(market, "is_market_open", lambda at=None: True)
    monkeypatch.setattr(config, "PAPER_SLIPPAGE_BPS", 0.0)
    set_mode(env.db, "dry_run")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db)).json()["data"]
    assert r["entry_fill_status"] == "pending" and r["filled_price"] is None
    env.kite.ltp_map["NSE:TCS"] = 2995.0   # the observed price, at or below the 3000 limit
    order_monitor.check_paper_orders(env.kite)
    row = ticket_row(env.db, t["id"])
    assert row["entry_fill_status"] == "filled" and row["filled_price"] == 2995.0


def test_confirm_broadcasts_tickets_changed(env, market_open):
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))
    reasons = [m.get("reason") for m in env.broadcasts if m["type"] == "tickets_changed"]
    assert "created" in reasons and "executed" in reasons


# ---------------------------------------------------------------- order monitor

def _confirm_live_entry(env, auto_fill: bool) -> dict:
    set_mode(env.db, "live")
    env.kite.auto_fill = auto_fill
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    return env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db)).json()["data"]


def test_order_monitor_records_entry_fill(env, market_open):
    from backend import order_monitor
    done = _confirm_live_entry(env, auto_fill=True)
    env.kite.order_book[done["kite_order_id"]]["average_price"] = 2999.5
    result = order_monitor.check_pending_orders(env.kite)
    assert result == {"checked": 1, "updated": 1}
    row = ticket_row(env.db, done["id"])
    assert row["entry_fill_status"] == "filled" and row["filled_price"] == 2999.5
    assert any(m["type"] == "order_update" and m["severity"] == "success" for m in env.broadcasts)
    assert order_monitor.check_pending_orders(env.kite)["checked"] == 0


def test_order_monitor_rejected_entry_cancels_ticket_and_gtt(env, market_open):
    from backend import order_monitor
    done = _confirm_live_entry(env, auto_fill=False)
    env.kite.set_order_state(done["kite_order_id"], "REJECTED", status_message="RMS: insufficient margin")
    order_monitor.check_pending_orders(env.kite)
    row = ticket_row(env.db, done["id"])
    assert row["status"] == "cancelled" and row["entry_fill_status"] == "rejected" and row["kite_gtt_id"] is None
    assert env.kite.gtt_book[done["kite_gtt_id"]]["status"] == "deleted"
    events = env.client.get(f"/api/tickets/{done['id']}").json()["data"]["events"]
    assert "never opened" in events[-1]["detail"]
    # a cancelled entry is no longer an open position anywhere
    assert env.client.get("/api/kill-switch/emergency-exit-preview").json()["data"] == []


def test_order_monitor_leaves_open_orders_alone(env, market_open):
    from backend import order_monitor
    done = _confirm_live_entry(env, auto_fill=False)
    assert order_monitor.check_pending_orders(env.kite)["updated"] == 0
    assert ticket_row(env.db, done["id"])["entry_fill_status"] == "pending"


def _open_position_with_exit(env) -> tuple[int, int, int]:
    set_mode(env.db, "live")
    gtt_id = env.kite.place_gtt("single", "TCS", "NSE", [2900.0], 3000.0, [])["trigger_id"]
    buy_id = _insert_ticket(env.db, source="strategy", strategy_name="momentum_breakout", kite_gtt_id=gtt_id,
                            executed_at="2026-09-01 04:00:00", entry_fill_status="filled", filled_price=3000.0)
    sell_id = _insert_ticket(env.db, source="strategy", strategy_name="momentum_breakout_exit",
                             transaction_type="SELL", entry_order_type="MARKET", entry_price=3100.0,
                             status="pending_confirmation", mode=None, closes_ticket_id=buy_id,
                             rule_checklist=json.dumps({"time_exit": {"pass": True}}))
    return buy_id, sell_id, gtt_id


def test_order_monitor_exit_fill_journals_at_fill_price(env):
    from backend import order_monitor
    buy_id, sell_id, _ = _open_position_with_exit(env)
    env.kite.auto_fill = False
    done = env.client.post(f"/api/tickets/{sell_id}/confirm").json()["data"]
    assert _rows(env, "SELECT * FROM closed_trades") == []  # PK-8: nothing before the fill
    env.kite.set_order_state(done["kite_order_id"], "COMPLETE", filled_quantity=5, average_price=3150.0)
    order_monitor.check_pending_orders(env.kite)
    trade = _rows(env, "SELECT * FROM closed_trades WHERE sell_ticket_id = ?", sell_id)[0]
    assert trade["exit_price"] == 3150.0 and trade["realized_pnl"] == pytest.approx(750.0)


def test_order_monitor_rejected_exit_keeps_position_open_and_protected(env):
    # PK-8: the exit never closed the position or removed its GTT, so a
    # rejection has nothing to reopen — the position simply stays as it was.
    from backend import order_monitor
    buy_id, sell_id, gtt_id = _open_position_with_exit(env)
    env.kite.auto_fill = False
    done = env.client.post(f"/api/tickets/{sell_id}/confirm").json()["data"]
    assert ticket_row(env.db, buy_id)["closed_at"] is None
    env.kite.set_order_state(done["kite_order_id"], "REJECTED", status_message="Exchange rejected")
    order_monitor.check_pending_orders(env.kite)
    buy = ticket_row(env.db, buy_id)
    assert buy["closed_at"] is None and buy["kite_gtt_id"] == gtt_id
    assert env.kite.gtt_book[gtt_id]["status"] == "active"
    assert ticket_row(env.db, sell_id)["status"] == "failed"
    assert _rows(env, "SELECT * FROM closed_trades") == []
    mismatches = env.client.get("/api/reconciliation/mismatches").json()["data"]
    assert any("still OPEN" in m["detail"] and "still active" in m["detail"] for m in mismatches)
    assert [p["ticket_id"] for p in env.client.get("/api/kill-switch/emergency-exit-preview").json()["data"]] == [buy_id]


def test_kill_switch_cancels_unfilled_entry_instead_of_selling(env, market_open):
    from backend import order_monitor
    done = _confirm_live_entry(env, auto_fill=False)
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"}).json()["data"]
    assert r["results"][0]["status"] == "entry_cancelled"
    assert [o["transaction_type"] for o in env.kite.calls_to("place_order")] == ["BUY"]  # no SELL placed
    order_monitor.check_pending_orders(env.kite)
    assert ticket_row(env.db, done["id"])["status"] == "cancelled"


# ---------------------------------------------------------------- risk-engine bug fixes

def test_rejected_exit_signal_keeps_position_open(env):
    from backend import risk_engine
    buy_id, sell_id, _ = _open_position_with_exit(env)
    env.client.post(f"/api/tickets/{sell_id}/reject", json={"reason": "not yet"})
    conn = _conn(env)
    try:
        assert [p["id"] for p in risk_engine.list_open_positions(conn, account_id(env.db))] == [buy_id]
    finally:
        conn.close()


def test_executed_exit_does_not_leave_phantom_open_risk(env):
    from backend import order_monitor, risk_engine

    def open_risk():
        conn = _conn(env)
        try:
            return risk_engine._open_risk_sum(conn, account_id(env.db))
        finally:
            conn.close()

    buy_id, sell_id, _ = _open_position_with_exit(env)
    assert open_risk() == 500.0
    env.client.post(f"/api/tickets/{sell_id}/confirm")
    assert open_risk() == 500.0  # PK-8: EXITING — still open until the fill
    order_monitor.check_pending_orders(env.kite)
    assert open_risk() == 0


# ---------------------------------------------------------------- trailing engine

def test_trailing_triggered_gtt_without_exit_evidence_stays_open(env):
    # PK-7 (P0-6): 'triggered' alone proves nothing was sold — no order at
    # Kite, no fill. The position stays open and is flagged for review.
    from backend import trailing_engine
    gtt_id = env.kite.place_gtt("single", "TCS", "NSE", [2900.0], 3000.0, [])["trigger_id"]
    env.kite.set_gtt_status(gtt_id, "triggered")
    tid = _insert_ticket(env.db, trailing_enabled=1, trailing_trigger="after_target_1", trailing_distance=50.0,
                         current_stop_loss=2900.0, kite_gtt_id=gtt_id, executed_at="2026-09-01 04:00:00")
    asyncio.run(trailing_engine._tick_once())
    row = ticket_row(env.db, tid)
    assert row["closed_at"] is None and row["kite_gtt_id"] == gtt_id
    assert any(m["type"] == "trailing_error" and "CLOSURE NEEDS REVIEW" in m["message"] for m in env.broadcasts)
    assert env.kite.calls_to("modify_gtt") == []


def test_trailing_deleted_gtt_without_fill_flags_unprotected(env):
    from backend import trailing_engine
    gtt_id = env.kite.place_gtt("single", "TCS", "NSE", [2900.0], 3000.0, [])["trigger_id"]
    env.kite.set_gtt_status(gtt_id, "deleted")
    tid = _insert_ticket(env.db, trailing_enabled=1, trailing_trigger="after_target_1", trailing_distance=50.0,
                         current_stop_loss=2900.0, kite_gtt_id=gtt_id, executed_at="2026-09-01 04:00:00")
    asyncio.run(trailing_engine._tick_once())
    row = ticket_row(env.db, tid)
    assert row["closed_at"] is None and row["kite_gtt_id"] is None
    assert any(m["type"] == "trailing_error" for m in env.broadcasts)


def test_trailing_ratchet_snaps_to_tick(env):
    from backend import trailing_engine
    gtt_id = env.kite.place_gtt("single", "TCS", "NSE", [2900.0], 3000.0, [])["trigger_id"]
    _insert_ticket(env.db, trailing_enabled=1, trailing_trigger="after_target_1", trailing_distance=50.0,
                   trailing_active=1, current_stop_loss=2900.0, kite_gtt_id=gtt_id)
    env.kite.ltp_map["NSE:TCS"] = 3101.23
    asyncio.run(trailing_engine._tick_once())
    call = env.kite.calls_to("modify_gtt")[-1]
    assert call["trigger_values"] == [3051.2] and _on_tick_grid(call["orders"][0]["price"])


def test_trailing_skips_entries_awaiting_fill(env):
    from backend import trailing_engine
    gtt_id = env.kite.place_gtt("single", "TCS", "NSE", [2900.0], 3000.0, [])["trigger_id"]
    _insert_ticket(env.db, trailing_enabled=1, trailing_trigger="after_target_1", trailing_distance=50.0,
                   trailing_active=1, current_stop_loss=2900.0, kite_gtt_id=gtt_id, entry_fill_status="pending")
    env.kite.ltp_map["NSE:TCS"] = 3500.0
    asyncio.run(trailing_engine._tick_once())
    assert env.kite.calls_to("modify_gtt") == []


# ---------------------------------------------------------------- strategy engine

def _enable(env, name="momentum_breakout"):
    cfg = env.client.get(f"/api/strategies/{name}").json()["data"]
    env.client.put(f"/api/strategies/{name}", json={"enabled": True, "params": cfg["params"]})


def test_scan_signal_is_tick_aligned_dated_and_not_reproposed(env):
    candles = _breakout_candles()
    seed_ohlc(env.db, 2953217, "TCS", candles)
    add_watch(env.db, "TCS", 2953217)
    _enable(env)
    first = env.client.post("/api/strategies/scan-now").json()["data"]
    ids = first["by_strategy"]["momentum_breakout"]["entry_tickets"]
    t = env.client.get(f"/api/tickets/{ids[0]}").json()["data"]
    assert all(_on_tick_grid(t[k]) for k in ("entry_price", "stop_loss", "target_1", "trailing_distance"))
    assert t["signal_date"] == candles[-1]["date"].strftime("%Y-%m-%d")
    env.client.post(f"/api/tickets/{ids[0]}/reject", json={"reason": "skip"})
    again = env.client.post("/api/strategies/scan-now").json()["data"]
    assert again["by_strategy"]["momentum_breakout"]["entry_tickets"] == []
    assert again["by_strategy"]["momentum_breakout"]["diagnostics"]["already_signalled"] == ["TCS"]


def test_old_risk_rejected_signal_no_longer_blocks_symbol(env):
    seed_ohlc(env.db, 2953217, "TCS", _breakout_candles())
    add_watch(env.db, "TCS", 2953217)
    _enable(env)
    _insert_ticket(env.db, source="strategy", strategy_name="momentum_breakout", status="risk_rejected", mode=None,
                   created_at="2026-01-05 10:00:00", signal_date="2026-01-05")
    result = env.client.post("/api/strategies/scan-now").json()["data"]
    assert len(result["by_strategy"]["momentum_breakout"]["entry_tickets"]) == 1


def test_scan_reports_insufficient_history(env):
    seed_ohlc(env.db, 2953217, "TCS", _breakout_candles()[-150:])
    add_watch(env.db, "TCS", 2953217)
    _enable(env)
    result = env.client.post("/api/strategies/scan-now").json()["data"]
    diag = result["by_strategy"]["momentum_breakout"]["diagnostics"]
    assert diag["insufficient_history"] == ["TCS"] and diag["evaluated"] == 0


def test_expire_stale_signals(env):
    from backend import strategy_engine
    old = _insert_ticket(env.db, source="strategy", strategy_name="momentum_breakout", status="pending_confirmation",
                         mode=None, signal_date="2026-09-01")
    same = _insert_ticket(env.db, source="strategy", strategy_name="momentum_breakout", status="pending_confirmation",
                          mode=None, signal_date="2026-09-11", symbol="INFY", instrument_token=408065)
    conn = _conn(env)
    try:
        assert strategy_engine.expire_stale_signals(conn, account_id(env.db), "2026-09-11") == [old]
    finally:
        conn.close()
    assert ticket_row(env.db, old)["status"] == "cancelled"
    assert ticket_row(env.db, same)["status"] == "pending_confirmation"


def test_thesis_invalidation_armed_only_after_price_reclaims_ema():
    from backend.strategies.shared import shared_exit_checks
    params = {"thesis_invalidation_ma": 50, "time_exit_days": 30}
    days = pd.bdate_range("2026-01-01", periods=123).strftime("%Y-%m-%d").tolist()
    closes = [100.0] * 120 + [90.0, 110.0, 95.0]
    df = pd.DataFrame({"date": days, "close": closes})
    position = {"executed_at": days[120], "stop_loss": 80.0, "target_1": 130.0}
    # Entry below the EMA (a mean-reversion buy): not armed, no exit signal.
    assert shared_exit_checks(position, df.iloc[:121], params)["passed"] is False
    # After reclaiming the EMA and falling back below it, the thesis is broken.
    result = shared_exit_checks(position, df, params)
    assert result["passed"] is True and result["checklist"]["thesis_invalidation"]["armed"] is True


# ---------------------------------------------------------------- stop adjustments

def _breakeven_proposal(env, gtt_id):
    tid = _insert_ticket(env.db, source="strategy", strategy_name="momentum_breakout", trailing_enabled=1,
                         trailing_trigger="after_target_1", trailing_distance=50.0, current_stop_loss=2900.0,
                         kite_gtt_id=gtt_id)
    conn = _conn(env)
    try:
        cur = conn.execute("INSERT INTO stop_adjustment_proposals (ticket_id, current_stop, proposed_stop, reason) "
                           "VALUES (?, 2900.0, 3000.03, 'test')", (tid,))
        conn.commit()
        return tid, cur.lastrowid
    finally:
        conn.close()


def test_breakeven_confirm_uses_live_price_and_tick_grid(env):
    set_mode(env.db, "live")
    gtt_id = env.kite.place_gtt("single", "TCS", "NSE", [2900.0], 3000.0, [])["trigger_id"]
    tid, pid = _breakeven_proposal(env, gtt_id)
    env.kite.ltp_map["NSE:TCS"] = 3200.0
    r = env.client.post(f"/api/strategies/stop-adjustments/{pid}/confirm")
    assert r.status_code == 200, r.text
    call = env.kite.calls_to("modify_gtt")[-1]
    assert call["last_price"] == 3200.0 and call["trigger_values"] == [3000.05]
    assert ticket_row(env.db, tid)["current_stop_loss"] == 3000.05


def test_breakeven_confirm_errors_are_readable(env):
    set_mode(env.db, "live")
    gtt_id = env.kite.place_gtt("single", "TCS", "NSE", [2900.0], 3000.0, [])["trigger_id"]
    _, pid = _breakeven_proposal(env, gtt_id)
    env.kite.ltp_map["NSE:TCS"] = 2990.0
    r = env.client.post(f"/api/strategies/stop-adjustments/{pid}/confirm")
    assert r.status_code == 409 and "already through" in r.json()["detail"]["message"]
    _, pid2 = _breakeven_proposal(env, None)
    r2 = env.client.post(f"/api/strategies/stop-adjustments/{pid2}/confirm")
    assert r2.status_code == 409 and "no active protective GTT" in r2.json()["detail"]["message"]


# ---------------------------------------------------------------- reconciliation

def test_position_check_counts_unsettled_and_ignores_surplus(env):
    from backend import reconciliation
    from backend.accounts import get_default_account
    _insert_ticket(env.db, entry_fill_status="filled")

    def run_with(holding_qty, t1_qty):
        env.kite._holdings = [{"tradingsymbol": "TCS", "quantity": holding_qty, "t1_quantity": t1_qty}]
        conn = _conn(env)
        try:
            reconciliation._check_position_state(conn, env.kite, get_default_account(conn))
            conn.commit()
        finally:
            conn.close()
        return env.client.get("/api/reconciliation/mismatches").json()["data"]

    assert run_with(0, 5) == []    # bought yesterday, still T+1
    assert run_with(20, 0) == []   # also held outside the app
    assert len(run_with(2, 0)) == 1


# ---------------------------------------------------------------- market data integrity

def test_refresh_corrects_partial_candle_and_deepens_history(env, monkeypatch):
    from backend import config, market, ohlc
    monkeypatch.setattr(config, "HISTORICAL_RATE_LIMIT_DELAY_SECONDS", 0)
    last_session = market.last_complete_session_date()
    full = make_candles(date.today(), 800, 500.0)
    final = [c for c in full if c["date"].date() <= last_session]
    stored = [dict(c) for c in final[-205:]]
    stored[-1] = dict(stored[-1], close=stored[-1]["close"] * 0.9, volume=10)  # captured mid-session
    seed_ohlc(env.db, 2953217, "TCS", stored)
    env.kite.candles[2953217] = full

    asyncio.run(ohlc.refresh_latest(env.kite, 2953217, "TCS"))
    agg = _rows(env, "SELECT MIN(date) AS lo, MAX(date) AS hi, COUNT(*) AS n FROM ohlc_daily WHERE instrument_token = 2953217")[0]
    assert agg["hi"] == last_session.isoformat()  # a still-forming session is never stored
    assert agg["lo"] <= (last_session - timedelta(days=1000)).isoformat() and agg["n"] > 700
    last = _rows(env, "SELECT close, volume FROM ohlc_daily WHERE instrument_token = 2953217 AND date = ?", agg["hi"])[0]
    assert last == {"close": final[-1]["close"], "volume": final[-1]["volume"]}

    calls_before = len(env.kite.calls_to("historical_data"))
    asyncio.run(ohlc.refresh_latest(env.kite, 2953217, "TCS"))
    assert len(env.kite.calls_to("historical_data")) == calls_before + 1  # deepening happens only once


# ---------------------------------------------------------------- autopilot

def _run_pipeline(trigger="manual"):
    from backend import automation, data_jobs

    async def go():
        worker = asyncio.create_task(data_jobs.run_job_worker())
        try:
            return await automation.run_pipeline(trigger)
        finally:
            worker.cancel()

    return asyncio.run(go())


def test_autopilot_pipeline_syncs_scans_and_queues_signals(env, monkeypatch):
    from backend import automation, config, market
    monkeypatch.setattr(config, "HISTORICAL_RATE_LIMIT_DELAY_SECONDS", 0)
    session = market.last_complete_session_date()
    env.kite.candles[2953217] = _breakout_candles(end=session)
    add_watch(env.db, "TCS", 2953217)
    _enable(env)
    stale = _insert_ticket(env.db, source="strategy", strategy_name="trend_pullback", status="pending_confirmation",
                           mode=None, signal_date="2020-01-01", symbol="INFY", instrument_token=408065)

    result = _run_pipeline()
    assert result["status"] == "success", result
    summary = result["summary"]
    assert summary["expired_signals"] == [stale]
    ids = summary["scan"]["by_strategy"]["momentum_breakout"]["entry_tickets"]
    assert len(ids) == 1 and ticket_row(env.db, ids[0])["status"] == "pending_confirmation"
    assert not automation.is_due(automation.scheduled_at(session) + timedelta(hours=1))
    status = env.client.get("/api/automation/status").json()["data"]
    assert status["last_session_done"] is True and status["pending"]["entries"] == 1
    assert status["runs"][0]["status"] == "success"
    assert any(m["type"] == "automation_run" and m.get("phase") == "finished" for m in env.broadcasts)


def test_autopilot_skips_when_no_new_candle(env, monkeypatch):
    from backend import automation, config, market
    monkeypatch.setattr(config, "HISTORICAL_RATE_LIMIT_DELAY_SECONDS", 0)
    session = market.last_complete_session_date()
    env.kite.candles[2953217] = make_candles(session - timedelta(days=14), 320, 1000.0)
    add_watch(env.db, "TCS", 2953217)
    _enable(env)
    result = _run_pipeline()
    assert result["status"] == "skipped" and "No candle" in result["summary"]["reason"]
    assert not automation.is_due(automation.scheduled_at(session) + timedelta(hours=1))


def test_autopilot_is_due_logic(env):
    from backend import automation, market
    ist = market.IST
    friday = date(2026, 9, 11)
    assert not automation.is_due(datetime(2026, 9, 11, 15, 45, tzinfo=ist))  # before the scheduled time
    assert automation.is_due(datetime(2026, 9, 11, 16, 5, tzinfo=ist))
    assert automation.is_due(datetime(2026, 9, 14, 9, 30, tzinfo=ist))       # Monday catch-up for Friday
    # Thursday's run is missing, so at Friday noon a catch-up is due right away...
    noon = datetime(2026, 9, 11, 12, 0, tzinfo=ist)
    assert automation.next_run_at(noon) == noon
    conn = env.db.get_connection()
    try:
        conn.execute("INSERT INTO automation_runs (account_id, run_date, trigger, status) "
                     "VALUES (?, '2026-09-10', 'scheduled', 'success')", (account_id(env.db),))
        for _ in range(3):
            conn.execute("INSERT INTO automation_runs (account_id, run_date, trigger, status) "
                         "VALUES (?, '2026-09-11', 'scheduled', 'failed')", (account_id(env.db),))
        conn.commit()
    finally:
        conn.close()
    # ...once it has run, the next run is Friday's scheduled time.
    assert automation.next_run_at(noon) == automation.scheduled_at(friday)
    # After the maximum number of failed attempts the day is given up on.
    assert not automation.is_due(datetime(2026, 9, 11, 19, 0, tzinfo=ist))


def test_autopilot_run_now_endpoint(env, monkeypatch):
    from backend import automation
    started = []

    async def fake_pipeline(trigger):
        started.append(trigger)

    monkeypatch.setattr(automation, "run_pipeline", fake_pipeline)
    r = env.client.post("/api/automation/run-now")
    assert r.status_code == 202 and r.json()["data"]["started"] is True


# ---------------------------------------------------------------- 2026-09 strategy parameter revision

def test_strategy_params_backfill_a_key_the_module_gained_later(env):
    """A config saved before a parameter existed must not crash the scanner.

    Strategy params are persisted as a JSON blob. When trend_pullback gained
    `volume_multiple`, every config already in the database lacked the key and
    the module's `params["volume_multiple"]` would have raised KeyError — on
    the nightly autopilot run, not at startup, which is the worst place to
    find out. get_config backfills missing keys from the module's own
    defaults, so an older row keeps working.
    """
    from backend import strategy_engine
    from backend.strategies import trend_pullback

    legacy = {k: v for k, v in trend_pullback.DEFAULT_PARAMS.items() if k != "volume_multiple"}
    merged = strategy_engine.params_with_defaults("trend_pullback", legacy)
    assert merged["volume_multiple"] == trend_pullback.DEFAULT_PARAMS["volume_multiple"]

    # A value the caller DID store always wins over the module default.
    stored = {**legacy, "atr_stop_multiple": 9.75}
    assert strategy_engine.params_with_defaults("trend_pullback", stored)["atr_stop_multiple"] == 9.75

    # An unknown strategy is passed through rather than exploding.
    assert strategy_engine.params_with_defaults("no_such_strategy", {"a": 1}) == {"a": 1}


def test_scan_survives_a_config_saved_before_the_new_parameter_existed(env):
    """End to end: write a legacy params blob straight into strategy_configs,
    then run a real scan. Before the backfill this raised KeyError."""
    import json as _json

    from backend.strategies import trend_pullback

    legacy = {k: v for k, v in trend_pullback.DEFAULT_PARAMS.items() if k != "volume_multiple"}
    conn = env.db.get_connection()
    try:
        conn.execute(
            "INSERT INTO strategy_configs (account_id, strategy_name, enabled, params, current_version) "
            "VALUES (?, 'trend_pullback', 1, ?, 1) "
            "ON CONFLICT(account_id, strategy_name) DO UPDATE SET enabled = 1, params = excluded.params",
            (account_id(env.db), _json.dumps(legacy)),
        )
        conn.commit()
    finally:
        conn.close()

    seed_ohlc(env.db, 2953217, "TCS", _breakout_candles())
    add_watch(env.db, "TCS", 2953217)
    r = env.client.post("/api/strategies/scan-now")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["by_strategy"]["trend_pullback"]["diagnostics"]["evaluated"] == 1


def test_revised_defaults_match_the_documented_research(env):
    """Pins the 2026-09 cost-aware parameter revision.

    These are not arbitrary numbers — each was chosen from the middle of a
    plateau that stayed profitable in both halves of the sample and at cost
    assumptions up to 0.70%. A silent edit back to the old, loss-making values
    (volume 1.5 / stop 2.0 / target 2.0) should fail loudly here and be
    argued for with fresh evidence.
    """
    from backend import db as db_module
    from backend.strategies import momentum_breakout, trend_pullback

    mb = momentum_breakout.DEFAULT_PARAMS
    assert (mb["volume_multiple"], mb["atr_stop_multiple"],
            mb["target_r_multiple"], mb["donchian_lookback"]) == (3.0, 3, 3, 30)

    tp = trend_pullback.DEFAULT_PARAMS
    assert (tp["volume_multiple"], tp["atr_stop_multiple"], tp["target_r_multiple"]) == (3.0, 3.5, 4)

    # backend/db.py keeps its own copy of these defaults to avoid a circular
    # import; the two must not drift apart or a fresh install seeds different
    # parameters from the ones the modules actually use.
    assert db_module._DEFAULT_MOMENTUM_BREAKOUT_PARAMS == mb
    assert db_module._DEFAULT_TREND_PULLBACK_PARAMS == tp


def test_trend_pullback_volume_filter_blocks_a_quiet_recovery(env):
    """The participation filter is the change that made this strategy viable:
    it cut 1511 trades to 165. A textbook pullback on thin volume must now be
    rejected."""
    from backend.strategies import trend_pullback
    from test_signal_detail import _frame, _pullback_candles

    params = dict(trend_pullback.DEFAULT_PARAMS)
    candles = _pullback_candles()
    assert trend_pullback.evaluate_entry(_frame(candles), params)["passed"] is True

    # Same price action, ordinary volume on the recovery bar.
    quiet = [dict(c) for c in candles]
    quiet[-1]["volume"] = int(sum(c["volume"] for c in quiet[-21:-1]) / 20)
    result = trend_pullback.evaluate_entry(_frame(quiet), params)
    by_name = {c["name"]: c for c in result["conditions"]}
    assert by_name["Volume surge"]["passed"] is False
    assert by_name["Recovery confirmed"]["passed"] is True, "only the volume condition should fail"
    assert result["passed"] is False
