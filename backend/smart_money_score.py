"""Smart Money Accumulation Score (PK-24): one transparent, bounded number for
"how strongly does the recent price/volume structure resemble
accumulation-like behaviour?", built from the PK-23 volume features.

What the score is NOT: evidence of who traded. It cannot see institutional,
mutual-fund, FII/DII, promoter or insider activity, and it is not a forecast.
It is a heuristic PRIOR: its weights and ramps were set by hand from the
shape of the PK-23 feature distributions (docs/OPUS55_PK24_IMPLEMENTATION_REPORT.md),
never fitted to forward returns. Whether it carries any predictive value is
PK-25's question. Nothing in the strategies, scanner, ranking, approval,
risk, sizing, execution or backtest trade selection reads it (tested).

Design rules
------------
* One entry point, `score(features)`. It consumes the PK-23 `VolumeFeatures`
  object exactly as volume_intelligence.compute_at / features_for_instrument
  return it — no OHLCV is re-read or recomputed here, so the score inherits
  PK-23's no-lookahead, window-local and data-quality guarantees. The API,
  Signal Detail and research export all call this one function.
* Pure and light: stdlib only (no pandas/numpy/scipy), no DB, no broker.
* Neutral evidence scores 50: every component is 0 on a flat, average bar.
  Positive components add up to POSITIVE_MAX (= 50) points, penalties
  subtract up to their own maxima, and the result is clamped to 0-100.
* Smooth. Every continuous input enters through `ramp` (piecewise-linear,
  saturating), so a small input change cannot cause a large score jump.
  Volume saturates: a 20x spike counts no more than a ~3x one.
* Decomposable. Each component reports its points, maximum, inputs and a
  factual sentence, so the number can always be explained.
* Confidence is reported separately from the score, and a result that cannot
  be computed is `score: None, confidence: INSUFFICIENT_DATA` — never 50.
"""

import math
from types import MappingProxyType

SMART_MONEY_SCORE_VERSION = "1.0"
REQUIRED_FEATURE_VERSION = 1   # the PK-23 feature definition this version reads

NAME = "Smart Money Accumulation Score"
DISCLAIMER = "Price-volume evidence, not proof of institutional buying."

HIGH, MEDIUM, LOW, INSUFFICIENT_DATA = "HIGH", "MEDIUM", "LOW", "INSUFFICIENT_DATA"
_LEVELS = (HIGH, MEDIUM, LOW)

POSITIVE, PENALTY = "positive", "penalty"

# The ONLY place weights live. (name, kind, max points, short label).
# Positive maxima sum to 50 and penalty maxima sum to 50, so neutral (50) plus
# every positive saturated is 100 and minus every penalty saturated is 0. The
# weak-close and wick penalties are a point heavier than their positive
# mirrors: high volume must never be enough on its own.
#
# The single-bar trio (participation, close_quality, price_response) shares
# one participation input and correlated at Pearson 0.73-0.80 on the stored
# history, so together it is held to 14 points; the multi-session evidence
# (accumulation frequency, OBV, pullback) carries more, so repeated
# constructive sessions outweigh one isolated spike. See the report's
# redundancy section; no weight was chosen from forward returns.
COMPONENTS = (
    ("participation", POSITIVE, 5.0, "Relative volume"),
    ("close_quality", POSITIVE, 6.0, "Close quality"),
    ("price_response", POSITIVE, 3.0, "Price response"),
    ("accumulation_frequency", POSITIVE, 13.0, "Accumulation-like sessions"),
    ("obv_confirmation", POSITIVE, 8.0, "OBV"),
    ("contraction_expansion", POSITIVE, 5.0, "Contraction, then expansion"),
    ("pullback_participation", POSITIVE, 6.0, "Low-volume pullback, then recovery"),
    ("breakout_participation", POSITIVE, 4.0, "Breakout participation"),
    ("distribution_frequency", PENALTY, 13.0, "Distribution-like sessions"),
    ("high_volume_weak_close", PENALTY, 8.0, "Weak close"),
    ("upper_wick_rejection", PENALTY, 7.0, "Upper-wick rejection"),
    ("failed_breakout", PENALTY, 6.0, "Failed breakout attempt"),
    ("obv_non_confirmation", PENALTY, 8.0, "OBV non-confirmation"),
    ("exhaustion_shock", PENALTY, 8.0, "Exhaustion-style bar"),
)
WEIGHTS = MappingProxyType({name: points for name, _, points, _ in COMPONENTS})
_KIND = {name: kind for name, kind, _, _ in COMPONENTS}
_LABEL = {name: label for name, _, _, label in COMPONENTS}
POSITIVE_MAX = sum(p for _, k, p, _ in COMPONENTS if k == POSITIVE)
PENALTY_MAX = sum(p for _, k, p, _ in COMPONENTS if k == PENALTY)
NEUTRAL = 50.0

