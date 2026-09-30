# OPUS55 PK-24 Implementation Report: Smart Money Accumulation Score (a transparent heuristic prior)

| | |
|---|---|
| Packet | **PK-24** only. PK-25 and later were not started. No strategy, scan, ranking, approval, risk, sizing, entry, exit, GTT or backtest-selection path was changed. |
| Baseline before | **1,317 passed**, 0 failed, 0 xfailed, 0 xpassed, re-measured on the untouched code (`ae86c10`) before the first edit. |
| Backups | git tag `backup-pre-pk24` and branch `backup/pre-pk24` at `ae86c10`. `backend/kite_terminal.db.pre_pk24_bak` made with the SQLite online-backup API from a read-only connection: `integrity_check` ok, 28 tables, 18 trade tickets, 50,556 daily candles, 10,133 cached instruments, sha256 `01a99048…7216`. Not committed (`*_bak` is git-ignored). |
| After | **1,415 passed**, 0 failed, 0 xfailed, 0 xpassed, three consecutive times (1,317 + 98 new PK-24 tests). See *Full Suite ×3*. |
| Live DB | `backend/kite_terminal.db` sha256 `1378f8b4…dacf` before and after all work. It was never opened by app code; all analysis ran on a scratch copy of the backup, deleted afterwards. |

# Executive Summary

PK-24 adds one authoritative, pure module, `backend/smart_money_score.py`. Its single entry point, `score(features)`, turns one PK-23 `VolumeFeatures` object into a bounded **0–100** score of *accumulation-like price/volume evidence*. The result also carries:

- a separate confidence level;
- 14 named components, each with points, inputs and a factual sentence;
- the top three positive and top three negative contributors;
- a fixed disclaimer: "Price-volume evidence, not proof of institutional buying."

It is wired into exactly three places, and all three call the same function on the same feature object:

- a read-only endpoint, `GET /api/smart-money-score/{symbol}`;
- a new **Accumulation evidence** section in Signal Detail, scored from the **same signal-date snapshot** as Volume context;
- 21 new feature columns in the PK-23 research export.

The strategies, scanner, ranking, approval, risk, sizing, execution and backtester never import or call it (tested structurally and behaviourally).

Key properties, each tested:

- **Neutral evidence scores exactly 50.** Positive and penalty maxima are both 50 points, so the full 0–100 range is reachable and nothing more.
- **Bounded, deterministic and smooth.** Every continuous input enters through a saturating piecewise-linear ramp: RVOL 1.99 against 2.01 moves the score by less than 1 point, and a ±1% change on any input moves it by at most 2.5 points over 300 random cases.
- **Volume alone is never enough.** A 20× RVOL bar that closed near its low under a long wick scores below 40. RVOL 1.8 with broad confirmation scores 75 or more.
- **No lookahead.** Rewriting every row after T changes nothing at T, and the research score is identical when outcomes are removed or replaced with garbage.
- **Unusable history gives `score: null, confidence: INSUFFICIENT_DATA`**, never a made-up 50.

Validation:

- **Mutations:** 12/12 caught, each file restored and sha256-verified.
- **Targeted repeats:** PK-24 suites ×10 and the no-lookahead/isolation subset ×20, all green.
- **Research export:** byte-identical on two runs. With the score columns removed it is byte-identical to **PK-23's own export** (sha `692bf959c68173be…`), proving no PK-23 feature changed.
- **Browser:** 29/29 checks under enforced CSP.
- **Full suite:** green three consecutive times.

**What PK-24 does not claim.** It sets no weight from forward returns. The exploratory in-sample appendix shows **no** monotone relationship between score and forward return; that is reported plainly, and it is PK-25's job to test out of sample.

# Score Philosophy

- **A prior, not a model.** The weights and ramp anchors were set by hand from the *shape* of the PK-23 feature distributions (40,843 full-window bars, 62 NSE series) and from a redundancy analysis between components. No weight, threshold or band was chosen by looking at outcomes. PK-25 decides whether the prior carries any incremental information.
- **Evidence for and against, symmetrically.** Every constructive component has a counterpart that subtracts: accumulation against distribution sessions, strong against weak close, OBV confirmation against non-confirmation, breakout participation against a failed breakout attempt. There is also an exhaustion penalty for extreme-volume bars that did not hold their highs.
- **Repeated sessions outweigh one bar.** The single-bar components are capped at 14 points together; the multi-session evidence (accumulation frequency, OBV, pullback recovery) carries 27 (test 17b).
- **Descriptive language only.** A wording test enforces this on every sentence, band label, API response and the UI source.

# What The Score Means

"On the as-of bar, and over the recent sessions PK-23 describes, how strongly does the price/volume structure resemble what the PK-23 definitions call accumulation-like behaviour — high participation on strong closes, repeated constructive sessions, OBV rising or leading price, quiet pullbacks followed by participation — net of the opposite evidence?"

50 is neutral. Above 50 means net accumulation-like evidence; below 50 means net distribution-like or rejection evidence.

# What The Score Does NOT Mean

- It is not evidence of institutional, mutual-fund, FII/DII, promoter, operator or insider buying. None of that is observable in daily OHLCV.
- It is not a forecast, a recommendation or a signal. It does not say BUY or SELL and uses no P&L colours.
- It does not feed trade acceptance, ranking, sizing, risk or exits.
- Its predictive value is **not established** (see *Exploratory Outcome Appendix*).

# Score Version

- `SMART_MONEY_SCORE_VERSION = "1.0"` and `REQUIRED_FEATURE_VERSION = 1` are in every result, together with the full `weights` table.
- `score()` **refuses** any other feature version (`ValueError`), so a PK-23 definition change cannot silently be scored with v1.0 weights.
- **The one PK-23 change is additive.** `bar.open`, `bar.high` and `bar.low` were added to the `VolumeFeatures` object, because the failed-breakout check needs the bar's high. No existing value changed, `FEATURE_VERSION` stays 1, and the research `FEATURE_FIELDS` are untouched. The export parity check proves this byte-for-byte.

