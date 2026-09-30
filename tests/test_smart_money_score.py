"""PK-24 — Smart Money Accumulation Score (backend/smart_money_score.py).

Pure tests. Most start from a hand-built NEUTRAL VolumeFeatures object and
change one or two inputs, so every expected movement can be reasoned about
directly; the lookahead, special-session and duplicate-series cases run the
real PK-23 engine (compute_at) on bars. Numbering follows the PK-24 packet's
test list (1-28); API, UI, research and strategy isolation (29-35) live in
test_smart_money_score_api.py.
"""
import copy
import json
import random
from datetime import date

import pandas as pd
import pytest

from backend import smart_money_score as sms, volume_intelligence as vi

from test_backtest_realism import _golden_series
from test_volume_intelligence import bars


def neutral_features(**overrides) -> dict:
    """A clean, average bar: RVOL 1, median close, flat OBV, no evidence."""
    f = {
        "feature_version": vi.FEATURE_VERSION, "symbol": "TEST", "instrument_token": 408065,
        "as_of_date": "2026-01-05", "requested_as_of": None, "history_status": "OK",
        "data_quality": {"status": vi.OK, "codes": []},
        "warnings": [], "notes": [],
        "bar": {"volume": 100_000, "close": 100.0, "prev_close": 100.0, "open": 100.0, "high": 101.0, "low": 99.0},
        "relative_volume": {"avg_volume_20": 100_000, "median_volume_20": 100_000, "rvol_5": 1.0, "rvol_10": 1.0,
                            "rvol_20": 1.0, "rvol_median_20": 1.0, "volume_percentile_60": 50.0,
                            "volume_zscore_20": 0.0},
        "liquidity": {"traded_value": 1e9, "avg_traded_value_20": 1e9, "relative_traded_value_20": 1.0,
                      "low_liquidity_threshold": 5e7, "liquidity_warning": False},
        "structure": {"close_location": 0.5, "body_pct": 0.0, "upper_wick_pct": 0.25, "lower_wick_pct": 0.25,
                      "range_pct": 2.0, "true_range_pct": 2.0, "price_change_pct": 0.0, "gap_pct": 0.0,
                      "high_volume_strong_close": False, "high_volume_weak_close": False,
                      "high_volume_upper_wick": False},
        "obv": {"slope_5": 0.0, "slope_20": 0.0, "trend": "flat", "obv_new_high_20": False,
                "price_new_high_20": False, "divergence": "none"},
        "contraction": {"contraction_ratio_5_20": 1.0, "volume_slope_10": 0.0,
                        "pre_breakout_volume_contraction": False, "current_volume_expansion": False},
        "pullback": {"in_pullback": False, "pullback_bars": 0, "pullback_depth_pct": None,
                     "pullback_volume_ratio": None, "pullback_vs_advance_volume": None,
                     "recovery_volume_ratio": None, "is_recovery_bar": False, "low_volume_pullback": None},
        "evidence": {"today": {"accumulation_like": False, "distribution_like": False, "high_volume_reversal": False},
                     "accumulation_like_10": 0, "distribution_like_10": 0, "net_accumulation_10": 0,
                     "accumulation_like_20": 0, "distribution_like_20": 0, "net_accumulation_20": 0,
                     "up_volume_days_20": 0, "down_volume_days_20": 0, "high_volume_reversals_20": 0},
        "breakout": {"lookback": 30, "level": 105.0, "is_breakout": False, "close_above_level_pct": None,
                     "close_quality": None, "volume_ratio": None, "volume_percentile_60": None,
                     "traded_value_ratio": None, "base_contraction_ratio": None},
        "anomalies": {"abnormal_volume": False, "extreme_volume": False, "negative_shock": False,
                      "positive_shock": False},
        "legacy_volume_multiple_20": 1.0,
        "thresholds": {}, "interpretation": [],
    }
    return with_(f, **overrides)


def with_(features: dict, **dotted) -> dict:
    """A copy with 'group__key' (or 'group__sub__key') values replaced."""
    f = copy.deepcopy(features)
    for path, value in dotted.items():
        node = f
        keys = path.split("__")
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return f


