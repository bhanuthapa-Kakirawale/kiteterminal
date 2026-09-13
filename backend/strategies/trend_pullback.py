import pandas as pd

from backend import config, indicators
from backend.strategies.shared import last_value, shared_exit_checks

STRATEGY_NAME = "trend_pullback"
EXIT_STRATEGY_NAME = "trend_pullback_exit"

DEFAULT_PARAMS = {
    "trend_filter_period": 200,
    "pullback_ma_period": 20,
    "pullback_band_pct": 1.5,
    "pullback_lookback_days": 3,
    "atr_stop_multiple": 1.5,
    "target_r_multiple": 2,
    "trailing_atr_multiple": 1.5,
    "thesis_invalidation_ma": 50,
    "time_exit_days": 15,
    "breakeven_trigger_r": 1.0,
    "max_signals_per_day": 2,
}

_ATR_PERIOD = 14


def evaluate_entry(df: pd.DataFrame, params: dict) -> dict:
    """Pure function: an uptrend (close > sma_200) that recently pulled back
    to touch its own pullback EMA and is now confirmed recovering above it.
    Checklist shape/transparency matches momentum_breakout — every
    condition's actual values are returned regardless of pass/fail."""
    if len(df) < 2:
        return {"passed": False, "insufficient_history": True, "checklist": {}}

    close = df["close"]
    trend_period = params["trend_filter_period"]
    ma_period = params["pullback_ma_period"]
    lookback = params["pullback_lookback_days"]
    band_pct = params["pullback_band_pct"]

    last_close = float(close.iloc[-1])
    prev_close = float(close.iloc[-2])
    last_sma = last_value(indicators.sma(close, trend_period))
    ema_series = indicators.ema(close, ma_period)
    last_ema = last_value(ema_series)
    last_atr = last_value(indicators.wilders_atr(df, _ATR_PERIOD))

    trend_ok = last_sma is not None and last_close > last_sma

    # Pullback check: for each of the last `lookback` days (today included),
    # compare THAT day's own low against THAT day's own pullback EMA — not
    # today's EMA applied retroactively to past lows. A touch on any of
    # those days satisfies the condition.
    window = min(lookback, len(df))
    lows = df["low"].tail(window)
    emas = ema_series.tail(window)
    touch_mask = lows <= emas * (1 + band_pct / 100)
    pullback_ok = bool(touch_mask.any())
    touch_idx = touch_mask[touch_mask].index[-1] if pullback_ok else None
    touched_date = df["date"].loc[touch_idx] if touch_idx is not None else None
    touched_low = float(df["low"].loc[touch_idx]) if touch_idx is not None else None
    touched_ema = float(ema_series.loc[touch_idx]) if touch_idx is not None else None

    reversal_ok = (
        last_ema is not None
        and last_close > prev_close
        and last_close > last_ema
    )

    checklist = {
        "trend_filter": {
            "pass": bool(trend_ok), "close": last_close, f"sma_{trend_period}": last_sma,
        },
        "pullback": {
            "pass": pullback_ok, "lookback_days": window,
            "touched_date": str(touched_date) if touched_date is not None else None,
            "touched_low": touched_low, f"ema_{ma_period}_at_touch": touched_ema,
            "band_pct": band_pct,
        },
        "reversal": {
            "pass": bool(reversal_ok), "close": last_close, "prev_close": prev_close,
            f"ema_{ma_period}": last_ema,
        },
    }
    return {
        "passed": bool(trend_ok and pullback_ok and reversal_ok),
        "checklist": checklist,
        "date": df["date"].iloc[-1],
        "close": last_close,
        "atr_14": last_atr,
        "ema_pullback": last_ema,
    }


def compute_entry_levels(candidate: dict, params: dict) -> dict | None:
    """Same entry-price-buffer convention and stop/target/trailing formula
    shape as momentum_breakout — only the multiples differ (tighter stop,
    since entries here are closer to support)."""
    close = candidate["close"]
    atr = candidate["atr_14"]
    if atr is None or atr <= 0:
        return None

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
    ma_period = params["pullback_ma_period"]
    trend_period = params["trend_filter_period"]
    pullback = candidate["checklist"]["pullback"]
    return (
        f"Trend pullback: close ₹{candidate['close']:.2f} pulled back to touch the {ma_period}-EMA "
        f"(₹{pullback[f'ema_{ma_period}_at_touch']:.2f} on {pullback['touched_date']}) and is now confirmed "
        f"recovering above it, with the {trend_period}-day uptrend still intact."
    )


def evaluate_exit(position: dict, df: pd.DataFrame, params: dict) -> dict:
    """No strategy-unique exit condition (per the Phase 7 spec) — the shared
    thesis-invalidation/time-exit building block is the whole story, same
    as momentum_breakout."""
    return shared_exit_checks(position, df, params)


def rank_key(candidate: dict) -> float:
    """Ranks passing candidates by how far price has already recovered
    above its pullback EMA — a bigger confirmed bounce ranks first.
    Judgment call: the spec didn't specify a ranking metric for this
    module (only for momentum_breakout), so this mirrors that module's
    "how decisive is the signal" spirit using this strategy's own
    reference level instead of a Donchian breakout level."""
    ema = candidate.get("ema_pullback")
    close = candidate.get("close")
    if not ema:
        return 0.0
    return (close - ema) / ema * 100
