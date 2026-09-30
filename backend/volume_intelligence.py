"""Volume Intelligence (PK-23): descriptive volume features for one instrument.

Turns a symbol's stored daily OHLCV into structured, versioned features —
relative volume, traded value, bar structure, OBV, contraction/expansion,
pullback and breakout participation, accumulation-/distribution-like
evidence and abnormal-volume flags — plus a few factual sentences about them.

What this module is NOT: a score, a signal, or a claim about who traded.
Nothing here feeds entry acceptance, sizing, risk checks or exits; the three
strategy modules keep their own volume rule untouched (see
strategy_volume_comparison for the side-by-side view). "Accumulation-like"
means exactly the rule written in DEFAULT_THRESHOLDS, nothing more.

Design rules
------------
* The core (everything above the "DB-reading wrapper" section) is pure: it
  takes a DataFrame with date/open/high/low/close/volume and returns data.
  No DB, no broker, and the input frame is never modified.
* No lookahead. Every value at date T is built from rows <= T only, and every
  reference baseline EXCLUDES T itself (shift(1) before the rolling window),
  so an extreme bar can never inflate its own denominator. The strategies'
  SMA-20 volume average does include the signal bar; that is reproduced only
  in `legacy_volume_multiple_20`, for comparison, never reused.
* Window-local. No feature needs more than LOOKBACK_BARS rows (the current
  bar plus the 60 prior sessions of the volume percentile), so compute_at
  only ever looks at that window and OBV is exposed only through
  start-invariant derivatives (a cumulative OBV level depends on where the
  stored history happens to begin and means nothing on its own).
* Untrusted history is not computed on. PK-22's SUSPECT / NEEDS_REFRESH
  status, an unverified corporate-action-sized gap inside the window, or an
  invalid bar (NaN, negative volume, high < low, duplicate date) makes the
  result UNAVAILABLE with the reason, instead of a silently wrong number.
"""

import hashlib
import json
import logging
import math
import sqlite3
from collections import OrderedDict
from datetime import date
from threading import Lock
from types import MappingProxyType

import numpy as np
import pandas as pd

from backend import config, indicators, ohlc_integrity, trading_calendar

logger = logging.getLogger(__name__)

# Bump when any feature definition or default threshold changes, so a PK-24
# score is always reproducible against the definition it was fitted on.
FEATURE_VERSION = 1

BARS_REQUIRED = 21      # the current bar + the 20 prior sessions of RVOL20
LOOKBACK_BARS = 61      # the current bar + the 60 prior sessions of the percentile
_PERCENTILE_WINDOW = 60
_SLOPE_WINDOW = 10
_PULLBACK_LOOKBACK = 20

OK, DEGRADED, UNAVAILABLE = "OK", "DEGRADED", "UNAVAILABLE"

# Every threshold a flag depends on, in one place. Echoed in every result.
DEFAULT_THRESHOLDS = MappingProxyType({
    "elevated_rvol": 1.5,            # RVOL20 at or above this is "elevated" / "high" volume
    "strong_close": 0.75,            # close location in the upper quartile of the bar
    "weak_close": 0.25,              # close location in the lower quartile
    "upper_wick": 0.5,               # upper wick at least half the bar's range
    "reversal_close_max": 0.5,       # ... and the close in the lower half
    "contraction_ratio": 0.8,        # prior-5 / prior-20 average volume at or below this
    "low_volume_pullback_ratio": 0.8,  # pullback volume / volume into the swing high
    "abnormal_percentile": 90.0,
    "abnormal_rvol_median": 2.0,
    "extreme_percentile": 98.0,
    "extreme_rvol_median": 3.0,
    "shock_return_pct": 3.0,         # |close-to-close return| for a shock
    "obv_trend_slope": 0.1,          # |normalised 20-session OBV slope| for rising/falling
    "breakout_lookback": 30,         # prior-N closing high (momentum_breakout's default)
})

# Wording the explanation layer must never use: these features describe
# volume and price, they cannot identify who traded or what happens next.
FORBIDDEN_PHRASES = (
    "institution", "smart money", "big money", "operator", "insider",
    "will rise", "will fall", "will go", "guarantee", "accumulating", "distributing",
)

