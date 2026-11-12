"""Instrument-specific margin preflight (backend/margin_preflight.py) for a LIVE new entry.

At the final gate, after the price checks: a fresh funds read (GET /user/margins/equity) and
Kite's own order-margin calculation (POST /margins/orders, which places nothing) for the exact
instrument, exchange, side, product, variety, order type and quantity. Available funds for a CNC
buy exclude pledged collateral; the requirement is Kite's figure plus charges, never assumed to
be the notional (the notional is only a floor for cash-equity CNC). Missing, stale or
inconsistent data refuses. Kite's RMS stays the final broker-side control.

M3: the order is never priced at 0. A LIMIT entry is sized at its limit price; a MARKET entry at
the instrument's upper circuit limit from a fresh quote (the worst price a MARKET BUY can fill
at), asked of Kite as the equivalent LIMIT. Charges are required and positive, never defaulted.

Behind the hard gate (lifted in-test by conftest `env`). FakeKite and scratch databases only.
"""

import math

import pytest
from kiteconnect.exceptions import NetworkException

from backend import account_safety, margin_preflight
from backend.kite_client import BrokerWritesDisabled
from conftest import approval_body, set_mode, ticket_row
from test_baseline_api import _insert_ticket, _manual_ticket

ORDER_VALUE = 5 * 3000.0  # 5 TCS at the FakeKite LTP
CHARGES = round(ORDER_VALUE * 0.001, 2)
UPPER_CIRCUIT = 3600.0    # FakeKite's default band: LTP + 20%
WORST_VALUE = 5 * UPPER_CIRCUIT  # what a MARKET BUY of 5 can cost at most
WORST_CHARGES = round(WORST_VALUE * 0.001, 2)


def _create(env, **overrides) -> dict:
    body = dict(entry_order_type="MARKET")
    body.update(overrides)
    r = env.client.post("/api/tickets", json=_manual_ticket(**body))
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _confirm(env, ticket_id: int):
    return env.client.post(f"/api/tickets/{ticket_id}/confirm", json=approval_body(env.db, ticket_id))


def _codes(r) -> set[str]:
    detail = r.json()["detail"]
    return {f["code"] for f in detail.get("failures", [])} | {detail["error_code"]}


def _live_confirm(env):
    set_mode(env.db, "live")
    t = _create(env)
    return t, _confirm(env, t["id"])


def _assert_refused(env, t, r, code, status):
    assert r.status_code == status, r.text
    assert code in _codes(r), r.text
    assert env.kite.calls_to("place_order") == [] and env.kite.calls_to("place_gtt") == []
    assert ticket_row(env.db, t["id"])["status"] == "pending_confirmation"


# ---------------------------------------------------------------- the gate at confirmation

def test_01_sufficient_funds_pass_with_kites_figure_for_the_exact_order(env, market_open):
    t, r = _live_confirm(env)
    assert r.status_code == 200, r.text
    assert env.kite.calls_to("margins")[-1] == {"segment": "equity"}
    (call,) = env.kite.calls_to("order_margins")
    (params,) = call["params"]
    # M3: never price 0 — the MARKET entry is asked about as a LIMIT at its upper circuit, from a fresh quote.
    assert env.kite.calls_to("quote")[-1] == {"keys": ["NSE:TCS"]}
    assert params == {"exchange": "NSE", "tradingsymbol": "TCS", "transaction_type": "BUY", "variety": "regular",
                      "product": "CNC", "order_type": "LIMIT", "quantity": 5, "price": UPPER_CIRCUIT}
    assert len(env.kite.calls_to("place_order")) == 1
    # The order actually sent is still the ticket's own MARKET order.
    assert env.kite.calls_to("place_order")[0]["order_type"] == "MARKET"


def test_02_insufficient_live_balance_refuses(env, market_open):
    env.kite.margins_live_balance = ORDER_VALUE - 1
    t, r = _live_confirm(env)
    _assert_refused(env, t, r, "INSUFFICIENT_FUNDS", 409)
    assert "estimated requirement" in r.json()["detail"]["message"]


