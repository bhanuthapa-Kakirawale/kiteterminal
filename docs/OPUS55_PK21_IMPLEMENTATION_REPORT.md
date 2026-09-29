# OPUS55 PK-21 Implementation Report: execution-realistic historical backtests

| | |
|---|---|
| Packet | **PK-21** only (audit **P1-14**, backtester findings **B1–B12**, plan row PK-21). PK-22 and later were not started. |
| Baseline before | **1,110 passed**, 0 failed, 0 xfailed, 0 xpassed. Re-measured on the untouched code (`50bd5b3`) before the first edit. |
| Backups | git tag `backup-pre-pk21` and branch `backup/pre-pk21` at `50bd5b3`; `backend/kite_terminal.db.pre_pk21_bak` (the WAL file was empty, so the copy is complete). |
| After | **1,157 passed**, 0 failed, 0 xfailed, 0 xpassed, three consecutive times (1,110 + 47 new). The runs were made outside market hours, after four pre-existing clock-dependent tests were pinned (see Full Suite ×3). |
| Live DB | `backend/kite_terminal.db` sha256 `1378f8b4…dacf`, unchanged before and after all work. It was never opened by app code; every run used scratch copies. |

# Executive Summary

The in-app backtester no longer grants fills that could not have happened. It has been rebuilt as a **portfolio-level, event-driven daily simulation** that models only two things:
- what was known at each bar's close;
- what price could actually be traded next.

The strategy functions are unchanged: it still calls the exact live `evaluate_entry` / `evaluate_exit` / `compute_entry_levels` / `rank_key`.

What changed:

| Before | After |
|---|---|
| Filled on the signal bar at `close × 1.001` | Filled at the **next session's open** plus slippage. The live ticket's DAY LIMIT is available as an alternative model. |
| Strategy exits at the same close that raised them | Filled at the **next open** |
| Gap-through stops filled at the stop price | Filled at the **open** |
| Trail raised from today's close, then tested against today's low | The new trail (and breakeven) is active **from the next session** |
| No costs | **Indian delivery charges** (STT, exchange, SEBI, GST, stamp duty, DP) plus configurable slippage, with gross, costs and net all reported |
| Unlimited simultaneous trades | **One pot of capital**, live risk sizing on simulated equity, `max_open_positions`, the strategy's `max_signals_per_day`, a cash check, and every skipped signal recorded with its reason |
| Force-close at `df.iloc[-1]`, i.e. after `end_date` | **No row after `end_date` is loaded** (SQL filter), and `simulate()` refuses frames that contain one |
| Drawdown from closed-trade R | Drawdown from the **daily mark-to-market equity curve** |
| Current watchlist used silently | **Labelled survivorship-biased** in the API and in a red UI banner |

**Headline result.** On the representative real-data fixture (51 watchlist symbols, 2024-09-01..2026-09-18; see Old vs New Golden Fixture), the old model's total R fell under the new one. Drawdowns rose once they were measured on MTM equity:

| Strategy | Old | New, net of costs, R | New, gross, R | Max DD old → new |
|---|---|---|---|---|
| `momentum_breakout` | +19.1R | **+15.5R** | +18.3R | 2.3% → 3.7% |
| `trend_pullback` | +18.3R | **+16.6R** | +19.2R | 2.0% → 3.1% |

The changes are attributed factor by factor below. No strategy parameter was touched.

Validation:
- 47 new tests;
- **9/9 focused mutations caught**, and every mutated file was restored to its sha256;
- the PK-21 suite ×10 and the timing/gap/look-ahead subset ×20, all green;
- a browser check of 15/15 under enforced CSP;
- `e2e_ui.py` identical to the baseline;
- the migration run three times on a copy of the real DB, with no existing row changed;
- the full suite green three consecutive times.

# Previous Backtester Problems

This is the PK-21A trace of the code at `50bd5b3`. It was frozen before any edit.

| # | Question | Old behaviour | Where |
|---|---|---|---|
| 1 | When is a signal known? | At bar N's close (`evaluate_entry(df.iloc[:i+1])`). This is correct. | `_simulate_symbol` L110-127 |
| 2 | What price is the entry? | `close × 1.001` **on bar N itself**, unconditionally (B1). | L130-141 |
| 3 | When are stop/target checked? | From bar N+1, against `low` / `high`. | `_step_position` L62-83 |
| 4 | Stop and target in one bar? | Stop first, which is conservative (B12). | L66-69 |
| 5 | Trailing timing? | The trail is raised from **today's close**, then tested against **today's low** (B4). | L76-83 |
| 6 | Gaps? | A gap-through stop fills **at the stop price** (B3). | L67, L83 |
| 7 | Costs? | None, and no slippage (B5). | whole file |
| 8 | Sizing? | None per trade. Stats used a fixed ₹ risk on the **live Kite portfolio value**, so each backtest made a broker call. | `run_backtest` L266-269 |
| 9 | Overlap? | One position per symbol, but unlimited across symbols. There was no `max_signals_per_day`, no cap and no cash (B6). | L257-262 |
| 10 | Dates? | The loop broke after `end_date`, but the force-close used `df.iloc[-1]`, the last row of **all** data (B7). Warm-up before `start_date` was correct. | L112, L143-145 |
| 11 | Universe? | The current watchlist, with no label (B8). | route |