_BLOCKING_MESSAGES = {
    "NO_HISTORY": "No daily candles are stored for this instrument.",
    "INSUFFICIENT_HISTORY": f"Fewer than {BARS_REQUIRED} daily candles are stored; relative volume needs "
                            f"the current bar plus 20 prior sessions.",
    "HISTORY_NEEDS_REFRESH": "The stored price history disagrees with the provider (likely a split or bonus "
                             "back-adjustment) and must be rebuilt before volume features can be trusted.",
    "HISTORY_SUSPECT": "The stored price history has an unverified corporate-action-sized gap; rebuild it "
                       "from System Diagnostics before volume features can be trusted.",
    "UNVERIFIED_GAP_IN_WINDOW": "An open-vs-previous-close gap larger than the corporate-action threshold "
                                "lies inside the feature window and has not been verified by a rebuild.",
    "DUPLICATE_DATE": "The history contains the same date more than once.",
    "UNSORTED_DATES": "The history is not in ascending date order.",
    "MISSING_VALUE": "A candle inside the feature window has a missing (NaN/None) open, high, low, close "
                     "or volume.",
    "NEGATIVE_VOLUME": "A candle inside the feature window has negative volume.",
    "INVALID_CANDLE": "A candle inside the feature window is impossible (high below low, open/close "
                      "outside the high-low range, or a non-positive price).",
}
_DEGRADING_MESSAGES = {
    "PARTIAL_HISTORY": f"Fewer than {LOOKBACK_BARS} candles are available; the 60-session percentile and "
                       f"the longer rolling counts are not computed.",
    "ZERO_VOLUME_BAR": "The as-of bar has zero volume.",
    "ZERO_VOLUME_IN_WINDOW": "At least one prior bar in the feature window has zero volume.",
    "ZERO_BASELINE": "A prior-session volume baseline is zero, so the ratio against it is undefined.",
    "ZERO_RANGE_BAR": "The as-of bar has high equal to low, so close location and wick shares are undefined.",
    # Muhurat and disaster-recovery sessions trade ~4-13% of a normal day's
    # volume (measured on the stored history, PK-23). Only the bars the
    # 20-session baselines are built from are checked: flagging the whole
    # 61-bar window would mark ~59% of all history, which says nothing.
    "SPECIAL_SESSION_BAR": "The as-of bar is an exchange special session (Muhurat, budget-day or "
                           "disaster-recovery trading); its volume is not comparable with a regular day's.",
    "SPECIAL_SESSION_IN_BASELINE": "An exchange special session (Muhurat, budget-day or disaster-recovery "
                                   "trading) is among the 20 prior sessions; a short session's thin volume "
                                   "pulls the 20-session average down.",
}


def _is_special_session(day: str) -> bool:
    try:
        return trading_calendar.special_session(date.fromisoformat(day)) is not None
    except ValueError:
        return False


def resolve_thresholds(overrides: dict | None = None) -> dict:
    """DEFAULT_THRESHOLDS plus the configurable low-liquidity level, with any
    override applied. An unknown key is an error, not silently ignored."""
    values = {**DEFAULT_THRESHOLDS, "low_liquidity_inr": float(config.VOLUME_INTEL_LOW_LIQUIDITY_INR)}
    if overrides:
        unknown = set(overrides) - set(values)
        if unknown:
            raise ValueError(f"Unknown volume-intelligence threshold(s): {sorted(unknown)}")
        values.update(overrides)
    return values


# --------------------------------------------------------------------------
# Input normalisation and data quality
# --------------------------------------------------------------------------

_COLUMNS = ("open", "high", "low", "close", "volume")


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """A fresh frame with date as 'YYYY-MM-DD' text and numeric OHLCV (a value
    that is not a number becomes NaN, which validation then reports)."""
    if df is None or len(df) == 0:
        return pd.DataFrame({"date": pd.Series(dtype=str), **{c: pd.Series(dtype=float) for c in _COLUMNS}})
    out = pd.DataFrame({"date": df["date"].astype(str).str[:10].to_numpy()})
    for column in _COLUMNS:
        out[column] = pd.to_numeric(df[column], errors="coerce").astype(float).to_numpy()
    return out


def _gap_dates(frame: pd.DataFrame) -> list[dict]:
    limit = config.SUSPECT_CORPORATE_ACTION_GAP_PCT / 100
    prev_close = frame["close"].shift(1)
    ratio = frame["open"] / prev_close.where(prev_close > 0)
    mask = (ratio - 1).abs() > limit
    return [{"date": d, "gap_pct": round(float((r - 1) * 100), 2)}
            for d, r in zip(frame["date"][mask], ratio[mask])]


def validate(frame: pd.DataFrame, history_status: str | None = None, verified_gaps=()) -> dict:
    """Data quality of a normalised frame whose LAST row is the as-of bar.

    Blocking problems (UNAVAILABLE) are checked over the feature window only:
    a bad bar two years ago cannot affect a window-local feature, and must
    not make today's features unavailable either."""
    n = len(frame)
    blocking: list[str] = []
    degrading: list[str] = []
    detail: dict = {}

    if history_status == ohlc_integrity.NEEDS_REFRESH:
        blocking.append("HISTORY_NEEDS_REFRESH")
    elif history_status == ohlc_integrity.SUSPECT:
        blocking.append("HISTORY_SUSPECT")

    if n == 0:
        blocking.append("NO_HISTORY")
    else:
        if frame["date"].duplicated().any():
            blocking.append("DUPLICATE_DATE")
            detail["duplicate_dates"] = sorted(set(frame["date"][frame["date"].duplicated()]))[:5]
        elif not frame["date"].is_monotonic_increasing:
            blocking.append("UNSORTED_DATES")

        window = frame.tail(LOOKBACK_BARS)
        values = window[list(_COLUMNS)]
        if values.isna().any().any():
            blocking.append("MISSING_VALUE")
            detail["missing_value_dates"] = window["date"][values.isna().any(axis=1)].tolist()[:5]
        if (window["volume"] < 0).any():
            blocking.append("NEGATIVE_VOLUME")
            detail["negative_volume_dates"] = window["date"][window["volume"] < 0].tolist()[:5]
        bad = ((window["high"] < window["low"])
               | (window["open"] > window["high"]) | (window["open"] < window["low"])
               | (window["close"] > window["high"]) | (window["close"] < window["low"])
               | (window[["open", "high", "low", "close"]] <= 0).any(axis=1))
        if bad.any():
            blocking.append("INVALID_CANDLE")
            detail["invalid_candle_dates"] = window["date"][bad].tolist()[:5]
        # The gap INTO the window's first bar counts too, hence one extra row.
        verified = set(verified_gaps or ())
        gaps = [g for g in _gap_dates(frame.tail(LOOKBACK_BARS + 1)) if g["date"] not in verified]
        if gaps:
            blocking.append("UNVERIFIED_GAP_IN_WINDOW")
            detail["unverified_gaps"] = gaps[:5]
        if n < BARS_REQUIRED:
            blocking.append("INSUFFICIENT_HISTORY")

        if not blocking:
            if n < LOOKBACK_BARS:
                degrading.append("PARTIAL_HISTORY")
            last = window.iloc[-1]
            if last["volume"] == 0:
                degrading.append("ZERO_VOLUME_BAR")
            if (window["volume"].iloc[:-1] == 0).any():
                degrading.append("ZERO_VOLUME_IN_WINDOW")
            if last["high"] == last["low"]:
                degrading.append("ZERO_RANGE_BAR")
            if _is_special_session(window["date"].iloc[-1]):
                degrading.append("SPECIAL_SESSION_BAR")
            special = [d for d in window["date"].iloc[-21:-1] if _is_special_session(d)]
            if special:
                degrading.append("SPECIAL_SESSION_IN_BASELINE")
                detail["special_sessions"] = special

    status = UNAVAILABLE if blocking else (DEGRADED if degrading else OK)
    return {
        "status": status,
        "codes": blocking + degrading,
        "bars_available": n,
        "bars_used": min(n, LOOKBACK_BARS),
        "bars_required": BARS_REQUIRED,
        "bars_for_full_features": LOOKBACK_BARS,
        "detail": detail,
    }


