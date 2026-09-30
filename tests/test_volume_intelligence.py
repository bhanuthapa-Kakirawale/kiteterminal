"""PK-23 — Volume Intelligence engine (backend/volume_intelligence.py).

Pure-function tests on hand-built bars whose every expected value can be
worked out by hand, plus no-lookahead properties over a seeded random walk.
Numbering follows the PK-23 packet's test list (1-31); the API/UI cases
(32-34) and the research harness live in test_volume_intelligence_api.py.
"""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from backend import volume_intelligence as vi
from backend.strategies import STRATEGIES

from test_backtest_realism import GOLDEN_SIGNALS, _golden_series

MON = date(2026, 1, 5)


def bars(volumes, closes=None, opens=None, highs=None, lows=None, start: date = MON) -> pd.DataFrame:
    """One weekday bar per volume. Defaults: flat 100 close, 99-101 range."""
    n = len(volumes)
    closes = list(closes) if closes is not None else [100.0] * n
    opens = list(opens) if opens is not None else list(closes)
    highs = list(highs) if highs is not None else [max(o, c) + 1 for o, c in zip(opens, closes)]
    lows = list(lows) if lows is not None else [min(o, c) - 1 for o, c in zip(opens, closes)]
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return pd.DataFrame({"date": days, "open": opens, "high": highs, "low": lows, "close": closes,
                         "volume": volumes})


def at(df, **kwargs) -> dict:
    return vi.compute_at(df, **kwargs)


def golden_frame(seed: int = 1000, n_bars: int = 400) -> pd.DataFrame:
    return pd.DataFrame(_golden_series(seed, n_bars=n_bars))


# ------------------------------------------------------------ B: relative volume

def test_1_rvol20_baseline_excludes_the_current_bar():
    f = at(bars([100] * 20 + [1000]))
    rv = f["relative_volume"]
    assert rv["avg_volume_20"] == 100
    assert rv["rvol_20"] == 10.0             # 1000 / mean(prior 20); never 1000 / 145
    # The spike does not inflate its own denominator — only the legacy
    # strategy multiple (SMA-20 including the bar) does: 1000 / 145.
    assert f["legacy_volume_multiple_20"] == pytest.approx(1000 / 145, abs=1e-4)


def test_2_rvol_mean_windows():
    prior = list(range(1, 21))               # 1..20
    f = at(bars(prior + [30]))
    rv = f["relative_volume"]
    assert rv["rvol_5"] == pytest.approx(30 / np.mean(prior[-5:]), abs=1e-4)
    assert rv["rvol_10"] == pytest.approx(30 / np.mean(prior[-10:]), abs=1e-4)
    assert rv["rvol_20"] == pytest.approx(30 / np.mean(prior), abs=1e-4)


def test_3_median_rvol_resists_one_extreme_prior_session():
    prior = [100] * 19 + [10_000]
    f = at(bars(prior + [300]))
    rv = f["relative_volume"]
    assert rv["median_volume_20"] == 100
    assert rv["rvol_median_20"] == 3.0
    assert rv["rvol_20"] == pytest.approx(300 / 595, abs=1e-4)   # the mean is dragged up by the outlier


@pytest.mark.parametrize("current, expected", [
    (45.5, 75.0),                    # 45 of 60 prior sessions below
    (45, (44 + 0.5) / 60 * 100),     # mid-rank for the tie
    (1000, 100.0),
    (0.5, 0.0),
])
def test_4_volume_percentile_uses_the_prior_60_sessions_only(current, expected):
    prior = list(range(1, 61))
    f = at(bars(prior + [current]))
    assert f["relative_volume"]["volume_percentile_60"] == pytest.approx(expected, abs=0.01)
    # deterministic
    assert at(bars(prior + [current]))["relative_volume"] == f["relative_volume"]


def test_5_zero_baseline_is_undefined_not_infinite():
    f = at(bars([0] * 20 + [100]))
    assert f["relative_volume"]["rvol_20"] is None
    assert f["relative_volume"]["rvol_median_20"] is None
    assert f["data_quality"]["status"] == vi.DEGRADED
    assert {"ZERO_BASELINE", "ZERO_VOLUME_IN_WINDOW"} <= set(f["data_quality"]["codes"])