Also found:
- **Close-based exits** filled at `day["close"]` on the same bar that raised them (B2).
- **Drawdown** was computed from closed-trade R sequenced by entry date (B10).
- **Breakeven** was not simulated (B11).
- **Sharpe/Sortino** were per-trade ratios with an implied trades-per-year annualisation.

The old golden run confirmed B7 in practice. On the real-data fixture, a `trend_pullback` position force-closed at 2026-09-22, four days after the requested `end_date` of 2026-09-18. Both synthetic runs had one such exit each.

# Execution Timing Model

`backend/backtest.py` loops over the sorted union of trading dates in [start, end]. On each date D it does the following, in this order:

1. **Sells at D's open:**
   - gap-through stops;
   - favourable OCO target gaps;
   - close-based exits decided at the previous close.

   Their proceeds are usable by buys at the same open.
2. **Pending entries** fill at D's open (market model), or at the open or intrabar (LIMIT model), subject to cash.
3. **Intrabar protective orders**, using the stop that was in force **before** D.
4. **At D's close:**
   - trailing activation and ratchet;
   - optional breakeven;
   - `evaluate_exit`.

   All three are effective from D+1 only.
5. **Mark to market:** equity = cash + Σ qty × the latest close.
6. **New signals** from D's close are ranked, capped, slot-checked and sized. Each is scheduled for the symbol's next bar, which must be ≤ `end_date`.

`signal_date` and `entry_date` are recorded separately on every trade. A test proves that the entry bar is always the signal bar + 1.

# Entry Fill Rules

- **`next_open` (default; user decision).** The fill is `fill_price("BUY", "MARKET", open)`: the next session's actual open × (1 + entry bps), rounded **up** to the tick. This is the PK-20 paper broker's pricing function, reused.
  - A gap-up fills at the real, higher open, never at the stale planned price.
  - A holiday or data gap in one symbol just means its next available bar.
- **`next_session_limit` (selectable).** This models the live ticket: a DAY LIMIT at the planned entry, `close × 1.001` snapped to the tick.
  - If open ≤ limit, the fill is min(slipped open, limit).
  - Else, if low ≤ limit, the fill is the limit.
  - Otherwise the signal is recorded as skipped, `limit_not_reached`.
- **No invented strategy rule.** There is no "invalidate after an extreme gap" rule; none exists in the strategies.
- **Final bar.** A signal on the final bar in range, or on a symbol's last bar ≤ `end_date`, has no next session. It is recorded as **unexecuted** (`no_next_session_before_end_date`) and never becomes a trade.
- **Quantity** is fixed at signal time, as on a live ticket. An entry whose fill plus buy charges exceeds cash is skipped as `insufficient_cash`; cash never goes negative, which is asserted in code.

# Stop / Target Rules

- **Stop and target** are the fixed absolute levels from `compute_entry_levels`, snapped to the instrument tick exactly as `_create_entry_ticket` does. The live GTT uses them unchanged, and so does the backtest. They are **not** re-anchored to the actual fill.
- **Long stop:**
  - open ≤ stop fills at the **open** less slippage (a gap);
  - open > stop and low ≤ stop fills at the **stop** less slippage (intrabar).
- **OCO target** (non-trailing, e.g. `rsi_mean_reversion`):
  - open ≥ target fills at the **open** less slippage. The favourable gap is taken at the open, not capped at the target;
  - high ≥ target fills at the **target** less slippage (intrabar). This is conservative against the live LIMIT target leg, which can't fill below its limit.
- **Trailing positions** have no hard target: touching `target_1` only activates trailing, as in live `_try_activate`.
- **Entry bar:** a position bought at today's open is protected for the rest of today's bar. If the fill itself is at or below the stop (the open gapped below the stop), it exits immediately at the open.

# Same-Bar Ambiguity Policy

Daily OHLC can't show which of the high and low came first. If the stop and the target (or activation level) both lie inside one bar, the **stop is assumed first**, on both the OCO and the trailing paths. The policy is fixed, not configurable, and is recorded in every run's `execution_config.same_bar_policy = "stop_first"`. Tests 7 (both paths) prove it, and mutation M8 (target first) is caught.

# Gap Handling

- **Gap stops** fill at the open (test 5: stop 100, open 94, 5 bps gives **93.95**, not 100). The trade carries `gap_fill = true`.
- **Gap targets** (OCO) fill at the open (test 8).
- **Precedence:** a gap stop takes precedence over a close-based exit scheduled for the same open. Both fill at that open; only the reason differs.
- **Slippage** is applied **once** to whichever reference price fills (the open on a gap, the trigger intrabar), never stacked.

# Trailing / Breakeven Timing

- **Trailing.** At D's close, if the trail is active (high ≥ `target_1` on D or earlier), the candidate is close − `trailing_distance`, rounded **down** to the tick like the live `_try_ratchet`. It becomes the stop **from D+1**.
  - D's own low is never tested against a stop that D created.
  - Tests 11 and 12 cover this: the trail created on D2 is 106 and D2's low is 105, yet the exit is on D3. Mutation M4 is caught.
- **Breakeven** is `simulate_breakeven`, **off by default** (user decision), because live breakeven is a proposal a human must approve.
  - When enabled, it applies to trailing-enabled positions only (as live).
  - When D's high first reaches entry + `breakeven_trigger_r` × initial risk, the stop becomes the entry fill rounded **up** to the tick, from D+1.
  - Test 13 covers this: D2 reaches +1R with a low below breakeven, and the exit is on D3.