# --------------------------------------------------------------------------
# The feature series (pure, vectorised, no lookahead)
# --------------------------------------------------------------------------

def _ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """numerator / denominator, NaN where the denominator is 0 or unknown."""
    return numerator / denominator.where(denominator > 0)


def _flag(condition: pd.Series, *inputs: pd.Series) -> pd.Series:
    """1.0 / 0.0, or NaN when any input the condition depends on is unknown —
    an unknowable flag must not be reported (or counted) as False."""
    values = condition.to_numpy(dtype=float)
    for x in inputs:
        values = np.where(np.isnan(x.to_numpy(dtype=float)), np.nan, values)
    return pd.Series(values, index=condition.index)


def _prior_percentile(volume: np.ndarray, window: int) -> np.ndarray:
    """Mid-rank percentile of each bar's volume within the `window` bars
    BEFORE it: 100 * (#prior < v + 0.5 * #prior == v) / window. The current
    bar is never part of its own reference distribution."""
    out = np.full(len(volume), np.nan)
    if len(volume) <= window:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(volume[:-1], window)  # windows[j] = v[j : j+window]
    current = volume[window:]                                                # the bar right after windows[j]
    less = (windows < current[:, None]).sum(axis=1)
    equal = (windows == current[:, None]).sum(axis=1)
    pct = 100.0 * (less + 0.5 * equal) / window
    pct[np.isnan(windows).any(axis=1) | np.isnan(current)] = np.nan
    out[window:] = pct
    return out


_SLOPE_X = np.arange(_SLOPE_WINDOW, dtype=float) - (_SLOPE_WINDOW - 1) / 2
_SLOPE_WEIGHTS = _SLOPE_X / float((_SLOPE_X ** 2).sum())


_PULLBACK_KEYS = ("in_pullback", "pullback_bars", "pullback_depth_pct", "pullback_volume_ratio",
                  "pullback_vs_advance_volume", "recovery_volume_ratio", "low_volume_pullback")


def _pullback_at(close: np.ndarray, volume: np.ndarray, i: int, low_ratio: float) -> dict:
    """Pullback context for bar i, using bars <= i only.

    Swing high = the highest close of the 20 sessions BEFORE i (the most
    recent one on a tie). The pullback is the bars after it up to i-1, and
    only exists if price actually came off the high (close[i-1] < the high's
    close). Bar i is treated as the potential recovery bar — the one
    trend_pullback's signal is measured on."""
    empty = dict.fromkeys(_PULLBACK_KEYS, np.nan)
    if i < _PULLBACK_LOOKBACK:
        return empty
    window = close[i - _PULLBACK_LOOKBACK:i]
    if np.isnan(window).any() or np.isnan(close[i]):
        return empty
    peak = i - _PULLBACK_LOOKBACK + int(np.flatnonzero(window == window.max())[-1])
    bars = i - peak - 1
    if bars == 0 or close[i - 1] >= close[peak]:
        return {**empty, "in_pullback": 0.0, "pullback_bars": 0.0}
    pullback_volume = float(volume[peak + 1:i].mean())
    depth = (float(close[peak + 1:i].min()) / float(close[peak]) - 1) * 100
    base = float(volume[peak - 19:peak + 1].mean()) if peak >= 19 else np.nan
    advance_bars = [j for j in range(max(peak - 9, 1), peak + 1) if close[j] > close[j - 1]]
    advance = float(volume[advance_bars].mean()) if advance_bars else np.nan
    ratio = pullback_volume / base if base and base > 0 else np.nan
    return {
        "in_pullback": 1.0,
        "pullback_bars": float(bars),
        "pullback_depth_pct": depth,
        "pullback_volume_ratio": ratio,
        "pullback_vs_advance_volume": pullback_volume / advance if advance and advance > 0 else np.nan,
        "recovery_volume_ratio": float(volume[i]) / pullback_volume if pullback_volume > 0 else np.nan,
        "low_volume_pullback": np.nan if np.isnan(ratio) else float(ratio <= low_ratio),
    }


