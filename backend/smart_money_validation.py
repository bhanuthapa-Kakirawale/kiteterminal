"""PK-25: out-of-sample validation of Smart Money Accumulation Score v1.0.

RESEARCH ONLY. Nothing live imports this module (tested). It never changes the
score, a strategy, the scanner, ranking, risk, sizing, execution or the PK-21
backtester — it builds research adapters AROUND them:

* the dataset replays each strategy's own `evaluate_entry` on the frame cut
  at the signal bar (exactly as volume_research / the backtester do) and
  scores the PK-23 features of that same cut frame with the frozen
  `smart_money_score.score`;
* outcomes are measured afterwards, in `outcome_*` columns only, and can
  never enter a feature matrix (`feature_matrix` refuses them);
* strategy outcomes and the practical filter test run through the unmodified
  `backtest.simulate` (the PK-21 execution and cost model).

Protocol (data-snooping guard)
------------------------------
    freeze-model  -> the score/feature definitions and code hashes, first
    build         -> the point-in-time signal dataset (run twice: identical)
    analyze --stage pre_holdout   (development + validation only; twice)
    freeze        -> splits, hypotheses, buckets, criteria, dataset and code
                     hashes, pre-holdout output hashes
    analyze --stage holdout       (ONCE; refuses on any hash mismatch or if
                                   holdout results already exist)
    verify        -> recompute the holdout stage elsewhere and compare

CLI:  python -m backend.smart_money_validation <command> ...
"""

import argparse
import csv
import hashlib
import json
import math
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backend import smart_money_score as sms
from backend import smart_money_validation_stats as st
from backend import volume_intelligence as vi
from backend import volume_research as vr
from backend.backtest import BacktestConfig, SymbolFrame, simulate
from backend.backtest_analytics import compute_portfolio_metrics
from backend.strategies import STRATEGIES

ROOT = Path(__file__).resolve().parent.parent
EXPERIMENT = "PK-25 smart money score v1.0 out-of-sample validation"
FREEZE_STATEMENT = "Score v1.0 was frozen before out-of-sample evaluation."

# ---------------------------------------------------------------- design ---
# Every constant below is part of the frozen design (written to the manifest
# by `freeze`). None of them may change after the holdout is evaluated
# without starting a new experiment.

DATASET_START = "2024-09-01"          # parity with the PK-23/PK-24 exports
HORIZONS = (5, 10, 20)
PRIMARY_HORIZON = 10
PURGE_SESSIONS = 20                   # the longest outcome window
OUTCOME = vr.OUTCOME_PREFIX

# The PK-24 packet buckets, [lower, upper) on the 0.1-rounded score; the top
# bucket includes 100. Frozen from the PK-24 report; never redefined here.
BUCKETS = (
    ("0-24", 0.0, 25.0),
    ("25-44", 25.0, 45.0),
    ("45-59", 45.0, 60.0),
    ("60-74", 60.0, 75.0),
    ("75-100", 75.0, 100.0001),
)
TOP_BUCKET, NEUTRAL_BUCKET, MODERATE_BUCKET = "75-100", "45-59", "60-74"
SMALL_BUCKET_N = 10                   # below this a bucket is flagged, not interpreted
PF_MIN_N = 20                         # profit factor only with this many trades

SPLIT_QUANTILES = (0.45, 0.70)        # validation start, holdout start (of signal dates)
WALK_FORWARD_MIN_TRAIN = 80
BOOTSTRAP_REPS = 2000
BOOTSTRAP_SEED = 25
TRIM = 0.10
WINSOR = (0.01, 0.99)

MARKET_PROXY = "NIFTYBEES"            # Nifty-50 ETF: the only index proxy stored
MARKET_SMA = 200
MARKET_VOL_WINDOW = 20
VOL_REGIME_MIN_HISTORY = 120
LIQUIDITY_CUTS_INR = (5e7, 5e8)       # Rs 5 crore / Rs 50 crore per day (prior-20 average)

EXTREME_BAR = {"price_change_pct_min": 8.0, "rvol_20_min": 3.0, "close_location_min": 0.75}
DEDUP_SESSIONS = 10
TOP_SYMBOLS_DROPPED = 5
MIN_STRATEGY_N = 15

FILTER_THRESHOLDS = (None, 60.0, 75.0)   # all signals, and the frozen PK-24 band edges
UNCONSTRAINED_CAPITAL = 1e12
UNCONSTRAINED_NOTIONAL = 1e5          # ~Rs 1 lakh per position (capital-based size binds)

LOGISTIC_RIDGE = 1e-3

STRATEGY_ORDER = ("momentum_breakout", "trend_pullback", "rsi_mean_reversion")
CONTINUOUS_MODEL_FEATURES = ("log_rvol_20", "close_location", "obv_slope_20", "net_accumulation_20",
                             "smart_money_score")
MODELS = {   # strategy dummies are always included
    "M0": (),
    "M1": ("log_rvol_20",),
    "M2": ("log_rvol_20", "close_location", "obv_slope_20", "net_accumulation_20"),
    "M3": ("smart_money_score",),
    "M4": ("log_rvol_20", "smart_money_score"),
    "M5": ("log_rvol_20", "close_location", "obv_slope_20", "net_accumulation_20", "smart_money_score"),
}
MODEL_PAIRS = (("M4", "M1"), ("M5", "M2"), ("M3", "M0"))

HYPOTHESES = (
    {"id": "H1", "tier": "PRIMARY",
     "statement": "Spearman rho(smart_money_score, 10-session forward return) > 0 on the holdout primary set."},
    {"id": "H2", "tier": "PRIMARY",
     "statement": "Median 10-session forward return of the 75-100 bucket exceeds that of the 45-59 bucket."},
    {"id": "H3", "tier": "PRIMARY",
     "statement": "Out-of-sample Spearman IC of M4 (strategy + RVOL20 + score) exceeds M1 (strategy + RVOL20), "
                  "on the holdout and as the mean walk-forward delta."},
    {"id": "H4", "tier": "SECONDARY",
     "statement": "rho(score, MFE_10) > 0 and rho(score, MAE_10) > 0 (a shallower adverse excursion)."},
    {"id": "H5", "tier": "SECONDARY",
     "statement": "rho(score, 10-session return) is positive in >= 2 strategies with N >= 15 and in >= 60% of "
                  "walk-forward folds."},
    {"id": "O", "tier": "SECONDARY",
     "statement": "Contrarian ordering check: 75-100 versus 60-74 versus 45-59 on forward returns, MFE and MAE."},
)
EXPLORATORY = "Every other horizon, trade R, regime, cohort, quintile and robustness result is EXPLORATORY."

DECISION_CRITERIA = {
    "min_holdout_n": 60,
    "rho_effect": 0.10,
    "delta_ic_min": 0.03,
    "wf_consistency": 0.60,
    "strategy_min_n": MIN_STRATEGY_N,
    "strategy_min_count": 2,
    "no_edge_max_ci_half_width": 0.15,
    "max_drawdown_ratio": 1.25,
    "order": ["INCONCLUSIVE_SAMPLE", "CONTRARIAN", "PROMISING", "NO_EDGE", "LIMITED", "INCONCLUSIVE"],
}
NEXT_STEPS = {
    "PROMISING": "PK-26 may expose ranking/scanner views based on the score (a separate approval is still "
                 "required for any strategy use).",
    "LIMITED": "PK-26 may show the score descriptively but must not prioritise signals strongly by it.",
    "NO_EDGE": "Do not build a scanner ranking around the score.",
    "CONTRARIAN": "Do not invert the score automatically; recommend score redesign / v1.1 research.",
    "INCONCLUSIVE": "Collect more forward (post-freeze) data before any use of the score.",
}

# Files whose content the validation is bound to (hashed with CRLF -> LF).
MODEL_FILES = ("backend/smart_money_score.py", "backend/volume_intelligence.py")
PRODUCTION_FILES = (
    "backend/volume_research.py", "backend/backtest.py", "backend/backtest_costs.py",
    "backend/backtest_analytics.py", "backend/risk_engine.py", "backend/execution.py",
    "backend/strategy_engine.py", "backend/strategies/__init__.py",
    "backend/strategies/momentum_breakout.py", "backend/strategies/trend_pullback.py",
    "backend/strategies/rsi_mean_reversion.py",
)
ANALYSIS_FILES = ("backend/smart_money_validation.py", "backend/smart_money_validation_stats.py")

MANIFEST = "validation_manifest.json"
HOLDOUT_RESULTS = "holdout_results.json"
PRE_HOLDOUT_RESULTS = "pre_holdout_results.json"
SYMBOL_ATTR = "pk25_symbol"


class ProtocolError(RuntimeError):
    """The validation protocol was violated (hash mismatch, repeated holdout)."""


# --------------------------------------------------------------------------
# Hashing / freezing
# --------------------------------------------------------------------------

def normalised_sha256(path: Path) -> str:
    """sha256 of a text file with CRLF normalised to LF (core.autocrlf)."""
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_hashes(files, root: Path = ROOT) -> dict:
    return {f: normalised_sha256(root / f) for f in files}


def _git_head(root: Path = ROOT) -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def freeze_definitions(root: Path = ROOT) -> dict:
    """PK-25A: the frozen model definition, taken from the modules themselves."""
    return {
        "statement": FREEZE_STATEMENT,
        "smart_money_score_version": sms.SMART_MONEY_SCORE_VERSION,
        "required_feature_version": sms.REQUIRED_FEATURE_VERSION,
        "volume_feature_version": vi.FEATURE_VERSION,
        "weights": dict(sms.WEIGHTS),
        "ramps": {k: list(v) for k, v in sms.RAMPS.items()},
        "bands": [list(b) for b in sms.BANDS],
        "model_file_sha256": code_hashes(MODEL_FILES, root),
        "production_file_sha256": code_hashes(PRODUCTION_FILES, root),
        "git_head": _git_head(root),
    }


def design() -> dict:
    """PK-25P: every analysis choice, frozen before the holdout."""
    return {
        "dataset_start": DATASET_START, "horizons": list(HORIZONS), "primary_horizon": PRIMARY_HORIZON,
        "purge_sessions": PURGE_SESSIONS, "buckets": [list(b) for b in BUCKETS],
        "small_bucket_n": SMALL_BUCKET_N, "pf_min_n": PF_MIN_N,
        "split_quantiles": list(SPLIT_QUANTILES), "walk_forward_min_train": WALK_FORWARD_MIN_TRAIN,
        "bootstrap": {"reps": BOOTSTRAP_REPS, "seed": BOOTSTRAP_SEED, "cluster": "ISO week of signal_date"},
        "trim": TRIM, "winsor": list(WINSOR), "market_proxy": MARKET_PROXY,
        "vol_regime_min_history": VOL_REGIME_MIN_HISTORY, "liquidity_cuts_inr": list(LIQUIDITY_CUTS_INR),
        "extreme_bar": EXTREME_BAR, "dedup_sessions": DEDUP_SESSIONS,
        "top_symbols_dropped": TOP_SYMBOLS_DROPPED, "filter_thresholds": list(FILTER_THRESHOLDS),
        "unconstrained": {"capital": UNCONSTRAINED_CAPITAL, "notional": UNCONSTRAINED_NOTIONAL},
        "models": {k: list(v) for k, v in MODELS.items()}, "model_pairs": [list(p) for p in MODEL_PAIRS],
        "logistic_ridge": LOGISTIC_RIDGE, "primary_set": "eligible signals (score not null, not a special-"
                                                         "session bar), pooled",
        "hypotheses": list(HYPOTHESES), "exploratory": EXPLORATORY,
        "decision_criteria": DECISION_CRITERIA, "next_steps": NEXT_STEPS,
    }