- The trailing **formula** is unchanged (close-based). Live trails from the intraday LTP; that mismatch is documented, not changed.

# Close-Based Exit Timing

`evaluate_exit` is called at D's close on data through D. If it fires (thesis invalidation, time exit, RSI recovery), the sell is scheduled for **the next session's open** (test 10: exit at D3's open 97, not D2's close 105; mutation M2 is caught).
- Protective stop/target orders are kept separate: they existed before the bar and act intrabar.
- `executed_at` passed to `evaluate_exit` is now the **fill date**, as a live position's is. The time-exit count therefore starts one bar later than the old code, where it was the signal date.

# Slippage Model

- `entry_slippage_bps` / `exit_slippage_bps` default to **5 / 5**, the same default as the PK-20 paper broker.
- They are applied through `paper_broker.fill_price`, reusing the pricing concept rather than the broker:
  - BUY = reference × (1 + bps), rounded **up** to the tick;
  - SELL = reference × (1 − bps), rounded **down** to the tick.
- The LIMIT entry model caps the fill at the limit.
- Exact arithmetic is tested (test 4: 100 → 100.10 buy and 99.90 sell at 10 bps; tests 5, 6 and 9).

# Indian Equity Cost Model

`backend/backtest_costs.py` provides `compute_charges(side, turnover, schedule, enabled)`, which returns every component. The defaults live in one place, `config.BACKTEST_COST_DEFAULTS`, taken from Zerodha's CNC delivery schedule. The comment says to verify them against the broker's current charges page. The schedule used is snapshotted into each run's `execution_config.cost_schedule`.

| Component | Default | Side |
|---|---|---|
| Brokerage | 0% (optional per-order cap supported) | both |
| STT | 0.1% | buy **and** sell (delivery) |
| NSE exchange transaction charge | 0.00297% | both |
| SEBI turnover fee | ₹10 per crore | both |
| GST | 18% of (brokerage + exchange + SEBI) | both |
| Stamp duty | 0.015% | buy |
| DP charge | ₹15.34 (₹13 + GST) per scrip per sell day | sell (once per exit) |

**Arithmetic (tests 14–16):**

| Case | STT | Exchange | SEBI | GST | Stamp | DP | Total |
|---|---|---|---|---|---|---|---|
| ₹1,00,000 BUY | 100.00 | 2.97 | 0.10 | 0.55 | 15.00 | — | **118.62** |
| ₹1,00,000 SELL | 100.00 | 2.97 | 0.10 | 0.55 | — | 15.34 | **118.96** |

A round trip is therefore about 0.24% plus ₹15.34, before slippage. That is consistent with the audit's estimate of 0.25–0.30%.

**Costs are never hidden in fill prices.** Every trade reports:
- `gross_pnl` = (exit − entry) × qty, on the slipped fills;
- `entry_costs`, `exit_costs`, `costs`, and a per-component `cost_breakdown`;
- `net_pnl`.

The run's metrics report gross, costs (with breakdown) and net. `costs_enabled=false` zeroes the charges and adds a warning to the run.

**Documented simplifications:**
- components are rounded to the paisa, not to whole rupees as contract notes do;
- a same-day round trip is still costed as delivery, which is the conservative direction.

# Capital Model

`BacktestConfig` has these fields, with defaults in `config.BACKTEST_DEFAULTS`, each overridable per run from the form or API:
- `initial_capital` ₹10,00,000;
- `risk_per_trade_pct` 0.5;
- `capital_per_trade_pct` 10;
- `max_open_positions` 5;
- slippage 5/5 bps;
- `costs_enabled` true;
- `entry_model` `next_open`;
- `simulate_breakeven` false.

**These are deliberately not the live account's `risk_settings` or its PK-12 limits.** A historical test is an explicit experiment, and the backtest no longer calls Kite at all (tested: zero FakeKite calls).

- **Sizing** uses the live `risk_engine.compute_position_size(entry, stop, equity, …)` on the simulated equity at the signal close (MTM): min(risk-based, capital-based) quantity. A zero quantity is skipped as `size_zero`.
- **Cash** decreases by fill × qty + buy charges and increases by proceeds − sell charges.
- **Cash reconciles exactly:** final cash = initial + Σ net P&L (test 17).

# Position Constraints

- **One position per symbol**, as live and as before. A symbol that was in a position at any time on D does not signal on D.
- **`max_open_positions`:** open + pending entries must stay below the cap, otherwise the signal is skipped as `max_open_positions` (test 19).
- **`max_signals_per_day`:** the strategy's own parameter, applied exactly as in the live `run_entry_scan`, i.e. before level computation. Signals over the cap are skipped as `max_signals_per_day`.
- **Partial fills (PK-13):** historical orders fill in full. Daily OHLC carries no information about partial execution, so none are invented. PK-13 remains a live/paper concern.

# Multiple-Signal Allocation

Same-day candidates are sorted by **(−`module.rank_key`, symbol)**:
- the strategy's existing ranking comes first;
- the symbol breaks ties deterministically.

This never depends on dict or DB order. Test 20 proves the same allocation for reversed input order, the tie broken alphabetically, and the daily cap. Pending entries execute at the next open in that same order. No new ranking logic was introduced.

