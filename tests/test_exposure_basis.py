"""PK-15 (audit P1-5): the exposure basis of the new-entry limits.

One broker snapshot (holdings, positions, margins — one call each) per
approval. Shares the account owns count once, whoever bought them:
settled + T1 + today's CNC change at the broker, or the app's own open
positions (PK-13 open quantity) if the broker has not caught up — never
both. Entries that may still fill (in flight, UNKNOWN, the unfilled rest of
a working order) are added at their planned entry value. Held shares are
valued at market; pledged-collateral margin is not counted twice in the
portfolio value. Anything that cannot be computed fails closed for a new
entry and never blocks an exit.
"""

from datetime import datetime, timedelta, timezone

import pytest
from kiteconnect.exceptions import NetworkException

from conftest import approval_body, account_id, displayed_mode, set_mode, ticket_row
from test_baseline_api import _insert_ticket
from test_emergency_exit_isolation import _emergency_exit, _position
from test_live_safety import _set_risk, _unknown_ticket, market_open, unsafe_client  # noqa: F401 — fixtures

SINGLE = "max_single_stock_exposure_pct"
SECTOR = "max_sector_exposure_pct"


def _utc(days_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _portfolio(env, cash: float) -> None:
    """An empty broker account holding only `cash`; tests add what they need."""
    env.kite._holdings = []
    env.kite._positions = []
    env.kite.margins_net = cash
    env.kite.margins_collateral = 0.0


def _hold(env, symbol, quantity=0, price=1000.0, t1=0, average_price=None):
    env.kite.set_holding(symbol, quantity, t1_quantity=t1, last_price=price,
                         average_price=price if average_price is None else average_price)


def _context(env, exclude_ticket_id: int = 0):
    from backend import risk_engine
    ctx = risk_engine.build_portfolio_context(env.kite)
    conn = env.db.get_connection()
    try:
        return ctx, risk_engine.account_exposures(conn, account_id(env.db), ctx, exclude_ticket_id)
    finally:
        conn.close()


def _exposure(env, symbol):
    return _context(env)[1].get(symbol)


def _position_ticket(env, symbol="INFY", token=408065, quantity=100, entry=1500.0, **fields) -> int:
    """An app-owned live BUY position, filled as `fields` say."""
    base = dict(entry_fill_status="filled", executed_at=_utc(), stop_loss=entry - 50, target_1=entry + 150)
    base.update(fields)
    return _insert_ticket(env.db, symbol=symbol, instrument_token=token, quantity=quantity, entry_price=entry, **base)


def _ticket(env, symbol="TCS", quantity=10, entry=3000.0, stop=2900.0, target=3300.0) -> dict:
    r = env.client.post("/api/tickets", json={
        "symbol": symbol, "exchange": "NSE", "transaction_type": "BUY", "quantity": quantity,
        "entry_price": entry, "entry_order_type": "LIMIT", "stop_loss": stop, "target_1": target,
        "target_2": None, "trailing_enabled": False, "trailing_trigger": None, "trailing_distance": None,
        "product_type": "CNC", "use_gtt": True, "rationale": "PK-15 test",
    })
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _check(ticket: dict, rule: str) -> dict:
    return next(c for c in ticket["risk_check_results"] if c["rule"] == rule)


# ---------------------------------------------------------------- 1-3, 7: broker quantities

def test_1_settled_holdings_count_at_market_value(env):
    _portfolio(env, 910_000.0)
    _hold(env, "TCS", 30, price=3000.0, average_price=2000.0)
    ctx, exposures = _context(env)
    assert ctx.portfolio_value == 1_000_000.0
    assert (exposures["TCS"].broker_quantity, exposures["TCS"].value) == (30, 90_000.0)   # 10 (test 10): not cost 60,000


def test_2_t1_quantity_counts_as_holdings_and_portfolio_value(env):
    _portfolio(env, 940_000.0)
    _hold(env, "TCS", 0, price=3000.0, t1=20)                  # bought yesterday, not settled
    ctx, exposures = _context(env)
    assert exposures["TCS"].broker_quantity == 20 and exposures["TCS"].value == 60_000.0
    assert ctx.portfolio_value == 1_000_000.0                  # yesterday's buy is not missing from the denominator


def test_3_same_day_external_cnc_buy_counts(env):
    _portfolio(env, 955_000.0)
    env.kite.set_position("TCS", 15, last_price=3000.0)        # bought today in Kite, no app ticket
    ctx, exposures = _context(env)
    assert exposures["TCS"].broker_quantity == 15 and exposures["TCS"].value == 45_000.0
    assert exposures["TCS"].app_held_quantity == 0
    assert ctx.portfolio_value == 1_000_000.0


def test_7_one_symbol_across_holdings_t1_and_positions(env):
    from backend import risk_engine
    _portfolio(env, 1_000_000.0)
    _hold(env, "TCS", 10, price=3000.0, t1=5)
    env.kite.set_position("TCS", 3, last_price=3000.0)                              # +3 bought today
    assert _exposure(env, "TCS").broker_quantity == 18

    # A sale of 4 held shares today shows as a negative CNC position.
    env.kite.set_position("TCS", -4, last_price=3000.0)
    assert _exposure(env, "TCS").broker_quantity == 11

    # CNC overnight quantity is yesterday's buy — already the holding's T1.
    env.kite.set_position("TCS", 5, last_price=3000.0, overnight_quantity=5)
    assert _exposure(env, "TCS").broker_quantity == 15

    # Intraday (MIS) and derivatives are not delivery exposure.
    env.kite.set_position("TCS", 0)
    env.kite._positions += [
        {"tradingsymbol": "TCS", "exchange": "NSE", "product": "MIS", "quantity": 50, "last_price": 3000.0},
        {"tradingsymbol": "TCS26OCTFUT", "exchange": "NFO", "product": "NRML", "quantity": 175, "last_price": 3000.0},
    ]
    broker = risk_engine.broker_quantities(env.kite.holdings(), env.kite.positions())
    assert broker["TCS"]["quantity"] == 15 and "TCS26OCTFUT" not in broker


def test_broker_snapshot_is_one_call_each(env):
    _portfolio(env, 1_000_000.0)
    before = len(env.kite.calls)
    _ticket(env, "TCS", 5)
    names = [n for n, _ in env.kite.calls[before:]]
    assert (names.count("holdings"), names.count("positions"), names.count("margins")) == (1, 1, 1)


# ---------------------------------------------------------------- 4-6, 18: app tickets vs broker

def test_4_app_partial_fill_counts_what_filled_once_the_entry_ended(env):
    _portfolio(env, 1_000_000.0)
    _position_ticket(env, quantity=100, entry_fill_status="partial", filled_quantity=40)
    e = _exposure(env, "INFY")
    assert (e.app_held_quantity, e.pending_quantity, e.held_quantity) == (40, 0, 40)
    assert e.value == 40 * 1500.0                              # not 100 requested


def test_4b_working_partial_fill_counts_the_requested_size(env):
    # PK-13 decision (a): the unfilled 60 can still fill at any moment.
    _portfolio(env, 1_000_000.0)
    _position_ticket(env, quantity=100, entry_fill_status="pending", filled_quantity=40)
    env.kite.set_position("INFY", 40, last_price=1500.0)
    e = _exposure(env, "INFY")
    assert (e.broker_quantity, e.app_held_quantity, e.pending_quantity) == (40, 40, 60)
    assert e.value == 100 * 1500.0


def test_5_app_position_already_at_the_broker_is_counted_once(env):
    _portfolio(env, 1_000_000.0)
    _position_ticket(env, quantity=100, entry_fill_status="partial", filled_quantity=40)
    env.kite.set_position("INFY", 40, last_price=1600.0)       # the same 40 shares, seen by the broker
    e = _exposure(env, "INFY")
    assert e.held_quantity == 40 and e.value == 40 * 1600.0    # once, at market — not 80


def test_5b_external_shares_on_top_of_the_app_position_count(env):
    _portfolio(env, 1_000_000.0)
    _position_ticket(env, quantity=40, executed_at=_utc(3))
    _hold(env, "INFY", 140, price=1500.0)                      # 40 of the app + 100 bought outside it
    assert _exposure(env, "INFY").held_quantity == 140


def test_5c_filled_app_shares_the_broker_has_not_shown_yet_still_count(env):
    _portfolio(env, 1_000_000.0)
    _hold(env, "INFY", 10, price=1600.0)                       # an older external lot only
    _position_ticket(env, quantity=40)                         # filled today; positions() not updated yet
    e = _exposure(env, "INFY")
    assert e.held_quantity == 40 and e.value == 40 * 1600.0


def test_6_unknown_entry_counts_in_full_whatever_its_age(env):
    _portfolio(env, 1_000_000.0)
    _unknown_ticket(env, symbol="INFY", instrument_token=408065, quantity=30, entry_price=1500.0,
                    submitted_at=_utc(2))                     # unresolved for two days
    e = _exposure(env, "INFY")
    assert (e.held_quantity, e.pending_quantity, e.value) == (0, 30, 45_000.0)


def test_6b_unknown_entry_is_never_zero_even_if_the_broker_already_filled_it(env):
    # Conservative until order_monitor resolves it: the broker's 30 and the
    # UNKNOWN 30 may be the same shares, counted twice for that moment.
    _portfolio(env, 1_000_000.0)
    _unknown_ticket(env, symbol="INFY", instrument_token=408065, quantity=30, entry_price=1500.0,
                    submitted_at=_utc())
    env.kite.set_position("INFY", 30, last_price=1500.0)
    assert _exposure(env, "INFY").value == 90_000.0


def test_12_dry_run_tickets_never_count(env):
    _portfolio(env, 1_000_000.0)
    _position_ticket(env, quantity=100, mode="dry_run", filled_quantity=100)
    _insert_ticket(env.db, symbol="INFY", instrument_token=408065, quantity=50, entry_price=1500.0,
                   status="confirmed", mode="dry_run")
    assert _exposure(env, "INFY") is None


def test_13_external_holding_without_any_ticket_is_in_the_risk_check(env):
    # conftest's default account: RELIANCE 10 @ 1,300 held, never bought here.
    ticket = _ticket(env, "RELIANCE", 5, entry=1300.0, stop=1250.0, target=1450.0)
    check = _check(ticket, SINGLE)
    assert check["current_value"] == pytest.approx(13_000 / 1_013_000 * 100)
    assert check["actual_value"] == pytest.approx(19_500 / 1_013_000 * 100)


# ---------------------------------------------------------------- 8, 16: yesterday's buy

def test_8_yesterdays_buy_gets_no_fresh_single_stock_allowance(env):
    # Day 1: TCS bought up to 9.9% (the app's ticket). Day 2: the shares are
    # T1 at the broker. A new TCS ticket must see the 9.9% already there.
    _portfolio(env, 901_000.0)
    _hold(env, "TCS", 0, price=3000.0, t1=33)
    _position_ticket(env, "TCS", 2953217, quantity=33, entry=3000.0, executed_at=_utc(1))
    ticket = _ticket(env, "TCS", 30)                           # +90,000

    check = _check(ticket, SINGLE)
    assert check["current_value"] == pytest.approx(9.9)       # counted once: T1 and the app ticket are one lot
    assert check["actual_value"] == pytest.approx(18.9) and not check["passed"]
    assert ticket["status"] == "risk_rejected"


# ---------------------------------------------------------------- 9, 17: sector interaction

def test_17_t1_holding_same_day_position_and_new_entry_share_one_sector_total(env):
    _portfolio(env, 870_000.0)
    _hold(env, "TCS", 0, price=3000.0, t1=20)                  # 60,000 IT, bought yesterday
    env.kite.set_position("HCLTECH", 50, last_price=1400.0)    # 70,000 IT, bought today (external)
    # The app's own ticket for those same HCLTECH shares must not add them again.
    _position_ticket(env, "HCLTECH", 1850625, quantity=50, entry=1400.0)

    ok = _ticket(env, "INFY", 40, entry=1500.0, stop=1450.0, target=1650.0)       # +60,000
    check = _check(ok, SECTOR)
    assert check["current_value"] == pytest.approx(13.0)       # 130,000 / 1,000,000, each lot once
    assert check["actual_value"] == pytest.approx(19.0) and check["passed"]

    over = _ticket(env, "INFY", 50, entry=1500.0, stop=1450.0, target=1650.0)     # +75,000 → 20.5%
    assert not _check(over, SECTOR)["passed"]


# ---------------------------------------------------------------- 11: portfolio value

def test_11_pledged_collateral_margin_is_not_counted_twice(env):
    _portfolio(env, 500_000.0)
    _hold(env, "TCS", 100, price=3000.0)                       # 300,000, pledged
    env.kite.margins_collateral = 200_000.0                    # already inside equity.net
    ctx, _ = _context(env)
    assert ctx.collateral_excluded == 200_000.0 and ctx.cash_component == 300_000.0
    assert ctx.portfolio_value == 600_000.0                    # was 800,000
    check = _check(_ticket(env, "INFY", 10, entry=1500.0, stop=1450.0, target=1650.0), SINGLE)
    assert check["actual_value"] == pytest.approx(15_000 / 600_000 * 100)


def test_max_open_positions_counts_app_positions_not_external_holdings(env):
    # Documented decision: the position-count limit is about this app's
    # trades; external holdings are covered by the exposure limits instead.
    _set_risk(env, max_open_positions=1)
    _hold(env, "TCS", 5, price=3000.0)                         # RELIANCE (default) + TCS held outside
    check = _check(_ticket(env, "INFY", 5, entry=1500.0, stop=1450.0, target=1650.0), "max_open_positions")
    assert check["passed"] and check["actual_value"] == 1


# ---------------------------------------------------------------- 22: fail closed

@pytest.mark.parametrize("call", ["holdings", "positions", "margins"])
def test_broker_data_unavailable_refuses_a_new_entry_at_creation(env, call):
    env.kite.raise_on_next(call, NetworkException("Gateway timed out"))
    r = env.client.post("/api/tickets", json={
        "symbol": "TCS", "exchange": "NSE", "transaction_type": "BUY", "quantity": 5, "entry_price": 3000.0,
        "entry_order_type": "LIMIT", "stop_loss": 2900.0, "target_1": 3300.0, "target_2": None,
        "trailing_enabled": False, "trailing_trigger": None, "trailing_distance": None, "product_type": "CNC",
        "use_gtt": True, "rationale": "x",
    })
    assert r.status_code == 502 and r.json()["detail"]["error_code"] == "KITE_API_ERROR"


@pytest.mark.parametrize("breakage", ["no_price", "bad_margin", "zero_value", "no_symbol"])
def test_unusable_portfolio_data_is_portfolio_exposure_unavailable(env, breakage):
    from backend import risk_engine
    if breakage == "no_price":
        env.kite._holdings[0]["last_price"] = 0
    elif breakage == "bad_margin":
        env.kite.margins_net = "n/a"
    elif breakage == "zero_value":
        env.kite.margins_net = -13_000.0
    else:
        env.kite._positions.append({"exchange": "NSE", "product": "CNC", "quantity": 1})
    with pytest.raises(risk_engine.RiskCheckUnavailableError) as e:
        risk_engine.build_portfolio_context(env.kite)
    assert (e.value.status_code, e.value.error_code) == (503, "PORTFOLIO_EXPOSURE_UNAVAILABLE")


def test_positions_failure_at_confirmation_sends_nothing(env, unsafe_client, market_open):
    t = _ticket(env, "TCS", 5)
    set_mode(env.db, "live")
    env.kite.raise_on_next("positions", NetworkException("Gateway timed out"))
    r = unsafe_client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t['id']))
    assert r.status_code == 503 and r.json()["detail"]["error_code"] == "RISK_REVALIDATION_UNAVAILABLE"
    assert env.kite.calls_to("place_order") == []
    assert ticket_row(env.db, t["id"])["status"] == "rejected"


def test_exits_never_need_the_portfolio_snapshot(env, market_open, monkeypatch):
    _position(env, "TCS")
    set_mode(env.db, "live")

    def down(*_a, **_k):
        raise NetworkException("Gateway timed out")
    for name in ("positions", "margins"):
        monkeypatch.setattr(env.kite, name, down)

    r = _emergency_exit(env.client)

    assert r.status_code == 200, r.text
    assert [res["status"] for res in r.json()["data"]["results"]] == ["exit_submitted"]