def test_6_insufficient_history():
    short = at(bars([100] * 20))
    assert short["data_quality"]["status"] == vi.UNAVAILABLE
    assert "INSUFFICIENT_HISTORY" in short["data_quality"]["codes"]
    assert short["relative_volume"] is None
    partial = at(bars([100] * 20 + [200]))
    assert partial["data_quality"]["status"] == vi.DEGRADED
    assert partial["data_quality"]["codes"] == ["PARTIAL_HISTORY"]
    assert partial["relative_volume"]["rvol_20"] == 2.0
    assert partial["relative_volume"]["volume_percentile_60"] is None
    empty = at(bars([]))
    assert empty["data_quality"]["codes"] == ["NO_HISTORY"]


# ------------------------------------------------------------ D: structure

def _one_bar(o, h, low, c, volume=300, prior=20):
    return bars([100] * prior + [volume], closes=[100.0] * prior + [c], opens=[100.0] * prior + [o],
                highs=[101.0] * prior + [h], lows=[99.0] * prior + [low])


def test_7_close_at_the_high():
    st = at(_one_bar(100, 110, 100, 110))["structure"]
    assert st["close_location"] == 1.0
    assert st["upper_wick_pct"] == 0.0
    assert st["body_pct"] == 1.0
    assert st["price_change_pct"] == 10.0
    assert st["high_volume_strong_close"] is True
    assert st["high_volume_weak_close"] is False


def test_8_close_at_the_low():
    st = at(_one_bar(100, 100, 90, 90))["structure"]
    assert st["close_location"] == 0.0
    assert st["lower_wick_pct"] == 0.0
    assert st["high_volume_weak_close"] is True
    assert st["high_volume_strong_close"] is False


def test_9_zero_range_bar_is_handled_safely():
    f = at(_one_bar(100, 100, 100, 100))
    st = f["structure"]
    assert st["close_location"] is None and st["upper_wick_pct"] is None and st["body_pct"] is None
    assert st["high_volume_strong_close"] is False and st["high_volume_weak_close"] is False
    assert "ZERO_RANGE_BAR" in f["data_quality"]["codes"]
    assert f["evidence"]["today"]["accumulation_like"] is False


def test_10_wicks_body_and_gap():
    st = at(_one_bar(o=101, h=110, low=99, c=103))["structure"]   # range 11
    assert st["upper_wick_pct"] == pytest.approx(7 / 11, abs=1e-4)
    assert st["lower_wick_pct"] == pytest.approx(2 / 11, abs=1e-4)
    assert st["body_pct"] == pytest.approx(2 / 11, abs=1e-4)
    assert st["close_location"] == pytest.approx(4 / 11, abs=1e-4)
    assert st["gap_pct"] == 1.0
    assert st["range_pct"] == 11.0


def test_10b_high_volume_long_upper_wick_is_flagged():
    f = at(_one_bar(o=100, h=110, low=99, c=100.5))
    assert f["structure"]["high_volume_upper_wick"] is True
    assert f["evidence"]["today"]["high_volume_reversal"] is True


# ------------------------------------------------------------ E: OBV

def test_11_up_days_add_volume():
    closes = [100 + i for i in range(21)]
    obv = at(bars([100] * 21, closes=closes))["obv"]
    assert obv["slope_5"] == 1.0 and obv["slope_20"] == 1.0
    assert obv["trend"] == "rising"


def test_12_down_days_subtract_volume():
    closes = [200 - i for i in range(21)]
    obv = at(bars([100] * 21, closes=closes))["obv"]
    assert obv["slope_5"] == -1.0 and obv["trend"] == "falling"


def test_13_flat_day_leaves_obv_unchanged():
    # last five bars: up 100, down 50, flat 1000, up 100, up 100
    closes = [100.0] * 16 + [101, 100, 100, 101, 102]
    volumes = [100] * 16 + [100, 50, 1000, 100, 100]
    obv = at(bars(volumes, closes=closes))["obv"]
    assert obv["slope_5"] == pytest.approx(250 / 1350, abs=1e-4)