# Equity Curve

Each simulated date produces one point:
- `date`, `cash`, `market_value` (open quantity × the latest close ≤ D), `equity` = cash + market value;
- `open_positions`, `open_risk` (Σ qty × max(close − stop, 0)), `exposure_pct`.

The last point is replaced by the liquidated state after the end-of-range closes, flagged `liquidated: true`. The curve is returned by the API and persisted in `backtest_runs.equity_curve`. Test 21 checks that an open position's MTM value is in equity.

# Drawdown Calculation

Max drawdown is peak-to-trough on the **daily MTM equity curve**, in ₹ and %. Test 22 is a case where the only closed trade is a winner, so closed-trade drawdown is 0, while an unrealised dip makes the MTM drawdown ₹2,000.

The Monte Carlo reshuffle now uses **net ₹ P&L** per closed trade. It is explicitly labelled closed-trade only: it understates MTM drawdown.

# Date / End-Date Isolation

- **Loader.** `indicators.load_ohlc_frame(token, end_date=None)` filters `date <= end_date` **in SQL**. No row after D exists in memory before any indicator or signal is computed. The default call is unchanged for live code.
- **Guard.** `backtest.simulate()` raises `FutureDataError` if handed a frame with a row after `end_date`.
- **Windows.** Consistency-check windows cut each window's frames at the **window's** end (`SymbolFrame.sliced`) **before** simulating. Each window is an independent simulation from the initial capital.
- **End of range.** A position open at `end_date` is closed at its last bar ≤ `end_date` at slipped close with sell charges (`backtest_end`, `forced_close = true`). A pending entry whose next bar is after `end_date` is unexecuted.
- **Tests:**
  - test 23: the loader keeps every warm-up row and none after the end; `simulate` refuses future rows;
  - test 25: nothing is entered, exited or marked after `end_date`;
  - a spy test: no window sees a later window's rows.
- **Mutations:** M7 (the loader filter removed) and M7b (the loader filter and the guard removed) are both caught.

# Warm-Up Data

All rows before `start_date` are loaded and passed to the strategy functions, which is correct for SMA-200. Signals, trades and equity points exist only from `start_date` on (test 24). Every run records `execution_config.data_window` = {`data_load_start`, `simulation_start`, `simulation_end`, `last_bar_used`}.

# Universe / Survivorship Bias

The route sets `universe_source` on every run:
- `current_watchlist` (no symbols given): `survivorship_bias = "likely"`, with the warning "Current watchlist used — survivorship bias possible…". The UI shows it as a red banner.
- `user_supplied` (explicit symbols): `survivorship_bias = "unverified"`, with "not verified as a point-in-time universe".

No constituent history was invented, and no unbiased-universe mode is claimed. Symbols without data up to `end_date` are listed in a warning.

# Corporate Action Assumptions

`ohlc.py` stores Kite daily candles as fetched, with **no adjustment metadata**. Kite back-adjusts history after splits/bonuses, but only the last 7 days are re-downloaded (audit P2-5). Stored older candles can therefore stay unadjusted. Dividends are not adjusted.

PK-21 adds **detection only**, with no behaviour change:
- any bar whose open is more than `config.SUSPECT_CORPORATE_ACTION_GAP_PCT` (25%, as the audit suggests) away from the previous close is listed in the run's warnings;
- every trade spanning it is flagged `data_warning = "suspected_corporate_action_gap"`.

A split gap is therefore never silently read as an ordinary stop-out (tested with a −50% gap). On the real data copy, no bar crosses 25%. The re-backfill fix belongs to PK-22.

# Metrics

`backtest_analytics.compute_portfolio_metrics` computes everything on **net** results:
- **Capital and P&L:** initial capital, ending equity, gross P&L, costs (plus breakdown), net P&L, gross/net return %;
- **Net CAGR:** only when the span is ≥ 365 days, otherwise null;
- **Max drawdown** (₹ and %) from the MTM curve;
- **Trade statistics:**
  - trade count, win rate (net P&L > 0) and profit factor;
  - average and median trade, largest win and largest loss;
  - expectancy per trade in ₹ and net R, and average win/loss in net R;
  - max consecutive losses (by exit date), average holding days and average bars held;
- **Signal counts:** skipped signals (total and by reason), unexecuted signals;
- **Exposure:** exposure % (the mean of market value / equity) and time in market %;
- **Recovery factor.**

**Sharpe/Sortino** now use the **daily equity returns** the curve provides: one observation per simulated trading day, annualised by √252, risk-free 0%. Sortino uses downside deviation over all days. These replace the per-trade ratios with implied annualisation, now that correct sampling exists.

**R-multiples:**
- `r_multiple` = (exit − entry fill) / (planned entry − initial stop) (gross);
- `net_r_multiple` = net P&L / (qty × that same initial risk).

The planned risk is always positive, so R is always defined.

**Trade record (PK-21P).** Each trade stores `symbol`, `strategy_name`, `signal_date`, `entry_date`, `entry_price`, `planned_entry_price`, `quantity`, `initial_stop`, `initial_target`, `exit_date`, `exit_price`, `exit_reason`, `gross_pnl`, `entry_costs`, `exit_costs`, `costs`, `net_pnl`, `holding_days`, `bars_held`, `r_multiple`, `net_r_multiple`, `gap_fill`, `forced_close`, `outcome` and `data_warning`.