# --------------------------------------------------------------------------
# PK-25B: point-in-time dataset
# --------------------------------------------------------------------------

def market_outcomes(df: pd.DataFrame, i: int) -> dict:
    """RESEARCH LABELS for a signal on bar i, measured from the next session's
    open (the PK-21 next-open entry): forward close returns and the maximum
    favourable / adverse excursion over 5/10/20 sessions, in %. Only this
    function and the trade outcomes read rows after i."""
    out = {}
    for h in HORIZONS:
        out[f"{OUTCOME}fwd_ret_{h}_pct"] = None
        out[f"{OUTCOME}mfe_{h}_pct"] = None
        out[f"{OUTCOME}mae_{h}_pct"] = None
    out[f"{OUTCOME}window_end"] = df["date"].iloc[i + PURGE_SESSIONS] if i + PURGE_SESSIONS < len(df) else None
    if i + 1 >= len(df):
        return out
    entry = float(df["open"].iloc[i + 1])
    if entry <= 0:
        return out
    for h in HORIZONS:
        if i + h < len(df):
            span = df.iloc[i + 1: i + h + 1]
            out[f"{OUTCOME}fwd_ret_{h}_pct"] = round((float(df["close"].iloc[i + h]) / entry - 1) * 100, 4)
            out[f"{OUTCOME}mfe_{h}_pct"] = round((float(span["high"].max()) / entry - 1) * 100, 4)
            out[f"{OUTCOME}mae_{h}_pct"] = round((float(span["low"].min()) / entry - 1) * 100, 4)
    return out


def _normal_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.reset_index(drop=True).copy()
    out["date"] = out["date"].astype(str).str[:10]
    return out


def signal_rows(module, frames: dict, params: dict, start: str, end: str, *, tokens: dict | None = None,
                verified_gaps: dict | None = None) -> list[dict]:
    """One row per bar in [start, end] on which module.evaluate_entry passes.
    Mirrors volume_research.signal_feature_rows (parity is tested) and adds
    the data-quality codes. Features and score come from the frame cut at the
    signal bar; `_bar_index` is kept (private) so outcomes can be attached
    afterwards."""
    rows = []
    for symbol in sorted(frames):
        df = _normal_frame(frames[symbol])
        for i in range(len(df)):
            day = df["date"].iloc[i]
            if day < start or day > end:
                continue
            upto = df.iloc[: i + 1]
            if not module.evaluate_entry(upto, params)["passed"]:
                continue
            features = vi.compute_at(upto, symbol=symbol, instrument_token=(tokens or {}).get(symbol),
                                     verified_gaps=(verified_gaps or {}).get(symbol, ()),
                                     strategy=module.STRATEGY_NAME, params=params)
            comparison = features.get("strategy_comparison") or {}
            rows.append({
                "strategy": module.STRATEGY_NAME, "symbol": symbol,
                "instrument_token": (tokens or {}).get(symbol), "signal_date": day, "bar_index": i,
                "feature_version": features["feature_version"],
                "data_quality": features["data_quality"]["status"],
                "dq_codes": ";".join(features["data_quality"]["codes"]),
                **vr.feature_record(features),
                "legacy_volume_pass": comparison.get("legacy_pass"),
                **vr.score_record(features),
            })
    return rows


def market_context(proxy: pd.DataFrame | None) -> dict:
    """Point-in-time market regime per date from the proxy frame: close vs
    SMA-200, 20-day realised volatility, and its tercile within the proxy's
    OWN PRIOR history (expanding, strictly earlier values only)."""
    if proxy is None or not len(proxy):
        return {}
    df = _normal_frame(proxy)
    close = df["close"].astype(float)
    sma = close.rolling(MARKET_SMA).mean()
    vol = np.log(close).diff().rolling(MARKET_VOL_WINDOW).std(ddof=1) * math.sqrt(252) * 100
    out, history = {}, []
    for i, day in enumerate(df["date"]):
        v = vol.iloc[i]
        regime = "unknown"
        if not math.isnan(v) and len(history) >= VOL_REGIME_MIN_HISTORY:
            lo, hi = np.quantile(history, [1 / 3, 2 / 3])
            regime = "low" if v <= lo else ("mid" if v <= hi else "high")
        out[day] = {
            "mkt_above_200dma": None if math.isnan(sma.iloc[i]) else bool(close.iloc[i] > sma.iloc[i]),
            "mkt_vol_20": None if math.isnan(v) else round(float(v), 4),
            "mkt_vol_regime": regime,
        }
        if not math.isnan(v):
            history.append(float(v))
    return out


def _context_for(context: dict, day: str) -> dict:
    if day in context:
        return context[day]
    earlier = [d for d in context if d <= day]
    return context[max(earlier)] if earlier else {"mkt_above_200dma": None, "mkt_vol_20": None,
                                                  "mkt_vol_regime": "unknown"}


def liquidity_bucket(avg_traded_value) -> str:
    if avg_traded_value is None or (isinstance(avg_traded_value, float) and math.isnan(avg_traded_value)):
        return "unknown"
    lo, hi = LIQUIDITY_CUTS_INR
    return "thin" if avg_traded_value < lo else ("mid" if avg_traded_value < hi else "large")


def quarter_of(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def exclusion_reason(row: dict) -> str:
    if row.get("smart_money_score") is None:
        return "score_null"
    if "SPECIAL_SESSION_BAR" in (row.get("dq_codes") or "").split(";"):
        return "special_session_bar"
    return ""


def unconstrained_config() -> BacktestConfig:
    """PK-21 execution (next open, 5/5 bps, costs, stop-first) without capital
    competition: a huge pot where the capital-based size binds at about
    UNCONSTRAINED_NOTIONAL per position, so every tradeable signal trades."""
    return BacktestConfig.from_overrides(
        initial_capital=UNCONSTRAINED_CAPITAL,
        capital_per_trade_pct=UNCONSTRAINED_NOTIONAL / UNCONSTRAINED_CAPITAL * 100,
        max_open_positions=10 ** 6,
    )


def sim_frames(frames: dict, end: str, verified_gaps: dict | None = None) -> dict:
    """SymbolFrames cut at `end` BEFORE any signal exists; the symbol rides
    along in DataFrame.attrs for the research filter wrapper."""
    out = {}
    for symbol, df in frames.items():
        cut = _normal_frame(df)
        cut = cut[cut["date"] <= end].reset_index(drop=True)
        cut.attrs[SYMBOL_ATTR] = symbol
        out[symbol] = SymbolFrame(symbol, cut)
    return out


def trade_outcomes(trades: list[dict], frames: dict, prefix: str) -> dict:
    """{(symbol, signal_date): outcome columns} for simulated trades."""
    out = {}
    for t in trades:
        df = _normal_frame(frames[t["symbol"]])
        span = df[(df["date"] >= t["entry_date"]) & (df["date"] <= t["exit_date"])]
        entry = float(t["entry_price"])
        notional = entry * t["quantity"]
        out[(t["symbol"], t["signal_date"])] = {
            f"{prefix}traded": True,
            f"{prefix}net_r": t["net_r_multiple"],
            f"{prefix}net_pnl_pct": round(t["net_pnl"] / notional * 100, 4) if notional > 0 else None,
            f"{prefix}won": t["net_pnl"] > 0,
            f"{prefix}exit_reason": t["exit_reason"],
            f"{prefix}bars_held": t["bars_held"],
            f"{prefix}forced_close": t["forced_close"],
            f"{prefix}mfe_pct": round((float(span["high"].max()) / entry - 1) * 100, 4) if len(span) else None,
            f"{prefix}mae_pct": round((float(span["low"].min()) / entry - 1) * 100, 4) if len(span) else None,
        }
    return out


TRADE_KEYS = ("traded", "net_r", "net_pnl_pct", "won", "exit_reason", "bars_held", "forced_close",
              "mfe_pct", "mae_pct")


def build_dataset(frames: dict, tokens: dict, verified: dict, *, start: str = DATASET_START,
                  end: str | None = None, strategies=STRATEGY_ORDER, market_proxy: str = MARKET_PROXY,
                  with_trades: bool = True) -> list[dict]:
    """PK-25B. The point-in-time signal dataset, sorted (signal_date,
    strategy, symbol). Feature/context/score columns first, outcomes last."""
    end = end or max(_normal_frame(df)["date"].iloc[-1] for df in frames.values())
    context = market_context(frames.get(market_proxy))
    normal = {s: _normal_frame(df) for s, df in frames.items()}
    rows = []
    for name in strategies:
        module = STRATEGIES[name]
        params = dict(module.DEFAULT_PARAMS)
        found = signal_rows(module, normal, params, start, end, tokens=tokens, verified_gaps=verified)
        # ---- everything above is point-in-time; outcomes start here ----
        for row in found:
            row.update(market_outcomes(normal[row["symbol"]], row["bar_index"]))
        if with_trades:
            frames_end = sim_frames(normal, end, verified)
            unconstrained = simulate(module, frames_end, params, unconstrained_config(), start, end)
            by_key = trade_outcomes(unconstrained["trades"], normal, f"{OUTCOME}trade_")
            portfolio = simulate(module, frames_end, params, BacktestConfig.from_overrides(), start, end)
            pf = {(t["symbol"], t["signal_date"]): t for t in portfolio["trades"]}
            for row in found:
                key = (row["symbol"], row["signal_date"])
                outcome = by_key.get(key) or {f"{OUTCOME}trade_{k}": (False if k == "traded" else None)
                                              for k in TRADE_KEYS}
                row.update(outcome)
                row[f"{OUTCOME}pf_traded"] = key in pf
                row[f"{OUTCOME}pf_net_r"] = pf[key]["net_r_multiple"] if key in pf else None
        rows.extend(found)
    for row in rows:
        ctx = _context_for(context, row["signal_date"])
        row.update(ctx)
        row["liquidity_bucket"] = liquidity_bucket(row.get("avg_traded_value_20"))
        row["quarter"] = quarter_of(row["signal_date"])
        row["special_session_baseline"] = "SPECIAL_SESSION_IN_BASELINE" in row["dq_codes"].split(";")
        row["excl_reason"] = exclusion_reason(row)
    rows.sort(key=lambda r: (r["signal_date"], r["strategy"], r["symbol"]))
    return rows


def dataset_columns(rows: list[dict]) -> list[str]:
    keys = list(rows[0]) if rows else []
    first = [k for k in keys if not k.startswith(OUTCOME)]
    return first + [k for k in keys if k.startswith(OUTCOME)]


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        return repr(round(value, 6))
    return value


def write_rows(rows: list[dict], path: Path, columns: list[str] | None = None) -> None:
    columns = columns or (dataset_columns(rows) if rows else [])
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_cell(row.get(c)) for c in columns])