# Ramp anchors, set from the PK-23 full-history distributions (40,843 bars,
# 62 NSE series): RVOL20 median 0.84 / P90 1.73 / P95 2.26, median-based
# RVOL P95 2.79, contraction ratio median 0.95 / P10 0.62, OBV slope-20
# P10 -0.31 / P90 0.41, range % P90 4.0 / P99 7.4. Documented in the report.
RAMPS = MappingProxyType({
    "rvol_20": (1.0, 2.5),               # participation: average -> ~P95
    "rvol_median_20": (1.0, 3.0),
    "volume_percentile_60": (50.0, 95.0),
    "non_weak_close": (0.35, 0.65),      # close location gating volume credit
    "strong_close": (0.5, 0.9),
    "weak_close": (0.5, 0.9),            # applied to (1 - close location)
    "price_up": (0.0, 2.5),              # % close-to-close
    "price_overextended": (6.0, 12.0),   # % where the price credit halves
    "evidence_count": (0.0, 3.0),        # effective accumulation-/distribution-like days
    "obv_slope": (0.0, 0.4),
    "contraction": (0.1, 0.4),           # applied to (1 - prior-5 / prior-20)
    "pullback_quiet": (0.1, 0.5),        # applied to (1 - pullback volume ratio)
    "recovery_volume": (1.0, 2.0),
    "recovery_up": (0.0, 1.0),           # % up on the recovery bar
    "upper_wick": (0.3, 0.6),
    "wick_close": (0.0, 0.3),            # applied to (0.7 - close location)
    "reversal_count": (0.0, 3.0),
    "breakout_pierce": (0.0, 1.0),       # % the high went above the breakout level
    "breakout_fail": (0.0, 1.0),         # % the close finished below it
    "exhaustion_rvol_median": (3.0, 6.0),
    "exhaustion_range": (4.0, 8.0),      # bar range, % of previous close
    "exhaustion_weak_close": (0.4, 0.8), # applied to (1 - close location)
    "liquidity_thin": (0.0, 1.0),        # log10(threshold / avg traded value)
})
LIQUIDITY_MAX_DAMPING = 0.3      # thin names are pulled up to 30% toward 50
VERY_THIN_TRADED_VALUE_INR = 1_00_00_000   # Rs 1 crore/day: confidence LOW
TOP_N = 3
TOP_MIN_POINTS = 0.5

# Symmetric about the neutral 50 (+-10 mixed, +-25 strong/weak), not tuned to
# outcomes. On the stored history ~2% of bars reach "strong".
BANDS = (   # (lower bound inclusive, key, label) — descriptive, never an instruction
    (75.0, "strong", "Strong accumulation-like evidence"),
    (60.0, "moderate", "Moderate accumulation-like evidence"),
    (40.0, "mixed", "Mixed / neutral evidence"),
    (25.0, "limited", "Limited accumulation-like evidence"),
    (0.0, "weak", "Weak accumulation-like evidence; distribution-like evidence dominates"),
)

# The score's own wording rule, on top of PK-23's list: no attribution of the
# trading, no instruction, no claim of proof. Only NAME and DISCLAIMER are
# exempt (the disclaimer names what the score is NOT).
_EXTRA_FORBIDDEN = ("fii", "dii", "buy", "sell", "recommend", "proof", "confirmed accumulation")


