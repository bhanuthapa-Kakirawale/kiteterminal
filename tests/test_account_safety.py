"""PK-12 (P1-6): account-level safety controls, one section per sub-packet.

Every refusal is asserted to have made NO broker order (place_order /
place_gtt), and — where the control is DB-only — no broker call at all.
The NSE clock is pinned open (market_open) unless a test is about market
hours; the gate's own clock (account_safety.now_ist) is only moved where a
test is about the trading date.
"""

from datetime import date, datetime, timedelta

import pytest
import requests
from kiteconnect.exceptions import InputException, NetworkException, OrderException

from conftest import IST, account_id, displayed_mode, set_mode, ticket_row
from test_baseline_api import _insert_ticket, _manual_ticket
from test_live_safety import _live_position, _pending_exit


# ---------------------------------------------------------------- helpers

ORDER_CALLS = ("place_order", "place_gtt", "modify_gtt", "delete_gtt", "cancel_order")


@pytest.fixture
def live(env, market_open):
    set_mode(env.db, "live")
    return env


def _sql(env, sql, *args):
    conn = env.db.get_connection()
    try:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        conn.commit()
        return rows
    finally:
        conn.close()


def _risk(env, **fields):
    assignments = ", ".join(f"{k} = ?" for k in fields)
    _sql(env, f"UPDATE risk_settings SET {assignments} WHERE account_id = ?", *fields.values(), account_id(env.db))


def _entry(env, **overrides) -> int:
    t = env.client.post("/api/tickets", json=_manual_ticket(**overrides)).json()["data"]
    assert t["status"] == "pending_confirmation", t
    return t["id"]


def _confirm(env, tid, **body):
    return env.client.post(f"/api/tickets/{tid}/confirm", json={**displayed_mode(env.db), **body})


def _code(r):
    return r.json()["detail"]["error_code"]


def _codes(r):
    return [f["code"] for f in r.json()["detail"]["failures"]]


def _orders(env):
    return [name for name, _ in env.kite.calls if name in ORDER_CALLS]


def _calls_since(env, mark):
    return [name for name, _ in env.kite.calls[mark:]]


def _sys_events(env, event_type):
    return _sql(env, "SELECT * FROM system_events WHERE event_type = ? ORDER BY id", event_type)


def _event_types(env, tid):
    return [e["event_type"] for e in _sql(env, "SELECT event_type FROM ticket_events WHERE ticket_id = ? ORDER BY id",
                                          tid)]


def _lock(env, locked=True, reason="incident drill"):
    r = env.client.post("/api/account-safety/trading-lock", json={"locked": locked, "reason": reason})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _status(env):
    return env.client.get("/api/account-safety/status").json()["data"]


def _session():
    from backend import market
    return market.last_complete_session_date()


def _strategy_entry(env, signal_date, **overrides) -> int:
    fields = dict(source="strategy", strategy_name="momentum_breakout", status="pending_confirmation", mode=None,
                  signal_date=signal_date, risk_check_results="[]")
    fields.update(overrides)
    return _insert_ticket(env.db, **fields)


def _assert_untouched_pending(env, tid):
    row = ticket_row(env.db, tid)
    assert row["status"] == "pending_confirmation"
    assert row["submission_state"] is None and row["order_tag"] is None and row["submitted_at"] is None
    assert row["kite_order_id"] is None


def _executed_exit(env):
    """A live position with a pending exit; returns (buy_id, sell_id)."""
    buy_id, _ = _live_position(env)
    return buy_id, _pending_exit(env, buy_id)


# ================================================================= PK-12A trading lock

def test_12a_locked_live_entry_is_refused_before_any_broker_call(live):
    tid = _entry(live)
    _lock(live)
    mark = len(live.kite.calls)
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "TRADING_LOCKED"
    assert _calls_since(live, mark) == []          # not even a portfolio / LTP read
    _assert_untouched_pending(live, tid)
    assert "confirmed" not in _event_types(live, tid)
    [refusal] = _sys_events(live, "account_safety_refusal")
    assert f"Ticket {tid}" in refusal["detail"] and "TRADING_LOCKED" in refusal["detail"]


def test_12a_lock_also_blocks_dry_run_entries(env):
    tid = _entry(env)
    _lock(env)
    r = _confirm(env, tid)
    assert r.status_code == 409 and _code(r) == "TRADING_LOCKED"
    _assert_untouched_pending(env, tid)


def test_12a_strategy_entry_cannot_bypass_the_lock(live):
    tid = _strategy_entry(live, _session().isoformat())
    _lock(live)
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "TRADING_LOCKED"
    assert _orders(live) == []
    _assert_untouched_pending(live, tid)


def test_12a_exit_is_still_allowed_while_locked(live):
    buy_id, sell_id = _executed_exit(live)
    _lock(live)
    r = live.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "executed"
    assert [c["transaction_type"] for c in live.kite.calls_to("place_order")] == ["SELL"]


def test_12a_emergency_exit_is_still_allowed_while_locked(live):
    _live_position(live)
    _lock(live)
    r = live.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    assert r.status_code == 200, r.text
    assert [x["status"] for x in r.json()["data"]["results"]] == ["exit_submitted"]
    assert [c["transaction_type"] for c in live.kite.calls_to("place_order")] == ["SELL"]


