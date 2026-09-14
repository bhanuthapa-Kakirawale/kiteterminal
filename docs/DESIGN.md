# Kite Terminal — App Design Document

## 1. Overview

Kite Terminal is a semi-automated algorithmic trading assistant built on top of
Zerodha's Kite Connect API. It runs as a single FastAPI backend serving a
vanilla-JS single-page frontend from the same origin (no separate build step,
no CORS). The system watches a configurable universe of NSE equities, detects
strategy signals, and queues them as **tickets** that a human must review and
approve before any order reaches the broker — the app never places a live
order autonomously without a human-in-the-loop approval step, and dry-run mode
is the default.

Core design goals reflected throughout the codebase:

- **Human approval gate on every trade.** Strategy signals and manual trades
  alike become `trade_tickets` that must pass risk checks and be explicitly
  confirmed before execution.
- **One shared code path for live and simulated execution**, and one shared
  code path for live and backtested strategy logic, so behavior never drifts
  between modes.
- **Never trust the client.** Symbol→instrument-token resolution, session
  validity, account type, and risk checks are all re-verified server-side on
  every request.
- **Assume the broker and the app can disagree**, and reconcile continuously
  rather than assume state stays in sync.

## 2. High-Level Architecture

```
┌─────────────────────────┐        ┌──────────────────────────────┐
│   Browser (frontend/)   │  HTTP  │        FastAPI app             │
│  index.html + app.js    │◄──────►│        (backend/main.py)       │
│  (vanilla JS, no build) │  WS    │                                │
└─────────────────────────┘        │  16 routers (backend/routes/) │
            ▲                      │  Domain modules (backend/*.py)│
            │ /ws/ticks            │  5 background asyncio tasks   │
            └──────────────────────┤                                │
                                    └───────────────┬────────────────┘
                                                     │
                              ┌──────────────────────┼───────────────────┐
                              ▼                      ▼                   ▼
                     ┌────────────────┐   ┌────────────────────┐  ┌────────────┐
                     │ SQLite (WAL)   │   │ Kite Connect API    │  │ KiteTicker │
                     │ kite_terminal  │   │ (REST, via          │  │ (WebSocket │
                     │ .db — 24 tables│   │  kite_client.py)    │  │  ticks)    │
                     └────────────────┘   └─────────────────────┘  └────────────┘
```

The frontend is served statically (`/static`) and via `/` from `frontend/`;
there is no separate frontend server or CORS configuration.

### Backend startup / shutdown (`backend/main.py`)

On `startup`:
1. `init_db()` — runs schema + idempotent migrations, seeds defaults.
2. Sets the ticker manager's event loop; if a Kite session is active, starts
   the WebSocket ticker for holdings + watchlist instruments.
3. Refreshes the instrument cache if stale, seeds sector mappings from
   holdings.
4. Launches five background `asyncio` tasks (stored on `app.state`):
   `trailing_task`, `data_job_worker_task`, `reconciliation_task`,
   `order_monitor_task`, `automation_task`.
5. If autopilot isn't due yet, kicks a one-time `sync_all_symbols()`.

On `shutdown`: stops the ticker and cancels all five background tasks.

### Storage (`backend/db.py`)

SQLite in WAL mode with foreign keys enabled. 24 tables; the central one is
`trade_tickets`, with `ticket_events` as its full audit trail. `init_db()` is
idempotent — safe to call on every startup — and runs ~15 migration functions
including three full-table-rebuild migrations for `CHECK` constraint changes.
It also seeds a default account from `KITE_API_KEY`/`KITE_API_SECRET`,
default execution settings (`dry_run`), default risk settings, default
dashboard panels, and disabled strategy configs.

Key tables: `accounts`, `sessions`, `holdings_snapshot`, `positions_snapshot`,
`trade_tickets`, `ticket_events`, `instrument_cache`, `execution_settings`,
`risk_settings` (+ `risk_settings_history`), `sector_mapping`, `watchlist`,
`ohlc_daily` (+ `ohlc_meta`), `indicator_snapshot`, `strategy_configs` (+
`strategy_config_versions`), `backtest_runs`/`walk_forward_windows`/
`backtest_trades`, `closed_trades`, `stop_adjustment_proposals`,
`system_events`, `reconciliation_log`, `automation_runs`, `dashboard_panels`.

### Configuration (`backend/config.py`)

Pure Python constants (no env-driven tunables besides the Kite credentials in
`.env`). Grouped by concern: alert thresholds, execution timing/behavior,
OHLC backfill sizing, strategy-engine tolerances, autopilot scheduling, and
reconciliation cadence. See §8 of this document for the values.

## 3. Domain Modules

