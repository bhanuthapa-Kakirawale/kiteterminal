import pandas as pd

from backend import config, indicators
from backend.strategies.shared import last_value, shared_exit_checks

STRATEGY_NAME = "rsi_mean_reversion"
EXIT_STRATEGY_NAME = "rsi_mean_reversion_exit"

DEFAULT_PARAMS = {
    "trend_filter_period": 200,
    "rsi_oversold_threshold": 35,
    "rsi_turn_confirmation": True,
    "swing_low_lookback_days": 10,
    "atr_stop_multiple": 1.0,
    "target_r_multiple": 1.5,
    "rsi_exit_level": 55,
    "thesis_invalidation_ma": 50,
    "time_exit_days": 10,
    "breakeven_trigger_r": 1.0,
    "max_signals_per_day": 2,
}

_ATR_PERIOD = 14
_RSI_PERIOD = 14

# Judgment call: the spec's params list for this module has no
# trailing_atr_multiple (unlike momentum_breakout/trend_pullback) — profit
# here is taken via target_1 or the rsi_exit_level exit condition, not a
# ratcheting stop, consistent with this strategy's "shorter/quicker trades"
# philosophy. compute_entry_levels below returns trailing_distance=None,
# and the shared ticket-builder in strategy_engine.py sets
# trailing_enabled=0 whenever a module doesn't provide one.


def evaluate_entry(df: pd.DataFrame, params: dict) -> dict:
    """Pure function. Trend filter is a HARD reject — this strategy must
    never fire without close > sma_200 passing, no matter how oversold RSI
    gets, specifically to avoid buying a falling knife in a genuine
    downtrend (per the spec's explicit requirement)."""
    if len(df) < 2:
        return {"passed": False, "insufficient_history": True, "checklist": {}}

    close = df["close"]
    trend_period = params["trend_filter_period"]
    threshold = params["rsi_oversold_threshold"]

    last_close = float(close.iloc[-1])
    last_sma = last_value(indicators.sma(close, trend_period))
    rsi_series = indicators.wilders_rsi(close, _RSI_PERIOD)
    last_rsi = last_value(rsi_series)
    prev_rsi = float(rsi_series.iloc[-2]) if len(rsi_series) >= 2 and pd.notna(rsi_series.iloc[-2]) else None
    last_atr = last_value(indicators.wilders_atr(df, _ATR_PERIOD))

    trend_ok = last_sma is not None and last_close > last_sma

    oversold_ok = last_rsi is not None and last_rsi < threshold
    turning_ok = True
    if params["rsi_turn_confirmation"]:
        turning_ok = prev_rsi is not None and last_rsi is not None and last_rsi > prev_rsi
    rsi_ok = oversold_ok and turning_ok

    checklist = {
        "trend_filter": {
            "pass": bool(trend_ok), "close": last_close, f"sma_{trend_period}": last_sma,
        },
        "rsi_condition": {
            "pass": bool(rsi_ok), "rsi_14": last_rsi, "prev_rsi_14": prev_rsi,
            "oversold_threshold": threshold, "turn_confirmation_required": params["rsi_turn_confirmation"],
            "oversold_pass": bool(oversold_ok), "turning_pass": bool(turning_ok),
        },
    }
    return {
        # Trend filter is evaluated and reported even when it fails, but a
        # failing trend filter unconditionally blocks the signal — the
        # spec's explicit "reject candidate entirely" requirement — so
        # `passed` is NOT `trend_ok and rsi_ok`'s usual symmetric AND in
        # spirit only; it IS exactly that AND, but documented here so the
        # hard-reject intent is unambiguous to a future reader.
        "passed": bool(trend_ok and rsi_ok),
        "checklist": checklist,
        "date": df["date"].iloc[-1],
        "close": last_close,
        "atr_14": last_atr,
        "swing_low": float(df["low"].tail(params["swing_low_lookback_days"]).min()),
    }


def compute_entry_levels(candidate: dict, params: dict) -> dict | None:
    close = candidate["close"]
    atr = candidate["atr_14"]
    swing_low = candidate["swing_low"]
    if atr is None or atr <= 0:
        return None

    entry_price = round(close * (1 + config.ENTRY_PRICE_BUFFER_PCT / 100), 2)

    # Stop-loss: spec says "min(swing_low, entry - atr*mult) — use whichever
    # is tighter (higher stop price for a long)". For a long, min() picks
    # the LOWER of the two prices, which is the opposite of tighter/higher —
    # a contradiction in the spec. Resolved in favor of the parenthetical's
    # stated intent (the genuinely tighter, higher stop), which also matches
    # this module's own "cut quickly if wrong" philosophy: max(), not min().
    atr_stop = entry_price - atr * params["atr_stop_multiple"]
    stop_loss = round(max(swing_low, atr_stop), 2)
    if stop_loss <= 0 or stop_loss >= entry_price:
        return None
    risk_per_share = entry_price - stop_loss
    target_1 = round(entry_price + risk_per_share * params["target_r_multiple"], 2)
    return {
        "entry_price": entry_price, "stop_loss": stop_loss,
        "target_1": target_1, "trailing_distance": None,
    }


def build_entry_rationale(candidate: dict, params: dict) -> str:
    rsi_info = candidate["checklist"]["rsi_condition"]
    trend_period = params["trend_filter_period"]
    sma_level = candidate["checklist"]["trend_filter"][f"sma_{trend_period}"]
    return (
        f"RSI mean-reversion: RSI-14 at {rsi_info['rsi_14']:.1f} is oversold (below {rsi_info['oversold_threshold']}) "
        f"and turning up from {rsi_info['prev_rsi_14']:.1f}, with price still above the {trend_period}-day "
        f"SMA ₹{sma_level:.2f} (uptrend intact)."
    )


def evaluate_exit(position: dict, df: pd.DataFrame, params: dict) -> dict:
    """Checks this module's own rsi_exit_level condition FIRST, then falls
    back to the shared thesis-invalidation/time-exit building blocks — per
    the spec's "checks its own condition first, then falls back to the
    shared checks." Both are always evaluated and reported; passed is true
    if either fires."""
    last_close = float(df["close"].iloc[-1])
    last_rsi = last_value(indicators.wilders_rsi(df["close"], _RSI_PERIOD))
    rsi_recovered = last_rsi is not None and last_rsi >= params["rsi_exit_level"]

    shared = shared_exit_checks(position, df, params)

    checklist = {
        "rsi_recovery": {
            "pass": bool(rsi_recovered), "rsi_14": last_rsi, "exit_level": params["rsi_exit_level"],
        },
        **shared["checklist"],
    }
    reasons = []
    if rsi_recovered:
        reasons.append("RSI recovered to neutral, taking reversion profit")
    if shared["rationale"]:
        reasons.append(shared["rationale"])

    return {
        "passed": bool(rsi_recovered or shared["passed"]),
        "checklist": checklist,
        "rationale": "; ".join(reasons) if reasons else None,
        "close": last_close,
    }


def rank_key(candidate: dict) -> float:
    """Ranks passing candidates by how oversold RSI was (more room below
    the threshold = a more prototypical setup) — judgment call, the spec
    didn't specify a ranking metric for this module."""
    rsi = candidate["checklist"]["rsi_condition"]["rsi_14"]
    threshold = candidate["checklist"]["rsi_condition"]["oversold_threshold"]
    if rsi is None:
        return 0.0
    return threshold - rsi