def forbidden_phrases() -> tuple:
    from backend.volume_intelligence import FORBIDDEN_PHRASES
    return tuple(FORBIDDEN_PHRASES) + _EXTRA_FORBIDDEN


# --------------------------------------------------------------------------
# Bounded transforms
# --------------------------------------------------------------------------

def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def ramp(x: float, x0: float, x1: float) -> float:
    """0 at or below x0, 1 at or above x1, linear between — continuous and
    saturating, so no input can push a component past its maximum."""
    return clamp((x - x0) / (x1 - x0))


def _r(x, key):
    return ramp(x, *RAMPS[key])


def _num(value):
    if value is None or isinstance(value, bool):
        return None if value is None else float(value)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) or math.isinf(value) else value


def _pct_top(close_location: float) -> str:
    return f"top {max(1, round((1 - close_location) * 100))}%"


# --------------------------------------------------------------------------
# Components. Each returns (fraction 0..1 or None when its inputs are
# unknown, inputs dict, sentence). Points = max points x fraction, signed by
# kind. A None fraction contributes 0 and lowers coverage (and confidence).
# --------------------------------------------------------------------------

def _participation(f: dict):
    """Shared participation intensity p in [0, 1]: RVOL20, median-based
    RVOL20 and the 60-session percentile, averaged. They are one phenomenon
    (Spearman 0.86-0.95 on the stored history), so they enter the score ONCE,
    here; relative traded value (Spearman 0.997 with RVOL20) is left out."""
    rv = f.get("relative_volume") or {}
    rvol, med, pct = _num(rv.get("rvol_20")), _num(rv.get("rvol_median_20")), _num(rv.get("volume_percentile_60"))
    parts = []
    if rvol is not None:
        parts.append(_r(rvol, "rvol_20"))
    if med is not None:
        parts.append(_r(med, "rvol_median_20"))
    if pct is not None:
        parts.append(_r(pct, "volume_percentile_60"))
    if rvol is None or not parts:
        return None, {"rvol_20": rvol, "rvol_median_20": med, "volume_percentile_60": pct}
    return sum(parts) / len(parts), {"rvol_20": rvol, "rvol_median_20": med, "volume_percentile_60": pct}


def _volume_words(inp: dict) -> str:
    text = f"RVOL20 {inp['rvol_20']:.2f}×"
    if inp.get("rvol_median_20") is not None:
        text += f" (median-based {inp['rvol_median_20']:.2f}×"
        if inp.get("volume_percentile_60") is not None:
            text += f", {inp['volume_percentile_60']:.0f}th percentile of 60 sessions"
        text += ")"
    return text