def compute_series(df: pd.DataFrame, thresholds: dict | None = None) -> pd.DataFrame:
    """Every feature for every row of `df` (date-ordered daily candles).

    Row T of the result depends only on rows <= T. This is the historical
    (backtest/research) entry point; compute_at is the as-of entry point and
    is guaranteed to agree with it row for row (tested). Does not validate —
    call validate() (or use compute_at) before trusting a row."""
    thr = resolve_thresholds(thresholds)
    frame = _normalise(df)
    o, h, l, c, v = (frame[k] for k in _COLUMNS)
    prev_close = c.shift(1)
    prior = v.shift(1)  # baselines exclude the current bar

    # Columns are collected in a dict and framed once at the end (inserting
    # ~60 columns one by one into a DataFrame dominated the run time).
    out: dict[str, pd.Series] = {"date": frame["date"], "volume": v, "close": c, "prev_close": prev_close,
                                 "open": o, "high": h, "low": l}

    # B — relative volume
    avg_5 = prior.rolling(5).mean()
    avg_10 = prior.rolling(10).mean()
    avg_20 = prior.rolling(20).mean()
    median_20 = prior.rolling(20).median()
    std_20 = prior.rolling(20).std(ddof=1)
    out["avg_volume_20"] = avg_20
    out["median_volume_20"] = median_20
    out["rvol_5"] = _ratio(v, avg_5)
    out["rvol_10"] = _ratio(v, avg_10)
    out["rvol_20"] = _ratio(v, avg_20)
    out["rvol_median_20"] = _ratio(v, median_20)
    out["volume_percentile_60"] = pd.Series(_prior_percentile(v.to_numpy(), _PERCENTILE_WINDOW), index=frame.index)
    out["volume_zscore_20"] = (v - avg_20) / std_20.where(std_20 > 0)
    # The strategies' own rule: SMA-20 INCLUDING the signal bar (unchanged).
    out["legacy_volume_multiple_20"] = _ratio(v, indicators.avg_volume(v, 20))

    # C — traded value
    traded_value = c * v
    avg_tv_20 = traded_value.shift(1).rolling(20).mean()
    out["traded_value"] = traded_value
    out["avg_traded_value_20"] = avg_tv_20
    out["relative_traded_value_20"] = _ratio(traded_value, avg_tv_20)
    out["liquidity_warning"] = _flag(avg_tv_20 < thr["low_liquidity_inr"], avg_tv_20)

    # D — bar structure
    bar_range = (h - l).where(h > l)
    clv = (c - l) / bar_range
    upper = (h - np.maximum(o, c)) / bar_range
    lower = (np.minimum(o, c) - l) / bar_range
    true_range = np.maximum(h, prev_close) - np.minimum(l, prev_close)
    out["close_location"] = clv
    out["body_pct"] = (c - o).abs() / bar_range
    out["upper_wick_pct"] = upper
    out["lower_wick_pct"] = lower
    out["range_pct"] = (h - l) / prev_close.where(prev_close > 0) * 100
    out["true_range_pct"] = true_range / prev_close.where(prev_close > 0) * 100
    out["price_change_pct"] = (c / prev_close.where(prev_close > 0) - 1) * 100
    out["gap_pct"] = (o / prev_close.where(prev_close > 0) - 1) * 100

    rvol_20 = out["rvol_20"]
    elevated = rvol_20 >= thr["elevated_rvol"]
    strong = clv >= thr["strong_close"]          # NaN (zero range) compares False
    weak = clv <= thr["weak_close"]
    wick_reversal = (upper >= thr["upper_wick"]) & (clv <= thr["reversal_close_max"])
    out["high_volume_strong_close"] = _flag(elevated & strong, rvol_20)
    out["high_volume_weak_close"] = _flag(elevated & weak, rvol_20)
    out["high_volume_upper_wick"] = _flag(elevated & wick_reversal, rvol_20)

    # E — OBV (only start-invariant derivatives are exposed)
    direction = np.sign(c - prev_close)          # NaN on the first row: unknown, not flat
    signed = direction * v
    obv = signed.fillna(0.0).cumsum()
    out["obv_slope_5"] = _ratio(signed.rolling(5).sum(), v.rolling(5).sum())
    out["obv_slope_20"] = _ratio(signed.rolling(20).sum(), v.rolling(20).sum())
    prior_obv_high = obv.shift(1).rolling(20).max()
    prior_close_high = indicators.donchian_high_excl_today(c, 20)
    obv_new_high = obv > prior_obv_high
    price_new_high = c > prior_close_high
    out["obv_new_high_20"] = _flag(obv_new_high, prior_obv_high)
    out["price_new_high_20"] = _flag(price_new_high, prior_close_high)
    divergence = np.select(
        [obv_new_high & price_new_high, obv_new_high & ~price_new_high, ~obv_new_high & price_new_high],
        [1.0, 2.0, 3.0], 0.0,
    )
    out["obv_divergence_code"] = pd.Series(divergence, index=frame.index).where(
        prior_obv_high.notna() & prior_close_high.notna())

    # F — contraction / expansion
    out["contraction_ratio_5_20"] = _ratio(avg_5, avg_20)
    out["volume_slope_10"] = _ratio(
        prior.rolling(_SLOPE_WINDOW).apply(lambda w: float(_SLOPE_WEIGHTS @ w), raw=True), avg_10)
    out["pre_breakout_volume_contraction"] = _flag(
        out["contraction_ratio_5_20"] <= thr["contraction_ratio"], out["contraction_ratio_5_20"])
    out["current_volume_expansion"] = _flag(elevated, rvol_20)

    close_np, volume_np = c.to_numpy(), v.to_numpy()
    pullback = pd.DataFrame(
        [_pullback_at(close_np, volume_np, i, thr["low_volume_pullback_ratio"]) for i in range(len(frame))],
        index=frame.index, columns=_PULLBACK_KEYS, dtype=float,
    )
    out.update({key: pullback[key] for key in _PULLBACK_KEYS})

    # G — per-bar evidence (each bar judged against ITS OWN prior baseline)
    up = c > prev_close
    down = c < prev_close
    accumulation = _flag(elevated & up & strong, rvol_20, prev_close)
    distribution = _flag(elevated & down & weak, rvol_20, prev_close)
    up_volume = _flag(elevated & up, rvol_20, prev_close)
    down_volume = _flag(elevated & down, rvol_20, prev_close)
    reversal = _flag(elevated & wick_reversal, rvol_20)
    out["accumulation_like"] = accumulation
    out["distribution_like"] = distribution
    out["high_volume_reversal"] = reversal
    for n in (10, 20):
        out[f"accumulation_like_{n}"] = accumulation.rolling(n).sum()
        out[f"distribution_like_{n}"] = distribution.rolling(n).sum()
        out[f"net_accumulation_{n}"] = out[f"accumulation_like_{n}"] - out[f"distribution_like_{n}"]
    out["up_volume_days_20"] = up_volume.rolling(20).sum()
    out["down_volume_days_20"] = down_volume.rolling(20).sum()
    out["high_volume_reversals_20"] = reversal.rolling(20).sum()

    # H — breakout reference level (prior-N closing high, excluding the bar)
    out["breakout_level"] = indicators.donchian_high_excl_today(c, int(thr["breakout_lookback"]))

    # I — abnormal volume (percentile- and median-based: volume is skewed)
    pct = out["volume_percentile_60"]
    rvol_median = out["rvol_median_20"]
    change = out["price_change_pct"]
    abnormal = (pct >= thr["abnormal_percentile"]) & (rvol_median >= thr["abnormal_rvol_median"])
    out["abnormal_volume"] = _flag(abnormal, pct, rvol_median)
    out["extreme_volume"] = _flag(
        (pct >= thr["extreme_percentile"]) & (rvol_median >= thr["extreme_rvol_median"]), pct, rvol_median)
    out["negative_shock"] = _flag(abnormal & (change <= -thr["shock_return_pct"]) & weak, pct, rvol_median, change)
    out["positive_shock"] = _flag(abnormal & (change >= thr["shock_return_pct"]) & strong, pct, rvol_median, change)
    return pd.DataFrame(out, index=frame.index)


