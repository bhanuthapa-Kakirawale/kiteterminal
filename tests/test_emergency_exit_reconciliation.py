"""E3: Emergency Exit reconciles the app's quantity with FRESH broker data before selling.

Per instrument (exchange + trading symbol, and instrument token where the broker gives one),
from one read of holdings, positions and today's orders per click:

- the broker must hold at least the app's open quantity (settled + T1 + the day's CNC net,
  an overnight CNC row never counted twice);
- no SELL of the instrument that this app did not place may be working or filled today
  (a manual sale, or a protective GTT that fired and is not yet recorded);
- otherwise the automated exit is refused and the user is told to reconcile / exit in Kite.

Shares are fungible: more held than the app's quantity is allowed (exactly the app's quantity
is sold) — that proves the quantity can be delivered, not which shares are the app's.
Authentication, the confirmation phrase, the session check and duplicate-click protection are
unchanged. FakeKite and scratch databases only.
"""

import pytest
from kiteconnect.exceptions import NetworkException, TokenException

from conftest import set_mode, ticket_row
from test_baseline_api import _insert_ticket
from test_live_safety import _protective_oco_gtt


def _position(env, quantity=5, **fields) -> int:
    gtt_id = _protective_oco_gtt(env, quantity=quantity)
    base = dict(source="strategy", strategy_name="momentum_breakout", quantity=quantity, kite_gtt_id=gtt_id,
                executed_at="2026-09-01 04:00:00", entry_fill_status="filled", filled_price=3000.0,
                filled_quantity=quantity, current_stop_loss=2900.0)
    base.update(fields)
    return _insert_ticket(env.db, **base)


def _exit(env) -> list[dict]:
    set_mode(env.db, "live")
    env.kite.auto_fill = False
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    assert r.status_code == 200, r.text
    return r.json()["data"]["results"]


def _sells(env) -> list[int]:
    return [c["quantity"] for c in env.kite.calls_to("place_order") if c["transaction_type"] == "SELL"]


def _refused(result, code="EXIT_RECONCILIATION_FAILED") -> str:
    assert (result["status"], result["error_code"]) == ("refused", code), result
    assert "manually in Kite" in result["message"] and "nothing was sent" in result["message"]
    return result["message"]


# ---------------------------------------------------------------- reconciled: the exit goes ahead

def test_01_t0_a_cnc_buy_today_in_positions_is_sold(env, market_open):
    _position(env)
    env.kite.set_position("TCS", 5)                       # bought today: positions only
    [result] = _exit(env)
    assert result["status"] == "exit_submitted", result
    assert "Reconciled with Kite" in result["message"] and _sells(env) == [5]


def test_02_t1_unsettled_shares_in_holdings_are_sold(env, market_open):
    _position(env)
    env.kite.set_holding("TCS", 0, t1_quantity=5)         # bought yesterday: holdings T1
    [result] = _exit(env)
    assert result["status"] == "exit_submitted", result
    assert _sells(env) == [5]


def test_03_t1_with_an_overnight_cnc_row_is_not_double_counted(env, market_open):
    _position(env, 5)
    _position(env, 5)                                     # the app holds 10 in all
    env.kite.set_holding("TCS", 0, t1_quantity=5)
    env.kite.set_position("TCS", 5, overnight_quantity=5)  # the same 5, also listed as carried overnight
    results = _exit(env)
    # Counted once, the broker holds 5: it cannot be 10, so nothing is sold.
    assert [r["status"] for r in results] == ["refused", "refused"]
    assert "shows only 5" in _refused(results[0])
    assert _sells(env) == []


def test_04_more_held_than_the_app_owns_sells_exactly_the_apps_quantity(env, market_open):
    _position(env)
    env.kite.set_holding("TCS", 50)                       # 45 of them the user's own
    [result] = _exit(env)
    assert result["status"] == "exit_submitted", result
    assert "45 more share(s)" in result["message"] and _sells(env) == [5]


def test_05_two_app_positions_in_one_instrument_are_reconciled_together(env, market_open):
    _position(env, 5)
    _position(env, 5)
    env.kite.set_holding("TCS", 10)
    results = _exit(env)
    assert [r["status"] for r in results] == ["exit_submitted", "exit_submitted"], results
    assert sorted(_sells(env)) == [5, 5]


def test_06_an_external_buy_today_is_reported_not_refused(env, market_open):
    _position(env)
    env.kite.set_holding("TCS", 5)
    env.kite.add_broker_order("TCS", "BUY", 3, status="COMPLETE")
    env.kite.set_position("TCS", 3)
    [result] = _exit(env)
    assert result["status"] == "exit_submitted", result
    assert "BUY activity today not placed by this app" in result["message"] and _sells(env) == [5]


# ---------------------------------------------------------------- cannot be reconciled: refused

def test_07_a_shortfall_is_refused(env, market_open):
    _position(env)
    env.kite.set_holding("TCS", 3)                        # 2 sold outside the app
    [result] = _exit(env)
    assert "hold 5 share(s), but the broker shows only 3" in _refused(result)
    assert _sells(env) == []


def test_08_nothing_at_the_broker_is_refused(env, market_open):
    _position(env)
    [result] = _exit(env)
    assert "shows only 0" in _refused(result) and _sells(env) == []


