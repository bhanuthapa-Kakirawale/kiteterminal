# Kite Terminal — Test & Verification Document

This document describes (1) the existing automated test suite, how to run
it, and what it covers/doesn't cover, and (2) a manual verification
checklist for confirming strategies and the app behave correctly before
trusting them with real money.

## 1. Automated Test Suite

### 1.1 Setup

```
pip install -r requirements-dev.txt
playwright install    # only needed once, for the E2E browser test
```

### 1.2 Running the tests

```
pytest                          # runs everything under tests/
pytest tests/test_baseline_api.py       # core API/flow coverage
pytest tests/test_review_changes.py     # regression suite for specific fixes
```

Playwright end-to-end UI test (requires the demo server running separately):
```
.venv/Scripts/python.exe tests/demo_server.py --port 8765
# in a second terminal:
.venv/Scripts/python.exe tests/e2e_ui.py --base http://127.0.0.1:8765 --out <output-dir>
```
This drives the real UI in headless Edge, takes numbered screenshots at each
step, and writes `e2e_results.json` with pass/fail results.

### 1.3 Test infrastructure

- **Real account data is off limits.** `tests/real_data_guard.py`, installed
  by `conftest.py` before any test runs, refuses (with an audit hook) every
  `open`, `sqlite3.connect` and `shutil.copyfile` of `backend/kite_terminal.db*`:
  the real database, its WAL/SHM files and every backup. The hook fires before
  the file is opened, and `tests/test_real_data_guard.py` proves every channel
  against a decoy file. An audit hook cannot see a child process, so a static
  check also keeps every test source from naming the real backup. Migration
  compatibility with pre-PK-22 databases is tested on a synthetic database
  built from `tests/fixtures/pre_pk22_schema.sql`: the exact DDL that the
  pre-PK-22 code (commit `6446c79`) produced, captured from an empty temporary
  database. It never uses the developer's local backup.
- **`tests/conftest.py`** — the `env` fixture spins up a fresh throwaway
  SQLite database per test, a fresh encryption key, 5 seeded instruments, an
  active session, and monkeypatches `kite_client.get_kite` to return a
  `FakeKite`. The real ticker is disabled and WebSocket broadcasts are
  captured into a list for assertions. Startup background loops (trailing,
  reconciliation, etc.) are **not** run during tests — each test controls
  its own scenario explicitly.
- **Clocks and the NSE calendar (PK-22).** Tests never depend on today's
  date: pin `market.is_market_open` (`market_open` fixture),
  `market.last_complete_session_date`, `paper_broker._now` or
  `account_safety.now_ist`, and use explicit IST datetimes. The real NSE
  holiday file is loaded by default (2026-09-14 is a holiday); swap in a
  synthetic calendar with `trading_calendar.use_calendar(holidays=[...])`.
  `conftest.last_weekday` returns the last *trading* day.
- **Old databases (PK-22).** Migrations run once per database. A test that
  edits the schema to imitate an old database calls
  `conftest.as_pre_pk22_database(db)` before `init_db()`, so the legacy
  migrations run again. `tests/test_platform_pk22.py` also migrates a
  synthetic early-era database and, when present, a copy of
  `backend/kite_terminal.db.pre_pk22_bak` (skipped otherwise).
- **`tests/fake_kite.py`** — a full in-memory stand-in for
  `kiteconnect.KiteConnect`: holdings/positions/margins/LTP,
  order placement/history/cancel (with deterministic order IDs and an
  auto-fill toggle), GTT placement/modify/delete, historical data,
  instruments. Records every call for assertion and supports `fail_next` to
  simulate a broker error on demand.