def test_14_obv_new_high_without_a_price_high():
    closes, volumes = [], []
    for i in range(23):
        closes.append(100.0 if i % 2 == 0 else 101.0)
        volumes.append(100)
    closes.append(100.5)       # up from 100 (the last close above), below the 101 closing high
    volumes.append(1000)
    obv = at(bars(volumes, closes=closes))["obv"]
    assert obv["obv_new_high_20"] is True
    assert obv["price_new_high_20"] is False
    assert obv["divergence"] == "obv_leads_price"


def test_15_divergence_rules():
    # OBV peaked on a heavy up-day, then drifted down; price then makes a
    # new closing high on light volume: price_unconfirmed.
    closes = [100.0] * 10 + [105.0] + [104.9 - 0.1 * i for i in range(12)] + [106.0]
    volumes = [100] * 10 + [10_000] + [100] * 12 + [50]
    obv = at(bars(volumes, closes=closes))["obv"]
    assert obv["price_new_high_20"] is True and obv["obv_new_high_20"] is False
    assert obv["divergence"] == "price_unconfirmed"
    # both at a new high: confirmed; neither: none
    rising = at(bars([100] * 25, closes=[100 + i for i in range(25)]))["obv"]
    assert rising["divergence"] == "confirmed"
    flat = at(bars([100] * 25))["obv"]
    assert flat["divergence"] == "none"


# ------------------------------------------------------------ F: contraction

def test_16_declining_volume_is_contraction():
    volumes = [1000] * 15 + [400] * 5 + [400]
    con = at(bars(volumes))["contraction"]
    assert con["contraction_ratio_5_20"] == pytest.approx(400 / 850, abs=1e-4)
    assert con["pre_breakout_volume_contraction"] is True
    assert con["volume_slope_10"] < 0
    assert con["current_volume_expansion"] is False


def test_17_expansion_after_contraction_is_reported_separately():
    f = at(bars([1000] * 15 + [400] * 5 + [3000]))
    assert f["contraction"]["pre_breakout_volume_contraction"] is True
    assert f["contraction"]["current_volume_expansion"] is True
    assert f["relative_volume"]["rvol_20"] == pytest.approx(3000 / 850, abs=1e-4)


def test_18_low_volume_pullback():
    closes = [100.0 + i for i in range(22)]          # advance to a 121 closing high (index 21)
    volumes = [1000] * 22
    closes += [119.0, 118.0, 117.5]                   # three-bar pullback on light volume
    volumes += [300, 300, 300]
    closes.append(119.5)                              # recovery bar
    volumes.append(900)
    pb = at(bars(volumes, closes=closes))["pullback"]
    assert pb["in_pullback"] is True
    assert pb["pullback_bars"] == 3
    assert pb["pullback_volume_ratio"] == pytest.approx(0.3, abs=1e-4)
    assert pb["pullback_vs_advance_volume"] == pytest.approx(0.3, abs=1e-4)
    assert pb["recovery_volume_ratio"] == pytest.approx(3.0, abs=1e-4)
    assert pb["pullback_depth_pct"] == pytest.approx((117.5 / 121 - 1) * 100, abs=1e-3)
    assert pb["low_volume_pullback"] is True
    assert pb["is_recovery_bar"] is True
    # at a fresh high there is no pullback
    assert at(bars([1000] * 25, closes=[100 + i for i in range(25)]))["pullback"]["in_pullback"] is False


# ------------------------------------------------------------ G: evidence

def test_19_strong_close_high_volume_up_day_is_accumulation_like():
    f = at(_one_bar(o=100, h=104, low=99.5, c=103.8))
    assert f["evidence"]["today"]["accumulation_like"] is True
    assert f["evidence"]["today"]["distribution_like"] is False