STRING_COLUMNS = {"strategy", "symbol", "signal_date", "data_quality", "dq_codes", "obv_divergence",
                  "smart_money_score_version", "smart_money_confidence", "mkt_vol_regime", "liquidity_bucket",
                  "quarter", "excl_reason", f"{OUTCOME}trade_exit_reason", f"{OUTCOME}window_end"}


def load_dataset(path: Path) -> pd.DataFrame:
    """The dataset CSV with numeric columns as float (booleans as 1.0/0.0)."""
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    return typed(raw)


def typed(raw: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=raw.index)
    for column in raw.columns:
        if column in STRING_COLUMNS:
            out[column] = raw[column].astype(str)
        else:
            mapped = raw[column].replace({"True": "1", "False": "0", "": np.nan})
            out[column] = pd.to_numeric(mapped, errors="coerce").astype(float)
    return out


# --------------------------------------------------------------------------
# Feature matrix (outcomes can never enter it)
# --------------------------------------------------------------------------

def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Derived research features on a COPY (no outcome is used)."""
    out = df.copy()
    rvol = out["rvol_20"].where(out["rvol_20"] > 0)
    out["log_rvol_20"] = np.log(rvol)
    out["acc_minus_dist_10"] = out["accumulation_like_10"] - out["distribution_like_10"]
    return out


def feature_matrix(df: pd.DataFrame, columns) -> pd.DataFrame:
    columns = list(columns)
    leaked = [c for c in columns if str(c).startswith(OUTCOME)]
    if leaked:
        raise ValueError(f"outcome columns can never be features: {leaked}")
    matrix = df[columns].astype(float)
    assert not any(str(c).startswith(OUTCOME) for c in matrix.columns)
    return matrix


# --------------------------------------------------------------------------
# PK-25C: chronological splits
# --------------------------------------------------------------------------

def _nearest_month_start(day: str) -> str:
    d = date.fromisoformat(day)
    this = d.replace(day=1)
    following = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return (this if (d - this) <= (following - d) else following).isoformat()


def split_dates(df: pd.DataFrame) -> dict:
    """Split boundaries from ELIGIBLE SIGNAL DATES ONLY — no outcome is read.
    Each boundary is the first day of the month nearest the quantile."""
    dates = sorted(df.loc[df["excl_reason"] == "", "signal_date"].tolist())
    if len(dates) < 3:
        raise ValueError("too few eligible signals to split")
    picks = [dates[int(round(q * (len(dates) - 1)))] for q in SPLIT_QUANTILES]
    validation_start, holdout_start = (_nearest_month_start(p) for p in picks)
    if not dates[0] < validation_start < holdout_start <= dates[-1]:
        raise ValueError(f"degenerate split {validation_start} / {holdout_start}")
    return {"validation_start": validation_start, "holdout_start": holdout_start,
            "rule": f"first day of the month nearest the {SPLIT_QUANTILES} quantiles of eligible signal dates"}


def assign_segments(df: pd.DataFrame, splits: dict) -> pd.DataFrame:
    """segment in {development, validation, holdout}; `purged` marks a
    development/validation row whose outcome window reaches the next segment."""
    out = df.copy()
    v, h = splits["validation_start"], splits["holdout_start"]
    out["segment"] = np.where(out["signal_date"] < v, "development",
                              np.where(out["signal_date"] < h, "validation", "holdout"))
    window = out[f"{OUTCOME}window_end"].replace("", "9999-12-31")
    out["purged"] = ((out["segment"] == "development") & (window >= v)) | \
                    ((out["segment"] == "validation") & (window >= h))
    return out


def primary(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["excl_reason"] == ""]


def pre_holdout_view(df: pd.DataFrame, splits: dict) -> pd.DataFrame:
    """The ONLY accessor of stage 1: development + validation, purged rows
    removed, holdout rows dropped entirely (their outcomes are never read)."""
    seg = assign_segments(df, splits)
    return seg[(seg["segment"] != "holdout") & ~seg["purged"]]


def holdout_view(df: pd.DataFrame, splits: dict) -> pd.DataFrame:
    seg = assign_segments(df, splits)
    return seg[seg["segment"] == "holdout"]


def quarters(df: pd.DataFrame) -> list[tuple[str, str, str]]:
    """(label, first day, first day of next quarter) for every quarter with data."""
    out = []
    for label in sorted(set(df["signal_date"].map(quarter_of))):
        year, q = int(label[:4]), int(label[-1])
        start = date(year, 3 * (q - 1) + 1, 1)
        end = date(year + (q == 4), (3 * q) % 12 + 1, 1)
        out.append((label, start.isoformat(), end.isoformat()))
    return out


def walk_forward_folds(df: pd.DataFrame, limit: str | None = None) -> list[dict]:
    """Expanding-window folds: train = eligible rows before the test quarter
    whose outcome window also ended before it (purged); test = the quarter.
    With `limit`, only test rows before `limit` whose window ends before it
    (stage 1: nothing at or after the holdout start is touched)."""
    data = primary(df)
    window = data[f"{OUTCOME}window_end"].replace("", "9999-12-31")
    folds = []
    for label, start, end in quarters(data):
        if limit and start >= limit:
            continue
        test_end = min(end, limit) if limit else end
        train = data[(data["signal_date"] < start) & (window < start)]
        test = data[(data["signal_date"] >= start) & (data["signal_date"] < test_end)]
        if limit:
            test = test[window.loc[test.index] < limit]
        if train[f"{OUTCOME}fwd_ret_{PRIMARY_HORIZON}_pct"].notna().sum() < WALK_FORWARD_MIN_TRAIN:
            continue
        folds.append({"fold": label, "test_start": start, "test_end": test_end, "train": train, "test": test})
    return folds


# --------------------------------------------------------------------------
# Buckets and shared helpers
# --------------------------------------------------------------------------

def bucket_of(score) -> str | None:
    if score is None or (isinstance(score, float) and math.isnan(score)):
        return None
    for label, lo, hi in BUCKETS:
        if lo <= score < hi:
            return label
    return None


def week_clusters(df: pd.DataFrame) -> np.ndarray:
    return np.array([f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
                     for d in pd.to_datetime(df["signal_date"])])


def _col(df, name) -> np.ndarray:
    return df[name].to_numpy(dtype=float)


def _r(value, digits=6):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return round(float(value), digits)


def boot(df: pd.DataFrame, statistic, seed_offset: int = 0) -> np.ndarray:
    """Cluster bootstrap over ISO weeks with the frozen seed/reps."""
    return st.cluster_bootstrap(week_clusters(df), statistic, BOOTSTRAP_REPS, BOOTSTRAP_SEED + seed_offset)


def rho_with_ci(df: pd.DataFrame, x: str, y: str, seed_offset: int = 0) -> dict:
    xs, ys = _col(df, x), _col(df, y)
    rho = st.spearman(xs, ys)
    n = st.paired_n(xs, ys)
    reps = boot(df, lambda idx: st.spearman(xs[idx], ys[idx]), seed_offset) if n >= 3 else np.full(1, np.nan)
    lo, hi = st.interval(reps)
    return {"n": n, "rho": _r(rho), "ci_low": _r(lo), "ci_high": _r(hi),
            "p_boot_greater": _r(st.one_sided_p(reps, True)), "p_boot_less": _r(st.one_sided_p(reps, False)),
            "p_naive_two_sided": _r(st.fisher_p_two_sided(rho, n))}


def median_diff_with_ci(df: pd.DataFrame, y: str, bucket_a: str, bucket_b: str, seed_offset: int = 0) -> dict:
    ys = _col(df, y)
    buckets = df["bucket"].to_numpy()

    def stat(idx):
        a = ys[idx][buckets[idx] == bucket_a]
        b = ys[idx][buckets[idx] == bucket_b]
        ma, mb = st.median(a), st.median(b)
        return None if ma is None or mb is None else ma - mb

    point = stat(np.arange(len(df)))
    reps = boot(df, stat, seed_offset)
    lo, hi = st.interval(reps)
    n_a = int(np.sum((buckets == bucket_a) & ~np.isnan(ys)))
    n_b = int(np.sum((buckets == bucket_b) & ~np.isnan(ys)))
    return {"n_a": n_a, "n_b": n_b, "diff": _r(point), "ci_low": _r(lo), "ci_high": _r(hi),
            "p_boot_greater": _r(st.one_sided_p(reps, True)), "p_boot_less": _r(st.one_sided_p(reps, False))}


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    out = add_derived(df)
    out["bucket"] = [bucket_of(s) for s in out["smart_money_score"]]
    return out


# --------------------------------------------------------------------------
# PK-25D baselines, E buckets, F monotonicity
# --------------------------------------------------------------------------

def _zfit(train: pd.DataFrame, column: str) -> tuple[float, float]:
    values = train[column].dropna().to_numpy(dtype=float)
    if len(values) < 2 or values.std(ddof=1) == 0:
        return 0.0, 1.0
    return float(values.mean()), float(values.std(ddof=1))


def baseline_variables(ev: pd.DataFrame, train: pd.DataFrame) -> dict:
    """Ranking variables compared with the score. The strategy-only baseline
    and the z-statistics of the simple combination come from TRAIN rows."""
    y = f"{OUTCOME}fwd_ret_{PRIMARY_HORIZON}_pct"
    strategy_means = train.groupby("strategy")[y].mean().to_dict()
    combo = 0.0
    for column in ("log_rvol_20", "close_location", "obv_slope_20"):
        mu, sd = _zfit(train, column)
        combo = combo + (ev[column] - mu) / sd
    return {
        "B1_strategy_only": ev["strategy"].map(strategy_means).astype(float),
        "B2_rvol_20": ev["rvol_20"],
        "B3_volume_percentile_60": ev["volume_percentile_60"],
        "B4_close_location": ev["close_location"],
        "B5_acc_minus_dist_10": ev["acc_minus_dist_10"],
        "B6_simple_combo": combo,
        "SMS_smart_money_score": ev["smart_money_score"],
    }


def baselines(ev: pd.DataFrame, train: pd.DataFrame, label: str) -> list[dict]:
    rows = []
    frame = ev.copy()
    for k, (name, values) in enumerate(baseline_variables(ev, train).items()):
        frame["_var"] = values.to_numpy(dtype=float)
        for h in HORIZONS:
            y = f"{OUTCOME}fwd_ret_{h}_pct"
            if h == PRIMARY_HORIZON:
                res = rho_with_ci(frame, "_var", y, seed_offset=100 + k)
            else:
                xs, ys = _col(frame, "_var"), _col(frame, y)
                res = {"n": st.paired_n(xs, ys), "rho": _r(st.spearman(xs, ys))}
            spread = quintile_spread(frame, "_var", y)
            rows.append({"eval_set": label, "baseline": name, "horizon": h, **res, **spread})
    return rows


def quintile_spread(df: pd.DataFrame, x: str, y: str) -> dict:
    data = df[[x, y]].dropna()
    if len(data) < 10:
        return {"q_low_median": None, "q_mid_median": None, "q_high_median": None, "q_high_minus_low": None}
    ranks = data[x].rank(method="first")
    q = pd.qcut(ranks, 5, labels=False)
    low, mid, high = (st.median(data[y][q == k]) for k in (0, 2, 4))
    return {"q_low_median": _r(low), "q_mid_median": _r(mid), "q_high_median": _r(high),
            "q_high_minus_low": _r(high - low)}


def complete_trades(df: pd.DataFrame) -> pd.DataFrame:
    """Signals that became a PK-21 (unconstrained) trade that was not cut off
    by the end of the data."""
    return df[(df[f"{OUTCOME}trade_traded"] == 1) & (df[f"{OUTCOME}trade_forced_close"] != 1)]


def bucket_table(ev: pd.DataFrame, label: str, group: str = "all") -> list[dict]:
    rows = []
    for k, (bucket, lo, hi) in enumerate(BUCKETS):
        part = ev[ev["bucket"] == bucket]
        row = {"eval_set": label, "group": group, "bucket": bucket, "n": len(part),
               "small_sample": len(part) < SMALL_BUCKET_N}
        for h in HORIZONS:
            y = part[f"{OUTCOME}fwd_ret_{h}_pct"]
            wins, n = st.win_rate(y)
            lo_ci, hi_ci = st.wilson(wins, n)
            row.update({
                f"n_{h}": n, f"median_ret_{h}": _r(st.median(y)), f"mean_ret_{h}": _r(st.mean(y)),
                f"win_rate_{h}": _r(wins / n) if n else None, f"win_ci_low_{h}": _r(lo_ci),
                f"win_ci_high_{h}": _r(hi_ci),
                f"median_mfe_{h}": _r(st.median(part[f"{OUTCOME}mfe_{h}_pct"])),
                f"median_mae_{h}": _r(st.median(part[f"{OUTCOME}mae_{h}_pct"])),
            })
        y10 = _col(part, f"{OUTCOME}fwd_ret_{PRIMARY_HORIZON}_pct")
        if len(part) >= 3:
            reps = boot(part, lambda idx: st.median(y10[idx]), seed_offset=200 + k)
            lo_b, hi_b = st.interval(reps)
        else:
            lo_b = hi_b = None
        row.update({f"median_ret_{PRIMARY_HORIZON}_ci_low": _r(lo_b),
                    f"median_ret_{PRIMARY_HORIZON}_ci_high": _r(hi_b)})
        trades = complete_trades(part)
        r = trades[f"{OUTCOME}trade_net_r"]
        wins_t, n_t = st.win_rate(r)
        row.update({
            "n_trades": n_t, "trade_win_rate": _r(wins_t / n_t) if n_t else None,
            "median_trade_net_r": _r(st.median(r)),
            "trade_profit_factor": _r(st.profit_factor(r)) if n_t >= PF_MIN_N else None,
        })
        rows.append(row)
    return rows


MONOTONIC_TARGETS = tuple(
    [f"{OUTCOME}fwd_ret_{h}_pct" for h in HORIZONS] + [f"{OUTCOME}trade_net_r"]
    + [f"{OUTCOME}mfe_{h}_pct" for h in HORIZONS] + [f"{OUTCOME}mae_{h}_pct" for h in HORIZONS]
)


def monotonicity(ev: pd.DataFrame, label: str) -> list[dict]:
    rows = []
    trades = complete_trades(ev)
    for k, target in enumerate(MONOTONIC_TARGETS):
        frame = trades if target == f"{OUTCOME}trade_net_r" else ev
        res = rho_with_ci(frame, "smart_money_score", target, seed_offset=300 + k)
        rows.append({"eval_set": label, "target": target, **res,
                     **quintile_spread(frame, "smart_money_score", target)})
    return rows


# --------------------------------------------------------------------------
# PK-25G: incremental models
# --------------------------------------------------------------------------

def _design(df: pd.DataFrame, features, stats: dict, strategies) -> np.ndarray:
    columns = [(df["strategy"] == s).astype(float).to_numpy() for s in strategies[1:]]
    for f in features:
        mu, sd = stats[f]
        columns.append(((df[f].astype(float) - mu) / sd).to_numpy())
    x = np.column_stack(columns) if columns else np.zeros((len(df), 0))
    return st.add_intercept(x)


def model_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Rows usable by EVERY model (so all comparisons are paired)."""
    needed = list(CONTINUOUS_MODEL_FEATURES) + [f"{OUTCOME}fwd_ret_{PRIMARY_HORIZON}_pct"]
    return df.dropna(subset=needed)