def test_03_pledged_collateral_never_funds_a_delivery_buy():
    funds = {"enabled": True, "net": 1_000_000.0,
             "available": {"live_balance": 1_000_000.0,             # even if live_balance says otherwise
                           "collateral": 1_000_000.0 - 10_000.0,    # only ₹10,000 of net is cash
                           "cash": 1_000_000.0}}
    out = margin_preflight.evaluate(_Broker(funds=funds), _ticket(), "regular", 3000.0)
    assert out["verdict"] == "INSUFFICIENT_FUNDS" and out["available"] == 10_000.0


def test_04_charges_are_part_of_the_requirement(env, market_open):
    env.kite.margins_live_balance = WORST_VALUE + WORST_CHARGES / 2  # covers the order, not its charges
    t, r = _live_confirm(env)
    _assert_refused(env, t, r, "INSUFFICIENT_FUNDS", 409)


def test_05_margin_read_failure_fails_closed(env, market_open):
    env.kite.raise_on_next("order_margins", NetworkException("proxy timeout"))
    t, r = _live_confirm(env)
    _assert_refused(env, t, r, "MARGIN_DATA_UNAVAILABLE", 503)


def test_06_rupee_cap_is_still_enforced_at_both_gates(env, market_open):
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE risk_settings SET max_order_notional = ?", (ORDER_VALUE - 1,))
        conn.commit()
    finally:
        conn.close()
    t, r = _live_confirm(env)
    assert r.status_code == 409 and "MAX_NOTIONAL_EXCEEDED" in _codes(r), r.text
    assert env.kite.calls_to("order_margins") == []  # refused before any margin read


def test_07_dry_run_makes_no_funds_or_margin_call(env, market_open):
    set_mode(env.db, "dry_run")
    t = _create(env)
    env.kite.calls.clear()
    assert _confirm(env, t["id"]).status_code == 200
    assert env.kite.calls_to("order_margins") == []
    assert env.kite.calls_to("margins") == [] or all(c.get("segment") is None for c in env.kite.calls_to("margins"))


def test_08_exits_never_need_margin(env, market_open):
    set_mode(env.db, "live")
    pos = _insert_ticket(env.db, entry_fill_status="filled", filled_quantity=5, kite_order_id="250000000501",
                         kite_gtt_id=None, use_gtt=0)
    exit_id = _insert_ticket(env.db, transaction_type="SELL", closes_ticket_id=pos, status="pending_confirmation",
                             entry_order_type="MARKET", use_gtt=0)
    env.kite.margins_live_balance = 0.0
    r = env.client.post(f"/api/tickets/{exit_id}/confirm", json=approval_body(env.db, exit_id))
    assert r.status_code == 200, r.text
    assert env.kite.calls_to("order_margins") == []


# ---------------------------------------------------------------- the evaluation (unit)

class _Broker:
    def __init__(self, funds=None, rows=None, fail=None, quotes=None):
        self.funds = funds if funds is not None else {
            "enabled": True, "net": 100_000.0,
            "available": {"live_balance": 100_000.0, "collateral": 0.0, "cash": 100_000.0}}
        self.rows = rows
        self.fail = fail or {}
        self.quotes = quotes
        self.sent = []

    def margins(self, segment=None):
        if "margins" in self.fail:
            raise self.fail["margins"]
        return self.funds

    def quote(self, keys):
        if "quote" in self.fail:
            raise self.fail["quote"]
        if self.quotes is not None:
            return self.quotes
        return {k: {"last_price": 3000.0, "upper_circuit_limit": UPPER_CIRCUIT, "lower_circuit_limit": 2400.0}
                for k in keys}

    def order_margins(self, params):
        if "order_margins" in self.fail:
            raise self.fail["order_margins"]
        self.sent.append(params)
        if self.rows is not None:
            return self.rows
        p = params[0]
        value = p["price"] * p["quantity"]
        return [{"tradingsymbol": p["tradingsymbol"], "exchange": p["exchange"], "total": value,
                 "charges": {"total": round(value * 0.001, 2)}}]