def test_20_weak_close_high_volume_down_day_is_distribution_like():
    f = at(_one_bar(o=100, h=100.5, low=96, c=96.2))
    assert f["evidence"]["today"]["distribution_like"] is True
    assert f["evidence"]["today"]["accumulation_like"] is False
    assert f["structure"]["high_volume_weak_close"] is True


def _evidence_frame(event_offsets: list[int], up: bool, n: int = 45) -> pd.DataFrame:
    """n bars of volume 100 on a 99-101 range; at each offset from the end
    (0 = the last bar) a volume-400 bar closing at the extreme of its range."""
    volumes = [100] * n
    closes, opens, highs, lows = [100.0] * n, [100.0] * n, [101.0] * n, [99.0] * n
    for k in event_offsets:
        i = n - 1 - k
        volumes[i] = 400
        opens[i] = 100.0
        closes[i] = 102.9 if up else 97.1
        highs[i] = 103.0 if up else 100.2
        lows[i] = 99.8 if up else 97.0
        if i + 1 < n:      # the next bar re-opens at 100: a down (or up) close
            closes[i + 1] = 100.0
    return bars(volumes, closes=closes, opens=opens, highs=highs, lows=lows)


def test_21_rolling_accumulation_count():
    ev = at(_evidence_frame([0, 3, 7, 12], up=True))["evidence"]
    assert ev["accumulation_like_10"] == 3            # offsets 0, 3, 7
    assert ev["accumulation_like_20"] == 4            # plus offset 12
    assert ev["distribution_like_10"] == 0
    assert ev["net_accumulation_10"] == 3
    assert ev["up_volume_days_20"] == 4


def test_22_rolling_distribution_count():
    ev = at(_evidence_frame([1, 5, 15], up=False))["evidence"]
    assert ev["distribution_like_10"] == 2
    assert ev["distribution_like_20"] == 3
    assert ev["accumulation_like_20"] == 0
    assert ev["net_accumulation_20"] == -3
    assert ev["down_volume_days_20"] == 3


# ------------------------------------------------------------ H: breakout

def test_23_breakout_participation():
    n = 40
    closes = [100.0 if i % 2 else 101.0 for i in range(n)] + [105.0]
    opens = closes[:-1] + [101.0]
    highs = [c + 0.5 for c in closes[:-1]] + [105.5]
    lows = [c - 0.5 for c in closes[:-1]] + [100.0]
    volumes = [100] * n + [400]
    f = at(bars(volumes, closes=closes, opens=opens, highs=highs, lows=lows),
           strategy="momentum_breakout", params={"volume_multiple": 3.0})
    bo = f["breakout"]
    assert bo["is_breakout"] is True and bo["lookback"] == 30 and bo["level"] == 101.0
    assert bo["close_above_level_pct"] == pytest.approx((105 / 101 - 1) * 100, abs=1e-3)
    assert bo["volume_ratio"] == 4.0
    assert bo["close_quality"] == pytest.approx(5 / 5.5, abs=1e-4)
    assert bo["traded_value_ratio"] == pytest.approx(400 * 105 / np.mean([100 * c for c in closes[-21:-1]]), abs=1e-4)
    cmp_ = f["strategy_comparison"]
    assert cmp_["legacy_multiple"] == pytest.approx(400 / 115, abs=1e-4)
    assert cmp_["legacy_pass"] is True
    assert cmp_["rvol_20_prior_only"] == 4.0
    assert cmp_["effective_multiple_of_prior_19_mean"] == pytest.approx(57 / 17, abs=1e-4)


def test_23b_no_breakout_leaves_participation_empty():
    bo = at(bars([100] * 40))["breakout"]
    assert bo["is_breakout"] is False and bo["volume_ratio"] is None


# ------------------------------------------------------------ I: anomalies

