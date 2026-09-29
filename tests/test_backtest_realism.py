"""PK-21 — execution-realistic historical backtester.

Most tests drive backend.backtest.simulate() directly with hand-built OHLC
bars and a stub strategy module that fires a signal (with explicit levels)
on a chosen bar, so every fill price can be asserted exactly. The remaining
tests use the real strategy modules, the loader and the HTTP route.

Numbering follows the PK-21 packet's test list (1-28).
"""
import random
import types
from datetime import date, timedelta

import pandas as pd
import pytest

from backend import backtest, backtest_costs, config, indicators
from backend.backtest import BacktestConfig, FutureDataError, SymbolFrame, simulate
from backend.strategies import STRATEGIES

from conftest import add_watch, seed_ohlc

MON = date(2026, 1, 5)  # a Monday
PARAMS = {"max_signals_per_day": 10, "breakeven_trigger_r": 1.0}


# ---------------------------------------------------------------- helpers

def trading_days(n: int, start: date = MON) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def frame(symbol: str, bars: list[tuple], start: date = MON, tick: float = 0.05) -> SymbolFrame:
    """bars: (open, high, low, close) per trading day from `start`."""
    days = trading_days(len(bars), start)
    df = pd.DataFrame([
        {"date": d, "open": o, "high": h, "low": l, "close": c, "volume": 100_000, "sym": symbol}
        for d, (o, h, l, c) in zip(days, bars)
    ])
    return SymbolFrame(symbol, df, tick)


def flat(n: int, price: float = 100.0) -> list[tuple]:
    return [(price, price + 1, price - 1, price)] * n


class Stub:
    """A strategy module whose signals and close-based exits are scripted by
    (symbol, date). Records the last date of every frame it was shown, so
    look-ahead can be asserted."""

    STRATEGY_NAME = "stub"

    def __init__(self, signals=None, exits=()):
        self.signals = signals or {}
        self.exits = set(exits)
        self.seen: list[tuple[str, str, str]] = []  # (kind, symbol, last date shown)

    def evaluate_entry(self, df, params):
        key = (df["sym"].iloc[-1], df["date"].iloc[-1])
        self.seen.append(("entry", *key))
        spec = self.signals.get(key)
        if spec is None:
            return {"passed": False}
        return {"passed": True, "close": float(df["close"].iloc[-1]), **spec}

    def compute_entry_levels(self, candidate, params):
        return {"entry_price": candidate["entry"], "stop_loss": candidate["stop"],
                "target_1": candidate["target"], "trailing_distance": candidate.get("trail")}

    def evaluate_exit(self, position, df, params):
        key = (df["sym"].iloc[-1], df["date"].iloc[-1])
        self.seen.append(("exit", *key))
        passed = key in self.exits
        return {"passed": passed, "checklist": {"thesis_invalidation": {"pass": passed}}}

    def rank_key(self, candidate):
        return candidate.get("rank", 0)


def sig(entry=100.0, stop=90.0, target=120.0, trail=None, rank=0):
    return {"entry": entry, "stop": stop, "target": target, "trail": trail, "rank": rank}


def cfg(**overrides) -> BacktestConfig:
    """Frictionless unless a test asks for friction, so fill prices are exact."""
    base = {"entry_slippage_bps": 0.0, "exit_slippage_bps": 0.0, "costs_enabled": False}
    return BacktestConfig.from_overrides(**{**base, **overrides})


def run(frames, stub, config_=None, start=None, end=None, params=PARAMS):
    frames = {f.symbol: f for f in frames}
    all_dates = sorted(d for f in frames.values() for d in f.dates)
    return simulate(stub, frames, params, config_ or cfg(), start or all_dates[0], end or all_dates[-1])


D = trading_days(30)  # D[0] = Monday 2026-01-05, D[1] = Tuesday, ...


# ---------------------------------------------------------------- ENTRY (1-4)

def test_1_monday_signal_fills_tuesday_open_never_the_signal_bar():
    bars = [(100, 101, 99, 100), (102, 104, 101, 103)] + flat(3, 103)
    stub = Stub({("AAA", D[0]): sig()})
    out = run([frame("AAA", bars)], stub)
    [t] = out["trades"]
    assert t["signal_date"] == D[0]
    assert t["entry_date"] == D[1]
    assert t["entry_price"] == 102.0  # Tuesday's OPEN, not Monday's close
    assert t["entry_date"] != t["signal_date"]


