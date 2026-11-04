"""Order-path assurance (2026-11-04 final engineering sprint).

One file that walks the whole order lifecycle - ticket creation, P1-D review, account and session
validation, calendar and market session, funds and risk, quantity and price, duplicate protection,
the broker call, acceptance versus fill, reconciliation - through the real HTTP routes, so the
sprint's scenario list maps onto concrete, named tests (docs/KITE_TERMINAL_BLUEPRINT.html, section
"Order-path assurance"). Many scenarios are also covered in depth elsewhere; this file is the index.

Two broker stand-ins, never the network:
- FakeKite (conftest `env`): the broker's state machine (order book, GTT book, fills).
- The NET-1 wire recorder (test_kite_rest_proxy): a REAL KiteConnect built by the production
  factory, with requests' HTTPAdapter.send replaced. Used for the KITE_TERMINAL_BROKER_WRITES
  end-to-end tests, which prove that with writes disabled no POST/PUT/DELETE ever reaches the wire
  from the confirm route, Emergency Exit, or the cancel routes - only proxied GETs.

FakeKite and synthetic tests do not prove real broker order acceptance or execution. They prove
what this process does with each broker answer; what Zerodha answers is not tested here.
"""

from datetime import datetime, timedelta, timezone

import pytest
import requests
from kiteconnect.exceptions import InputException, OrderException

from backend import kite_client
from conftest import account_id, approval_body, set_mode, ticket_row
from test_baseline_api import _insert_ticket, _manual_ticket
from test_kite_rest_proxy import PROXY_MAP, proxy_env, real_kite, wire  # noqa: F401 - fixtures
from test_live_critical_gate import calendar_not_covered


# ---------------------------------------------------------------- helpers

def _create(env, **overrides) -> dict:
    r = env.client.post("/api/tickets", json=_manual_ticket(**overrides))
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _confirm(env, ticket_id: int, **body):
    payload = approval_body(env.db, ticket_id)
    payload.update(body)
    return env.client.post(f"/api/tickets/{ticket_id}/confirm", json=payload)


def _error(r) -> str:
    return r.json()["detail"]["error_code"]


def _codes(r) -> set[str]:
    detail = r.json()["detail"]
    return {f["code"] for f in detail.get("failures", [])} | {detail["error_code"]}


def _sql(env, statement: str, params=()) -> None:
    conn = env.db.get_connection()
    try:
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def _nothing_sent(env) -> None:
    for name in ("place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt"):
        assert env.kite.calls_to(name) == [], name


def _monitor(env) -> None:
    from backend import order_monitor
    order_monitor.check_pending_orders(kite=env.kite)


def _live_order(env) -> tuple[int, str]:
    """A live manual BUY 5 TCS accepted by the broker and not filled: (ticket id, order id)."""
    set_mode(env.db, "live")
    env.kite.auto_fill = False
    t = _create(env)
    r = _confirm(env, t["id"])
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "executed", data
    return t["id"], data["kite_order_id"]


# ---------------------------------------------------------------- 1. valid reviewed ticket; acceptance is not a fill

def test_01_valid_reviewed_ticket_is_accepted_but_not_filled(env, market_open):
    ticket_id, order_id = _live_order(env)
    row = ticket_row(env.db, ticket_id)
    assert row["submission_state"] == "acknowledged" and row["kite_order_id"] == order_id
    # Broker acceptance is never presented as a fill: no fill price, time or quantity yet.
    assert row["entry_fill_status"] == "pending"
    assert row["filled_quantity"] == 0 and row["filled_price"] is None and row["filled_at"] is None
    assert len(env.kite.calls_to("place_order")) == 1
    order = env.kite.calls_to("place_order")[0]
    assert (order["tradingsymbol"], order["exchange"], order["transaction_type"], order["quantity"],
            order["product"], order["order_type"], order["price"]) == ("TCS", "NSE", "BUY", 5, "CNC", "LIMIT", 3000.0)
    assert order["tag"] == row["order_tag"]
    assert len(env.kite.calls_to("place_gtt")) == 1        # protective GTT placed with the entry


# ---------------------------------------------------------------- 2-6. P1-D review binding

