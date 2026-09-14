import pandas as pd

from backend import indicators


def last_value(series: pd.Series) -> float | None:
    if len(series) == 0 or pd.isna(series.iloc[-1]):
        return None
    return float(series.iloc[-1])


def shared_exit_checks(position: dict, df: pd.DataFrame, params: dict) -> dict:
    """Thesis-invalidation + time-exit checklist, shared by every strategy
    module's evaluate_exit — moved verbatim from Phase 5's
    strategy_engine.evaluate_exit (momentum_breakout's original exit logic).
    A module with no strategy-unique exit condition (momentum_breakout,
    trend_pullback) returns this result directly; a module with its own
    extra condition (rsi_mean_reversion's rsi_exit_level) checks that first
    and ORs it into this result.

    Thesis invalidation ("close back below the EMA") is only ARMED once price
    has closed above that EMA at or after entry. Breakout/pullback entries
    are above it on the signal bar, so for them this changes nothing. An
    oversold mean-reversion entry is usually BELOW its 50-EMA by design —
    without arming, the very next scan flagged "thesis invalidated" and
    proposed exiting a trade that was doing exactly what it was bought for."""
    thesis_period = params["thesis_invalidation_ma"]
    close = df["close"]
    last_close = float(close.iloc[-1])
    ema_series = indicators.ema(close, thesis_period)
    last_ema = last_value(ema_series)

    entry_date_str = (position.get("executed_at") or df["date"].iloc[0])[:10]
    since_entry = df["date"] >= entry_date_str
    closes_since, emas_since = close[since_entry], ema_series[since_entry]
    armed = bool((closes_since > emas_since).any()) if len(closes_since) else True
    thesis_invalid = armed and last_ema is not None and last_close < last_ema

    days_held = int((df["date"] > entry_date_str).sum())
    stop_hit = last_close <= position["stop_loss"]
    target_hit = last_close >= position["target_1"]
    time_exit = days_held > params["time_exit_days"] and not stop_hit and not target_hit

    reasons = []
    if thesis_invalid:
        reasons.append(f"close {last_close:.2f} below {thesis_period}-EMA {last_ema:.2f} (thesis invalidated)")
    if time_exit:
        reasons.append(f"held {days_held} trading days without hitting stop or target (limit {params['time_exit_days']})")

    checklist = {
        "thesis_invalidation": {
            "pass": bool(thesis_invalid), "close": last_close, f"ema_{thesis_period}": last_ema, "armed": armed,
        },
        "time_exit": {
            "pass": bool(time_exit), "days_held": days_held, "limit_days": params["time_exit_days"],
            "stop_hit": bool(stop_hit), "target_hit": bool(target_hit),
        },
    }
    return {
        "passed": bool(thesis_invalid or time_exit),
        "checklist": checklist,
        "rationale": "; ".join(reasons) if reasons else None,
        "close": last_close,
    }