- **`tests/demo_server.py`** — a standalone runnable instance of the real
  app against a throwaway DB and a synthetic 10-symbol universe with
  realistic seeded state (pending signals, a risk-blocked signal, two open
  live positions with GTTs, a pending exit signal, a pending breakeven
  adjustment, historical closed trades, and autopilot run history). Useful
  both for manual exploration and as the E2E test target.
  Its entry signals are **not** hand-written rows: each symbol's candle tail
  is shaped to trigger a particular strategy and the real
  `strategy_engine.run_entry_scan` is then run, so every seeded signal
  carries a genuine entry-evaluation record, a generated rationale, a
  resolved Kite chart URL, and price levels that actually sit on the candles
  a chart will plot. Deliberately included: at least one signal per
  strategy, a risk-blocked signal (TCS, held in size so single-stock
  exposure fails), a symbol with **no** sector mapping (NEWLISTCO, so the
  skipped sector check can be seen), and four momentum candidates against a
  cap of three so one qualifying signal is always dropped.
  Note the E2E suite is destructive — it approves, rejects and confirms
  seeded items — so start a fresh demo server for each run.

### 1.4 What's covered

**`test_baseline_api.py`** — broad coverage of every core flow: session and
portfolio endpoints, instrument search, manual ticket creation/validation/
risk-checking/rejection/archiving/edit-restrictions, dry-run vs. live
confirmation (including OCO GTT placement, trailing → single-leg GTT,
order-rejected handling, protection-failed handling), cancel-all-pending,
manual trailing start, an exit ticket correctly closing a position,
emergency exit, execution mode switching, the order/GTT book and
cancellation, risk settings roundtrip and position-size calculation, sector
mappings, dashboard panels and UI preferences, watchlist add/remove/
duplicate handling, strategy config versioning, an end-to-end "scan now"
producing a real breakout signal, the backtest endpoint (including
walk-forward and Monte Carlo options), journal endpoints, reconciliation
listing/review, and trailing-engine ratchet plus vanished-GTT detection.

**`test_signal_detail.py`** — the Signal Detail view's backend:
- `GET /api/tickets/{id}/chart` returns candles, the strategy-appropriate
  indicator series, the ticket's own levels and the marker bars, for a
  signal produced by a real scan.
- That endpoint makes **no** Kite call at all (asserted against FakeKite's
  recorded calls), proving it reads from `ohlc_daily`.