def test_12a_lock_persists_across_restart(live):
    _lock(live, reason="broker incident")
    live.db.init_db()  # what a restart runs: migrations + seeds must not clear it
    status = _status(live)
    assert status["trading_locked"] is True and status["trading_lock_reason"] == "broker incident"
    r = _confirm(live, _entry(live))
    assert r.status_code == 409 and _code(r) == "TRADING_LOCKED"


def test_12a_unlock_restores_entry_execution(live):
    tid = _entry(live)
    _lock(live)
    assert _confirm(live, tid).status_code == 409
    _lock(live, locked=False)
    r = _confirm(live, tid)
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"
    assert [c["transaction_type"] for c in live.kite.calls_to("place_order")] == ["BUY"]


def test_12a_lock_changes_are_explicit_logged_and_broadcast(live):
    assert _lock(live)["changed"] is True
    assert _lock(live)["changed"] is False                      # already locked: no-op
    assert _lock(live, locked=False)["changed"] is True
    assert [e["event_type"] for e in _sql(live, "SELECT event_type FROM system_events "
                                                "WHERE event_type LIKE 'trading_%' ORDER BY id")] == [
        "trading_locked", "trading_unlocked"]
    assert "incident drill" in _sys_events(live, "trading_locked")[0]["detail"]
    assert sum(b.get("type") == "account_safety_changed" for b in live.broadcasts) == 2


def test_12a_lock_set_during_revalidation_is_honoured_before_the_broker(live):
    from backend import account_safety
    tid = _entry(live)

    def lock_now(_kwargs):
        conn = live.db.get_connection()
        try:
            account_safety.set_trading_lock(conn, account_id(live.db), True, "locked mid-approval")
        finally:
            conn.close()

    live.kite.on_call("margins", lock_now)   # revalidation reads the portfolio
    r = _confirm(live, tid)
    live.kite.on_call("margins", None)
    assert r.status_code == 409 and _code(r) == "TRADING_LOCKED"
    assert _orders(live) == [] and live.kite.calls_to("ltp") == []
    _assert_untouched_pending(live, tid)     # the claim was withdrawn, not left in flight
    # claimed, risk revalidated, then withdrawn by the final gate
    assert _event_types(live, tid)[-3:] == ["confirmed", "modified", "modified"]
    last = _sql(live, "SELECT detail FROM ticket_events WHERE ticket_id = ? ORDER BY id DESC LIMIT 1", tid)[0]
    assert "TRADING_LOCKED" in last["detail"] and "No order was sent" in last["detail"]


# ================================================================= PK-12B absolute max notional

def _settings_body(env, **pk12):
    """What the Settings form posts: the existing fields as they are, plus
    any PK-12 fields given (an omitted PK-12 field is not sent)."""
    s = env.client.get("/api/risk/settings").json()["data"]
    body = {k: s[k] for k in ("max_risk_per_trade_pct", "max_capital_per_trade_pct", "max_open_positions",
                              "max_single_stock_exposure_pct", "max_sector_exposure_pct",
                              "max_total_portfolio_risk_pct", "min_risk_reward", "starting_capital")}
    body.update(pk12)
    return body


def test_12b_exactly_at_the_cap_is_allowed(live):
    _risk(live, max_order_notional=15000.0)            # 5 x 3000
    r = _confirm(live, _entry(live))
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


@pytest.mark.parametrize("overrides", [{"quantity": 6}, {"entry_price": 3000.05}], ids=["one_share", "one_tick"])
def test_12b_one_share_or_tick_above_the_cap_is_refused(live, overrides):
    _risk(live, max_order_notional=15000.0)
    tid = _entry(live, **overrides)
    mark = len(live.kite.calls)
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "MAX_NOTIONAL_EXCEEDED"
    assert _calls_since(live, mark) == []
    _assert_untouched_pending(live, tid)


def test_12b_disabled_cap_does_not_limit(live):
    _risk(live, max_order_notional=None)
    r = _confirm(live, _entry(live, quantity=6))
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


@pytest.mark.parametrize("value", [-1, 0, "lots"])
def test_12b_malformed_or_negative_setting_is_rejected(live, value):
    r = live.client.post("/api/risk/settings", json=_settings_body(live, max_order_notional=value))
    assert r.status_code == 422
    assert live.client.get("/api/risk/settings").json()["data"]["max_order_notional"] is None


def test_12b_database_rejects_a_negative_cap_too(live):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        _risk(live, max_order_notional=-5.0)


def test_12b_setting_is_saved_history_tracked_and_kept_when_omitted(live):
    r = live.client.post("/api/risk/settings", json=_settings_body(live, max_order_notional=250000.0))
    assert r.status_code == 200 and r.json()["data"]["max_order_notional"] == 250000.0
    history = live.client.get("/api/risk/settings/history").json()["data"]
    assert any(h["field_changed"] == "max_order_notional" and h["new_value"] == "250000.0" for h in history)
    # An older tab that does not know the field must never switch it off.
    r = live.client.post("/api/risk/settings", json=_settings_body(live))
    assert r.json()["data"]["max_order_notional"] == 250000.0
    # Explicit null disables it.
    r = live.client.post("/api/risk/settings", json=_settings_body(live, max_order_notional=None))
    assert r.json()["data"]["max_order_notional"] is None


