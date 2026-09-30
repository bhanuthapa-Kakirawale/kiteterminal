# OPUS55 PK-23 Implementation Report: Volume Intelligence Engine (features and explanations only)

| | |
|---|---|
| Packet | **PK-23** only. PK-24 and later were not started. No strategy module, threshold, scan, sizing, risk or exit path was changed. |
| Baseline before | **1,239 passed**, 0 failed, 0 xfailed, 0 xpassed, measured on the untouched code (`a9e7083`) before the first edit. |
| Backups | git tag `backup-pre-pk23` and branch `backup/pre-pk23` at `a9e7083`. `backend/kite_terminal.db.pre_pk23_bak` made with the SQLite online-backup API from a read-only connection: `integrity_check` ok, 28 tables, 18 trade tickets, 50,556 daily candles, sha256 `01a99048…7216`. Not committed (`*_bak` is git-ignored). |
| After | **1,317 passed**, 0 failed, 0 xfailed, 0 xpassed, three consecutive times (1,239 + 78 new PK-23 tests). |
| Live DB | `backend/kite_terminal.db` sha256 `1378f8b4…dacf`, identical before and after all work. It was never opened by app code. See *Known Limitations* 9 for the WAL sidecar note. |

# Executive Summary

PK-23 adds one authoritative, pure module, `backend/volume_intelligence.py`. It turns a symbol's stored daily OHLCV into versioned, descriptive volume features and a few factual sentences about them. The features cover:

- relative volume (mean and median), the volume percentile and a z-score;
- traded value and liquidity;
- bar structure (close location and wicks);
- start-invariant OBV and its divergence;
- contraction and expansion;
- pullback and breakout participation;
- accumulation-/distribution-like evidence counts;
- abnormal-volume and shock flags.

It also adds:

- a read-only endpoint, `GET /api/volume-intelligence/{symbol}`;
- a **Volume context** section in Signal Detail, computed as of the signal bar;
- a research harness, `backend/volume_research.py`, that exports features at every historical strategy signal, with forward outcomes in separate `outcome_*` columns.

Key properties, each tested:

- **Baselines exclude the current bar.** A 10× spike reads RVOL20 = 10.0, never 1000/145.
- **No lookahead.** The historical path (`compute_series` over all history) and the as-of path (`compute_at` on history cut at T) agree on every row. Perturbing any row after T changes nothing at or before T.
- **Untrusted history is not computed on.** The following all give `UNAVAILABLE` with the reason:
  - PK-22 `SUSPECT` or `NEEDS_REFRESH` status;
  - an unverified split-sized gap inside the feature window;
  - NaN or negative volume, an impossible candle, or a duplicate date.
- **Strategies are untouched.** The existing rule (`volume > 3× SMA-20` *including* the signal bar) is only reproduced side by side, and it equals the strategy's own `volume_multiple_actual` to 1e-12 on every bar of the golden fixture.

Validation:

- **Mutations:** 15/15 focused mutants caught, each file restored and sha256-verified.
- **Targeted repeats:** PK-23 suites ×10 and the no-lookahead subset ×20, all green.
- **Research export:** deterministic, with byte-identical output on two runs over the real history.
- **Historical sanity:** all six examples match an independent plain-Python recomputation.
- **Browser:** 26/26 checks under enforced CSP.
- **Full suite:** green three consecutive times.

# Existing Strategy Volume Usage

Verified in the current code; nothing here was changed.

| Strategy | Volume rule | Where | Does the average include the signal bar? |
|---|---|---|---|
| `momentum_breakout` | `volume_T > volume_multiple × SMA-20(volume)[T]`, `volume_multiple = 3.0` | `strategies/momentum_breakout.py:72-80` | **Yes.** `indicators.avg_volume` is `rolling(20).mean()`, so T is in its own average. |
| `trend_pullback` | The same rule on the recovery bar, `volume_multiple = 3.0` | `strategies/trend_pullback.py:107-110` | **Yes**, identically. |
| `rsi_mean_reversion` | **None.** Volume only appears in the chart's generic volume subpanel. | `signal_chart.py:144-156` | n/a |

**What including the bar means exactly.**
- Algebra: `v > m·(v + S19)/20 ⇔ v > 19m/(20−m) × mean(prior 19)`.
- For `m = 3` that is **57/17 = 3.353×** the prior-19 average.
- The audit's "about 3.4×" is confirmed and made exact: `test_legacy_rule_equals_57_over_17_of_the_prior_19_mean` checks both sides of the boundary.

The prior-only RVOL20 is therefore always the larger number on a spike bar. For example, on the Signal Detail fixture it is 3.60× prior-only against 3.19× under the strategy rule.

# Data Source / Volume Semantics

PK-23A was a read-only audit of the backup, opened `immutable=1`.