def test_02_missing_review_token_is_refused_before_the_claim(env, market_open):
    set_mode(env.db, "live")
    t = _create(env)
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json={"expected_mode": "live"})
    assert r.status_code == 409 and "TICKET_REVIEW_CHANGED" in _codes(r)
    assert ticket_row(env.db, t["id"])["status"] == "pending_confirmation"
    _nothing_sent(env)


def test_03_expired_review_needs_a_re_review(env, market_open):
    set_mode(env.db, "live")
    t = _create(env)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    _sql(env, "UPDATE trade_tickets SET reviewed_at = ? WHERE id = ?", (old, t["id"]))
    r = _confirm(env, t["id"])                 # the token of the stored (old) review
    assert r.status_code == 409 and "TICKET_REVIEW_REQUIRED" in _codes(r)
    assert ticket_row(env.db, t["id"])["status"] == "pending_confirmation"
    _nothing_sent(env)


def test_04_ticket_modified_outside_the_edit_path_is_rejected_for_good(env, market_open):
    set_mode(env.db, "live")
    t = _create(env)
    _sql(env, "UPDATE trade_tickets SET quantity = 500 WHERE id = ?", (t["id"],))
    r = _confirm(env, t["id"])
    assert r.status_code == 409 and "TICKET_EVIDENCE_MISMATCH" in _codes(r)
    assert ticket_row(env.db, t["id"])["status"] == "rejected"
    _nothing_sent(env)


def test_05_a_view_of_an_earlier_review_cannot_approve_after_a_re_review(env, market_open):
    """Only drafts are editable (PUT refuses a pending ticket), so the stale-view case is a
    Re-review in another tab: it issues a new review generation, and the old view's token is dead."""
    set_mode(env.db, "live")
    t = _create(env)
    stale = approval_body(env.db, t["id"])
    assert env.client.put(f"/api/tickets/{t['id']}", json=_manual_ticket(quantity=6)).status_code == 409
    r = env.client.post(f"/api/tickets/{t['id']}/re-review")
    assert r.status_code == 200, r.text
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=stale)
    assert r.status_code == 409 and "TICKET_REVIEW_CHANGED" in _codes(r)
    _nothing_sent(env)
    # The current view approves it exactly once.
    assert _confirm(env, t["id"]).status_code == 200
    assert _confirm(env, t["id"]).status_code == 409
    assert [o["quantity"] for o in env.kite.calls_to("place_order")] == [5]


def test_06_a_review_from_an_earlier_kite_login_is_not_current(env, market_open):
    t = _create(env)
    conn = env.db.get_connection()
    try:
        acct = account_id(env.db)
        conn.execute("UPDATE sessions SET is_active = 0 WHERE account_id = ?", (acct,))
        conn.execute("INSERT INTO sessions (access_token, user_id, user_name, login_time, account_id) "
                     "VALUES ('', 'DT0001', 'Demo Trader', ?, ?)", (datetime.now(timezone.utc).isoformat(), acct))
        conn.commit()
    finally:
        conn.close()
    r = _confirm(env, t["id"])                 # dry run: isolates P1-D from LIVE arming
    assert r.status_code == 409 and "TICKET_REVIEW_REQUIRED" in _codes(r)
    assert env.kite.calls_to("place_order") == []


def test_06b_live_is_disarmed_by_a_new_kite_login(env, market_open):
    set_mode(env.db, "live")
    t = _create(env)
    acct = account_id(env.db)
    _sql(env, "UPDATE sessions SET is_active = 0 WHERE account_id = ?", (acct,))
    _sql(env, "INSERT INTO sessions (access_token, user_id, user_name, login_time, account_id) "
              "VALUES ('', 'DT0001', 'Demo Trader', ?, ?)", (datetime.now(timezone.utc).isoformat(), acct))
    r = _confirm(env, t["id"])
    assert r.status_code in (409, 503)
    assert {"LIVE_NOT_ARMED_FOR_SESSION", "TICKET_REVIEW_REQUIRED"} <= _codes(r)
    _nothing_sent(env)