def test_12b_exit_is_not_blocked_by_the_entry_notional_cap(live):
    _risk(live, max_order_notional=1.0)
    _, sell_id = _executed_exit(live)
    r = live.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


# ================================================================= PK-12C new entries per day

def _now_utc_sql():
    from backend import execution
    return execution.utc_now_sql()


def _entries_today(env):
    return _status(env)["entries_today"]


def test_12c_below_and_at_the_cap_then_next_entry_blocked(live):
    _risk(live, max_new_entries_per_day=2)
    for n in (1, 2):
        r = _confirm(live, _entry(live))
        assert r.status_code == 200 and r.json()["data"]["status"] == "executed"
        assert _entries_today(live) == n
    third = _entry(live)
    mark = len(live.kite.calls)
    r = _confirm(live, third)
    assert r.status_code == 409 and _code(r) == "DAILY_ENTRY_LIMIT"
    assert "2 live entries already sent today" in r.json()["detail"]["message"]
    assert _calls_since(live, mark) == []
    _assert_untouched_pending(live, third)
    assert len(live.kite.calls_to("place_order")) == 2


def test_12c_default_limit_is_three(live):
    assert _status(live)["max_new_entries_per_day"] == 3


def test_12c_exits_are_allowed_and_not_counted_at_the_cap(live):
    _risk(live, max_new_entries_per_day=1)
    assert _confirm(live, _entry(live)).status_code == 200
    _, sell_id = _executed_exit(live)
    r = live.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"
    assert _entries_today(live) == 1


def test_12c_dry_run_entries_are_excluded(env, market_open):
    _risk(env, max_new_entries_per_day=1)
    for _ in range(3):                                  # dry run: never limited, never counted
        assert _confirm(env, _entry(env)).json()["data"]["mode"] == "dry_run"
    set_mode(env.db, "live")
    assert _entries_today(env) == 0
    r = _confirm(env, _entry(env))
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_12c_unknown_live_submission_counts(live):
    from test_live_safety import _unknown_ticket
    _risk(live, max_new_entries_per_day=1)
    _unknown_ticket(live, submitted_at=_now_utc_sql())  # a real order may exist
    assert _entries_today(live) == 1
    r = _confirm(live, _entry(live))
    assert r.status_code == 409 and _code(r) == "DAILY_ENTRY_LIMIT"
    assert live.kite.calls_to("place_order") == []


def test_12c_definite_broker_rejection_is_not_counted(live):
    _risk(live, max_new_entries_per_day=1)
    live.kite.raise_on_next("place_order", OrderException("RMS: insufficient margin"))
    assert _confirm(live, _entry(live)).json()["data"]["status"] == "failed"
    assert _entries_today(live) == 0
    r = _confirm(live, _entry(live))
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_12c_claim_withdrawn_before_the_broker_is_not_counted(live):
    _risk(live, max_new_entries_per_day=1, max_entry_price_deviation_pct=1.0)
    live.kite.ltp_map["NSE:TCS"] = 3100.0              # final gate refuses: nothing reached the broker
    assert _code(_confirm(live, _entry(live))) == "PRICE_DEVIATION_EXCEEDED"
    assert _entries_today(live) == 0
    live.kite.ltp_map["NSE:TCS"] = 3000.0
    r = _confirm(live, _entry(live))
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_12c_next_ist_day_resets_the_count(live, monkeypatch):
    from backend import account_safety
    _risk(live, max_new_entries_per_day=1)
    assert _confirm(live, _entry(live)).status_code == 200
    blocked = _entry(live)
    assert _code(_confirm(live, blocked)) == "DAILY_ENTRY_LIMIT"
    tomorrow = account_safety.now_ist() + timedelta(days=1)
    monkeypatch.setattr(account_safety, "now_ist", lambda: tomorrow)
    assert _entries_today(live) == 0
    r = _confirm(live, blocked)
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_12c_ist_day_starts_at_ist_midnight():
    from backend import account_safety
    # 00:10 IST on the 28th is 18:40 UTC on the 27th; the day began 18:30 UTC.
    assert account_safety.ist_day_start_utc(datetime(2026, 9, 28, 0, 10, tzinfo=IST)) == "2026-09-27 18:30:00"
    assert account_safety.ist_day_start_utc(datetime(2026, 9, 28, 23, 50, tzinfo=IST)) == "2026-09-27 18:30:00"


def test_12c_disabled_limit_never_blocks(live):
    _risk(live, max_new_entries_per_day=None)
    for _ in range(4):
        assert _confirm(live, _entry(live)).status_code == 200
    assert len(live.kite.calls_to("place_order")) == 4


# ================================================================= PK-12D price deviation / stop sanity

def _refused_at_gate(env, tid, r, code, status="pending_confirmation"):
    assert code in _codes(r), r.json()
    assert _orders(env) == []
    row = ticket_row(env.db, tid)
    assert row["status"] == status
    assert row["submission_state"] is None and row["order_tag"] is None


def test_12d_normal_deviation_is_allowed(live):
    live.kite.ltp_map["NSE:TCS"] = 3050.0             # 1.67% from the 3000 limit, default max 3%
    r = _confirm(live, _entry(live))
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"
    order = live.kite.calls_to("place_order")[0]
    assert order["price"] == 3000.0                    # never rewritten