# Component Architecture

`COMPONENTS` in `smart_money_score.py` is the only place weights live. It is an ordered, immutable table of `(name, kind, max points, label)`.

| # | Component | Kind | Max | Inputs (PK-23) |
|---|---|---|---|---|
| 1 | participation | + | 5 | p (see below), close location |
| 2 | close_quality | + | 6 | close location × volume weight |
| 3 | price_response | + | 3 | close-to-close % × volume weight |
| 4 | accumulation_frequency | + | 13 | accumulation-like counts over 10 and 20 sessions |
| 5 | obv_confirmation | + | 8 | OBV slope-20, OBV 20-session high |
| 6 | contraction_expansion | + | 5 | prior-5/prior-20 ratio × p × close |
| 7 | pullback_participation | + | 6 | pullback volume ratio, recovery ratio, return, close |
| 8 | breakout_participation | + | 4 | is_breakout, breakout close quality × p |
| 9 | distribution_frequency | − | 13 | distribution-like counts over 10 and 20 sessions |
| 10 | high_volume_weak_close | − | 8 | (1 − close location) × volume weight |
| 11 | upper_wick_rejection | − | 7 | upper wick, close location, p, high-volume reversals over 20 |
| 12 | failed_breakout | − | 6 | bar high and close against the prior-30 closing high |
| 13 | obv_non_confirmation | − | 8 | `price_unconfirmed` divergence, falling OBV |
| 14 | exhaustion_shock | − | 8 | median RVOL, range %, close location or upper wick |

**Formula.** `score = clamp(50 + liquidity_factor × Σ signed points, 0, 100)`, rounded to 0.1.

- Each component contributes `max × fraction`, with the fraction in [0, 1]. Penalties are negative.
- `positive_score` and `negative_penalty` are reported separately.
- A component whose inputs are unknown contributes 0 and lowers `coverage`, the share of total weight that could be evaluated.

# Relative Volume Component

**One participation measure, used once.** RVOL20, median-based RVOL20 and the 60-session percentile measure the same phenomenon: on the stored history their Spearman correlation is 0.86–0.95, and relative traded value correlates **0.997** with RVOL20. So they are averaged into a single participation intensity `p`, which every volume-dependent component reuses:

- `p = mean(ramp(RVOL20, 1.0, 2.5), ramp(median RVOL20, 1.0, 3.0), ramp(percentile, 50, 95))`.
- **Anchors:** RVOL20 on the stored history has median 0.84, P90 1.73, P95 2.26 and P99 4.53. The ramp starts at "average" (1.0) and saturates around P95.
- **Saturation:** a 20× spike gives the same `p` as a 3× one (test 5b).
- **Relative traded value is left out of the score entirely** (see *Redundancy*). Absolute traded value is used only for confidence and damping.
- **Gating:** the `participation` component itself is `p × ramp(close location, 0.35, 0.65)`. Volume earns credit only with a close that is not weak.

# Price/Close Quality Component

- `close_quality = ramp(close location, 0.5, 0.9) × (0.25 + 0.75p)`. A strong close on low volume gets a quarter of the credit; on elevated volume, all of it (test 6).
- `price_response = hump(return) × (0.3 + 0.7p)`. The hump rises from 0 to +2.5% (about P95 of daily returns), then **halves** between +6% and +12%. A 19% one-day jump earns less than a 3% one, so blow-off moves are not rewarded blindly.

# Accumulation Frequency

- **Effective count** = accumulation-like days in the last 10, plus half of those in sessions 11–20. It is ramped over 0–3.
- On the stored history, 70% of bars have 0 accumulation-like days in 10, 23% have 1, and 1.4% have 3 or more. Three effective days is therefore already rare, which is why the component saturates there.
- It carries the largest single weight (13 points), so repeated constructive sessions dominate one isolated spike. Scores rise monotonically with the count and are bounded (test 7).

# Distribution Penalties

- `distribution_frequency` is the exact mirror of accumulation frequency (same count rule, same ramp, same 13 points). Two accumulation-like and two distribution-like sessions cancel to exactly 50 (test 12b).
- `high_volume_weak_close` is the mirror of close quality, and one point heavier (8 against 6), so volume on a weak close always costs more than the same volume on a strong close earns.

# OBV Component

- `obv_confirmation = 0.7 × ramp(slope-20, 0, 0.4) + 0.3 × OBV at a 20-session high`, capped at 8 points. OBV is derived from the same volume stream, so it is deliberately worth less than accumulation frequency (test 8).
- `obv_non_confirmation = 0.5 × price_unconfirmed + 0.5 × ramp(−slope-20, 0, 0.4)`, capped at 8. "Price unconfirmed" means the close made a 20-session high while OBV did not.

# Contraction / Expansion

- `contraction_expansion = ramp(1 − prior-5/prior-20, 0.1, 0.4) × p × close gate`. On the stored history the contraction ratio has median 0.95 and P10 0.62.
- **A quiet base alone earns nothing**: expansion on this bar is required (test 9).

# Breakout / Pullback Evidence

**Breakout participation (4 points).** `is_breakout × ramp(breakout close quality, 0.5, 0.9) × p`. A breakout is never required: the RVOL 1.8 broad-confirmation case scores 75 or more with no breakout (test 17).

**Failed breakout (penalty, 6 points).** This is the only check that needed the new `bar.high`:

- `ramp(% the high pierced the prior-30 closing high, 0, 1) × ramp(% the close finished below it, 0, 1) × (0.25 + 0.75p)`.
- Both conditions are smooth, so there is no cliff at `high == level`.

