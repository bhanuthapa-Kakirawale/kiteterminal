import pandas as pd

from backend import config, indicators
from backend.strategies import conditions as cond
from backend.strategies.shared import last_value, shared_exit_checks

STRATEGY_NAME = "trend_pullback"
EXIT_STRATEGY_NAME = "trend_pullback_exit"
STRATEGY_LABEL = "Trend pullback"
# See momentum_breakout.STRATEGY_DESCRIPTION for why this lives server-side.
STRATEGY_DESCRIPTION = (
    "Buys an established uptrend on a dip rather than a breakout: the stock must pull back to "
    "touch its own short-term moving average and then close back above it on heavy volume, "
    "confirming the dip is over and that real buyers are behind the recovery. The volume "
    "requirement is what keeps it selective — without it the pattern appears constantly and "
    "trading costs consume the edge."
)

# Revised 2026-09-19 by the same cost-aware study that reworked
# momentum_breakout. This strategy was by far the worst of the three: 1511
# trades for -295R net. Its problem was not a bad edge but a tiny one spread
# over far too many trades -- gross it was only -99R, so transaction costs
# alone accounted for two thirds of the damage. No exit rule, stop width or
# band setting fixed it; the trade count was the disease.
#
# The cure was the one thing this strategy had that momentum_breakout also
# needed: a participation filter. It had NO volume condition at all, so it
# bought every technically-valid dip regardless of whether anyone was buying.
#
#   volume_multiple   (new) 3.0   recovery bar must trade 3x its 20-day
#                                 average volume; cuts 1511 trades to 165
#   atr_stop_multiple 1.5 -> 3.5  it had the tightest stop of the three and
#                                 was being shaken out constantly
#   target_r_multiple 2.0 -> 4.0
#
# Result: 165 trades, 46.7% win rate, +15.2R net, profit factor 1.43, max
# drawdown 5.2R, positive in both halves (+10.8 / +4.4) and at every cost
# assumption tested up to 0.70%. The out-of-sample half is notably weaker
# than the in-sample half, so this one deserves more paper-trading
# confirmation than momentum_breakout before it is trusted.
DEFAULT_PARAMS = {
    "trend_filter_period": 200,
    "pullback_ma_period": 20,
    "pullback_band_pct": 1.5,
    "pullback_lookback_days": 3,
    "volume_multiple": 3.0,
    "atr_stop_multiple": 3.5,
    "target_r_multiple": 4,
    "trailing_atr_multiple": 1.5,
    "thesis_invalidation_ma": 50,
    "time_exit_days": 15,
    "breakeven_trigger_r": 1.0,
    "max_signals_per_day": 2,
}

_ATR_PERIOD = 14
_VOLUME_LOOKBACK_DAYS = 20  # matches momentum_breakout; only the multiple is configurable