def test_abnormal_extreme_and_shock_flags():
    prior = list(range(100, 160))                          # 60 prior sessions
    down = bars(prior + [1000], closes=[100.0] * 60 + [95.0], opens=[100.0] * 61,
                highs=[101.0] * 60 + [100.2], lows=[99.0] * 60 + [94.9])
    an = at(down)["anomalies"]
    assert an["abnormal_volume"] and an["extreme_volume"] and an["negative_shock"]
    assert an["positive_shock"] is False
    up = bars(prior + [1000], closes=[100.0] * 60 + [105.0], opens=[100.0] * 61,
              highs=[101.0] * 60 + [105.1], lows=[99.0] * 60 + [99.8])
    an = at(up)["anomalies"]
    assert an["positive_shock"] is True and an["negative_shock"] is False
    quiet = at(bars(prior + [130]))
    assert quiet["anomalies"]["abnormal_volume"] is False
    assert quiet["relative_volume"]["volume_zscore_20"] < 0


# ------------------------------------------------------------ data quality

def test_24_nan_volume_inside_the_window_is_unavailable():
    df = bars([100.0] * 70)
    df.loc[65, "volume"] = np.nan
    f = at(df)
    assert f["data_quality"]["status"] == vi.UNAVAILABLE
    assert "MISSING_VALUE" in f["data_quality"]["codes"]
    assert f["relative_volume"] is None
    # ...but a bad bar far outside the feature window does not matter
    df = bars([100.0] * 200)
    df.loc[5, "volume"] = None
    assert at(df)["data_quality"]["status"] == vi.OK


def test_25_negative_volume_is_rejected():
    df = bars([100] * 30)
    df.loc[28, "volume"] = -5
    f = at(df)
    assert f["data_quality"]["status"] == vi.UNAVAILABLE
    assert f["data_quality"]["codes"] == ["NEGATIVE_VOLUME"]


@pytest.mark.parametrize("status, code", [("SUSPECT", "HISTORY_SUSPECT"), ("NEEDS_REFRESH", "HISTORY_NEEDS_REFRESH")])
def test_26_flagged_corporate_action_history_is_not_computed(status, code):
    f = at(bars([100] * 80), history_status=status)
    assert f["data_quality"]["status"] == vi.UNAVAILABLE
    assert code in f["data_quality"]["codes"]
    assert all(f[g] is None for g in ("relative_volume", "obv", "evidence"))
    assert f["interpretation"][0].startswith("Volume features are unavailable")


def test_26b_unverified_split_sized_gap_inside_the_window():
    closes = [200.0] * 40 + [100.0] * 30                      # a 2:1 split never back-adjusted
    df = bars([100] * 40 + [200] * 30, closes=closes)
    split_date = df["date"].iloc[40]
    f = at(df)
    assert f["data_quality"]["status"] == vi.UNAVAILABLE
    assert "UNVERIFIED_GAP_IN_WINDOW" in f["data_quality"]["codes"]
    assert f["data_quality"]["detail"]["unverified_gaps"][0]["date"] == split_date
    verified = at(df, verified_gaps=[split_date])
    assert verified["data_quality"]["status"] == vi.OK
    # once the gap is older than the feature window it no longer matters
    later = bars([100] * 40 + [200] * 70, closes=[200.0] * 40 + [100.0] * 70)
    assert at(later)["data_quality"]["status"] == vi.OK


def test_27_duplicate_date_and_invalid_candles():
    df = bars([100] * 30)
    df.loc[29, "date"] = df.loc[28, "date"]
    assert at(df)["data_quality"]["codes"] == ["DUPLICATE_DATE"]
    df = bars([100] * 30)
    df.loc[29, "high"], df.loc[29, "low"] = 95.0, 105.0
    assert at(df)["data_quality"]["codes"] == ["INVALID_CANDLE"]
    df = bars([100] * 30)
    df.loc[29, "close"] = 150.0                               # above the high
    assert "INVALID_CANDLE" in at(df)["data_quality"]["codes"]