| Module | Responsibility |
|---|---|
| `instruments.py` | NSE equity instrument cache; symbol search and authoritative symbol→token resolution (never trusts client-sent tokens). |
| `market.py` | IST market-hours clock (`09:15`–`15:30`, weekdays only, no holiday calendar); tick-size rounding. |
| `ohlc.py` | Daily candle backfill/refresh from Kite historical data, chunked to ≤400 days per request, dropping any still-forming candle. |
| `indicators.py` | SMA/EMA/Wilder's RSI/Wilder's ATR/Donchian channel/avg-volume, computed from a single shared OHLC read path used by both live scans and the backtester. |
| `data_jobs.py` | Single-consumer async queue guaranteeing only one Kite `historical_data()` call is ever in flight, rate-limited to respect Kite's 3 req/s limit. |
| `execution.py` | Order/GTT placement: dry-run simulation, live entry (order + protective GTT), and live exit (order + GTT cleanup). |
| `order_monitor.py` | Background loop polling the broker's order book to resolve pending tickets to terminal states (filled, rejected, partially filled). |
| `trailing_engine.py` | Background loop that ratchets stop-loss GTTs upward as price moves favorably, and detects/closes positions whose GTT has triggered or vanished. |
| `reconciliation.py` | Background loop running three independent app-vs-broker consistency checks. |
| `risk_engine.py` | Pre-trade gate: six checks for every entry, one check for every exit; also portfolio-context and position-sizing math. |
| `strategy_engine.py` | Generic scan orchestration (entry/exit/breakeven) shared by every strategy module; config CRUD with versioning. |
| `strategies/*.py` | Strategy-specific entry/exit signal logic (see §6). |
| `automation.py` | "Autopilot" — runs the full post-close pipeline (sync data → scan strategies → queue tickets) once per completed session, with retry/backoff. |
| `journal.py` | Single source of truth for realized P&L; trade statistics (win rate, expectancy, profit factor, drawdown). |
| `backtest.py` / `backtest_analytics.py` | Historical simulation reusing the exact live-scan entry/exit functions; risk/reward analytics (Sharpe, Sortino, Monte Carlo). |
| `watchlist.py` | CRUD for the symbol watchlist. |
| `sector_mapping.py` | Hardcoded sector lookup (~65 large-caps, 11 sectors) used for sector-exposure risk checks. |
| `todays_actions.py` | Read-only pre-market briefing aggregator (buy/exit candidates, pending adjustments, near-breakout watch list). |
| `accounts.py` / `kite_client.py` | Session/account resolution; FastAPI dependencies (`require_kite`, `require_trading_account`) enforcing server-side auth on nearly every route. |
| `ticker.py` | Wraps `KiteTicker`, fanning live ticks out to all connected browser WebSockets. |
| `crypto.py` | Fernet encryption of `api_secret`/`access_token` at rest. |

## 4. The Trade Ticket Lifecycle

Every trade — manual or strategy-generated — flows through the same
`trade_tickets` state machine:

```
draft/pending ──risk checks──► pending_confirmation ──human confirm──► executed
        │                             │                                    │
        └──────► rejected ◄───────────┘ (risk_rejected)              (fills tracked
                                                                        by order_monitor)
```

- **Creation**: manual tickets are created via `POST /api/tickets`; strategy
  tickets are created by `strategy_engine.run_entry_scan` /
  `run_exit_scan`. Both paths converge on the same risk-check and
  ticket-construction logic.
- **Risk gate**: `risk_engine.run_risk_checks_and_transition` moves the
  ticket to `pending_confirmation` (all checks pass) or `risk_rejected`
  (persisting the full check breakdown as JSON for the UI to display).
- **Confirmation**: `POST /api/tickets/{id}/confirm` is the single shared
  path used by manual approval, strategy-signal approval, and Emergency Exit
  All. It calls `execution.execute_ticket`, which dispatches to dry-run
  simulation, live entry, or live exit.
- **Live entry**: places the entry order, then immediately places a
  protective GTT (single stop-only leg if trailing is enabled, otherwise an
  OCO stop+target). If the GTT placement fails after the entry order filled,
  the ticket is marked `protection_failed` with an explicit UI warning to
  place a manual stop by hand.
- **Live exit**: places the exit order, then synchronously deletes the
  original position's GTT.
- **Post-execution tracking**: `order_monitor.py` polls until the order
  reaches a terminal broker state and updates the ticket accordingly
  (including re-opening a position if an exit order is rejected).
- **Every transition is logged** to `ticket_events` for audit.

## 5. Risk & Safety Design

Safety is layered, not single-point:

1. **Execution mode gate** — the app defaults to `dry_run`. Switching to
   `live` requires an explicit `confirm_real_money=True` flag from the
   frontend confirmation dialog, and is logged as a `system_events` row.