def _ticket(**fields):
    base = {"symbol": "TCS", "exchange": "NSE", "transaction_type": "BUY", "product_type": "CNC",
            "entry_order_type": "MARKET", "quantity": 5, "entry_price": 3000.0}
    base.update(fields)
    return base


def test_09_pass_reports_every_figure():
    out = margin_preflight.evaluate(_Broker(), _ticket(), "regular", 3000.0)
    assert out["verdict"] == "PASS"
    assert out["available"] == 100_000.0 and out["order_margin"] == WORST_VALUE and out["charges"] == WORST_CHARGES
    assert out["required"] == WORST_VALUE + WORST_CHARGES and out["order_value"] == WORST_VALUE
    assert (out["margin_price"], out["margin_price_basis"], out["reference_price"]) == (
        UPPER_CIRCUIT, "upper_circuit", 3000.0)


def test_10_a_broker_figure_below_the_cash_equity_notional_uses_the_notional():
    rows = [{"tradingsymbol": "TCS", "exchange": "NSE", "total": WORST_VALUE / 5, "charges": {"total": 1.0}}]
    out = margin_preflight.evaluate(_Broker(rows=rows), _ticket(), "regular", 3000.0)
    assert out["notional_floor_applied"] is True and out["required"] == WORST_VALUE + 1.0


@pytest.mark.parametrize("broker", [
    _Broker(fail={"margins": NetworkException("timeout")}),
    _Broker(fail={"order_margins": NetworkException("timeout")}),
    _Broker(fail={"order_margins": BrokerWritesDisabled("order.margins", "POST")}),
    _Broker(funds={"enabled": False, "net": 1e6, "available": {"live_balance": 1e6, "collateral": 0}}),
    _Broker(funds={"enabled": True, "net": 1e6, "available": {"collateral": 0}}),
    _Broker(funds={"enabled": True, "net": 1e6, "available": {"live_balance": "1e6", "collateral": 0}}),
    _Broker(funds={"enabled": True, "net": math.inf, "available": {"live_balance": 1e6, "collateral": 0}}),
    _Broker(funds={"enabled": True, "net": 1e6, "available": {"live_balance": 1e6, "collateral": -5}}),
    _Broker(funds={"enabled": True, "net": 1e6}),
    _Broker(funds=["not", "an", "object"]),
    _Broker(rows=[]),
    _Broker(rows=[{"tradingsymbol": "INFY", "exchange": "NSE", "total": 1.0}]),
    _Broker(rows=[{"tradingsymbol": "TCS", "exchange": "BSE", "total": 1.0}]),
    _Broker(rows=[{"tradingsymbol": "TCS", "exchange": "NSE", "total": 0}]),
    _Broker(rows=[{"tradingsymbol": "TCS", "exchange": "NSE", "total": None}]),
    _Broker(rows=[{"tradingsymbol": "TCS", "exchange": "NSE", "total": 10.0, "charges": {"total": -1}}]),
    _Broker(rows=[{"tradingsymbol": "TCS", "exchange": "NSE", "total": WORST_VALUE}]),
    _Broker(rows=[{"tradingsymbol": "TCS", "exchange": "NSE", "total": WORST_VALUE, "charges": {}}]),
    _Broker(rows=[{"tradingsymbol": "TCS", "exchange": "NSE", "total": WORST_VALUE, "charges": {"total": 0}}]),
    _Broker(rows=[{"tradingsymbol": "TCS", "exchange": "NSE", "total": WORST_VALUE, "charges": {"total": "18"}}]),
    _Broker(fail={"quote": NetworkException("timeout")}),
    _Broker(quotes={}),
    _Broker(quotes={"NSE:TCS": {"last_price": 3000.0}}),
    _Broker(quotes={"NSE:TCS": {"last_price": 3000.0, "upper_circuit_limit": 0}}),
    _Broker(quotes={"NSE:TCS": {"last_price": 3000.0, "upper_circuit_limit": None}}),
    _Broker(quotes={"NSE:TCS": {"last_price": 3000.0, "upper_circuit_limit": 2999.0}}),
    _Broker(quotes={"NSE:TCS": {"last_price": 0, "upper_circuit_limit": 3600.0}}),
    _Broker(quotes={"NSE:TCS": {"last_price": math.nan, "upper_circuit_limit": 3600.0}}),
    _Broker(quotes={"NSE:INFY": {"last_price": 1500.0, "upper_circuit_limit": 1800.0}}),
    _Broker(rows=[{"tradingsymbol": "TCS", "exchange": "NSE", "total": 1}, {"tradingsymbol": "TCS",
                                                                         "exchange": "NSE", "total": 1}]),
], ids=["funds-error", "order-margin-error", "writes-disabled", "segment-disabled", "live-balance-missing",
        "live-balance-string", "net-infinite", "collateral-negative", "available-missing", "funds-not-object",
        "no-rows", "other-symbol", "other-exchange", "total-zero", "total-missing", "charges-negative",
        "charges-missing", "charges-total-missing", "charges-zero", "charges-string", "quote-error", "quote-empty",
        "circuit-missing", "circuit-zero", "circuit-none", "circuit-below-ltp", "ltp-zero", "ltp-nan",
        "quote-other-instrument", "two-rows"])