def elevated(rvol=3.0, pct=95.0, **more) -> dict:
    return neutral_features(relative_volume__rvol_20=rvol, relative_volume__rvol_median_20=rvol,
                            relative_volume__volume_percentile_60=pct, **more)


def s(features) -> dict:
    return sms.score(features)


def pts(result, name) -> float:
    return result["components"][name]["points"]


# ------------------------------------------------------------ 1-4 baseline

def test_1_neutral_inputs_score_exactly_mid_range():
    r = s(neutral_features())
    assert r["score"] == 50.0
    assert r["positive_score"] == 0 and r["negative_penalty"] == 0
    assert r["band"]["key"] == "mixed"
    assert r["confidence"] == sms.HIGH
    assert r["top_positive"] == [] and r["top_negative"] == []


def _random_features(rng: random.Random) -> dict:
    return neutral_features(
        relative_volume__rvol_20=rng.choice([0.1, 0.8, 1.5, 3, 8, 50, 500]),
        relative_volume__rvol_median_20=rng.choice([0.1, 1, 2, 6, 100]),
        relative_volume__volume_percentile_60=rng.choice([0, 30, 60, 95, 100]),
        structure__close_location=rng.choice([0.0, 0.1, 0.5, 0.9, 1.0]),
        structure__upper_wick_pct=rng.choice([0.0, 0.4, 0.9, 1.0]),
        structure__price_change_pct=rng.choice([-20, -3, 0, 2, 9, 40]),
        structure__range_pct=rng.choice([0.5, 3, 9, 30]),
        obv__slope_20=rng.choice([-1, -0.3, 0, 0.3, 1]),
        obv__obv_new_high_20=rng.choice([True, False]),
        obv__divergence=rng.choice(["none", "confirmed", "obv_leads_price", "price_unconfirmed"]),
        contraction__contraction_ratio_5_20=rng.choice([0.1, 0.6, 1.0, 3.0]),
        evidence__accumulation_like_10=rng.choice([0, 2, 10]),
        evidence__accumulation_like_20=20 if rng.random() < 0.3 else 10,
        evidence__distribution_like_10=rng.choice([0, 2, 10]),
        evidence__distribution_like_20=rng.choice([10, 20]),
        evidence__high_volume_reversals_20=rng.choice([0, 5, 20]),
        pullback__in_pullback=rng.choice([True, False]),
        pullback__pullback_volume_ratio=rng.choice([0.1, 0.9, 3.0]),
        pullback__recovery_volume_ratio=rng.choice([0.2, 1.5, 30.0]),
        breakout__is_breakout=rng.choice([True, False]),
        breakout__close_quality=rng.choice([0.0, 1.0]),
        breakout__close_above_level_pct=1.0,
        bar__high=rng.choice([100.0, 104.0, 110.0]),
        bar__close=rng.choice([95.0, 100.0, 106.0]),
        liquidity__avg_traded_value_20=rng.choice([1e5, 3e7, 1e9, 1e12]),
        liquidity__liquidity_warning=rng.choice([True, False]),
    )


def test_2_score_is_always_bounded_0_to_100():
    rng = random.Random(24)
    seen = []
    for _ in range(3000):
        r = s(_random_features(rng))
        assert 0.0 <= r["score"] <= 100.0
        seen.append(r["score"])
        for name, c in r["components"].items():
            assert abs(c["points"]) <= c["max_points"] + 1e-9, name
    assert min(seen) < 25 and max(seen) > 75          # the whole range is reachable


def test_2b_extremes_reach_but_never_exceed_the_bounds():
    best = elevated(rvol=50, pct=100, structure__close_location=1.0, structure__upper_wick_pct=0.0,
                    structure__price_change_pct=3.0, evidence__accumulation_like_10=5,
                    evidence__accumulation_like_20=8, obv__slope_20=0.9, obv__obv_new_high_20=True,
                    obv__divergence="confirmed", contraction__contraction_ratio_5_20=0.3,
                    pullback__in_pullback=True, pullback__pullback_volume_ratio=0.3,
                    pullback__recovery_volume_ratio=5.0, breakout__is_breakout=True,
                    breakout__close_quality=1.0, breakout__close_above_level_pct=2.0,
                    breakout__level=97.0, bar__high=103.0, bar__close=103.0)
    r = s(best)
    assert r["score"] == 100.0 and r["positive_score"] == sms.POSITIVE_MAX
    worst = elevated(rvol=50, pct=100, structure__close_location=0.0, structure__upper_wick_pct=0.9,
                     structure__range_pct=12.0, evidence__distribution_like_10=5, evidence__distribution_like_20=8,
                     evidence__high_volume_reversals_20=5, obv__slope_20=-0.9, obv__divergence="price_unconfirmed",
                     bar__high=108.0, bar__close=100.0)
    w = s(worst)
    assert w["score"] == 0.0 and w["negative_penalty"] == -sms.PENALTY_MAX == -50.0