def fit_models(train: pd.DataFrame, test: pd.DataFrame) -> dict:
    """Fit every model on TRAIN only and predict TEST. Returns predictions
    and metrics; nothing is written anywhere (no model artifact)."""
    train, test = model_rows(train), model_rows(test)
    y_name = f"{OUTCOME}fwd_ret_{PRIMARY_HORIZON}_pct"
    out = {"n_train": len(train), "n_test": len(test), "models": {}}
    if len(train) < 10 or len(test) < 3:
        return out
    strategies = [s for s in STRATEGY_ORDER if (train["strategy"] == s).any()]
    stats = {f: _zfit(train, f) for f in CONTINUOUS_MODEL_FEATURES}
    feature_matrix(train, CONTINUOUS_MODEL_FEATURES)   # the guard: no outcome column may be requested
    y_train, y_test = _col(train, y_name), _col(test, y_name)
    lo, hi = np.quantile(y_train, WINSOR)
    y_train_w = st.winsorize(y_train, lo, hi)
    for name, features in MODELS.items():
        x_train = _design(train, features, stats, strategies)
        x_test = _design(test, features, stats, strategies)
        coef = st.ols_fit(x_train, y_train)
        coef_w = st.ols_fit(x_train, y_train_w)
        beta = st.logistic_fit(x_train, (y_train > 0).astype(float), ridge=LOGISTIC_RIDGE)
        pred, pred_w = x_test @ coef, x_test @ coef_w
        prob = st.logistic_predict(x_test, beta)
        sst = float(((y_test - y_train.mean()) ** 2).sum())
        y_test_w = st.winsorize(y_test, lo, hi)
        sst_w = float(((y_test_w - y_train_w.mean()) ** 2).sum())
        out["models"][name] = {
            "features": list(features), "pred": pred, "prob": prob,
            "ic": st.spearman(pred, y_test), "ic_winsor": st.spearman(pred_w, y_test),
            "r2_oos": 1 - float(((y_test - pred) ** 2).sum()) / sst if sst > 0 else None,
            "r2_oos_winsor": 1 - float(((y_test_w - pred_w) ** 2).sum()) / sst_w if sst_w > 0 else None,
            "mae": float(np.abs(y_test - pred).mean()),
            "auc": st.auc(prob, (y_test > 0).astype(float)),
            "log_loss": st.log_loss(prob, (y_test > 0).astype(float)),
            "brier": st.brier(prob, (y_test > 0).astype(float)),
        }
    out["test"] = test
    out["y_test"] = y_test
    return out


def model_table(fit: dict, label: str) -> list[dict]:
    rows = []
    for name, m in fit.get("models", {}).items():
        rows.append({"eval_set": label, "model": name, "features": "strategy+" + "+".join(m["features"]),
                     "n_train": fit["n_train"], "n_test": fit["n_test"],
                     **{k: _r(m[k]) for k in ("ic", "ic_winsor", "r2_oos", "r2_oos_winsor", "mae", "auc",
                                              "log_loss", "brier")}})
    return rows


def delta_ic(fit: dict, a: str, b: str, seed_offset: int = 0) -> dict:
    if a not in fit.get("models", {}) or b not in fit["models"]:
        return {"delta_ic": None, "ci_low": None, "ci_high": None, "p_boot_greater": None}
    pa, pb, y = fit["models"][a]["pred"], fit["models"][b]["pred"], fit["y_test"]

    def stat(idx):
        ra, rb = st.spearman(pa[idx], y[idx]), st.spearman(pb[idx], y[idx])
        return None if ra is None or rb is None else ra - rb

    point = stat(np.arange(len(y)))
    reps = boot(fit["test"], stat, seed_offset)
    lo, hi = st.interval(reps)
    return {"delta_ic": _r(point), "ci_low": _r(lo), "ci_high": _r(hi),
            "p_boot_greater": _r(st.one_sided_p(reps, True))}


def delta_table(fit: dict, label: str) -> list[dict]:
    rows = []
    for k, (a, b) in enumerate(MODEL_PAIRS):
        res = delta_ic(fit, a, b, seed_offset=400 + k)
        auc_a = fit.get("models", {}).get(a, {}).get("auc")
        auc_b = fit.get("models", {}).get(b, {}).get("auc")
        rows.append({"eval_set": label, "comparison": f"{a}-{b}", "n_test": fit.get("n_test"), **res,
                     "delta_auc": _r(auc_a - auc_b) if auc_a is not None and auc_b is not None else None})
    return rows


def walk_forward(df: pd.DataFrame, limit: str | None, label: str) -> list[dict]:
    rows = []
    y = f"{OUTCOME}fwd_ret_{PRIMARY_HORIZON}_pct"
    for fold in walk_forward_folds(df, limit):
        test = prepare(fold["test"])
        fit = fit_models(prepare(fold["train"]), test)
        ic = {k: fit["models"][k]["ic"] for k in fit.get("models", {})}
        diff = None
        top = test[test["bucket"] == TOP_BUCKET][y]
        mid = test[test["bucket"] == NEUTRAL_BUCKET][y]
        if top.notna().sum() >= 5 and mid.notna().sum() >= 5:
            diff = st.median(top) - st.median(mid)
        rows.append({
            "eval_set": label, "fold": fold["fold"], "test_start": fold["test_start"],
            "test_end": fold["test_end"], "n_train": fit["n_train"], "n_test": fit["n_test"],
            "rho_score_ret10": _r(st.spearman(_col(test, "smart_money_score"), _col(test, y))),
            "ic_M1": _r(ic.get("M1")), "ic_M4": _r(ic.get("M4")),
            "delta_ic_M4_M1": _r(ic["M4"] - ic["M1"]) if ic.get("M4") is not None and ic.get("M1") is not None
            else None,
            "top_minus_neutral_median": _r(diff),
        })
    return rows


# --------------------------------------------------------------------------
# PK-25H strategies, I regimes, K robustness, O contrarian
# --------------------------------------------------------------------------