def test_12d_large_deviation_is_refused_without_an_order(live):
    live.kite.ltp_map["NSE:TCS"] = 3200.0             # 6.67%
    tid = _entry(live)
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "PRICE_DEVIATION_EXCEEDED"
    _refused_at_gate(live, tid, r, "PRICE_DEVIATION_EXCEEDED")
    assert ticket_row(live.db, tid)["entry_price"] == 3000.0
    assert _sys_events(live, "account_safety_refusal")


@pytest.mark.parametrize("ltp,allowed", [(3060.0, True), (2940.0, True), (3060.05, False), (2939.95, False)],
                         ids=["exactly_+2pct", "exactly_-2pct", "one_tick_over", "one_tick_under"])
def test_12d_threshold_boundary(live, ltp, allowed):
    _risk(live, max_entry_price_deviation_pct=2.0)
    live.kite.ltp_map["NSE:TCS"] = ltp
    r = _confirm(live, _entry(live))
    if allowed:
        assert r.status_code == 200 and r.json()["data"]["status"] == "executed"
    else:
        assert r.status_code == 409 and _code(r) == "PRICE_DEVIATION_EXCEEDED"
        assert _orders(live) == []


def test_12d_market_order_reference_price_is_checked_too(live):
    live.kite.ltp_map["NSE:TCS"] = 3200.0
    tid = _entry(live, entry_order_type="MARKET")
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "PRICE_DEVIATION_EXCEEDED"
    assert "reference price" in r.json()["detail"]["message"]


@pytest.mark.parametrize("ltp", [2900.0, 2850.0], ids=["at_stop", "through_stop"])
def test_12d_buy_whose_stop_is_already_crossed_is_rejected(live, ltp):
    _risk(live, max_entry_price_deviation_pct=50.0)    # isolate the stop check
    live.kite.ltp_map["NSE:TCS"] = ltp
    tid = _entry(live)
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "STOP_ALREADY_INVALID"
    _refused_at_gate(live, tid, r, "STOP_ALREADY_INVALID", status="rejected")
    assert ticket_row(live.db, tid)["stop_loss"] == 2900.0


def test_12d_short_entry_whose_stop_is_already_crossed_is_rejected(live):
    _risk(live, max_entry_price_deviation_pct=50.0)
    live.kite.ltp_map["NSE:TCS"] = 3100.0
    tid = _entry(live, transaction_type="SELL", entry_price=3000.0, stop_loss=3100.0, target_1=2700.0,
                 product_type="MIS")
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "STOP_ALREADY_INVALID"
    assert _orders(live) == []


def test_12d_both_failures_are_reported(live):
    live.kite.ltp_map["NSE:TCS"] = 2800.0              # 6.7% away AND through the stop
    r = _confirm(live, _entry(live))
    assert sorted(_codes(r)) == ["PRICE_DEVIATION_EXCEEDED", "STOP_ALREADY_INVALID"]


@pytest.mark.parametrize("failure", [NetworkException("Gateway timed out"), requests.ReadTimeout("read timeout")],
                         ids=["NetworkException", "ReadTimeout"])
def test_12d_ltp_unavailable_fails_closed(live, failure):
    live.kite.raise_on_next("ltp", failure)
    tid = _entry(live)
    r = _confirm(live, tid)
    assert r.status_code == 503 and _code(r) == "ACCOUNT_SAFETY_UNAVAILABLE"
    _refused_at_gate(live, tid, r, "ACCOUNT_SAFETY_UNAVAILABLE")
    assert _status(live)["breaker_recent_failures"] == 1     # a transport failure counts (PK-12G)


@pytest.mark.parametrize("price", [0, None, -5, float("nan")])
def test_12d_malformed_ltp_fails_closed(live, price):
    live.kite.ltp_map["NSE:TCS"] = price
    r = _confirm(live, _entry(live))
    assert r.status_code == 503 and _code(r) == "ACCOUNT_SAFETY_UNAVAILABLE"
    assert _orders(live) == []


def test_12d_disabled_deviation_still_checks_the_stop(live):
    _risk(live, max_entry_price_deviation_pct=None)
    live.kite.ltp_map["NSE:TCS"] = 3500.0
    assert _confirm(live, _entry(live)).status_code == 200
    live.kite.ltp_map["NSE:TCS"] = 2800.0
    assert _code(_confirm(live, _entry(live))) == "STOP_ALREADY_INVALID"


def test_12d_exit_is_unaffected_by_price_checks(live):
    _, sell_id = _executed_exit(live)
    live.kite.ltp_map["NSE:TCS"] = 1000.0              # far from the exit's 3100 reference, through the stop
    r = live.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_12d_dry_run_entry_makes_no_price_check(env, market_open):
    env.kite.ltp_map["NSE:TCS"] = 5000.0
    r = _confirm(env, _entry(env))
    assert r.status_code == 200 and r.json()["data"]["mode"] == "dry_run"
    assert env.kite.calls_to("ltp") == []


# ================================================================= PK-12E stale strategy signal

def _previous_session(d):
    from backend import market
    d -= timedelta(days=1)
    while not market.is_trading_day(d):
        d -= timedelta(days=1)
    return d


