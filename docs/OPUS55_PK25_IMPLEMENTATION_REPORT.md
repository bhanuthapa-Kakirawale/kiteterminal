# OPUS55 PK-25 Implementation Report: out-of-sample validation of Smart Money Score v1.0

| | |
|---|---|
| Packet | **PK-25** only, research and validation. PK-26 and later were not started. No production file was modified: score, features, strategies, scanner, ranking, risk, approval, sizing, execution, backtester and research export are all untouched (hash-checked). |
| Baseline before | **1,415 passed**, 0 failed, 0 xfailed, 0 xpassed, measured on the untouched code (`5391586`) before the first edit. |
| Backups | Git tag `backup-pre-pk25` and branch `backup/pre-pk25` at `5391586`. `backend/kite_terminal.db.pre_pk25_bak`, made with the SQLite online-backup API from a read-only connection: `integrity_check` ok, 28 tables, 18 trade tickets, 50,556 daily candles, 10,133 cached instruments, sha256 `01a99048…7216` (identical to the PK-24 backup). Not committed (`*_bak` is git-ignored). |
| After | **1,461 passed** (1,415 + 46 new PK-25 tests), three consecutive times. See *Full Suite ×3*. |
| Live DB | `backend/kite_terminal.db` sha256 `1378f8b4…dacf` before and after all work. It was never opened by app code; every run used a scratch copy of the backup, deleted afterwards. |
| **Result** | **LIMITED** under the pre-declared rules. In substance the score shows **no incremental value** once the strategy is known. See *Final Classification* and *Practical Interpretation*. |

# Executive Summary

PK-25 tested whether Smart Money Accumulation Score v1.0 carries out-of-sample information beyond:
- the strategy signal itself;
- simple PK-23 volume features;
- basic market context.

**Method.**
- The score was frozen first, by hash. The dataset is the 410 historical strategy signals, each scored point-in-time.
- The split is chronological: development before 2025-09-01, validation to 2026-01-31, holdout from 2026-02-01. Outcome windows that cross a boundary are purged.
- Every analysis choice was frozen in a manifest before the holdout was evaluated **once**. That includes hypotheses, buckets, models, bootstrap settings and the decision rules.

**The pre-declared classifier returns LIMITED.** Two primary hypotheses point in the hypothesised direction on the holdout, but neither is statistically established:

| Hypothesis | Holdout (N = 98) | Supported? |
|---|---|---|
| H1: rank correlation of score with 10-session return > 0 | ρ = **+0.138**, 95% CI −0.069 … +0.364, one-sided p 0.11 (Holm 0.22) | direction only |
| H2: 75–100 bucket beats 45–59 bucket on median 10-session return | +1.57 pp, CI −0.19 … +3.20, p 0.037 (Holm 0.11) | direction only |
| H3: adding the score improves a strategy + RVOL model out of sample | ΔIC = **−0.140**, CI −0.350 … +0.121; walk-forward ΔIC negative in **6 of 6** folds | **No** |

**What the numbers actually say.**
1. **The score adds nothing once the strategy is known.** Every model that adds the score to a strategy-aware baseline gets *worse* out of sample, in validation, in the holdout and in all six walk-forward folds (H3).
2. **The pooled "support" is mostly strategy composition.** RSI mean-reversion signals almost always score 25–59: 2 of 106 reached 60, none of them in the holdout. That strategy lost money in both out-of-sample periods. Most momentum and pullback signals score 60–97. So a pooled "high score beats low score" comparison mostly compares strategies. Within strategies the holdout correlations are +0.02 (momentum), +0.20 (pullback) and −0.06 (RSI), all with CIs that include zero.
3. **The sign is not stable.** In the validation period the same pooled correlation was **−0.117**, and it was negative within all three strategies. It turned positive in the holdout. The regime also changed completely between the two periods (above-200DMA / low volatility, then below-200DMA / higher volatility).
4. **The 75+ band is the weakest part in practice, as PK-24 warned.** Inside the two volume-gated strategies, a `score ≥ 75` filter **cut expectancy in both out-of-sample periods**:

   | Period | momentum_breakout | trend_pullback |
   |---|---|---|
   | validation | +0.45R → +0.07R | +0.36R → −0.03R |
   | holdout | +0.19R → 0.00R | +0.10R → −0.13R |

   `score ≥ 60` was roughly neutral within those strategies. It looks better only when pooled, because it removes every RSI signal.
5. **This is not CONTRARIAN by the frozen rules.** The pooled correlation is not reliably negative.

**Recommendation for PK-26.** Show the score **descriptively only**:
- no ranking or prioritisation by it;
- no filter;
- no "strong" band shown as a positive cue.

A v1.1 research item should examine why the 75+ band underperforms inside the volume strategies (see *Extreme-Bar / Exhaustion Analysis*). PK-25 changed nothing.

**Validation of the harness:**
- 46 new tests;
- 9/9 mutants caught, and every file was restored to its sha256;
- PK-25 tests ×10 and the leakage/split/isolation subset ×20, all green;
- dataset built twice byte-identically, and it reproduces the PK-24 export on all 62 feature/score columns;
- pre-holdout stage run twice byte-identically;
- holdout stage re-verified byte-identically;
- full suite green three consecutive times.

# Frozen Score / Feature Versions

`python -m backend.smart_money_validation freeze-model` was the **first** PK-25 artifact. It ran at 2026-09-30T05:59:57Z, before any outcome was computed on real data. It wrote `model_freeze` into `research/pk25/validation_manifest.json`:

- **Versions:**
  - `SMART_MONEY_SCORE_VERSION = "1.0"`;
  - `REQUIRED_FEATURE_VERSION = 1`;
  - `FEATURE_VERSION = 1`.
- **Definitions:** the full `WEIGHTS`, `RAMPS` and `BANDS`.
- **Model file hashes** (sha256, line endings normalised CRLF→LF because of `core.autocrlf=true`):
  - `backend/smart_money_score.py` `7ca2b95d…42c7`;
  - `backend/volume_intelligence.py` `0b2a4757…2a01`.
- **Production file hashes**, taken the same way: `volume_research`, `backtest`, `backtest_costs`, `backtest_analytics`, `risk_engine`, `execution`, `strategy_engine` and all strategy modules.
- **Git HEAD:** `5391586`.
- **The statement "Score v1.0 was frozen before out-of-sample evaluation."** It is stored in the manifest and asserted by test 24.

Tests 24–26 re-check these hashes on every run:
- a score or feature change **without a version bump** fails;
- a change to strategy, risk, execution or backtest code fails unless the manifest is marked `superseded_by`. A later packet that legitimately changes those files must consciously re-validate or supersede.