def test_07_a_ticket_of_another_account_is_not_found(env, market_open):
    t = _create(env)
    conn = env.db.get_connection()
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("UPDATE trade_tickets SET account_id = 9999 WHERE id = ?", (t["id"],))
        conn.commit()
    finally:
        conn.close()
    r = _confirm(env, t["id"])
    assert r.status_code == 404
    _nothing_sent(env)


# ---------------------------------------------------------------- 8-9. instrument, quantity, price

@pytest.mark.parametrize("symbol, exchange", [("NOSUCHSYMBOL", "NSE"), ("TCS", "XYZ"), ("TCS", "BSE")])
def test_08_unknown_instrument_or_exchange_is_refused_at_creation(env, symbol, exchange):
    r = env.client.post("/api/tickets", json=_manual_ticket(symbol=symbol, exchange=exchange))
    assert r.status_code == 400 and _error(r) == "INSTRUMENT_NOT_FOUND"


@pytest.mark.parametrize("field, value", [("quantity", 0), ("quantity", -5), ("quantity", 1.5),
                                          ("entry_price", 0), ("entry_price", -1), ("stop_loss", 0),
                                          ("target_1", 0), ("transaction_type", "SHORT"),
                                          ("entry_order_type", "SL-M"), ("product_type", "BO")])
def test_09_invalid_quantity_price_or_order_shape_is_refused_by_the_server(env, field, value):
    r = env.client.post("/api/tickets", json=_manual_ticket(**{field: value}))
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("overrides", [dict(transaction_type="SELL"), dict(product_type="MIS"),
                                       dict(product_type="NRML")])
def test_09b_only_a_long_cnc_entry_can_open_a_position(env, overrides):
    r = env.client.post("/api/tickets", json=_manual_ticket(**overrides))
    assert r.status_code in (400, 409, 422) and "UNSUPPORTED_MANUAL_TRADE" in r.text


def test_09c_a_stop_on_the_wrong_side_of_a_buy_is_refused(env):
    r = env.client.post("/api/tickets", json=_manual_ticket(stop_loss=3100.0))
    assert r.status_code in (400, 422), r.text


def test_09d_order_value_above_the_absolute_cap_is_refused_live(env, market_open):
    set_mode(env.db, "live")
    _sql(env, "UPDATE risk_settings SET max_order_notional = 1000")
    t = _create(env)
    r = _confirm(env, t["id"])
    assert r.status_code == 409 and "MAX_NOTIONAL_EXCEEDED" in _codes(r)
    _nothing_sent(env)


def test_09e_price_far_from_the_market_is_refused_before_the_broker_call(env, market_open):
    set_mode(env.db, "live")
    _sql(env, "UPDATE risk_settings SET max_entry_price_deviation_pct = 2")
    t = _create(env)
    env.kite.ltp_map["NSE:TCS"] = 3200.0       # 6.7% away from the 3000 limit
    r = _confirm(env, t["id"])
    assert r.status_code == 409 and "PRICE_DEVIATION_EXCEEDED" in _codes(r)
    assert env.kite.calls_to("place_order") == []


# ---------------------------------------------------------------- 10. funds and margin

def test_10_insufficient_portfolio_for_the_capital_limit_is_risk_rejected(env, market_open):
    env.kite.margins_net = 10_000.0            # portfolio ~23,000: a 15,000 order is ~65% of it
    t = _create(env)
    assert t["status"] == "risk_rejected"
    assert any(c["rule"] == "max_capital_per_trade_pct" and not c["passed"] for c in t["risk_check_results"])
    _nothing_sent(env)


def test_10b_broker_rms_rejection_for_funds_never_becomes_a_position(env, market_open):
    """There is no local 'order value <= available cash' check: Kite's RMS is the backstop. Its
    asynchronous REJECTED is followed to the end: no fill is invented and the GTT is removed."""
    ticket_id, order_id = _live_order(env)
    env.kite.set_order_state(order_id, "REJECTED", filled_quantity=0, status_message="Insufficient funds")
    _monitor(env)
    row = ticket_row(env.db, ticket_id)
    assert row["status"] == "cancelled" and row["filled_quantity"] == 0 and row["filled_price"] is None
    assert len(env.kite.calls_to("delete_gtt")) == 1