# --------------------------------------------------------------------------
# One as-of result (the stable VolumeFeatures object)
# --------------------------------------------------------------------------

_DIVERGENCE = {0.0: "none", 1.0: "confirmed", 2.0: "obv_leads_price", 3.0: "price_unconfirmed"}


def _num(value, digits: int = 4):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return round(value, digits)


def _bool(value):
    number = _num(value)
    return None if number is None else bool(number)


def _count(value):
    number = _num(value)
    return None if number is None else int(number)


def _obv_trend(slope, limit: float):
    if slope is None:
        return None
    if slope >= limit:
        return "rising"
    if slope <= -limit:
        return "falling"
    return "flat"


def _assemble(row: pd.Series, thr: dict) -> dict:
    close_location = _num(row["close_location"])
    in_pullback = _bool(row["in_pullback"])
    level = _num(row["breakout_level"], 2)
    close = float(row["close"])
    is_breakout = None if level is None else close > level
    return {
        # open/high/low were added in PK-24 (additive; no existing value
        # changed, FEATURE_VERSION unchanged): the score's failed-breakout
        # check needs the bar's high against the breakout level.
        "bar": {
            "volume": _num(row["volume"], 0), "close": _num(close, 2), "prev_close": _num(row["prev_close"], 2),
            "open": _num(row["open"], 2), "high": _num(row["high"], 2), "low": _num(row["low"], 2),
        },
        "relative_volume": {
            "avg_volume_20": _num(row["avg_volume_20"], 2),
            "median_volume_20": _num(row["median_volume_20"], 2),
            "rvol_5": _num(row["rvol_5"]),
            "rvol_10": _num(row["rvol_10"]),
            "rvol_20": _num(row["rvol_20"]),
            "rvol_median_20": _num(row["rvol_median_20"]),
            "volume_percentile_60": _num(row["volume_percentile_60"], 2),
            "volume_zscore_20": _num(row["volume_zscore_20"]),
        },
        "liquidity": {
            "traded_value": _num(row["traded_value"], 2),
            "avg_traded_value_20": _num(row["avg_traded_value_20"], 2),
            "relative_traded_value_20": _num(row["relative_traded_value_20"]),
            "low_liquidity_threshold": thr["low_liquidity_inr"],
            "liquidity_warning": _bool(row["liquidity_warning"]),
        },
        "structure": {
            "close_location": close_location,
            "body_pct": _num(row["body_pct"]),
            "upper_wick_pct": _num(row["upper_wick_pct"]),
            "lower_wick_pct": _num(row["lower_wick_pct"]),
            "range_pct": _num(row["range_pct"]),
            "true_range_pct": _num(row["true_range_pct"]),
            "price_change_pct": _num(row["price_change_pct"]),
            "gap_pct": _num(row["gap_pct"]),
            "high_volume_strong_close": _bool(row["high_volume_strong_close"]),
            "high_volume_weak_close": _bool(row["high_volume_weak_close"]),
            "high_volume_upper_wick": _bool(row["high_volume_upper_wick"]),
        },
        "obv": {
            "slope_5": _num(row["obv_slope_5"]),
            "slope_20": _num(row["obv_slope_20"]),
            "trend": _obv_trend(_num(row["obv_slope_20"]), thr["obv_trend_slope"]),
            "obv_new_high_20": _bool(row["obv_new_high_20"]),
            "price_new_high_20": _bool(row["price_new_high_20"]),
            "divergence": _DIVERGENCE.get(_num(row["obv_divergence_code"], 0)),
        },
        "contraction": {
            "contraction_ratio_5_20": _num(row["contraction_ratio_5_20"]),
            "volume_slope_10": _num(row["volume_slope_10"]),
            "pre_breakout_volume_contraction": _bool(row["pre_breakout_volume_contraction"]),
            "current_volume_expansion": _bool(row["current_volume_expansion"]),
        },
        "pullback": {
            "in_pullback": in_pullback,
            "pullback_bars": _count(row["pullback_bars"]),
            "pullback_depth_pct": _num(row["pullback_depth_pct"]),
            "pullback_volume_ratio": _num(row["pullback_volume_ratio"]),
            "pullback_vs_advance_volume": _num(row["pullback_vs_advance_volume"]),
            "recovery_volume_ratio": _num(row["recovery_volume_ratio"]),
            "is_recovery_bar": None if row["prev_close"] != row["prev_close"] else bool(close > row["prev_close"]),
            "low_volume_pullback": _bool(row["low_volume_pullback"]),
        },
        "evidence": {
            "today": {
                "accumulation_like": _bool(row["accumulation_like"]),
                "distribution_like": _bool(row["distribution_like"]),
                "high_volume_reversal": _bool(row["high_volume_reversal"]),
            },
            "accumulation_like_10": _count(row["accumulation_like_10"]),
            "distribution_like_10": _count(row["distribution_like_10"]),
            "net_accumulation_10": _count(row["net_accumulation_10"]),
            "accumulation_like_20": _count(row["accumulation_like_20"]),
            "distribution_like_20": _count(row["distribution_like_20"]),
            "net_accumulation_20": _count(row["net_accumulation_20"]),
            "up_volume_days_20": _count(row["up_volume_days_20"]),
            "down_volume_days_20": _count(row["down_volume_days_20"]),
            "high_volume_reversals_20": _count(row["high_volume_reversals_20"]),
        },
        "breakout": {
            "lookback": int(thr["breakout_lookback"]),
            "level": level,
            "is_breakout": is_breakout,
            "close_above_level_pct": _num((close / level - 1) * 100) if is_breakout else None,
            "close_quality": close_location if is_breakout else None,
            "volume_ratio": _num(row["rvol_20"]) if is_breakout else None,
            "volume_percentile_60": _num(row["volume_percentile_60"], 2) if is_breakout else None,
            "traded_value_ratio": _num(row["relative_traded_value_20"]) if is_breakout else None,
            "base_contraction_ratio": _num(row["contraction_ratio_5_20"]) if is_breakout else None,
        },
        "anomalies": {
            "abnormal_volume": _bool(row["abnormal_volume"]),
            "extreme_volume": _bool(row["extreme_volume"]),
            "negative_shock": _bool(row["negative_shock"]),
            "positive_shock": _bool(row["positive_shock"]),
        },
        "legacy_volume_multiple_20": _num(row["legacy_volume_multiple_20"]),
    }