| Question | Finding |
|---|---|
| Providers | **Kite only**: `kite.historical_data(token, …, "day")` via `backend/ohlc.py`. There is **no Yahoo or NSE fallback** in the codebase (grep: none). The scanner, backtester, chart and new engine all read `ohlc_daily` through `indicators.load_ohlc_frame`. |
| Units | Daily traded **quantity in shares**, stored as `INTEGER NOT NULL`. All 50,556 rows are `integer`. |
| Adjusted history | Kite back-adjusts prices for splits and bonuses, and dividends are not adjusted (PK-22). Whether Kite also adjusts **volume** cannot be proven from the code or the data: the stored history has no confirmed split (72 instruments, no rebuild, no transition). PK-23 does not rely on it: a split-sized gap in the window blocks the features (see *Corporate-Action Interaction*). |
| Missing volume | Impossible in the DB (`NOT NULL`). The engine still validates it, because frames can come from elsewhere (harness, tests). |
| Zero-volume rows | **0** of 50,556. |
| Negative volume / high < low / OHLC outside range / zero-range bars | **0 / 0 / 0 / 0.** |
| Duplicate dates | **0.** A unique index `(instrument_token, date)` prevents them in the DB. |
| Still-forming bar | Never stored: `ohlc._final_rows` drops bars newer than the last completed session, and the overlap re-fetch corrects mid-session captures. |
| Lookback | 72 instruments, 146–754 bars. The scanner requires 200 bars, so scanned signals always have the 61 bars the engine needs. |
| Special sessions | 6 weekend dates plus 2 weekday Muhurat dates carry candles. **Muhurat and disaster-recovery sessions trade 4–13% of a normal day's volume** (median per session, measured). The full Saturday session of 2024-01-20 is 39% and the two budget days are 68–80%. See *Data Quality*. |
| **Same symbol, two tokens** | **6 symbols** (AWL, UPL, SBICARD, ITBEES, FRACTAL, ATHERENERG) are stored under an NSE token (low byte 1) **and a BSE token** (low byte 4, pulled in via holdings, absent from the NSE instrument cache). Their volumes differ 10–40×; for example UPL has a median of 54k on BSE against 2.1M on NSE. The engine is therefore keyed by **instrument token**, and the API resolves a symbol deliberately (see *API*). |
| History status | The live DB has **no `ohlc_history_status` table yet**: it was last started before PK-22, and v4 creates the table on the next start. The engine treats an absent status as unknown and still applies its own in-window gap check. |

# Volume Feature Architecture

- **`backend/volume_intelligence.py`** is the one authoritative module.
  - **Pure core:** pandas and numpy only. It makes no DB call and no broker call, and it **never mutates its input** (tested).
  - `validate(frame, history_status, verified_gaps)` gives the data quality of the as-of window.
  - `compute_series(df)` computes every feature for every row, with row T built only from rows ≤ T. This is the backtest and research path.
  - `compute_at(df, as_of, …)` cuts the frame at `as_of` **before** computing, validates, computes on the last `LOOKBACK_BARS = 61` rows and returns the `VolumeFeatures` object. This is the live, API and UI path.
  - `strategy_volume_comparison` and `interpret` are the side-by-side diagnostics and the factual sentences.
  - **DB-reading wrapper** (separate section, reads only): `features_for_instrument(conn, token, symbol, as_of, strategy, params)`. It applies the PK-22 status and verified gaps, adds freshness, and holds a bounded LRU cache.
- **Window-local by construction.** No feature needs more than the current bar plus 60 prior sessions. `test_28b` computes the full-history series and the 61-bar-window result for **every row** of a 260-bar random walk and requires them to be equal. Any feature that silently needed older data would fail it.
- **Consumers.**
  - The API and Signal Detail call `features_for_instrument`.
  - The research harness calls `compute_at`.
  - The strategies, scanner, backtester simulation, risk engine and execution **do not import it**. `test_no_decision_path_consumes_volume_intelligence` checks the source of all nine files.

# Relative Volume

Every baseline is the prior N completed sessions, excluding T (`v.shift(1)` before the rolling window).

| Field | Definition |
|---|---|
| `avg_volume_20`, `median_volume_20` | mean and median of the prior 20 sessions |
| `rvol_5`, `rvol_10`, `rvol_20` | `v_T / mean(prior N)` |
| `rvol_median_20` | `v_T / median(prior 20)`. It resists one extreme prior session (test 3: 3.0× against 0.50× by mean). On real data it separates skewed cases, for example BIRET-RR 2025-05-02 at 0.79× mean-based and 1.92× median-based. |
| `volume_zscore_20` | `(v_T − mean) / sd(prior 20, ddof = 1)`. It is `None` when the sd is 0. |
| `legacy_volume_multiple_20` | `v_T / SMA-20` **including** T, which is the strategies' number and is reported only for comparison |

A zero baseline gives `None`, never infinity, and adds `ZERO_BASELINE`.

# Volume Percentile

- **Formula:** `volume_percentile_60 = 100 × (#prior < v + 0.5 × #prior == v) / 60`, a mid-rank over the **60 prior sessions only**.
- **Tests:**
  - exact values: 45 of 60 below gives 75.0; a tie gives 74.17; above all gives 100; below all gives 0;
  - determinism;
  - a direct check against a hand-built window on a random walk;
  - a spike planted at T+1 leaves T unchanged (test 29).