def test_10c_synchronous_input_rejection_is_a_definite_failure(env, market_open):
    set_mode(env.db, "live")
    t = _create(env)
    env.kite.raise_on_next("place_order", InputException("Insufficient funds. Required margin is 15000"))
    r = _confirm(env, t["id"])
    assert r.status_code == 200 and r.json()["data"]["success"] is False
    row = ticket_row(env.db, t["id"])
    assert row["status"] == "failed" and row["submission_state"] == "rejected"
    assert env.kite.calls_to("place_gtt") == []


# ---------------------------------------------------------------- 11-12. market session and calendar

def test_11_market_closed_needs_explicit_amo_consent(env, monkeypatch):
    from backend import market
    monkeypatch.setattr(market, "is_market_open", lambda at=None: False)
    set_mode(env.db, "live")
    t = _create(env)
    r = _confirm(env, t["id"])
    assert r.status_code == 409 and "AMO_CONFIRMATION_REQUIRED" in _codes(r)
    assert ticket_row(env.db, t["id"])["status"] == "pending_confirmation"
    _nothing_sent(env)


def test_12_calendar_not_covering_today_fails_closed(env, market_open, monkeypatch):
    set_mode(env.db, "live")
    t = _create(env)
    calendar_not_covered(env, monkeypatch)
    r = _confirm(env, t["id"])
    assert r.status_code == 503 and "LIVE_STATE_UNVERIFIED" in _codes(r)
    _nothing_sent(env)


# ---------------------------------------------------------------- 13. proxy unavailable

@pytest.mark.parametrize("exc", [requests.exceptions.ProxyError("Unable to connect to proxy"),
                                 requests.exceptions.ConnectTimeout("connect timed out")])
def test_13_proxy_down_before_the_order_sends_nothing(env, market_open, proxy_env, exc):  # noqa: F811
    set_mode(env.db, "live")
    t = _create(env)
    env.kite.raise_on_next("holdings", exc)
    r = _confirm(env, t["id"])
    assert r.status_code == 503 and _error(r) == "RISK_REVALIDATION_UNAVAILABLE"
    _nothing_sent(env)


def test_13b_invalid_proxy_setting_refuses_every_broker_route(env, monkeypatch):
    from test_kite_rest_proxy import REAL_GET_KITE, _with_token
    _with_token(env)
    monkeypatch.setattr(kite_client, "get_kite", REAL_GET_KITE)
    monkeypatch.setenv("KITE_REST_PROXY", "socks5://10.77.0.1:1080")
    r = env.client.post("/api/tickets", json=_manual_ticket())
    assert r.status_code == 503 and _error(r) == "KITE_REST_PROXY_INVALID"


# ---------------------------------------------------------------- 14-16. broker rejection, timeout, ambiguity

def test_14_broker_rejection_is_definite_and_places_no_gtt(env, market_open):
    set_mode(env.db, "live")
    t = _create(env)
    env.kite.raise_on_next("place_order", OrderException("RMS: blocked for this instrument"))
    r = _confirm(env, t["id"])
    row = ticket_row(env.db, t["id"])
    assert r.status_code == 200 and row["status"] == "failed" and row["submission_state"] == "rejected"
    assert env.kite.calls_to("place_gtt") == []


def test_15_timeout_is_unknown_never_retried_and_never_resubmittable(env, market_open):
    set_mode(env.db, "live")
    t = _create(env)
    env.kite.raise_on_next("place_order", requests.exceptions.ReadTimeout("read timed out"))
    first = _confirm(env, t["id"])
    assert "DO NOT RESUBMIT" in first.json()["data"]["execution_message"]
    assert ticket_row(env.db, t["id"])["submission_state"] == "unknown"
    again = _confirm(env, t["id"])
    assert again.status_code == 409
    _monitor(env)                               # still searching: nothing placed again
    assert len(env.kite.calls_to("place_order")) == 1
    assert ticket_row(env.db, t["id"])["submission_state"] == "unknown"


def test_15b_an_unknown_live_outcome_blocks_further_live_entries(env, market_open):
    set_mode(env.db, "live")
    t = _create(env)
    env.kite.raise_on_next("place_order", requests.exceptions.ReadTimeout("read timed out"))
    _confirm(env, t["id"])
    other = _create(env, symbol="INFY", entry_price=1500.0, stop_loss=1450.0, target_1=1650.0)
    r = _confirm(env, other["id"])
    assert r.status_code == 409 and "LIVE_PREREQUISITE_MISSING" in _codes(r)
    assert len(env.kite.calls_to("place_order")) == 1