def test_3_deterministic_result():
    f = elevated(structure__close_location=0.85, evidence__accumulation_like_10=2, obv__slope_20=0.2)
    first = json.dumps(s(f), sort_keys=True)
    for _ in range(5):
        assert json.dumps(s(copy.deepcopy(f)), sort_keys=True) == first
    assert s(f) == s(f)


def test_3b_input_is_never_mutated():
    f = elevated(structure__close_location=0.85)
    before = json.dumps(f, sort_keys=True)
    s(f)
    assert json.dumps(f, sort_keys=True) == before


def test_4_version_and_weights_are_in_every_result():
    for f in (neutral_features(), neutral_features(data_quality={"status": vi.UNAVAILABLE, "codes": ["NO_HISTORY"]},
                                                   relative_volume=None)):
        r = s(f)
        assert r["score_version"] == sms.SMART_MONEY_SCORE_VERSION == "1.0"
        assert r["weights"] == dict(sms.WEIGHTS)
        assert r["disclaimer"] == sms.DISCLAIMER
    assert sms.POSITIVE_MAX == 50.0                        # neutral 50 + all positives = 100
    assert len({n for n, *_ in sms.COMPONENTS}) == len(sms.COMPONENTS)


def test_4b_other_feature_version_is_refused():
    with pytest.raises(ValueError, match="feature_version"):
        s(neutral_features(feature_version=2))


def test_4c_result_is_json_serialisable():
    json.dumps(s(elevated(structure__close_location=0.9)))


# ------------------------------------------------------------ 5-10 positive evidence

def test_5_elevated_rvol_raises_score():
    base = s(neutral_features(structure__close_location=0.7))["score"]
    up = s(elevated(rvol=2.5, structure__close_location=0.7))["score"]
    assert up > base
    assert pts(s(elevated(rvol=2.5, structure__close_location=0.7)), "participation") > 0


def test_5b_volume_credit_saturates():
    three = s(elevated(rvol=3.0, pct=100, structure__close_location=0.9))
    twenty = s(elevated(rvol=20.0, pct=100, structure__close_location=0.9))
    assert twenty["score"] == three["score"]               # 20x counts no more than ~3x


def test_6_strong_close_reinforces_elevated_volume():
    low_vol = s(neutral_features(structure__close_location=0.95))
    high_vol = s(elevated(structure__close_location=0.95))
    assert pts(high_vol, "close_quality") > 3 * pts(low_vol, "close_quality") > 0
    mid_close = s(elevated(structure__close_location=0.5))
    assert high_vol["score"] > mid_close["score"]


def test_7_repeated_accumulation_like_days_raise_score():
    scores = [s(neutral_features(evidence__accumulation_like_10=n, evidence__accumulation_like_20=n))["score"]
              for n in range(5)]
    assert scores == sorted(scores) and scores[3] > scores[0]
    assert scores[3] == scores[4]                          # bounded contribution


def test_8_obv_confirmation_raises_score():
    base = s(neutral_features())["score"]
    rising = s(neutral_features(obv__slope_20=0.35))["score"]
    new_high = s(neutral_features(obv__slope_20=0.35, obv__obv_new_high_20=True, obv__divergence="obv_leads_price"))
    assert base < rising < new_high["score"]
    assert "OBV at a 20-session high" in new_high["components"]["obv_confirmation"]["reason"]
    assert pts(new_high, "obv_confirmation") <= sms.WEIGHTS["obv_confirmation"] < sms.WEIGHTS["accumulation_frequency"]