def evaluate_entry(df: pd.DataFrame, params: dict) -> dict:
    """Pure function: an uptrend (close > sma_200) that recently pulled back
    to touch its own pullback EMA and is now confirmed recovering above it.
    Checklist shape/transparency matches momentum_breakout — every
    condition's actual values are returned regardless of pass/fail."""
    if len(df) < 2:
        return {"passed": False, "insufficient_history": True, "checklist": {}, "conditions": [], "markers": []}

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

    # Participation filter. Without it this strategy bought every technically
    # valid dip regardless of whether anyone else was buying, which is how it
    # reached 1500 trades and let costs eat the edge. Measured on the recovery
    # bar, because that is the bar claiming the dip is over.
    last_volume = float(df["volume"].iloc[-1])
    last_avg_vol = last_value(indicators.avg_volume(df["volume"], _VOLUME_LOOKBACK_DAYS))
    volume_multiple_actual = last_volume / last_avg_vol if last_avg_vol else None
    volume_ok = last_avg_vol is not None and last_volume > last_avg_vol * params["volume_multiple"]

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
        "volume": {
            "pass": bool(volume_ok),
            "volume": last_volume,
            f"avg_volume_{_VOLUME_LOOKBACK_DAYS}": last_avg_vol,
            "volume_multiple_actual": volume_multiple_actual,
            "volume_multiple_required": params["volume_multiple"],
        },
    }
    # Flat record shared by the card's one-liner and the Signal Detail panel
    # (see strategies/conditions.py), derived from the same locals the nested
    # checklist above uses.
    signal_date = df["date"].iloc[-1]
    # The band the touch is measured against is THAT bar's own EMA, not
    # today's applied retroactively — same value the touch_mask test used.
    touch_band = None if touched_ema is None else touched_ema * (1 + band_pct / 100)
    # Compound rule: close must clear BOTH the previous close and the EMA, so
    # the binding side is whichever is higher. Reporting only one of them
    # would let the panel claim a comfortable pass on a marginal recovery.
    recovery_threshold = max(prev_close, last_ema) if last_ema is not None else prev_close
    ema_binds = last_ema is not None and last_ema > prev_close
    recovery_threshold_label = (
        f"{ma_period}-EMA {cond.inr(last_ema)}" if ema_binds else f"prev close {cond.inr(prev_close)}"
    )
    # Only the slack side goes in `detail` — repeating the binding side there
    # made the card's one-liner state the same number twice.
    recovery_detail = (
        f"prev close {cond.inr(prev_close)} also cleared" if ema_binds
        else f"{ma_period}-EMA {cond.inr(last_ema)} also cleared"
    )
    entry_conditions = [
        cond.condition(
            "Uptrend filter",
            f"Close must be above the {trend_period}-day SMA (long-term uptrend intact)",
            actual_value=last_close, threshold=last_sma, operator=">", passed=trend_ok,
            value_label=f"close {cond.inr(last_close)}",
            threshold_label=f"SMA-{trend_period} {cond.inr(last_sma)}",
        ),
        cond.condition(
            f"Pullback to {ma_period}-EMA",
            f"Within the last {window} days, a bar's low must have come back to within "
            f"{band_pct}% of that bar's own {ma_period}-EMA",
            actual_value=touched_low, threshold=touch_band, operator="≤", passed=pullback_ok,
            value_label=f"low {cond.inr(touched_low)}",
            threshold_label=f"{band_pct}% band {cond.inr(touch_band)}",
            detail=None if touched_date is None
            else f"touched {touched_date}, {ma_period}-EMA was {cond.inr(touched_ema)} that day",
        ),
        cond.condition(
            "Recovery confirmed",
            f"Close must be above BOTH the previous close and the {ma_period}-EMA — "
            f"the dip is over, not still in progress",
            actual_value=last_close, threshold=recovery_threshold, operator=">", passed=reversal_ok,
            value_label=f"close {cond.inr(last_close)}",
            threshold_label=recovery_threshold_label,
            detail=recovery_detail,
        ),
        cond.condition(
            "Volume surge",
            f"The recovery bar must trade above {params['volume_multiple']}x its "
            f"{_VOLUME_LOOKBACK_DAYS}-day average volume — real buyers stepping in, not just "
            f"a quiet drift back up",
            actual_value=volume_multiple_actual, threshold=params["volume_multiple"],
            operator=">", passed=volume_ok,
            value_label=cond.multiple(volume_multiple_actual),
            threshold_label=f"{cond.multiple(params['volume_multiple'])} avg-{_VOLUME_LOOKBACK_DAYS} volume",
            detail=None if last_avg_vol is None else f"{last_volume:,.0f} vs {last_avg_vol:,.0f} average",
        ),
    ]
    markers = [
        m for m in (
            cond.marker(
                "pullback", touched_date, touched_low, f"Pullback to the {ma_period}-EMA",
                None if touched_ema is None
                else f"low {cond.inr(touched_low)} against a {ma_period}-EMA of {cond.inr(touched_ema)}",
            ) if touched_date is not None else None,
            cond.marker(
                "recovery", signal_date, last_close, "Recovery bar",
                f"closed {cond.inr(last_close)}, above both the previous close "
                f"{cond.inr(prev_close)} and the {ma_period}-EMA {cond.inr(last_ema)}",
            ),
        ) if m is not None
    ]

    return {
        "passed": bool(trend_ok and pullback_ok and reversal_ok and volume_ok),
        "checklist": checklist,
        "conditions": entry_conditions,
        "markers": markers,
        "date": signal_date,
        "close": last_close,
        "atr_14": last_atr,
        "ema_pullback": last_ema,
        "volume_multiple_actual": volume_multiple_actual,
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
    """Rendered from the candidate's own `conditions` record — see
    momentum_breakout.build_entry_rationale for the rationale behind this
    being generic rather than per-module prose."""
    return cond.build_rationale(STRATEGY_LABEL, candidate["conditions"])


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