def test_11_unavailable_or_inconsistent_margin_data_fails_closed(broker):
    out = margin_preflight.evaluate(broker, _ticket(), "regular", 3000.0)
    assert out["verdict"] == "MARGIN_DATA_UNAVAILABLE", out
    failures, _ = margin_preflight.check(broker, _ticket(), "regular", 3000.0)
    assert [f.code for f in failures] == ["MARGIN_DATA_UNAVAILABLE"] and failures[0].http_status == 503


@pytest.mark.parametrize("fields", [dict(product_type="MIS"), dict(product_type="NRML"), dict(exchange="NFO"),
                                    dict(transaction_type="SELL")])
def test_12_no_derivatives_or_intraday_margin_model_is_guessed(fields):
    out = margin_preflight.evaluate(_Broker(), _ticket(**fields), "regular", 3000.0)
    assert out["verdict"] == "MARGIN_PREFLIGHT_UNSUPPORTED"
    failures, _ = margin_preflight.check(_Broker(), _ticket(**fields), "regular", 3000.0)
    assert failures and failures[0].http_status == 503


# ---------------------------------------------------------------- the preview shown before approval

def test_13_preview_shows_funds_margin_charges_and_cap_for_a_live_entry(env, market_open):
    set_mode(env.db, "live")
    t = _create(env)
    data = env.client.get(f"/api/tickets/{t['id']}/margin-preview").json()["data"]
    assert data["applicable"] is True and data["verdict"] == "PASS"
    assert data["order_value"] == WORST_VALUE and data["charges"] == WORST_CHARGES
    assert data["max_order_notional"] is not None and data["available"] > 0
    assert env.kite.calls_to("place_order") == []


def test_14_preview_is_not_applicable_in_dry_run_or_for_an_exit(env):
    set_mode(env.db, "dry_run")
    t = _create(env)
    data = env.client.get(f"/api/tickets/{t['id']}/margin-preview").json()["data"]
    assert data["applicable"] is False and env.kite.calls_to("order_margins") == []
    set_mode(env.db, "live")
    pos = _insert_ticket(env.db, entry_fill_status="filled", filled_quantity=5, kite_order_id="250000000502")
    exit_id = _insert_ticket(env.db, transaction_type="SELL", closes_ticket_id=pos, status="pending_confirmation")
    assert env.client.get(f"/api/tickets/{exit_id}/margin-preview").json()["data"]["applicable"] is False