def _components(f: dict) -> dict:
    rv = f.get("relative_volume") or {}
    st = f.get("structure") or {}
    obv = f.get("obv") or {}
    con = f.get("contraction") or {}
    pb = f.get("pullback") or {}
    ev = f.get("evidence") or {}
    bo = f.get("breakout") or {}
    bar = f.get("bar") or {}

    p, p_inputs = _participation(f)
    clv = _num(st.get("close_location"))
    ret = _num(st.get("price_change_pct"))
    upper = _num(st.get("upper_wick_pct"))
    range_pct = _num(st.get("range_pct"))
    out = {}

    # --- positive evidence -------------------------------------------------
    if p is None or clv is None:
        out["participation"] = (None, {**p_inputs, "close_location": clv}, None)
    else:
        frac = p * _r(clv, "non_weak_close")
        out["participation"] = (frac, {**p_inputs, "participation": round(p, 4), "close_location": clv},
                                f"Participation: {_volume_words(p_inputs)}, with a close that is not weak.")

    if clv is None:
        out["close_quality"] = (None, {"close_location": clv}, None)
    else:
        weight = 0.25 + 0.75 * (p or 0.0)
        out["close_quality"] = (_r(clv, "strong_close") * weight, {"close_location": clv, "volume_weight": weight},
                                f"Close in the {_pct_top(clv)} of the day's range"
                                + (" on elevated volume." if (p or 0) >= 0.5 else "."))

    if ret is None:
        out["price_response"] = (None, {"price_change_pct": ret}, None)
    else:
        shape = _r(ret, "price_up") * (1 - 0.5 * _r(ret, "price_overextended"))
        weight = 0.3 + 0.7 * (p or 0.0)
        out["price_response"] = (shape * weight, {"price_change_pct": ret, "volume_weight": weight},
                                 f"Close {ret:+.2f}% versus the previous close"
                                 + (" (a very large one-day move earns less credit)." if ret > RAMPS["price_overextended"][0]
                                    else "."))

    def _count_words(kind, n10, n20):
        if n10 == 0 and n20:
            return f"{int(n20)} {kind} session(s) in the last 20, none in the last 10."
        span = f" ({int(n20)} in the last 20)" if n20 is not None else ""
        return f"{int(n10)} {kind} session(s) in the last 10{span}."

    def _effective(n10, n20):
        n10, n20 = _num(n10), _num(n20)
        if n10 is None:
            return None, n10, n20
        return n10 + (0.5 * (n20 - n10) if n20 is not None else 0.0), n10, n20

    acc, acc10, acc20 = _effective(ev.get("accumulation_like_10"), ev.get("accumulation_like_20"))
    if acc is None:
        out["accumulation_frequency"] = (None, {"accumulation_like_10": acc10}, None)
    else:
        out["accumulation_frequency"] = (_r(acc, "evidence_count"),
                                         {"accumulation_like_10": acc10, "accumulation_like_20": acc20,
                                          "effective": acc},
                                         _count_words("accumulation-like", acc10, acc20))

    slope = _num(obv.get("slope_20"))
    if slope is None:
        out["obv_confirmation"] = (None, {"obv_slope_20": slope}, None)
    else:
        new_high = bool(obv.get("obv_new_high_20"))
        frac = 0.7 * _r(slope, "obv_slope") + 0.3 * float(new_high)
        words = f"OBV slope over 20 sessions {slope:+.2f}"
        if new_high:
            words += "; OBV at a 20-session high" + (" while price is not" if obv.get("divergence") == "obv_leads_price"
                                                     else "")
        out["obv_confirmation"] = (frac, {"obv_slope_20": slope, "obv_new_high_20": new_high,
                                          "divergence": obv.get("divergence")}, words + ".")

    ratio = _num(con.get("contraction_ratio_5_20"))
    if ratio is None or p is None or clv is None:
        out["contraction_expansion"] = (None, {"contraction_ratio_5_20": ratio}, None)
    else:
        frac = ramp(1 - ratio, *RAMPS["contraction"]) * p * _r(clv, "non_weak_close")
        out["contraction_expansion"] = (frac, {"contraction_ratio_5_20": ratio, "participation": round(p, 4)},
                                        f"Prior-5 volume {ratio:.2f}× the prior-20 average (a quieter base), "
                                        f"then {_volume_words(p_inputs)} on this bar.")

    in_pullback = pb.get("in_pullback")
    pvr, rec = _num(pb.get("pullback_volume_ratio")), _num(pb.get("recovery_volume_ratio"))
    if in_pullback is None or ret is None or clv is None:
        out["pullback_participation"] = (None, {"in_pullback": in_pullback}, None)
    elif not in_pullback or pvr is None or rec is None:
        out["pullback_participation"] = (0.0, {"in_pullback": bool(in_pullback), "pullback_volume_ratio": pvr,
                                               "recovery_volume_ratio": rec}, None)
    else:
        # The recovery must be an up bar that also HELD (not a weak close
        # under a long wick) — the PK-24 sanity run found an exhaustion bar
        # earning full recovery credit without this gate.
        frac = (ramp(1 - pvr, *RAMPS["pullback_quiet"]) * _r(rec, "recovery_volume") * _r(ret, "recovery_up")
                * _r(clv, "non_weak_close"))
        out["pullback_participation"] = (frac, {"in_pullback": True, "pullback_volume_ratio": pvr,
                                                "recovery_volume_ratio": rec, "price_change_pct": ret,
                                                "close_location": clv},
                                         f"Pullback volume {pvr:.2f}× the volume into the high, then this up bar "
                                         f"at {rec:.2f}× the pullback's volume.")

    is_breakout = bo.get("is_breakout")
    if is_breakout is None or p is None:
        out["breakout_participation"] = (None, {"is_breakout": is_breakout}, None)
    elif not is_breakout or _num(bo.get("close_quality")) is None:
        out["breakout_participation"] = (0.0, {"is_breakout": bool(is_breakout)}, None)
    else:
        quality = _num(bo.get("close_quality"))
        frac = _r(quality, "strong_close") * p
        out["breakout_participation"] = (frac, {"is_breakout": True, "close_quality": quality,
                                                "participation": round(p, 4)},
                                         f"Close {bo.get('close_above_level_pct') or 0:.2f}% above the prior "
                                         f"{bo.get('lookback')}-session closing high, {_volume_words(p_inputs)}.")

    # --- negative evidence -------------------------------------------------
    dist, dist10, dist20 = _effective(ev.get("distribution_like_10"), ev.get("distribution_like_20"))
    if dist is None:
        out["distribution_frequency"] = (None, {"distribution_like_10": dist10}, None)
    else:
        out["distribution_frequency"] = (_r(dist, "evidence_count"),
                                         {"distribution_like_10": dist10, "distribution_like_20": dist20,
                                          "effective": dist},
                                         _count_words("distribution-like", dist10, dist20))

    if clv is None:
        out["high_volume_weak_close"] = (None, {"close_location": clv}, None)
    else:
        weight = 0.25 + 0.75 * (p or 0.0)
        out["high_volume_weak_close"] = (_r(1 - clv, "weak_close") * weight,
                                         {"close_location": clv, "volume_weight": weight},
                                         f"Close in the bottom {max(1, round(clv * 100))}% of the day's range"
                                         + (" on elevated volume." if (p or 0) >= 0.5 else "."))

    reversals = _num(ev.get("high_volume_reversals_20"))
    if upper is None or clv is None or p is None:
        out["upper_wick_rejection"] = (None, {"upper_wick_pct": upper}, None)
    else:
        today = _r(upper, "upper_wick") * ramp(0.7 - clv, *RAMPS["wick_close"]) * p
        recurring = _r(reversals, "reversal_count") if reversals is not None else 0.0
        frac = 0.8 * today + 0.2 * recurring
        words = f"Upper wick {upper:.0%} of the range with the close at {clv:.2f} of the range"
        if reversals:
            words += f"; {int(reversals)} high-volume reversal bar(s) in the last 20"
        out["upper_wick_rejection"] = (frac, {"upper_wick_pct": upper, "close_location": clv,
                                              "high_volume_reversals_20": reversals}, words + ".")

    level, high, close = _num(bo.get("level")), _num(bar.get("high")), _num(bar.get("close"))
    if level is None or high is None or close is None or level <= 0:
        out["failed_breakout"] = (None, {"breakout_level": level, "high": high}, None)
    else:
        pierce = _r((high / level - 1) * 100, "breakout_pierce")
        fail = _r((1 - close / level) * 100, "breakout_fail")
        frac = pierce * fail * (0.25 + 0.75 * (p or 0.0))
        out["failed_breakout"] = (frac, {"breakout_level": level, "high": high, "close": close},
                                  f"The high traded above the prior {bo.get('lookback')}-session closing high "
                                  f"({level:,.2f}) but the close finished below it.")

    if slope is None:
        out["obv_non_confirmation"] = (None, {"obv_slope_20": slope}, None)
    else:
        unconfirmed = obv.get("divergence") == "price_unconfirmed"
        frac = 0.5 * float(unconfirmed) + 0.5 * _r(-slope, "obv_slope")
        if unconfirmed:
            words = f"The close made a 20-session high while OBV did not (OBV slope {slope:+.2f})."
        else:
            words = f"OBV over 20 sessions is falling (slope {slope:+.2f})."
        out["obv_non_confirmation"] = (frac, {"obv_slope_20": slope, "divergence": obv.get("divergence")}, words)

    med = _num(rv.get("rvol_median_20"))
    if med is None or range_pct is None or clv is None:
        out["exhaustion_shock"] = (None, {"rvol_median_20": med, "range_pct": range_pct}, None)
    else:
        rejection = max(_r(1 - clv, "exhaustion_weak_close"), _r(upper, "upper_wick") if upper is not None else 0.0)
        frac = _r(med, "exhaustion_rvol_median") * _r(range_pct, "exhaustion_range") * rejection
        out["exhaustion_shock"] = (frac, {"rvol_median_20": med, "range_pct": range_pct, "close_location": clv,
                                          "upper_wick_pct": upper},
                                   f"Extreme volume (median-based RVOL20 {med:.1f}×) on a wide {range_pct:.1f}% range "
                                   f"that did not hold its highs — an exhaustion-style bar.")
    return out


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------