def test_12e_current_signal_is_allowed(live):
    tid = _strategy_entry(live, _session().isoformat())
    r = _confirm(live, tid)
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_12e_old_signal_is_refused_and_rejected(live):
    tid = _strategy_entry(live, _previous_session(_session()).isoformat())
    mark = len(live.kite.calls)
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "STALE_SIGNAL"
    assert _calls_since(live, mark) == []
    row = ticket_row(live.db, tid)
    assert row["status"] == "rejected" and row["submission_state"] is None
    assert row["entry_price"] == 3000.0 and row["signal_date"] == _previous_session(_session()).isoformat()
    assert "STALE_SIGNAL" in _sql(live, "SELECT detail FROM ticket_events WHERE ticket_id = ? "
                                        "AND event_type = 'rejected'", tid)[0]["detail"]


def test_12e_stale_signal_is_refused_in_dry_run_too(env, market_open):
    tid = _strategy_entry(env, _previous_session(_session()).isoformat())
    r = _confirm(env, tid)
    assert r.status_code == 409 and _code(r) == "STALE_SIGNAL"
    assert ticket_row(env.db, tid)["status"] == "rejected"


def test_12e_missing_signal_date_falls_back_to_creation_date(live):
    tid = _strategy_entry(live, None, created_at="2026-01-05 10:30:00")
    assert _code(_confirm(live, tid)) == "STALE_SIGNAL"


def test_12e_manual_ticket_semantics_are_preserved(live):
    # A manual ticket has no signal candle: however old, it is not a stale signal.
    tid = _insert_ticket(live.db, status="pending_confirmation", mode=None, risk_check_results="[]",
                         created_at="2026-01-05 10:30:00")
    r = _confirm(live, tid)
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


FRI = date(2026, 9, 25)


@pytest.mark.parametrize("now,valid", [
    (datetime(2026, 9, 25, 16, 30, tzinfo=IST), True),    # Friday evening, after the scan
    (datetime(2026, 9, 26, 11, 0, tzinfo=IST), True),     # Saturday
    (datetime(2026, 9, 27, 20, 0, tzinfo=IST), True),     # Sunday
    (datetime(2026, 9, 28, 10, 0, tzinfo=IST), True),     # Monday, in session
    (datetime(2026, 9, 28, 15, 29, tzinfo=IST), True),    # Monday, last minute
    (datetime(2026, 9, 28, 15, 30, tzinfo=IST), False),   # Monday's candle is complete
    (datetime(2026, 9, 29, 10, 0, tzinfo=IST), False),    # Tuesday
], ids=["fri_evening", "saturday", "sunday", "monday_session", "monday_1529", "monday_close", "tuesday"])
def test_12e_weekend_boundary(now, valid):
    from backend import account_safety
    ticket = {"source": "strategy", "signal_date": FRI.isoformat(), "created_at": "2026-09-25 10:40:00"}
    failure = account_safety.check_stale_signal(ticket, now)
    assert (failure is None) is valid
    if not valid:
        assert failure.code == "STALE_SIGNAL" and failure.permanent


def test_12e_thursday_signal_is_stale_over_the_weekend():
    from backend import account_safety
    ticket = {"source": "strategy", "signal_date": "2026-09-24", "created_at": "2026-09-24 10:40:00"}
    assert account_safety.check_stale_signal(ticket, datetime(2026, 9, 26, 11, 0, tzinfo=IST)).code == "STALE_SIGNAL"


def test_12e_signal_not_refreshed_on_refusal(live):
    old = _previous_session(_session()).isoformat()
    tid = _strategy_entry(live, old, entry_price=3000.0, stop_loss=2900.0)
    _confirm(live, tid)
    row = ticket_row(live.db, tid)
    assert (row["signal_date"], row["entry_price"], row["stop_loss"], row["target_1"]) == (old, 3000.0, 2900.0, 3300.0)


# ================================================================= PK-12F daily loss limit

def _today_ist():
    from backend import account_safety
    return account_safety.now_ist().date()


def _closed_trade(env, pnl, mode="live", exit_date=None):
    """A closed_trades row (the journal's realized P&L) for a finished trade."""
    exit_date = exit_date or _today_ist().isoformat()
    buy = _insert_ticket(env.db, mode=mode, closed_at=f"{exit_date} 05:00:00")
    sell = _insert_ticket(env.db, mode=mode, transaction_type="SELL", closes_ticket_id=buy)
    _sql(env, """
        INSERT INTO closed_trades (buy_ticket_id, sell_ticket_id, account_id, symbol, source, mode, entry_price,
            exit_price, quantity, realized_pnl, realized_pnl_pct, entry_date, exit_date,
            holding_period_calendar_days, holding_period_trading_days, exit_reason)
        VALUES (?, ?, ?, 'TCS', 'manual', ?, 3000, 3000, 5, ?, 0, ?, ?, 0, 0, 'stop_loss')
    """, buy, sell, account_id(env.db), mode, pnl, exit_date, exit_date)


def test_12f_below_the_limit_is_allowed(live):
    _risk(live, max_daily_loss_amount=1000.0)
    _closed_trade(live, -999.0)
    r = _confirm(live, _entry(live))
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


