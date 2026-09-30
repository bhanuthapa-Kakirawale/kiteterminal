"""PK-25: out-of-sample validation harness for Smart Money Score v1.0.

Covers the dataset (point-in-time scores, outcomes strictly after the signal,
no outcome in any feature matrix, exclusions, ordering), chronological
splits and purging, holdout isolation, the frozen buckets, the statistics,
the incremental models, robustness, the PK-21 filter simulation, isolation of
the production code, the manifest protocol and the pre-declared classifier.
"""

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend import backtest, smart_money_score as sms, volume_intelligence as vi, volume_research as vr
from backend import smart_money_validation as smv
from backend import smart_money_validation_stats as st
from backend.strategies import STRATEGIES
from test_backtest_realism import _golden_series
from test_smart_money_score_api import DECISION_PATHS

ROOT = Path(__file__).resolve().parent.parent
OUT = smv.OUTCOME
Y10 = f"{OUT}fwd_ret_10_pct"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def _golden_frames(n=4):
    return {f"S{k}": pd.DataFrame(_golden_series(1000 + k * 17)) for k in range(n)}


@pytest.fixture(scope="module")
def golden_rows():
    frames = _golden_frames()
    tokens = {s: 100 + k for k, s in enumerate(sorted(frames))}
    return smv.build_dataset(frames, tokens, {}, start="2025-09-01", market_proxy="S0")


@pytest.fixture(scope="module")
def golden_df(golden_rows, tmp_path_factory):
    path = tmp_path_factory.mktemp("pk25") / "dataset.csv"
    smv.write_rows(golden_rows, path)
    return smv.load_dataset(path)