def test_9_contraction_then_expansion_raises_score():
    no_base = s(elevated(rvol=2.0, structure__close_location=0.8))
    quiet_base = s(elevated(rvol=2.0, structure__close_location=0.8, contraction__contraction_ratio_5_20=0.55))
    assert pts(quiet_base, "contraction_expansion") > 0 == pts(no_base, "contraction_expansion")
    assert quiet_base["score"] > no_base["score"]
    # a quiet base alone, with no expansion, is not evidence
    assert pts(s(neutral_features(contraction__contraction_ratio_5_20=0.55)), "contraction_expansion") == 0


def test_10_low_volume_pullback_then_recovery_is_constructive():
    recovery = neutral_features(pullback__in_pullback=True, pullback__pullback_volume_ratio=0.45,
                                pullback__recovery_volume_ratio=2.2, structure__price_change_pct=1.5,
                                structure__close_location=0.7)
    r = s(recovery)
    assert pts(r, "pullback_participation") == sms.WEIGHTS["pullback_participation"]
    # an up bar that did not hold (weak close under a wick) is not a constructive recovery
    assert pts(s(with_(recovery, structure__close_location=0.2)), "pullback_participation") == 0
    # low volume by itself is not rewarded: no recovery participation, or no up bar
    assert pts(s(with_(recovery, pullback__recovery_volume_ratio=0.8)), "pullback_participation") == 0
    assert pts(s(with_(recovery, structure__price_change_pct=-0.5)), "pullback_participation") == 0
    # a heavy-volume pullback earns nothing
    assert pts(s(with_(recovery, pullback__pullback_volume_ratio=1.2)), "pullback_participation") == 0


def test_10b_breakout_participation_is_credited_but_not_required():
    bo = elevated(structure__close_location=0.9, breakout__is_breakout=True, breakout__close_quality=0.9,
                  breakout__close_above_level_pct=1.5, breakout__level=98.0)
    assert pts(s(bo), "breakout_participation") == sms.WEIGHTS["breakout_participation"]
    assert sms.WEIGHTS["breakout_participation"] < 10      # a high score never needs a breakout (see 17)


# ------------------------------------------------------------ 11-15 negative evidence

def test_11_high_volume_weak_close_reduces_score():
    r = s(elevated(structure__close_location=0.05, structure__price_change_pct=-1.0))
    assert pts(r, "high_volume_weak_close") == -sms.WEIGHTS["high_volume_weak_close"]
    assert r["score"] < 50
    # high volume alone never gives a high score
    assert s(elevated(rvol=10, pct=100))["score"] < 60


def test_12_distribution_days_reduce_score():
    scores = [s(neutral_features(evidence__distribution_like_10=n, evidence__distribution_like_20=n))["score"]
              for n in range(5)]
    assert scores == sorted(scores, reverse=True) and scores[3] < 50


def test_12b_accumulation_and_distribution_are_mirror_images():
    acc = s(neutral_features(evidence__accumulation_like_10=2, evidence__accumulation_like_20=3))
    dist = s(neutral_features(evidence__distribution_like_10=2, evidence__distribution_like_20=3))
    assert acc["score"] - 50 == pytest.approx(50 - dist["score"])
    both = s(neutral_features(evidence__accumulation_like_10=2, evidence__accumulation_like_20=3,
                              evidence__distribution_like_10=2, evidence__distribution_like_20=3))
    assert both["score"] == 50.0


def test_13_large_upper_wick_reduces_score():
    clean = s(elevated(structure__close_location=0.45, structure__upper_wick_pct=0.1))
    wick = s(elevated(structure__close_location=0.3, structure__upper_wick_pct=0.65))
    assert pts(wick, "upper_wick_rejection") < pts(clean, "upper_wick_rejection") <= 0
    assert wick["score"] < clean["score"]
    assert "Upper wick" in wick["components"]["upper_wick_rejection"]["reason"]