def _confidence(features: dict, coverage: float) -> tuple[str, list[str]]:
    quality = features.get("data_quality") or {}
    codes = set(quality.get("codes") or ())
    liq = features.get("liquidity") or {}
    level, reasons, cap = 0, [], 0

    def drop(steps, reason):
        nonlocal level
        level += steps
        reasons.append(reason)

    if "SPECIAL_SESSION_BAR" in codes:
        drop(2, "The as-of bar is an exchange special session; its volume is not comparable with a regular day's.")
    if "SPECIAL_SESSION_IN_BASELINE" in codes:
        drop(1, "A thin exchange special session is inside the 20-session volume baseline.")
    zero = sorted(c for c in codes if c.startswith("ZERO_"))
    if zero:
        drop(1, f"Zero-volume or zero-range data in the window ({', '.join(zero)}).")
    if "PARTIAL_HISTORY" in codes:
        cap = 2
        reasons.append("Fewer than 61 sessions of history: the percentile and 20-session counts are missing.")
    avg_tv = _num(liq.get("avg_traded_value_20"))
    if avg_tv is not None and avg_tv < VERY_THIN_TRADED_VALUE_INR:
        cap = 2
        reasons.append(f"Very thin trading: 20-session average traded value ₹{avg_tv / 1e7:,.2f} crore.")
    elif liq.get("liquidity_warning"):
        drop(1, "20-session average traded value is below the low-liquidity analytics threshold.")
    if (features.get("freshness") or {}).get("is_stale"):
        drop(1, "The newest stored candle is older than the staleness limit.")
    if coverage < 0.8:
        drop(1, f"Only {coverage:.0%} of the score's components could be evaluated.")
    if features.get("history_status") is None:
        reasons.append("History integrity status not yet recorded; the in-window corporate-action gap check "
                       "still applied.")
    return _LEVELS[min(max(level, cap), len(_LEVELS) - 1)], reasons