def synthetic(n=360, seed=7, signal=0.0):
    """A typed research dataset with a controllable score->return link."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2024-09-02", "2026-08-31")
    picks = np.sort(rng.integers(0, len(days) - 30, n))
    rows = []
    for k, j in enumerate(picks):
        score = round(float(rng.uniform(20, 95)), 1)
        noise = rng.normal(0, 4, 3)
        row = {
            "strategy": smv.STRATEGY_ORDER[k % 3], "symbol": f"S{k % 12}",
            "signal_date": days[j].strftime("%Y-%m-%d"), "bar_index": float(j),
            "data_quality": "OK", "dq_codes": "", "smart_money_score": score,
            "rvol_20": float(rng.lognormal(0.3, 0.5)), "volume_percentile_60": float(rng.uniform(0, 100)),
            "close_location": float(rng.uniform(0, 1)), "accumulation_like_10": float(rng.integers(0, 4)),
            "distribution_like_10": float(rng.integers(0, 4)), "obv_slope_20": float(rng.uniform(-0.5, 0.5)),
            "net_accumulation_20": float(rng.integers(-3, 4)), "price_change_pct": float(rng.normal(1, 3)),
            "excl_reason": "", "special_session_baseline": float(k % 17 == 0), "liquidity_warning": float(k % 23 == 0),
            "mkt_above_200dma": float(k % 5 != 0), "mkt_vol_regime": ("low", "mid", "high")[k % 3],
            "liquidity_bucket": "large", "quarter": smv.quarter_of(days[j].strftime("%Y-%m-%d")),
            f"{OUT}window_end": days[j + smv.PURGE_SESSIONS].strftime("%Y-%m-%d"),
            f"{OUT}trade_traded": 1.0, f"{OUT}trade_forced_close": 0.0,
        }
        for h, e in zip(smv.HORIZONS, noise):
            ret = signal * (score - 50) / 10 + e
            row[f"{OUT}fwd_ret_{h}_pct"] = ret
            row[f"{OUT}mfe_{h}_pct"] = abs(ret) + 1
            row[f"{OUT}mae_{h}_pct"] = -abs(e) - 1
        row[f"{OUT}trade_net_r"] = float(noise[1] / 4)
        row[f"{OUT}trade_won"] = float(noise[1] > 0)
        rows.append(row)
    return pd.DataFrame(rows)


@pytest.fixture
def fast_bootstrap(monkeypatch):
    monkeypatch.setattr(smv, "BOOTSTRAP_REPS", 200)


def _frame_hash(df: pd.DataFrame) -> str:
    return hashlib.sha256(pd.util.hash_pandas_object(df, index=True).values.tobytes()).hexdigest()


# --------------------------------------------------------------------------
# DATA 1-5
# --------------------------------------------------------------------------

def test_01_score_is_fixed_at_the_signal_date_and_blind_to_future_rows(golden_rows):
    frames = {s: smv._normal_frame(df) for s, df in _golden_frames().items()}
    assert golden_rows
    for row in golden_rows:
        cut = frames[row["symbol"]].iloc[: row["bar_index"] + 1]
        assert row["signal_date"] == cut["date"].iloc[-1]
        assert row["smart_money_score"] == sms.score(vi.compute_at(cut))["score"]
    # Rewrite every row after one signal: its score and features cannot move.
    row = golden_rows[0]
    df = frames[row["symbol"]].copy()
    after = df.index > row["bar_index"]
    for column in ("open", "high", "low", "close"):
        df.loc[after, column] = df.loc[after, column] * 0.37
    df.loc[after, "volume"] = df.loc[after, "volume"] * 60 + 7
    module = STRATEGIES[row["strategy"]]
    again = smv.signal_rows(module, {row["symbol"]: df}, dict(module.DEFAULT_PARAMS), row["signal_date"],
                            row["signal_date"], tokens={row["symbol"]: row["instrument_token"]})
    assert len(again) == 1
    for key, value in again[0].items():
        assert row[key] == value, key


def test_01b_dataset_features_match_the_pk24_research_export(golden_rows):
    frames = _golden_frames()
    module = STRATEGIES["momentum_breakout"]
    params = dict(module.DEFAULT_PARAMS)
    reference = vr.signal_feature_rows(module, frames, params, "2025-09-01", "2026-09-18", with_outcomes=False)
    ours = [r for r in golden_rows if r["strategy"] == "momentum_breakout"]
    key = lambda r: (r["symbol"], r["signal_date"])
    ours_by_key = {key(r): r for r in ours}
    assert sorted(ours_by_key) == sorted(key(r) for r in reference)
    for ref in reference:
        mine = ours_by_key[key(ref)]
        for column in (*vr.FEATURE_COLUMNS, *vr.SCORE_COLUMNS, "legacy_volume_pass"):
            assert mine[column] == ref[column], column


def test_02_outcomes_start_after_the_signal_bar():
    df = pd.DataFrame({
        "date": [f"2026-01-{d:02d}" for d in range(1, 31)],
        "open": [100.0 + i for i in range(30)], "high": [110.0 + i for i in range(30)],
        "low": [90.0 + i for i in range(30)], "close": [105.0 + i for i in range(30)],
        "volume": [1000] * 30,
    })
    out = smv.market_outcomes(df, 3)
    entry = df["open"].iloc[4]
    assert out[f"{OUT}fwd_ret_5_pct"] == round((df["close"].iloc[8] / entry - 1) * 100, 4)
    assert out[f"{OUT}mfe_5_pct"] == round((df["high"].iloc[4:9].max() / entry - 1) * 100, 4)
    assert out[f"{OUT}mae_5_pct"] == round((df["low"].iloc[4:9].min() / entry - 1) * 100, 4)
    assert out[f"{OUT}window_end"] == df["date"].iloc[23]
    # Bars at or before the signal cannot move any outcome.
    changed = df.copy()
    changed.loc[:3, ["open", "high", "low", "close"]] = 1.0
    assert smv.market_outcomes(changed, 3) == out
    # A horizon without enough bars is None, never a truncated value.
    late = smv.market_outcomes(df, 25)
    assert late[f"{OUT}fwd_ret_5_pct"] is None and late[f"{OUT}window_end"] is None


def test_02b_twenty_session_outcomes_equal_the_pk23_definition(golden_rows):
    frames = {s: smv._normal_frame(df) for s, df in _golden_frames().items()}
    for row in golden_rows:
        ref = vr.forward_outcomes(frames[row["symbol"]], row["bar_index"])
        for h in smv.HORIZONS:
            assert row[f"{OUT}fwd_ret_{h}_pct"] == ref[f"{OUT}fwd_ret_{h}_pct"]
        assert row[f"{OUT}mfe_20_pct"] == ref[f"{OUT}mfe_20_pct"]
        assert row[f"{OUT}mae_20_pct"] == ref[f"{OUT}mae_20_pct"]


def test_03_outcome_columns_can_never_enter_a_feature_matrix(golden_df):
    with pytest.raises(ValueError):
        smv.feature_matrix(golden_df, ["smart_money_score", Y10])
    for features in smv.MODELS.values():
        assert not any(f.startswith(OUT) for f in features)
    assert not any(f.startswith(OUT) for f in smv.CONTINUOUS_MODEL_FEATURES)
    matrix = smv.feature_matrix(smv.add_derived(golden_df), smv.CONTINUOUS_MODEL_FEATURES)
    assert not any(c.startswith(OUT) for c in matrix.columns)
    # Every outcome column comes after every non-outcome column in the file.
    columns = list(golden_df.columns)
    first_outcome = min(i for i, c in enumerate(columns) if c.startswith(OUT))
    assert all(c.startswith(OUT) for c in columns[first_outcome:])


def test_04_suspect_rows_are_excluded_and_counted():
    assert smv.exclusion_reason({"smart_money_score": None, "dq_codes": ""}) == "score_null"
    assert smv.exclusion_reason({"smart_money_score": 61.0, "dq_codes": "SPECIAL_SESSION_BAR"}) == \
        "special_session_bar"
    assert smv.exclusion_reason({"smart_money_score": 61.0, "dq_codes": "SPECIAL_SESSION_IN_BASELINE"}) == ""
    df = synthetic(60)
    df.loc[3, "excl_reason"] = "score_null"
    df.loc[3, "smart_money_score"] = np.nan
    df.loc[7, "excl_reason"] = "special_session_bar"
    kept = smv.primary(df)
    assert 3 not in kept.index and 7 not in kept.index and len(kept) == 58
    summary = smv.dataset_summary(df, None, [])
    assert summary["exclusions"] == {"eligible": 58, "score_null": 1, "special_session_bar": 1}


def test_05_dataset_ordering_is_deterministic(golden_rows, tmp_path):
    keys = [(r["signal_date"], r["strategy"], r["symbol"]) for r in golden_rows]
    assert keys == sorted(keys)
    frames = _golden_frames()
    reversed_frames = {s: frames[s] for s in sorted(frames, reverse=True)}
    tokens = {s: 100 + k for k, s in enumerate(sorted(frames))}
    again = smv.build_dataset(reversed_frames, tokens, {}, start="2025-09-01", market_proxy="S0",
                              with_trades=False)
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    columns = [c for c in smv.dataset_columns(golden_rows) if not c.startswith(f"{OUT}trade_")
               and not c.startswith(f"{OUT}pf_")]
    smv.write_rows(golden_rows, a, columns)
    smv.write_rows(again, b, columns)
    assert a.read_bytes() == b.read_bytes()


# --------------------------------------------------------------------------
# SPLITS 6-8
# --------------------------------------------------------------------------

def test_06_split_is_chronological():
    df = synthetic()
    splits = smv.split_dates(df)
    seg = smv.assign_segments(df, splits)
    dev, val, hold = (seg[seg["segment"] == s]["signal_date"] for s in ("development", "validation", "holdout"))
    assert dev.max() < splits["validation_start"] <= val.min()
    assert val.max() < splits["holdout_start"] <= hold.min()
    assert splits["validation_start"].endswith("-01") and splits["holdout_start"].endswith("-01")
    # The segment is a pure function of the date: shuffling rows changes nothing.
    shuffled = df.sample(frac=1.0, random_state=3)
    assert smv.split_dates(shuffled) == splits
    again = smv.assign_segments(shuffled, splits).sort_index()
    assert (again["segment"] == seg["segment"]).all()
    # Monotone: no later signal is ever in an earlier segment.
    order = {"development": 0, "validation": 1, "holdout": 2}
    ranked = seg.sort_values("signal_date")["segment"].map(order).to_numpy()
    assert (np.diff(ranked) >= 0).all()


def test_06b_split_uses_signal_dates_only():
    df = synthetic()
    garbage = df.copy()
    for column in [c for c in df.columns if c.startswith(OUT) and c != f"{OUT}window_end"]:
        garbage[column] = 12345.0
    assert smv.split_dates(garbage) == smv.split_dates(df)


def test_07_purge_removes_rows_whose_outcome_window_crosses_a_boundary():
    df = synthetic()
    splits = smv.split_dates(df)
    seg = smv.assign_segments(df, splits)
    dev = seg[seg["segment"] == "development"]
    crossing = dev[f"{OUT}window_end"] >= splits["validation_start"]
    assert crossing.any()
    assert (dev.loc[crossing, "purged"]).all() and not (dev.loc[~crossing, "purged"]).any()
    val = seg[seg["segment"] == "validation"]
    assert (val["purged"] == (val[f"{OUT}window_end"] >= splits["holdout_start"])).all()
    assert not seg[seg["segment"] == "holdout"]["purged"].any()
    view = smv.pre_holdout_view(df, splits)
    assert (view[f"{OUT}window_end"] < splits["holdout_start"]).all()


def test_08_holdout_rows_never_reach_the_pre_holdout_stage(tmp_path, fast_bootstrap):
    df = synthetic(signal=0.3)
    splits = smv.split_dates(df)
    view = smv.pre_holdout_view(df, splits)
    assert (view["signal_date"] < splits["holdout_start"]).all()
    garbage = df.copy()
    hold = garbage["signal_date"] >= splits["holdout_start"]
    hold_nan = hold & (np.arange(len(garbage)) % 2 == 0)
    for column in [c for c in df.columns if c.startswith(OUT) and c != f"{OUT}window_end"]:
        garbage.loc[hold, column] = -9999.0
        garbage.loc[hold_nan, column] = np.nan      # even the availability of an outcome is invisible
    garbage.loc[hold, "smart_money_score"] = 99.9
    tables = smv.run_stage(df, splits, "pre_holdout")
    assert set(tables["_result"]["segment_counts"]["holdout"]) == {"signals", "eligible"}
    a = smv.write_stage(tables, tmp_path / "a", "pre_holdout")
    b = smv.write_stage(smv.run_stage(garbage, splits, "pre_holdout"), tmp_path / "b", "pre_holdout")
    assert a == b
    # ...while the holdout stage does read them.
    c = smv.write_stage(smv.run_stage(df, splits, "holdout"), tmp_path / "c", "holdout")
    d = smv.write_stage(smv.run_stage(garbage, splits, "holdout"), tmp_path / "d", "holdout")
    assert c != d


def test_08b_walk_forward_trains_only_on_the_past():
    df = synthetic()
    splits = smv.split_dates(df)
    folds = smv.walk_forward_folds(df, splits["holdout_start"])
    assert folds
    for fold in folds:
        assert (fold["train"]["signal_date"] < fold["test_start"]).all()
        assert (fold["train"][f"{OUT}window_end"] < fold["test_start"]).all()
        assert (fold["test"]["signal_date"] >= fold["test_start"]).all()
        assert (fold["test"]["signal_date"] < splits["holdout_start"]).all()
        assert (fold["test"][f"{OUT}window_end"] < splits["holdout_start"]).all()


# --------------------------------------------------------------------------
# BUCKETS 9-11
# --------------------------------------------------------------------------

def test_09_buckets_are_the_frozen_pk24_buckets():
    assert [(label, lo, hi) for label, lo, hi in smv.BUCKETS] == [
        ("0-24", 0.0, 25.0), ("25-44", 25.0, 45.0), ("45-59", 45.0, 60.0), ("60-74", 60.0, 75.0),
        ("75-100", 75.0, 100.0001)]
    cases = {0.0: "0-24", 24.9: "0-24", 25.0: "25-44", 44.9: "25-44", 45.0: "45-59", 59.9: "45-59",
             60.0: "60-74", 74.9: "60-74", 75.0: "75-100", 100.0: "75-100"}
    for score, bucket in cases.items():
        assert smv.bucket_of(score) == bucket
    assert smv.bucket_of(None) is None and smv.bucket_of(float("nan")) is None
    assert [b[0] for b in sms.BANDS][:2] == [75.0, 60.0]   # the PK-24 band edges the filters reuse
    manifest = smv.read_manifest(ROOT / "research" / "pk25")
    if manifest.get("design_freeze"):
        assert manifest["design_freeze"]["design"]["buckets"] == smv._jsonable([list(b) for b in smv.BUCKETS])


def test_10_bucket_counts_are_exact(fast_bootstrap):
    ev = smv.prepare(synthetic())
    table = smv.bucket_table(ev, "x")
    expected = {label: int(((ev["smart_money_score"] >= lo) & (ev["smart_money_score"] < hi)).sum())
                for label, lo, hi in smv.BUCKETS}
    assert {r["bucket"]: r["n"] for r in table} == expected
    assert sum(r["n"] for r in table) == len(ev)


def test_11_empty_and_small_buckets_are_flagged_not_interpreted(fast_bootstrap):
    ev = smv.prepare(synthetic(40))
    ev = ev[ev["smart_money_score"] >= 30]
    table = {r["bucket"]: r for r in smv.bucket_table(ev, "x")}
    assert table["0-24"]["n"] == 0 and table["0-24"]["small_sample"]
    assert table["0-24"]["median_ret_10"] is None and table["0-24"]["trade_profit_factor"] is None
    for row in table.values():
        assert row["small_sample"] == (row["n"] < smv.SMALL_BUCKET_N)
        if row["n_trades"] < smv.PF_MIN_N:
            assert row["trade_profit_factor"] is None


# --------------------------------------------------------------------------
# METRICS 12-16
# --------------------------------------------------------------------------

def test_12_median_mean_and_trimmed_mean():
    values = [1.0, 2.0, 3.0, 100.0, float("nan")]
    assert st.median(values) == 2.5
    assert st.mean(values) == 26.5
    assert st.trimmed_mean(list(range(10)), 0.1) == 4.5
    assert st.median([]) is None and st.mean([float("nan")]) is None
    original = np.array([1.0, 50.0, -40.0])
    clipped = st.winsorize(original, -5, 5)
    assert list(clipped) == [1.0, 5.0, -5.0] and list(original) == [1.0, 50.0, -40.0]


def test_13_win_rate_and_wilson_interval():
    assert st.win_rate([1.0, -1.0, 0.0, 2.0, float("nan")]) == (2, 4)
    lo, hi = st.wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-4) and hi == pytest.approx(0.7634, abs=1e-4)
    lo0, hi0 = st.wilson(0, 20)
    assert lo0 == 0.0 and hi0 == pytest.approx(0.1611, abs=1e-4)
    assert st.wilson(0, 0) == (None, None)
    assert st.profit_factor([2.0, -1.0, 1.0, -1.0]) == 1.5
    assert st.profit_factor([1.0, 2.0]) is None


def test_14_mfe_and_mae_bucket_medians(fast_bootstrap):
    ev = smv.prepare(synthetic())
    table = {r["bucket"]: r for r in smv.bucket_table(ev, "x")}
    part = ev[ev["bucket"] == "60-74"]
    assert table["60-74"]["median_mfe_10"] == pytest.approx(float(np.median(part[f"{OUT}mfe_10_pct"])), abs=1e-6)
    assert table["60-74"]["median_mae_10"] == pytest.approx(float(np.median(part[f"{OUT}mae_10_pct"])), abs=1e-6)


def test_15_spearman_handles_ties_and_direction():
    assert list(st.rankdata([10, 20, 20, 30])) == [1.0, 2.5, 2.5, 4.0]
    assert list(st.rankdata([3, 1, 2])) == [3.0, 1.0, 2.0]
    x, y = [1, 2, 2, 3], [1, 3, 2, 4]
    expected = float(np.corrcoef([1, 2.5, 2.5, 4], [1, 3, 2, 4])[0, 1])
    assert st.spearman(x, y) == pytest.approx(expected)
    assert st.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert st.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert st.spearman([1, 2, float("nan"), 4, 5], [2, 4, 100, 8, 10]) == pytest.approx(1.0)
    assert st.spearman([1, 1, 1], [1, 2, 3]) is None
    assert st.spearman([1, 2], [1, 2]) is None
    assert st.fisher_p_two_sided(0.0, 50) == pytest.approx(1.0)


def test_16_bootstrap_is_deterministic_for_a_fixed_seed():
    rng = np.random.default_rng(1)
    x, y = rng.normal(size=120), rng.normal(size=120)
    clusters = np.repeat(np.arange(40), 3)
    stat = lambda idx: st.spearman(x[idx], y[idx])
    a = st.cluster_bootstrap(clusters, stat, 300, 25)
    b = st.cluster_bootstrap(clusters, stat, 300, 25)
    c = st.cluster_bootstrap(clusters, stat, 300, 26)
    assert np.array_equal(a, b) and not np.array_equal(a, c)
    lo, hi = st.interval(a)
    assert lo < st.spearman(x, y) < hi
    assert st.holm({"a": 0.01, "b": 0.04, "c": 0.03}) == pytest.approx({"a": 0.03, "b": 0.06, "c": 0.06})
    assert st.holm({"a": None, "b": 0.02}) == {"a": None, "b": 0.02}


# --------------------------------------------------------------------------
# INCREMENTAL 17-20
# --------------------------------------------------------------------------

def test_17_baseline_models_contain_no_score():
    for name in ("M0", "M1", "M2"):
        assert "smart_money_score" not in smv.MODELS[name]
        assert not any("sms" in f for f in smv.MODELS[name])


def test_18_score_models_add_exactly_the_score_column():
    for with_score, without in smv.MODEL_PAIRS:
        assert list(smv.MODELS[with_score]) == [*smv.MODELS[without], "smart_money_score"]
    assert set(smv.MODELS["M4"]) == {"log_rvol_20", "smart_money_score"}
    assert set(smv.MODELS["M1"]) == {"log_rvol_20"}


def test_18b_the_score_model_gains_ic_only_when_the_score_carries_signal():
    df = synthetic(600, seed=11, signal=1.0)
    ev = smv.prepare(df)
    train, test = ev.iloc[:400], ev.iloc[400:]
    fit = smv.fit_models(train, test)
    assert fit["models"]["M4"]["ic"] > fit["models"]["M1"]["ic"] + 0.2
    null = smv.prepare(synthetic(600, seed=11, signal=0.0))
    fit0 = smv.fit_models(null.iloc[:400], null.iloc[400:])
    assert abs(fit0["models"]["M4"]["ic"] - fit0["models"]["M1"]["ic"]) < 0.15


def test_19_estimators_match_reference_computations():
    rng = np.random.default_rng(5)
    x = st.add_intercept(rng.normal(size=(200, 3)))
    y = x @ np.array([0.5, 1.0, -2.0, 0.3]) + rng.normal(scale=0.1, size=200)
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    assert np.allclose(st.ols_fit(x, y), coef)
    xl = st.add_intercept(rng.normal(size=(4000, 2)))
    truth = np.array([-0.3, 1.2, -0.8])
    labels = (rng.uniform(size=4000) < 1 / (1 + np.exp(-(xl @ truth)))).astype(float)
    beta = st.logistic_fit(xl, labels)
    assert np.allclose(beta, truth, atol=0.15)
    scores, ys = rng.normal(size=60), (rng.uniform(size=60) > 0.5).astype(float)
    pairs = [(a, b) for a, la in zip(scores, ys) if la == 1 for b, lb in zip(scores, ys) if lb == 0]
    brute = sum(1.0 if a > b else 0.5 if a == b else 0.0 for a, b in pairs) / len(pairs)
    assert st.auc(scores, ys) == pytest.approx(brute)
    assert st.auc([1, 2], [1, 1]) is None


def test_20_no_model_is_persisted_and_nothing_live_imports_the_harness(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ev = smv.prepare(synthetic(200, signal=0.5))
    smv.fit_models(ev.iloc[:150], ev.iloc[150:])
    assert list(tmp_path.iterdir()) == []
    for name in ("smart_money_validation.py", "smart_money_validation_stats.py"):
        source = (ROOT / "backend" / name).read_text(encoding="utf-8")
        for forbidden in ("import pickle", "import joblib", "import sklearn", "from sklearn", "to_pickle",
                          "np.save"):
            assert forbidden not in source
    live = [p for p in (ROOT / "backend").rglob("*.py") if not p.name.startswith("smart_money_validation")]
    assert len(live) > 20
    for path in live:
        assert "smart_money_validation" not in path.read_text(encoding="utf-8"), path
    for path in DECISION_PATHS:
        assert "smart_money_validation" not in (ROOT / path).read_text(encoding="utf-8")
    assert "smart_money_validation" not in (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# ROBUSTNESS 21-22
# --------------------------------------------------------------------------

def test_21_robustness_and_outlier_handling_never_alter_the_dataset(fast_bootstrap):
    df = synthetic(signal=0.2)
    df.loc[5, Y10] = 400.0
    before = _frame_hash(df)
    ev = smv.prepare(smv.primary(df))
    ev_before = _frame_hash(ev)
    rows = smv.robustness_table(ev, "2025Q1", "x")
    smv.evaluate(df, df, "x", "2025Q1")
    assert _frame_hash(df) == before and _frame_hash(ev) == ev_before
    by_variant = {r["variant"]: r for r in rows}
    assert by_variant["full"]["mean_ret10"] != by_variant["full"]["winsor_mean_ret10"]
    assert by_variant["drop_largest_abs_return"]["n"] == by_variant["full"]["n"] - 1


def test_22_leave_group_out_is_deterministic(fast_bootstrap):
    ev = smv.prepare(synthetic(signal=0.2))
    a = smv.robustness_table(ev, "2025Q2", "x")
    assert smv.robustness_table(ev, "2025Q2", "x") == a
    shuffled = smv.robustness_table(ev.sample(frac=1.0, random_state=9), "2025Q2", "x")
    for x, y in zip(a, shuffled):
        assert (x["variant"], x["n"]) == (y["variant"], y["n"])
        assert x["rho"] == pytest.approx(y["rho"], abs=1e-9)
        assert x["median_ret10"] == pytest.approx(y["median_ret10"], abs=1e-9)
    variants = smv.robustness_variants(ev, "2025Q2")
    counts = ev["symbol"].value_counts()
    top = sorted(counts.index, key=lambda s: (-counts[s], s))[:5]
    assert not variants["drop_top5_symbols"]["symbol"].isin(top).any()
    assert (variants["leave_out_trend_pullback"]["strategy"] != "trend_pullback").all()
    assert (variants["drop_strongest_market_quarter"]["quarter"] != "2025Q2").all()
    dedup = variants["dedup_first_signal_10_sessions"]
    for (_, _), g in dedup.groupby(["symbol", "strategy"]):
        assert (np.diff(np.sort(g["bar_index"].to_numpy())) >= smv.DEDUP_SESSIONS).all()


# --------------------------------------------------------------------------
# PK-21 23
# --------------------------------------------------------------------------

def test_23_filter_simulation_runs_through_the_real_pk21_simulator(golden_df, monkeypatch):
    frames = _golden_frames()
    calls = []
    real = backtest.simulate

    def spy(module, frames_, params, cfg, start, end):
        calls.append((getattr(module, "threshold", None), start, end,
                      max(f.dates[-1] for f in frames_.values())))
        return real(module, frames_, params, cfg, start, end)

    monkeypatch.setattr(smv, "simulate", spy)
    ev = smv.prepare(smv.primary(golden_df))
    rows = smv.filter_simulation(frames, {}, ev, "2025-09-01", "2026-06-30", "x",
                                 strategies=("momentum_breakout",))
    assert [c[0] for c in calls] == [None, 60.0, 75.0]
    assert all(c[3] <= "2026-06-30" for c in calls)      # frames cut at the period end first
    assert [r["scenario"] for r in rows] == ["all", "score>=60", "score>=75"]
    assert rows[0]["trades"] >= rows[1]["trades"] >= rows[2]["trades"]


def test_23b_zero_threshold_wrapper_reproduces_plain_pk21_and_scores_point_in_time(golden_df):
    frames = smv.sim_frames(_golden_frames(), "2026-08-31")
    module = STRATEGIES["momentum_breakout"]
    params = dict(module.DEFAULT_PARAMS)
    cfg = backtest.BacktestConfig.from_overrides()
    plain = backtest.simulate(module, frames, params, cfg, "2025-09-01", "2026-08-31")
    wrapper = smv.ScoreFilteredStrategy(module, 0.0)
    wrapped = backtest.simulate(wrapper, frames, params, cfg, "2025-09-01", "2026-08-31")
    assert wrapped["trades"] == plain["trades"]
    assert wrapped["skipped_signals"] == plain["skipped_signals"]
    assert wrapper.scored
    dataset = {(r.symbol, r.signal_date): r.smart_money_score
               for r in golden_df[golden_df["strategy"] == "momentum_breakout"].itertuples()}
    for symbol, day, value in wrapper.scored:
        assert value == pytest.approx(dataset[(symbol, day)])
    # A filter above every score removes every trade — the simulator really consults it.
    none = backtest.simulate(smv.ScoreFilteredStrategy(module, 101.0), frames, params, cfg,
                             "2025-09-01", "2026-08-31")
    assert none["trades"] == [] and plain["trades"]


def test_23c_unconstrained_trade_outcomes_come_from_pk21_trades(golden_rows):
    traded = [r for r in golden_rows if r[f"{OUT}trade_traded"]]
    assert traded
    for r in traded:
        assert r[f"{OUT}trade_won"] == (r[f"{OUT}trade_net_pnl_pct"] > 0)
        assert r[f"{OUT}trade_mae_pct"] <= r[f"{OUT}trade_mfe_pct"]
        assert isinstance(r[f"{OUT}trade_net_r"], float)
    cfg = smv.unconstrained_config()
    assert cfg.max_open_positions >= 10 ** 6 and cfg.costs_enabled and cfg.entry_model == "next_open"
    assert cfg.entry_slippage_bps == 5.0 and cfg.exit_slippage_bps == 5.0


# --------------------------------------------------------------------------
# ISOLATION 24-26
# --------------------------------------------------------------------------

def _frozen():
    manifest = smv.read_manifest(ROOT / "research" / "pk25")
    if not manifest.get("model_freeze"):
        pytest.fail("research/pk25/validation_manifest.json has no model freeze")
    return manifest


def _check(files_key: str, subset):
    manifest = _frozen()
    if manifest.get("superseded_by"):
        return
    frozen = manifest["model_freeze"][files_key]
    for path in subset:
        assert smv.normalised_sha256(ROOT / path) == frozen[path], (
            f"{path} changed after the PK-25 freeze; re-validate or mark the manifest superseded_by")


def test_24_score_and_feature_code_are_the_frozen_v1_0():
    manifest = _frozen()
    frozen = manifest["model_freeze"]
    assert frozen["statement"] == smv.FREEZE_STATEMENT
    if frozen["smart_money_score_version"] == sms.SMART_MONEY_SCORE_VERSION and not manifest.get("superseded_by"):
        _check("model_file_sha256", smv.MODEL_FILES)
        assert frozen["weights"] == dict(sms.WEIGHTS)
        assert frozen["volume_feature_version"] == vi.FEATURE_VERSION


def test_25_strategy_code_is_unchanged():
    _check("production_file_sha256", [f for f in smv.PRODUCTION_FILES if "strateg" in f])


def test_26_risk_execution_and_backtest_are_unchanged():
    _check("production_file_sha256", [f for f in smv.PRODUCTION_FILES if "strateg" not in f])


def test_normalised_hash_ignores_line_endings(tmp_path):
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_bytes(b"x = 1\r\ny = 2\r\n")
    b.write_bytes(b"x = 1\ny = 2\n")
    assert smv.normalised_sha256(a) == smv.normalised_sha256(b)


# --------------------------------------------------------------------------
# Protocol and classification
# --------------------------------------------------------------------------

def _protocol_dir(tmp_path):
    out = tmp_path / "pk25"
    out.mkdir(parents=True)
    dataset = tmp_path / "dataset.csv"
    df = synthetic(80)
    df.to_csv(dataset, index=False)
    pre = {}
    for name in [f"{t}_pre_holdout.csv" for t in list(smv.OUTPUT_TABLES) + ["extreme_bar_members"]] + \
                [smv.PRE_HOLDOUT_RESULTS]:
        (out / name).write_text("x\n", encoding="utf-8")
        pre[name] = smv.file_sha256(out / name)
    manifest = {
        "model_freeze": {**smv.freeze_definitions(), "frozen_at": "t"},
        "design_freeze": {"dataset_sha256": smv.file_sha256(dataset), "splits": smv.split_dates(df),
                          "design": smv._jsonable(smv.design()),
                          "analysis_code_sha256": smv.code_hashes(smv.ANALYSIS_FILES),
                          "pre_holdout_output_sha256": pre},
    }
    smv.write_manifest(out, manifest)
    return out, dataset


def test_protocol_design_freeze_detects_every_change(tmp_path, monkeypatch):
    out, dataset = _protocol_dir(tmp_path)
    manifest = smv.read_manifest(out)
    assert smv.check_design_frozen(manifest, dataset, out) == []
    (out / "buckets_pre_holdout.csv").write_text("changed\n", encoding="utf-8")
    assert any("buckets_pre_holdout" in p for p in smv.check_design_frozen(manifest, dataset, out))
    out2, dataset2 = _protocol_dir(tmp_path / "second")
    dataset2.write_text(dataset2.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert any("dataset" in p for p in smv.check_design_frozen(smv.read_manifest(out2), dataset2, out2))
    out3, dataset3 = _protocol_dir(tmp_path / "third")
    monkeypatch.setattr(smv, "BUCKETS", (("0-49", 0.0, 50.0), ("50-100", 50.0, 100.0001)))
    assert any("design" in p for p in smv.check_design_frozen(smv.read_manifest(out3), dataset3, out3))


def test_protocol_holdout_runs_once_and_refuses_on_mismatch(tmp_path):
    out, dataset = _protocol_dir(tmp_path)
    (out / smv.HOLDOUT_RESULTS).write_text("{}", encoding="utf-8")
    args = ["analyze", "--stage", "holdout", "--out", str(out), "--dataset", str(dataset),
            "--db", str(tmp_path / "no-such.db")]
    with pytest.raises(smv.ProtocolError, match="evaluated once"):
        smv.main(args)
    manifest = smv.read_manifest(out)
    manifest["design_freeze"]["dataset_sha256"] = "0" * 64
    smv.write_manifest(out, manifest)
    with pytest.raises(smv.ProtocolError, match="dataset"):
        smv.main(args)
    manifest["model_freeze"]["model_file_sha256"]["backend/smart_money_score.py"] = "0" * 64
    smv.write_manifest(out, manifest)
    with pytest.raises(smv.ProtocolError, match="smart_money_score"):
        smv.main(args)


def test_protocol_pre_holdout_cannot_be_regenerated_after_the_freeze(tmp_path):
    out, dataset = _protocol_dir(tmp_path)
    with pytest.raises(smv.ProtocolError, match="already frozen"):
        smv.main(["analyze", "--stage", "pre_holdout", "--out", str(out), "--dataset", str(dataset),
                  "--db", str(tmp_path / "no-such.db")])
    with pytest.raises(smv.ProtocolError, match="already frozen"):
        smv.main(["freeze", "--out", str(out), "--dataset", str(dataset)])


BASE = {"n10": 150, "rho10": 0.0, "rho10_lo": -0.1, "rho10_hi": 0.1, "h2_diff": 0.0, "dic": 0.0,
        "dic_lo": -0.05, "dic_hi": 0.05, "wf_mean_dic": 0.0, "wf_share_dic_pos": 0.5, "wf_share_rho_pos": 0.5,
        "wf_share_rho_neg": 0.5, "wf_share_h2_pos": 0.5, "strat_pos": 1, "strat_neg": 1,
        "filter60_dd_ratio": 1.0, "filter60_expectancy_delta": 0.0}


@pytest.mark.parametrize("overrides, label", [
    ({"n10": 40, "rho10": 0.5, "rho10_lo": 0.3}, "INCONCLUSIVE"),
    ({"rho10": -0.2, "rho10_lo": -0.35, "rho10_hi": -0.05, "h2_diff": -1.0, "strat_neg": 2}, "CONTRARIAN"),
    ({"rho10": 0.2, "rho10_lo": 0.05, "rho10_hi": 0.35, "h2_diff": 1.0, "dic": 0.05, "wf_mean_dic": 0.02,
      "strat_pos": 2, "wf_share_rho_pos": 0.7}, "PROMISING"),
    ({}, "NO_EDGE"),
    ({"rho10": 0.08, "rho10_lo": -0.1, "rho10_hi": 0.26, "wf_share_rho_pos": 0.7}, "LIMITED"),
    ({"rho10": 0.02, "rho10_lo": -0.3, "rho10_hi": 0.34}, "INCONCLUSIVE"),
    ({"rho10": -0.2, "rho10_lo": -0.35, "rho10_hi": -0.05, "h2_diff": 1.0, "strat_neg": 2}, "INCONCLUSIVE"),
])
def test_classification_follows_the_frozen_rules_in_order(overrides, label):
    assert smv.classify({**BASE, **overrides})["label"] == label


def test_classification_promising_requires_the_practical_filter_check():
    promising = {**BASE, "rho10": 0.2, "rho10_lo": 0.05, "rho10_hi": 0.35, "h2_diff": 1.0, "dic": 0.05,
                 "wf_mean_dic": 0.02, "strat_pos": 2, "wf_share_rho_pos": 0.7}
    assert smv.classify(promising)["label"] == "PROMISING"
    worse = smv.classify({**promising, "filter60_dd_ratio": 1.6})
    assert worse["label"] == "LIMITED" and not worse["checks"]["PROMISING"]["filter60_drawdown_ok"]
    assert set(smv.NEXT_STEPS) == {"PROMISING", "LIMITED", "NO_EDGE", "CONTRARIAN", "INCONCLUSIVE"}


def test_primary_hypotheses_are_declared_and_tiered():
    tiers = {h["id"]: h["tier"] for h in smv.HYPOTHESES}
    assert tiers == {"H1": "PRIMARY", "H2": "PRIMARY", "H3": "PRIMARY", "H4": "SECONDARY", "H5": "SECONDARY",
                     "O": "SECONDARY"}
    assert smv.PRIMARY_HORIZON == 10 and smv.BOOTSTRAP_SEED == 25