- Its auth gate (403 on a non-trading account) and account scoping (404 on
  another account's ticket).
- Insufficient history, and no history at all, return clean diagnostics
  alongside the candles that do exist — never a 500.
- The RSI subpanel carries both the 35 entry and 55 exit threshold lines.
- The exit plan states the thesis-invalidation rule without inventing an
  "armed" flag for a signal that has not been executed yet.
- Rank against the daily cap is captured at scan time and exposed.
- Kite URL construction: the TradingView and ChartIQ variants, non-EQ series
  suffixes preserved (`-BE`, `-RR`, `-IV`), NSE/BSE dual listing resolving
  on the ticket's own exchange, an unresolvable symbol producing a disabled
  link plus a `system_events` row, a stale instrument cache still producing
  a link but flagged, a corporate-action rename falling back to the
  ticket's own token, and all four fallback tiers in order.
- The URL is stored at ticket-creation time, and backfilled on first view
  for tickets that predate the feature.
- The entry-evaluation record is persisted for all three strategies, its
  rows match the rules in DESIGN.md §6, its conditions AND up to the
  module's own `passed`, and the card's one-line description is generated
  from that same record.
- `rsi_mean_reversion` entry and exit unit coverage, including its hard
  trend filter, its RSI-recovery exit being evaluated before the shared
  checks, and its stop being the tighter of the swing low and the ATR
  level.
- A manual ticket (no evaluation record) and a malformed record both
  degrade gracefully rather than breaking the ticket list.

**`test_review_changes.py`** — a regression suite guarding specific
previously-fixed behaviors:
- Tick-size rounding/snapping correctness.
- AMO vs. regular order variety selection based on market hours.
- Order monitor: fill, reject, partial-fill, leave-alone, exit-reprice, and
  exit-reject-reopens-the-position scenarios.
- Kill switch cancels unfilled entries rather than attempting to sell them.
- Risk-engine correctness: a rejected exit leaves the position open; an
  executed exit removes the position's "phantom" risk from later
  calculations.
- Trailing engine: triggered-GTT detected as closed, deleted-GTT flags the
  position unprotected, ratchet snaps correctly to the tick grid, and
  pending (unfilled) entries are correctly skipped.
- Strategy engine: a signal is tick-aligned and dated correctly and is not
  re-proposed once already pending; an old risk-rejected signal does not
  block forever; insufficient-history diagnostics; signal expiry; and the
  thesis-invalidation "armed" logic for mean-reversion entries.
- Breakeven stop-adjustment confirmation uses live price and the tick grid,
  and returns readable errors on conflict (409).
- Reconciliation position-check correctly handles T+1 quantities and
  ignores surpluses (only flags shortfalls).
- OHLC refresh correctly re-prices a mid-session candle and deepens history
  exactly once when needed.
- Autopilot: full pipeline success, skip-on-no-new-candle, `is_due`
  scheduling/retry/catch-up logic, and the manual `run-now` endpoint.
- The September 2026 parameter revision: the revised defaults are pinned (so
  a silent edit back to the old loss-making values fails loudly),
  `backend/db.py`'s duplicated copy of those defaults is asserted equal to
  the modules' own, `trend_pullback`'s new volume filter is shown to reject
  an otherwise-valid pullback on thin volume, and a config blob saved
  *before* that parameter existed is shown to backfill rather than crash a
  live scan.

**`e2e_ui.py`** (Playwright) — exercises the real UI end to end: app load
and the approvals badge, every sidebar section routing correctly,
approve/reject/archive flows from Approvals, the ticket detail dialog,
Trade Queue tabs, the New Ticket form (autofill quantity, live preview,
submit → approval dialog), Strategies "scan now", the Journal equity
curve, the Orders GTT book, theme toggle persistence across reload,
hash-based deep linking, triggering autopilot from the UI, the emergency
exit typed-phrase gate, responsive layout at 820px width (no horizontal
overflow), and zero JavaScript console/page errors.
It also opens the **Signal Detail** view from an approvals card and asserts
the chart canvas renders, the three entry/stop/target price lines were
drawn, the six named risk-check rows and the itemised entry conditions are
present, the sizing derivation and exit plan are shown, the execution mode
is called out, the chart re-themes when the theme is toggled, ←/→ move
between pending signals without closing, there is no horizontal overflow at
820px, and Escape closes the view. (Price lines and chart colours are
painted into a canvas, so those two are asserted through the view's own
read-only `window.__signalDetail` handle rather than against the DOM.)

**LIVE-0 — live trading pre-flight** (`tests/test_live_preflight.py`,
`tests/test_live_readiness_ui.py` + `tests/js/render_readiness.js`): one
`ready` fixture in which every blocking check passes, then each test breaks
exactly one thing — schema pending/newer, integrity, broker session and
account identity, static IP (unconfirmed / user-confirmed / verified /
failed / stale observation), trading lock, notional and ₹ daily-loss unset,
worker dead / stalled / failing, reconciliation stale / critical /
reviewed / non-critical, UNKNOWN / needs-review / stale-submitting live
orders, strategy vs research history freshness, calendar coverage. Isolation
is proven by a digest of every table before and after (the pre-flight writes
nothing), by FakeKite recording no mutating call, and by the read-only proxy
refusing every mutating attribute. The confirm-time gate
(`LIVE_PREREQUISITE_MISSING`) is tested for live refusal before any broker
call, for dry run and exits never being blocked, and for Emergency Exit. The
`conftest.py` `env` fixture seeds deliberately generous prerequisites
(`seed_live_prerequisites`) so other tests' live entries are unaffected;
`clear_live_prerequisites` removes them. `tests/fixtures/kite_payload_shapes.json`
is the key/type structure (no values) of a real read-only Kite snapshot, and a
test pins the fields the PK-14/15 exposure maths reads to it. The `/ws/ticks`
handler is tested to end when a tab closes while no tick flows (the shutdown
hang LIVE-0 fixed).

**SEC-1 — live-trading safety gates** (`tests/test_live_critical_gate.py`,
`tests/test_live_arming.py`, plus the tightened `tests/test_csrf_protection.py`
/ `tests/test_security_hardening.py`). Covered:
- A write or WebSocket handshake with neither Origin nor Referer is refused
  on every mutating route, the static header notwithstanding (as are empty
  and `null` Origins).
- The confirm-time critical-state gate refuses a live entry before any
  broker call, for each degraded state: reconciliation never ran / stale /
  stamped in the future / GTT book or holdings unreadable / checks failed;
  each `CRITICAL_ISSUES` code and a NULL-coded finding; paper/live id mix;
  each critical worker dead / stalled / failing; uncovered calendar.
- The final gate catches a finding that appears after the claim, and the
  claim is withdrawn.
- Dry run is not gated. Emergency Exit, a linked exit, an alert-action sell
  and a switch to dry run all work with everything degraded at once.
- Mark-reviewed requires a reason and logs it.
- Arming LIVE runs a real broker-checked readiness evaluation (no stub),
  refuses each blocking code and changes nothing, and allows the trading
  lock to stay ON.
- A login or logout disarms LIVE, and an entry armed for an earlier session
  is refused.
- An AST test keeps the pre-SEC-1 setter `execution.set_execution_mode`
  uncalled. `backend/execution.py` is left byte-identical, because its hash
  is part of the PK-25 research freeze.

Fixtures: the `env` fixture now also seeds a healthy, just-reconciled
process (`seed_healthy_live_state`: workers alive and fresh, a complete
reconciliation cycle); `clear_healthy_live_state` gives a fresh process.
`set_mode(db, "live")` stamps the live arming for the seeded session.
`make_live_ready(env)` builds the state in which a real Live Readiness
evaluation passes, for tests that switch to LIVE through the API.

### 1.5 Known coverage gaps

These are not currently exercised by automated tests — treat them as a
backlog for future test-writing, and be extra careful verifying them
manually (see §2):

- `data_jobs.py`'s worker loop is not tested in isolation (only indirectly
  via flows that depend on it).