def test_special_session_in_the_window_is_reported():
    """2024-03-02 was a Saturday disaster-recovery session: a real candle with
    a fraction of a normal day's volume. Features are still computed, but the
    result says the window contains it (found on the real history, PK-23)."""
    df = bars([1000] * 40, start=date(2024, 2, 1))
    special = pd.DataFrame([{"date": "2024-03-02", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                             "volume": 40}])
    df = pd.concat([df[df["date"] < "2024-03-02"], special, df[df["date"] > "2024-03-02"]], ignore_index=True)
    f = at(df)                                   # 2024-03-02 is among the prior 20 sessions
    assert f["data_quality"]["status"] == vi.DEGRADED
    assert "SPECIAL_SESSION_IN_BASELINE" in f["data_quality"]["codes"]
    assert f["data_quality"]["detail"]["special_sessions"] == ["2024-03-02"]
    assert f["relative_volume"]["rvol_20"] is not None
    on_the_day = at(df, as_of="2024-03-02")
    assert "SPECIAL_SESSION_BAR" in on_the_day["data_quality"]["codes"]
    later = at(pd.concat([df, bars([1000] * 25, start=date(2024, 3, 28))], ignore_index=True))
    assert later["data_quality"]["status"] == vi.OK          # out of the baseline: no longer flagged


# ------------------------------------------------------------ no lookahead

_COMPARED = None


def _assert_rows_equal(a: pd.Series, b: pd.Series, where: str):
    for column in a.index:
        if column == "date":
            assert a[column] == b[column], where
            continue
        x, y = a[column], b[column]
        if pd.isna(x) or pd.isna(y):
            assert pd.isna(x) and pd.isna(y), (where, column, x, y)
        else:
            assert float(x) == pytest.approx(float(y), rel=1e-9, abs=1e-9), (where, column, x, y)


def test_28_changing_future_rows_never_changes_an_earlier_feature():
    df = golden_frame(1017, 300)
    base = vi.compute_series(df)
    T = 200
    future = df.copy()
    future.loc[T + 1:, "volume"] = future.loc[T + 1:, "volume"] * 7 + 1
    future.loc[T + 1:, "close"] = future.loc[T + 1:, "close"] * 0.5
    future.loc[T + 1:, "high"] = future.loc[T + 1:, "high"] * 2
    changed = vi.compute_series(future)
    for i in range(T + 1):
        _assert_rows_equal(base.iloc[i], changed.iloc[i], f"row {i}")
    as_of = df["date"].iloc[T]
    cut = vi.compute_at(df.iloc[: T + 1], as_of=as_of)
    assert vi.compute_at(df, as_of=as_of) == cut
    assert vi.compute_at(future, as_of=as_of) == cut


def test_28b_series_equals_as_of_computation_on_every_row():
    """The backtest/research path (compute_series over all history) and the
    as-of path (compute_at on history cut at T, window-local) agree on every
    single row — so no feature silently needs more than LOOKBACK_BARS rows."""
    df = golden_frame(1034, 260)
    full = vi.compute_series(df)
    for T in range(len(df)):
        window = df.iloc[max(0, T + 1 - vi.LOOKBACK_BARS): T + 1].reset_index(drop=True)
        _assert_rows_equal(vi.compute_series(window).iloc[-1], full.iloc[T], f"row {T}")


def test_29_percentile_contains_no_current_or_future_row():
    df = golden_frame(1051, 150)
    T = 120
    expected_window = df["volume"].iloc[T - 60:T].to_numpy(dtype=float)
    v = float(df["volume"].iloc[T])
    expected = 100 * ((expected_window < v).sum() + 0.5 * (expected_window == v).sum()) / 60
    series = vi.compute_series(df)
    assert series["volume_percentile_60"].iloc[T] == pytest.approx(expected, abs=1e-9)
    spiked = df.copy()
    spiked.loc[T + 1, "volume"] = 10 ** 9
    assert vi.compute_series(spiked)["volume_percentile_60"].iloc[T] == pytest.approx(expected, abs=1e-9)


def test_30_obv_slope_contains_no_future_row():
    df = golden_frame(1068, 150)
    T = 100
    before = vi.compute_series(df)[["obv_slope_5", "obv_slope_20", "obv_new_high_20", "obv_divergence_code"]]
    shifted = df.copy()
    shifted.loc[T + 1:, "close"] = shifted.loc[T + 1:, "close"][::-1].to_numpy()
    after = vi.compute_series(shifted)[before.columns]
    pd.testing.assert_frame_equal(before.iloc[: T + 1], after.iloc[: T + 1])