_FEATURE_GROUPS = ("bar", "relative_volume", "liquidity", "structure", "obv", "contraction", "pullback",
                   "evidence", "breakout", "anomalies")


def compute_at(df: pd.DataFrame, as_of: str | None = None, *, symbol: str | None = None,
               instrument_token: int | None = None, history_status: str | None = None, verified_gaps=(),
               thresholds: dict | None = None, strategy: str | None = None, params: dict | None = None) -> dict:
    """The VolumeFeatures object for the last bar on or before `as_of`
    (the last bar when None). Rows after `as_of` are dropped BEFORE anything
    is computed, so they cannot influence the result."""
    thr = resolve_thresholds(thresholds)
    frame = _normalise(df)
    requested = str(as_of)[:10] if as_of else None
    if requested:
        frame = frame[frame["date"] <= requested].reset_index(drop=True)
    quality = validate(frame, history_status, verified_gaps)
    as_of_date = frame["date"].iloc[-1] if len(frame) else None

    result = {
        "feature_version": FEATURE_VERSION,
        "symbol": symbol,
        "instrument_token": instrument_token,
        "as_of_date": as_of_date,
        "requested_as_of": requested,
        "history_status": history_status,
        "data_quality": quality,
        "warnings": [],
        "notes": [],
        **{group: None for group in _FEATURE_GROUPS},
        "legacy_volume_multiple_20": None,
        "thresholds": thr,
        "interpretation": [],
    }
    if requested and as_of_date and as_of_date != requested:
        result["notes"].append(f"No candle on {requested}; features are as of the previous session, {as_of_date}.")

    if quality["status"] != UNAVAILABLE:
        window = frame.tail(LOOKBACK_BARS).reset_index(drop=True)
        series = compute_series(window, thr)
        result.update(_assemble(series.iloc[-1], thr))
        rv = result["relative_volume"]
        if (rv["avg_volume_20"] == 0 or rv["median_volume_20"] == 0) and "ZERO_BASELINE" not in quality["codes"]:
            quality["codes"].append("ZERO_BASELINE")
            quality["status"] = DEGRADED

    messages = {**_BLOCKING_MESSAGES, **_DEGRADING_MESSAGES}
    result["warnings"] = [messages[code] for code in quality["codes"]]
    if strategy:
        result["strategy_comparison"] = strategy_volume_comparison(result, strategy, params or {})
    result["interpretation"] = interpret(result)
    return result