The legacy top-level keys (`total_signals` = trades, `win_rate`, `avg_r_multiple` = net expectancy R, `profit_factor`, `max_drawdown_pct`, …) are kept, so the existing UI and walk-forward table read unchanged.

# Old vs New Golden Fixture

Two fixtures were run through the **old** code (a `git archive` of `backup-pre-pk21`, with the portfolio value pinned at ₹10,00,000) **before any edit**, then through the new code. The scripts were `run_golden_old.py` / `run_golden_new.py` in the session scratchpad.

- **Synthetic:** 8 seeded random-walk symbols with volume spikes, ±5–6% overnight gaps, one −50% split-like gap and data past the end. Simulated 2025-09-01..2026-08-31.
- **Real:** a scratch copy of `kite_terminal.db.pre_pk21_bak`, covering 51 watchlist symbols, simulated 2024-09-01..2026-09-18 (data runs to 09-22). Strategy parameters = the module defaults.

**Real data** (new = ₹10L, 5 bps, costs on, max 5, next-open):

| | Old | New: timing only¹ | + capital constraints | + slippage | + costs = **default** | LIMIT entry | breakeven on |
|---|---|---|---|---|---|---|---|
| **momentum_breakout** trades | 101 | 99 | 82 | 82 | **82** | 76 | 82 |
| total R (gross → net) | +19.12 | +16.09 | +19.47 | +18.25 | **+15.46 net** (+18.25 gross) | +11.20 | +15.27 |
| expectancy R | +0.189 | +0.163 | +0.237 | +0.223 | **+0.189** | +0.147 | +0.186 |
| profit factor | 1.68 | 1.52 | 1.87 | 1.80 | **1.64** | 1.53 | 1.63 |
| max DD | 2.31%² | 4.24% | 3.07% | 3.25% | **3.69%** | 2.55% | 3.69% |
| net P&L | — | — | ₹98,485 | ₹91,975 | **₹77,306** (costs ₹13,907) | ₹54,546 | ₹76,395 |
| **trend_pullback** trades | 109 | 107 | 89 | 89 | **89** | 83 | 90 |
| total R | +18.26 | +18.29 | +20.35 | +19.22 | **+16.59 net** (+19.22 gross) | +15.95 | +16.19 |
| expectancy R | +0.168 | +0.171 | +0.229 | +0.216 | **+0.186** | +0.192 | +0.180 |
| profit factor | 1.84 | 1.77 | 2.16 | 2.07 | **1.85** | 1.99 | 1.81 |
| max DD | 2.00%² | 2.72% | 2.51% | 2.67% | **3.05%** | 1.80% | 3.05% |
| net P&L | — | — | ₹1,01,000 | ₹95,476 | **₹81,073** (costs ₹13,089) | ₹78,661 | ₹78,547 |

¹ "Timing only" means next-open entries, gap stops, next-session trailing, next-open exits and the end-date fix, with no costs, no slippage and unconstrained capital (₹10¹²; cap 1000; `max_signals_per_day` still applied).
² The old DD was closed-trade R-based, sequenced by entry date; the new DD is daily MTM.