def test_2_final_bar_signal_is_never_invented_as_a_trade():
    stub = Stub({("AAA", D[4]): sig()})
    out = run([frame("AAA", flat(5))], stub)
    assert out["trades"] == []
    assert [(u["symbol"], u["signal_date"], u["reason"]) for u in out["unexecuted_signals"]] == [
        ("AAA", D[4], "no_next_session_before_end_date")]


def test_3_gap_up_entry_fills_at_the_actual_open_not_the_stale_planned_price():
    bars = [(100, 101, 99, 100), (106, 107, 105, 106)] + flat(2, 106)
    out = run([frame("AAA", bars)], Stub({("AAA", D[0]): sig(entry=100.1, stop=90, target=130)}))
    [t] = out["trades"]
    assert t["planned_entry_price"] == 100.1
    assert t["entry_price"] == 106.0


def test_4_entry_slippage_makes_a_buy_worse_and_exit_slippage_makes_a_sell_worse():
    bars = [(100, 101, 99, 100), (100, 101, 99.5, 100), (100, 101, 99.5, 100)]
    out = run([frame("AAA", bars)], Stub({("AAA", D[0]): sig()}), cfg(entry_slippage_bps=10, exit_slippage_bps=10))
    [t] = out["trades"]
    assert t["entry_price"] == 100.10  # 100 x 1.001, rounded UP to the tick
    assert t["exit_price"] == 99.90  # backtest_end at close 100 x 0.999, rounded DOWN


@pytest.mark.parametrize("bar, expected", [
    ((99.0, 101, 98, 100), 99.0),       # opens below the limit: fills at the open
    ((101.0, 102, 100.0, 101), 100.1),  # trades down through the limit: fills AT the limit
    ((101.0, 102, 100.5, 101), None),   # never reaches the limit: unfilled
])
def test_limit_entry_model_mirrors_the_live_day_limit(bar, expected):
    bars = [(100, 101, 99, 100), bar, (101, 102, 100.5, 101)]
    out = run([frame("AAA", bars)], Stub({("AAA", D[0]): sig(entry=100.1, stop=90, target=130)}),
              cfg(entry_model="next_session_limit"))
    if expected is None:
        assert out["trades"] == []
        assert [s["reason"] for s in out["skipped_signals"]] == ["limit_not_reached"]
    else:
        assert out["trades"][0]["entry_price"] == expected


def test_default_entry_model_is_next_open_and_breakeven_is_off():
    c = BacktestConfig.from_overrides()
    assert c.entry_model == "next_open"
    assert c.simulate_breakeven is False
    assert c.snapshot()["same_bar_policy"] == "stop_first"


# ---------------------------------------------------------------- STOP (5-7)

def test_5_gap_through_the_stop_fills_at_the_open_not_the_stop():
    # The packet's own numbers: stop 100, next open 94.
    stub = Stub({("AAA", D[0]): sig(entry=110, stop=100, target=140)})
    bars = [(110, 111, 109, 110), (110, 111, 101, 110), (94, 95, 93, 94)]
    out = run([frame("AAA", bars)], stub, cfg(exit_slippage_bps=5))
    [t] = out["trades"]
    assert t["exit_reason"] == "stop_loss"
    assert t["exit_price"] == 93.95  # 94 x (1 - 5 bps), rounded down — NOT 100
    assert t["gap_fill"] is True


def test_6_intrabar_stop_fills_at_the_stop_less_slippage():
    stub = Stub({("AAA", D[0]): sig(entry=110, stop=100, target=140)})
    bars = [(110, 111, 109, 110), (110, 111, 105, 110), (103, 104, 99, 101)]
    out = run([frame("AAA", bars)], stub, cfg(exit_slippage_bps=5))
    [t] = out["trades"]
    assert (t["exit_date"], t["exit_reason"], t["exit_price"], t["gap_fill"]) == (D[2], "stop_loss", 99.95, False)


@pytest.mark.parametrize("trail", [None, 5.0])
def test_7_stop_and_target_inside_one_bar_resolves_stop_first(trail):
    stub = Stub({("AAA", D[0]): sig(entry=100, stop=90, target=120, trail=trail)})
    bars = [(100, 101, 99, 100), (100, 101, 99, 100), (100, 121, 89, 105)]
    out = run([frame("AAA", bars)], stub)
    [t] = out["trades"]
    assert (t["exit_date"], t["exit_reason"], t["exit_price"]) == (D[2], "stop_loss", 90.0)


# ---------------------------------------------------------------- TARGET (8-9)