def strategy_table(ev: pd.DataFrame, fit: dict | None, label: str) -> list[dict]:
    rows = []
    y = f"{OUTCOME}fwd_ret_{PRIMARY_HORIZON}_pct"
    for k, name in enumerate(STRATEGY_ORDER):
        part = ev[ev["strategy"] == name]
        score = part["smart_money_score"]
        res = rho_with_ci(part, "smart_money_score", y, seed_offset=500 + k)
        top = part[part["bucket"] == TOP_BUCKET][y]
        bottom_bucket = next((b for b, *_ in BUCKETS if (part["bucket"] == b).any()), None)
        bottom = part[part["bucket"] == bottom_bucket][y] if bottom_bucket else pd.Series(dtype=float)
        dic = None
        if fit and fit.get("models"):
            mask = (fit["test"]["strategy"] == name).to_numpy()
            if mask.sum() >= 3:
                p4, p1, yt = fit["models"]["M4"]["pred"][mask], fit["models"]["M1"]["pred"][mask], \
                    fit["y_test"][mask]
                a, b = st.spearman(p4, yt), st.spearman(p1, yt)
                dic = None if a is None or b is None else a - b
        trades = complete_trades(part)
        rows.append({
            "eval_set": label, "strategy": name, "n": len(part),
            "score_mean": _r(st.mean(score)), "score_median": _r(st.median(score)),
            "score_p10": _r(st.quantile(score, 0.1)), "score_p90": _r(st.quantile(score, 0.9)),
            **{f"rho_{k2}": v for k2, v in res.items()},
            "n_top_75_100": int(top.notna().sum()), "median_ret10_top": _r(st.median(top)),
            "lowest_bucket": bottom_bucket, "n_lowest": int(bottom.notna().sum()),
            "median_ret10_lowest": _r(st.median(bottom)),
            "rho_score_trade_net_r": _r(st.spearman(_col(trades, "smart_money_score"),
                                                    _col(trades, f"{OUTCOME}trade_net_r"))),
            "n_trades": len(trades), "delta_ic_M4_M1_within": _r(dic),
        })
    return rows


REGIME_DIMENSIONS = ("mkt_above_200dma", "mkt_vol_regime", "quarter", "liquidity_bucket")


def regime_table(ev: pd.DataFrame, label: str) -> list[dict]:
    rows = []
    y = f"{OUTCOME}fwd_ret_{PRIMARY_HORIZON}_pct"
    for dim in REGIME_DIMENSIONS:
        values = ev[dim].map(lambda v: "unknown" if v is None or (isinstance(v, float) and math.isnan(v))
                             else ("above" if v == 1.0 else "below") if dim == "mkt_above_200dma" else str(v))
        for cell in sorted(set(values)):
            part = ev[values == cell]
            top = part[part["bucket"] == TOP_BUCKET][y]
            mid = part[part["bucket"] == NEUTRAL_BUCKET][y]
            rows.append({
                "eval_set": label, "dimension": dim, "cell": cell, "n": len(part),
                "n_ret10": int(part[y].notna().sum()),
                "rho_score_ret10": _r(st.spearman(_col(part, "smart_money_score"), _col(part, y))),
                "n_top": int(top.notna().sum()), "median_ret10_top": _r(st.median(top)),
                "n_neutral": int(mid.notna().sum()), "median_ret10_neutral": _r(st.median(mid)),
                "interpretable": int(part[y].notna().sum()) >= MIN_STRATEGY_N,
            })
    return rows


def _dedup(ev: pd.DataFrame) -> pd.DataFrame:
    keep, last = [], {}
    ordered = ev.assign(_order=ev.index).sort_values(["signal_date", "strategy", "symbol", "_order"])
    for idx, row in ordered.iterrows():
        key = (row["symbol"], row["strategy"])
        if key in last and row["bar_index"] - last[key] < DEDUP_SESSIONS:
            continue
        last[key] = row["bar_index"]
        keep.append(idx)
    kept = set(keep)
    return ev[[i in kept for i in ev.index]]


def robustness_variants(ev: pd.DataFrame, strongest_quarter: str | None) -> dict:
    y = f"{OUTCOME}fwd_ret_{PRIMARY_HORIZON}_pct"
    counts = ev["symbol"].value_counts()
    top_symbols = sorted(counts.index, key=lambda s: (-counts[s], s))[:TOP_SYMBOLS_DROPPED]
    variants = {"full": ev,
                "drop_top5_symbols": ev[~ev["symbol"].isin(top_symbols)],
                "drop_strongest_market_quarter": ev[ev["quarter"] != strongest_quarter] if strongest_quarter
                else ev}
    for name in STRATEGY_ORDER:
        variants[f"leave_out_{name}"] = ev[ev["strategy"] != name]
    if ev[y].notna().any():
        worst = ev[y].abs().idxmax()
        variants["drop_largest_abs_return"] = ev.drop(index=worst)
    variants["drop_special_session_baseline"] = ev[ev["special_session_baseline"] != 1]
    variants["drop_liquidity_warning"] = ev[ev["liquidity_warning"] != 1]
    variants["dedup_first_signal_10_sessions"] = _dedup(ev)
    return variants


def robustness_table(ev: pd.DataFrame, strongest_quarter: str | None, label: str) -> list[dict]:
    rows = []
    y = f"{OUTCOME}fwd_ret_{PRIMARY_HORIZON}_pct"
    for k, (name, part) in enumerate(robustness_variants(ev, strongest_quarter).items()):
        res = rho_with_ci(part, "smart_money_score", y, seed_offset=600 + k)
        values = part[y].dropna().to_numpy(dtype=float)
        winsor = None
        if len(values):
            lo, hi = np.quantile(values, WINSOR)
            winsor = float(st.winsorize(values, lo, hi).mean())
        top = part[part["bucket"] == TOP_BUCKET][y]
        mid = part[part["bucket"] == NEUTRAL_BUCKET][y]
        rows.append({
            "eval_set": label, "variant": name, **res,
            "mean_ret10": _r(st.mean(values)), "median_ret10": _r(st.median(values)),
            "trimmed_mean_ret10": _r(st.trimmed_mean(values, TRIM)), "winsor_mean_ret10": _r(winsor),
            "n_top": int(top.notna().sum()), "median_ret10_top": _r(st.median(top)),
            "n_neutral": int(mid.notna().sum()), "median_ret10_neutral": _r(st.median(mid)),
        })
    return rows


CONTRARIAN_TARGETS = tuple([f"{OUTCOME}fwd_ret_{h}_pct" for h in HORIZONS]
                           + [f"{OUTCOME}mfe_{PRIMARY_HORIZON}_pct", f"{OUTCOME}mae_{PRIMARY_HORIZON}_pct"])


def contrarian_table(ev: pd.DataFrame, label: str) -> list[dict]:
    rows = []
    groups = [("all", ev)] + [(s, ev[ev["strategy"] == s]) for s in STRATEGY_ORDER]
    for g, (group, part) in enumerate(groups):
        for t, target in enumerate(CONTRARIAN_TARGETS):
            row = {"eval_set": label, "group": group, "target": target}
            for bucket in (TOP_BUCKET, MODERATE_BUCKET, NEUTRAL_BUCKET):
                values = part[part["bucket"] == bucket][target]
                row[f"n_{bucket}"] = int(values.notna().sum())
                row[f"median_{bucket}"] = _r(st.median(values))
            for other in (MODERATE_BUCKET, NEUTRAL_BUCKET):
                res = median_diff_with_ci(part, target, TOP_BUCKET, other, seed_offset=700 + 20 * g + 2 * t
                                          + (other == NEUTRAL_BUCKET))
                row.update({f"top_minus_{other}_{k}": v for k, v in res.items() if k not in ("n_a", "n_b")})
            rows.append(row)
    return rows


def strongest_market_quarter(frames: dict | None, lo: str, hi: str) -> str | None:
    """The calendar quarter in [lo, hi) with the largest proxy return."""
    if not frames or MARKET_PROXY not in frames:
        return None
    df = _normal_frame(frames[MARKET_PROXY])
    df = df[(df["date"] >= lo) & (df["date"] < hi)]
    if df.empty:
        return None
    df = df.assign(q=df["date"].map(quarter_of))
    returns = {q: g["close"].iloc[-1] / g["close"].iloc[0] - 1 for q, g in df.groupby("q") if len(g) > 1}
    return max(sorted(returns), key=lambda q: returns[q]) if returns else None


# --------------------------------------------------------------------------
# PK-25J: extreme-bar cohort
# --------------------------------------------------------------------------

def is_extreme(price_change_pct, rvol_20, close_location) -> bool:
    try:
        return (price_change_pct >= EXTREME_BAR["price_change_pct_min"] and rvol_20 >= EXTREME_BAR["rvol_20_min"]
                and close_location >= EXTREME_BAR["close_location_min"])
    except TypeError:
        return False


def extreme_bar_cohort(frames: dict, tokens: dict, verified: dict, lo: str, hi: str) -> list[dict]:
    """Every full-window bar in [lo, hi) meeting the pre-declared extreme
    definition, scored point-in-time (compute_at on the cut frame), with
    market outcomes that stay before `hi` (frames are cut at `hi` first)."""
    rows = []
    for symbol in sorted(frames):
        df = _normal_frame(frames[symbol])
        df = df[df["date"] < hi].reset_index(drop=True)
        if len(df) <= vi.LOOKBACK_BARS:
            continue
        series = vi.compute_series(df)
        mask = ((series["price_change_pct"] >= EXTREME_BAR["price_change_pct_min"])
                & (series["rvol_20"] >= EXTREME_BAR["rvol_20_min"])
                & (series["close_location"] >= EXTREME_BAR["close_location_min"]))
        for i in np.flatnonzero(mask.to_numpy()):
            if i < vi.LOOKBACK_BARS - 1 or df["date"].iloc[i] < lo:
                continue
            features = vi.compute_at(df.iloc[: i + 1], symbol=symbol, instrument_token=tokens.get(symbol),
                                     verified_gaps=verified.get(symbol, ()))
            record = vr.score_record(features)
            rows.append({
                "symbol": symbol, "date": df["date"].iloc[i],
                "price_change_pct": _r(series["price_change_pct"].iloc[i], 4),
                "rvol_20": _r(series["rvol_20"].iloc[i], 4),
                "close_location": _r(series["close_location"].iloc[i], 4),
                "smart_money_score": record["smart_money_score"],
                "bucket": bucket_of(record["smart_money_score"]),
                "sms_exhaustion": record["sms_exhaustion"],
                **market_outcomes(df, int(i)),
            })
    return rows