**Why the results changed:**
- **Timing (next-open entries, gap stops, next-session trail, next-open exits, end-date fix).**
  - For `momentum_breakout`: −3.0R. The breakouts it buys tend to open higher the next day, so they are bought worse than `close × 1.001`.
  - For `trend_pullback`: ≈0. Its entries are not gap-prone.
  - `max_signals_per_day` (live's cap, previously ignored) drops 2–3 trades.
  - The one leaked post-`end_date` exit is gone (`exits_after_end`: old 1, new 0).
- **Capital constraints** (₹10L, max 5 positions, sized on equity) remove 17–18 more trades (20 skipped as `max_open_positions` each).
  - In this sample the skipped trades happened to be net losers, so total R *rose* (+3.4R / +2.1R).
  - This is a **selection effect of which trades fit, not an edge**, and a different cap or order could move it either way. It is reported, not relied on.
- **Slippage** at 5 bps each way costs −1.2R / −1.1R.
- **Costs** cost −2.8R / −2.6R: about ₹170 per round trip, roughly 0.03R per trade.
- **The LIMIT model** (live semantics) cuts `momentum_breakout` further: 18 breakouts never trade back to their limit. That is the audit's B1 concern, and it is quantified here.
- **Drawdown** rises 1–1.4 points because it now includes open-position losses.

**Synthetic fixture.** It exercises the mechanics; its P&L is noise.
- `momentum_breakout`: old 9 trades +1.40R, new 9 trades +5.87R net.
- `trend_pullback`: old 8 trades −1.84R, new −0.38R net.

The synthetic `momentum_breakout` improved mostly because the old same-bar trail stopped trades out early (one `trailing_stop` exit now runs). On random-walk data this is not evidence of anything. In both synthetic runs the old code force-closed one position after `end_date`; the new code does not.

**Manual spot-checks** of real trades against the raw OHLC all reconcile:
- **DMART thesis exit:** entry = the 09-06 open 5,293 × 1.0005, rounded up to the 0.1 tick = 5,295.7. The exit is at the open after the thesis close.
- **ADANIPOWER trailing stop:** the 09-22 close 170.25 − 4.9 trail = 165.35, hit intrabar on 09-23, filled at 165.27.
- **EICHERMOT intrabar stop.**
- **BAJAJ-AUTO time exit.**
- **ETERNAL:** force-closed at 09-18, not at the 09-22 data.

No strategy parameter was tuned to recover the old returns.

# Strategy Semantics Preserved

- **No strategy module was modified.** `git diff` is empty for `backend/strategies/*`. The thresholds are as in the audit:
  - `momentum_breakout`: SMA-200, 30-close Donchian excluding today, volume > 3× SMA-20, 3 ATR stop, 3R target, 1.5 ATR trail;
  - `trend_pullback`: 3.5 ATR stop, 4R target, volume filter;
  - `rsi_mean_reversion`: disabled by default.
- **Test 27** reproduces the exact signal dates the **pre-PK-21** code produced on the golden fixture, for all three strategies. It also checks that every backtest trade comes from one of those real signals, entered on the next bar.
- **Implementation/spec differences noticed, documented rather than changed:**
  - the Donchian is close-based;
  - the volume average includes the signal bar;
  - the `trend_pullback` band has no lower bound (P3-1);
  - `rsi_mean_reversion` uses max() for "tighter" stops.

  All were already recorded in the audit.

# Performance

| Run (real fixture, 51 symbols, ~2 years) | Old | New (default) |
|---|---|---|
| momentum_breakout | 34.1 s | 34.5 s |
| trend_pullback | 38.1 s | 38.2 s |

There is **no material regression.** The cost is dominated, as before, by calling the live strategy functions on each day's prefix frame, O(symbols × days × history). Per-bar lookups use precomputed lists and date→index maps; there is no per-day DataFrame scan. Consistency-check windows re-simulate their own ranges (about one extra run in total). A vectorised simulator was not attempted, because it would sacrifice the single-code-path guarantee and lifecycle clarity.

# Tests Added

`tests/test_backtest_realism.py`: **47 tests**, including parametrisations, numbered by the packet's list.

- **Entry 1–4:**
  - Monday signal → Tuesday open;
  - a final-bar signal is unexecuted, never a trade;
  - a gap-up fills at the actual open;
  - slippage direction;
  - the LIMIT model (fills at the open, fills at the limit, unfilled);
  - defaults are next-open with breakeven off.
- **Stop 5–7:** the gap stop (100 / 94 → 93.95); the intrabar stop at slipped stop; stop-first in one bar (OCO and trailing).
- **Target 8–9:** favourable gap at the open; intrabar target at slipped target.
- **Exit signal 10:** fills at the next open; a gap stop takes precedence over a scheduled exit.
- **Trailing 11–12:** the trail is active only next session; today's low can't hit today's new trail.
- **Breakeven 13:** next-session activation; off by default.
- **Costs 14–17:** exact buy and sell arithmetic; the DP charge; the brokerage cap and GST base; disabled costs; gross − costs = net; cash reconciliation.
- **Capital 18–20:**
  - insufficient cash skips, and cash stays ≥ 0;
  - the `max_open_positions` cap;
  - rank-then-symbol allocation that is invariant to input order;
  - `max_signals_per_day`;
  - live sizing formula on simulated equity.
- **Equity 21–22:** MTM of open positions; MTM drawdown where closed-trade drawdown is 0.
- **Dates 23–25:**
  - the loader cuts at `end_date` and keeps warm-up;
  - `FutureDataError`;
  - warm-up is used but never traded;
  - nothing executes after `end_date` (real strategy);
  - force-close at the last bar ≤ end;
  - walk-forward windows never see a later window.
- **Universe 26:** watchlist runs are "likely" biased; explicit lists are "unverified". Corporate-action gaps are flagged.
- **Regression 27–28:** golden signal dates from the old code for all three strategies; trades come only from real signals; identical inputs give identical results.
- **Persistence / API:**
  - net metric completeness;
  - the API round trip of every audit field;
  - pre-PK-21 run rows still load (NULL new fields);
  - route validation (bad entry model, negative capital, start > end, bad date format);
  - **zero broker calls**;
  - migration idempotency.

**Existing tests.** `tests/test_baseline_api.py::test_backtest_endpoint` passes unchanged. No existing assertion was changed or weakened.

**Four pre-existing tests** now take the existing `market_open` fixture. This was a user-approved, test-only change: the tests were clock-dependent since PK-20 (see Full Suite ×3). Their assertions are untouched.

| Test | Change |
|---|---|
| `test_baseline_api::test_confirm_dry_run_places_nothing` | `(env)` → `(env, market_open)` |
| `test_csrf_protection::test_d_same_origin_with_header_behaves_normally` | `(env)` → `(env, market_open)` (module-local fixture, same behaviour) |
| `test_csrf_protection::test_g_no_origin_with_header_is_accepted` | `(env)` → `(env, market_open)` (module-local fixture) |
| `test_review_changes::test_confirm_broadcasts_tickets_changed` | `(env)` → `(env, market_open)` (module-local fixture) |

# Mutation Results

Each mutation was applied once and the PK-21 suite run. The file was then restored byte-for-byte and its sha256 re-checked; all three target files matched afterwards (`backtest.py` `e6c42386…`, `backtest_costs.py` `ef94ccca…`, `indicators.py` `51fb3883…`).

| # | Mutation | File | Result | Catching tests |
|---|---|---|---|---|
| M1 | same-bar entry restored (fill at the signal close, dated the signal bar) | `backtest.py` | **caught**, 4 | test_1, test_3, test_18, test_27 (trades) |
| M2 | same-close exit restored (close-based exit priced at the close that raised it) | `backtest.py` | **caught**, 2 | test_10, test_27 (trades) |
| M3 | gap-through stop filled at the stale stop | `backtest.py` | **caught**, 2 | test_5, gap-stop precedence |
| M4 | newly raised trail / breakeven applied the same day | `backtest.py` | **caught**, 4 | test_7[trailing], test_12, test_13, gap-stop precedence |
| M5 | transaction costs disabled | `backtest_costs.py` | **caught**, 5 | test_14, test_15, test_16, test_17, metrics |
| M6 | capital allowed negative (cash check and guard removed) | `backtest.py` | **caught**, 1 | test_18 |
| M7 | `end_date` leakage: loader filter removed | `indicators.py` | **caught**, 6 | test_23 loader, test_25, walk-forward, test_26, API round trip, no-broker-call |
| M7b | `end_date` leakage: loader filter **and** `simulate()` guard removed | both | **caught**, 4 | test_23 (both), test_25, walk-forward |
| M8 | stop/target ambiguity resolved favourably (target first) | `backtest.py` | **caught**, 1 | test_7[OCO] |

**9/9 caught.**

# Targeted Repeats

| Run | Result |
|---|---|
| PK-21 suite (47 tests) ×10 | **10/10 green** (14–15 s each) |
| timing / gap / look-ahead subset (23 tests: entries 1–3, stops 5–7, targets 8–9, exit 10, trailing 11–12, breakeven 13, dates 23–25, walk-forward, gap precedence, LIMIT model, end-of-range close) ×20 | **20/20 green** |

No flake was seen. Everything is deterministic: fixed bars, a seeded fixture, no clock and no sleeps.

# UI / API Validation

**API.** `POST /api/strategies/backtests` gains these optional fields, validated:
- `initial_capital`, `risk_per_trade_pct`, `capital_per_trade_pct`, `max_open_positions`;
- `entry_slippage_bps`, `exit_slippage_bps`, `costs_enabled`;
- `entry_model` (Literal) and `simulate_breakeven`.

Dates must be `YYYY-MM-DD`, and start ≤ end (400). The response adds `execution_config`, `universe`, `metrics`, `equity_curve`, `skipped_signals`, `unexecuted_signals` and `warnings`. The route no longer reads the live portfolio.

**UI (minimal, Strategies → Backtest).**
- **New inputs:** capital, max positions, entry/exit slippage, entry fill model, costs, assume-breakeven.
- **Result header:** the **execution summary** line, e.g. "Execution: Next-session open (market order) · slippage 5 / 5 bps · costs ON … · capital ₹10,00,000 · max 5 positions · … stop first …".
- **Survivorship:** a **red banner** when the watchlist is the universe.
- **Metric rows:** initial, ending, gross, costs, net, CAGR, skipped/unexecuted, expectancy, average/median trade, largest win/loss, MTM drawdown, exposure, holding period.
- **Trade table:** signal date, quantity, gross, costs, net, and gap/data-warning badges.
- All new text is set via `textContent` or `esc()`.

**Browser run.** This used `tests/demo_server.py` on scratch databases, Playwright with Edge headless, **CSP enforced** and a `securitypolicyviolation` listener installed before any page script. **Result: 15/15** checks passed:
- the defaults;
- the default form passes browser validation;
- the survivorship banner;
- the execution summary;
- gross/costs/net;
- the MTM drawdown label;
- 13-column trade rows with signal date < entry date;
- the user-supplied label;
- the LIMIT/costs-off/₹2,50,000 summary;
- the costs-disabled warning;
- **zero CSP violations, zero console/page errors, zero 4xx/5xx.**

The first attempt found a real bug, fixed before the final run: `step="1000"` with `min="1"` made the default ₹10,00,000 an invalid value, so the form would not submit. The inputs now use `step="any"`.

**Regression.** `tests/e2e_ui.py` against a fresh default demo gave **53/54**, identical to the PK-20 baseline, with the same pre-existing failure ("risk-blocked signal listed separately").

# Database Migration

`db._ensure_backtest_pk21_columns` uses additive, nullable `ALTER TABLE … ADD COLUMN` only:
- `backtest_runs` gains 7 columns: `execution_config`, `metrics`, `equity_curve`, `skipped_signals`, `warnings`, `universe_source`, `survivorship_bias`;
- `backtest_trades` gains 17.

Validation on scratch copies of `kite_terminal.db.pre_pk21_bak` (`migrate_one.py`, `migration_compare.py`):
- **Three runs with the new code:** the schema after runs 1, 2 and 3 is identical.
- **Rows preserved.** A second copy was migrated with the **pre-PK-21 code**. All 31 tables were digested over their common columns. The existing 15 `backtest_runs` and 41 `backtest_trades` rows are identical, and the only difference is `account_safety_state.updated_at`, a seed timestamp 1 s apart (the same artefact PK-20 recorded). **No existing row changed.**
- Old runs read back with NULL new fields. The API and UI tolerate that (tested).
- **The real DB is still unstarted since before PK-12**, and PK-21's migration joins the queue it will apply on its first start. It was not opened.

# Full Suite ×3

Three consecutive runs of the whole suite, after the last change of any kind:

| Run | Started (IST) | Result |
|---|---|---|
| 1 | market hours | **1,157 passed**, 0 failed (207 s) |
| 2 | market hours | **1,157 passed**, 0 failed (350 s) |
| 3 | ~15:00, ran 30 min past the 15:30 close | 1,156 passed, **1 failed**: `test_review_changes.py::test_confirm_broadcasts_tickets_changed` |

1,157 = the 1,110 baseline + the 47 new PK-21 tests; 0 xfailed, 0 xpassed.

**The run-3 failure is pre-existing, clock-dependent, and unrelated to PK-21.**
- **Finding.** A fourth run made after the close (15:45 IST) fails **four** tests:
  - `test_baseline_api::test_confirm_dry_run_places_nothing`;
  - `test_csrf_protection::test_d_same_origin_with_header_behaves_normally`;
  - `test_csrf_protection::test_g_no_origin_with_header_is_accepted`;
  - `test_review_changes::test_confirm_broadcasts_tickets_changed`.

  **The untouched pre-PK-21 code** (`git archive backup-pre-pk21`) fails **exactly the same four** at the same time.
- **Cause.** Each test confirms a dry-run entry without the `market_open` fixture. Since PK-20, dry-run entries obey PK-12H: outside market hours, an entry needs explicit AMO consent, so the confirm returns 409. A probe confirmed it: with the clock pinned open, the same confirm returns 200 and broadcasts `executed`.
- **Scope.** PK-20's three green runs, and runs 1–2 here, happened during market hours. None of these tests touch the backtester.

**Resolution (user decision).** The four tests take the existing `market_open` fixture (see Tests Added); nothing else changed. The full suite was then re-run three consecutive times, **after market hours**, which proves the fix. **This is the gate result:**

| Final run | Started (IST) | Result |
|---|---|---|
| 1 | after the close | **1,157 passed**, 0 failed (347 s) |
| 2 | after the close | **1,157 passed**, 0 failed (349 s) |
| 3 | after the close | **1,157 passed**, 0 failed (353 s) |

# Known Limitations

1. **Intrabar order is unknowable from daily bars:** stop-first, and no partial fills.
2. **Slippage** is a flat bps figure. There is no spread, liquidity or volume model, and quantity never scales with volume.
3. **Gap stops are assumed to fill at the open.** Live, the GTT's LIMIT at stop × 0.995 may **not fill at all** on a gap (P0-6). That is worse than modelled, and the backtest cannot represent an unfilled stop.
4. **Human approval is instant:** close-based exits and entries assume the human approves before the next open.
5. **Trailing uses the daily close; live uses the intraday LTP** every 45 s. The mismatch is documented and not changed.
6. **Tick sizes** are today's `instrument_cache` values, not historical ones.
7. **Survivorship bias:** both universe modes are potentially biased. No point-in-time constituent data exists.
8. **Corporate actions** are flagged, not adjusted. Dividends are not adjusted.
9. **Costs** follow one broker's schedule, paisa-rounded. A same-day round trip is costed as delivery.
10. **One position per symbol, no same-day re-entry:** a symbol exited on D cannot signal on D (as before PK-21; live would allow it).
11. **Sharpe/Sortino** assume cash earns 0%.
12. **The audit's "commit the study scripts"** could not be done: the September 2026 parameter-study scripts are not in the repository, so there is nothing to commit. The golden scripts used here live in the session scratchpad. The in-app backtester is now the reproducible tool.

# Deferred PK-22 Issues

- The pre-existing PK-20 finding: on a **fresh** database, the second `init_db()` re-creates `idx_dashboard_panels_account_type`. It was not touched here.
- Corporate-action re-backfill (P2-5): detect the gap and re-download history. PK-21 only flags it.
- The holiday calendar: sessions use weekends only.
- `schema_version`.
- Optional later work: point-in-time universes, a volume-aware liquidity/partial-fill model, historical tick sizes, and modelling an unfilled GTT LIMIT on gaps.

# Deviations From Audit

1. **Default entry model** (the audit's B1 describes live's next-session LIMIT).
   - The default is the **next-session open (market)**, the packet's preferred default and the user's decision.
   - The live-mirroring LIMIT model is implemented, selectable per run, and its effect is quantified above.
2. **Target fills intrabar at target − slippage**, which is conservative against the live LIMIT target leg. Favourable gaps fill at the open, per the packet.
3. **Gap stops fill at the open** (packet rule), not "at the open or not at all" as the live GTT LIMIT would.
4. **Breakeven** (B11) is implemented but **off by default** (user decision), because live breakeven is a manual proposal.
5. **The backtest no longer reads the live portfolio.** It uses explicit `BACKTEST_DEFAULTS` rather than the account's `risk_settings`, per the packet's "do not blindly borrow live defaults".
6. **Sharpe/Sortino** were changed from per-trade to daily MTM returns, now that correct sampling exists.
7. **The study scripts were not committed:** they are absent from the repository (limitation 12).
8. **Outside the backtester, four pre-existing clock-dependent tests** (a latent PK-20 defect) were given the existing `market_open` fixture. This was test-only and approved by the user, so the suite no longer depends on the time of day (see Full Suite ×3). No production code outside the backtest path changed.
9. `docs/OPUS55_AUDIT_REPORT.md` was not modified.