@pytest.mark.parametrize("pnl", [-1000.0, -1500.0], ids=["exactly_at", "beyond"])
def test_12f_limit_reached_blocks_new_entries_without_broker_calls(live, pnl):
    _risk(live, max_daily_loss_amount=1000.0)
    _closed_trade(live, pnl)
    tid = _entry(live)
    mark = len(live.kite.calls)
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "DAILY_LOSS_LIMIT"
    assert _calls_since(live, mark) == []
    _assert_untouched_pending(live, tid)
    assert _status(live)["daily_loss_limit_hit"] is True


def test_12f_net_pnl_counts_wins_against_losses(live):
    _risk(live, max_daily_loss_amount=1000.0)
    _closed_trade(live, -1500.0)
    _closed_trade(live, 800.0)
    assert _confirm(live, _entry(live)).status_code == 200


def test_12f_exits_remain_allowed_when_the_limit_is_hit(live):
    _risk(live, max_daily_loss_amount=1000.0)
    _closed_trade(live, -5000.0)
    _, sell_id = _executed_exit(live)
    r = live.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_12f_dry_run_pnl_is_excluded(live):
    _risk(live, max_daily_loss_amount=1000.0)
    _closed_trade(live, -50000.0, mode="dry_run")
    assert _status(live)["realized_live_pnl_today"] == 0
    assert _confirm(live, _entry(live)).status_code == 200


def test_12f_next_trading_day_resets(live, monkeypatch):
    from backend import account_safety
    _risk(live, max_daily_loss_amount=1000.0)
    _closed_trade(live, -5000.0)
    tid = _entry(live)
    assert _code(_confirm(live, tid)) == "DAILY_LOSS_LIMIT"
    tomorrow = account_safety.now_ist() + timedelta(days=1)
    monkeypatch.setattr(account_safety, "now_ist", lambda: tomorrow)
    r = _confirm(live, tid)
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_12f_percentage_limit_is_checked_at_the_final_gate(live):
    # Portfolio value = holdings 10 x 1300 + margin 1,000,000 = 1,013,000; 1% = 10,130.
    _risk(live, max_daily_loss_pct=1.0)
    _closed_trade(live, -10130.0)
    tid = _entry(live)
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "DAILY_LOSS_LIMIT"
    assert _orders(live) == [] and live.kite.calls_to("ltp") == []     # no price lookup after a hard failure
    _assert_untouched_pending(live, tid)


def test_12f_tighter_of_amount_and_percentage_applies(live):
    _risk(live, max_daily_loss_amount=20000.0, max_daily_loss_pct=1.0)  # 1% = 10,130 is tighter
    _closed_trade(live, -15000.0)
    assert _code(_confirm(live, _entry(live))) == "DAILY_LOSS_LIMIT"
    _risk(live, max_daily_loss_amount=5000.0, max_daily_loss_pct=50.0)  # now the rupee limit is tighter
    assert _code(_confirm(live, _entry(live))) == "DAILY_LOSS_LIMIT"


def test_12f_unusable_portfolio_value_fails_closed_when_pct_is_enabled(live):
    _risk(live, max_daily_loss_pct=1.0)
    live.kite.margins_net = -13000.0                      # portfolio value 0
    tid = _entry(live)
    r = _confirm(live, tid)
    assert r.status_code == 503 and _code(r) == "ACCOUNT_SAFETY_UNAVAILABLE"
    assert _orders(live) == []


def test_12f_pnl_that_cannot_be_computed_fails_closed(live, monkeypatch):
    from backend import account_safety
    _risk(live, max_daily_loss_amount=1000.0)

    def broken(*_a, **_k):
        raise RuntimeError("journal unreadable")
    monkeypatch.setattr(account_safety, "realized_live_pnl_today", broken)
    r = _confirm(live, _entry(live))
    assert r.status_code == 503 and _code(r) == "ACCOUNT_SAFETY_UNAVAILABLE"
    assert _orders(live) == []


def test_12f_disabled_by_default(live):
    s = _status(live)
    assert s["max_daily_loss_amount"] is None and s["max_daily_loss_pct"] is None
    _closed_trade(live, -1_000_000.0)
    assert _confirm(live, _entry(live)).status_code == 200


def test_12f_lock_is_not_overwritten_by_the_loss_limit(live):
    _risk(live, max_daily_loss_amount=1000.0)
    _closed_trade(live, -5000.0)
    _confirm(live, _entry(live))
    assert _status(live)["trading_locked"] is False       # derived state, never flips the manual lock


# ================================================================= PK-12G broker circuit breaker

def _record_failures(env, n, source="test"):
    from backend import account_safety
    for i in range(n):
        account_safety.record_broker_failure(account_id(env.db), source, f"failure {i}")


def _reset_breaker(env, reason="broker recovered"):
    r = env.client.post("/api/account-safety/breaker/reset", json={"reason": reason})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_12g_failures_accumulate_and_trip_the_breaker_once(live):
    _record_failures(live, 2)
    s = _status(live)
    assert s["breaker_open"] is False and s["breaker_recent_failures"] == 2
    _record_failures(live, 2)
    s = _status(live)
    assert s["breaker_open"] is True and "threshold 3" in s["breaker_reason"]
    assert len(_sys_events(live, "broker_breaker_tripped")) == 1
    assert any(b.get("type") == "account_safety_changed" for b in live.broadcasts)