**Pullback participation (6 points).** `in_pullback × ramp(1 − pullback volume ratio, 0.1, 0.5) × ramp(recovery volume ratio, 1, 2) × ramp(return, 0, 1%) × close gate`.

- PK-23's generic pullback flag is true on **86%** of bars, so this component only pays for a *quiet* pullback **plus** a participating, up, held recovery bar. A low-volume pullback by itself earns nothing (test 10).
- The close gate was added after the sanity run (see *Historical Sanity Cases* and *Deviations* 2).

# Liquidity / Traded Value

Absolute traded value does **not** add points, so the score does not favour large caps. It acts in two places:

- **Damping toward neutral.** Below the configured ₹5 crore/day threshold (`config.VOLUME_INTEL_LOW_LIQUIDITY_INR`, read from the feature object's echoed threshold), `liquidity_factor` shrinks the distance from 50 by up to 30%, reached at one tenth of the threshold (log scale).
  - The damping is symmetric: a thin, distribution-heavy name is pulled *up* toward 50, never penalised (test 22).
  - 1,297 of 40,843 historical bars were damped.
- **Confidence** (see below).

# Exhaustion / Upper Wick Penalty

- `upper_wick_rejection = 0.8 × ramp(upper wick, 0.3, 0.6) × ramp(0.7 − close location, 0, 0.3) × p + 0.2 × ramp(high-volume reversals in 20, 0, 3)`. On the stored history the upper wick has median 0.23 and P90 0.57.
- `exhaustion_shock = ramp(median RVOL, 3, 6) × ramp(range %, 4, 8) × max(ramp(1 − close location, 0.4, 0.8), ramp(upper wick, 0.3, 0.6))`. Range % has P90 4.0 and P99 7.4. The same extreme range and volume that **held** its highs is not penalised (test 15b).
- PK-23's `negative_shock` flag is not used: its thresholded condition is covered smoothly by the weak-close and exhaustion terms.

# Normalization

- **Transforms:** `clamp`, `ramp` (piecewise linear, continuous, saturating) and one `hump`. No scipy, sklearn, pandas or numpy in the scoring path: `score()` takes **0.107 ms**.
- **Discrete inputs are few** and each moves at most its share of one component: OBV new high (2.4 points), `price_unconfirmed` (4 points), `is_breakout`, `in_pullback`.
- **Robustness tests:**
  - RVOL 1.99 against 2.01 differs by less than 1 point;
  - a ±1% perturbation of every continuous input over 300 random feature sets moves the score by at most 2.5 points;
  - the score is monotonic in close location on elevated volume.

# Weighting

The weights went through three states, and none of the changes used outcomes.

| Version | Weights (+ / −) | Median | P10 / P90 | Share ≥ 75 | Why it changed |
|---|---|---|---|---|---|
| Starting prior | participation 6, close 9, price 5, acc 10, OBV 7, contraction 5, pullback 4, breakout 4 / distribution 10, weak close 9, wick 6, failed breakout 6, OBV non-confirmation 7, exhaustion 8 | 51.3 | 43.2 / 61.8 | 1.9% | — |
| Redundancy fix | single-bar trio 20 → 14 points; acc 13, OBV 8, pullback 6; distribution 13, weak close 7 | 51.4 | 42.4 / 62.3 | 1.7% | the trio correlated at Pearson 0.73–0.80 (see below) |
| **Final v1.0** | as the table in *Component Architecture*: weak close 8, wick 7 | **51.2** | **42.0 / 62.2** | **1.7%** | penalty maxima summed to only 48, so 0 was unreachable; both sides now sum to 50 |

After those three, the **pullback close gate** was added from the sanity run (see *Deviations* 2). That changed the pullback component on 1.3% of bars.

# Redundancy / Correlation Analysis

Research-only pandas, over all 40,843 full-window bars.

**Raw volume measures (Spearman):**

| | RVOL20 | median RVOL20 | percentile-60 | relative traded value | z-score |
|---|---|---|---|---|---|
| RVOL20 | 1 | 0.949 | 0.857 | **0.997** | 0.935 |
| median RVOL20 | | 1 | 0.896 | 0.946 | 0.952 |

**Decision:**
- Relative traded value is a near-duplicate of RVOL20 and is excluded.
- RVOL20, median RVOL20 and the percentile are combined into **one** measure `p`, entering the score once.
- The z-score is excluded (PK-23 already notes it is not relied on).

**Component points (final model), pairs with |ρ| ≥ 0.5:**

| Pair | Spearman | Pearson | Interpretation / decision |
|---|---|---|---|
| close_quality ↔ high_volume_weak_close | 0.851 | < 0.5 | The two mutually exclusive halves of one input (close location above or below 0.5). They never both fire, so there is no double counting. Kept. |
| obv_confirmation ↔ obv_non_confirmation | 0.770 | < 0.5 | The two sides of OBV slope. Kept. |
| participation ↔ close_quality | 0.605 | **0.801** | Single-bar trio sharing `p`. **Held to 14 points together** (from 20). |
| participation ↔ price_response | 0.521 | **0.737** | Same. |
| close_quality ↔ price_response | 0.687 | **0.728** | Same. |
| breakout_participation ↔ trio | — | 0.56–0.58 | Rarely non-zero (6.4% of bars); kept at 4 points. |

Accumulation frequency, distribution frequency and OBV are all below |0.5| against each other and against the trio.

**Measured share of total |points|:**

| Component | Share |
|---|---|
| accumulation frequency | 23.0% |
| distribution frequency | 19.3% |
| OBV confirmation | 19.1% |
| high-volume weak close | 9.1% |
| OBV non-confirmation | 9.0% |
| close quality | 6.2% |
| participation | 4.3% |
| everything else | < 3.3% each |

No single weight exceeds 13 of the 100 attainable points.

# Confidence Model

The levels are `HIGH`, `MEDIUM`, `LOW` and `INSUFFICIENT_DATA`. Every level comes with `confidence_reasons[]`.

| Condition | Effect |
|---|---|
| PK-23 data quality `UNAVAILABLE` (no or short history, `HISTORY_SUSPECT`, `HISTORY_NEEDS_REFRESH`, unverified in-window split gap, invalid bar, duplicate date) | **score null, `INSUFFICIENT_DATA`**, with the codes |
| Component coverage below 50% | **score null, `INSUFFICIENT_DATA`** |
| `SPECIAL_SESSION_BAR` | −2 levels |
| `SPECIAL_SESSION_IN_BASELINE` | −1 level |
| any `ZERO_*` code | −1 level |
| `PARTIAL_HISTORY` (21–60 bars) | capped at LOW |
| 20-session average traded value below ₹1 crore | capped at LOW |
| `liquidity_warning` (below ₹5 crore) | −1 level |
| stale history (API "latest" only, the scanner's own staleness rule) | −1 level |
| coverage below 80% | −1 level |
| `history_status` unknown (live DB before its first PK-22 start, or the research path) | a note only: PK-23's in-window gap check still applied |

On the stored history this gives 76.0% HIGH, 22.2% MEDIUM (almost all special sessions in the baseline) and 1.7% LOW. Tests 18–22d cover each rule, with real `compute_at` cases for short history, a partial history, an unverified split and a real special session.

# Special Session Handling

PK-23 measured that Muhurat and disaster-recovery sessions trade 4–13% of a normal day's volume, and flags the bar or its 20-session baseline. PK-24 respects that:

- the score is **still computed** (not forced to zero or null), because the other evidence is valid;
- **confidence drops**: two levels when the as-of bar is the special session, one when it sits in the baseline;
- the reason names it.

Test 21b builds the real 2024-03-02 disaster-recovery Saturday into a baseline and gets `MEDIUM` with the reason.

# NSE/BSE Duplicate Instrument Handling

- **The score is computed for one instrument token.** `score()` consumes the features of exactly one series and echoes its `instrument_token`. NSE and BSE volumes are never combined.
- **The API reuses PK-23's `resolve_instrument` unchanged**: the watchlist token, then the NSE instrument cache, then stored history preferring an NSE-segment token. `?instrument_token=` selects a specific series, for example the BSE one.
- **Tests:**
  - test 27 (pure): two series of one symbol score differently under their own tokens;
  - test 27 (API): the NSE series gives RVOL 4.5× and the BSE series 1.0×, with different scores;
  - test 28: AWL stored under both tokens resolves to the NSE token deterministically on repeated calls.
- The research export's universe is still NSE-cache tokens only.

# Explanation Model

- **Every component carries:** `label`, `kind`, `max_points`, `points`, `available`, `inputs` (the exact numbers used), and a factual `reason` when |points| ≥ 0.5. Examples:
  - "4 accumulation-like session(s) in the last 10 (5 in the last 20)."
  - "Close in the top 3% of the day's range on elevated volume."
  - "The high traded above the prior 30-session closing high (1,152.15) but the close finished below it."
  - "Extreme volume (median-based RVOL20 35.7×) on a wide 9.9% range that did not hold its highs — an exhaustion-style bar."
- **`positive_reasons` / `negative_reasons`:** every contributing sentence, largest first.
- **`top_positive` / `top_negative`:** at most 3 each, sorted by points, ties broken by `COMPONENTS` order, so the result is stable (test 23). These are ready for PK-26.
- **Wording:** `forbidden_phrases()` is PK-23's list ("institution", "smart money", "operator", "insider", "accumulating", "will rise", …) plus "fii", "dii", "buy", "sell", "recommend", "proof" and "confirmed accumulation".
  - It is enforced on every sentence, band label, confidence reason and warning over 500 random feature sets (test 24), on the API response (test 31d) and on the UI source (test 35).
  - Only the fixed `NAME` and `DISCLAIMER` constants are exempt; the disclaimer names what the score is *not*.
- **Bands** are symmetric about 50 and descriptive:

  | Range | Label |
  |---|---|
  | 75–100 | Strong accumulation-like evidence |
  | 60–74.9 | Moderate accumulation-like evidence |
  | 40–59.9 | Mixed / neutral evidence |
  | 25–39.9 | Limited accumulation-like evidence |
  | 0–24.9 | Weak accumulation-like evidence; distribution-like evidence dominates |

# API

`GET /api/smart-money-score/{symbol}?as_of=YYYY-MM-DD&instrument_token=<int>`, in `backend/routes/smart_money_score.py`.

- **A separate endpoint** keeps the descriptive features (PK-23) and the heuristic judgement (PK-24) apart.
- **Features are computed once** through PK-23's `features_for_instrument`, including its LRU cache, then scored.
- **Response:** the whole score object plus `resolved`, `freshness`, `history_status`, `requested_as_of` and `notes`.
- **Read-only.** No broker dependency. Test 31 asserts `FakeKite.calls == []` and a byte-identical DB dump before and after.
- **Errors:**
  - 404 `UNKNOWN_SYMBOL`;
  - 422 `INVALID_DATE`;
  - a known symbol with no, flagged or insufficient history gives **200 with `score: null`, `INSUFFICIENT_DATA`** (tests 32, 32b);
  - stale history gives `MEDIUM` with the reason (test 32c).
- **Security.** It is a GET, so the CSRF write inventory is unchanged, and the PK-18 GET crawl covers it (it runs in the full suite).

# Signal Detail UI

**Backend.** `signal_chart.build_payload` now computes `_volume_context(...)` once and adds `smart_money_score = _smart_money(volume)`, scoring that **same** dict. The consequences:

- the score is always as of the ticket's `signal_date`;
- there is no second feature computation;
- a failure degrades only this section (`COMPUTATION_FAILED`, test 33b).

**Frontend.** `frontend/app.js` gets `sdSmartMoneyHtml`, rendered as a new **Accumulation evidence** section between *Volume context* and *Risk checks*. It shows:

- "Smart Money Accumulation Score: 76 / 100", the band label and "Confidence: High";
- a neutral meter with a 50 tick;
- the top positives and "Cautions";
- the disclaimer in italics;
- a footer: "Score as of the 2026-09-29 candle · score v1.0 (features v1) · 50 is neutral · context only, not part of the signal."

**Styling and safety.**
- Accent colour only: no P&L green or red and no buy/sell wording (test 35 checks both the JS and the CSS block).
- 11 lines of CSS. All values go through `esc()` and `fmt()`.
- A null score renders as "Score not available" with the reasons.
- The function sits **outside** the PK-23 volume-section span, because PK-23's wording test forbids the words "smart money" there; the display name comes from the server.

Approval behaviour is unchanged (test 30).

# Historical Research Export

- `volume_research.score_record(features)` produces **21 columns** (`SCORE_COLUMNS`):
  - `smart_money_score`, `smart_money_score_version`, `smart_money_confidence`;
  - the 14 `sms_<component>` points;
  - `sms_positive`, `sms_negative`, `sms_liquidity_factor`;
  - the flags `sms_failed_breakout` and `sms_exhaustion` (penalty ≥ 1 point).
- **Separation.** They are computed in `signal_feature_rows` right after `compute_at` on the frame cut at the signal bar, **before** `forward_outcomes` runs. `write_csv` keeps every `outcome_*` column last. The score itself raises on any `outcome_*` key.
- **Tests:**
  - score columns come before the outcomes, and the golden signals are unchanged;
  - test 26: scores are identical with outcomes off, on, or monkeypatched to garbage, and equal the pure score of the frame cut at the signal date;
  - test 26b: `score_record` rejects outcome keys;
  - the CLI test checks the header order.

**Two runs on a scratch copy of the real backup** (2024-09-01 onward, all three strategies, `--with-trades`):

| | Result |
|---|---|
| Rows / columns | 410 / **75** (PK-23's 54 + 21) |
| Runs | 304 s and 296 s (PK-23: 323 s and 410 s) |
| Reproducibility | **byte-identical**, sha256 `168289cb6efc95aa…` |
| PK-23 parity | dropping the 21 score columns gives sha256 **`692bf959c68173be…`**, exactly PK-23's reported export |

# Historical Score Distribution

Every full-window bar of the 62 NSE series (40,843 bars; each built exactly as `compute_at` builds it) is scored. The sanity run confirms the as-of path equals this full-history path on every example.

| count | mean | median | std | min | max | P10 | P25 | P50 | P75 | P90 |
|---|---|---|---|---|---|---|---|---|---|---|
| 40,843 | 51.81 | 51.20 | 8.62 | 13.8 | 97.0 | 42.0 | 46.5 | 51.2 | 56.4 | 62.2 |

**Packet buckets:**

| 0–24 | 25–44 | 45–59 | 60–74 | 75–100 |
|---|---|---|---|---|
| 35 (0.1%) | 7,652 (18.7%) | 27,407 (67.1%) | 5,061 (12.4%) | 688 (1.7%) |

**Chosen bands:**

| Band | Bars |
|---|---|
| weak | 35 (0.1%) |
| limited | 2,511 (6.1%) |
| mixed | 32,548 (79.7%) |
| moderate | 5,061 (12.4%) |
| strong | 688 (1.7%) |

**Reading.** Most sessions are mixed, as they should be for a neutral-anchored evidence score. The median sits 1.2 points above 50 without being forced there. 'Strong' is rare, and 'weak' is rarer because distribution days are slightly less frequent than accumulation days in this bull-heavy sample. Median by confidence: HIGH 51.3, MEDIUM 51.1, LOW 49.4.

# Strategy-Level Score Distribution

At the 410 historical strategy signals (research export):

| Strategy | n | mean | median | std | P10 | P25 | P75 | P90 | Chosen bands (weak / limited / mixed / moderate / strong) |
|---|---|---|---|---|---|---|---|---|---|
| momentum_breakout | 145 | 74.96 | 77.3 | 10.55 | 60.3 | 69.6 | 82.6 | 85.5 | 0 / 0 / 14 / 44 / 87 |
| trend_pullback | 159 | 69.97 | 72.0 | 12.17 | 52.2 | 62.4 | 79.7 | 84.3 | 0 / 1 / 32 / 61 / 65 |
| rsi_mean_reversion | 106 | 46.33 | 46.25 | 4.83 | 40.5 | 43.3 | 48.2 | 52.0 | 0 / 8 / 96 / 2 / 0 |
| all | 410 | 65.62 | 68.7 | 15.40 | 44.4 | 49.0 | 79.4 | 84.3 | 0 / 9 / 142 / 107 / 152 |

**Does the score merely duplicate the strategies' own volume rule?** Rank correlation between the score and the strategy's legacy volume multiple *within* each strategy's signals:

| Strategy | vs legacy multiple | vs RVOL20 |
|---|---|---|
| momentum_breakout | −0.08 | −0.07 |
| trend_pullback | 0.01 | 0.00 |
| rsi_mean_reversion | 0.24 | 0.23 |

**Reading:**
- The two volume-gated strategies score high on average, because their signals already require high volume and a breakout or recovery.
- *Within* those signals, however, the score is essentially independent of how far the volume rule was exceeded. It spreads them from 42 to 97 on the other evidence (closes, repeated sessions, OBV, rejection).
- RSI mean-reversion signals, which follow sell-offs, sit in the mixed band with net distribution evidence (mean distribution component −3.35).
- No strategy was modified.

# No-Lookahead Validation

- **Inherited.** The score reads only the PK-23 feature object, whose no-lookahead properties PK-23 proved (tests 28–30 there).
- **Test 25** (four dates on the golden series): the score at T is identical — the whole object, not just the number — when every row after T is scaled (prices ×0.37, volume ×60 + 7), and when the frame is cut at T.
- **Test 26 / 26b:** outcome keys are rejected anywhere in the input, and research scores are independent of outcomes.
- **Test 33:** a ticket's score does not move when 15 heavy later sessions are appended, while today's score does.
- **Mutants caught:** M7a (Signal Detail scores today), M7b (research features see the full frame) and M7c (the API ignores `as_of`).
- **Repeats:** the no-lookahead/isolation subset ran 20/20 green.

# Strategy Isolation

- **Structural (test 29):** "smart_money" appears in none of 22 decision-path files:
  - `strategy_engine`, `risk_engine`, `backtest`, `backtest_analytics`, `backtest_costs`, `execution`;
  - `automation`, `todays_actions`, `paper_broker`, `trailing_engine`, `order_monitor`, `workers`, `lifecycle`, `position_quantity`;
  - `routes/tickets`, `routes/strategies`;
  - all strategy modules and `strategies/__init__`.
- **Behavioural:**
  - test 29b: with `score()` replaced by a tripwire that raises, all three strategies reproduce the golden signal dates exactly;
  - test 29c: a PK-21 backtest simulation makes zero score calls and trades only golden signals;
  - test 30: a full scan (rank, size, risk-check), ticket fetch, ticket list and **confirm** make zero score calls, and the ticket record contains no score. Opening the chart then makes exactly one call, which proves the tripwire works.
- **Unchanged files:** `git diff` is empty for `backend/strategies/*`, `strategy_engine.py`, `risk_engine.py`, `backtest.py` and `execution.py`.
- **Mutant M9** (momentum_breakout consults the score) is caught by 4 tests.

# Historical Sanity Cases

`sanity.py` (scratchpad; scratch DB copy) works as follows:
- it picks the most extreme HIGH-confidence, OK-quality bar for each situation, deterministically and never the same bar twice;
- it **recomputes** the case through the live path (`compute_at` on the history cut at that date, then `score()`);
- it asserts equality with the full-history score. All 10 matched.

No forward return was read.

| # | Situation | Case | Key features | Score / band | Main contributors |
|---|---|---|---|---|---|
| 1 | Strong volume + strong close | ADANIPOWER 2025-01-14 | RVOL 11.4×, close location 0.97, **+19.3%**, OBV leads price | **78.9** strong, HIGH | close +6, participation +5, pullback recovery +5.9, OBV +5.0; price credit tapered to 1.5 of 3 |
| 2 | Strong volume + weak close | ITC 2026-01-01 | RVOL 34×, close location 0.03, −9.7%, range 9.9% | **25.7** limited | weak close −8, exhaustion −8, OBV falling −4, 1 distribution-like day −4.3 |
| 3 | Repeated accumulation-like sessions | ADANIGREEN 2024-09-23 | 4 of 10 (5 of 20) accumulation-like, OBV +0.56 confirmed | **84.9** strong | accumulation +13, OBV +8, close +4.9 |
| 4 | Repeated distribution-like sessions | ADANIGREEN 2024-12-04 | 3 accumulation-like / **4** distribution-like in 10, close location 0.11, −4.0% | **39.0** limited | accumulation +13 cancelled by distribution −13; weak close −5.2, exhaustion −3.0 |
| 5 | OBV leads price | SOLARINDS 2026-04-30 | OBV slope +0.76 at a 20-session high, price not; RVOL 0.55× | **54.7** mixed | OBV +8, 1 distribution-like day −4.3 |
| 6 | Price unconfirmed | AWL 2025-11-24 | close at a 20-session high, OBV −0.40 | **52.0** mixed | OBV non-confirmation −8.0 against 3 accumulation-like days in 20 (+6.5) |
| 7 | Contraction, then expansion | ADANIENT 2026-05-14 | prior-5/prior-20 0.58, then RVOL 3.7×, close location 0.96, +8.6%, breakout | **97.0** strong | every positive component near its maximum |
| 8 | Extreme-volume exhaustion bar | ADANIENT 2024-01-03 | median RVOL 6.9×, range 8.3%, upper wick 62%, close location 0.19 | **35.5** limited | exhaustion −8, weak close −6.1, wick −6.1 |
| 9 | Low-volume pullback, then recovery | ADANIENT 2024-07-26 | pullback at 0.28× the volume into the high, recovery 2.13× the pullback, +3.6%, close location 0.76 | **64.5** moderate | pullback recovery +6, 1 accumulation-like day +4.3 |
| 10 | Failed breakout attempt | ADANIGREEN 2025-09-23 | high above the prior-30 closing high, close back below; RVOL 3.7×, close location 0.19 | **40.6** mixed | failed breakout −6, weak close −6.2 |

**What the first run found.**
- **Case 8 was also picked as the "best pullback recovery"**: the exhaustion bar earned the full 6 pullback points, because it was an up bar at 8× the pullback's volume, even though it closed at 0.19 under a 62% wick. That is not a constructive recovery, so the pullback component now requires a close that held (the same close gate participation and contraction use). Case 8 moved from 41.5 to 35.5, and case 9 is now a genuine quiet-pullback recovery.
- **Case 6's participation sentence said "Elevated participation" at RVOL 0.54×** (the median-based 1.64× carried `p`). It now reads "Participation: RVOL20 0.54× (median-based 1.64×, …)".
- **Count sentences** now read "3 accumulation-like session(s) in the last 20, none in the last 10" instead of "0 … in the last 10 (3 in the last 20)".

These are sanity-driven corrections of meaning, not outcome-driven tuning (see *Deviations* 2).

# Exploratory Outcome Appendix

**EXPLORATORY ONLY · IN-SAMPLE · NOT VALIDATION.** Computed once from the export's `outcome_*` columns after the model was frozen. **No weight, ramp or band was changed because of it.**

| Score bucket | Signals | Median forward 10-day return % | Share with 10-day return > 0 | Became trades | Median trade net R |
|---|---|---|---|---|---|
| 0–24 | 0 | — | — | 0 | — |
| 25–44 | 49 | 0.02 | 53.2% | 35 | −0.50 |
| 45–59 | 102 | 0.02 | 50.0% | 70 | −0.65 |
| 60–74 | 107 | 0.94 | 57.0% | 66 | +0.23 |
| 75–100 | 152 | −0.44 | 43.4% | 98 | −0.03 |

**Reading.** There is **no monotone relationship**: the highest bucket has the weakest median 10-day return in this sample. Nothing here supports a claim of predictive edge, in either direction. The sample is small, overlapping and in-sample, and bucket membership is confounded by strategy: 87 of the 152 "strong" signals are momentum breakouts. PK-25 must test this properly (out of sample, per strategy, walk-forward, with the score as an *incremental* feature).

# Performance

`perf.py` used real TCS bars (754) seeded into a throw-away database. Timings are medians.

| Measurement | Time |
|---|---|
| `score()` alone, one feature object | **0.107 ms** |
| `compute_at`, 61-bar window (PK-23, reference) | 16.3 ms |
| `GET /api/volume-intelligence/TCS`, cold / warm | 30.6 / 13.4 ms |
| `GET /api/smart-money-score/TCS`, cold / warm | 31.3 / 13.8 ms |
| Signal Detail `build_payload`, cold cache, without / with score | 41.2 / 39.8 ms (difference within noise) |
| Research export, 410 signals, `--with-trades` | 304 s / 296 s (PK-23: 323 s / 410 s) |

The score reuses the one feature computation in every path, and nothing is computed twice per request.

# Tests Added

| File | Tests | Covers |
|---|---|---|
| `tests/test_smart_money_score.py` | 52 | See below |
| `tests/test_smart_money_score_api.py` | 46 | See below |

**`test_smart_money_score.py`:**
- **1–4 baseline:** neutral = 50; bounded over 3,000 random feature sets; the extremes reach exactly 100 and 0; deterministic; the input is not mutated; version and weights present; another feature version refused; JSON-serialisable.
- **5–10 positive evidence:** including volume saturation and breakout participation.
- **11–15b negative evidence:** including the accumulation/distribution mirror and exhaustion.
- **16–17b outliers**, including repeated evidence beating an isolated spike.
- **18–22d confidence:** real short/partial history, split gap, special session, liquidity, staleness, ZERO codes, unknown status, coverage.
- **23–24 explanation and wording.**
- **25–26 no lookahead**, including outcome rejection.
- **27 NSE/BSE separation.**
- **Robustness:** 1.99 against 2.01, ±1% perturbation, monotonic close location.

**`test_smart_money_score_api.py`:**
- **31–31d endpoint:** read-only, as-of, 404/422, wording.
- **32–32c:** null score for no or flagged history; staleness.
- **27–28:** exchange separation and deterministic selection.
- **33–33c Signal Detail:** signal-date snapshot, stable after new rows, failure isolation.
- **34–35 UI source:** rendering, disclaimer, no P&L colours or advice wording.
- **Research export:** columns and order; outcome independence; `score_record` guard; CLI.
- **29–30 isolation:** 22-file scan, tripwire golden signals, backtest, scan/approval.

No existing test was changed.

# Mutation Results

`mutations.py` (scratchpad) applied each mutant to one file and ran both PK-24 suites. It restored the original bytes in `finally` and **verified the sha256** each time (12/12 matched).

| # | Mutation | File | Result | First catching tests |
|---|---|---|---|---|
| M1 | remove negative penalties | score | **caught**, 13 | 2, 2b, 11, 12 |
| M2 | RVOL dominates the whole score | score | **caught**, 13 | 2, 2b, 6, 7 |
| M3 | score allowed above 100 (clamps and count cap removed) | score | **caught**, 3 | 2, 2b, 7 |
| M4 | accumulation / distribution swapped | score | **caught**, 7 | 2b, 7, 12, 17 |
| M5 | weak-close penalty removed | score | **caught**, 4 | 2, 2b, 11, 16 |
| M6 | confidence degradation ignored | score | **caught**, 7 | 19b, 21, 21b, 22 |
| M7a | Signal Detail scores today, not the signal date | signal_chart | **caught**, 1 | 33 |
| M7b | research features see the full (future) frame | volume_research | **caught**, 1 | 26 |
| M7c | API ignores `as_of` | route | **caught**, 1 | 31b |
| M8a | outcome guard removed | score | **caught**, 2 | 26, 26b |
| M8b | an outcome leaks into the research score | volume_research | **caught**, 1 | 26 (research) |
| M9 | momentum_breakout consults the score | strategy | **caught**, 4 | 29, 29b, 29c, 30 |

**12/12 caught.** A first run stopped at M8a because the harness matched `\n` patterns against CRLF working copies (`core.autocrlf=true`). It was fixed to match either newline style and M8a–M9 were rerun; M1–M7c had already run and been restored.

# Targeted Repeats

| Run | Result |
|---|---|
| PK-24 suites (2 files) ×10 | **10/10 green**, 98 passed each, about 19 s |
| No-lookahead/isolation subset ×20 (tests 25, 26, 29, 30, 31b, 33) | **20/20 green**, 38 passed each, about 13 s |

No flake was seen.

# Browser Validation

**Setup:**
- `tests/demo_server.py --market-open` (FakeKite, throw-away DB), with its output sent to a **file**;
- Playwright with Edge headless and the app's own CSP enforced;
- a `securitypolicyviolation` listener installed before any page script.

**29/29 checks passed:**
- **API:** 200 with score, version and confidence (BHARTIARTL 75.6, HIGH); an NSE token resolved; clean wording; 404 for an unknown symbol; 422 for a bad date.
- **UI, walking all 3 visible pending signals** (BHARTIARTL momentum, BHARTIARTL pullback, DIVISLAB RSI):
  - score and confidence render and match the API payload;
  - "Score as of" equals the signal candle date;
  - the disclaimer is present;
  - the section sits between Volume context and Risk checks;
  - no forbidden wording;
  - no horizontal overflow.
- **420 px:** no overflow in the section.
- **Zero CSP violations, zero console or page errors, zero 4xx/5xx** during the UI walk.

**Screenshots** (dark, light and 420 px) are in the session scratchpad (`browser24/`). Both themes are legible, and the meter uses the accent colour only.

**Note.** A first walk timed out on a hidden Chart button: the approvals list renders 6 buttons, 3 of them in collapsed cards. The walk was restricted to visible buttons; the product is unaffected.

# Full Suite ×3

Three consecutive runs of the whole suite, after the last code change:

| Run | Result |
|---|---|
| 1 | **1,415 passed**, 0 failed (193 s) |
| 2 | **1,415 passed**, 0 failed (203 s) |
| 3 | **1,415 passed**, 0 failed (232 s) |

1,415 = the 1,317 baseline + 98 new PK-24 tests; 0 xfailed, 0 xpassed. The live DB sha256 afterwards is `1378f8b4…dacf`.

# Known Limitations

1. **A single extreme up-day that closes at its high can reach "strong"** (sanity case 1: ADANIPOWER +19%, 78.9). The price credit is tapered, but a quiet pullback before it adds pullback-recovery credit, and the score cannot tell a news gap from sustained participation. Whether such bars should be damped further is a PK-25 question.
2. **Counts saturate at about 3 effective days.** A symbol with 3 accumulation-like and 4 distribution-like days nets to zero on that pair (sanity case 4). The difference is carried by the single-bar and OBV terms instead.
3. **Inherited PK-23 definitions:**
   - generic pullback (86% of bars have one);
   - breakout lookback fixed at 30;
   - special sessions flagged only in the bar or its 20-session baseline;
   - split-volume adjustment unverified.
4. **Recomputed on demand, not snapshotted** (the same design as PK-23). A signal's score is deterministic for unchanged history. After a PK-22 rebuild it reflects the corrected history, not what was on screen at signal time.
5. **The in-sample outcome appendix is small and confounded** (410 signals, overlapping windows, dominated by momentum breakouts). It supports no conclusion.
6. **Bands and ramps are round, hand-set anchors.** They are documented and versioned, not optimised.
7. **Pre-existing:** the main app shell overflows horizontally at 420 px (PK-23 note 10); the new section itself does not.

# Deferred PK-25 / PK-26 Work

**PK-25:**
- out-of-sample, walk-forward validation of whether the score (or any component) adds incremental information beyond each strategy's own rule;
- per strategy, using the research export's `sms_*` columns and `outcome_*` labels;
- deciding whether damping of extreme single-day moves (limitation 1) is warranted.

**PK-26:** the scanner/ranking UI. `top_positive`, `top_negative` and the band labels are ready for it.

**Open decisions:**
- whether to persist a signal-time score snapshot on the ticket;
- whether special sessions should be excluded from baselines rather than only lowering confidence.

**Not started:** PK-25 to PK-28, live weighting, trade rejection by score, and any recommendation.

# Deviations From Plan

1. **One additive change to the PK-23 feature object** (`bar.open`, `bar.high`, `bar.low`). The failed-breakout check needs the bar's high, and the alternative, reconstructing it from rounded wick and range percentages, would have been fragile. `FEATURE_VERSION` stays 1, because no existing value changed; the export parity sha proves it.
2. **Three changes after the first historical pass, none outcome-driven:**
   - (a) the single-bar trio was reduced from 20 to 14 points for **redundancy** (Pearson 0.73–0.80);
   - (b) the penalty maxima were rebalanced to sum to 50, so the scale is symmetric and 0 is reachable;
   - (c) the pullback-recovery component was gated on a close that held, plus two wording fixes, after the **sanity cases** showed an exhaustion bar earning recovery credit.

   The outcome appendix was computed only after these, and nothing changed afterwards.
3. **Liquidity damps toward 50 instead of subtracting points.** A subtraction would have made thin names look distribution-like; shrinking toward neutral expresses "less trustworthy either way", and confidence carries the rest.
4. **Bands are symmetric about 50** (±10 mixed, ±25 strong or weak) rather than the packet's example 45–59 mixed range. Both bucketings are reported.
5. **Signal Detail section title** is "Accumulation evidence". The score's full name appears in the section body, served by the API, so the PK-23 volume section's wording test stays intact.
6. **Numbering.** The packet listed some sub-packets twice under different letters. They were implemented as: A–E score, F confidence, G/H explanation and model, I API, J UI, K export, L/M distribution and redundancy, N robustness, O no-lookahead, P isolation, Q signal date.
7. `docs/OPUS55_AUDIT_REPORT.md` was not modified.