2. **Pre-trade risk engine** (`risk_engine.py`) — six checks always run in
   full for every entry (never short-circuited), so the UI can show a
   complete pass/fail breakdown even when the trade is rejected:
   - minimum risk/reward ratio
   - maximum capital per trade (%)
   - maximum single-stock exposure (%)
   - maximum sector exposure (%) — skipped if the symbol's sector is unmapped
   - maximum number of open positions
   - maximum total portfolio risk (%)

   Exits run a single `exit_validity` check instead (that the ticket
   references a valid, still-open, executed BUY).
3. **Protective GTT always-on** — no live entry is ever left unprotected by
   design; failure to place the protective GTT is surfaced loudly rather than
   silently retried.
4. **Kill switch** (`kill_switch.py`) — "Cancel All Pending" rejects every
   draft/pending ticket in one action. "Emergency Exit All" requires the user
   to type the exact phrase `"EXIT ALL"` (validated server-side), cancels any
   unfilled entry orders instead of trying to sell them, and exits everything
   else through the normal confirm-and-execute path.
5. **Order monitor** — follows every live order to a terminal state,
   auto-cancels tickets whose entry never filled, and flags partial fills or
   rejected exits as `urgent` (an exit rejection means the position is still
   open with a phantom "closed" record if not caught).
6. **Trailing engine** — detects when a stop GTT has triggered or vanished
   from the broker's side and synthesizes the correct closing ticket so the
   journal stays accurate even if the position closed "out of band."
7. **Reconciliation loop** — every 3 minutes, independently re-derives (a)
   order status, (b) GTT state, and (c) expected open-position quantities
   from the broker's own data and compares against the app's records.
   Mismatches are persisted, deduplicated, and require an explicit "Mark
   Reviewed" — nothing is auto-resolved.
8. **Signal expiry** — a pending strategy signal computed from a stale
   (older) candle is auto-cancelled before the next scan runs, so a human can
   never approve a signal against stale price levels.
9. **Autopilot backoff** — capped at 3 failed attempts/day with a 15-minute
   retry delay, so a broker outage cannot spin the automation loop
   indefinitely.

## 6. Strategy Engine Design

`strategy_engine.py` implements the parts of a scan that are identical
across every strategy — position sizing, tick-grid snapping, risk validation,
and ticket construction — and delegates only the market-logic decision to a
per-strategy module via a small, uniform interface:

- `evaluate_entry(symbol, indicators, params) -> signal | None`
- `evaluate_exit(position, indicators, params) -> exit signal | None`
- `compute_entry_levels(...)` — stop/target/trailing-distance calculation
- `rank_key(...)` — used to prioritize signals when more candidates pass than
  `max_signals_per_day` allows

All three shipped strategies also share `strategies/shared.py`'s
`shared_exit_checks`: a thesis-invalidation exit (close falls back below a
configurable EMA — "armed" only after price has closed above it since entry,
so mean-reversion trades that intentionally start below the EMA aren't
exited immediately) and a time-based exit (position held longer than
`time_exit_days` without hitting stop or target).

### Shipped strategies

| Strategy | Entry condition | Stop / Target | Notes |
|---|---|---|---|
| **`momentum_breakout`** | Close > SMA-200 AND close > 20-day Donchian high (excl. today) AND volume > 1.5× avg-20 volume | Stop = entry − 2×ATR14; target = entry + 2×risk; trailing = 1.5×ATR14 | Max 3 signals/day; ranked by how decisively price broke the level. |
| **`trend_pullback`** | Uptrend (close > SMA-200) AND price pulled back to within 1.5% of its 20-EMA within the last 3 days AND is now recovering (close > prev close AND close > EMA) | Stop = entry − 1.5×ATR14 (tighter); target = entry + 2×risk | Max 2 signals/day; ranked by recovery strength above the EMA. |
| **`rsi_mean_reversion`** | Hard trend filter (close > SMA-200, never bypassed) AND RSI-14 < 35 AND (optionally) RSI turning up vs. previous day | Stop = max(10-day swing low, entry − 1.0×ATR14) — deliberately the tighter of the two; target = entry + 1.5×risk; **no trailing** | Exit checks RSI ≥ 55 (recovered to neutral) first, then falls back to shared checks; max 2 signals/day. |

Strategy parameters are validated server-side per strategy via Pydantic
models in `backend/models.py`. Configuration changes are versioned
(`strategy_config_versions`) — a version bump happens only on a genuine
parameter change, not on enable/disable toggling.

## 7. Backtesting Design

`backtest.py` reuses the exact same `evaluate_entry` / `evaluate_exit` /
`compute_entry_levels` functions the live scanner calls, so backtested and
live logic can never silently diverge. Documented simplifying assumptions:

- If both stop and target are touched on the same day, the stop is assumed
  hit first (conservative).
- Trailing stops are evaluated once per day at the close, not intraday.
- Position sizing uses the account's *current* portfolio value, not a
  historically accurate one.