def test_8_favourable_target_gap_fills_at_the_open():
    stub = Stub({("AAA", D[0]): sig(entry=100, stop=90, target=120)})
    bars = [(100, 101, 99, 100), (100, 101, 99, 100), (125, 126, 124, 125)]
    [t] = run([frame("AAA", bars)], stub)["trades"]
    assert (t["exit_reason"], t["exit_price"], t["gap_fill"]) == ("target", 125.0, True)


def test_9_intrabar_target_fills_at_the_target_less_slippage():
    stub = Stub({("AAA", D[0]): sig(entry=100, stop=90, target=120)})
    bars = [(100, 101, 99, 100), (100, 101, 99, 100), (110, 121, 109, 115)]
    [t] = run([frame("AAA", bars)], stub, cfg(exit_slippage_bps=5))["trades"]
    assert (t["exit_date"], t["exit_reason"], t["exit_price"]) == (D[2], "target", 119.9)


# ---------------------------------------------------------------- EXIT SIGNAL (10)

def test_10_close_based_exit_fills_at_the_next_session_open_not_the_same_close():
    stub = Stub({("AAA", D[0]): sig()}, exits={("AAA", D[2])})
    bars = [(100, 101, 99, 100), (100, 101, 99, 100), (100, 106, 99, 105), (97, 98, 96, 97), (97, 98, 96, 97)]
    [t] = run([frame("AAA", bars)], stub)["trades"]
    assert t["exit_reason"] == "thesis_invalidation"
    assert t["exit_date"] == D[3]
    assert t["exit_price"] == 97.0  # D[3]'s open — not D[2]'s close of 105


def test_gap_stop_takes_precedence_over_a_scheduled_exit_at_the_same_open():
    stub = Stub({("AAA", D[0]): sig(entry=100, stop=95)}, exits={("AAA", D[1])})
    bars = [(100, 101, 99, 100), (100, 101, 99, 100), (92, 93, 91, 92)]
    [t] = run([frame("AAA", bars)], stub)["trades"]
    assert (t["exit_reason"], t["exit_price"], t["gap_fill"]) == ("stop_loss", 92.0, True)


# ---------------------------------------------------------------- TRAILING (11-12)

def _trailing_case():
    stub = Stub({("AAA", D[0]): sig(entry=100, stop=90, target=110, trail=5.0)})
    bars = [
        (100, 101, 99, 100),     # D0 signal
        (100, 101, 99, 100),     # D1 entry at 100
        (107, 112, 106.5, 111),  # D2 high 112 activates; close 111 -> trail 106 FROM D3
        (108, 109, 105.5, 107),  # D3 low 105.5 <= 106 -> trailing stop at 106
        (107, 108, 106.5, 107),
    ]
    return stub, bars


def test_11_todays_high_raises_the_trail_only_for_the_next_session():
    stub, bars = _trailing_case()
    [t] = run([frame("AAA", bars)], stub)["trades"]
    assert (t["exit_date"], t["exit_reason"], t["exit_price"]) == (D[3], "trailing_stop", 106.0)


def test_12_todays_low_cannot_trigger_a_trail_raised_today():
    stub, bars = _trailing_case()
    bars[2] = (107, 112, 105.0, 111)  # D2's own low (105) is BELOW the trail D2 creates (106)
    [t] = run([frame("AAA", bars)], stub)["trades"]
    assert t["exit_date"] == D[3]  # not D2: the 106 stop did not exist when D2's low printed


# ---------------------------------------------------------------- BREAKEVEN (13)

def _breakeven_case():
    stub = Stub({("AAA", D[0]): sig(entry=100, stop=90, target=130, trail=5.0)})
    bars = [
        (100, 101, 99, 100),   # D0 signal
        (100, 101, 99.5, 100), # D1 entry at 100 (risk 10 -> +1R = 110)
        (101, 111, 99, 108),   # D2 first reaches +1R; its own low 99 is below breakeven 100
        (104, 105, 99.5, 101), # D3: breakeven (100) now in force -> exit at 100
        (101, 102, 100.5, 101),
    ]
    return stub, bars


def test_13_breakeven_is_active_only_from_the_next_session_and_off_by_default():
    stub, bars = _breakeven_case()
    [t] = run([frame("AAA", bars)], stub, cfg(simulate_breakeven=True))["trades"]
    assert (t["exit_date"], t["exit_reason"], t["exit_price"]) == (D[3], "breakeven_stop", 100.0)

    stub, bars = _breakeven_case()
    [t] = run([frame("AAA", bars)], stub)["trades"]  # default: off
    assert t["exit_reason"] == "backtest_end"


# ---------------------------------------------------------------- COSTS (14-17)