def test_14_obv_non_confirmation_reduces_score():
    confirmed = s(neutral_features(obv__slope_20=0.1, obv__obv_new_high_20=True, obv__price_new_high_20=True,
                                   obv__divergence="confirmed"))
    unconfirmed = s(neutral_features(obv__slope_20=0.1, obv__price_new_high_20=True,
                                     obv__divergence="price_unconfirmed"))
    assert pts(unconfirmed, "obv_non_confirmation") < 0 == pts(confirmed, "obv_non_confirmation")
    assert unconfirmed["score"] < confirmed["score"]
    falling = s(neutral_features(obv__slope_20=-0.4))
    assert falling["score"] < 50


def test_15_failed_breakout_reduces_score():
    # the high pierced the prior-30 closing high (105) by >1% but the close finished >1% below it
    failed = elevated(bar__high=107.0, bar__close=103.5, structure__close_location=0.3)
    held = elevated(bar__high=107.0, bar__close=106.5, structure__close_location=0.3)
    assert pts(s(failed), "failed_breakout") == -sms.WEIGHTS["failed_breakout"]
    assert pts(s(held), "failed_breakout") == 0
    assert "closing high" in s(failed)["components"]["failed_breakout"]["reason"]
    # no attempt: the high never reached the level
    assert pts(s(elevated(bar__high=104.0, bar__close=103.0)), "failed_breakout") == 0


def test_15b_exhaustion_style_bar_is_penalised():
    r = s(elevated(rvol=12, pct=100, structure__range_pct=9.0, structure__close_location=0.2,
                   structure__upper_wick_pct=0.7))
    assert pts(r, "exhaustion_shock") == -sms.WEIGHTS["exhaustion_shock"]
    # the same range and volume that HELD its highs is not exhaustion
    held = s(elevated(rvol=12, pct=100, structure__range_pct=9.0, structure__close_location=0.95,
                      structure__upper_wick_pct=0.03))
    assert pts(held, "exhaustion_shock") == 0


# ------------------------------------------------------------ 16-17 outliers

def test_16_twenty_x_rvol_with_weak_close_and_wick_is_not_a_high_score():
    r = s(elevated(rvol=20, pct=100, structure__close_location=0.1, structure__upper_wick_pct=0.75,
                   structure__range_pct=9.0, structure__price_change_pct=0.8, obv__slope_20=0.15))
    assert r["score"] < 40
    assert r["band"]["key"] in ("limited", "weak")
    assert {t["component"] for t in r["top_negative"]} >= {"exhaustion_shock", "high_volume_weak_close"}


def test_17_moderate_rvol_with_broad_confirmation_can_score_strongly():
    r = s(neutral_features(
        relative_volume__rvol_20=1.8, relative_volume__rvol_median_20=1.9, relative_volume__volume_percentile_60=88,
        structure__close_location=0.88, structure__upper_wick_pct=0.05, structure__price_change_pct=2.0,
        obv__slope_20=0.3, obv__obv_new_high_20=True, obv__divergence="obv_leads_price",
        evidence__accumulation_like_10=3, evidence__accumulation_like_20=4,
        pullback__in_pullback=True, pullback__pullback_volume_ratio=0.5, pullback__recovery_volume_ratio=1.9))
    assert r["score"] >= 75 and r["band"]["key"] == "strong"
    assert pts(r, "breakout_participation") == 0            # no breakout needed


def test_17b_repeated_evidence_outweighs_one_isolated_spike():
    spike = s(elevated(rvol=6, pct=100, structure__close_location=0.95, structure__upper_wick_pct=0.02,
                       structure__price_change_pct=3.0, evidence__accumulation_like_10=1,
                       evidence__accumulation_like_20=1))
    repeated = s(neutral_features(
        relative_volume__rvol_20=1.3, relative_volume__rvol_median_20=1.3, relative_volume__volume_percentile_60=70,
        structure__close_location=0.7, structure__price_change_pct=0.8, obv__slope_20=0.35,
        evidence__accumulation_like_10=3, evidence__accumulation_like_20=4))
    assert repeated["score"] > spike["score"]


# ------------------------------------------------------------ 18-22 confidence

def test_18_clean_history_is_high_confidence():
    r = s(elevated(structure__close_location=0.9))
    assert r["confidence"] == sms.HIGH and r["confidence_reasons"] == []