- **Implementation:** numpy `sliding_window_view` over `v[:-1]`, so the reference window can never contain the current bar. Mutant M2a (window includes T) and mutant M2b (ranked against the following 60 rows) are both caught.

# Traded Value / Liquidity

- **Fields:**
  - `traded_value = close × volume` (₹);
  - `avg_traded_value_20` (prior 20);
  - `relative_traded_value_20`;
  - `liquidity_warning`: the prior-20 average traded value is below `config.VOLUME_INTEL_LOW_LIQUIDITY_INR`, which defaults to **₹5 crore per day**.
- **What the warning does:** it is analytics only. Nothing rejects, sizes or ranks a trade by it, and its threshold is echoed in every result.
- **No better value field exists:** Kite daily candles carry no traded-value field.
- **On the real history,** 11 series have a median 20-day traded value below ₹5 crore. They are mostly InvITs, REITs, thin ETFs and the BSE duplicates.

# Price-Volume Structure

| Field | Definition |
|---|---|
| `close_location` | `(C − L)/(H − L)`, from 0 (closed at the low) to 1 (closed at the high) |
| `body_pct`, `upper_wick_pct`, `lower_wick_pct` | shares of the bar's range |
| `range_pct`, `true_range_pct` | as a % of the previous close |
| `price_change_pct`, `gap_pct` | close-to-close change and open-vs-previous-close gap |
| `high_volume_strong_close` | RVOL20 ≥ 1.5 and close location ≥ 0.75 |
| `high_volume_weak_close` | RVOL20 ≥ 1.5 and close location ≤ 0.25 |
| `high_volume_upper_wick` | RVOL20 ≥ 1.5, upper wick ≥ 0.5 of the range and close location ≤ 0.5 |

- **Zero-range bar:** `close_location`, the wicks and the body are `None`, and `ZERO_RANGE_BAR` is set. The close-dependent flags are `False`, not `None`, because the bar has no close position.
- **Flag with unknown volume:** a flag is `None` only when its RVOL is unknown.
- **Thresholds:** all of them live in `DEFAULT_THRESHOLDS`, can be overridden per call (an unknown key raises), and are returned in every result.

# OBV

- **Standard rule:** add volume on an up-close, subtract it on a down-close, leave OBV unchanged on an equal close. The first row's direction is *unknown*, not flat.
- **Only start-invariant derivatives are exposed.** A cumulative OBV level depends on where the stored history begins, so it is meaningless on its own and is not in the output.
  - `slope_5` and `slope_20`: `Σ signed volume / Σ volume` over the window, from −1 to +1. A value of +0.35 means net 35% of the period's volume came on up-closes minus down-closes.
  - `trend`: `rising` or `falling` when `|slope_20| ≥ 0.1`, otherwise `flat`.
  - `obv_new_high_20` and `price_new_high_20` compare T against the prior 20 sessions.
- **Divergence rules (explicit):**

  | OBV new high? | Price new high? | Label |
  |---|---|---|
  | yes | yes | `confirmed` |
  | yes | no | `obv_leads_price` |
  | no | yes | `price_unconfirmed` |
  | no | no | `none` |

  Neither label is described as institutional activity.

# Volume Contraction / Expansion

- `contraction_ratio_5_20` = mean(prior 5) / mean(prior 20). `pre_breakout_volume_contraction` is true when it is ≤ 0.8.
- `volume_slope_10` is the least-squares slope of the prior 10 volumes, divided by their mean (fraction per session).
- `current_volume_expansion` is RVOL20 ≥ 1.5.
- The contraction and expansion flags are **separate fields**. "Quiet base, then expansion" is both at once, and is not defined as a signal (test 17).

# Accumulation-Like Evidence

Each bar is judged against **its own** prior-20 baseline.

| Label | Rule |
|---|---|
| Elevated volume | RVOL20 ≥ 1.5 |
| `up_volume_day` | elevated and close > previous close |
| `accumulation_like` | elevated, close > previous close and close location ≥ 0.75 |

Rolling counts cover the 10 and 20 sessions ending at T:
- `accumulation_like_10` and `accumulation_like_20`;
- `up_volume_days_20`;
- `net_accumulation_10/20` (accumulation-like minus distribution-like).

A count is `None` if any bar in its window has an unknown baseline. Wording is neutral, for example "3 accumulation-like vs 1 distribution-like sessions in the last 10".

# Distribution-Like Evidence

| Label | Rule |
|---|---|
| `down_volume_day` | elevated and close < previous close |
| `distribution_like` | elevated, close < previous close and close location ≤ 0.25 |
| `high_volume_reversal` | elevated, upper wick ≥ 0.5 of the range and close location ≤ 0.5 |

The rolling counts `distribution_like_10/20`, `down_volume_days_20` and `high_volume_reversals_20` follow the same rules as above. Mutant M5, which swaps the accumulation and distribution rules, is caught by 4 tests.

# Breakout Participation