def test_14_buy_side_charges_arithmetic():
    c = backtest_costs.compute_charges("BUY", 100_000)
    assert c == {"brokerage": 0.0, "stt": 100.0, "exchange_txn": 2.97, "sebi": 0.1, "gst": 0.55,
                 "stamp_duty": 15.0, "dp_charge": 0.0, "total": 118.62}


def test_15_sell_side_charges_arithmetic():
    c = backtest_costs.compute_charges("SELL", 100_000)
    assert c == {"brokerage": 0.0, "stt": 100.0, "exchange_txn": 2.97, "sebi": 0.1, "gst": 0.55,
                 "stamp_duty": 0.0, "dp_charge": 15.34, "total": 118.96}


def test_16_dp_charge_once_per_exit_and_brokerage_cap_and_disabled_costs():
    assert backtest_costs.compute_charges("BUY", 50_000)["dp_charge"] == 0.0
    assert backtest_costs.compute_charges("SELL", 50_000)["dp_charge"] == 15.34
    assert backtest_costs.compute_charges("SELL", 50_000, enabled=False)["total"] == 0.0
    custom = {**config.BACKTEST_COST_DEFAULTS, "brokerage_pct": 0.03, "brokerage_max_per_order": 20.0}
    c = backtest_costs.compute_charges("BUY", 1_000_000, custom)
    assert c["brokerage"] == 20.0  # 0.03% = 300, capped at 20
    assert c["gst"] == round((20 + 29.7 + 1.0) * 0.18, 2)
    with pytest.raises(ValueError):
        backtest_costs.compute_charges("SHORT", 1)


def test_17_gross_minus_costs_is_net_and_cash_reconciles():
    stub = Stub({("AAA", D[0]): sig()})
    bars = [(100, 101, 99, 100), (100, 101, 99, 100), (100, 106, 99, 105), (105, 106, 104, 105)]
    out = run([frame("AAA", bars)], stub, cfg(costs_enabled=True))
    [t] = out["trades"]
    qty = t["quantity"]
    buy = backtest_costs.compute_charges("BUY", 100.0 * qty)
    sell = backtest_costs.compute_charges("SELL", 105.0 * qty)
    assert t["gross_pnl"] == round(5.0 * qty, 2)
    assert t["entry_costs"] == buy["total"] and t["exit_costs"] == sell["total"]
    assert t["costs"] == round(buy["total"] + sell["total"], 2)
    assert t["net_pnl"] == round(t["gross_pnl"] - t["costs"], 2)
    assert t["cost_breakdown"]["dp_charge"] == 15.34 and t["cost_breakdown"]["stamp_duty"] == buy["stamp_duty"]
    assert out["final_cash"] == pytest.approx(1_000_000 + t["net_pnl"], abs=0.01)

    free = run([frame("AAA", bars)], Stub({("AAA", D[0]): sig()}), cfg(costs_enabled=False))
    assert free["trades"][0]["costs"] == 0.0
    assert free["trades"][0]["net_pnl"] == free["trades"][0]["gross_pnl"]


# ---------------------------------------------------------------- CAPITAL (18-20)

def test_18_insufficient_cash_skips_the_trade_and_cash_never_goes_negative():
    # Sized to spend ~all capital at the PLANNED 100; the open gaps to 110.
    stub = Stub({("AAA", D[0]): sig(entry=100, stop=50, target=200)})
    bars = [(100, 101, 99, 100), (110, 111, 109, 110), (110, 111, 109, 110)]
    out = run([frame("AAA", bars)], stub, cfg(capital_per_trade_pct=100, risk_per_trade_pct=100))
    assert out["trades"] == []
    [s] = out["skipped_signals"]
    assert s["reason"] == "insufficient_cash" and s["required"] > s["available"]
    assert min(p["cash"] for p in out["equity_curve"]) >= 0


def test_19_max_open_positions_is_respected():
    syms = ["AAA", "BBB", "CCC", "DDD"]
    stub = Stub({(s, D[0]): sig() for s in syms})
    out = run([frame(s, flat(4)) for s in syms], stub, cfg(max_open_positions=2))
    assert len(out["trades"]) == 2
    assert [s["reason"] for s in out["skipped_signals"]] == ["max_open_positions"] * 2
    assert max(p["open_positions"] for p in out["equity_curve"]) <= 2


