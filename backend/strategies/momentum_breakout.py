import pandas as pd

from backend import config, indicators
from backend.strategies import conditions as cond
from backend.strategies.shared import last_value, shared_exit_checks

STRATEGY_NAME = "momentum_breakout"
EXIT_STRATEGY_NAME = "momentum_breakout_exit"
STRATEGY_LABEL = "Momentum breakout"
# Plain-English statement of the rule, served to the UI rather than hardcoded
# there, so adding a fourth strategy never requires touching frontend/app.js.
STRATEGY_DESCRIPTION = (
    "Buys a stock breaking out to a new multi-week high on unusually heavy volume, while its "
    "long-term trend is still up. The premise is that a decisive break of a level the stock has "
    "failed at repeatedly, backed by real volume, tends to keep going. Because breakouts can "
    "retrace sharply, it uses a wide ATR-based stop and trails it upward once the trade is "
    "working."
)

# Revised 2026-09-19 after a cost-aware parameter study over 63 NSE series,
# 2024-07..2026-09, split in half for out-of-sample checking. The shipped
# originals (volume 1.5, stop 2.0 ATR, target 2R, Donchian 20) lost 100R net
# of costs. Four independent sweeps all pointed the same way: the strategy was
# entering on marginal volume and being stopped out too tightly.
#
#   volume_multiple 1.5 -> 3.0    demanding real participation behind the
#                                 break; monotonic improvement 1.5..3.25
#   atr_stop_multiple 2.0 -> 3.0  a wider stop both survives normal noise and
#                                 shrinks cost drag, since a fixed-% round
#                                 trip is a smaller fraction of a larger 1R
#   target_r_multiple 2.0 -> 3.0  the winners that ran paid for everything
#   donchian_lookback 20 -> 30    fewer, more meaningful breakouts
#
# Result over the same period: 137 trades, 49.6% win rate, +16.9R net of a
# 0.40% round-trip cost, profit factor 1.43, max drawdown 6.7R, positive in
# BOTH halves and still positive at a pessimistic 0.70% cost assumption.
# Each value sits in the middle of a broad plateau rather than on a peak.
DEFAULT_PARAMS = {
    "donchian_lookback": 30,
    "volume_multiple": 3.0,
    "trend_filter_period": 200,
    "atr_stop_multiple": 3,
    "target_r_multiple": 3,
    "trailing_atr_multiple": 1.5,
    "thesis_invalidation_ma": 50,
    "time_exit_days": 15,
    "breakeven_trigger_r": 1.0,
    "max_signals_per_day": 3,
}

_VOLUME_LOOKBACK_DAYS = 20  # fixed per spec ("avg_volume_20") — only volume_multiple is configurable
_ATR_PERIOD = 14  # fixed per spec — atr_stop_multiple/trailing_atr_multiple apply to ATR-14, not a configurable period