def test_12g_open_breaker_blocks_new_entries_without_broker_calls(live):
    _record_failures(live, 3)
    tid = _entry(live)
    mark = len(live.kite.calls)
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "BROKER_CIRCUIT_OPEN"
    assert _calls_since(live, mark) == []
    _assert_untouched_pending(live, tid)


def test_12g_unknown_order_outcomes_trip_the_breaker(live, unsafe_client):
    _risk(live, max_new_entries_per_day=None)
    for _ in range(3):
        tid = _entry(live)
        live.kite.raise_on_next("place_order", requests.ConnectionError("Connection aborted."))
        r = unsafe_client.post(f"/api/tickets/{tid}/confirm", json=displayed_mode(live.db))
        assert r.json()["data"]["submission_state"] == "unknown"
    assert _status(live)["breaker_open"] is True
    r = _confirm(live, _entry(live))
    assert _code(r) == "BROKER_CIRCUIT_OPEN"
    assert len(live.kite.calls_to("place_order")) == 3


def test_12g_risk_revalidation_broker_failure_counts(live):
    tid = _entry(live)
    live.kite.raise_on_next("margins", NetworkException("Gateway timed out"))
    r = _confirm(live, tid)
    assert r.status_code == 503 and _code(r) == "RISK_REVALIDATION_UNAVAILABLE"
    assert _status(live)["breaker_recent_failures"] == 1


@pytest.mark.parametrize("rejection", [OrderException("RMS: margin exceeds"), InputException("Invalid price")],
                         ids=["OrderException", "InputException"])
def test_12g_validation_and_rms_rejections_do_not_count(live, rejection):
    _risk(live, max_new_entries_per_day=None)
    for _ in range(4):
        live.kite.raise_on_next("place_order", rejection)
        assert _confirm(live, _entry(live)).json()["data"]["status"] == "failed"
    s = _status(live)
    assert s["breaker_open"] is False and s["breaker_recent_failures"] == 0


def test_12g_risk_rejections_and_lifecycle_refusals_do_not_count(live):
    t = live.client.post("/api/tickets", json=_manual_ticket(quantity=5000)).json()["data"]
    assert t["status"] == "risk_rejected"
    assert _confirm(live, t["id"]).status_code == 409          # lifecycle 409
    tid = _entry(live)
    assert _confirm(live, tid, expected_mode="dry_run").status_code == 409
    assert _status(live)["breaker_recent_failures"] == 0


def test_12g_a_broker_success_restarts_the_failure_window(live):
    _record_failures(live, 2)
    assert _confirm(live, _entry(live)).status_code == 200    # the broker accepted an order
    _record_failures(live, 2)
    s = _status(live)
    assert s["breaker_open"] is False and s["breaker_recent_failures"] == 2


def test_12g_failures_outside_the_window_decay(live, monkeypatch):
    from backend import account_safety
    _record_failures(live, 2)
    later = account_safety.now_ist() + timedelta(minutes=11)   # default window 10 min
    monkeypatch.setattr(account_safety, "now_ist", lambda: later)
    _record_failures(live, 1)
    s = _status(live)
    assert s["breaker_open"] is False and s["breaker_recent_failures"] == 1


def test_12g_success_does_not_close_an_open_breaker(live):
    from backend import account_safety
    _record_failures(live, 3)
    account_safety.record_broker_success(account_id(live.db))
    assert _status(live)["breaker_open"] is True


def test_12g_exits_remain_possible_while_the_breaker_is_open(live):
    _record_failures(live, 3)
    _, sell_id = _executed_exit(live)
    r = live.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_12g_open_breaker_survives_restart_and_manual_reset_is_logged(live):
    _record_failures(live, 3)
    live.db.init_db()
    assert _status(live)["breaker_open"] is True
    data = _reset_breaker(live, "Kite status page green again")
    assert data["changed"] is True and data["breaker_open"] is False and data["breaker_recent_failures"] == 0
    [reset] = _sys_events(live, "broker_breaker_reset")
    assert "Kite status page green again" in reset["detail"]
    assert _reset_breaker(live)["changed"] is False            # closed already: no second event
    assert len(_sys_events(live, "broker_breaker_reset")) == 1
    r = _confirm(live, _entry(live))
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_12g_disabled_breaker_never_trips(live):
    _risk(live, broker_breaker_threshold=None)
    _record_failures(live, 10)
    assert _status(live)["breaker_open"] is False


@pytest.mark.parametrize("outcome", ["accepted", "unknown"])
def test_12g_bookkeeping_failure_never_changes_an_order_outcome(live, unsafe_client, monkeypatch, outcome):
    from backend import account_safety
    tid = _entry(live)

    def broken():
        raise RuntimeError("database is locked")
    # The breaker's own connection (success / failure recording) is unusable.
    monkeypatch.setattr(account_safety, "get_connection", broken)
    if outcome == "unknown":
        live.kite.raise_on_next("place_order", requests.ConnectionError("Connection aborted."))
    r = unsafe_client.post(f"/api/tickets/{tid}/confirm", json=displayed_mode(live.db))
    assert r.status_code == 200, r.text
    row = ticket_row(live.db, tid)
    if outcome == "accepted":
        assert row["status"] == "executed" and row["submission_state"] == "acknowledged"
    else:
        assert row["status"] == "confirmed" and row["submission_state"] == "unknown"