def test_20_same_day_signals_are_allocated_by_rank_then_symbol_regardless_of_input_order():
    specs = {("MMM", D[0]): sig(rank=1.0), ("ZZZ", D[0]): sig(rank=5.0),
             ("AAA", D[0]): sig(rank=1.0), ("BBB", D[0]): sig(rank=0.5)}
    order_a = [frame(s, flat(4)) for s in ["MMM", "ZZZ", "AAA", "BBB"]]
    order_b = [frame(s, flat(4)) for s in ["BBB", "AAA", "ZZZ", "MMM"]]
    a = run(order_a, Stub(specs), cfg(max_open_positions=2))
    b = run(order_b, Stub(dict(reversed(list(specs.items())))), cfg(max_open_positions=2))
    assert [t["symbol"] for t in a["trades"]] == ["AAA", "ZZZ"]  # ZZZ (5.0), then AAA beats MMM on the tie
    assert a["trades"] == b["trades"] and a["skipped_signals"] == b["skipped_signals"]

    capped = run(order_a, Stub(specs), cfg(), params={**PARAMS, "max_signals_per_day": 1})
    assert [t["symbol"] for t in capped["trades"]] == ["ZZZ"]
    assert sorted(s["reason"] for s in capped["skipped_signals"]) == ["max_signals_per_day"] * 3


def test_sizing_uses_the_live_risk_formula_on_simulated_equity():
    stub = Stub({("AAA", D[0]): sig(entry=100, stop=90)})
    [t] = run([frame("AAA", flat(4))], stub, cfg(initial_capital=200_000))["trades"]
    # risk: 0.5% of 200k = 1000 / 10 per share = 100; capital: 10% of 200k / 100 = 200 -> min = 100
    assert t["quantity"] == 100


# ---------------------------------------------------------------- EQUITY (21-22)

def test_21_open_positions_are_marked_to_market_in_equity():
    stub = Stub({("AAA", D[0]): sig(entry=100, stop=50, target=500)})
    bars = [(100, 101, 99, 100), (100, 101, 99, 100), (100, 121, 99, 120), (120, 121, 119, 120)]
    out = run([frame("AAA", bars)], stub)
    qty = out["trades"][0]["quantity"]
    point = next(p for p in out["equity_curve"] if p["date"] == D[2])
    assert point["open_positions"] == 1
    assert point["market_value"] == 120.0 * qty
    assert point["equity"] == pytest.approx(point["cash"] + 120.0 * qty)
    assert point["equity"] == pytest.approx(1_000_000 + 20.0 * qty)


def test_22_max_drawdown_comes_from_the_mtm_curve_not_closed_trades():
    stub = Stub({("AAA", D[0]): sig(entry=100, stop=50, target=500)})
    bars = [(100, 101, 99, 100), (100, 101, 99, 100), (80, 81, 79, 80), (110, 111, 109, 110)]
    out = backtest.simulate_with_metrics(stub, {"AAA": frame("AAA", bars)}, PARAMS, cfg(), D[0], D[3])
    [t] = out["trades"]
    assert t["net_pnl"] > 0  # the only closed trade is a winner: closed-trade DD would be 0
    qty = t["quantity"]
    assert out["metrics"]["max_drawdown_amount"] == pytest.approx(20.0 * qty)
    assert out["metrics"]["max_drawdown_pct"] > 0


# ---------------------------------------------------------------- DATES (23-25)

def test_23_loader_never_returns_rows_after_end_date(env):
    candles = _golden_series(1000)
    seed_ohlc(env.db, 2953217, "TCS", [{**c, "date": _dt(c["date"])} for c in candles])
    full = indicators.load_ohlc_frame(2953217)
    cut = indicators.load_ohlc_frame(2953217, end_date="2026-03-02")
    assert full["date"].max() > "2026-03-02"
    assert cut["date"].max() <= "2026-03-02"
    assert cut["date"].min() == full["date"].min()  # every warm-up row kept


def test_23_simulate_refuses_frames_containing_future_rows():
    with pytest.raises(FutureDataError):
        run([frame("AAA", flat(10))], Stub(), end=D[5], start=D[0])


def test_24_warm_up_rows_before_start_are_used_but_never_traded():
    stub = Stub({("AAA", D[2]): sig(), ("AAA", D[6]): sig()})
    out = run([frame("AAA", flat(12))], stub, start=D[5], end=D[11])
    assert [t["signal_date"] for t in out["trades"]] == [D[6]]
    assert all(d >= D[5] for kind, _, d in stub.seen)
    assert out["data_window"]["data_load_start"] == D[0]
    assert all(p["date"] >= D[5] for p in out["equity_curve"])