`backtest_analytics.py` adds Sharpe/Sortino (annualized via an implied
trades-per-year figure, risk-free rate = 0), max consecutive losses, recovery
factor, average win/loss in R-multiples, and time-in-market %. An optional
walk-forward "consistency check" (explicitly *not* walk-forward optimization
— no parameter refitting between windows) and Monte Carlo trade-reshuffling
(minimum 5 trades, no fixed random seed by default) produce drawdown/equity
distribution percentiles.

## 8. Key Configuration Constants (`backend/config.py`)

| Category | Constant | Value |
|---|---|---|
| Alerts | `HOLDING_LOSS_ALERT_PCT` | 8.0 |
| Alerts | `CONCENTRATION_ALERT_PCT` | 20.0 |
| Alerts | `DAY_PNL_ALERT_PCT` | 2.0 |
| Alerts | `SESSION_EXPIRY_WARNING_MINUTES` | 60 |
| Alerts | `MARGIN_ALERT_THRESHOLD_INR` | 10,000.0 |
| Execution | `GTT_SLIPPAGE_BUFFER_PCT` | 0.5 |
| Execution | `TRAILING_CHECK_INTERVAL_SECONDS` | 45 |
| Execution | `INSTRUMENT_CACHE_MAX_AGE_HOURS` | 24 |
| Execution | `USE_AMO_WHEN_MARKET_CLOSED` | True |
| Execution | `ORDER_MONITOR_INTERVAL_SECONDS` | 20 |
| OHLC | `HISTORICAL_RATE_LIMIT_DELAY_SECONDS` | 0.4 |
| OHLC | `MAX_DAYS_PER_HISTORICAL_REQUEST` | 400 |
| OHLC | `BACKFILL_TARGET_DAYS` | 1100 |
| OHLC | `REFRESH_OVERLAP_DAYS` | 7 |
| OHLC | `DAILY_REFRESH_HOUR_IST` | 16 |
| Strategy engine | `WATCH_PROXIMITY_PCT` | 3.0 |
| Strategy engine | `ENTRY_PRICE_BUFFER_PCT` | 0.1 |
| Strategy engine | `MAX_DATA_STALENESS_DAYS` | 5 |
| Autopilot | `AUTOMATION_POLL_SECONDS` | 60 |
| Autopilot | `SIGNAL_EXPIRY_ENABLED` | True |
| Autopilot | `AUTOMATION_MAX_ATTEMPTS_PER_DAY` | 3 |
| Autopilot | `AUTOMATION_RETRY_MINUTES` | 15 |
| Reconciliation | `RECONCILIATION_INTERVAL_SECONDS` | 180 |

## 9. API Surface (summary)

16 routers under `backend/routes/`. Full endpoint list is in the User Guide
(§"API reference" is intentionally omitted there in favor of task-based
instructions); the routers are: `auth`, `portfolio`, `ws`, `tickets`,
`orders`, `instruments`, `risk`, `ui_prefs`, `dashboard_panels`, `watchlist`,
`strategies`, `journal`, `kill_switch`, `reconciliation`, `automation`.

Nearly every route depends on `require_kite` / `require_trading_account`
(FastAPI dependencies) for a hard server-side auth/permission gate,
independent of any frontend state.

## 10. Frontend Design

Single-page vanilla JS app (`frontend/app.js`, no framework, no build step),
served same-origin. Dark/light theme persisted to `localStorage`.
`Sortable.js` (CDN) provides drag-to-reorder for the dashboard panel grid.
Hash-based routing across 11 sidebar sections: Dashboard, Approvals, Today's
Actions, Trade Queue, Orders & Trades, New Ticket, Watchlist, Strategies,
Journal, Ticket History, Settings.

A single WebSocket connection (`/ws/ticks`) delivers both live price ticks
and app-event broadcasts (`tickets_changed`, `order_update`,
`trailing_heartbeat`, `automation_run`, `auth_error`), letting the UI update
reactively instead of polling.

## 11. External Dependencies

| Package | Version | Purpose |
|---|---|---|
| `fastapi` | 0.115.6 | Web framework |
| `uvicorn[standard]` | 0.34.0 | ASGI server |
| `kiteconnect` | 5.0.1 | Zerodha Kite Connect SDK |
| `python-dotenv` | 1.0.1 | `.env` loading |
| `cryptography` | 50.0.1 | Fernet encryption of stored credentials |
| `pandas` | 2.2.3 | OHLC/indicator data handling |
| `pytest`, `httpx`, `playwright` (dev) | 9.1.1 / 0.28.1 / 1.62.0 | Testing |

Note: `numpy` is used directly in `journal.py` but is not pinned explicitly
in `requirements.txt` — it arrives transitively via `pandas`.