def all_bar_outcome_summary(frames: dict, lo: str, hi: str) -> dict:
    """Unconditional outcomes of every full-window bar in [lo, hi), frames
    cut at `hi` — the reference the cohort is compared against."""
    values = {f"fwd_ret_{h}": [] for h in HORIZONS}
    values.update({f"mfe_{PRIMARY_HORIZON}": [], f"mae_{PRIMARY_HORIZON}": []})
    for symbol in sorted(frames):
        df = _normal_frame(frames[symbol])
        df = df[df["date"] < hi].reset_index(drop=True)
        o, c = df["open"].to_numpy(float), df["close"].to_numpy(float)
        high, low = df["high"].to_numpy(float), df["low"].to_numpy(float)
        for i in range(vi.LOOKBACK_BARS - 1, len(df) - 1):
            if df["date"].iloc[i] < lo:
                continue
            entry = o[i + 1]
            for h in HORIZONS:
                if i + h < len(df):
                    values[f"fwd_ret_{h}"].append((c[i + h] / entry - 1) * 100)
            h = PRIMARY_HORIZON
            if i + h < len(df):
                values[f"mfe_{h}"].append((high[i + 1: i + h + 1].max() / entry - 1) * 100)
                values[f"mae_{h}"].append((low[i + 1: i + h + 1].min() / entry - 1) * 100)
    return {k: {"n": len(v), "median": _r(st.median(v)), "mean": _r(st.mean(v))} for k, v in values.items()}


def cohort_summary(cohort: list[dict], ev: pd.DataFrame, reference: dict, label: str,
                   bar_scope: str = "") -> list[dict]:
    """Bar groups cover every full-window bar in `bar_scope`; signal groups
    cover the evaluation set `label` only."""
    rows = []
    frame = pd.DataFrame(cohort)
    targets = [f"{OUTCOME}fwd_ret_{h}_pct" for h in HORIZONS] + \
              [f"{OUTCOME}mfe_{PRIMARY_HORIZON}_pct", f"{OUTCOME}mae_{PRIMARY_HORIZON}_pct"]
    signal_mask = np.array([is_extreme(a, b, c) for a, b, c in zip(ev["price_change_pct"], ev["rvol_20"],
                                                                   ev["close_location"])], dtype=bool)
    groups = [("bar_cohort_all", frame)]
    if len(frame):
        for bucket, *_ in BUCKETS:
            groups.append((f"bar_cohort_{bucket}", frame[frame["bucket"] == bucket]))
    groups.append(("signal_cohort", ev[signal_mask]))
    groups.append(("signal_non_cohort_75_100", ev[~signal_mask & (ev["bucket"] == TOP_BUCKET).to_numpy()]))
    for name, part in groups:
        row = {"eval_set": label, "group": name,
               "scope": f"bars {bar_scope}" if name.startswith("bar_") else f"{label} signals", "n": len(part),
               "score_median": _r(st.median(part["smart_money_score"])) if len(part) else None,
               "share_score_ge_75": _r((part["smart_money_score"] >= 75).mean()) if len(part) else None}
        for t in targets:
            row[f"median_{t}"] = _r(st.median(part[t])) if len(part) and t in part else None
        rows.append(row)
    ref = {"eval_set": label, "group": "all_full_window_bars (unconditional)", "scope": f"bars {bar_scope}",
           "n": reference.get(f"fwd_ret_{PRIMARY_HORIZON}", {}).get("n"), "score_median": None,
           "share_score_ge_75": None}
    for h in HORIZONS:
        ref[f"median_{OUTCOME}fwd_ret_{h}_pct"] = reference.get(f"fwd_ret_{h}", {}).get("median")
    ref[f"median_{OUTCOME}mfe_{PRIMARY_HORIZON}_pct"] = reference.get(f"mfe_{PRIMARY_HORIZON}", {}).get("median")
    ref[f"median_{OUTCOME}mae_{PRIMARY_HORIZON}_pct"] = reference.get(f"mae_{PRIMARY_HORIZON}", {}).get("median")
    rows.append(ref)
    return rows


# --------------------------------------------------------------------------
# PK-25N: practical value through the PK-21 simulator
# --------------------------------------------------------------------------

class ScoreFilteredStrategy:
    """A research adapter around a strategy module. Every attribute is the
    module's own; only evaluate_entry is wrapped: when the strategy passes,
    the score is computed from THE SAME PREFIX FRAME (point-in-time) and the
    signal is dropped below `threshold` or when the score is null. The
    strategy module itself is never modified."""

    def __init__(self, module, threshold: float, verified_gaps: dict | None = None):
        self._module = module
        self.threshold = threshold
        self._gaps = verified_gaps or {}
        self.scored = []

    def __getattr__(self, name):
        return getattr(self._module, name)

    def evaluate_entry(self, df, params):
        result = self._module.evaluate_entry(df, params)
        if not result["passed"]:
            return result
        symbol = df.attrs.get(SYMBOL_ATTR)
        value = sms.score(vi.compute_at(df, symbol=symbol, verified_gaps=self._gaps.get(symbol, ())))["score"]
        self.scored.append((symbol, str(df["date"].iloc[-1])[:10], value))
        if value is None or value < self.threshold:
            return {**result, "passed": False, "pk25_filtered_score": value}
        return result


def filter_simulation(frames: dict, verified: dict, ev: pd.DataFrame, lo: str, hi_inclusive: str,
                      label: str, strategies=STRATEGY_ORDER) -> list[dict]:
    """All signals vs score >= 60 vs score >= 75, through the unmodified
    backtest.simulate with the default PK-21 config, over [lo, hi]. Frames
    are cut at `hi_inclusive` before anything runs."""
    rows = []
    cut = sim_frames(frames, hi_inclusive, verified)
    cfg = BacktestConfig.from_overrides()
    for name in strategies:
        module = STRATEGIES[name]
        params = dict(module.DEFAULT_PARAMS)
        in_period = ev[(ev["strategy"] == name) & (ev["signal_date"] >= lo) & (ev["signal_date"] <= hi_inclusive)]
        for threshold in FILTER_THRESHOLDS:
            runner = module if threshold is None else ScoreFilteredStrategy(module, threshold, verified)
            sim = simulate(runner, cut, params, cfg, lo, hi_inclusive)
            metrics = compute_portfolio_metrics(sim["trades"], sim["equity_curve"], cfg.initial_capital, lo,
                                                hi_inclusive, sim["skipped_signals"], sim["unexecuted_signals"])
            if threshold is None:
                filtered_out = in_period.iloc[0:0]
            else:
                filtered_out = in_period[in_period["smart_money_score"].isna()
                                         | (in_period["smart_money_score"] < threshold)]
            outcome = complete_trades(filtered_out)
            rows.append({
                "eval_set": label, "strategy": name, "scenario": "all" if threshold is None else f"score>={threshold:g}",
                "period_start": lo, "period_end": hi_inclusive,
                "evaluable_signals": len(in_period), "evaluable_signals_filtered_out": len(filtered_out),
                "trades": metrics["trades"], "net_pnl": metrics["net_pnl"],
                "net_return_pct": metrics["net_return_pct"], "net_cagr_pct": metrics["net_cagr_pct"],
                "total_net_r": _r(sum(t["net_r_multiple"] for t in sim["trades"]), 4),
                "expectancy_r": metrics["expectancy_r"], "win_rate": _r(metrics["win_rate"]),
                "profit_factor": _r(metrics["profit_factor"]), "max_drawdown_pct": metrics["max_drawdown_pct"],
                "exposure_pct": metrics["exposure_pct"], "time_in_market_pct": metrics["time_in_market_pct"],
                "skipped_signals": metrics["skipped_signals"],
                "missed_winners": int((outcome[f"{OUTCOME}trade_won"] == 1).sum()),
                "avoided_losers": int((outcome[f"{OUTCOME}trade_won"] == 0).sum()),
            })
    return rows


def filter_summary(rows: list[dict]) -> dict:
    """Pooled across strategies: expectancy (trade-weighted) and the worst
    per-strategy drawdown, per scenario."""
    out = {}
    for scenario in sorted({r["scenario"] for r in rows}):
        part = [r for r in rows if r["scenario"] == scenario]
        trades = sum(r["trades"] for r in part)
        total_r = sum(r["total_net_r"] or 0 for r in part)
        dds = [r["max_drawdown_pct"] for r in part if r["max_drawdown_pct"] is not None]
        out[scenario] = {"trades": trades, "total_net_r": _r(total_r, 4),
                         "expectancy_r": _r(total_r / trades, 4) if trades else None,
                         "max_drawdown_pct": _r(max(dds)) if dds else None}
    return out


# --------------------------------------------------------------------------
# Hypotheses and PK-25R classification
# --------------------------------------------------------------------------

def hypotheses(ev: pd.DataFrame, fit: dict, wf: list[dict]) -> dict:
    y = f"{OUTCOME}fwd_ret_{PRIMARY_HORIZON}_pct"
    h1 = rho_with_ci(ev, "smart_money_score", y, seed_offset=1)
    h2 = median_diff_with_ci(ev, y, TOP_BUCKET, NEUTRAL_BUCKET, seed_offset=2)
    h3 = delta_ic(fit, "M4", "M1", seed_offset=3)
    wf_dic = [r["delta_ic_M4_M1"] for r in wf if r["delta_ic_M4_M1"] is not None]
    wf_rho = [r["rho_score_ret10"] for r in wf if r["rho_score_ret10"] is not None]
    wf_h2 = [r["top_minus_neutral_median"] for r in wf if r["top_minus_neutral_median"] is not None]
    h4 = {"mfe": rho_with_ci(ev, "smart_money_score", f"{OUTCOME}mfe_{PRIMARY_HORIZON}_pct", seed_offset=4),
          "mae": rho_with_ci(ev, "smart_money_score", f"{OUTCOME}mae_{PRIMARY_HORIZON}_pct", seed_offset=5)}
    per_strategy = {}
    for name in STRATEGY_ORDER:
        part = ev[ev["strategy"] == name]
        n = st.paired_n(_col(part, "smart_money_score"), _col(part, y))
        per_strategy[name] = {"n": n, "rho": _r(st.spearman(_col(part, "smart_money_score"), _col(part, y)))}
    eligible = {k: v for k, v in per_strategy.items() if v["n"] >= MIN_STRATEGY_N and v["rho"] is not None}
    share = lambda xs, positive: (sum(1 for x in xs if (x > 0 if positive else x < 0)) / len(xs)) if xs else None
    primary_p = {"H1": h1["p_boot_greater"], "H2": h2["p_boot_greater"], "H3": h3["p_boot_greater"]}
    return {
        "H1": h1, "H2": h2, "H3": {**h3, "wf_mean_delta_ic": _r(st.mean(wf_dic)), "wf_folds": len(wf_dic),
                                   "wf_share_delta_ic_positive": _r(share(wf_dic, True))},
        "H4": h4,
        "H5": {"per_strategy": per_strategy, "strategies_eligible": len(eligible),
               "strategies_positive": sum(1 for v in eligible.values() if v["rho"] > 0),
               "strategies_negative": sum(1 for v in eligible.values() if v["rho"] < 0),
               "wf_folds": len(wf_rho), "wf_share_rho_positive": _r(share(wf_rho, True)),
               "wf_share_rho_negative": _r(share(wf_rho, False)),
               "wf_share_h2_positive": _r(share(wf_h2, True))},
        "primary_p_one_sided": primary_p,
        "primary_p_holm": {k: _r(v) for k, v in st.holm(primary_p).items()},
    }