def test_25_nothing_executes_after_end_date(env):
    candles = _golden_series(1017)
    seed_ohlc(env.db, 408065, "INFY", [{**c, "date": _dt(c["date"])} for c in candles])
    conn = env.db.get_connection()
    try:
        end = "2026-06-30"
        result = backtest.run_backtest(conn, "momentum_breakout", [(408065, "INFY")],
                                       dict(STRATEGIES["momentum_breakout"].DEFAULT_PARAMS),
                                       "2025-09-01", end, cfg=BacktestConfig.from_overrides())
    finally:
        conn.close()
    assert result["trades"], "fixture should produce trades"
    assert all(t["entry_date"] <= end and t["exit_date"] <= end for t in result["trades"])
    assert all(p["date"] <= end for p in result["equity_curve"])
    assert result["execution_config"]["data_window"]["last_bar_used"] <= end


def test_open_position_at_end_is_closed_at_the_last_bar_on_or_before_end_date():
    stub = Stub({("AAA", D[0]): sig(entry=100, stop=50, target=500)})
    bars = flat(3) + [(100, 101, 99, 107)]
    [t] = run([frame("AAA", bars)], stub)["trades"]
    assert (t["exit_date"], t["exit_reason"], t["exit_price"], t["forced_close"]) == (D[3], "backtest_end", 107.0, True)


def test_walk_forward_windows_never_see_a_later_window(env, monkeypatch):
    candles = _golden_series(1034)
    seed_ohlc(env.db, 738561, "RELIANCE", [{**c, "date": _dt(c["date"])} for c in candles])
    calls = []
    original = backtest.simulate

    def spy(module, frames, params, cfg_, start_date, end_date):
        calls.append((end_date, max(f.dates[-1] for f in frames.values())))
        return original(module, frames, params, cfg_, start_date, end_date)

    monkeypatch.setattr(backtest, "simulate", spy)
    conn = env.db.get_connection()
    try:
        backtest.run_backtest(conn, "trend_pullback", [(738561, "RELIANCE")],
                              dict(STRATEGIES["trend_pullback"].DEFAULT_PARAMS), "2025-09-01", "2026-06-30",
                              run_walk_forward=True, window_days=60)
    finally:
        conn.close()
    assert len(calls) > 3
    assert all(last <= end for end, last in calls)
    assert calls[1][1] < calls[-1][1]  # early windows really were cut short


# ---------------------------------------------------------------- UNIVERSE / DATA (26)

def test_26_current_watchlist_backtest_is_labelled_survivorship_biased(env):
    seed_ohlc(env.db, 2953217, "TCS", [{**c, "date": _dt(c["date"])} for c in _golden_series(1000)])
    add_watch(env.db, "TCS", 2953217)
    body = {"strategy_name": "momentum_breakout", "start_date": "2025-09-01", "end_date": "2026-06-30"}
    run_ = env.client.post("/api/strategies/backtests", json=body).json()["data"]
    assert run_["universe"]["source"] == "current_watchlist"
    assert run_["universe"]["survivorship_bias"] == "likely"
    assert any("survivorship bias possible" in w for w in run_["warnings"])

    explicit = env.client.post("/api/strategies/backtests", json={**body, "symbols": ["TCS"]}).json()["data"]
    assert explicit["universe"]["source"] == "user_supplied"
    assert explicit["universe"]["survivorship_bias"] == "unverified"


def test_suspected_corporate_action_gap_is_flagged_not_silently_a_stop_out():
    stub = Stub({("AAA", D[0]): sig(entry=100, stop=90, target=200)})
    bars = [(100, 101, 99, 100), (100, 101, 99, 100), (50, 51, 49, 50), (50, 51, 49, 50)]
    out = run([frame("AAA", bars)], stub)
    [t] = out["trades"]
    assert t["data_warning"] == "suspected_corporate_action_gap"
    assert out["data_warnings"][0]["date"] == D[2] and out["data_warnings"][0]["gap_pct"] == -50.0


# ---------------------------------------------------------------- REGRESSION (27-28)

# Produced by the PRE-PK-21 strategy code (git tag backup-pre-pk21) on the
# fixture below: PK-21 must not change which bars the strategies fire on.
GOLDEN_SIGNALS = {
    "momentum_breakout": [[1, "2025-12-19"], [1, "2026-01-20"], [1, "2026-08-04"],
                          [2, "2025-09-08"], [2, "2026-06-16"], [2, "2026-09-16"]],
    "trend_pullback": [[0, "2026-03-18"], [2, "2025-10-14"]],
    "rsi_mean_reversion": [[1, "2025-09-24"], [1, "2025-09-25"]],
}