@pytest.mark.parametrize("codes", [["NO_HISTORY"], ["INSUFFICIENT_HISTORY"]])
def test_19_insufficient_history_gives_null_score(codes):
    f = neutral_features(data_quality={"status": vi.UNAVAILABLE, "codes": codes})
    for group in ("relative_volume", "liquidity", "structure", "obv", "evidence"):
        f[group] = None
    r = s(f)
    assert r["score"] is None and r["band"] is None
    assert r["confidence"] == sms.INSUFFICIENT_DATA
    assert codes[0] in r["confidence_reasons"][0]


def test_19b_real_short_history_gives_null_and_partial_history_is_low():
    short = vi.compute_at(bars([1000] * 15))
    assert s(short)["score"] is None and s(short)["confidence"] == sms.INSUFFICIENT_DATA
    partial = vi.compute_at(bars([1000] * 40 + [3000], closes=[100.0] * 40 + [102.0],
                                 highs=[101.0] * 40 + [102.2], lows=[99.0] * 41))
    r = s(partial)
    assert partial["data_quality"]["codes"] == ["PARTIAL_HISTORY"]
    assert r["score"] is not None and r["confidence"] == sms.LOW


@pytest.mark.parametrize("code", ["HISTORY_SUSPECT", "HISTORY_NEEDS_REFRESH", "UNVERIFIED_GAP_IN_WINDOW"])
def test_20_suspect_corporate_action_data_gives_null(code):
    f = neutral_features(data_quality={"status": vi.UNAVAILABLE, "codes": [code]})
    f["relative_volume"] = None
    r = s(f)
    assert r["score"] is None and r["confidence"] == sms.INSUFFICIENT_DATA and code in r["confidence_reasons"][0]


def test_20b_real_unverified_split_gap_gives_null():
    closes = [100.0] * 50 + [50.0] * 20                     # an unadjusted 2:1 split inside the window
    r = s(vi.compute_at(bars([1000] * 70, closes=closes)))
    assert r["score"] is None and "UNVERIFIED_GAP_IN_WINDOW" in r["confidence_reasons"][0]


def test_21_special_session_degrades_confidence_not_score():
    in_baseline = neutral_features(data_quality={"status": vi.DEGRADED, "codes": ["SPECIAL_SESSION_IN_BASELINE"]})
    on_bar = neutral_features(data_quality={"status": vi.DEGRADED, "codes": ["SPECIAL_SESSION_BAR"]})
    assert s(in_baseline)["confidence"] == sms.MEDIUM
    assert s(on_bar)["confidence"] == sms.LOW
    assert s(in_baseline)["score"] == s(on_bar)["score"] == 50.0      # not forced to zero


def test_21b_real_special_session_in_the_baseline():
    df = bars([1_000_000] * 90, start=date(2023, 11, 20))
    special = pd.DataFrame([{"date": "2024-03-02", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                             "volume": 40_000}])
    df = pd.concat([df[df["date"] < "2024-03-02"], special, df[df["date"] > "2024-03-02"]], ignore_index=True)
    f = vi.compute_at(df, as_of="2024-03-08")
    assert "SPECIAL_SESSION_IN_BASELINE" in f["data_quality"]["codes"]
    r = s(f)
    assert r["score"] is not None and r["confidence"] == sms.MEDIUM
    assert any("special session" in reason for reason in r["confidence_reasons"])


def test_22_low_liquidity_lowers_confidence_and_damps_toward_neutral():
    strong = elevated(structure__close_location=0.95, evidence__accumulation_like_10=3,
                      evidence__accumulation_like_20=3)
    liquid = s(strong)
    thin = s(with_(strong, liquidity__avg_traded_value_20=2e7, liquidity__liquidity_warning=True))
    very_thin = s(with_(strong, liquidity__avg_traded_value_20=5e5, liquidity__liquidity_warning=True))
    assert liquid["confidence"] == sms.HIGH and thin["confidence"] == sms.MEDIUM and very_thin["confidence"] == sms.LOW
    assert 50 < very_thin["score"] < thin["score"] < liquid["score"]
    assert very_thin["liquidity_factor"] == pytest.approx(1 - sms.LIQUIDITY_MAX_DAMPING)
    assert any("Thin liquidity" in w for w in very_thin["warnings"])
    # damping is symmetric: a thin, distribution-heavy name is pulled UP toward 50, not penalised
    weak = neutral_features(evidence__distribution_like_10=3, evidence__distribution_like_20=3)
    assert s(weak)["score"] < s(with_(weak, liquidity__avg_traded_value_20=5e5))["score"] < 50


