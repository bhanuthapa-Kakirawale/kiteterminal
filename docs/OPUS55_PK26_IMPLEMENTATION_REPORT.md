# OPUS55 PK-26 Implementation Report: Price-Volume Research (a descriptive evidence explorer)

| | |
|---|---|
| Packet | **PK-26** only. PK-27 and later were not started. No strategy, scan, ranking, risk, approval, sizing, execution, GTT or backtest file was changed, and the PK-25 manifest was **not** superseded. |
| Baseline before | **1,461 passed**, 0 failed, 0 xfailed, 0 xpassed, re-measured on the untouched code (`00584a0`) before the first edit. |
| Backups | git tag `backup-pre-pk26` and branch `backup/pre-pk26` at `00584a0`. `backend/kite_terminal.db.pre_pk26_bak`, a copy of the live file taken while its WAL was empty (0 bytes), so the copy is complete. Not committed (`*_bak` is git-ignored). |
| After | **1,559 passed** (1,461 + 98 new PK-26 tests), three consecutive times. See *Full Suite ×3*. |
| Live DB | `backend/kite_terminal.db` sha256 `1378f8b4…dacf` before and after all work. It was never opened by app code; every real-data run used a scratch copy of the backup, deleted afterwards. |

# Executive Summary

PK-26 adds a **research view, "Price-Volume Research"** ("Explore price-volume evidence across the watchlist."), built as an exploration tool, not a signal generator. It shows, for every watchlist instrument, the PK-23 volume evidence with Smart Money Score v1.0 as **one descriptive reference column**. It answers where volume is unusual, expanding or contracting, where price closes near its high or low, where OBV is improving, and where accumulation-like or distribution-like sessions appear. It does not answer "what should I buy".

What was added:

- `backend/research_scanner.py`: a batch service that computes the latest (or as-of) PK-23 `VolumeFeatures` and PK-24 score for the whole watchlist with **one query per concern** (three `ohlc_daily` reads for all symbols together), cached by history content and both definition versions.
- Three read-only routes: `GET /api/research/volume-scan` (the batch), `…/volume-scan.csv` (export) and `…/volume-scan/{instrument_token}` (drill-down).
- `frontend/research.js`, a new sidebar page under *Research*, with transparent presets, explicit filters, user-chosen sorting, a detail dialog and CSV export.

What it deliberately does **not** have, because PK-25 classified the score LIMITED:

- no ranking, composite, "opportunity" or "trade" score;
- no default filter, and the default order is **symbol A→Z**, never the score;
- no band label, badge or colour for the score (82 is shown as `82.0`);
- no write, broker call, order, GTT or ticket change.

The PK-25 limitation, the LIMITED validation status and the "not proof of institutional buying" disclaimer are on screen at all times.

Validation:

- **Parity.** Every row's features equal `features_for_instrument` and every score equals `score()`, field by field (tests). On the real backup this held for **51/51** watchlist instruments.
- **Mutations:** 14/14 caught, each file restored and sha256-verified.
- **Targeted repeats:** PK-26 suites ×10 and the isolation/no-ranking subset ×20, all green.
- **Export:** byte-identical across two fresh processes on real data, twice (two query variants).
- **Browser:** 34/34 checks under the app's enforced CSP, at desktop width and 420 px.
- **Full suite:** green three consecutive times.

# PK-25 Validation Constraint

PK-25 (frozen manifest, holdout evaluated once) classified Smart Money Score v1.0 **LIMITED**:

- pooled score/return relationships were largely explained by which strategy produced the signal;
- within-strategy incremental value was weak and not established;
- adding the score made the strategy + RVOL20 model worse out of sample, in **6 of 6** walk-forward folds;
- a score ≥ 75 filter reduced expectancy in **both** volume strategies in **both** out-of-sample periods;
- extreme single-day up-bars often score 75+.

PK-26 treats these as design constraints, and each one is pinned by a test or a mutant (see *Mutation Results*).

# Why PK-26 Is Not A Ranking Scanner

A scanner that sorted by the score, pre-filtered on it, or coloured high scores would be acting on a relationship PK-25 could not establish, and at the top of the range, one that went the wrong way inside the volume strategies. So:

- the page title and every label are neutral ("Price-Volume Research", "SMS v1.0"). The page is not called a "Smart Money Scanner";
- the server's `scanner.ranking` is `null` and it states that "Rows are ordered only by the column you choose; there is no composite rank";
- the default sort comes from the server and is the symbol; the client has no default of its own (tested);
- the score *can* be sorted and range-filtered by hand, because the packet allows it as a labelled research tool. The filter shows an inline warning and is off by default.

# Research Scanner Architecture

```
watchlist (default account) ──► research_scanner.build_scan(conn, as_of)
                                   │  load_universe        1 query  (watchlist)
                                   │  load_windows         2 queries (last 62 bars of every token; per-token counts)
                                   │  load_statuses        1 query  (PK-22 status + verified gaps)
                                   │  load_identity        2 queries (exchange; other stored series of each symbol)
                                   │  load_active_signals  1 query  (pending strategy entry tickets; latest view only)
                                   │  per token: evaluate()  ── cache ──► vi.compute_at + freshness ─► sms.score
                                   ▼
                    flat descriptive rows  +  evidence {features, smart_money (no band)}
routes/research_scanner.py ── filter (conditions) ── sort ── JSON / CSV / detail
frontend/research.js ── one fetch ── same conditions + same sort, applied locally (parity-tested)
```

- **Dependencies are one-way.** The scanner reads PK-23, PK-24, PK-22 status, the watchlist and tickets. Only `main.py` and its own route import it; tested over every `backend/*.py` and the 22 PK-24 decision-path files.
- **Frozen files untouched.** `volume_intelligence.py`, `smart_money_score.py` and `volume_research.py` are hash-frozen by the PK-25 manifest, so nothing was added to them. The scanner reuses three of PK-23's helpers directly (`_normalise`, `_history_digest`, `_freshness`), so its normalisation, cache digest and staleness rule are *the same code*, not copies (see *Deviations* 2).

# Batch Feature Calculation

- **Rows read.** Each token contributes its last `LOOKBACK_BARS + 1 = 62` rows on or before `as_of`, which is exactly the window PK-23 computes on plus the one extra row its gap check needs. They come from one `ROW_NUMBER() OVER (PARTITION BY instrument_token …)` query for all tokens (SQLite 3.50.4). A second query gives each token's full row count, so `data_quality.bars_available` equals PK-23's.
- **Computation.** `vi.compute_at` runs on that frame with the token's PK-22 status and verified gaps, and then PK-23's own post-processing is applied (freshness, stale warning). `sms.score` then scores the same object. Nothing is computed twice.
- **Parity (tests 7–12).**
  - Five varied random-walk symbols: the whole `VolumeFeatures` object equals `features_for_instrument`, latest and as of a past date.
  - The score object (minus `band`) equals `score()` and `/api/smart-money-score`.
- **Parity on real data.** On the scratch copy of the backup: **0 mismatches out of 51** watchlist instruments, for both the features and the score.
- **Never the per-symbol path.** Test 1 replaces `features_for_instrument` with a tripwire. Test 1b counts SQL: exactly **3** `ohlc_daily` statements for 5 symbols.
- **Unusable history is never fabricated** (tests 5, 6).
  - `SUSPECT`, `NEEDS_REFRESH`, `INSUFFICIENT_HISTORY` and `NO_HISTORY` give `data_quality: UNAVAILABLE`, every feature `null`, `sms_score: null` and `INSUFFICIENT_DATA`, with the warnings.
  - `PARTIAL_HISTORY` is DEGRADED, with the percentile `null`.
- **Special sessions** (test 5b): a row whose baseline contains the 2024-03-02 disaster-recovery Saturday shows `special_session: true`, `SPECIAL_SESSION_IN_BASELINE` and a lowered confidence.

# Instrument / Exchange Identity