@pytest.mark.parametrize("status,filled", [("COMPLETE", 2), ("OPEN", 0), ("OPEN", 1), ("CANCELLED", 1)])
def test_09_an_external_sell_today_is_refused(env, market_open, status, filled):
    _position(env)
    env.kite.set_holding("TCS", 50)                       # plenty held: the sale is still ambiguous
    env.kite.add_broker_order("TCS", "SELL", 2, status=status, filled_quantity=filled)
    [result] = _exit(env)
    assert "SELL activity on this instrument today was not placed by this app" in _refused(result)
    assert _sells(env) == []


def test_10_an_external_sell_that_ended_unfilled_is_ignored(env, market_open):
    _position(env)
    env.kite.set_holding("TCS", 5)
    env.kite.add_broker_order("TCS", "SELL", 2, status="CANCELLED", filled_quantity=0)
    [result] = _exit(env)
    assert result["status"] == "exit_submitted", result


def test_11_a_protective_gtt_that_fired_unrecorded_is_refused(env, market_open):
    pos = _position(env)
    env.kite.set_holding("TCS", 5)
    env.kite.trigger_gtt(ticket_row(env.db, pos)["kite_gtt_id"], leg=0, status="COMPLETE")
    [result] = _exit(env)
    message = _refused(result)
    assert "protective GTT that fired" in message and "sell the same shares twice" in message
    assert _sells(env) == []


def test_12_the_same_symbol_on_another_exchange_is_not_this_instrument(env, market_open):
    _position(env)                                        # NSE:TCS
    env.kite.set_holding("TCS", 5, exchange="BSE")
    [result] = _exit(env)
    message = _refused(result)
    assert "shows only 0" in message and "listed under BSE" in message
    assert _sells(env) == []


def test_13_a_conflicting_instrument_token_is_refused(env, market_open):
    _position(env)                                        # TCS, token 2953217
    env.kite.set_holding("TCS", 5, instrument_token=408065)
    [result] = _exit(env)
    assert "instrument [408065]" in _refused(result) and _sells(env) == []


def test_14_inconsistent_broker_rows_are_refused(env, market_open):
    _position(env)
    env.kite.set_holding("TCS", 5)
    tcs = next(h for h in env.kite._holdings if h["tradingsymbol"] == "TCS")
    env.kite._holdings.append(dict(tcs))                     # the same holding listed twice
    [result] = _exit(env)
    assert "listed more than once" in _refused(result) and _sells(env) == []


@pytest.mark.parametrize("read", ["holdings", "positions", "orders"])
def test_15_an_unreadable_broker_snapshot_refuses_every_exit(env, market_open, read):
    _position(env)
    env.kite.set_holding("TCS", 5)
    env.kite.raise_on_next(read, NetworkException("Gateway timed out"))
    [result] = _exit(env)
    assert "could not be read" in _refused(result, "BROKER_RECONCILIATION_UNAVAILABLE")
    assert _sells(env) == []


def test_16_a_malformed_payload_refuses_every_exit(env, market_open):
    _position(env)
    env.kite._holdings = [{"quantity": 5}]                   # no instrument at all
    [result] = _exit(env)
    _refused(result, "BROKER_RECONCILIATION_UNAVAILABLE")
    assert _sells(env) == []


def test_17_an_expired_session_refuses_and_says_so(env, market_open):
    _position(env)
    env.kite.raise_on_next("holdings", TokenException("Incorrect `api_key` or `access_token`."))
    [result] = _exit(env)
    message = _refused(result, "BROKER_RECONCILIATION_UNAVAILABLE")
    assert "session has expired" in message and "Log in to Kite again" in message
    assert _sells(env) == []


def test_18_an_unknown_open_quantity_elsewhere_in_the_instrument_is_refused(env, market_open):
    _position(env, 5)
    _position(env, 5, entry_fill_status="partial", filled_quantity=None)   # quantity never recorded
    env.kite.set_holding("TCS", 50)
    results = _exit(env)
    assert all(r["status"] == "refused" for r in results), results
    assert _sells(env) == []


# ---------------------------------------------------------------- protections that stay

def test_19_a_second_click_never_sells_twice(env, market_open):
    _position(env)
    env.kite.set_holding("TCS", 5)
    [first] = _exit(env)
    assert first["status"] == "exit_submitted"
    [second] = _exit(env)
    assert second["status"] == "already_exiting" and _sells(env) == [5]


def test_20_the_phrase_is_checked_before_any_broker_read(env):
    _position(env)
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "exit"})
    assert r.status_code == 400
    assert env.kite.calls_to("holdings") == [] and env.kite.calls_to("orders") == []


def test_21_one_refused_instrument_never_stops_the_others(env, market_open):
    _position(env)                                        # TCS: shortfall
    env.kite.set_holding("TCS", 1)
    gtt = _protective_oco_gtt(env, symbol="INFY", stop=1450.0, target=1650.0)
    _insert_ticket(env.db, symbol="INFY", instrument_token=408065, entry_price=1500.0, stop_loss=1450.0,
                   target_1=1650.0, kite_gtt_id=gtt, entry_fill_status="filled", filled_quantity=5,
                   executed_at="2026-09-01 04:00:00")
    env.kite.set_holding("INFY", 5)
    results = {r["symbol"]: r for r in _exit(env)}
    assert results["TCS"]["status"] == "refused" and results["INFY"]["status"] == "exit_submitted"
    assert [c["tradingsymbol"] for c in env.kite.calls_to("place_order")] == ["INFY"]