- **When it applies:** the close is above the prior-N closing high, where N = `breakout_lookback`, default 30 (momentum_breakout's Donchian) and computed with the unchanged `indicators.donchian_high_excl_today`.
- **What the `breakout` object reports:**
  - `level` and `close_above_level_pct`;
  - `close_quality`, which is the close location;
  - `volume_ratio` (prior-only RVOL20) and `volume_percentile_60`;
  - `traded_value_ratio`;
  - `base_contraction_ratio`.
- **Signal Detail** shows it next to the strategy's own rule, for example: "Strategy rule: 3.19× its 20-day average *including* the signal bar (needs more than 3.00×, met); prior-only RVOL20 is 3.60×."

# Pullback Participation

- **Definitions** (explicit, using bars ≤ T only):
  - swing high: the highest close of the **20 sessions before T**, the most recent one on a tie;
  - pullback bars: the bars after the swing high up to T−1. A pullback exists only if the close at T−1 is below the swing high's close;
  - bar T is the potential recovery bar, which is the bar trend_pullback's signal is measured on.
- **Fields:**
  - `pullback_bars` and `pullback_depth_pct`;
  - `pullback_volume_ratio`: pullback volume divided by the average of the 20 sessions ending at the swing high;
  - `pullback_vs_advance_volume`: pullback volume divided by the average of the up-close bars in the 10 sessions into the high;
  - `recovery_volume_ratio`: `v_T` divided by the pullback's average volume;
  - `is_recovery_bar`;
  - `low_volume_pullback`: `pullback_volume_ratio` ≤ 0.8.
- **Not wired to trend_pullback.** Its own EMA-touch rule is not replicated or altered; these fields are generic context.

# Abnormal Volume

Percentile- and median-based, because volume is skewed. The z-score is reported but not relied on.

| Flag | Rule |
|---|---|
| `abnormal_volume` | percentile ≥ 90 and median-based RVOL20 ≥ 2 |
| `extreme_volume` | percentile ≥ 98 and median-based RVOL20 ≥ 3 |
| `negative_shock` | abnormal, return ≤ −3% and close location ≤ 0.25 |
| `positive_shock` | abnormal, return ≥ +3% and close location ≥ 0.75 |

All four are descriptive only.

# Feature Schema / Version

`VolumeFeatures` is a JSON object with finite floats rounded and NaN as `null`:

```
feature_version (=1), symbol, instrument_token, as_of_date, requested_as_of, history_status,
data_quality {status: OK|DEGRADED|UNAVAILABLE, codes[], bars_available, bars_used, bars_required (21),
              bars_for_full_features (61), detail{}},
warnings[], notes[],
bar, relative_volume, liquidity, structure, obv, contraction, pullback, evidence{today, counts},
breakout, anomalies, legacy_volume_multiple_20, thresholds{…}, interpretation[],
strategy_comparison (when a strategy is given), freshness (wrapper only)
```

- `FEATURE_VERSION = 1` and the full threshold set are in every result, so a PK-24 score can be tied to the exact definition it was fitted on.
- When `UNAVAILABLE`, every feature group is `null`, and `warnings` and `interpretation[0]` say why.

# Data Quality

| Code | Effect | Rule (checked on the 61-bar window; gaps on 62 rows) |
|---|---|---|
| `NO_HISTORY`, `INSUFFICIENT_HISTORY` (fewer than 21 bars) | UNAVAILABLE | |
| `HISTORY_NEEDS_REFRESH`, `HISTORY_SUSPECT` | UNAVAILABLE | PK-22 status |
| `UNVERIFIED_GAP_IN_WINDOW` | UNAVAILABLE | open vs previous close beyond `SUSPECT_CORPORATE_ACTION_GAP_PCT` (25%), not in `verified_gaps` |
| `DUPLICATE_DATE`, `UNSORTED_DATES` | UNAVAILABLE | whole cut frame |
| `MISSING_VALUE`, `NEGATIVE_VOLUME`, `INVALID_CANDLE` | UNAVAILABLE | high < low, open or close outside the range, or a price ≤ 0 |
| `PARTIAL_HISTORY` (21–60 bars) | DEGRADED | the percentile and longer counts are `null` |
| `ZERO_VOLUME_BAR`, `ZERO_VOLUME_IN_WINDOW`, `ZERO_BASELINE`, `ZERO_RANGE_BAR` | DEGRADED | |
| `SPECIAL_SESSION_BAR`, `SPECIAL_SESSION_IN_BASELINE` | DEGRADED | the as-of bar, or one of its 20 baseline sessions, is a PK-22 calendar special session |

- **A bad bar outside the window does not block today's features** (tested). Every feature is window-local.
- **Special sessions** were added after the historical sanity run found a "low-volume pullback" whose only pullback bar was the 2024-03-02 Saturday disaster-recovery session, at 0.04× normal volume. See *Deviations* 3 for why only the baseline bars are checked.

# Corporate-Action Interaction

- A symbol PK-22 has marked `SUSPECT` or `NEEDS_REFRESH` is **not computed**. The history status and verified gaps are part of the cache key.
- Independently of the status table, a split-sized gap **inside the feature window** blocks the result until a rebuild records it as verified. The test "2:1 split never back-adjusted" is UNAVAILABLE, then OK once verified, and OK again once the gap has aged out of the window.
- **Cache invalidation.**
  - The feature cache key includes the **sha256 of the exact rows used** (plus the status, verified gaps, version, as-of, strategy and params).
  - A refresh, a PK-22 rebuild or any edited bar therefore produces a new key: stale features cannot survive a history change.
  - Tested by rewriting a bar (the old value 4.5× becomes 2.0×). Mutant M9 (a static key) is caught.

# No-Lookahead Guarantees

- **As-of path:** `compute_at` drops rows after `as_of` **before** validation and computation. Mutant M10 (the cut removed) is caught.
- **Every baseline is shifted.** The rolling windows and the percentile reference end at T−1, and the rolling counts end at T.
- **Tests:**
  - **28:** changing volume, close and high on every row after T leaves all columns ≤ T unchanged, and `compute_at` is identical with or without the future rows.
  - **28b:** the series equals the as-of computation on **every** row.
  - **29:** the percentile equals a hand-built prior-60 window, and a spike at T+1 changes nothing.
  - **30:** reversing all future closes leaves the OBV slopes, new-high and divergence ≤ T unchanged.
  - **Research:** each signal's features are identical when recomputed from frames cut at the signal date.
- **PK-21 backtester use.** The backtester can consume `compute_series` rows directly, with row i equal to `compute_at(f.upto(i))`. The simulation itself was not changed.

# Historical Research Export

`backend/volume_research.py` is for research only and is imported by nothing live.

- **`signal_feature_rows`** replays `module.evaluate_entry(df.iloc[: i+1])` exactly as test_27 and the backtester do. For each passing bar it records the strategy, symbol, token, signal date, feature version, data quality, **39 feature columns** and `legacy_volume_pass`.
- **`forward_outcomes`** is the **only** function that reads rows after i. Its values are labelled `outcome_*` and never passed into a feature:
  - `outcome_fwd_ret_5/10/20_pct`, measured from the next session's open;
  - `outcome_mfe_20_pct` and `outcome_mae_20_pct`.
- **`--with-trades`** adds the PK-21 default-config backtest result per signal: `outcome_traded`, `outcome_trade_net_r` and `outcome_trade_exit_reason`.
- **CLI:** `python -m backend.volume_research export --db <copy> --out file.csv [--strategy …] [--start …] [--end …] [--with-trades]`.
  - It opens a **copy** `mode=ro&immutable=1`.
  - Its universe is NSE tokens present in the NSE instrument cache, so BSE duplicates are never mixed in.
  - Flagged histories are excluded.
  - Outcome columns are always written last.

**Smoke run and determinism** on a scratch copy of the real backup, from 2024-09-01, all three strategies, `--with-trades`:

| | Result |
|---|---|
| Universe | 62 NSE symbols, none excluded |
| Signal rows | **410**: momentum_breakout 145, trend_pullback 159, rsi_mean_reversion 106 |
| Columns | 54, with the 8 `outcome_*` columns last |
| Data quality | 345 OK, 65 DEGRADED (almost all special sessions in the baseline), 0 UNAVAILABLE |
| Became backtest trades | momentum_breakout 89, trend_pullback 100, rsi_mean_reversion 80 |
| Two runs | **byte-identical** (sha256 `692bf959c68173be…`), 323 s and 410 s |

The scratch copy was deleted afterwards, because it holds real trade tickets. No profitability conclusion is drawn; that is PK-24/25.

# API

`GET /api/volume-intelligence/{symbol}?as_of=YYYY-MM-DD&strategy=<name>&instrument_token=<int>`, in `backend/routes/volume_intelligence.py`.

- **Read-only.** No broker dependency, no broker call, no write. The test asserts `FakeKite.calls == []` and a byte-identical DB dump before and after.
- **Symbol → one token.** The resolved `{instrument_token, symbol, source}` is echoed. The lookup order is:
  1. an explicit `instrument_token`;
  2. the watchlist's token (the scanner's own);
  3. the NSE instrument cache;
  4. stored history, preferring an NSE-segment token.

  A BSE series is never chosen over the NSE one (tested; mutant M11 caught).