def evaluate_entry(df: pd.DataFrame, params: dict) -> dict:
    """Pure function: evaluates the three momentum-breakout entry conditions
    against the LAST row of `df` (a symbol's full OHLC history, ordered by
    date, as returned by indicators.load_ohlc_frame). Returns a checklist
    with every condition's actual values regardless of pass/fail — same
    transparency philosophy as the Phase 4 risk breakdown, never a single
    opaque score. This exact function is also called by backend/backtest.py
    at every simulated day, so live and backtested signal logic can never
    drift apart."""
    if len(df) < 2:
        return {"passed": False, "insufficient_history": True, "checklist": {}, "conditions": [], "markers": []}

    close = df["close"]
    trend_period = params["trend_filter_period"]
    donchian_period = params["donchian_lookback"]

    last_close = float(close.iloc[-1])
    last_volume = float(df["volume"].iloc[-1])
    last_sma = last_value(indicators.sma(close, trend_period))
    last_donchian = last_value(indicators.donchian_high_excl_today(close, donchian_period))
    last_avg_vol = last_value(indicators.avg_volume(df["volume"], _VOLUME_LOOKBACK_DAYS))
    last_atr = last_value(indicators.wilders_atr(df, _ATR_PERIOD))

    trend_ok = last_sma is not None and last_close > last_sma
    breakout_ok = last_donchian is not None and last_close > last_donchian
    volume_ok = last_avg_vol is not None and last_volume > last_avg_vol * params["volume_multiple"]

    breakout_pct = (
        (last_close - last_donchian) / last_donchian * 100 if last_donchian else None
    )
    volume_multiple_actual = last_volume / last_avg_vol if last_avg_vol else None

    checklist = {
        "trend_filter": {
            "pass": bool(trend_ok),
            "close": last_close,
            f"sma_{trend_period}": last_sma,
        },
        "breakout": {
            "pass": bool(breakout_ok),
            "close": last_close,
            f"donchian_high_{donchian_period}": last_donchian,
            "breakout_pct_above_level": breakout_pct,
        },
        "volume": {
            "pass": bool(volume_ok),
            "volume": last_volume,
            f"avg_volume_{_VOLUME_LOOKBACK_DAYS}": last_avg_vol,
            "volume_multiple_actual": volume_multiple_actual,
            "volume_multiple_required": params["volume_multiple"],
        },
    }
    # The flat, uniform record the Approvals card's one-liner and the Signal
    # Detail panel both render from (see strategies/conditions.py). It is
    # derived from the same locals the nested checklist above uses — never a
    # second evaluation — so the two can never disagree about what fired.
    signal_date = df["date"].iloc[-1]
    entry_conditions = [
        cond.condition(
            "Uptrend filter",
            f"Close must be above the {trend_period}-day SMA (long-term uptrend intact)",
            actual_value=last_close, threshold=last_sma, operator=">", passed=trend_ok,
            value_label=f"close {cond.inr(last_close)}",
            threshold_label=f"SMA-{trend_period} {cond.inr(last_sma)}",
        ),
        cond.condition(
            "Donchian breakout",
            f"Close must break above the highest close of the prior {donchian_period} days "
            f"(the current day is excluded from its own comparison level)",
            actual_value=last_close, threshold=last_donchian, operator=">", passed=breakout_ok,
            value_label=f"close {cond.inr(last_close)}",
            threshold_label=f"{donchian_period}-day high {cond.inr(last_donchian)}",
            detail=None if breakout_pct is None else f"{breakout_pct:+.2f}% past the level",
        ),
        cond.condition(
            "Volume surge",
            f"Volume must exceed {params['volume_multiple']}× the {_VOLUME_LOOKBACK_DAYS}-day "
            f"average, confirming real participation behind the break",
            actual_value=volume_multiple_actual, threshold=params["volume_multiple"],
            operator=">", passed=volume_ok,
            value_label=cond.multiple(volume_multiple_actual),
            threshold_label=f"{cond.multiple(params['volume_multiple'])} avg-{_VOLUME_LOOKBACK_DAYS} volume",
            detail=None if last_avg_vol is None else f"{last_volume:,.0f} vs {last_avg_vol:,.0f} average",
        ),
    ]
    markers = [
        cond.marker(
            "breakout", signal_date, last_close, "Breakout bar",
            None if last_donchian is None else f"closed {cond.inr(last_close)} above the "
                                               f"{donchian_period}-day high {cond.inr(last_donchian)}",
        ),
    ]

    return {
        "passed": bool(trend_ok and breakout_ok and volume_ok),
        "checklist": checklist,
        "conditions": entry_conditions,
        "markers": markers,
        "date": signal_date,
        "close": last_close,
        "atr_14": last_atr,
        "breakout_pct_above_level": breakout_pct,
        "volume_multiple_actual": volume_multiple_actual,
    }


def compute_entry_levels(candidate: dict, params: dict) -> dict | None:
    """Pure function: derives entry_price/stop_loss/target_1/trailing_distance
    from a passing evaluate_entry() candidate, per the strategy's formulas.
    Called by both the live ticket-builder (strategy_engine.py) and
    backend/backtest.py, so a simulated trade's entry mechanics can never
    drift from a live one's."""
    close = candidate["close"]
    atr = candidate["atr_14"]
    if atr is None or atr <= 0:
        return None

    # Signals are generated by the nightly job AFTER today's candle has
    # already closed — a LIMIT order "at today's close" cannot be placed
    # retroactively. Buffer slightly above close so the order has a
    # realistic chance of filling on the small gap-up that commonly follows
    # a genuine breakout (documented judgment call).
    entry_price = round(close * (1 + config.ENTRY_PRICE_BUFFER_PCT / 100), 2)
    stop_loss = round(entry_price - atr * params["atr_stop_multiple"], 2)
    if stop_loss <= 0:
        return None
    risk_per_share = entry_price - stop_loss
    target_1 = round(entry_price + risk_per_share * params["target_r_multiple"], 2)
    trailing_distance = round(atr * params["trailing_atr_multiple"], 2)
    return {
        "entry_price": entry_price, "stop_loss": stop_loss,
        "target_1": target_1, "trailing_distance": trailing_distance,
    }


def build_entry_rationale(candidate: dict, params: dict) -> str:
    """Rendered from the candidate's own `conditions` record rather than from
    hand-picked checklist keys, so this sentence and the Signal Detail
    panel's itemised rows can never describe the signal differently."""
    return cond.build_rationale(STRATEGY_LABEL, candidate["conditions"])


def evaluate_exit(position: dict, df: pd.DataFrame, params: dict) -> dict:
    """Pure function: given one open position (needs at least entry_price,
    stop_loss, target_1, executed_at) and the symbol's full OHLC history,
    evaluates thesis-invalidation and time-exit independently. Called
    identically by the live scanner and backend/backtest.py. No
    strategy-unique exit condition for momentum_breakout — the shared
    building block is the whole story."""
    return shared_exit_checks(position, df, params)


def rank_key(candidate: dict) -> float:
    """Ranks passing candidates by breakout strength (most decisive break of
    the trigger level first) when max_signals_per_day caps results —
    documented judgment call from the Phase 5 plan, extracted unchanged."""
    return candidate["breakout_pct_above_level"] or 0
