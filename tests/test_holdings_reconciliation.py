"""C8: broker quantity per instrument (exchange + symbol), with no double counting.

backend/broker_holdings.instrument_quantities turns Kite's holdings and positions into one
figure per (EXCHANGE, TRADINGSYMBOL): settled + T1 + the day's CNC net position, where a CNC
row carried overnight is not counted again (its shares are already T1 in holdings) and MIS /
NRML rows are never counted. Reconciliation compares the app's open positions against that
figure per instrument, never per symbol alone. FakeKite and scratch databases only.
"""

import pytest

from backend import broker_holdings, reconciliation
from backend.accounts import get_default_account
from test_baseline_api import _insert_ticket


def _q(holdings, net, key=("NSE", "TCS")):
    return broker_holdings.instrument_quantities(holdings, {"net": net, "day": []}).get(key)


def _h(symbol="TCS", exchange="NSE", quantity=0, t1=0, **extra):
    return {"tradingsymbol": symbol, "exchange": exchange, "quantity": quantity, "t1_quantity": t1, **extra}


def _p(symbol="TCS", exchange="NSE", quantity=0, overnight=0, product="CNC"):
    return {"tradingsymbol": symbol, "exchange": exchange, "quantity": quantity, "overnight_quantity": overnight,
            "product": product}


# ---------------------------------------------------------------- the arithmetic

def test_01_t0_a_buy_today_is_only_in_positions():
    q = _q([], [_p(quantity=10)])
    assert (q.settled, q.t1, q.day_net, q.total, q.problems) == (0, 0, 10, 10, [])


def test_02_t1_unsettled_shares_count():
    q = _q([_h(t1=10)], [])
    assert (q.t1, q.total) == (10, 10)


def test_03_an_overnight_cnc_row_is_not_counted_twice():
    q = _q([_h(t1=10)], [_p(quantity=10, overnight=10)])
    assert (q.t1, q.day_net, q.total) == (10, 0, 10)


def test_04_t1_plus_more_bought_today():
    q = _q([_h(quantity=5, t1=10)], [_p(quantity=13, overnight=10)])
    assert q.total == 5 + 10 + 3


def test_05_selling_holdings_today_reduces_the_total():
    q = _q([_h(quantity=20)], [_p(quantity=-8)])
    assert (q.settled, q.day_net, q.total) == (20, -8, 12)


def test_06_mis_and_nrml_never_count_as_held():
    q = _q([_h(quantity=5)], [_p(quantity=50, product="MIS"), _p(quantity=7, product="NRML")])
    assert q.total == 5 and len(q.other_products) == 2


def test_07_identical_symbols_on_different_exchanges_are_separate():
    out = broker_holdings.instrument_quantities([_h(exchange="NSE", quantity=5), _h(exchange="BSE", quantity=7)],
                                                {"net": [_p(exchange="BSE", quantity=2)], "day": []})
    assert out[("NSE", "TCS")].total == 5 and out[("BSE", "TCS")].total == 9
    assert [q.exchange for q in broker_holdings.same_symbol_elsewhere(out, "NSE", "TCS")] == ["BSE"]


@pytest.mark.parametrize("holdings,net,problem", [
    ([_h(quantity=5), _h(quantity=5)], [], "listed more than once"),
    ([_h(quantity="5")], [], "unreadable"),
    ([_h(quantity=None)], [], "unreadable"),
    ([_h(quantity=2.5)], [], "unreadable"),
    ([_h(quantity=-1)], [], "negative holding"),
    ([_h(quantity=5)], [_p(quantity=-9)], "negative quantity"),
    ([_h(quantity=5, instrument_token=1), _h(exchange="NSE", symbol="TCS", quantity=0, instrument_token=2)], [],
     "conflicting instrument tokens"),
    ([], [_p(quantity=5), _p(quantity=5)], "CNC position is listed more than once"),
    ([], [_p(quantity="x")], "unreadable"),
], ids=["dup-holding", "qty-string", "qty-none", "qty-fraction", "qty-negative", "total-negative", "tokens",
        "dup-position", "position-string"])