def _golden_series(seed: int, n_bars: int = 650, data_end: date = date(2026, 9, 18)) -> list[dict]:
    """Seeded random walk with volume spikes and overnight gaps."""
    rng = random.Random(seed)
    days, d = [], data_end
    while len(days) < n_bars:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    prev_close = 400.0 + seed % 7 * 50
    candles = []
    for day in days:
        roll = rng.random()
        gap = -0.06 if roll < 0.025 else (0.05 if roll < 0.05 else 0.0)
        open_ = prev_close * (1 + gap + rng.gauss(0, 0.004))
        close = open_ * (1 + rng.gauss(0.0008, 0.016))
        high = max(open_, close) * (1 + abs(rng.gauss(0, 0.007)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, 0.007)))
        volume = int(100_000 * (1 + max(-0.5, rng.gauss(0, 0.2))))
        if rng.random() < 0.07 and close > open_:
            volume = int(volume * 4.5)
        candles.append({"date": day.isoformat(), "open": round(open_, 2), "high": round(high, 2),
                        "low": round(low, 2), "close": round(close, 2), "volume": volume})
        prev_close = close
    return candles


def _dt(iso: str):
    from datetime import datetime
    return datetime.fromisoformat(iso)


@pytest.mark.parametrize("name", sorted(GOLDEN_SIGNALS))
def test_27_strategy_signal_generation_is_unchanged(name):
    module = STRATEGIES[name]
    params = dict(module.DEFAULT_PARAMS)
    hits = []
    for n in range(4):
        df = pd.DataFrame(_golden_series(1000 + n * 17))
        for i in range(len(df)):
            if df["date"].iloc[i] >= "2025-09-01" and module.evaluate_entry(df.iloc[: i + 1], params)["passed"]:
                hits.append([n, df["date"].iloc[i]])
    assert hits == GOLDEN_SIGNALS[name]


def test_27_backtest_trades_only_come_from_real_strategy_signals():
    module = STRATEGIES["momentum_breakout"]
    frames = {f"S{n}": SymbolFrame(f"S{n}", pd.DataFrame(_golden_series(1000 + n * 17))) for n in range(4)}
    frames = {s: f.sliced("2026-08-31") for s, f in frames.items()}
    out = simulate(module, frames, dict(module.DEFAULT_PARAMS), BacktestConfig.from_overrides(),
                   "2025-09-01", "2026-08-31")
    golden = {(f"S{n}", d) for n, d in GOLDEN_SIGNALS["momentum_breakout"]}
    assert out["trades"]
    for t in out["trades"]:
        assert (t["symbol"], t["signal_date"]) in golden
        assert t["entry_date"] > t["signal_date"]
        f = frames[t["symbol"]]
        assert f.index[t["entry_date"]] == f.index[t["signal_date"]] + 1
        if t["exit_reason"] in ("thesis_invalidation", "time_exit"):
            i = f.index[t["exit_date"]]
            assert t["exit_price"] < f.open[i] + 1e-9  # the open, less slippage — never a close


def test_28_identical_inputs_give_identical_results():
    module = STRATEGIES["trend_pullback"]
    frames = {f"S{n}": SymbolFrame(f"S{n}", pd.DataFrame(_golden_series(1000 + n * 17))).sliced("2026-08-31")
              for n in range(4)}
    a = backtest.simulate_with_metrics(module, frames, dict(module.DEFAULT_PARAMS),
                                       BacktestConfig.from_overrides(), "2025-09-01", "2026-08-31")
    reversed_frames = dict(reversed(list(frames.items())))
    b = backtest.simulate_with_metrics(module, reversed_frames, dict(module.DEFAULT_PARAMS),
                                       BacktestConfig.from_overrides(), "2025-09-01", "2026-08-31")
    assert a == b


# ---------------------------------------------------------------- METRICS / PERSISTENCE