# --------------------------------------------------------------------------
# Existing strategy rule, side by side (diagnostic only — never a filter)
# --------------------------------------------------------------------------

def strategy_volume_comparison(features: dict, strategy_name: str, params: dict) -> dict:
    """The strategy's own volume rule next to the prior-only measures.

    momentum_breakout and trend_pullback both require
    volume > volume_multiple x SMA-20(volume) where the SMA includes the
    signal bar. Algebraically that is volume > 19m/(20-m) x the mean of the
    19 prior sessions (3.35x for m = 3). rsi_mean_reversion has no volume
    condition. Nothing here changes what any strategy does."""
    multiple = params.get("volume_multiple")
    rv = features.get("relative_volume") or {}
    base = {
        "strategy": strategy_name,
        "rvol_20_prior_only": rv.get("rvol_20"),
        "volume_percentile_60": rv.get("volume_percentile_60"),
        "close_location": (features.get("structure") or {}).get("close_location"),
    }
    if multiple is None:
        return {**base, "has_volume_rule": False,
                "rule": "This strategy has no volume condition; volume is shown for context only."}
    multiple = float(multiple)
    legacy = features.get("legacy_volume_multiple_20")
    return {
        **base,
        "has_volume_rule": True,
        "rule": f"volume > {multiple:g}x the 20-day average volume, where the average includes the signal bar",
        "required_multiple": multiple,
        "legacy_multiple": legacy,
        "legacy_pass": None if legacy is None else legacy > multiple,
        "effective_multiple_of_prior_19_mean": round(19 * multiple / (20 - multiple), 4) if multiple < 20 else None,
    }


# --------------------------------------------------------------------------
# Explanation layer (factual sentences only)
# --------------------------------------------------------------------------

def _x(value) -> str:
    return f"{value:.2f}×"


def _crore(value) -> str:
    return f"₹{value / 1e7:,.2f} crore"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def interpret(features: dict) -> list[str]:
    """Short, factual sentences about the features. Never a forecast, never a
    claim about who traded (FORBIDDEN_PHRASES is enforced by tests)."""
    quality = features.get("data_quality") or {}
    if quality.get("status") == UNAVAILABLE:
        return [f"Volume features are unavailable: {features['warnings'][0]}"] if features.get("warnings") else [
            "Volume features are unavailable."]
    lines: list[str] = []
    rv, liq = features["relative_volume"], features["liquidity"]
    st, obv = features["structure"], features["obv"]
    con, pb, ev = features["contraction"], features["pullback"], features["evidence"]
    bo, an = features["breakout"], features["anomalies"]

    if rv["rvol_20"] is not None:
        median = f" (median-based {_x(rv['rvol_median_20'])})" if rv["rvol_median_20"] is not None else ""
        lines.append(f"RVOL20: {_x(rv['rvol_20'])} the average of the prior 20 sessions{median}.")
    if rv["volume_percentile_60"] is not None:
        lines.append(f"Volume is in the {_ordinal(int(round(rv['volume_percentile_60'])))} percentile "
                     f"of the prior 60 sessions.")
    if liq["traded_value"] is not None and liq["avg_traded_value_20"] is not None:
        lines.append(f"Traded value {_crore(liq['traded_value'])} against a 20-session average of "
                     f"{_crore(liq['avg_traded_value_20'])}.")
    if liq["liquidity_warning"]:
        lines.append(f"The 20-session average traded value is below the {_crore(liq['low_liquidity_threshold'])} "
                     f"low-liquidity analytics threshold.")
    if st["close_location"] is not None:
        lines.append(f"Close location {st['close_location']:.2f} within the day's range "
                     f"(1.00 = closed at the high, 0.00 = at the low).")
    if st["high_volume_upper_wick"]:
        lines.append(f"Elevated volume with a long upper wick ({st['upper_wick_pct']:.0%} of the range) "
                     f"and a close in the lower half of the bar.")
    if obv["trend"] is not None:
        lines.append(f"OBV over 20 sessions is {obv['trend']} (net {obv['slope_20']:+.2f} of the period's "
                     f"volume on up-closes minus down-closes).")
    if obv["divergence"] == "obv_leads_price":
        lines.append("OBV made a 20-session high while the close did not.")
    elif obv["divergence"] == "price_unconfirmed":
        lines.append("The close made a 20-session high while OBV did not.")
    if ev["accumulation_like_10"] is not None:
        lines.append(f"{ev['accumulation_like_10']} accumulation-like vs {ev['distribution_like_10']} "
                     f"distribution-like sessions in the last 10.")
    if con["contraction_ratio_5_20"] is not None:
        state = "lower than" if con["contraction_ratio_5_20"] < 1 else "at or above"
        lines.append(f"Average volume of the prior 5 sessions is {_x(con['contraction_ratio_5_20'])} the prior-20 "
                     f"average ({state} the longer baseline).")
    if pb["in_pullback"] and pb["pullback_volume_ratio"] is not None:
        lines.append(f"Price came {abs(pb['pullback_depth_pct']):.1f}% off its 20-session closing high over "
                     f"{pb['pullback_bars']} session(s), on {_x(pb['pullback_volume_ratio'])} the average volume "
                     f"of the 20 sessions into that high.")
    if bo["is_breakout"]:
        lines.append(f"Close is {bo['close_above_level_pct']:.2f}% above the prior {bo['lookback']}-session "
                     f"closing high.")
    if an["negative_shock"]:
        lines.append("Abnormal volume on a large negative return with a weak close.")
    elif an["positive_shock"]:
        lines.append("Abnormal volume on a large positive return with a strong close.")
    elif an["extreme_volume"]:
        lines.append("Volume is extreme relative to the prior 60 sessions.")
    elif an["abnormal_volume"]:
        lines.append("Volume is abnormal relative to the prior 60 sessions.")
    comparison = features.get("strategy_comparison")
    if comparison and comparison.get("has_volume_rule") and comparison.get("legacy_multiple") is not None:
        lines.append(f"Strategy rule: {_x(comparison['legacy_multiple'])} its 20-day average including the "
                     f"signal bar (requires more than {_x(comparison['required_multiple'])}).")
    lines.append("These are descriptive measurements of volume and price, not a forecast.")
    return lines