- **Identity is the instrument token.** The universe is the default account's watchlist, using **the watchlist's own token**, which is PK-23 `resolve_instrument`'s first choice. Test 3 checks the scanner and `resolve_instrument` agree deterministically.
- **NSE and BSE series are never merged.** A BSE series of a watched symbol is not a row. It is named in the row's `notes`: "A BSE series of TCS (token 139024900) is also stored; its volume is not comparable and is not combined with this one." Test 2 checks the RVOL comes from the NSE series only.
- **On the real backup,** 2 of the 51 watched symbols carry that note (of PK-23's six duplicates, the others are not watched). All 51 rows are NSE.
- **Exchange** comes from the instrument cache, falling back to the token's segment byte (1 = NSE, 4 = BSE).

# Filters

These are explicit, user-selected filters. Nothing is on by default: test 19 checks the default response has `conditions: []` and every row. Each filter maps to one declared condition `{field, op, value}`. Conditions are ANDed, and **a null value never matches**, so an unavailable row never passes a threshold (test 19c).

| Query parameter | Condition |
|---|---|
| `min_rvol` | `rvol_20 ≥ x` |
| `min_volume_percentile` | `volume_percentile_60 ≥ x` |
| `min_avg_traded_value` (₹; the UI takes ₹ crore) | `avg_traded_value_20 ≥ x` |
| `min_close_location` | `close_location ≥ x` |
| `min_accumulation_10` / `max_distribution_10` | `accumulation_like_10 ≥ x` / `distribution_like_10 ≤ x` |
| `obv` = rising / falling / new_high / obv_leads_price / price_unconfirmed | trend, 20-session high, or divergence label |
| `volume` = contracting / expanding / contraction_then_expansion | PK-23 contraction / expansion flags |
| `breakout` = present / absent | `is_breakout` |
| `pullback` = any / low_volume | `in_pullback` / `low_volume_pullback` |
| `signal` = active / none | an active strategy entry signal |
| `tag` | watchlist tag |
| `sms_min`, `sms_max` | **optional** score range, labelled "research reference only, off by default"; the response and the UI add *"Score v1.0 did not show independent out-of-sample predictive value."* |

Unknown values, non-numbers, NaN, unknown presets and unknown sort keys give **422 `INVALID_FILTER`** (test 19b). Tests 13–19 check each filter keeps exactly the rows its predicate selects, and no others.

# Presets

Six presets, each a transparent AND of PK-23 primitives **at PK-23's own thresholds** (test 20b). None uses the score (tests 20–23 and mutant M10), and none is named as advice (test 20c). The API returns every condition, and the UI prints them, for example "RVOL20 ≥ 1.5 AND Close location ≥ 0.75".

| Key | Label | Conditions |
|---|---|---|
| `unusual_participation` | Unusual participation | RVOL20 ≥ 2.0 AND volume percentile ≥ 90 |
| `close_near_high_on_volume` | Close near the high on elevated volume | RVOL20 ≥ 1.5 AND close location ≥ 0.75 |
| `accumulation_like_pattern` | Accumulation-like pattern | net accumulation (10) ≥ 1 AND OBV slope-20 ≥ 0.1 AND no high-volume weak close |
| `low_volume_pullback` | Low-volume pullback | in pullback AND pullback volume ratio ≤ 0.8 |
| `contraction_then_expansion` | Contraction → expansion | prior-5/prior-20 ≤ 0.8 AND RVOL20 ≥ 1.5 |
| `distribution_caution` | Distribution-like / caution | RVOL20 ≥ 1.5 AND close location ≤ 0.25 AND distribution-like (10) ≥ 1 |

Tests 22b and 23b build a real low-volume pullback and a real high-volume weak close and check each is found by its preset.

# Sorting Policy

- **Default:** `symbol` ascending (`DEFAULT_SORT`), served to the UI as `sort.default`. Test 24 checks it, and checks that the fixture's scores are *not* in that order, so the test has teeth. Mutant M1 is caught.
- **User sort:** any numeric column, including the score, either direction (test 26).
  - Missing values always sort last.
  - Ties break by symbol, then token, so a snapshot always gives the same order (tests 26b, 37).
  - The JS sort equals the server sort for every sortable column in both directions (UI test).
- **No server "best" order.** Rows carry no key matching `rank|composite|opportunit|trade_score|priority|weight|recommend` (test 25).

# Smart Money Score Display

- **Row fields:** `sms_score`, `sms_version`, `sms_confidence`, `sms_coverage` and `sms_failed_breakout_points` (see *Export*). There is **no band**: the scanner's copy of the PK-24 object drops `band` (`smart_money_view`), so "Strong accumulation-like evidence" can never reach this page.
- **Table:** column "SMS v1.0" shows the number with one decimal in the ordinary text colour, and "Conf." shows the level. The browser check confirms the computed colour equals the neighbouring cells'.
- **Detail:** "SMS v1.0: 75.6 (50 is neutral) · Confidence: HIGH · coverage 100%", then *Largest positive contributors*, *Cautions*, confidence notes and warnings. All of these are PK-24's own `top_positive`, `top_negative` and reasons.
- **Test 29 / UI test.** A fixture scoring ≥ 75 is rendered through the real table and detail code. The visible text contains none of "strong", "buy", "sell", "top pick", "best", "bullish", "opportunity", and no P&L or badge classes. Mutants M4a and M4b are caught.
- **Extreme up-bar note.** A row matching PK-25's pre-declared cohort (≥ +8% day, RVOL20 ≥ 3, close location ≥ 0.75) carries `extreme_up_bar: true`. The detail then adds PK-25's caution that such bars frequently score 75+ from one bar rather than repeated sessions.

# PK-25 LIMITED Warning

**Always on the page** (the notice box above the table) and **in every detail view**:

> **SMS v1.0:** Smart Money Score v1.0 is descriptive price-volume evidence. PK-25 found no independent out-of-sample predictive value after controlling for strategy.
> Validation status: LIMITED — no incremental value beyond strategy was established.
> *Price-volume evidence, not proof of institutional buying.*

**The detail view also lists PK-25's findings**, so the user does not need to open the report:

- strategy mix explains pooled results;
- within-strategy value not established;
- the model got worse in 6/6 folds;
- ≥ 75 reduced expectancy in both volume strategies;
- extreme up-bars.

**Source of the text.** It comes from the server (`SMS_LIMITATION`, `SMS_VALIDATION`, `sms.DISCLAIMER`); the UI never hardcodes it. Test 36 ties `SMS_VALIDATION.status` to `research/pk25/holdout_results.json`'s classification label (`LIMITED`) and to the manifest's frozen score version.

**Mutants M2a, M2b, M3a and M3b** each remove the warning or the disclaimer, from the API or from the UI; all four are caught.

# Detail View

Clicking or pressing Enter on a row opens a dialog from **one** request to `/api/research/volume-scan/{token}`. The response holds the row, the full PK-23 `VolumeFeatures` object and the PK-24 object without `band`, all produced by the same batch code for that token (tests 30–31). The dialog shows:

- the as-of candle, feature version and "descriptive measurements, not a signal";
- the current strategy signal, if any (see below);
- **Price-volume evidence:**
  - RVOL20, with the median-based figure;
  - volume percentile;
  - traded value against its 20-session average, with the low-liquidity note;
  - close location and price change;
  - OBV trend, slope, 20-session high and divergence;
  - accumulation-like / distribution-like counts (10 and 20 sessions);
  - contraction and expansion;
  - breakout context and pullback context;
  - flags;
- **In words:** PK-23's own factual sentences;
- **SMS v1.0 — reference only:** score, confidence, contributors, cautions, validation box and disclaimer;
- **Data quality:** status, codes, history status, bars stored, and all warnings and notes.

An instrument that is not on the watchlist gives 404 `NOT_IN_WATCHLIST`.

# Strategy Signal Context

**What counts.** Strategy **entry** tickets awaiting a decision (`draft`, `pending_confirmation`; not archived), read with one SELECT and attached per token as `{strategy, signal_date, status}`.

**Where they appear.** The table shows a neutral chip per strategy. The detail shows "Signal: momentum_breakout · 2026-09-30 candle · awaiting approval", followed by: "The scanner does not combine a signal and the evidence into one judgement; approval is decided in Approvals, unaffected by this page."

**Tests:**
- every evidence field of a row is identical before and after a signal exists;
- reading signals writes nothing (DB dump identical);
- `signal=active` filters to it;
- an as-of view attaches no signal, because a pending signal belongs to the latest candle.

No composite, opportunity or trade score exists anywhere (tests 25, 32d).

# Export

`GET /api/research/volume-scan.csv` takes the same parameters as the list, so the file is **exactly the rows shown, in the order shown**. The browser check downloads it and compares symbol order against the table. The file has 52 columns in a fixed order:

- scanner and feature versions;
- as-of and requested as-of dates;
- identity: token, symbol, exchange;
- history status, data quality and codes, special session, stale;
- every PK-23 feature shown on the page;
- `sms_score`, `sms_version`, `sms_confidence`;
- `sms_failed_breakout_points`, read **directly from the score's `components.failed_breakout.points`**, not from the PK-24 research export's `sms_failed_breakout` column, which holds a flag (PK-26N; test 11 checks it is a float equal to the component);
- `sms_validation_status` (`LIMITED`);
- `active_strategy_signals` (`strategy@signal_date` only);
- warnings.

**Not in the file:**
- no ticket or order id, holdings, quantity, P&L, watchlist notes or tags (test 39 plants a private note and tag and checks neither appears);
- **no `outcome_*` or forward column.** `export_csv` raises if one is ever added (test 38; mutants M8a and M8b).

**Other details:**
- A zero penalty is written as `0.0` rather than PK-24's `-0.0`. The API keeps PK-24's value untouched, for exact parity.
- **Reproducibility** on the real backup, two fresh processes each:
  - default: 51 rows, sha256 **`56a8b7ce…6763`** both times;
  - `sort=rvol_20&order=desc&min_volume_percentile=50`: 10 rows, byte-identical both times.

# Cache / Invalidation

- **Store.** An in-process LRU of 512 entries holds the serialised `(features, score)` per instrument. Each caller gets its own copy.
- **Key** (`cache_key`):
  - instrument token, symbol and requested as-of;
  - **sha256 of exactly the rows read** (PK-23's `_history_digest`);
  - row count, PK-22 history status and verified gaps;
  - `FEATURE_VERSION`, `SMART_MONEY_SCORE_VERSION` and `REQUIRED_FEATURE_VERSION`;
  - the liquidity threshold;
  - for the latest view only, the latest completed session (so staleness, which lowers confidence, is re-evaluated each day).
- **Tests:**
  - **40:** a PK-22 rebuild (series replaced and `mark_rebuilt`) recomputes, and 4.5× becomes 2.0×;
  - **40b:** a status change alone invalidates;
  - **40b2:** one bar edited in place, with the same count and no status change, invalidates (3.0×) — this is the test that catches mutant M7 (digest dropped from the key);
  - **40c:** both versions and the digest are in the key.
- Every response reports `timing.cache_hits` and `timing.cache_misses`.

# Performance

Real data: a scratch copy of the backup, 51 watchlist instruments, Windows laptop, medians.

| Measurement | Time |
|---|---|
| Batch queries (windows 68 ms, identity 28 ms, the rest < 1 ms) | ~97 ms |
| One instrument, cold (`compute_at` + score) | ~19 ms |
| **Cold scan**, 51 instruments (`build_scan`) | **1,138 ms** (range 1,071–1,186) |
| **Warm scan**, all cache hits | **215 ms** (range 202–244) |
| Per-symbol alternative (`features_for_instrument` + score ×51, cold) | 1,266 ms |
| API cold / warm (`GET /api/research/volume-scan`) | 1,089 / 220 ms; payload 81 kB |
| CSV export, warm | 211 ms, 21 kB |
| Detail, warm | 24 ms |

- **In the browser,** the page makes **one** request to fill the table (checked), and filters, presets and sorting are then instant, because they run locally on the same condition lists.
- **Where warm time goes.** The two batch queries account for roughly half of warm time; the rest is hashing each window and copying the cached objects.
- **Honesty note.** The very first performance attempt (fresh copy, migration, then 30 scans) produced no output for 10 minutes, with stdout block-buffered, and was stopped. The cause was not identified. Every later run completed normally: the step profile, the full performance script, and four export runs that each migrated a fresh copy. The in-app timings above come from those runs.

# PK-25 Manifest Compatibility

- **`research/pk25/validation_manifest.json` is unchanged and not superseded.** No PK-25 artifact was modified.
- **Frozen files are byte-identical to their frozen hashes:** the model files (`smart_money_score.py`, `volume_intelligence.py`) and every production file (`volume_research.py`, `backtest*.py`, `risk_engine.py`, `execution.py`, `strategy_engine.py`, the strategy modules).
- **Test 36** re-checks the frozen hashes independently, requires no `superseded_by`, and ties the displayed status to the holdout classification.
- **PK-25's own isolation tests 24–26** ran unchanged in every repeat, and pass.
- **The PK-24 export's `sms_failed_breakout` bug was not touched** (see *Known Limitations* 7).
- **No backend file contains "smart_money_validation".** PK-25's `test_20` enforces this, and it passes.

# Production Isolation

| Guard | Test |
|---|---|
| none of the 22 decision-path files (strategy engine, risk, backtest ×3, execution, automation, today's actions, paper broker, trailing, order monitor, workers, lifecycle, position quantity, tickets and strategies routes, every strategy module) mentions `research_scanner` or `volume-scan` | 32–35 (22 parametrised) |
| only `main.py` and the scanner's own route import the scanner | 32b |
| a full scan (rank, size, risk), ticket fetch and ticket list with `build_scan` and `evaluate` replaced by tripwires: zero calls | 32c |
| scanning, detail and CSV before a strategy scan leave the ticket's `entry_evaluation` without any score or research field | 32d |
| every scanner route leaves the DB dump byte-identical and makes no broker call | read-only test, test 1 |
| in the browser: no write and no order/ticket/GTT/position call from the research page (the only POSTs were the validator's own theme toggle to `/api/ui-prefs`) | browser check 13 |

**`git diff` is empty** for `backend/strategies/*`, `strategy_engine.py`, `risk_engine.py`, `backtest*.py` and `execution.py`. Changed tracked files: `backend/main.py` (+2), `frontend/index.html` (+25), `frontend/app.js` (+11/−1), `frontend/style.css` (+55).

# Tests Added

| File | Tests | Covers |
|---|---|---|
| `tests/test_research_scanner.py` | 86 | See below |
| `tests/test_research_scanner_ui.py` + `tests/js/render_research.js` | 12 | See below |

**`test_research_scanner.py`:**
- **Data 1–6:** the batch universe; one query per concern; NSE/BSE separation; NSE preference; as-of dates; suspect, needs-refresh, special-session, insufficient, partial and no history.
- **Features 7–12:** whole-object parity with PK-23 (latest and as-of); score, confidence, contributors and failed-breakout points against PK-24.
- **Filters 13–19c:** every filter; the labelled, off-by-default score filter; 422s; nulls never pass.
- **Presets 20–23b:** exact conditions; PK-23 thresholds; naming; real pullback and weak-close cases.
- **No ranking 24–26b:** default order; no rank-like keys; user sort; nulls last and tie-breaks.
- **Validation text 27–29.**
- **Drill-down 30–31.**
- **Signal context.**
- **Isolation 32–36:** including the PK-25 manifest.
- **Export 37–39.**
- **Cache 40–40c.**
- **Read-only and wording.**

**`test_research_scanner_ui.py`,** running the real `frontend/research.js` in Node:
- JS conditions and matches equal the server for every preset and a set of filters;
- the JS sort equals the server sort for every column and direction;
- the default sort comes from the server;
- the LIMITED text and disclaimer are rendered;
- the score-filter inline warning appears;
- a high score renders as a plain number with no label or colour class;
- the signal is shown beside the evidence;
- an unavailable row shows no numbers;
- every value is escaped;
- no advice wording or P&L colours in the source or CSS block;
- the page markup is CSP-safe (no inline handlers or scripts);
- exactly two fetches (the batch, and the detail).

No existing test was changed.

# Mutation Results

`mutations_pk26.py` (scratchpad) worked as follows:
- it applied each mutant to one file (either newline style);
- it ran both PK-26 files (plus PK-25 tests 25/26 for M5) with `-x`;
- it restored the original bytes in `finally` and **verified the sha256 (14/14 matched)**.

`git status` afterwards showed only the PK-26 changes.

| # | Mutation | File | Result | First catching test |
|---|---|---|---|---|
| M1 | default sort → Smart Money descending | scanner | **caught** | 24 default order |
| M2a | PK-25 LIMITED warning removed (API) | scanner | **caught** | 27 |
| M2b | PK-25 LIMITED warning removed (UI) | research.js | **caught** | UI limited/disclaimer |
| M3a | institutional-proof disclaimer removed (API) | scanner | **caught** | 28 |
| M3b | disclaimer removed (UI) | research.js | **caught** | UI limited/disclaimer |
| M4a | high score keeps its "Strong …" band | scanner | **caught** | 11–12 (and 29) |
| M4b | UI labels a 75+ score "Strong" | research.js | **caught** | UI high score |
| M5 | strategy ranking consults the research scanner | strategy_engine | **caught** | 32–35 [strategy_engine] (and PK-25 test 26) |
| M6 | scanner modifies strategy output (writes to tickets) | scanner | **caught** | signal context (DB unchanged) |
| M7 | stale cache survives a history change (digest dropped) | scanner | **caught** | 40b2 |
| M8a | outcome column leaks into the export (guard kept) | scanner | **caught** | 32d (export refuses, 500) |
| M8b | outcome column leaks with the guard removed | scanner | **caught** | 38 |
| M9 | score ≥ 75 filter on by default | scanner | **caught** | 1 (universe) / 19 |
| M10 | a preset secretly includes the score | scanner | **caught** | 20 [unusual_participation] |

**14/14 caught**, covering all eight mutations the packet required (M1, M2, M3, M4, M5, M6, M7, M8), plus M9 and M10.

# Targeted Repeats

| Run | Result |
|---|---|
| PK-26 suites (2 files) ×10 | **10/10 green**, 98 passed each, 34–38 s |
| Isolation / no-ranking / validation-text subset ×20 (tests 24–29, 32–36, default sort, high score, LIMITED, wording, read-only, signal context, plus PK-25 tests 24–26) | **20/20 green**, 44 passed each, about 7 s |
| Real-data export reproducibility ×2 (two query variants) | byte-identical |

No flake was seen.

# Browser Validation

**Setup:**
- `tests/demo_server.py --market-open` (FakeKite, throw-away DB, 10 watchlist symbols, 6 pending strategy signals), with its output sent to a **file**;
- Playwright with Edge headless and the app's CSP header enforced (`script-src 'self'`);
- a `securitypolicyviolation` listener installed before any page script.

It was run after the last code change: **34/34 checks passed.**

| # | Check | Result |
|---|---|---|
| 1 | page loads under *Research → Price-Volume Research* | pass |
| 2 | **one** `/api/research/volume-scan` request fills the table; no per-symbol volume or score request | pass |
| 3 | RVOL filter, RVOL + OBV filter match the server; score-filter warning hidden until used, shown once used; "Clear" restores all rows | pass |
| 4 | all 6 presets select exactly the server's rows and print their conditions | pass |
| 5 | manual sort by RVOL20 and by SMS v1.0 match the server | pass |
| 6 | default order is symbol A→Z, shown as "(default)", and differs from score order | pass |
| 7 | detail opens (one request) with every PK-23 row, SMS number/confidence/contributors/cautions, the LIMITED summary and the signal-beside-evidence note; closes | pass |
| 8 | LIMITED status and the disclaimer visible on the page | pass |
| 9 | no buy/sell/strong/best/top-pick/bullish/opportunity wording anywhere in the view; score cells use the ordinary text colour | pass |
| 10 | CSV downloads exactly the shown rows in the shown order; no outcome or account columns | pass |
| 11 | CSP clean (0 violations) | pass |
| 12 | console clean (0 errors, 0 page errors) | pass |
| 13 | no order, ticket, GTT or position write; the only POSTs were the validator's own theme toggle (`/api/ui-prefs`); 0 4xx/5xx | pass |
| N | at 420 px the research view does not overflow (the table scrolls inside its own `overflow-x: auto` wrapper), and neither does the detail dialog | pass |

**Screenshots** (dark and light at 1400 px, the detail dialog, 420 px) are in the session scratchpad (`browser26/`). Both themes are legible.

**Notes:**
- **Pre-existing 420 px overflow.** The page as a whole still overflows at 420 px because of the app shell's top bar, which is PK-23's pre-existing finding. PK-26 does not add to it (check N) and does not change the header.
- **A first run reported 33/34.** Check 13 was written too broadly: it counted the validator's own `/api/ui-prefs` POSTs and the dashboard shell's read-only `GET /api/orders`. The check was narrowed to non-GET calls; the product was unchanged.

# Full Suite ×3

Three consecutive runs of the whole suite, after the last code change:

| Run | Result |
|---|---|
| 1 | **1,559 passed**, 0 failed (259 s) |
| 2 | **1,559 passed**, 0 failed (273 s) |
| 3 | **1,559 passed**, 0 failed (313 s) |

1,559 = the 1,461 baseline + 98 PK-26 tests; 0 xfailed, 0 xpassed. The live DB sha256 afterwards is `1378f8b4…dacf`.

# Known Limitations

1. **Warm scan is about 215 ms** for 51 instruments, and cold is about 1.1 s. Both are interactive, and filtering and sorting are local, but warm time is dominated by the windowed query and the identity `DISTINCT` scan. A persistent cache was not added.
2. **The cache is per process** (as PK-23's). A restart recomputes.
3. **The universe is the default account's watchlist only.** There is no historical-universe management, as the packet required, and no scan of the whole NSE universe.
4. **Signal context covers pending strategy entries only.** Exit signals and executed positions are not shown; this is research, not portfolio state.
5. **The live DB has no `ohlc_history_status` table until its first post-PK-22 start.** The scanner, like PK-23's endpoint, relies on the migrations `init_db` runs at app start. On the migrated backup copy, every series reads `OK`.
6. **Stored data is stale.** The backup's newest candle is 2026-09-22, so every real row carries the staleness warning and a confidence one level lower until the history is refreshed. This is correct behaviour.
7. **PK-24 export bug deferred, not fixed.** The PK-24 research export's `sms_failed_breakout` column still holds the flag, not the points. It is frozen by PK-25 and was deliberately left alone. PK-26 reads the points from the score's component structure instead (`sms_failed_breakout_points`). A compatible fix would be to rename the flag to `sms_failed_breakout_flag` in a later packet that re-freezes the export.
8. **Inherited PK-23 / PK-24 definitions** are unchanged:
   - generic pullback;
   - breakout lookback fixed at 30;
   - special sessions flagged only in the bar or its baseline;
   - split-volume adjustment unverified.
9. **The pre-existing top-bar overflow at 420 px** remains (not PK-26's; not redesigned here).

# Deferred v1.1 Smart Money Research

Not started. These carry over from PK-25, and each needs a **new** frozen holdout of post-freeze data (signals after 2026-09-22):

- why the 75+ band underperforms 60–74 inside the volume strategies;
- whether extreme single-day up-bars should be damped (the scanner only *flags* PK-25's cohort);
- whether the score is redundant with the strategies' own volume rules.

# Deferred PK-27 Work

Not started. Candidates:

- a persistent or shared scanner cache, if a larger universe is ever wanted;
- a compatibility-safe rename of the PK-24 export's `sms_failed_breakout` flag, with a manifest supersession;
- scanning beyond the watchlist.

None of these was begun.

# Deviations From Plan

1. **Manual score sort and a labelled score filter exist.** PK-25's *Recommended Next Step* said "no ranking, sorting, filtering or prioritisation" by the score. The PK-26 packet, which is the later and explicit instruction, allows manual sorting and an optional, clearly labelled research filter. PK-26 follows the packet: neither is on by default, the filter carries PK-25's warning inline, and nothing is ordered or filtered by the score unless the user chooses it.
2. **Three private PK-23 helpers are reused** (`_normalise`, `_history_digest`, `_freshness`). A public batch function in `volume_intelligence.py` would have changed a PK-25 hash-frozen file, which would have required superseding the manifest. Reusing the helpers directly keeps the scanner's normalisation, cache digest and staleness rule identical to PK-23's, and parity is tested on the whole object.
3. **Window read plus a count, not full history.** The batch reads only the 62 rows any feature can use, and sets `data_quality.bars_available` from a count query. Every other field is byte-for-byte what the full-history path returns (tests 7–10; 51/51 on real data).
4. **The score's band is removed from the scanner's copy of the PK-24 object.** This is the one intentional difference from `score()`, so that no "Strong …" label can appear (packet PK-26H).
5. **Filtering and sorting run in the browser** on the server's own condition lists and sort rule, rather than one request per filter change. This gives one batch request per view (packet L). The API applies the same rules for direct use and for the CSV, and JS/server parity is tested.
6. **Preset naming.** The packet's example "Strong price/volume close" is named "Close near the high on elevated volume", so that no preset uses "strong". "Possible accumulation pattern" became "Accumulation-like pattern", matching PK-23's wording.
7. **Additions:**
   - `extreme_up_bar` (PK-25's cohort, descriptive note only);
   - an optional `as_of` date for research snapshots;
   - `tag` and `signal` filters, using existing watchlist tags and tickets;
   - negative zero normalised in the CSV only.
8. **Numbering.** The packet listed sub-packets under two letter schemes (A–K in the sequence, A–O in the body). They were implemented as:
   - scope (A);
   - batch service (B);
   - API (C);
   - filters (D);
   - presets (E);
   - sorting and UI (F / G);
   - Smart Money display and limitation text (H);
   - drill-down (I);
   - signal context (J);
   - export (K);
   - performance and cache (L);
   - isolation (M);
   - export bug left deferred (N);
   - tests (O).
9. `docs/OPUS55_AUDIT_REPORT.md` was not modified. PK-27 was not started.