**No score bug was found.** One pre-existing issue in the **PK-24 research export** was found and left unfixed, because the export is frozen:
- `SCORE_COLUMNS` names both the failed-breakout *component points* and the failed-breakout *flag* `sms_failed_breakout`.
- In `score_record` the flag overwrites the points, so the export's `sms_failed_breakout` column holds the boolean flag only.
- The score itself is unaffected, and PK-25 does not use that column (see *Known Limitations*).

# Validation Question

Does Smart Money Score v1.0 carry information about what happens after a strategy signal, specifically information that is **not already carried** by:
- which strategy fired;
- relative volume;
- a few simple PK-23 primitives?

The question is asked out of sample and chronologically, under the PK-21 realistic execution model.

# Primary Hypotheses

All were frozen in `design_freeze.design.hypotheses` before the holdout ran.

- **Primary horizon:** 10 sessions.
- **Primary set:** eligible signals, pooled. Eligible means the score is not null and the bar is not a special session.

| Tier | Id | Hypothesis |
|---|---|---|
| PRIMARY | H1 | Spearman ρ(score, 10-session forward return) > 0 on the holdout |
| PRIMARY | H2 | Median 10-session return of the 75–100 bucket > that of 45–59 |
| PRIMARY | H3 | Out-of-sample Spearman IC of M4 (strategy + RVOL20 + score) > M1 (strategy + RVOL20), on the holdout and as the mean walk-forward ΔIC |
| SECONDARY | H4 | ρ(score, MFE₁₀) > 0 and ρ(score, MAE₁₀) > 0 (a shallower adverse excursion) |
| SECONDARY | H5 | ρ(score, 10-session return) is positive in ≥ 2 strategies with N ≥ 15 and in ≥ 60% of walk-forward folds |
| SECONDARY | O | Contrarian ordering check: 75–100 vs 60–74 vs 45–59 |

- Everything else is **EXPLORATORY**: other horizons, trade R, regimes, cohorts, quintiles and robustness variants.
- The three primary one-sided p-values are Holm-corrected.

# Dataset

`backend/smart_money_validation.py build` works on a scratch copy of the backup, opened read-only and immutable:

- **Universe:** 62 NSE-cache symbols, via the unchanged `volume_research.load_universe`. 59 of them produced at least one signal.
- **Signals:**
  - `evaluate_entry` for all three strategies, on the frame cut at each bar, from 2024-09-01 to the last bar (2026-09-22);
  - **410 signals:** momentum_breakout 145, trend_pullback 159, rsi_mean_reversion 106;
  - first signal 2024-09-03, last 2026-09-17.
- **Features and score:**
  - the 39 PK-23 feature columns, `legacy_volume_pass` and the 21 PK-24 score columns, taken from `vi.compute_at` and `volume_research.score_record` on that same cut frame;
  - the data-quality codes;
  - the point-in-time market context (see below).