# ------------------------------------------------------------ strategies (P)

@pytest.mark.parametrize("name", ["momentum_breakout", "trend_pullback"])
def test_31_legacy_multiple_is_the_strategy_own_number_and_signals_are_unchanged(name):
    module = STRATEGIES[name]
    params = dict(module.DEFAULT_PARAMS)
    for n in range(4):
        df = pd.DataFrame(_golden_series(1000 + n * 17))
        snapshot = df.copy(deep=True)
        series = vi.compute_series(df)
        pd.testing.assert_frame_equal(df, snapshot)                # the engine never mutates its input
        hits = []
        for i in range(len(df)):
            if df["date"].iloc[i] < "2025-09-01":
                continue
            result = module.evaluate_entry(df.iloc[: i + 1], params)
            assert result["volume_multiple_actual"] == pytest.approx(
                series["legacy_volume_multiple_20"].iloc[i], rel=1e-12)
            if result["passed"]:
                hits.append([n, df["date"].iloc[i]])
        expected = [h for h in GOLDEN_SIGNALS[name] if h[0] == n]
        assert hits == expected


def test_31b_rsi_mean_reversion_has_no_volume_rule():
    cmp_ = vi.strategy_volume_comparison(at(bars([100] * 30)), "rsi_mean_reversion",
                                         dict(STRATEGIES["rsi_mean_reversion"].DEFAULT_PARAMS))
    assert cmp_["has_volume_rule"] is False
    assert "no volume condition" in cmp_["rule"]


def test_legacy_rule_equals_57_over_17_of_the_prior_19_mean():
    """volume > 3 x SMA20(including the bar)  <=>  volume > 57/17 x mean(prior 19)."""
    prior19 = [100] * 19
    threshold = 57 / 17 * 100
    just_above = at(bars([100] + prior19 + [threshold + 0.01]), strategy="momentum_breakout",
                    params={"volume_multiple": 3.0})["strategy_comparison"]
    just_below = at(bars([100] + prior19 + [threshold - 0.01]), strategy="momentum_breakout",
                    params={"volume_multiple": 3.0})["strategy_comparison"]
    assert just_above["legacy_pass"] is True and just_below["legacy_pass"] is False


# ------------------------------------------------------------ misc

def test_thresholds_are_centralised_overridable_and_echoed():
    df = _one_bar(100, 110, 100, 110)
    assert at(df)["thresholds"]["elevated_rvol"] == 1.5
    strict = at(df, thresholds={"elevated_rvol": 5.0})
    assert strict["thresholds"]["elevated_rvol"] == 5.0
    assert strict["structure"]["high_volume_strong_close"] is False
    with pytest.raises(ValueError):
        at(df, thresholds={"not_a_threshold": 1})


def test_liquidity_warning_is_descriptive_and_configurable():
    f = at(bars([1000] * 21))                  # Rs 1 lakh/day
    assert f["liquidity"]["liquidity_warning"] is True
    assert f["liquidity"]["traded_value"] == 100_000
    rich = at(bars([10_000_000] * 21))         # Rs 100 crore/day
    assert rich["liquidity"]["liquidity_warning"] is False
    assert at(bars([1000] * 21), thresholds={"low_liquidity_inr": 1.0})["liquidity"]["liquidity_warning"] is False


def test_interpretation_is_factual_across_many_bars():
    df = golden_frame(1085, 300)
    for T in range(60, 300, 7):
        for line in vi.compute_at(df.iloc[: T + 1], strategy="momentum_breakout",
                                  params={"volume_multiple": 3.0})["interpretation"]:
            lower = line.lower()
            assert not any(p in lower for p in vi.FORBIDDEN_PHRASES), line


def test_as_of_on_a_non_session_day_uses_the_previous_bar():
    df = bars([100] * 30)
    saturday = (date.fromisoformat(df["date"].iloc[-1]) + timedelta(days=1)).isoformat()
    f = at(df, as_of=saturday)
    assert f["as_of_date"] == df["date"].iloc[-1]
    assert f["notes"]