def test_16_ambiguous_response_is_resolved_by_the_order_tag_not_by_resubmitting(env, market_open):
    set_mode(env.db, "live")
    env.kite.auto_fill = False
    t = _create(env)
    env.kite.accept_then_raise("place_order", requests.exceptions.ReadTimeout("response lost"))
    _confirm(env, t["id"])
    assert ticket_row(env.db, t["id"])["submission_state"] == "unknown"
    _monitor(env)
    row = ticket_row(env.db, t["id"])
    assert row["submission_state"] == "adopted" and row["status"] == "executed"
    assert row["kite_order_id"] in env.kite.order_book
    assert len(env.kite.calls_to("place_order")) == 1


# ---------------------------------------------------------------- 17. duplicate submission

def test_17_a_second_confirmation_never_sends_a_second_order(env, market_open):
    ticket_id, _ = _live_order(env)
    for _ in range(3):
        r = _confirm(env, ticket_id)
        assert r.status_code == 409
    assert len(env.kite.calls_to("place_order")) == 1 and len(env.kite.calls_to("place_gtt")) == 1


# ---------------------------------------------------------------- 18-19. partial and full fill

def test_18_partial_fill_is_recorded_as_partial_not_filled(env, market_open):
    ticket_id, order_id = _live_order(env)
    env.kite.set_order_state(order_id, "OPEN", filled_quantity=2, average_price=3000.0)
    _monitor(env)
    row = ticket_row(env.db, ticket_id)
    assert row["filled_quantity"] == 2 and row["entry_fill_status"] == "pending"


def test_19_full_fill_records_the_brokers_price_and_quantity(env, market_open):
    ticket_id, order_id = _live_order(env)
    env.kite.set_order_state(order_id, "COMPLETE", filled_quantity=5, average_price=2999.5)
    _monitor(env)
    row = ticket_row(env.db, ticket_id)
    assert row["entry_fill_status"] == "filled" and row["filled_quantity"] == 5
    assert row["filled_price"] == 2999.5 and row["filled_at"] is not None


# ---------------------------------------------------------------- 20. reconciliation mismatch

def test_20_broker_shortfall_is_a_finding_never_an_invented_fill(env, market_open):
    from backend import reconciliation
    position = _insert_ticket(env.db, symbol="INFY", instrument_token=408065, entry_price=1500.0,
                              stop_loss=1450.0, target_1=1650.0, executed_at="2026-09-01 04:00:00",
                              entry_fill_status="filled", filled_price=1500.0, filled_quantity=5,
                              filled_at="2026-09-01 04:00:05", kite_order_id="250000000777")
    before = ticket_row(env.db, position)
    reconciliation._run_cycle()                 # the broker holds no INFY at all
    conn = env.db.get_connection()
    try:
        findings = conn.execute("SELECT issue_code FROM reconciliation_log WHERE symbol = 'INFY' OR ticket_id = ?",
                                (position,)).fetchall()
    finally:
        conn.close()
    assert "BROKER_POSITION_SHORTFALL" in {f["issue_code"] for f in findings}
    after = ticket_row(env.db, position)
    for col in ("filled_quantity", "filled_price", "entry_fill_status", "closed_at", "quantity", "status"):
        assert after[col] == before[col], col
    set_mode(env.db, "live")
    t = _create(env)
    r = _confirm(env, t["id"])
    assert r.status_code == 409 and "RECONCILIATION_CRITICAL" in _codes(r)
    assert env.kite.calls_to("place_order") == []


# ---------------------------------------------------------------- 21. terminal session expiry between review and submission

@pytest.fixture
def clock(monkeypatch):
    from backend import security
    now = [10_000.0]

    class _Clock:
        def advance(self, seconds: float) -> None:
            now[0] += seconds
    monkeypatch.setattr(security, "_clock", lambda: now[0])
    monkeypatch.delenv(security.SESSION_ABSOLUTE_ENV, raising=False)
    monkeypatch.delenv(security.SESSION_IDLE_ENV, raising=False)
    return _Clock()