- **Strategy parameters:** `strategy` adds `strategy_comparison` using the parameters the scanner would use now (the default account's saved config, read only). Changing `volume_multiple` to 4.0 is reflected in the comparison (tested).
- **Errors:**

  | Case | Response |
  |---|---|
  | Unknown symbol | **404** `UNKNOWN_SYMBOL` |
  | Bad date | **422** `INVALID_DATE` |
  | Unknown strategy | **422** `UNKNOWN_STRATEGY` |
  | Known symbol, no, flagged or stale history | **200**, stating it in `data_quality`, `warnings` and `freshness` (`is_stale` uses the scanner's own `MAX_DATA_STALENESS_DAYS` rule) |

- **Security.** The PK-18 GET crawl covers the route automatically and passes (no secret in any response). It is a GET, so the CSRF write inventory is unchanged.
- **Signal Detail.** `GET /api/tickets/{id}/chart` gains `volume_intelligence`, computed **as of the ticket's `signal_date`** with the ticket's own evaluation params. A failure degrades only that section (`COMPUTATION_FAILED`) and never the payload (tested).

# UI

- **Placement:** Signal Detail gets one new section, **Volume context**, between *Entry conditions* and *Risk checks* (`frontend/app.js` `sdVolumeHtml`). No redesign; it reuses the sizing rows' `sd-calc-*` styles and the existing `sd-warn` banners, with 7 lines of CSS.
- **Rows:**
  - RVOL20, and RVOL20 by median;
  - volume percentile;
  - ₹ traded value against its 20-session average, with a low-liquidity note;
  - close location;
  - OBV trend and divergence;
  - accumulation-like / distribution-like counts over 10 (and 20);
  - volume contraction;
  - breakout participation or pullback volume when applicable.
- **Also shown:**
  - the strategy-rule comparison line;
  - a collapsible **In words** list of the server's factual sentences;
  - a footer: "As of the … candle · feature v1 · descriptive measurements, not part of the signal".
- **Quality banners:** a warning banner when data is unavailable, and an info banner when it is degraded.
- **Escaping:** all values go through `esc()`, `fmt()` or the helpers.
- **Wording:** a test asserts that neither the API text nor the UI section's source contains any `FORBIDDEN_PHRASES` ("institution", "smart money", "big money", "operator", "insider", "will rise", "will fall", "will go", "guarantee", "accumulating", "distributing").

# Performance

`perf_pk23.py` used real TCS bars (754) seeded into a throw-away DB. Timings are medians.

| Measurement | Time |
|---|---|
| `compute_series`, 754 bars (research path) | 29.1 ms |
| `compute_series`, 1,100 bars | 38.7 ms |
| `compute_at`, 61-bar window | 16.4 ms |
| `load_ohlc_frame`, 754 bars | 4.4 ms |
| `features_for_instrument`, cold (load + hash + compute) | 24.3 ms |
| `features_for_instrument`, warm (load + hash, cache hit) | **8.2 ms** |
| `GET /api/volume-intelligence/TCS`, cold / warm | 29.5 / **12.8 ms** |
| Signal Detail `build_payload`, without / with volume (cold cache) | 9.5 / 34.9 ms |

- **Cache:** a bounded in-process LRU of 256 entries. It is kept because it roughly halves repeat renders, and it costs one hash of about 62 rows. No persistence was added, as the packet preferred.
- **Earlier optimisation:** profiling during development found `compute_series` dominated by per-column DataFrame inserts and a concat per flag. Building a dict and using numpy flags cut it from 70 to 29 ms per call (profiled). This is why test 28b, which runs 260 window computations, takes about 3 s.

# Existing Strategy Preservation

- **`git diff` is empty for:**
  - `backend/strategies/*`;
  - `strategy_engine.py`, `risk_engine.py`, `backtest.py` and `execution.py`.
- **Test 31:**
  - on all 4 golden series, every bar's `evaluate_entry` `volume_multiple_actual` equals the engine's `legacy_volume_multiple_20` (rel. 1e-12);
  - the golden signal dates are reproduced exactly with the engine run on the same frames;
  - the frames are unmutated.
- **Unchanged ticket record:** `test_scan_ticket_record_is_unchanged_by_pk23` confirms that `entry_evaluation` has exactly its 13 pre-PK-23 keys. The scanner stores nothing new, and the volume context is computed on demand.
- **Mutants M8a and M8b** (either strategy switched to a prior-only average) are caught.
  - **Honest note:** PK-21's golden `test_27` alone did **not** catch M8a, because the golden fixture's signal dates happen to be identical under both rules. The new test 31 pins the strategy's own number and catches both.

# Tests Added

| File | Tests | Covers |
|---|---|---|
| `tests/test_volume_intelligence.py` | 48 | B relative volume and percentile (1–6), D structure (7–10b), E OBV (11–15), F contraction and pullback (16–18), G evidence (19–22), H breakout (23, 23b), I anomalies and shocks, data quality (24–27, 26b, special sessions), lookahead (28, 28b, 29, 30), strategy preservation (31, 31b, 57/17), thresholds, liquidity, interpretation wording, as-of on a non-session day |
| `tests/test_volume_intelligence_api.py` | 30 | 32 latest and as-of responses, saved-param comparison, NSE-vs-BSE resolution, watchlist-first; 33 unknown / no history / NEEDS_REFRESH / SUSPECT / stale / bad params / cache invalidation; 34 wording in the API and UI source; chart payload at the signal bar, without a volume rule, and with a failing compute; no decision path imports the engine (9 files); unchanged ticket record; research rows are the real golden signals; features never see future rows; forward outcomes from the next open; CLI on a DB copy |

No existing test was changed.

# Mutation Results

`mutations.py` (scratchpad) applied each mutant to one file, ran both PK-23 files (plus the golden test_27 for M8), restored the original bytes in `finally` and **verified the sha256 (15/15 matched)**.

| # | Mutation | File | Result | First catching tests |
|---|---|---|---|---|
| M1 | RVOL baseline includes the current bar | engine | **caught**, 13 | tests 1, 2, 3, 5 |
| M2a | percentile window includes T | engine | **caught**, 6 | test 4 ×4 |
| M2b | percentile ranked against **future** rows | engine | **caught**, 9 | test 4, test 29 |
| M3 | close location inverted | engine | **caught**, 10 | tests 7, 8, 10, 10b |
| M4 | OBV sign inverted | engine | **caught**, 5 | tests 11–14 |
| M5 | accumulation / distribution swapped | engine | **caught**, 4 | tests 19–22 |
| M6 | negative-volume validation removed | engine | **caught**, 1 | test 25 |
| M7a | SUSPECT history allowed | engine | **caught**, 2 | 26, 33c |
| M7b | NEEDS_REFRESH history allowed | engine | **caught**, 3 | 26, 33c, 33f |
| M7c | unverified split gap in the window allowed | engine | **caught**, 1 | 26b |
| M8a | momentum_breakout volume rule altered | strategy | **caught**, 2 | 31, chart payload (test_27 alone: not caught, see above) |
| M8b | trend_pullback volume rule altered | strategy | **caught**, 1 | 31 |
| M9 | cache ignores the history content | engine | **caught**, 1 | 33f |
| M10 | as-of cut removed | engine | **caught**, 1 | 28 |
| M11 | symbol resolves to the BSE series | route | **caught**, 1 | 32d |

**15/15 caught.**

# Targeted Repeats

| Run | Result |
|---|---|
| PK-23 suites (2 files) ×10 | **10/10 green**, 78 passed each, 19–29 s |
| No-lookahead subset ×20 (tests 28, 28b, 29, 30, the as-of tests, research features) | **20/20 green**, 7 passed each, 8–11 s |

No flake was seen. The engine tests use fixed data, and the API tests use `last_weekday(today)` fixtures, like the existing suites.

# Historical Sanity Examples

`historical_sanity.py` ran on a scratch copy of the backup, which was deleted afterwards.
- **Selection:** deterministic, restricted to windows with status OK.
- **Checking:** each example's key numbers were recomputed **independently in plain Python** (lists only, no pandas and no engine code): RVOL20, median RVOL, percentile, close location, contraction, OBV slope and divergence, the accumulation count and the pullback ratio.
- **Result: every value matched** in all seven examples.

| Situation | Symbol, date | Key facts (engine = independent) |
|---|---|---|
| Normal-volume day | COALINDIA 2025-01-03 | RVOL20 0.999×, median 1.19×, **50th percentile**, close location 0.42, OBV slope −0.06 (flat), 0 / 0 evidence, ₹273 cr against ₹275 cr average |
| Extreme-volume breakout | NTPCGREEN 2026-03-12 | V 165.3M, **RVOL20 67.3×**, median 75.8×, 100th percentile, +12.6%, close location 0.69, close 5.76% above the 30-session high, OBV confirmed, abnormal + extreme, ₹1,615 cr against ₹21.8 cr |
| High-volume weak close | IRBINVIT-IV 2025-10-16 | **RVOL20 142×**, close location **0.02**, −3.8%, `high_volume_weak_close` and **`negative_shock`**, 1 accumulation-like vs 4 distribution-like in 10, OBV −0.90 falling, below the ₹5 cr liquidity threshold on average |
| Low-volume pullback (recovery bar) | BIRET-RR 2025-05-02 | 6-session, −0.78% pullback at **0.05×** the volume into the high, recovery bar at 2.68× the pullback's volume; contraction 0.27 (quiet base); mean-based RVOL 0.79× against **median-based 1.92×** (a skewed baseline) |
| OBV leads price | SOLARINDS 2026-04-30 | OBV at a 20-session high, price not (**`obv_leads_price`**), OBV slope +0.76, RVOL20 0.55×, 13th percentile |
| Price unconfirmed | AWL 2025-11-24 | Close at a 20-session (and 30-session) high, OBV not (**`price_unconfirmed`**), OBV slope −0.40 falling, RVOL20 0.54× |
| Contraction, then expansion | LTTS 2026-03-13 | Prior-5 / prior-20 = **0.54** (contracting), then **RVOL20 22.1×**, 100th percentile, +9.6%, close location 0.73, extreme volume |

**What the first run found.** It chose ICICIBANK 2024-03-04 as the low-volume pullback. Its only pullback bar was the 2024-03-02 disaster-recovery Saturday (0.04× normal volume). This led to the special-session codes (see *Data Quality* and *Deviations*); after them, the example above was chosen instead.

# Browser Validation

**Setup:**
- `tests/demo_server.py --market-open` (FakeKite, throw-away DB), with its output sent to a **file**, not a pipe (the PK-22 lesson).
- Playwright with Edge headless and the app's own CSP enforced.
- A `securitypolicyviolation` listener installed before any page script.

**26/26 checks passed:**
- **API:** 200 with the resolved NSE token, data quality and strategy comparison; no forbidden wording; 404 for an unknown symbol; 422 for a bad date.
- **UI, walking all 3 pending signals** (BHARTIARTL momentum, BHARTIARTL pullback, DIVISLAB RSI):
  - the Volume context section renders with every row;
  - the strategy-rule line appears, and for RSI the "no volume condition" line;
  - there is no forbidden wording;
  - "In words" expands.
- **Screenshots:** dark and light themes (both legible) and 420 px; the section has no horizontal overflow.
- **Zero CSP violations, zero console or page errors, zero 4xx/5xx.**

**Note.** A first, page-wide overflow check failed at 420 px. `overflow_probe.py` showed the overflow comes from the app shell's top bar and approval cards behind the overlay (`#topbar-right` reaches x = 640). It is **identical with the Volume section removed from the DOM**, so it pre-dates PK-23. The check was re-scoped to the new section. Screenshots are in the session scratchpad (`browser23/`).

# Full Suite ×3

Three consecutive runs of the whole suite, after the last change of any kind:

| Run | Result |
|---|---|
| 1 | **1,317 passed**, 0 failed (172 s) |
| 2 | **1,317 passed**, 0 failed (153 s) |
| 3 | **1,317 passed**, 0 failed (151 s) |

1,317 = the 1,239 baseline + 78 new PK-23 tests; 0 xfailed, 0 xpassed. A first ×3 attempt was cut off when the previous session ended before it wrote any output. It was discarded and rerun in full. The live DB sha256 afterwards is `1378f8b4…dacf`.

# Known Limitations

1. **Volume adjustment on splits is unverified.** No stored symbol has a confirmed split. If Kite back-adjusts prices but not volume, `traded_value` across a split would be inconsistent. The in-window gap block and PK-22's statuses keep such windows out, but a split below the 25% gap threshold (for example a 5:4 bonus, about 20%) is not detected, as in PK-22.
2. **Special sessions are only flagged when they are the bar or in its 20-session baseline.** A thin Muhurat bar 21–60 sessions back still enters the percentile window (it can move the percentile by at most 1.7 points) and the older pullback baseline.
3. **Pullback is generic** (drawdown from the prior-20 closing high). A 19-session decline counts as a "pullback". It does not replicate trend_pullback's EMA-touch rule.
4. **Breakout participation uses a fixed default lookback of 30** unless a threshold override is passed. It is not read from each strategy's `donchian_lookback`.
5. **Thresholds are round, unfitted defaults** (1.5×, quartiles, 0.8, 90th and 98th percentiles). They are descriptive, and whether any of them carries edge is PK-24/25's question.
6. **Signal-time features are recomputed on demand, not snapshotted.** They are deterministic for unchanged history. After a PK-22 rebuild they reflect the rebuilt, correct history rather than what was on screen at signal time.
7. **The cache is per process** and holds no persistence; a restart recomputes (about 24 ms per symbol).
8. **The research export is slow** (about 5–7 minutes over 62 symbols and 3 strategies), dominated by replaying `evaluate_entry` on every bar, as test_27 does.
9. **WAL sidecars.** The step-0 read-only backup connection left an empty `kite_terminal.db-wal` (0 bytes) and a `-shm` next to the live DB (WAL mode). The main file is byte-identical before and after. This is the mirror image of the PK-22 note.
10. **Pre-existing:** the main app shell overflows horizontally at 420 px (top bar), independent of PK-23.

# Deferred PK-24 / PK-25 Work

- **PK-24:** combine validated PK-23 features into a probabilistic accumulation score, fitted against `FEATURE_VERSION = 1`, using the research export as its dataset.
- **PK-25:** out-of-sample validation of the score, using the `outcome_*` labels and the walk-forward windows.
- **Open decisions:**
  - whether thin special sessions should be *excluded* from the volume baselines rather than flagged;
  - whether breakout participation should take each strategy's lookback;
  - whether a signal-time feature snapshot should be persisted on the ticket.
- **Not started:** PK-26 to PK-28, any volume-based strategy, and strategy weighting.

# Deviations From Plan

1. **Signal-time features are not stored on the ticket.** They are computed on demand at `signal_date`. This keeps the scanner, `strategy_engine.py` and the ticket record completely unchanged (tested), and deterministic window-local features make a snapshot unnecessary. See *Known Limitations* 6.
2. **The API resolves symbols to one token with an NSE preference, and accepts `instrument_token`.** The audit found BSE duplicates of 6 symbols with incomparable volumes; this was not in the packet.
3. **Special-session codes were added** (`SPECIAL_SESSION_BAR`, `SPECIAL_SESSION_IN_BASELINE`), found by the historical sanity check. Flagging any special session anywhere in the 61-bar window was implemented first and measured: it would mark **59%** of all history DEGRADED, which says nothing. Restricting the check to the bar and its 20-session baseline marks 22%, where the thin volume actually distorts the averages.
4. **Numbering.** The packet listed some sections twice under different letters (for example J/K/L/M/N/O and K/L/M). They were implemented as: engine (B–I, K), API and model (J, L), UI (M), historical and lookahead (N), harness (O), strategies unchanged (P).
5. **`FORBIDDEN_PHRASES` is an explicit list** enforced by tests on the API output and the UI source, rather than a free-form review.
6. `docs/OPUS55_AUDIT_REPORT.md` was not modified.
