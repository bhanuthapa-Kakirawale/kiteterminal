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

- **`tests/conftest.py`** — the `env` fixture spins up a fresh throwaway
  SQLite database per test, a fresh encryption key, 5 seeded instruments, an
  active session, and monkeypatches `kite_client.get_kite` to return a
  `FakeKite`. The real ticker is disabled and WebSocket broadcasts are
  captured into a list for assertions. Startup background loops (trailing,
  reconciliation, etc.) are **not** run during tests — each test controls
  its own scenario explicitly.
- **`tests/fake_kite.py`** — a full in-memory stand-in for
  `kiteconnect.KiteConnect`: holdings/positions/margins/LTP,
  order placement/history/cancel (with deterministic order IDs and an
  auto-fill toggle), GTT placement/modify/delete, historical data,
  instruments. Records every call for assertion and supports `fail_next` to
  simulate a broker error on demand.
- **`tests/demo_server.py`** — a standalone runnable instance of the real
  app against a throwaway DB and a synthetic 8-symbol universe with
  realistic seeded state (pending signals, a risk-blocked signal, two open
  live positions with GTTs, a pending exit signal, a pending breakeven
  adjustment, historical closed trades, and autopilot run history). Useful
  both for manual exploration and as the E2E test target.

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

**`e2e_ui.py`** (Playwright) — exercises the real UI end to end: app load
and the approvals badge, every sidebar section routing correctly,
approve/reject/archive flows from Approvals, the ticket detail dialog,
Trade Queue tabs, the New Ticket form (autofill quantity, live preview,
submit → approval dialog), Strategies "scan now", the Journal equity
curve, the Orders GTT book, theme toggle persistence across reload,
hash-based deep linking, triggering autopilot from the UI, the emergency
exit typed-phrase gate, responsive layout at 820px width (no horizontal
overflow), and zero JavaScript console/page errors.

### 1.5 Known coverage gaps

These are not currently exercised by automated tests — treat them as a
backlog for future test-writing, and be extra careful verifying them
manually (see §2):

- `data_jobs.py`'s worker loop is not tested in isolation (only indirectly
  via flows that depend on it).
- `sector_mapping.py` edge cases beyond basic CRUD.
- `backtest_analytics.py`'s Sharpe/Sortino/Monte Carlo math is only
  exercised indirectly through the backtest endpoint test, not directly
  with known-answer inputs.
- `rsi_mean_reversion` has no dedicated entry/exit unit test — only
  `momentum_breakout` and `trend_pullback` are directly exercised via
  scan-now/backtest tests.
- No test for dashboard panel reordering edge cases beyond a single field
  change.
- No negative/permission test for a non-trading Kite account type.
- No test of the `/ws/ticks` WebSocket tick delivery itself.
- No load or rate-limit test for the Kite historical-data queue.

## 2. Manual Verification Checklist

Run this checklist in **Dry Run mode against a real Kite session** (real
market data, simulated orders) before ever trusting a strategy or the app
with live money, and again after any change to strategy logic, risk
settings, or execution code.

### 2.1 Environment sanity

- [ ] App starts cleanly (`uvicorn backend.main:app`) with no startup errors.
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

### 2.5 Live-mode-only checks (use extreme caution — real money)

Only perform these with small position sizes, ideally on a test/small
account, and only after all Dry Run checks above pass:

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