- `sector_mapping.py` edge cases beyond basic CRUD.
- `backtest_analytics.py`'s Sharpe/Sortino/Monte Carlo math is only
  exercised indirectly (the fill model, costs, capital and net metrics have
  known-answer tests in `tests/test_backtest_realism.py`; the ratios do not).
- No test for dashboard panel reordering edge cases beyond a single field
  change.
- `/ws/ticks`: overflow (PK-22), event-before-tick ordering and ending on a
  closed tab (LIVE-0) are tested; a live KiteTicker feed end-to-end is not.
- No load or rate-limit test for the Kite historical-data queue.

## 2. Manual Verification Checklist

Run this checklist in **Dry Run mode against a real Kite session** (real
market data, simulated orders) before ever trusting a strategy or the app
with live money, and again after any change to strategy logic, risk
settings, or execution code.

### 2.1 Environment sanity

- [ ] App starts cleanly (`python -m backend.server`) with no startup errors,
      and the startup log's `Security summary:` line shows `bind=127.0.0.1`,
      `remote_exposure=no`, `reload=no`, `workers=1`.
- [ ] Login flow completes and the session status shows active.
- [ ] Instrument cache refreshes without error.
- [ ] Watchlist and holdings both appear in the strategy scan universe.
- [ ] Execution mode defaults to Dry Run on first run.

### 2.2 Risk engine

- [ ] A manual ticket with reasonable parameters passes all six risk
      checks and reaches `pending_confirmation`.
- [ ] A manual ticket that exceeds max capital-per-trade is rejected with
      that specific check shown as failed (not a generic error).
- [ ] A manual ticket that exceeds max single-stock exposure is rejected.
- [ ] A manual ticket for a symbol whose sector is over-exposed is
      rejected — and confirm a symbol with an *unmapped* sector correctly
      skips that check rather than failing it.
- [ ] Opening one more position than `max_open_positions` allows is
      rejected.