def test_22b_stale_data_and_zero_codes_lower_confidence():
    assert s(neutral_features(freshness={"is_stale": True}))["confidence"] == sms.MEDIUM
    zero = neutral_features(data_quality={"status": vi.DEGRADED, "codes": ["ZERO_VOLUME_IN_WINDOW"]})
    assert s(zero)["confidence"] == sms.MEDIUM
    combined = neutral_features(data_quality={"status": vi.DEGRADED,
                                              "codes": ["SPECIAL_SESSION_IN_BASELINE", "ZERO_VOLUME_IN_WINDOW"]},
                                freshness={"is_stale": True})
    assert s(combined)["confidence"] == sms.LOW


def test_22c_unknown_history_status_is_a_note_not_a_downgrade():
    r = s(neutral_features(history_status=None))
    assert r["confidence"] == sms.HIGH
    assert any("not yet recorded" in reason for reason in r["confidence_reasons"])


def test_22d_missing_components_lower_confidence_or_null_the_score():
    no_counts = neutral_features(evidence__accumulation_like_10=None, evidence__distribution_like_10=None)
    r = s(no_counts)
    assert r["coverage"] < 0.8 and r["confidence"] == sms.MEDIUM
    assert r["components"]["accumulation_frequency"]["available"] is False
    gutted = neutral_features(structure__close_location=None, structure__price_change_pct=None,
                              obv__slope_20=None, evidence__accumulation_like_10=None,
                              evidence__distribution_like_10=None)
    g = s(gutted)
    assert g["score"] is None and g["confidence"] == sms.INSUFFICIENT_DATA


# ------------------------------------------------------------ 23-24 explanation

def test_23_top_contributors_are_sorted_stable_and_capped():
    f = elevated(structure__close_location=0.9, structure__price_change_pct=2.0, obv__slope_20=0.4,
                 evidence__accumulation_like_10=2, evidence__accumulation_like_20=2,
                 evidence__distribution_like_10=1, evidence__distribution_like_20=1, obv__divergence="none")
    r = s(f)
    tops = r["top_positive"]
    assert 0 < len(tops) <= 3 and len(r["top_negative"]) <= 3
    assert [t["points"] for t in tops] == sorted((t["points"] for t in tops), reverse=True)
    assert tops[0]["component"] == "accumulation_frequency"
    assert r["top_negative"][0]["component"] == "distribution_frequency"
    assert all(t["reason"] for t in tops + r["top_negative"])
    assert s(copy.deepcopy(f))["top_positive"] == tops
    # ties keep COMPONENTS order
    order = [n for n, *_ in sms.COMPONENTS]
    pos = [n for n, c in r["components"].items() if c["points"] >= sms.TOP_MIN_POINTS]
    ranked = sorted(pos, key=lambda n: (-r["components"][n]["points"], order.index(n)))
    assert [t["component"] for t in tops] == ranked[:3]


def test_23b_every_nonzero_component_explains_itself():
    rng = random.Random(7)
    for _ in range(300):
        r = s(_random_features(rng))
        for name, c in r["components"].items():
            if abs(c["points"]) >= sms.TOP_MIN_POINTS:
                assert c["reason"], name
        assert len(r["positive_reasons"]) == sum(1 for c in r["components"].values()
                                                 if c["points"] >= sms.TOP_MIN_POINTS)


def _all_text(result) -> str:
    parts = [result["band"]["label"] if result["band"] else "", *result["positive_reasons"],
             *result["negative_reasons"], *result["confidence_reasons"], *result["warnings"],
             *[t["reason"] or "" for t in result["top_positive"] + result["top_negative"]],
             *[c["label"] for c in result["components"].values()]]
    return " ".join(parts).lower()