- **Market outcomes** (`outcome_*`, measured from the next session's open):
  - forward close return, MFE and MAE at 5, 10 and 20 sessions;
  - `outcome_window_end`, the date of bar i+20, used for purging.
- **Strategy outcomes**, per signal, **through the unmodified PK-21 `backtest.simulate`**, next-open, 5/5 bps, costs on, stop-first. There are two variants:
  - **Unconstrained** (`outcome_trade_*`): a ₹10¹² pot where the capital-based size binds at about ₹1 lakh per position, with `max_open_positions` 10⁶. Every signal that PK-21 would execute trades, subject only to `max_signals_per_day` and one position per symbol, both live semantics. Columns: `traded`, `net_r`, `net_pnl_pct`, `won`, `exit_reason`, `bars_held`, `forced_close`, and trade MFE/MAE between the entry and exit dates. Trades force-closed at the end of the data are excluded from trade statistics.
  - **Default portfolio** (`outcome_pf_*`): ₹10 lakh, max 5 positions, exactly as the PK-23/24 export.
- **Ordering:** rows are sorted by (signal_date, strategy, symbol), and every outcome column comes after every feature column.

**Parity and reproducibility:**
- **Two builds** gave byte-identical output, sha256 **`1ccadace…31f0`**.
- **The PK-24 export was re-run** on the same copy and reproduced its reported sha256 **`168289cb6efc95aa…`** exactly.
- **Joined on (strategy, symbol, signal_date),** all 410 rows match and all 62 feature and score columns are identical. Forward 10-day returns and the default-portfolio trade R also match the export exactly.
- `signal_dataset.csv` (243 kB) is committed. It holds market data and derived research values only; no account, ticket or credential data.

# Point-in-Time Integrity

- **Features and score** are computed from `df.iloc[: i+1]`.
  - Test 1 recomputes every golden-fixture score from the cut frame and rewrites every row after a signal (prices ×0.37, volume ×60+7): the whole row is unchanged.
  - Mutant M3, which scores on the full frame, is caught.
- **Outcomes** are the only values that read rows after i.
  - They start at bar i+1 (test 2), and changing bars ≤ i leaves them unchanged.
  - The 20-session values equal `volume_research.forward_outcomes` exactly (test 2b).
- **No outcome can enter a feature matrix.** `feature_matrix` raises on any `outcome_*` column, and every model fit calls it (test 3). Mutant M2 (the guard removed, an outcome added to the model features) is caught.
- **Market context** comes from NIFTYBEES rows ≤ the signal date:
  - `mkt_above_200dma` (close vs SMA-200);
  - `mkt_vol_20` (20-day realised volatility);
  - `mkt_vol_regime`: low/mid/high, from terciles of NIFTYBEES's **own prior** volatility values (expanding window, strictly earlier values, at least 120 of them).
  - Liquidity buckets use **fixed, pre-declared** cut-offs (₹5 cr and ₹50 cr prior-20 average traded value), not sample percentiles.
- **The score is v1.0 unchanged.** No normalisation of the score uses any population statistic.

# Exclusions

| Category | Count | Treatment |
|---|---|---|
| Score null (`INSUFFICIENT_DATA`) | 0 | would be excluded |
| Special-session bar (the signal bar is Muhurat, budget-day or disaster-recovery trading) | **3** | **excluded** from every primary analysis |
| Special session inside the 20-session baseline | 62 | labelled and kept; the robustness variant drops them |
| Liquidity warning (below ₹5 cr/day) | 13 | labelled and kept; the robustness variant drops them |
| `NEEDS_REFRESH` / `SUSPECT` history | 0 series | the live DB has no `ohlc_history_status` table yet (last started before PK-22), so no series is flagged; `load_universe` would exclude them |
| Ambiguous NSE/BSE series | 0 | the universe is NSE-cache tokens only; the 10 BSE-duplicate tokens are never loaded |
| Missing outcome (end of data) | 4 at 5 and 10 sessions, 7 at 20 | dropped per horizon; N is always shown |
| Sector analysis | — | **skipped**: `sector_mapping` holds 1 row |

# Chronological Split

The boundaries are **mechanical and use eligible signal dates only**: the first day of the month nearest the 45th and 70th percentiles.
- Test 6b replaces every outcome with garbage and gets the same split.
- Test 6 shows the segment is a pure, monotone function of the date.

| Segment | Dates | Signals | Eligible | Purged | Eligible, unpurged, with a 10-session return |
|---|---|---|---|---|---|
| Development | 2024-09-03 … 2025-08-31 | 181 | 178 | 18 | 160 |
| Validation | 2025-09-01 … 2026-01-31 | 127 | 127 | 30 | 97 |
| Holdout | 2026-02-01 … 2026-09-17 | 102 | 102 | 0 | 98 |

**Purging.** A development or validation row whose 20-session outcome window ends on or after the next segment's start is removed from every statistic of its segment (test 7; mutant M9 is caught). Its future prices belong to the next segment.

# Holdout Definition

- The holdout is every signal from 2026-02-01 on: 98 evaluable at 10 sessions.
- **Stage 1** (`analyze --stage pre_holdout`) sees only `pre_holdout_view`, which drops holdout rows **before** anything runs:
  - test 8 replaces holdout outcomes with garbage and with NaN, and the stage-1 files are byte-identical;
  - even holdout outcome *availability* counts are not read;
  - mutant M5 is caught.
- **The PK-21 filter run in stage 1** uses frames cut at 2026-01-31.
- **The extreme-bar scan in stage 1** uses frames cut before 2026-02-01, so no holdout price enters stage 1 at all.

**Honesty note: the holdout is not virgin data.** PK-24's in-sample appendix already looked at pooled outcomes over the **whole** Sep 2024 – Sep 2026 period, including this holdout. What makes PK-25 an out-of-sample test is therefore different:
- the score was never fitted to outcomes (PK-24);
- it was frozen by hash before PK-25 computed anything;
- every PK-25 analysis choice was frozen in the manifest before the holdout statistics were computed, and the holdout was then evaluated once.

The PK-24 appendix pooled all periods and strategies, so it could not have revealed, for example, the H3 result or the within-strategy filter behaviour.

**Timeline:**
- model freeze: 05:59:57Z;
- dataset built twice;
- pre-holdout stage run twice;
- design freeze: 06:32:32Z;
- mutations and repeats;
- holdout evaluated once: 07:46:10Z;
- `verify` re-run into a temporary directory.

`analyze --stage holdout` refuses:
- a second run into the same directory (tested);
- any change to the dataset, the analysis code, the design constants or a pre-holdout output (tested);
- any change to the frozen model or production code.

# Walk-Forward Design

- **Folds:** calendar quarters, each tested on a model trained on the **expanding, purged past**. A training row must be dated before the quarter and have its outcome window closed before it.
- **Start:** a fold runs once there are at least 80 training signals with a 10-session outcome, which makes 2025Q2 the first fold.
- **Stage 1** used only the folds before the holdout, with test rows whose windows closed before 2026-02-01.
- **Stage 2** used all six.
- **Nothing is fitted to choose anything.** The folds only show stability through time (test 8b).

| Fold | N train / test | ρ(score, ret₁₀) | IC M1 | IC M4 | ΔIC (M4−M1) | 75–100 minus 45–59 median |
|---|---|---|---|---|---|---|
| 2025Q2 | 98 / 37 | +0.201 | −0.103 | −0.262 | **−0.159** | too few |
| 2025Q3 | 116 / 63 | −0.247 | +0.209 | −0.263 | **−0.472** | −3.39 |
| 2025Q4 | 178 / 71 | +0.042 | −0.166 | −0.205 | **−0.039** | +1.11 |
| 2026Q1 | 247 / 52 | −0.154 | +0.032 | −0.123 | **−0.155** | −2.04 |
| 2026Q2 | 316 / 38 | +0.327 | +0.094 | −0.142 | **−0.237** | +2.31 |
| 2026Q3 | 354 / 38 | +0.036 | +0.170 | +0.168 | **−0.003** | +0.44 |

- **ΔIC is negative in 6 of 6 folds** (mean −0.177).
- The raw score–return correlation changes sign from quarter to quarter: 4 of 6 positive, 2 negative, ranging from −0.25 to +0.33.

# Baselines

Each ranking variable's Spearman ρ with the 10-session return. The strategy-only baseline (B1) and the z-statistics of the combination (B6) come from **training** rows.

| Baseline | Development (N = 160) | Validation (N = 97) | Holdout (N = 98) |
|---|---|---|---|
| B1 strategy alone (training-period mean per strategy) | +0.096 | −0.046 | +0.048 |
| B2 RVOL20 | +0.019 | −0.103 | −0.073 |
| B3 volume percentile (60) | +0.048 | +0.156 | +0.077 |
| B4 close location | +0.062 | −0.156 | +0.007 |
| B5 accumulation − distribution (10) | −0.067 | −0.107 | **+0.187** |
| B6 z(log RVOL) + z(close location) + z(OBV slope), unfitted | −0.060 | −0.184 | +0.017 |
| **Smart Money Score** | −0.083 | −0.117 | +0.138 |

**No baseline is stable either.**
- Every ρ above has a bootstrap CI that spans zero, in every period.
- The score is not the best ranking variable in the holdout: the simple accumulation-minus-distribution count (B5) is higher. B5 was also negative in both earlier periods.

The full per-horizon table, with quintile spreads and CIs, is in `baselines_*.csv`.

# Score Distribution

| Strategy (holdout) | N | Mean | Median | P10 | P90 |
|---|---|---|---|---|---|
| momentum_breakout | 43 | 75.5 | 76.8 | 59.7 | 87.0 |
| trend_pullback | 41 | 70.4 | 73.3 | 54.0 | 83.2 |
| rsi_mean_reversion | 18 | 45.7 | 46.0 | 40.5 | 51.6 |

The same structure appears in development (75.3 / 69.8 / 46.0) and validation (73.7 / 69.6 / 45.2).

**Bucket membership is largely strategy membership.**
- Only 2 of 106 RSI signals reached 60 (PK-24's distribution), and none in the holdout.
- Momentum and pullback signals make up almost all of 60–100.

This is the central confound of every pooled comparison.

# Score Bucket Outcomes

Frozen PK-24 buckets, primary set, 10-session horizon. The median CI is a cluster bootstrap over ISO weeks. N < 10 is flagged as too small to interpret.

| Bucket | Development: N, median ret₁₀, win % | Validation: N, median, win % | **Holdout: N, median, win % (Wilson CI), median MFE / MAE, median trade R** |
|---|---|---|---|
| 0–24 | 0 | 0 | 0 |
| 25–44 | 19, +0.02%, 53% | 12, +3.33%, 75% | 6 (*small*), −0.70%, 33% (10–70%), +2.65 / −3.72, −0.74R |
| 45–59 | 41, +0.07%, 51% | 24, +0.29%, 50% | 21, −0.66%, 29% (14–50%), +2.79 / −3.75, −0.27R |
| 60–74 | 46, −2.05%, 37% | 25, +2.56%, 76% | 25, +1.33%, 72% (52–86%), +2.79 / −3.18, +0.16R |
| 75–100 | 54, −1.08%, 28% | 36, +0.38%, 53% | 46, +0.91%, 57% (42–70%), +2.79 / −2.80, −0.16R |

- **No bucket ordering repeats across the three periods.**
  - In development, the two highest buckets were the worst.
  - In validation and the holdout, 60–74 was the best bucket and 75–100 fell below it.
  - 75–100 beats 45–59 in the holdout, but not in development.
- **Trade profit factor** (only where at least 20 trades): holdout 60–74 = 5.15, 75–100 = 0.95.

Full table (all horizons, per strategy, CIs): `buckets_*.csv`.

# Monotonicity

**Spearman ρ of the score with the outcomes.** 95% CIs are cluster-bootstrap (ISO week, 2,000 replicates, seed 25).

| Target | Development | Validation | Holdout |
|---|---|---|---|
| ret₅ | +0.103 (−0.17, +0.33) | −0.213 (−0.42, +0.10) | −0.036 (−0.24, +0.19) |
| **ret₁₀** | −0.083 (−0.30, +0.16) | −0.117 (−0.31, +0.19) | **+0.138 (−0.08, +0.37)** |
| ret₂₀ | −0.074 | −0.033 | +0.002 (−0.27, +0.26) |
| trade net R | +0.163 (−0.04, +0.36) | +0.194 (−0.07, +0.47) | +0.140 (−0.08, +0.35) |
| MFE₁₀ | +0.120 | −0.093 | +0.085 (−0.15, +0.34) |
| MAE₁₀ | −0.018 | −0.226 (−0.39, +0.01) | +0.047 (−0.20, +0.29) |

- **Every interval shown spans zero.** Outside the table, only MAE₅ excludes zero, and with opposite signs in different periods: development +0.27, validation −0.29. The sign of ρ₅ and ρ₁₀ also flips between periods.
- **The pooled trade-R correlation is positive in all three periods** (+0.14 to +0.19), but every CI spans zero. It is also confounded by strategy: RSI trades have both the lowest scores and the worst R.
- **Within strategies, the trade-R correlation is mixed.** Holdout: +0.09 momentum, −0.03 pullback, +0.10 RSI. Validation: −0.10, +0.18, −0.65. Development: −0.04, −0.14, −0.26.
- **Quintiles** (holdout ret₁₀ medians, lowest / middle / highest): −0.74% / +0.31% / +0.90%. At ret₂₀ they are −2.37% / +1.34% / −0.38%, so there is no consistent ordering across horizons.

The holdout ρ₁₀ interval here, (−0.08, +0.37), differs slightly from the H1 interval (−0.069, +0.364) only because the two tables use different pre-assigned bootstrap seed offsets.

# Spearman Results

**H1 (holdout, primary).**
- ρ = **+0.138**, N = 98, 95% CI **−0.069 … +0.364**.
- One-sided bootstrap p = 0.111; Holm-adjusted 0.223.
- The naive Fisher-z two-sided p is 0.176. It assumes independence, which overlapping signal windows violate, so it is reference only.
- **Not established.**

**H4 (secondary):**
- ρ(score, MFE₁₀) = +0.085 (CI −0.16, +0.32);
- ρ(score, MAE₁₀) = +0.047 (CI −0.21, +0.32);
- **not established.**

In validation, the score was *negatively* associated with MAE₁₀ (−0.226, i.e. deeper adverse excursions at higher scores; CI −0.39 … +0.01).

# Incremental Model Results

**Setup.**
- Models: numpy OLS on the 10-session return, raw and winsorised at the training 1st/99th percentiles; numpy logistic IRLS on "return > 0", with a fixed 1e-3 ridge (numerical only, not tuned).
- Features are standardised with training statistics, and all models are compared on identical rows.
- Nothing is persisted (test 20).

| Model | Features (+ strategy dummies) | Validation IC | Validation AUC | **Holdout IC** | Holdout AUC | Holdout OOS R² |
|---|---|---|---|---|---|---|
| M0 | — | −0.046 | 0.468 | +0.048 | 0.434 | +0.002 |
| M1 | log RVOL20 | −0.135 | 0.438 | **+0.145** | 0.477 | +0.010 |
| M2 | M1 + close location + OBV slope-20 + net accumulation-20 | −0.188 | 0.486 | −0.009 | 0.462 | −0.036 |
| M3 | score | −0.142 | 0.564 | −0.051 | 0.408 | −0.025 |
| **M4** | **log RVOL20 + score** | −0.198 | 0.556 | **+0.005** | 0.426 | −0.018 |
| M5 | M2 + score | −0.188 | 0.540 | −0.036 | 0.386 | −0.042 |

**Paired differences on the same test rows** (cluster-bootstrap CI):

| Comparison | Validation ΔIC | **Holdout ΔIC** | Holdout ΔAUC |
|---|---|---|---|
| **M4 − M1** (the score on top of strategy + RVOL) | −0.063 (−0.19, +0.05) | **−0.140 (−0.34, +0.10)** | −0.051 |
| M5 − M2 (the score on top of simple primitives) | +0.000 (−0.01, +0.01) | −0.027 (−0.06, +0.003) | −0.076 |
| M3 − M0 (the score on top of strategy) | −0.096 (−0.20, +0.05) | −0.100 (−0.30, +0.11) | −0.026 |

**H3 fails.**
- Adding the score never improves the out-of-sample IC.
- It worsens the holdout AUC in every comparison.
- It lowered the walk-forward IC in all six folds.

Two further notes:
- **No volume model is predictive either.** Every out-of-sample R² is at most +0.01.
- **Two CIs for the same M4−M1 quantity.** The hypothesis table reports −0.140 (−0.350, +0.121), while `models_holdout.csv` reports (−0.340, +0.103). Both are the same statistic bootstrapped with different pre-assigned seed offsets. The difference is Monte-Carlo noise, and the hypothesis table is the one used for classification.

# Strategy-Specific Results

Within-strategy Spearman ρ(score, ret₁₀). The CIs are in `strategies_*.csv`; all of them span zero except the one noted.

| Strategy | Development | Validation | **Holdout** | Holdout 75–100 median ret₁₀ (N) | Holdout ΔIC M4−M1 within the strategy |
|---|---|---|---|---|---|
| momentum_breakout | +0.081 (N 52) | **−0.308** (N 36; CI −0.62 … −0.09) | +0.020 (N 43) | +0.40% (27) | −0.081 |
| trend_pullback | −0.131 (N 64) | −0.121 (N 38) | +0.203 (N 41) | +1.43% (19) | −0.297 |
| rsi_mean_reversion | +0.073 (N 44) | −0.361 (N 23) | −0.064 (N 14, below the N ≥ 15 rule) | — (no holdout signal ≥ 60) | +0.110 |

**Reading:**
- **momentum_breakout** already requires volume above 3× the 20-session average (including the bar). The score adds nothing there: ρ ≈ 0 in the holdout, and a significantly negative ρ in validation.
- **trend_pullback** shows the best holdout ρ (+0.20), but it was negative in both earlier periods.
- **rsi_mean_reversion** never leaves the mixed band, so the score cannot separate its signals.
- **No strategy shows a consistent positive relationship across the three periods.**

# Market Regime Results

Regime is fully confounded with time in this sample:

| Period | Above the 200DMA (NIFTYBEES) | Volatility regime |
|---|---|---|
| Validation | 97 of 97 signals | all "low" |
| Holdout | 12 of 102 signals | 45 high, 38 mid, 19 low |

| Holdout cell | N (ret₁₀) | ρ(score, ret₁₀) | Interpretable (N ≥ 15)? |
|---|---|---|---|
| Below the 200DMA | 86 | +0.101 | yes |
| Above the 200DMA | 12 | +0.374 | no |
| High volatility | 45 | +0.299 | yes |
| Mid volatility | 38 | +0.074 | yes |
| Low volatility | 15 | −0.196 | yes (borderline) |
| 2026Q1 / Q2 / Q3 | 22 / 38 / 38 | +0.088 / +0.327 / +0.036 | yes |
| Large liquidity | 91 | +0.122 | yes |

**The positive holdout ρ comes mostly from high-volatility, below-200DMA sessions**, and in particular from 2026Q2 (ρ +0.33). In the validation period's calm, above-200DMA market the relationship was negative. The score therefore **cannot be treated as regime-independent**, and with one regime per period the data cannot separate "regime" from "period". Mid and thin liquidity cells have 3–6 signals and are not interpreted.

# Extreme-Bar / Exhaustion Analysis

**Pre-declared cohort:** a one-day return of at least +8%, RVOL20 of at least 3 and close location of at least 0.75. It is scanned over **every full-window bar** of the universe, and each bar is scored point-in-time.

| Scope | Cohort bars | Score median | Share ≥ 75 | Median ret₅ / ret₁₀ / ret₂₀ | Median MFE₁₀ / MAE₁₀ |
|---|---|---|---|---|---|
| Bars 2024-09 … 2026-01 | 18 | 76.5 | **56%** | −1.91 / **−2.71** / −3.83 | +5.52 / −5.70 |
| … of which scored 75–100 | 10 | 78.7 | 100% | −2.45 / −2.08 / −3.83 | +3.92 / −5.77 |
| Unconditional, all full-window bars (same span) | 20,080 | — | — | −0.12 / −0.05 / −0.01 | +2.88 / −3.06 |
| Bars 2026-02 onwards | 10 | 77.1 | **90%** | −2.72 / +0.74 / +3.25 | +3.16 / −5.11 |
| Unconditional (same span) | 9,148 | — | — | −0.24 / −0.41 / −0.23 | +2.96 / −3.35 |

**ADANIPOWER, the PK-24 case.** 2025-01-14: +19.3% on 11.4× RVOL, close location 0.97, **score 78.9**.
- The next 10 sessions returned **−7.9%**. Over that window the MFE was +10.8% and the MAE **−10.5%**; daily bars cannot show which came first.
- The same symbol's +19.5% bar on 2024-11-27 scored only 49.1 and returned −2.9% over 10 sessions.

**Reading (descriptive, small N):**
- The score gives such bars a high score most of the time: 56% and 90% are ≥ 75. **None** carries an exhaustion penalty, because they closed near their highs.
- Their subsequent 5-session return is clearly below the unconditional reference in both spans.
- Their MAE is deeper: about −5 to −6% against −3%.
- The early span also shows negative 10- and 20-session medians, but the holdout span does not.

This supports, **as a v1.1 research lead only**, the concern that a single extreme up-bar is read as "accumulation-like evidence" while it behaves more like short-term exhaustion. With 10–18 bars it is not a conclusion. Nothing was changed.

# Robustness Checks

Holdout ρ(score, ret₁₀) under each robustness variant. The score is untouched; outcomes are winsorised only on copies (test 21).

| Variant | N | ρ | 95% CI | Mean / median / 10%-trimmed / winsorised ret₁₀ |
|---|---|---|---|---|
| Full | 98 | +0.138 | −0.07 … +0.36 | 0.90 / 0.29 / 0.32 / 0.90 |
| Drop the 5 most frequent symbols | 67 | **+0.363** | **+0.12 … +0.62** | 1.03 / 0.40 / 0.75 / 1.03 |
| Drop the strongest market quarter (2026Q2) | 60 | **+0.039** | −0.24 … +0.33 | −0.82 / −0.33 / −0.79 / −0.87 |
| Leave out momentum_breakout | 55 | +0.199 | −0.05 … +0.44 | |
| Leave out trend_pullback | 57 | +0.094 | −0.13 … +0.35 | |
| Leave out rsi_mean_reversion | 84 | +0.110 | −0.13 … +0.36 | |
| Drop the largest absolute return | 97 | +0.139 | −0.08 … +0.38 | |
| Drop special sessions in the baseline | 87 | +0.105 | −0.11 … +0.34 | |
| Drop liquidity-warning names | 94 | +0.132 | −0.07 … +0.37 | |
| First signal per symbol and strategy within 10 sessions | 90 | +0.180 | −0.06 … +0.42 | |

**The holdout result is fragile.**
- **Removing one strong quarter erases it** (ρ +0.04).
- **Removing five symbols more than doubles it.**
- **In validation every variant was negative**, from −0.08 to −0.21.

The conclusion depends heavily on which stocks and which quarter are included. That is itself the finding.

# Statistical Uncertainty

- **Cluster bootstrap by ISO week of the signal date,** 2,000 replicates, seed 25, with fixed per-analysis seed offsets. It is deterministic (test 16).
  - Clustering matters: signals in the same week share market moves and overlapping forward windows.
  - The same bar can signal for both momentum_breakout and trend_pullback (for example SBICARD on 2024-09-03).
- **Wilson intervals** are used for win rates.
- **Naive Fisher-z p-values** are shown only as a labelled reference.
- **Holm correction** is applied to the three primary tests: H1 0.223, H2 0.111, H3 0.861. **No primary hypothesis is significant after correction.** Only H2's uncorrected one-sided p (0.037) is below 0.05.
- **No test was added, dropped or re-specified after the holdout.** Every table listed in the manifest is reported, including the unflattering ones.

# Realistic PK-21 Filter Simulation

**Setup.**
- Unmodified `backtest.simulate` with the default PK-21 config: ₹10 lakh, max 5 positions, 5/5 bps, costs, next-open.
- Each strategy runs separately, on frames cut at the period end.
- The filter is a research wrapper (`ScoreFilteredStrategy`) around the strategy module. It scores the **same prefix frame** that the strategy just evaluated, so it is point-in-time.
  - Test 23b shows that a 0 threshold reproduces plain PK-21 trades exactly, and that the wrapper's scores equal the dataset's.
  - Mutant M8 (the filter bypassing the simulator) is caught.
- The filter acts before ranking and the daily cap, as a scanner filter would.
- **Net CAGR is not meaningful:** both periods are shorter than 365 days, so the metrics return null.

| Period | Strategy | All: trades, net R, expectancy, win %, PF, MTM DD | Score ≥ 60 | Score ≥ 75 |
|---|---|---|---|---|
| Validation 2025-09-01 … 2026-01-31 | momentum_breakout | 24, +10.8R, **+0.45**, 67%, 3.15, 1.28% | 24, +10.6R, +0.44, 63%, 3.02, 1.28% | 20, +1.4R, **+0.07**, 55%, 1.20, 1.44% |
| | trend_pullback | 26, +9.2R, **+0.36**, 58%, 2.99, 1.72% | 22, +10.2R, +0.46, 64%, 3.31, 1.54% | 16, −0.4R, **−0.03**, 44%, 0.90, 1.23% |
| | rsi_mean_reversion | 24, −11.4R, −0.48 | 1, −1.1R | 0 |
| Holdout 2026-02-01 … 2026-09-22 | momentum_breakout | 28, +5.2R, **+0.19**, 57%, 1.75, 1.90% | 26, +4.7R, +0.18, 54%, 1.74, 1.32% | 20, −0.1R, **−0.00**, 50%, 1.00, 1.37% |
| | trend_pullback | 28, +2.7R, **+0.10**, 50%, 1.45, 1.42% | 25, +2.2R, +0.09, 56%, 1.37, 1.39% | 16, −2.1R, **−0.13**, 44%, 0.61, 1.77% |
| | rsi_mean_reversion | 16, −5.2R, −0.33 | 0 | 0 |

**Pooled holdout**, where expectancy is trade-weighted and DD is the worst per-strategy DD:

| Scenario | Trades | Net R | Expectancy | Worst DD |
|---|---|---|---|---|
| All | 72 | +2.7R | +0.038 | 1.90% |
| ≥ 60 | 51 | +6.9R | +0.136 | 1.39% |
| ≥ 75 | 36 | −2.2R | −0.060 | 1.77% |

**Missed winners / avoided losers** (holdout, unconstrained trade outcomes of the filtered-out signals):

| Strategy | Filter ≥ 60 | Filter ≥ 75 |
|---|---|---|
| momentum_breakout | 1 missed / 3 avoided | 8 missed / 6 avoided |
| trend_pullback | 1 missed / 6 avoided | 8 missed / 10 avoided |
| rsi_mean_reversion | 4 missed / 10 avoided | the same |

**Reading.** The pooled "≥ 60 is better" result is **entirely** the removal of rsi_mean_reversion, which the score does implicitly because RSI signals almost never reach 60 (validation 1 trade, holdout 0). A strategy switch would do the same thing more simply. **Within the two volume strategies:**
- ≥ 60 is neutral in both periods;
- **≥ 75 cut expectancy sharply in both periods and both strategies.**

**Capital utilisation.** The ≥ 75 filter lowered exposure (holdout momentum_breakout: 17% → 12%) without improving results. See `filter_simulation_*.csv` for exposure, time in market and skipped signals.

# Contrarian Test

**Secondary test O.** Medians of the 10-session return by bucket, with the cluster-bootstrap CI of the difference:

| Set | 75–100 | 60–74 | 45–59 | 75+ minus 60–74 | 75+ minus 45–59 |
|---|---|---|---|---|---|
| Validation, pooled | +0.38 (36) | +2.56 (25) | +0.29 (24) | −2.18 (−10.4, +0.2) | +0.09 |
| Validation, momentum_breakout | −0.21 (20) | +4.78 (12) | +3.59 (4) | **−4.99 (−9.9, −1.3)** | −3.80 |
| Validation, trend_pullback | +0.49 (16) | +1.57 (13) | +0.63 (8) | −1.08 | −0.14 |
| Holdout, pooled | +0.91 (46) | +1.33 (25) | −0.66 (21) | −0.41 (−2.7, +1.9) | +1.57 (−0.2, +3.3) |
| Holdout, momentum_breakout | +0.40 (27) | +1.33 (11) | −1.04 (5) | −0.93 | +1.44 |
| Holdout, trend_pullback | +1.43 (19) | +1.07 (14) | −0.45 (7) | +0.36 | +1.88 |

- **At 20 sessions** (holdout, pooled), 75+ trails 60–74 by **−2.94 pp** (CI −9.4, +0.6).

**Reading:**
- **75–100 is never better than 60–74** on the pooled 10- or 20-session median in either out-of-sample period.
- Within momentum_breakout it was significantly worse in validation.
- **The score is non-monotonic at the top**: 60–74 is the best-performing band in both validation and holdout.
- This is a consistent *direction* but not the pre-declared CONTRARIAN pattern: the overall correlation is not negative, and 75+ still beats 45–59 in the holdout.
- **Stated plainly:** very high scores do not add value over moderately high ones, and inside the volume strategies they are associated with weaker trade results. **Nothing was inverted or acted on.**

# Final Holdout Results

`research/pk25/holdout_results.json`, produced once, at 07:46:10Z:

| Input | Value |
|---|---|
| N (10-session) | 98 |
| ρ₁₀ and CI | +0.138 (−0.069, +0.364) |
| H2 difference | +1.57 pp |
| ΔIC M4−M1 and CI | −0.140 (−0.350, +0.121) |
| Walk-forward mean ΔIC; share of folds positive | −0.177; 0 of 6 |
| Walk-forward share of folds with ρ > 0 | 4 of 6 (0.67) |
| Walk-forward share of folds with H2 > 0 | 3 of 5 (0.60) |
| Strategies with positive / negative ρ (N ≥ 15) | 2 / 0 |
| ≥ 60 filter, DD ratio | 0.73 |
| ≥ 60 filter, expectancy change | +0.098R |

# Decision Criteria

`classify()` is a pure, tested function. It was frozen in the manifest together with its thresholds, and the first rule that matches wins:

1. **INCONCLUSIVE (sample)** if the holdout N₁₀ is below 60.
2. **CONTRARIAN** if all of these hold:
   - ρ₁₀ ≤ −0.10 and the CI upper bound < 0;
   - 75–100 has a lower median than 45–59;
   - ρ₁₀ is negative in ≥ 2 strategies (N ≥ 15) or in ≥ 60% of folds.
3. **PROMISING** if all of these hold:
   - ρ₁₀ ≥ 0.10 and the CI lower bound > 0;
   - 75–100 has a higher median than 45–59;
   - ΔIC(M4−M1) ≥ 0.03 and the mean walk-forward ΔIC > 0;
   - ρ positive in ≥ 2 strategies and in ≥ 60% of folds;
   - the ≥ 60 filter has DD ≤ 1.25 × all-signals and expectancy ≥ all-signals.
4. **NO EDGE** if all of these hold:
   - the ρ₁₀ CI contains 0;
   - |ρ₁₀| < 0.10;
   - the ΔIC CI contains 0, or ΔIC ≤ 0;
   - the ρ₁₀ CI half-width is ≤ 0.15, i.e. precise enough to have seen a moderate effect.
5. **LIMITED** if any of H1, H2 or H3 is *directionally* supported: the point estimate is in the hypothesised direction **and** the walk-forward sign agrees in ≥ 60% of folds.
6. **INCONCLUSIVE** otherwise.

Tests cover every label, the rule order, and the drawdown clause.

# Final Classification

## **LIMITED**

**Why not the other labels:**
- **Not PROMISING:** the H1 CI lower bound (−0.069) is not > 0, ΔIC is −0.140 rather than ≥ 0.03, and the mean walk-forward ΔIC is −0.177, not > 0.
- **Not CONTRARIAN:** ρ₁₀ is positive and no strategy is negative with N ≥ 15.
- **Not NO EDGE:** |ρ₁₀| = 0.138 is not below 0.10, and the CI half-width of 0.217 is wider than 0.15.

**Why LIMITED:**
- **H1 is directional:** ρ > 0 and 67% of folds are positive.
- **H2 is directional:** the difference is > 0 and 60% of folds are positive.
- **H3 is not directional.**

The label was produced mechanically. It was not chosen or adjusted after viewing the results.

# Practical Interpretation

The frozen rule says LIMITED, and the next-step text attached to that label is exactly right: *"PK-26 may show the score descriptively but must not prioritise signals strongly by it."*

The evidence behind the label is weaker than "some useful relationship" might suggest:

- **The directional support is pooled and confounded by strategy.**
  - The score mostly sorts RSI signals below momentum and pullback signals.
  - RSI mean-reversion lost money in both out-of-sample periods; it is disabled by default in live use.
  - Conditional on the strategy (M3−M0, M4−M1, M5−M2, and the within-strategy ρ), the score adds **no** measurable information, and in the models it slightly *hurts*.
- **The holdout's positive ρ is fragile.** It was negative in validation and near zero in development. It disappears when one strong quarter (2026Q2) is removed. It is concentrated in the high-volatility, below-200DMA regime of that period.
- **The top band is not a quality signal.**
  - For both volume strategies, a `score ≥ 75` filter reduced expectancy in both out-of-sample periods.
  - 60–74 outperformed 75–100 on pooled medians in both periods.
  - Extreme single-day up-bars mostly score ≥ 75 and are followed by weaker short-term returns and deeper MAE.

In practical terms: **treat the score as a description of the bar, not as a ranking.**

# What The Result Does NOT Prove

- **It does not prove that the score is useless** in every regime or universe. With 98 holdout signals, a true ρ of about 0.15 could not be distinguished from 0.
- **It does not prove that 75+ scores predict losses.** That pattern is consistent in direction but mostly not significant, is based on 16–36 trades per cell, and is not a basis for any short or avoidance rule.
- **It does not validate a "score ≥ 60" filter.** Its pooled benefit is the removal of one strategy.
- **It does not test institutional activity.** Nothing in daily OHLCV can.
- **It does not escape PK-24's prior look at pooled outcomes over the same period**, nor survivorship bias in the current watchlist universe.
- **It covers one strategy parameter set** (the defaults) and one execution model (PK-21 next-open).

# Recommended Next Step

For PK-26, when separately approved, per the frozen LIMITED rule:
- **show the score and its components descriptively only**;
- **no ranking, sorting, filtering or prioritisation** by it;
- no presentation of the "strong" band as a positive cue.

Separately, and **not** in PK-26, a future **v1.1 research packet** could investigate:
- (a) why 75+ underperforms 60–74 inside the volume strategies;
- (b) extreme single-bar moves (the exhaustion lead above);
- (c) whether the score is redundant with the strategies' own volume rules.

That packet would need a **new** frozen holdout of post-freeze data (signals after 2026-09-22), because this holdout has now been used.

PK-25 implements none of this.

# Tests Added

`tests/test_smart_money_validation.py`: **46 tests**, including parametrisations.

- **Data:**
  - 1: the score is fixed at the signal date and unchanged by any future-row rewrite;
  - 1b: parity with the PK-24 export columns;
  - 2 / 2b: outcomes start at i+1, bars ≤ i cannot move them, and the 20-session values equal `forward_outcomes`;
  - 3: the feature matrix refuses outcomes, and outcomes come last in the file;
  - 4: exclusions and counts;
  - 5: deterministic ordering under reversed input.
- **Splits:**
  - 6: chronological, monotone and shuffle-invariant;
  - 6b: split dates ignore outcomes;
  - 7: purge;
  - 8: stage-1 files are byte-identical with holdout outcomes replaced by garbage or NaN, while the holdout stage does change;
  - 8b: walk-forward trains only on the past.
- **Buckets:** 9 frozen edges; 10 exact counts; 11 empty and small buckets.
- **Metrics:** 12 medians, trimmed mean, winsorised copy; 13 win rate, Wilson interval, profit factor; 14 MFE/MAE medians; 15 Spearman with ties; 16 deterministic bootstrap and Holm.
- **Incremental:**
  - 17: baselines contain no score;
  - 18: the score models add exactly the score;
  - 18b: sanity — ΔIC appears when a signal is planted and not otherwise;
  - 19: OLS against `lstsq`, logistic regression against known coefficients, AUC against a brute-force count;
  - 20: no model persisted, no pickle or sklearn, and no live file (all of `backend/`, `DECISION_PATHS`, `frontend/app.js`) references the harness.
- **Robustness:** 21 the dataset is unaltered and winsorising works on a copy; 22 leave-group-out is deterministic.
- **PK-21:**
  - 23: the filter calls the real `simulate` with frames cut at the period end;
  - 23b: the 0-threshold wrapper reproduces PK-21 exactly, scores equal the dataset's, and a 101 threshold yields no trades;
  - 23c: the unconstrained config and the trade-outcome fields.
- **Isolation:** 24 score and feature files against the frozen hashes and versions; 25 strategy files; 26 risk, execution and backtest files; the line-ending-insensitive hash.
- **Protocol:**
  - the design freeze detects dataset, output and constant changes;
  - the holdout runs once and refuses on any mismatch;
  - pre-holdout and freeze cannot be redone after the freeze;
  - the classifier returns each label in rule order;
  - PROMISING requires the drawdown clause;
  - the hypotheses are tiered.

No existing test was changed.

# Mutation Results

`mutations.py` (scratchpad) edits one file, runs the PK-25 file, then restores the original bytes and verifies the sha256. It matches either newline style.

It was run twice:
- once before the pre-holdout fixes (9/9);
- once on the frozen code, after the design freeze (9/9).

| # | Mutation | File | Result | First catching test |
|---|---|---|---|---|
| M1 | random rather than chronological split | validation | **caught** | 06 chronological |
| M2 | outcome column leaks into the features, guard removed | validation | **caught** | 03 feature matrix |
| M3 | score recalculated on the full (future) frame | validation | **caught** | 01 point-in-time |
| M4 | bucket boundary changed after the holdout (75 → 70) | validation | **caught** | 09 frozen buckets |
| M5 | holdout rows included in the development statistics | validation | **caught** | 07 purge / 08 isolation |
| M6 | production score weight modified (participation 5 → 6) | `smart_money_score.py` | **caught** | 24 frozen v1.0 |
| M7 | strategy logic changed (volume multiple 3.0 → 2.5) | `momentum_breakout.py` | **caught** | 25 strategy unchanged |
| M8 | PK-21 execution bypassed for the filter comparison | validation | **caught** | 23 real simulator |
| M9 | purge removed | validation | **caught** | 07 purge |

**9/9 caught**, and all files were restored to their recorded sha256.

# Reproducibility

| Check | Result |
|---|---|
| Dataset `build` ×2 | byte-identical, sha256 `1ccadace…31f0` |
| PK-24 export re-run | sha256 `168289cb6efc95aa…`, identical to PK-24's report; 410/410 rows and 62/62 feature/score columns equal to the PK-25 dataset |
| Pre-holdout stage ×2 (final code) | all 13 files byte-identical (the second run used a separate output directory holding only the model freeze) |
| Holdout stage | run **once**; `verify` recomputed it in a temporary directory and all **13/13 files were identical** (e.g. `holdout_results.json` sha256 `b4561ff1…68de1`) |
| PK-25 tests ×10 | **10/10 green**, 46 passed each (about 47 s). Run 4's wall time was 2,695 s with the same 46 passes: the host was evidently suspended mid-run, and per-test timings in every other run are unchanged. |
| Leakage/split/isolation/protocol subset ×20 | **20/20 green**, 20 passed each (about 38 s) |

**Order of events after the first pre-holdout pass.** Stage 1 (pre-holdout) was run, inspected for **bugs only**, and three non-result changes were made before the design freeze:
1. the stage-1 JSON no longer reads even the *availability* of holdout outcomes, and test 8 now also uses NaN;
2. a filter column was renamed to `evaluable_signals`, because it counts eligible, unpurged signals, not all signals the simulator sees;
3. the extreme-bar table gained a `scope` column.

No hypothesis, bucket, model, threshold or criterion changed. Stage 1 was then re-run twice (identical), and only the three affected files changed.

# Full Suite ×3

After the last code change:

| Run | Result |
|---|---|
| 1 | **1,461 passed**, 0 failed (308 s) |
| 2 | **1,461 passed**, 0 failed (223 s) |
| 3 | **1,461 passed**, 0 failed (227 s) |

1,461 = the 1,415 baseline + 46 PK-25 tests; 0 xfailed, 0 xpassed. The live DB sha256 afterwards is `1378f8b4…dacf`.

# Known Limitations

1. **Small sample.** 98 holdout signals, 14–43 per strategy. A modest true effect (ρ ≈ 0.1–0.15) is undetectable at this size.
2. **One regime per period.** The validation period was entirely above the 200DMA in low volatility, and the holdout was mostly below it in higher volatility. Regime and period cannot be separated.
3. **NIFTYBEES is a proxy** for the market (an ETF, not the index), and the universe is today's watchlist (survivorship bias, as in PK-21).
4. **The holdout overlaps PK-24's in-sample appendix** (see *Holdout Definition*).
5. **Overlapping signals.** The same bar can signal two strategies, and nearby signals share market moves. The ISO-week cluster bootstrap addresses this only approximately.
6. **Unconstrained trade outcomes** use a research configuration (about ₹1 lakh per position, no capital competition). They are per-signal labels, not a portfolio result; the portfolio view is the filter simulation.
7. **The extreme-bar comparison** uses all bars as the unconditional reference, not same-score non-cohort bars. Scoring every one of about 45,000 bars would have taken about 12 minutes per stage. The signal-level "non-cohort 75–100" group gives a same-band comparison within the evaluation set.
8. **PK-24 export column collision** (pre-existing, not fixed, because `volume_research.py` is frozen): `sms_failed_breakout` holds the boolean flag, overwriting the failed-breakout component points. It is harmless to PK-25, which does not use that column. A later packet could rename the flag, e.g. `sms_failed_breakout_flag`, which would change the export's bytes.
9. **Liquidity cells** mid and thin have 3–6 signals and are not interpreted; the sector analysis was not possible.
10. **Net CAGR** is not reported, because both evaluation periods are shorter than 365 days.

# Deviations From Plan

1. **Stage 1 was re-run after bug-level fixes** (see *Reproducibility*), before the design freeze. No analysis choice changed.
2. **The extreme-bar comparison group** is the unconditional all-bar reference, not "non-cohort bars in the same score band" (limitation 7).
3. **Stats helpers were split** into `backend/smart_money_validation_stats.py` (pure numpy), as the plan allowed. `smart_money_validation.py` is about 1,750 lines.
4. **The design-freeze check also compares the design constants** themselves, not only the code hash. This is stricter than planned.
5. `docs/OPUS55_AUDIT_REPORT.md` was not modified. PK-26 was not started.