def test_21_terminal_session_expiry_between_review_and_submission_sends_nothing(clock, env, market_open):
    set_mode(env.db, "live")
    t = _create(env)
    clock.advance(31 * 60)                      # past the 30-minute idle timeout
    r = _confirm(env, t["id"])
    assert r.status_code == 401 and r.json()["detail"]["error_code"] == "TERMINAL_SESSION_REQUIRED"
    assert ticket_row(env.db, t["id"])["status"] == "pending_confirmation"
    _nothing_sent(env)


def test_21b_absolute_lifetime_expiry_cannot_be_kept_alive_by_activity(clock, env, market_open):
    set_mode(env.db, "live")
    t = _create(env)
    for _ in range(17):                         # 17 x 29 min of user activity > the 8 h lifetime
        clock.advance(29 * 60)
        env.client.get("/api/tickets", headers={"X-Kite-Terminal-Activity": "user"})
    r = _confirm(env, t["id"])
    assert r.status_code == 401
    _nothing_sent(env)


# ---------------------------------------------------------------- 22. KITE_TERMINAL_BROKER_WRITES=disabled, end to end on the wire

def _kite_replies(method: str, path: str):
    if path == "/user/margins":
        return {"equity": {"net": 1_000_000.0, "available": {"collateral": 0.0}}}
    if path == "/portfolio/holdings":
        return []
    if path == "/portfolio/positions":
        return {"net": [], "day": []}
    if path.startswith("/quote/ltp"):
        return {"NSE:TCS": {"instrument_token": 2953217, "last_price": 3000.0},
                "NSE:INFY": {"instrument_token": 408065, "last_price": 1500.0}}
    if path in ("/orders", "/gtt/triggers", "/trades"):
        return []
    if path == "/user/profile":
        return {"user_id": "DT0001", "user_name": "Demo Trader"}
    raise AssertionError(f"unexpected Kite call {method} {path}")


@pytest.fixture
def writes_disabled(real_kite, wire, monkeypatch):  # noqa: F811
    monkeypatch.setenv(kite_client.BROKER_WRITES_ENV, "disabled")
    wire.reply_for = _kite_replies
    return real_kite


def _only_proxied_reads(wire) -> None:
    assert wire.sends, "no request reached the wire"
    for s in wire.sends:
        assert s["method"] == "GET", s
        assert s["host"] == "api.kite.trade" and s["proxies"] == PROXY_MAP, s


def test_22_writes_disabled_live_confirm_sends_no_order(writes_disabled, wire, market_open):  # noqa: F811
    env = writes_disabled
    set_mode(env.db, "live")
    t = _create(env)
    assert t["status"] == "pending_confirmation", t
    r = _confirm(env, t["id"])
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["success"] is False and "never sent to Kite" in data["execution_message"]
    row = ticket_row(env.db, t["id"])
    assert row["status"] == "failed" and row["submission_state"] == "rejected" and row["kite_order_id"] is None
    _only_proxied_reads(wire)


def test_22b_writes_disabled_emergency_exit_sends_no_sell(writes_disabled, wire, market_open):  # noqa: F811
    env = writes_disabled
    position = _insert_ticket(env.db, executed_at="2026-09-01 04:00:00", entry_fill_status="filled",
                              filled_price=3000.0, filled_quantity=5, filled_at="2026-09-01 04:00:05",
                              kite_order_id="250000000778")
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    assert r.status_code == 200, r.text
    results = r.json()["data"]["results"]
    assert results and all("never sent to Kite" in (res.get("message") or "") for res in results), results
    assert ticket_row(env.db, position)["closed_at"] is None
    assert all(s["method"] == "GET" for s in wire.sends), wire.sends


@pytest.mark.parametrize("path", ["/api/gtts/9001/cancel", "/api/orders/250000000779/cancel"])
def test_22c_writes_disabled_cancel_routes_send_nothing(writes_disabled, wire, path):  # noqa: F811
    env = writes_disabled
    env.client.post(path)
    assert all(s["method"] == "GET" for s in wire.sends), wire.sends