def test_24_wording_never_claims_who_traded_or_advises():
    rng = random.Random(11)
    phrases = sms.forbidden_phrases()
    for _ in range(500):
        text = _all_text(s(_random_features(rng)))
        assert not [p for p in phrases if p in text], text
    for _, _, label in sms.BANDS:
        assert not [p for p in phrases if p in label.lower()]
    assert "not proof" in sms.DISCLAIMER.lower()
    assert {"institution", "smart money", "fii", "buy"} <= set(phrases)


# ------------------------------------------------------------ 25-26 no lookahead

def _mutate_after(df: pd.DataFrame, as_of: str) -> pd.DataFrame:
    out = df.copy()
    future = out["date"] > as_of
    for column in ("open", "high", "low", "close"):
        out.loc[future, column] = out.loc[future, column] * 0.37
    out.loc[future, "volume"] = out.loc[future, "volume"] * 60 + 7
    return out


@pytest.mark.parametrize("index", [80, 150, 233, 310])
def test_25_future_rows_never_change_the_score(index):
    df = pd.DataFrame(_golden_series(1000, n_bars=400))
    df["date"] = df["date"].astype(str).str[:10]
    as_of = df["date"].iloc[index]
    before = s(vi.compute_at(df, as_of))
    after = s(vi.compute_at(_mutate_after(df, as_of), as_of))
    cut = s(vi.compute_at(df.iloc[: index + 1]))
    assert before["score"] is not None
    assert before == after == cut


def test_26_outcome_columns_are_rejected():
    f = neutral_features()
    f["outcome_fwd_ret_10_pct"] = 12.0
    with pytest.raises(ValueError, match="outcome"):
        s(f)
    nested = neutral_features()
    nested["structure"]["outcome_mfe_20_pct"] = 3.0
    with pytest.raises(ValueError, match="outcome"):
        s(nested)


# ------------------------------------------------------------ 27-28 duplicate exchange series

def test_27_nse_and_bse_histories_are_scored_separately():
    nse = bars([1_000_000] * 60 + [2_600_000], closes=[100.0] * 60 + [102.0], highs=[101.0] * 60 + [102.1],
               lows=[99.0] * 61)
    bse = bars([20_000] * 60 + [21_000], closes=[100.0] * 60 + [102.0], highs=[101.0] * 60 + [102.1],
               lows=[99.0] * 61)
    a = s(vi.compute_at(nse, instrument_token=2076161, symbol="AWL"))
    b = s(vi.compute_at(bse, instrument_token=139125252, symbol="AWL"))
    assert a["instrument_token"] == 2076161 and b["instrument_token"] == 139125252
    assert a["components"]["participation"]["inputs"]["rvol_20"] == 2.6
    assert b["components"]["participation"]["inputs"]["rvol_20"] == 1.05
    assert a["score"] > b["score"]


# ------------------------------------------------------------ robustness (N)

def test_robust_rvol_199_vs_201_is_not_a_cliff():
    lo = s(elevated(rvol=1.99, pct=90, structure__close_location=0.8))["score"]
    hi = s(elevated(rvol=2.01, pct=90, structure__close_location=0.8))["score"]
    assert abs(hi - lo) < 1.0


_CONTINUOUS = ("relative_volume__rvol_20", "relative_volume__rvol_median_20", "relative_volume__volume_percentile_60",
               "structure__close_location", "structure__upper_wick_pct", "structure__price_change_pct",
               "structure__range_pct", "obv__slope_20", "contraction__contraction_ratio_5_20",
               "pullback__pullback_volume_ratio", "pullback__recovery_volume_ratio",
               "liquidity__avg_traded_value_20")


def test_robust_small_input_changes_move_the_score_a_little():
    rng = random.Random(3)
    worst = 0.0
    for _ in range(300):
        f = _random_features(rng)
        base = s(f)["score"]
        for path in _CONTINUOUS:
            group, key = path.split("__")
            value = f[group][key]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value == 0:
                continue
            for factor in (0.99, 1.01):
                g = copy.deepcopy(f)
                g[group][key] = value * factor
                worst = max(worst, abs(s(g)["score"] - base))
    assert worst <= 2.5, worst


def test_robust_score_is_monotonic_in_close_location_on_elevated_volume():
    scores = [s(elevated(structure__close_location=x / 20, structure__upper_wick_pct=0.0))["score"]
              for x in range(21)]
    assert scores == sorted(scores)