# --------------------------------------------------------------------------
# DB-reading wrapper (reads only; the core above stays pure)
# --------------------------------------------------------------------------

_CACHE_SIZE = 256
_cache: "OrderedDict[tuple, dict]" = OrderedDict()
_cache_lock = Lock()
cache_stats = {"hits": 0, "misses": 0}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
        cache_stats.update(hits=0, misses=0)


def _history_digest(df: pd.DataFrame) -> str:
    """Content hash of exactly the rows the features are computed from.
    Keying the cache on it means a refresh, a PK-22 rebuild or any edited bar
    produces a different key — stale features cannot survive a history
    change, whatever the ohlc_history_status bookkeeping says."""
    if len(df) == 0:
        return "empty"
    data = pd.util.hash_pandas_object(df[["date", *_COLUMNS]], index=False).to_numpy().tobytes()
    return hashlib.sha256(data).hexdigest()


def _freshness(as_of_date: str | None, requested: str | None) -> dict:
    last_session = trading_calendar.latest_completed_session()
    out = {"last_bar_date": as_of_date, "latest_completed_session": last_session.isoformat(),
           "max_staleness_days": config.MAX_DATA_STALENESS_DAYS, "age_days": None, "is_stale": False}
    if as_of_date:
        # Same rule the live scanner applies (strategy_engine._is_stale).
        age = (last_session - date.fromisoformat(as_of_date)).days
        out["age_days"] = age
        out["is_stale"] = requested is None and age > config.MAX_DATA_STALENESS_DAYS
    return out


def features_for_instrument(conn: sqlite3.Connection, instrument_token: int, symbol: str,
                            as_of: str | None = None, strategy: str | None = None,
                            params: dict | None = None) -> dict:
    """compute_at over the stored history of one instrument, with its PK-22
    history status applied. Read-only: never writes, never calls the broker."""
    status_row = ohlc_integrity.get_status(conn, instrument_token)
    history_status = status_row["status"] if status_row else None
    verified = json.loads(status_row["verified_gaps"]) if status_row and status_row["verified_gaps"] else []

    df = indicators.load_ohlc_frame(instrument_token, end_date=as_of)
    frame = _normalise(df)
    key = (instrument_token, str(as_of)[:10] if as_of else None, FEATURE_VERSION, history_status,
           tuple(sorted(verified)), strategy, json.dumps(params or {}, sort_keys=True, default=str),
           _history_digest(frame.tail(LOOKBACK_BARS + 1)), symbol, config.VOLUME_INTEL_LOW_LIQUIDITY_INR)
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            cache_stats["hits"] += 1
    if cached is None:
        cached = compute_at(frame, as_of, symbol=symbol, instrument_token=instrument_token,
                            history_status=history_status, verified_gaps=verified,
                            strategy=strategy, params=params)
        with _cache_lock:
            cache_stats["misses"] += 1
            _cache[key] = cached
            while len(_cache) > _CACHE_SIZE:
                _cache.popitem(last=False)
    result = json.loads(json.dumps(cached))  # callers get their own copy
    result["freshness"] = _freshness(result["as_of_date"], result["requested_as_of"])
    if result["freshness"]["is_stale"]:
        result["warnings"].append(
            f"The newest stored candle is {result['as_of_date']}, {result['freshness']['age_days']} days behind "
            f"the last completed session — beyond the {config.MAX_DATA_STALENESS_DAYS}-day limit.")
    return result