- [ ] An exit ticket for a position that doesn't exist (or was already
      closed) is rejected by the `exit_or_reduction` check.
- [ ] Changing a risk setting is reflected immediately in the next
      ticket's checks, and the change appears in risk settings history.

### 2.3 Manual ticket → execution flow (Dry Run)

- [ ] Create, edit, and delete a draft ticket.
- [ ] Confirm a pending ticket and verify it becomes `executed` with a
      simulated fill (no real order placed).
- [ ] Reject a pending ticket and verify it moves to `rejected` and can be
      archived.
- [ ] Create an exit ticket against an open simulated position and confirm
      it closes correctly and produces a `closed_trades` journal entry.
- [ ] Confirm a rejected/cancelled/failed ticket can be archived but a
      confirmed/executed ticket cannot be edited or deleted.

### 2.4 Strategy scans

For **each** of the three strategies (`momentum_breakout`,
`trend_pullback`, `rsi_mean_reversion`):

- [ ] Enable the strategy with default parameters and run "Scan Now."
- [ ] Manually pick one symbol you expect to trigger a signal (verify the
      entry condition by hand against a chart/indicator values) and
      confirm the app either produces the expected signal or correctly
      does not, matching your manual read of the entry rule in the Design
      Document (§6).
- [ ] Verify the computed stop/target/trailing-distance match the formula
      in the Design Document for that strategy.
- [ ] Verify signals are capped at `max_signals_per_day` and the
      highest-ranked candidates are the ones kept when more symbols
      qualify than the cap.
- [ ] Change one parameter (e.g., trend filter length) and verify the
      change is versioned in the strategy's version history, while
      toggling enable/disable does *not* create a new version.
- [ ] Run the exit scan against an open strategy-opened position and
      verify the shared thesis-invalidation and time-exit checks trigger
      correctly; for `rsi_mean_reversion`, verify its own RSI-recovery
      exit is checked first.
- [ ] Run a scan twice in a row without new data and confirm no duplicate
      signal is created for the same candle.