def summary_for_classification(h: dict, n10: int, filters: dict | None) -> dict:
    all_f, f60 = (filters or {}).get("all"), (filters or {}).get("score>=60")
    dd_ratio = exp_delta = None
    if all_f and f60:
        if all_f["max_drawdown_pct"] and f60["max_drawdown_pct"] is not None:
            dd_ratio = f60["max_drawdown_pct"] / all_f["max_drawdown_pct"]
        if all_f["expectancy_r"] is not None and f60["expectancy_r"] is not None:
            exp_delta = f60["expectancy_r"] - all_f["expectancy_r"]
    return {
        "n10": n10, "rho10": h["H1"]["rho"], "rho10_lo": h["H1"]["ci_low"], "rho10_hi": h["H1"]["ci_high"],
        "h2_diff": h["H2"]["diff"], "dic": h["H3"]["delta_ic"], "dic_lo": h["H3"]["ci_low"],
        "dic_hi": h["H3"]["ci_high"], "wf_mean_dic": h["H3"]["wf_mean_delta_ic"],
        "wf_share_dic_pos": h["H3"]["wf_share_delta_ic_positive"],
        "wf_share_rho_pos": h["H5"]["wf_share_rho_positive"], "wf_share_rho_neg": h["H5"]["wf_share_rho_negative"],
        "wf_share_h2_pos": h["H5"]["wf_share_h2_positive"],
        "strat_pos": h["H5"]["strategies_positive"], "strat_neg": h["H5"]["strategies_negative"],
        "filter60_dd_ratio": dd_ratio, "filter60_expectancy_delta": exp_delta,
    }


def classify(s: dict, c: dict = DECISION_CRITERIA) -> dict:
    """PK-25R. A pure function of the summary numbers and the frozen
    criteria; the first rule that matches wins. Returns the label and the
    list of evaluated conditions (so the report can show why)."""
    ge = lambda a, b: a is not None and b is not None and a >= b
    gt = lambda a, b: a is not None and b is not None and a > b
    le = lambda a, b: a is not None and b is not None and a <= b
    lt = lambda a, b: a is not None and b is not None and a < b
    checks = {}

    checks["INCONCLUSIVE_SAMPLE"] = {"n10_below_min": s["n10"] < c["min_holdout_n"]}
    if all(checks["INCONCLUSIVE_SAMPLE"].values()):
        return {"label": "INCONCLUSIVE", "rule": "INCONCLUSIVE_SAMPLE", "checks": checks}

    checks["CONTRARIAN"] = {
        "rho10_le_minus_effect": le(s["rho10"], -c["rho_effect"]),
        "rho10_ci_high_below_0": lt(s["rho10_hi"], 0),
        "top_median_below_neutral": lt(s["h2_diff"], 0),
        "negative_in_strategies_or_folds": s["strat_neg"] >= c["strategy_min_count"]
        or ge(s["wf_share_rho_neg"], c["wf_consistency"]),
    }
    if all(checks["CONTRARIAN"].values()):
        return {"label": "CONTRARIAN", "rule": "CONTRARIAN", "checks": checks}

    checks["PROMISING"] = {
        "h1_rho10_ge_effect": ge(s["rho10"], c["rho_effect"]),
        "h1_ci_low_above_0": gt(s["rho10_lo"], 0),
        "h2_top_above_neutral": gt(s["h2_diff"], 0),
        "h3_delta_ic_ge_min": ge(s["dic"], c["delta_ic_min"]),
        "h3_wf_mean_delta_ic_positive": gt(s["wf_mean_dic"], 0),
        "h5_strategies_positive": s["strat_pos"] >= c["strategy_min_count"],
        "h5_folds_positive": ge(s["wf_share_rho_pos"], c["wf_consistency"]),
        "filter60_drawdown_ok": le(s["filter60_dd_ratio"], c["max_drawdown_ratio"]),
        "filter60_expectancy_not_worse": ge(s["filter60_expectancy_delta"], 0),
    }
    if all(checks["PROMISING"].values()):
        return {"label": "PROMISING", "rule": "PROMISING", "checks": checks}

    half_width = (s["rho10_hi"] - s["rho10_lo"]) / 2 if s["rho10_hi"] is not None and s["rho10_lo"] is not None \
        else None
    checks["NO_EDGE"] = {
        "rho10_ci_contains_0": le(s["rho10_lo"], 0) and ge(s["rho10_hi"], 0),
        "abs_rho10_below_effect": s["rho10"] is not None and abs(s["rho10"]) < c["rho_effect"],
        "delta_ic_ci_contains_0_or_not_positive": (le(s["dic_lo"], 0) and ge(s["dic_hi"], 0)) or le(s["dic"], 0),
        "rho10_ci_precise": le(half_width, c["no_edge_max_ci_half_width"]),
    }
    if all(checks["NO_EDGE"].values()):
        return {"label": "NO_EDGE", "rule": "NO_EDGE", "checks": checks}

    checks["LIMITED"] = {
        "h1_directional": gt(s["rho10"], 0) and ge(s["wf_share_rho_pos"], c["wf_consistency"]),
        "h2_directional": gt(s["h2_diff"], 0) and ge(s["wf_share_h2_pos"], c["wf_consistency"]),
        "h3_directional": gt(s["dic"], 0) and ge(s["wf_share_dic_pos"], c["wf_consistency"]),
    }
    if any(checks["LIMITED"].values()):
        return {"label": "LIMITED", "rule": "LIMITED", "checks": checks}
    return {"label": "INCONCLUSIVE", "rule": "INCONCLUSIVE", "checks": checks}


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------

def evaluate(ev_raw: pd.DataFrame, train_raw: pd.DataFrame | None, label: str, strongest_quarter) -> dict:
    """Every dataset-only analysis on one evaluation set (primary rows)."""
    ev = prepare(primary(ev_raw))
    train = prepare(primary(train_raw)) if train_raw is not None else None
    out = {
        "baselines": baselines(ev, train if train is not None else ev, label),
        "buckets": bucket_table(ev, label) + [row for s in STRATEGY_ORDER
                                              for row in bucket_table(ev[ev["strategy"] == s], label, s)],
        "monotonicity": monotonicity(ev, label),
        "regimes": regime_table(ev, label),
        "robustness": robustness_table(ev, strongest_quarter, label),
        "contrarian": contrarian_table(ev, label),
    }
    fit = fit_models(train, ev) if train is not None else None
    out["models"] = model_table(fit, label) + delta_table(fit, label) if fit else []
    out["strategies"] = strategy_table(ev, fit, label)
    out["_fit"] = fit
    out["_ev"] = ev
    return out


OUTPUT_TABLES = ("baselines", "buckets", "monotonicity", "models", "walk_forward", "strategies", "regimes",
                 "extreme_bars", "robustness", "filter_simulation", "contrarian")


def run_stage(df: pd.DataFrame, splits: dict, stage: str, *, frames: dict | None = None,
              tokens: dict | None = None, verified: dict | None = None) -> dict:
    """stage 'pre_holdout': development (descriptive) + validation (models
    trained on development) + walk-forward folds before the holdout. Holdout
    rows are removed by pre_holdout_view BEFORE anything else runs.
    stage 'holdout': the holdout, models trained on the pre-holdout view,
    and the full walk-forward."""
    tables = {name: [] for name in OUTPUT_TABLES}
    verified = verified or {}
    if stage == "pre_holdout":
        visible = pre_holdout_view(df, splits)
        dev = visible[visible["segment"] == "development"]
        val = visible[visible["segment"] == "validation"]
        lo, hi = dev["signal_date"].min(), splits["holdout_start"]
        quarter = strongest_market_quarter(frames, lo, hi)
        results = {"development": evaluate(dev, None, "development", quarter),
                   "validation": evaluate(val, dev, "validation", quarter)}
        wf = walk_forward(visible, splits["holdout_start"], "pre_holdout")
        headline_set, headline = "validation", results["validation"]
        period = (splits["validation_start"], _day_before(splits["holdout_start"]))
        cohort_range = (DATASET_START, splits["holdout_start"])
    elif stage == "holdout":
        visible = assign_segments(df, splits)
        train = pre_holdout_view(df, splits)
        hold = visible[visible["segment"] == "holdout"]
        end = df["signal_date"].max()
        quarter = strongest_market_quarter(frames, splits["holdout_start"], "9999-12-31")
        results = {"holdout": evaluate(hold, train, "holdout", quarter)}
        wf = walk_forward(visible, None, "full")
        headline_set, headline = "holdout", results["holdout"]
        period = (splits["holdout_start"], _last_date(frames) or end)
        cohort_range = (splits["holdout_start"], "9999-12-31")
    else:
        raise ValueError(f"unknown stage {stage!r}")

    for res in results.values():
        for name in ("baselines", "buckets", "monotonicity", "models", "strategies", "regimes", "robustness",
                     "contrarian"):
            tables[name].extend(res[name])
    tables["walk_forward"] = wf

    filters = None
    if frames is not None:
        tables["filter_simulation"] = filter_simulation(frames, verified, headline["_ev"], period[0], period[1],
                                                        headline_set)
        filters = filter_summary(tables["filter_simulation"])
        cohort = extreme_bar_cohort(frames, tokens or {}, verified, *cohort_range)
        reference = all_bar_outcome_summary(frames, *cohort_range)
        tables["extreme_bars"] = cohort_summary(cohort, headline["_ev"], reference, headline_set,
                                                f"{cohort_range[0]} to before {cohort_range[1]}")
        tables["extreme_bar_members"] = cohort

    h = hypotheses(headline["_ev"], headline["_fit"] or {}, wf)
    n10 = int(headline["_ev"][f"{OUTCOME}fwd_ret_{PRIMARY_HORIZON}_pct"].notna().sum())
    summary = summary_for_classification(h, n10, filters)
    result = {
        "experiment": EXPERIMENT, "stage": stage, "headline_set": headline_set, "splits": splits,
        "segment_counts": segment_counts(df, splits, include_holdout_outcomes=(stage == "holdout")),
        "hypotheses": h, "filter_summary": filters, "classification_inputs": summary,
    }
    if stage == "holdout":
        verdict = classify(summary)
        result["classification"] = verdict
        result["next_step"] = NEXT_STEPS[verdict["label"]]
    else:
        result["classification_preview_note"] = ("No classification is issued before the holdout; the "
                                                 "criteria are applied once, to holdout results.")
    tables["_result"] = result
    return tables


def _day_before(day: str) -> str:
    return date.fromordinal(date.fromisoformat(day).toordinal() - 1).isoformat()


def _last_date(frames: dict | None) -> str | None:
    if not frames:
        return None
    return max(_normal_frame(df)["date"].iloc[-1] for df in frames.values())