def test_15_final_gate_unit_runs_the_margin_check_last(env, monkeypatch):
    set_mode(env.db, "live")  # armed for the active session
    seen = []
    monkeypatch.setattr(margin_preflight, "check", lambda kite, ticket, variety, ltp: (seen.append(ltp) or ([], {})))
    tid = _insert_ticket(env.db, status="confirmed", mode="live", entry_order_type="MARKET", source="strategy",
                         strategy_name="momentum_breakout")
    conn = env.db.get_connection()
    try:
        row = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (tid,)).fetchone()
        decision = account_safety.final_gate(conn, row["account_id"], row, env.kite, None, variety="regular")
    finally:
        conn.close()
    assert decision.passed, decision.failures
    assert seen == [3000.0]  # called with the LTP the price checks used


# ---------------------------------------------------------------- M3: pricing, never 0

def test_16_a_market_entry_is_never_priced_at_zero():
    broker = _Broker()
    out = margin_preflight.evaluate(broker, _ticket(), "regular", 3000.0)
    assert out["verdict"] == "PASS"
    [[params]] = broker.sent
    assert params["price"] == UPPER_CIRCUIT and params["price"] > 0 and params["order_type"] == "LIMIT"


def test_17_a_limit_entry_is_sized_at_its_own_limit_price_without_a_quote():
    broker = _Broker(fail={"quote": AssertionError("a LIMIT entry needs no quote")})
    out = margin_preflight.evaluate(broker, _ticket(entry_order_type="LIMIT", entry_price=3010.0), "regular", None)
    assert out["verdict"] == "PASS" and out["margin_price_basis"] == "limit_price"
    [[params]] = broker.sent
    assert (params["order_type"], params["price"]) == ("LIMIT", 3010.0)


@pytest.mark.parametrize("price", [None, 0, -1.0, math.inf, "3000"])
def test_18_a_limit_entry_without_a_usable_price_fails_closed(price):
    out = margin_preflight.evaluate(_Broker(), _ticket(entry_order_type="LIMIT", entry_price=price), "regular", None)
    assert out["verdict"] == "MARGIN_DATA_UNAVAILABLE", out


@pytest.mark.parametrize("quantity", [0, -5, None, 2.5, True])
def test_19_an_unusable_quantity_fails_closed(quantity):
    out = margin_preflight.evaluate(_Broker(), _ticket(quantity=quantity), "regular", 3000.0)
    assert out["verdict"] == "MARGIN_DATA_UNAVAILABLE", out


def test_20_the_callers_ltp_never_replaces_the_worst_case_price():
    # A MARKET order can fill above the LTP the caller just read: the circuit, not the LTP, sizes it.
    out = margin_preflight.evaluate(_Broker(), _ticket(), "regular", 3000.0)
    assert out["order_value"] == WORST_VALUE > 5 * 3000.0


def test_21_funds_between_the_expected_and_the_worst_case_value_are_refused():
    funds = {"enabled": True, "net": 16_000.0, "available": {"live_balance": 16_000.0, "collateral": 0.0}}
    out = margin_preflight.evaluate(_Broker(funds=funds), _ticket(), "regular", 3000.0)
    assert out["verdict"] == "INSUFFICIENT_FUNDS"        # 15,000 at the LTP would have passed


def test_22_writes_stay_disabled_and_the_gate_fails_closed():
    """The order-margin POST refused by the writes switch is not worked around: the verdict is
    MARGIN_DATA_UNAVAILABLE, never a guessed figure."""
    broker = _Broker(fail={"order_margins": BrokerWritesDisabled("order.margins", "POST")})
    failures, evidence = margin_preflight.check(broker, _ticket(), "regular", 3000.0)
    assert [f.code for f in failures] == ["MARGIN_DATA_UNAVAILABLE"] and evidence["required"] is None