@pytest.fixture
def unsafe_client(env):
    from backend.main import app
    from conftest import terminal_client
    return terminal_client(app, raise_server_exceptions=False)


# ================================================================= PK-12H market hours / AMO consent

@pytest.fixture
def closed(env, monkeypatch):
    from backend import market
    monkeypatch.setattr(market, "is_market_open", lambda at=None: False)
    set_mode(env.db, "live")
    return env


def test_12h_market_open_entry_is_a_regular_order(live):
    r = _confirm(live, _entry(live))
    assert r.status_code == 200 and r.json()["data"]["order_variety"] == "regular"
    assert live.kite.calls_to("place_order")[0]["variety"] == "regular"


@pytest.mark.parametrize("body", [{}, {"allow_amo": False}], ids=["missing", "false"])
def test_12h_closed_market_entry_without_explicit_amo_is_refused(closed, body):
    tid = _entry(closed)
    mark = len(closed.kite.calls)
    r = _confirm(closed, tid, **body)
    assert r.status_code == 409 and _code(r) == "AMO_CONFIRMATION_REQUIRED"
    assert _calls_since(closed, mark) == []
    _assert_untouched_pending(closed, tid)
    [refusal] = _sys_events(closed, "account_safety_refusal")
    assert "AMO_CONFIRMATION_REQUIRED" in refusal["detail"]


def test_12h_closed_market_entry_with_explicit_amo_is_sent_as_amo(closed):
    tid = _entry(closed)
    r = _confirm(closed, tid, allow_amo=True)
    assert r.status_code == 200 and r.json()["data"]["order_variety"] == "amo"
    assert closed.kite.calls_to("place_order")[0]["variety"] == "amo"
    [approved] = _sys_events(closed, "amo_entry_approved")
    assert f"Ticket {tid}" in approved["detail"] and "explicit consent" in approved["detail"]


def test_12h_stale_tab_cannot_silently_become_amo(closed):
    # The tab still showed the market open (no AMO checkbox, allow_amo false)
    # when the user clicked; the server's clock decides.
    tid = _entry(closed)
    r = closed.client.post(f"/api/tickets/{tid}/confirm", json={"expected_mode": "live", "allow_amo": False})
    assert _code(r) == "AMO_CONFIRMATION_REQUIRED"
    assert closed.kite.calls_to("place_order") == []


def test_12h_consent_during_market_hours_never_makes_an_amo(live):
    r = _confirm(live, _entry(live), allow_amo=True)
    assert r.json()["data"]["order_variety"] == "regular"
    assert _sys_events(live, "amo_entry_approved") == []


def test_12h_variety_decided_by_the_gate_is_bound_to_execution(env, monkeypatch):
    """The market is open at the gate and closes before place_order: the
    entry goes out as the REGULAR order the gate decided (Kite would reject
    it — a definite, safe outcome), never silently re-routed into an AMO."""
    from backend import market
    set_mode(env.db, "live")
    tid = _entry(env)
    answers = iter([True])
    monkeypatch.setattr(market, "is_market_open", lambda at=None: next(answers, False))
    r = _confirm(env, tid)
    assert r.status_code == 200
    assert env.kite.calls_to("place_order")[0]["variety"] == "regular"


def test_12h_exit_outside_hours_keeps_its_existing_amo_behaviour(closed):
    _, sell_id = _executed_exit(closed)
    r = closed.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200 and r.json()["data"]["order_variety"] == "amo"


def test_12h_emergency_exit_outside_hours_is_not_blocked(closed):
    _live_position(closed)
    r = closed.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    assert [x["status"] for x in r.json()["data"]["results"]] == ["exit_submitted"]


def test_12h_dry_run_entry_outside_hours_is_simulated_without_amo(env, monkeypatch):
    from backend import market
    monkeypatch.setattr(market, "is_market_open", lambda at=None: False)
    r = _confirm(env, _entry(env))
    assert r.status_code == 200 and r.json()["data"]["mode"] == "dry_run"
    assert r.json()["data"]["order_variety"] is None
    assert _orders(env) == [] and env.kite.calls_to("ltp") == []


def test_12h_amo_disabled_in_configuration_refuses_even_with_consent(closed, monkeypatch):
    from backend import config
    monkeypatch.setattr(config, "USE_AMO_WHEN_MARKET_CLOSED", False)
    r = _confirm(closed, _entry(closed), allow_amo=True)
    assert r.status_code == 409 and _code(r) == "AMO_DISABLED"
    assert closed.kite.calls_to("place_order") == []


def test_12h_invalid_allow_amo_is_rejected(live):
    r = live.client.post(f"/api/tickets/{_entry(live)}/confirm", json={"expected_mode": "live", "allow_amo": "maybe"})
    assert r.status_code == 422


def test_12h_live_entry_without_a_decided_variety_never_reaches_the_broker():
    from backend import execution
    from fake_kite import FakeKite
    kite = FakeKite()
    ticket = {"mode": "live", "transaction_type": "BUY", "closes_ticket_id": None, "source": "manual"}
    with pytest.raises(ValueError):
        execution.execute_ticket(kite, ticket)
    assert kite.calls == []


def test_12h_status_reports_market_and_amo_requirement(closed):
    s = _status(closed)
    assert s["market_open"] is False and s["amo_confirmation_required"] is True