def segment_counts(df: pd.DataFrame, splits: dict, include_holdout_outcomes: bool = True) -> dict:
    """Signal counts per segment. Before the holdout is evaluated, the
    holdout's outcome columns are not read at all — not even for a count."""
    seg = assign_segments(df, splits)
    out = {}
    for name in ("development", "validation", "holdout"):
        part = seg[seg["segment"] == name]
        out[name] = {"signals": len(part), "eligible": int((part["excl_reason"] == "").sum())}
        if name == "holdout" and not include_holdout_outcomes:
            continue
        out[name]["purged"] = int(part["purged"].sum())
        out[name]["eligible_unpurged_with_ret10"] = int(((part["excl_reason"] == "") & ~part["purged"]
                                                         & part[f"{OUTCOME}fwd_ret_10_pct"].notna()).sum())
    return out


def union_columns(rows: list[dict]) -> list[str]:
    """Every key of every row, in first-seen order (tables mix row shapes)."""
    columns: dict[str, None] = {}
    for row in rows:
        columns.update(dict.fromkeys(row))
    return list(columns) or ["empty"]


def write_stage(tables: dict, out_dir: Path, stage: str) -> dict:
    """Writes one CSV per table plus the stage JSON; returns their hashes."""
    out_dir.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name in list(OUTPUT_TABLES) + ["extreme_bar_members"]:
        rows = tables.get(name) or []
        path = out_dir / f"{name}_{stage}.csv"
        write_rows(rows, path, union_columns(rows))
        hashes[path.name] = file_sha256(path)
    result_name = HOLDOUT_RESULTS if stage == "holdout" else PRE_HOLDOUT_RESULTS
    path = out_dir / result_name
    path.write_text(json.dumps(_jsonable(tables["_result"]), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    hashes[result_name] = file_sha256(path)
    return hashes


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return None if math.isnan(float(obj)) else round(float(obj), 6)
    return obj


def dataset_summary(df: pd.DataFrame, splits: dict | None, excluded_series: list) -> dict:
    out = {
        "rows": len(df), "symbols": int(df["symbol"].nunique()),
        "first_signal": df["signal_date"].min(), "last_signal": df["signal_date"].max(),
        "by_strategy": {s: int((df["strategy"] == s).sum()) for s in STRATEGY_ORDER},
        "exclusions": {k: int(v) for k, v in df["excl_reason"].replace("", "eligible").value_counts().sort_index()
                       .items()},
        "labelled_not_excluded": {
            "special_session_baseline": int((df["special_session_baseline"] == 1).sum()),
            "liquidity_warning": int((df["liquidity_warning"] == 1).sum()),
            "data_quality_degraded": int((df["data_quality"] == "DEGRADED").sum()),
        },
        "missing_outcome": {f"fwd_ret_{h}": int(df[f"{OUTCOME}fwd_ret_{h}_pct"].isna().sum()) for h in HORIZONS},
        "excluded_series": excluded_series,
        "ambiguous_exchange_series": "none: the universe is NSE-cache tokens only (BSE duplicates never loaded)",
        "sector_analysis": "skipped: sector_mapping holds 1 row, not a usable classification",
    }
    if splits:
        out["splits"] = splits
        out["segments"] = segment_counts(df, splits)
    return out


# --------------------------------------------------------------------------
# Manifest protocol
# --------------------------------------------------------------------------

def read_manifest(out_dir: Path) -> dict:
    path = out_dir / MANIFEST
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_manifest(out_dir: Path, manifest: dict) -> None:
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check_model_frozen(manifest: dict, root: Path = ROOT) -> list[str]:
    """Differences between the frozen model/production code and now."""
    problems = []
    frozen = manifest.get("model_freeze") or {}
    if frozen.get("smart_money_score_version") != sms.SMART_MONEY_SCORE_VERSION:
        problems.append("smart_money_score_version changed")
    if frozen.get("volume_feature_version") != vi.FEATURE_VERSION:
        problems.append("volume feature version changed")
    for group in ("model_file_sha256", "production_file_sha256"):
        for f, sha in (frozen.get(group) or {}).items():
            if normalised_sha256(root / f) != sha:
                problems.append(f"{f} differs from the frozen hash")
    return problems


def check_design_frozen(manifest: dict, dataset_path: Path, out_dir: Path, root: Path = ROOT) -> list[str]:
    problems = check_model_frozen(manifest, root)
    frozen = manifest.get("design_freeze")
    if not frozen:
        return problems + ["design not frozen (run `freeze` first)"]
    if file_sha256(dataset_path) != frozen["dataset_sha256"]:
        problems.append("dataset differs from the frozen dataset")
    for f, sha in frozen["analysis_code_sha256"].items():
        if normalised_sha256(root / f) != sha:
            problems.append(f"{f} (analysis code) changed after the design freeze")
    if frozen["design"] != _jsonable(design()):
        problems.append("design constants changed after the freeze")
    for name, sha in frozen["pre_holdout_output_sha256"].items():
        path = out_dir / name
        if not path.exists() or file_sha256(path) != sha:
            problems.append(f"pre-holdout output {name} changed")
    return problems


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _load(db: Path):
    conn = vr._open_copy(db)
    try:
        return vr.load_universe(conn)
    finally:
        conn.close()


def cmd_freeze_model(args) -> int:
    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = read_manifest(out_dir)
    frozen = freeze_definitions()
    if manifest.get("model_freeze"):
        stable = {k: v for k, v in frozen.items() if k != "git_head"}
        before = {k: v for k, v in manifest["model_freeze"].items() if k not in ("git_head", "frozen_at")}
        if stable != before:
            raise ProtocolError("the model differs from the frozen model; start a new experiment")
        print("model already frozen; unchanged")
        return 0
    manifest = {"experiment": EXPERIMENT, "model_freeze": {**frozen, "frozen_at": _now()}}
    write_manifest(out_dir, manifest)
    print(f"model frozen: score v{frozen['smart_money_score_version']}, features v{frozen['volume_feature_version']}")
    return 0


def cmd_build(args) -> int:
    manifest = read_manifest(args.out)
    problems = check_model_frozen(manifest) if manifest.get("model_freeze") else ["model not frozen"]
    if problems:
        raise ProtocolError("; ".join(problems))
    frames, tokens, verified, excluded = _load(args.db)
    rows = build_dataset(frames, tokens, verified)
    write_rows(rows, args.dataset)
    print(f"{len(rows)} signal rows -> {args.dataset} sha256 {file_sha256(args.dataset)}")
    df = load_dataset(args.dataset)
    splits = split_dates(df)
    summary = dataset_summary(df, splits, excluded)
    (args.out / "dataset_summary.json").write_text(json.dumps(_jsonable(summary), indent=2, sort_keys=True) + "\n",
                                                   encoding="utf-8")
    return 0


def cmd_analyze(args) -> int:
    manifest = read_manifest(args.out)
    df = load_dataset(args.dataset)
    if args.stage == "pre_holdout":
        problems = check_model_frozen(manifest)
        if manifest.get("design_freeze"):
            problems.append("design already frozen; pre-holdout outputs may not be regenerated in place")
        if problems:
            raise ProtocolError("; ".join(problems))
        splits = split_dates(df)
        out_dir = args.out
    else:
        problems = check_design_frozen(manifest, args.dataset, args.out)
        if problems:
            raise ProtocolError("; ".join(problems))
        splits = manifest["design_freeze"]["splits"]
        out_dir = args.verify_dir or args.out
        if args.verify_dir is None and (args.out / HOLDOUT_RESULTS).exists() and not args.new_experiment:
            raise ProtocolError("holdout results already exist: the holdout is evaluated once")
    frames, tokens, verified, _ = _load(args.db)
    tables = run_stage(df, splits, args.stage, frames=frames, tokens=tokens, verified=verified)
    hashes = write_stage(tables, out_dir, args.stage)
    for name, sha in sorted(hashes.items()):
        print(f"{sha}  {name}")
    if args.stage == "holdout" and args.verify_dir is None:
        manifest["holdout_evaluated"] = {"at": _now(), "output_sha256": hashes}
        write_manifest(args.out, manifest)
    return 0


def cmd_freeze(args) -> int:
    manifest = read_manifest(args.out)
    problems = check_model_frozen(manifest)
    if manifest.get("design_freeze"):
        problems.append("design already frozen")
    if problems:
        raise ProtocolError("; ".join(problems))
    df = load_dataset(args.dataset)
    splits = split_dates(df)
    pre = {}
    for name in [f"{t}_pre_holdout.csv" for t in list(OUTPUT_TABLES) + ["extreme_bar_members"]] + \
                [PRE_HOLDOUT_RESULTS]:
        path = args.out / name
        if not path.exists():
            raise ProtocolError(f"missing pre-holdout output {name}")
        pre[name] = file_sha256(path)
    manifest["design_freeze"] = {
        "frozen_at": _now(), "dataset_sha256": file_sha256(args.dataset), "dataset_rows": len(df),
        "splits": splits, "design": _jsonable(design()), "analysis_code_sha256": code_hashes(ANALYSIS_FILES),
        "pre_holdout_output_sha256": pre,
        "statement": "Every analysis choice above was fixed before the holdout statistics were computed.",
    }
    write_manifest(args.out, manifest)
    print(f"design frozen: holdout from {splits['holdout_start']}, dataset {manifest['design_freeze']['dataset_sha256']}")
    return 0


def cmd_verify(args) -> int:
    manifest = read_manifest(args.out)
    recorded = (manifest.get("holdout_evaluated") or {}).get("output_sha256")
    if not recorded:
        raise ProtocolError("no holdout evaluation recorded")
    with tempfile.TemporaryDirectory() as tmp:
        args.stage, args.verify_dir, args.new_experiment = "holdout", Path(tmp), False
        cmd_analyze(args)
        mismatched = [name for name, sha in recorded.items() if file_sha256(Path(tmp) / name) != sha]
    if mismatched:
        print("NOT reproducible:", ", ".join(mismatched))
        return 1
    print(f"holdout stage reproduced: {len(recorded)} files identical")
    return 0


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.smart_money_validation")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("freeze-model", "build", "analyze", "freeze", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--out", type=Path, default=ROOT / "research" / "pk25")
        if name != "freeze-model":
            p.add_argument("--dataset", type=Path, required=True)
        if name in ("build", "analyze", "verify"):
            p.add_argument("--db", type=Path, required=True, help="a COPY of kite_terminal.db (read-only)")
        if name == "analyze":
            p.add_argument("--stage", choices=("pre_holdout", "holdout"), required=True)
            p.add_argument("--new-experiment", action="store_true")
    args = parser.parse_args(argv)
    args.verify_dir = getattr(args, "verify_dir", None)
    handlers = {"freeze-model": cmd_freeze_model, "build": cmd_build, "analyze": cmd_analyze,
                "freeze": cmd_freeze, "verify": cmd_verify}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