# --------------------------------------------------------------------------
# The one entry point
# --------------------------------------------------------------------------

def _reject_outcomes(obj, path="features"):
    """Research labels (outcome_*) read rows after the as-of bar; they must
    never reach the score. Any such key anywhere in the input is an error."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).startswith("outcome_"):
                raise ValueError(f"outcome column {key!r} passed into the score ({path})")
            _reject_outcomes(value, f"{path}.{key}")


def band_for(value: float | None) -> dict | None:
    if value is None:
        return None
    for lower, key, label in BANDS:
        if value >= lower:
            return {"key": key, "label": label}
    return {"key": BANDS[-1][1], "label": BANDS[-1][2]}


def _base(features: dict) -> dict:
    quality = features.get("data_quality") or {}
    return {
        "name": NAME,
        "score_version": SMART_MONEY_SCORE_VERSION,
        "feature_version": features.get("feature_version"),
        "symbol": features.get("symbol"),
        "instrument_token": features.get("instrument_token"),
        "as_of_date": features.get("as_of_date"),
        "score": None,
        "band": None,
        "confidence": INSUFFICIENT_DATA,
        "confidence_reasons": [],
        "positive_score": None,
        "negative_penalty": None,
        "liquidity_factor": None,
        "coverage": None,
        "components": {},
        "positive_reasons": [],
        "negative_reasons": [],
        "top_positive": [],
        "top_negative": [],
        "data_quality": {"status": quality.get("status"), "codes": list(quality.get("codes") or ())},
        "warnings": [],
        "disclaimer": DISCLAIMER,
        "weights": dict(WEIGHTS),
    }


def score(features: dict) -> dict:
    """The Smart Money Accumulation Score of one PK-23 VolumeFeatures object.

    Deterministic: the same features always give the same result. Reads
    nothing but `features`; rejects research outcome keys."""
    _reject_outcomes(features)
    result = _base(features)
    quality = features.get("data_quality") or {}
    if quality.get("status") == "UNAVAILABLE" or features.get("relative_volume") is None:
        codes = list(quality.get("codes") or ())
        result["confidence_reasons"] = [f"Volume features are unavailable ({', '.join(codes) or 'no data'})."]
        result["warnings"] = list(features.get("warnings") or ())
        return result
    if features.get("feature_version") != REQUIRED_FEATURE_VERSION:
        raise ValueError(f"score v{SMART_MONEY_SCORE_VERSION} reads feature_version {REQUIRED_FEATURE_VERSION}, "
                         f"got {features.get('feature_version')!r}")

    raw = _components(features)
    components, covered = {}, 0.0
    for name, kind, max_points, label in COMPONENTS:
        fraction, inputs, sentence = raw[name]
        if fraction is not None:
            covered += max_points
        points = 0.0 if fraction is None else clamp(fraction) * max_points * (1 if kind == POSITIVE else -1)
        components[name] = {
            "label": label, "kind": kind, "max_points": max_points,
            "points": round(points, 2), "available": fraction is not None,
            "inputs": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in inputs.items()},
            "reason": sentence if abs(points) >= TOP_MIN_POINTS else None,
        }
    coverage = covered / (POSITIVE_MAX + PENALTY_MAX)
    if coverage < 0.5:
        result["components"] = components
        result["coverage"] = round(coverage, 4)
        result["confidence_reasons"] = [f"Only {coverage:.0%} of the score's components could be evaluated."]
        return result

    positive = sum(c["points"] for c in components.values() if c["kind"] == POSITIVE)
    negative = sum(c["points"] for c in components.values() if c["kind"] == PENALTY)

    liq = features.get("liquidity") or {}
    avg_tv, threshold = _num(liq.get("avg_traded_value_20")), _num(liq.get("low_liquidity_threshold"))
    factor = 1.0
    if avg_tv is not None and threshold and avg_tv < threshold:
        thin = math.log10(threshold / max(avg_tv, 1.0))
        factor = 1.0 - LIQUIDITY_MAX_DAMPING * _r(thin, "liquidity_thin")

    value = round(clamp(NEUTRAL + factor * (positive + negative), 0.0, 100.0), 1)
    confidence, reasons = _confidence(features, coverage)

    ordered = [(name, c) for name, c in components.items()]   # COMPONENTS order breaks ties
    pos = sorted((x for x in ordered if x[1]["points"] >= TOP_MIN_POINTS), key=lambda x: -x[1]["points"])
    neg = sorted((x for x in ordered if x[1]["points"] <= -TOP_MIN_POINTS), key=lambda x: x[1]["points"])
    warnings = []
    if factor < 1.0:
        warnings.append(f"Thin liquidity: the score is pulled {1 - factor:.0%} of the way toward neutral (50).")

    result.update({
        "score": value,
        "band": band_for(value),
        "confidence": confidence,
        "confidence_reasons": reasons,
        "positive_score": round(positive, 2),
        "negative_penalty": round(negative, 2),
        "liquidity_factor": round(factor, 4),
        "coverage": round(coverage, 4),
        "components": components,
        "positive_reasons": [c["reason"] for _, c in pos],
        "negative_reasons": [c["reason"] for _, c in neg],
        "top_positive": [{"component": n, "label": c["label"], "points": c["points"], "reason": c["reason"]}
                         for n, c in pos[:TOP_N]],
        "top_negative": [{"component": n, "label": c["label"], "points": c["points"], "reason": c["reason"]}
                         for n, c in neg[:TOP_N]],
        "warnings": warnings,
    })
    return result