def test_08_unreadable_rows_are_problems_never_guessed(holdings, net, problem):
    q = _q(holdings, net)
    assert q.problems and any(problem in p for p in q.problems), q.problems


@pytest.mark.parametrize("holdings,positions", [
    ("not a list", {"net": []}),
    ([], {"day": []}),
    ([{"quantity": 5}], {"net": []}),
    (["junk"], {"net": []}),
    ([], {"net": [{"tradingsymbol": "TCS", "quantity": 1}]}),
])
def test_09_unattributable_payloads_raise(holdings, positions):
    with pytest.raises(broker_holdings.BrokerDataError):
        broker_holdings.instrument_quantities(holdings, positions)


# ---------------------------------------------------------------- the reconciliation cycle

def _check(env):
    conn = env.db.get_connection()
    try:
        reconciliation._check_position_state(conn, env.kite, get_default_account(conn))
        conn.commit()
        return [dict(r) for r in conn.execute(
            "SELECT issue_code, symbol, actual, expected_quantity, broker_quantity, detail FROM reconciliation_log "
            "WHERE resolved = 0 ORDER BY id")]
    finally:
        conn.close()


def _open_position(env, symbol="TCS", exchange="NSE", token=2953217, quantity=10):
    return _insert_ticket(env.db, symbol=symbol, exchange=exchange, instrument_token=token, quantity=quantity,
                          entry_fill_status="filled", filled_quantity=quantity, executed_at="2026-09-01 04:00:00")


def test_10_a_bse_holding_never_covers_an_nse_position(env):
    _open_position(env)
    env.kite.set_holding("TCS", 10, exchange="BSE")
    [finding] = _check(env)
    assert finding["issue_code"] == "BROKER_POSITION_SHORTFALL" and finding["actual"] == "NSE:TCS 0 shares"
    assert "also listed under BSE" in finding["detail"]


def test_11_t0_and_t1_both_reconcile(env):
    _open_position(env, quantity=10)
    env.kite.set_position("TCS", 10)                       # T+0
    assert _check(env) == []
    env.kite.set_position("TCS", 0)
    env.kite.set_holding("TCS", 0, t1_quantity=10)         # T+1
    assert _check(env) == []


def test_12_no_double_count_hides_a_shortfall(env):
    _open_position(env, quantity=15)
    env.kite.set_holding("TCS", 0, t1_quantity=10)
    env.kite.set_position("TCS", 10, overnight_quantity=10)   # the same 10 again
    [finding] = _check(env)
    assert (finding["expected_quantity"], finding["broker_quantity"]) == (15, 10)   # never 20


def test_13_an_mis_position_does_not_cover_a_delivery_position(env):
    _open_position(env, quantity=10)
    env.kite.set_position("TCS", 10, product="MIS")
    [finding] = _check(env)
    assert finding["issue_code"] == "BROKER_POSITION_SHORTFALL"


def test_14_unreadable_figures_are_flagged_not_guessed(env):
    _open_position(env)
    env.kite.set_holding("TCS", 10)
    env.kite._holdings[-1]["t1_quantity"] = "ten"
    [finding] = _check(env)
    assert finding["issue_code"] == "BROKER_QUANTITY_UNVERIFIABLE" and finding["broker_quantity"] is None
    assert "unreadable" in finding["detail"]


def test_15_two_instruments_at_zero_are_two_findings(env):
    """The finding's identity includes the instrument: two shortfalls with the same broker
    figure are no longer collapsed into one."""
    _open_position(env, symbol="TCS", token=2953217)
    _open_position(env, symbol="INFY", token=408065)
    findings = _check(env)
    assert sorted(f["actual"] for f in findings) == ["NSE:INFY 0 shares", "NSE:TCS 0 shares"]


def test_16_a_malformed_payload_skips_the_check_for_the_cycle(env):
    _open_position(env)
    env.kite._holdings = [{"quantity": 10}]
    assert _check(env) == []           # nothing guessed; the cycle reports holdings unavailable
    snapshot = reconciliation.BrokerSnapshot()
    snapshot.load_held(env.kite)
    assert snapshot.held is None and snapshot.held_error