- [ ] Advance to a new trading day (or use `demo_server.py`'s seeded data)
      and confirm an old, still-pending signal computed from a stale
      candle is auto-expired rather than approved against stale prices.

For each signal a scan produces, open its **Signal Detail** view from
Approvals (the **Chart** link on the card) and confirm:

- [ ] **The chart's plotted indicator values match the values the strategy
      claims it used.** Hover the signal bar and compare the tooltip's
      indicator readings against the numbers in the Entry Conditions panel
      — the SMA-200 in "Uptrend filter", the Donchian level in "Donchian
      breakout", the 20-EMA in "Pullback to 20-EMA", the RSI-14 in "RSI
      oversold". Both come from the same `indicators.py` functions and must
      agree exactly. A disagreement means the chart and the strategy are
      reading different data — investigate before trusting either.
- [ ] The entry/stop/target lines sit at the prices the card states, and
      the red/green shading is proportional to the stated R:R.
- [ ] The marked bars really are the ones the strategy keyed off — for
      `trend_pullback`, check by hand that the marked pullback bar's low is
      within the band of *that bar's own* 20-EMA, not today's.
- [ ] Position sizing reproduces by hand: risk per share × quantity equals
      the stated risk, and quantity is the floor of the risk budget divided
      by risk per share.
- [ ] For a symbol with no sector mapping, the sector risk check renders as
      *skipped*, not as a pass.
- [ ] "Open in Kite" opens the correct symbol's chart, and a symbol that
      cannot be resolved shows the button disabled with a stated reason
      rather than a dead link.

### 2.5 Live-mode-only checks (use extreme caution — real money)

Only perform these with small position sizes, ideally on a test/small
account, and only after all Dry Run checks above pass. The first live order
follows [FIRST_LIVE_TRADE.md](FIRST_LIVE_TRADE.md), starting from a Live
Readiness page that shows nothing blocking except the trading lock:

- [ ] Switching to Live requires the explicit confirmation dialog and is
      logged as a `system_events` row.
- [ ] A live entry places the entry order and immediately places the
      correct protective GTT (single stop-only if trailing is enabled,
      OCO stop+target otherwise).
- [ ] Simulate a GTT placement failure (or observe one) and confirm the
      ticket is marked `protection_failed` with a visible warning.
- [ ] The order monitor correctly resolves a filled entry, a rejected
      entry (ticket cancelled, no GTT), and a partially filled entry
      (flagged `urgent`).
- [ ] A rejected exit order correctly re-opens the position rather than
      leaving it incorrectly marked closed.
- [ ] The trailing engine ratchets the stop upward as price moves
      favorably, snapping correctly to the tick grid, and correctly
      detects a triggered or deleted GTT and synthesizes the matching
      closing ticket.
- [ ] Reconciliation correctly surfaces a deliberately induced mismatch
      (e.g., manually cancel a GTT directly in Kite) and requires explicit
      "Mark Reviewed" — nothing silently self-corrects.
- [ ] "Cancel All Pending" clears the queue without touching open
      positions.
- [ ] "Emergency Exit All" refuses to run without the exact typed phrase
      `EXIT ALL`, then correctly cancels unfilled entries and exits all
      open positions.

### 2.6 Journal & statistics

- [ ] Dry Run and Live trade history are kept fully separate in the
      Journal.
- [ ] Win rate, expectancy (R), profit factor, and drawdown match a manual
      recalculation from the underlying closed trades for a small sample.
- [ ] The small-sample-size warning appears below 20 trades and disappears
      once past that threshold.
- [ ] By-strategy and by-symbol breakdowns sum consistently with the
      overall totals.

### 2.7 Backtesting

- [ ] Run a backtest for each strategy over a known historical period and
      spot-check 2–3 individual trades by hand against the raw OHLC data
      to confirm entry/exit prices and dates are correct.
- [ ] Confirm the backtest's entry/exit logic matches live scan results
      for the same symbol/date when both are available (they share the
      same code path, so a mismatch indicates a bug).
- [ ] Enable the walk-forward consistency check and confirm each window's
      results are reported separately without parameter refitting between
      windows.
- [ ] Enable Monte Carlo reshuffling with at least 5 trades and confirm it
      returns a distribution of drawdown/equity outcomes rather than a
      single number.

### 2.8 Frontend / cross-cutting

- [ ] Every sidebar section loads without a console error.
- [ ] The WebSocket connection indicator turns green and price ticks
      update visible tables without a manual refresh.
- [ ] Theme toggle persists across a page reload.
- [ ] Hash-based deep links (e.g., linking directly to Approvals) work.
- [ ] Layout has no horizontal overflow at a narrow (≈820px) viewport
      width.

## 3. Regression Policy

Whenever strategy logic, risk-check thresholds, or execution/order-monitor
code changes:

1. Run the full automated suite (`pytest`) and confirm no regressions in
   `test_baseline_api.py` or `test_review_changes.py`.
2. Re-run the relevant subsection of the manual checklist above (§2.4 for
   strategy changes, §2.2 for risk changes, §2.5 for execution changes).
3. For any strategy parameter change, run a fresh backtest and compare
   headline statistics (win rate, expectancy, drawdown) against the
   pre-change baseline before enabling it live.
4. Judge any such change **net of transaction costs**. Since PK-21
   `backend/backtest.py` charges them (see `backend/backtest_costs.py` and
   `config.BACKTEST_COST_DEFAULTS`); read the **net** figures. On ATR-based
   stops a round trip is roughly 0.08–0.10R —
   enough on its own to turn a gross-positive strategy net-negative. The
   September 2026 revision found all three shipped strategies looking
   merely mediocre on gross numbers and clearly losing once costs applied.
5. Prefer a parameter in the middle of a broad plateau over the single best
   point of a grid, and require it to hold in both halves of the sample. A
   value that only works on one half, or that degrades sharply one notch
   either side, is curve-fitting rather than a finding.