def test_metrics_are_net_and_complete():
    stub = Stub({("AAA", D[0]): sig(entry=100, stop=90, target=130), ("BBB", D[0]): sig(entry=100, stop=90, target=130)})
    win = [(100, 101, 99, 100), (100, 101, 99, 100), (110, 111, 109, 110), (110, 111, 109, 110)]
    lose = [(100, 101, 99, 100), (100, 101, 99, 100), (95, 96, 94, 95), (95, 96, 94, 95)]
    out = backtest.simulate_with_metrics(stub, {"AAA": frame("AAA", win), "BBB": frame("BBB", lose)},
                                         PARAMS, cfg(costs_enabled=True), D[0], D[3])
    m = out["metrics"]
    for key in ("initial_capital", "ending_equity", "gross_pnl", "costs", "net_pnl", "net_cagr_pct",
                "max_drawdown_pct", "win_rate", "profit_factor", "avg_trade", "median_trade",
                "avg_holding_days", "trades", "skipped_signals", "exposure_pct", "largest_win",
                "largest_loss", "expectancy_r", "max_consecutive_losses"):
        assert key in m
    assert m["trades"] == 2 and m["win_rate"] == 0.5
    assert m["net_pnl"] == pytest.approx(m["gross_pnl"] - m["costs"], abs=0.02)
    assert m["ending_equity"] == pytest.approx(m["initial_capital"] + m["net_pnl"], abs=0.02)
    assert m["net_cagr_pct"] is None  # a 3-day span is not annualised
    assert m["cost_breakdown"]["dp_charge"] == pytest.approx(2 * 15.34)


def test_api_round_trip_persists_the_audit_fields_and_old_runs_still_load(env):
    seed_ohlc(env.db, 2953217, "TCS", [{**c, "date": _dt(c["date"])} for c in _golden_series(1017)])
    body = {"strategy_name": "momentum_breakout", "start_date": "2025-09-01", "end_date": "2026-06-30",
            "symbols": ["TCS"], "initial_capital": 500000, "max_open_positions": 3,
            "entry_slippage_bps": 7, "exit_slippage_bps": 9, "costs_enabled": True}
    r = env.client.post("/api/strategies/backtests", json=body)
    assert r.status_code == 200, r.text
    run_ = r.json()["data"]
    assert run_["execution_config"]["initial_capital"] == 500000
    assert run_["execution_config"]["entry_slippage_bps"] == 7
    assert run_["execution_config"]["same_bar_policy"] == "stop_first"
    assert run_["trades"]

    stored = env.client.get(f"/api/strategies/backtests/{run_['id']}").json()["data"]
    assert stored["metrics"]["net_pnl"] == run_["metrics"]["net_pnl"]
    assert stored["equity_curve"] == run_["equity_curve"]
    t = stored["trades"][0]
    for key in ("signal_date", "quantity", "initial_stop", "initial_target", "gross_pnl", "costs",
                "net_pnl", "holding_days", "r_multiple", "net_r_multiple", "strategy_name"):
        assert t[key] is not None

    conn = env.db.get_connection()
    try:
        conn.execute(
            "INSERT INTO backtest_runs (strategy_name, params_snapshot, start_date, end_date, symbols_tested, "
            "total_signals) VALUES ('momentum_breakout', '{}', '2025-01-01', '2025-02-01', '[]', 0)")
        conn.commit()
        old_id = conn.execute("SELECT MAX(id) FROM backtest_runs").fetchone()[0]
    finally:
        conn.close()
    old = env.client.get(f"/api/strategies/backtests/{old_id}").json()["data"]
    assert old["metrics"] is None and old["execution_config"] is None
    assert env.client.get("/api/strategies/backtests").status_code == 200


def test_route_validates_config_and_dates(env):
    base = {"strategy_name": "momentum_breakout", "start_date": "2026-01-01", "end_date": "2026-02-01",
            "symbols": ["TCS"]}
    assert env.client.post("/api/strategies/backtests", json={**base, "entry_model": "same_bar"}).status_code == 422
    assert env.client.post("/api/strategies/backtests", json={**base, "initial_capital": -1}).status_code == 422
    assert env.client.post("/api/strategies/backtests", json={**base, "start_date": "2026-03-01"}).status_code == 400
    assert env.client.post("/api/strategies/backtests", json={**base, "end_date": "31-01-2026"}).status_code == 422


def test_backtest_makes_no_broker_call(env):
    seed_ohlc(env.db, 2953217, "TCS", [{**c, "date": _dt(c["date"])} for c in _golden_series(1000)])
    before = len(env.kite.calls)
    r = env.client.post("/api/strategies/backtests", json={
        "strategy_name": "momentum_breakout", "start_date": "2025-09-01", "end_date": "2026-06-30",
        "symbols": ["TCS"]})
    assert r.status_code == 200
    assert env.kite.calls[before:] == []  # sized on simulated capital: no holdings/margins read


def test_migration_is_idempotent(env):
    env.db.init_db()
    env.db.init_db()
    conn = env.db.get_connection()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(backtest_trades)")]
        run_cols = [r[1] for r in conn.execute("PRAGMA table_info(backtest_runs)")]
    finally:
        conn.close()
    assert cols.count("net_pnl") == 1 and run_cols.count("equity_curve") == 1
