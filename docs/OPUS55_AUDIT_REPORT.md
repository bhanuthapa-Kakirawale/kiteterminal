# Kite Terminal — Independent Engineering Audit (Opus 5.5)

| | |
|---|---|
| Audited revision | `13ae3f8` (branch `feature/signal-detail-and-strategy-revision`) |
| Backups taken before audit | tag `backup-pre-opus55-audit`, branch `backup/pre-opus55-audit`, `backend/kite_terminal.db.pre_opus55_audit_bak` |
| Date | 2026-09-27 |
| Scope | Entire repository: backend, frontend, schema, tests, docs. Audit only; no source file was modified. |
| Baseline tests | `pytest`: **115 passed in 15.2 s** (113 test functions, some parametrised) |

Every finding below was checked against the code at the revision above, with file, function and line references.
The report says whether each defect is **proven by code reading** or **only a risk**. None were reproduced against the real broker, because live order placement has never succeeded. The DB holds four live attempts, all rejected by Kite with *"No IPs configured for this app"*.

---

# Executive Summary

Kite Terminal is a well-structured, heavily commented, human-in-the-loop trading assistant. The team clearly knew where the dangerous edges were. Examples:
- A placed entry is explicitly *not* treated as a fill: `entry_fill_status='pending'` holds until `order_monitor.py` sees `COMPLETE`.
- Protective-GTT failure is surfaced loudly.
- Ambiguous network errors are never blindly retried.
- Dry-run and live P&L are never mixed.
- Signals never use a still-forming candle.

**It is nevertheless not yet safe to place real-money orders.** Eight P0 issues were verified in the code. Four of them can put real money at risk without any action from the user beyond what the UI invites. The rest corrupt the app's picture of which positions are open and protected.

1. **No server-side idempotency on order submission.** Two concurrent confirms of the same ticket both reach `place_order` (P0-1).
2. **A network timeout during `place_order` strands the ticket.** It is left in `confirmed` with no order id, no GTT, and nothing monitoring it, while the order may be live (P0-2).
3. **Execution mode is global, not per position.** In DRY RUN mode, "Emergency Exit All" or any exit approval *simulates* the exit of a *live* position and marks it closed in the app. The real shares stay held and all monitoring of them stops (P0-3).
4. **Confirmation does not re-validate.** A `draft` ticket that never passed risk checks can be confirmed. An exit ticket can be confirmed after its position has already closed, which sends a second SELL (P0-4).
5. **Positions without trailing (OCO GTT) are never marked closed** when their stop or target fires (P0-5).
6. **"GTT triggered" is treated as "position closed"**, even when no fill was found (P0-6).
7. **"Exit order placed" is treated as "position closed".** The protective GTT is deleted and the journal written before the exit fills (P0-7).
8. **CSRF.** Any web page open in the same browser can POST to `/api/tickets/{id}/confirm` and approve a trade (P0-8).

All eight have small, local fixes. None needs a redesign or a framework change. The recommended path:
- First, add failure-path tests that pin down the current behaviour (packet PK-0).
- Then land the P0 fixes as small, independently testable packets in the order given in the Recommended Implementation Packets section.
- Finally, do a controlled one-share live test, once the static IP is configured and the GO/NO-GO checklist is green.

The strategy logic itself is implemented correctly: indicators, Donchian construction and data timing are verified. **The in-app backtester, however, systematically overstates realism.** It assumes:
- a same-bar entry fill;
- same-close exits;
- stop fills at the stop price through gaps;
- zero transaction costs;
- no portfolio caps.

Its numbers should not be used for capital decisions until those points are addressed. The 0.40 %-cost study quoted in `docs/DESIGN.md` was done with scripts that are not in the repository, so it cannot be reproduced.

---

# Current Architecture

## Repository structure
| Area | Contents | Size |
|---|---|---|
| `backend/` | FastAPI app (`main.py`), 16 routers (`routes/`), ~30 domain modules, `strategies/` (3 strategies + shared + conditions) | ~9.5 k LOC |
| `frontend/` | `index.html` (1,383), `app.js` (4,942), `style.css` (1,528); vanilla JS, no build step; Sortable.js and lightweight-charts from CDN | ~7.9 k LOC |
| `tests/` | pytest suite with in-memory `FakeKite`, `demo_server.py`, Playwright `e2e_ui.py` smoke script | ~2.3 k LOC |
| `docs/` | `DESIGN.md`, `USER_GUIDE.md`, `TESTING.md` | |
| DB | `backend/kite_terminal.db` (SQLite WAL, 24 tables, untracked in git) | ~6 MB |

## Backend
Single process: uvicorn plus FastAPI.
- **Route handlers** are plain `def` functions, so they run in the Starlette threadpool.
- **Five background asyncio tasks** start in `main.on_startup`:

| Task | How it runs |
|---|---|
| `trailing_engine.run_trailing_loop` | 45 s cycle, **on the event loop** |
| `order_monitor.run_order_monitor_loop` | 20 s cycle, in a thread |
| `reconciliation.run_reconciliation_loop` | 180 s cycle, in a thread |
| `automation.run_automation_loop` | 60 s poll |
| `data_jobs.run_job_worker` | single-consumer historical-data queue |

- **Database access:** every function opens its own `sqlite3` connection (`db.get_connection`). There is no connection pool and no ORM.

## Frontend
- Hash-routed single-page app with 11 sections.
- One WebSocket (`/ws/ticks`) carries both ticks and app events: `tickets_changed`, `order_update`, `trailing_heartbeat`, `trailing_error`, `automation_run`, `auth_error`.
- Global `state` object; `renderAll()` re-renders on every tick batch.

## Database / schema
- **`trade_tickets` is the central table.** One row represents:
  - the trade intent;
  - its broker order (`kite_order_id`, `order_variety`, `entry_fill_status`, `filled_price`);
  - the resulting position (`closed_at`);
  - its protection (`kite_gtt_id`, `current_stop_loss`, `trailing_active`).
- **An exit** is a second `trade_tickets` row whose `closes_ticket_id` points at the entry.
- **Other tables:**
  - `ticket_events` is the audit trail;
  - `closed_trades` is the realised-P&L journal;
  - `reconciliation_log`, `system_events` and `automation_runs` hold operational logs;
  - `ohlc_daily` and `indicator_snapshot` hold market data;
  - `strategy_configs` and `strategy_config_versions` hold versioned parameters.
- **Migrations** are about 20 idempotent introspection functions run by `init_db()` on every start. Five of them are full table rebuilds for `CHECK`-constraint changes.

## Zerodha integration and session lifecycle
- `/login` redirects to Kite. `/callback` exchanges the `request_token` (`kite_client.exchange_request_token`).
- The access token is stored in `sessions` (plaintext) and in `accounts.access_token_enc` (Fernet).
- `get_kite()` accepts a session only if its `login_time` is today (IST).
- `require_kite` and `require_trading_account` are the FastAPI dependencies that gate the API. They check that a *broker session* exists, not who is making the HTTP request.

## Market data and ticker
- **Daily OHLC:** `ohlc.py` backfills about 1,100 calendar days in 400-day chunks, then refreshes a 7-day overlap. Any candle newer than `market.last_complete_session_date()` is dropped.
- **Indicators:** recomputed after every refresh (`indicators.compute_and_store`).
- **Live ticks:** `ticker.TickerManager` wraps `KiteTicker` and fans ticks out to browser sockets. Ticks are not stored server-side.

## Trade lifecycle as implemented
```
signal (strategy_engine.run_entry_scan, post-close)          manual: POST /api/tickets
  -> trade_tickets 'draft' -> risk_engine.run_risk_checks_and_transition
  -> 'pending_confirmation' | 'risk_rejected' | 'cancelled'(risk check unavailable)
  -> POST /api/tickets/{id}/confirm  (routes/tickets.confirm_and_execute_ticket)
       status='confirmed', mode=<global mode at this instant>
       execution.execute_ticket:
         dry_run  -> _simulate (filled immediately at entry_price)
         entry    -> _execute_live: place_order (REGULAR or AMO) -> ltp -> place_gtt (SINGLE if trailing else OCO)
         exit     -> _execute_exit: place_order -> delete original GTT
       status='executed', entry_fill_status='pending' (live) | 'filled' (dry run)
       exit: original.closed_at=now, closed_trades row written (reference price)
  -> order_monitor: COMPLETE -> filled; REJECTED/CANCELLED -> cancel ticket + delete GTT (entry)
                                                       -> reopen original (exit)
  -> trailing_engine (trailing tickets only): activate at target_1, ratchet GTT, detect GTT closure
  -> reconciliation: order status, GTT active, position shortfall -> reconciliation_log
  -> journal: closed_trades -> stats
```

## Other subsystems
- **Kill switch:**
  - "Cancel All Pending" rejects draft and pending tickets.
  - "Emergency Exit All" requires typing "EXIT ALL", then cancels unfilled entries and exits everything else with MARKET SELLs, through the normal confirm path.
- **Autopilot:** runs at 16:00 IST after each session: sync → freshness check → expire stale signals → scan every enabled strategy (entries, exits, breakeven proposals). At most 3 attempts per day, 15 minutes apart.
- **Background-job error handling:** each loop wraps its whole cycle in `try/except Exception` and logs, so one failure never kills a loop.
- **Logging:** `logging.basicConfig` to stdout only.

---

# What Is Already Well Designed

These were verified in code and are marked **VERIFIED SAFE**.

| # | Protection | Where |
|---|---|---|
| S1 | **A placed entry order is not treated as a fill.** Live executions return `entry_fill_status="pending"`, and only `order_monitor._resolve_entry` sets `filled` on broker `COMPLETE`. Trailing skips pending entries; reconciliation and the exit scan exclude them. | `execution.py:_execute_live` L256-264; `order_monitor.py:_resolve_entry` L141-150; `trailing_engine.py` L53 |
| S2 | A REJECTED or CANCELLED entry with zero fill deletes its protective GTT and cancels the ticket. | `order_monitor.py:_resolve_entry` L170-184 |
| S3 | A rejected or cancelled exit re-opens the position, checks whether the original GTT is still active, and raises an urgent alert and reconciliation row. | `order_monitor.py:_resolve_exit` L220-252 |
| S4 | When the protective GTT fails after an entry, the outcome is `protection_failed`, not "failed". The UI shows a persistent urgent banner. | `execution.py` L241-254; `app.js` L790-800 |
| S5 | NetworkException and TokenException on `place_order` are reported as "outcome UNKNOWN — verify manually" and never retried. (Raw `requests` errors are not handled — see P0-2.) | `execution.py` L180-192 |
| S6 | Instrument tokens are always resolved server-side; client tokens are never trusted. | `instruments.get_instrument_by_symbol` |
| S7 | All order and GTT prices are snapped to the instrument's tick grid. | `market.round_to_tick`, `execution.py` |
| S8 | A still-forming daily candle is never stored, so live signals are computed on final bars. | `ohlc._store_candles` L19-33 |
| S9 | The Donchian level excludes the current bar (`shift(1)`). | `indicators.donchian_high_excl_today` |
| S10 | RSI and ATR use Wilder smoothing (`alpha=1/period, adjust=False, min_periods=period`). The EMA is the conventional recursive one. | `indicators.py` |
| S11 | Indicator warm-up is adequate: about 750 candles are stored, and SMA-200 and EMA-50 are fully converged before any tested date. | `config.BACKFILL_TARGET_DAYS` |
| S12 | Journal statistics filter on `mode` first; dry run and live are never mixed. | `journal.compute_stats` |
| S13 | Switching to live requires `confirm_real_money=true` server-side and is logged as a `system_event`. | `execution.set_execution_mode` |
| S14 | The Emergency Exit phrase is validated server-side. | `kill_switch.emergency_exit_confirm` L72 |
| S15 | `closed_trades` has a unique `sell_ticket_id`, and `record_closed_trade` is idempotent. | `db.py` L340, `journal.py` L53 |
| S16 | Executed exits do not add phantom open risk. | `risk_engine._open_risk_sum` |
| S17 | All SQL is parameterised. The only f-string SQL interpolates fixed column names from Pydantic models or constants. | throughout |
| S18 | Historical-data calls go through a single consumer, rate-limited to Kite's 3 req/s. | `data_jobs.py` |
| S19 | Same-day IST cutoffs are computed consistently against UTC `datetime('now')` timestamps. | `risk_engine._same_day_ist_cutoff_utc` |
| S20 | All six entry risk checks are always evaluated and persisted, so rejections are fully explainable. | `risk_engine.run_checks` |
| S21 | Strategy parameters are validated per strategy (Pydantic) and versioned; stored parameters are back-filled with module defaults. | `models.py`, `strategy_engine.params_with_defaults` |
| S22 | The test suite runs against a throwaway DB and a faithful `FakeKite`, and already covers several failure paths (see Testing Assessment). | `tests/` |

The docstrings explain *why* the code does what it does, which made this audit much faster. That is worth preserving.

---

# Critical Findings

Severity:
- **P0** — must fix before any live trading.
- **P1** — fix before meaningful capital.
- **P2** — engineering or robustness improvement.
- **P3** — optional.

"Proven" means the defect follows deterministically from the code as written. "Risk" means it depends on timing or on broker behaviour not observable here.

## P0 findings

### P0-1 — Ticket confirmation is not atomic; the same ticket can be submitted twice
- **Component:** Execution / idempotency
- **Files and functions:** `backend/routes/tickets.py` — `confirm_and_execute_ticket` (L430-512), `confirm_ticket` (L515-523)
- **Problem:** The function reads the ticket and checks `status in ('draft','pending_confirmation')` (L437-443). It then runs an *unconditional* `UPDATE trade_tickets SET status='confirmed'` (L445), commits, and calls `execution.execute_ticket` → `kite.place_order`. Route handlers run concurrently in the threadpool, and nothing makes check-and-set atomic:
  - no `WHERE status='pending_confirmation'` guard or rowcount check;
  - no idempotency key;
  - no Kite order `tag`.
- **Failure scenario:** The user has the Approvals inbox and the Signal Detail view (or two browser tabs) open. They approve the same signal from both within the ~1 s a Kite round trip takes. Both requests read `pending_confirmation`, both write `confirmed`, both place orders: **two BUY orders and two GTTs**, and the second response overwrites the first order id on the ticket. The first order then becomes invisible to the order monitor. The same thing happens with a client retry after a slow response.
- **Impact:** Double position size. One order is untracked, and its GTT is orphaned.
- **Recommended correction:**
  - Replace L445 with `UPDATE … SET status='confirmed', mode=? WHERE id=? AND status='pending_confirmation'`; if `rowcount == 0`, return 409.
  - Pass `tag=f"kt{ticket_id}"` to `place_order`; Kite tags allow up to 20 characters. This lets any ambiguous outcome be looked up (see P0-2).
- **Status:** Proven by code reading; not reproduced.
- **Regression risk:** Low. It is a single-statement change, and the existing tests cover the confirm path.

### P0-2 — An unknown order outcome after a network timeout strands the ticket
- **Component:** Execution
- **Files and functions:** `backend/execution.py` — `_execute_live` L161-192, `_execute_exit` L288-316; `.venv/.../kiteconnect/connect.py:_request` L904-916 (kiteconnect 5.0.1)
- **Problem:**
  - kiteconnect's `_request` re-raises raw `requests` exceptions (`except Exception as e: raise e`, L915-916). A `ReadTimeout` (7 s default) or `ConnectionError` is therefore **not** a `NetworkException`, and neither is caught in `_execute_live` or `_execute_exit`. `DataException` (unparseable response) is not caught either.
  - The exception escapes `confirm_and_execute_ticket` after the ticket was already committed as `confirmed` (L447), so the user gets an HTTP 500.
  - **No component ever looks at a `confirmed` ticket again:**
    - `order_monitor.pending_tickets` needs `status='executed' AND kite_order_id IS NOT NULL`;
    - reconciliation needs `executed`;
    - the ticket cannot be re-confirmed, because it is outside `_OPEN_STATUSES`.
- **Failure scenario:** At 09:15:02, with the exchange busy, a BUY LIMIT reaches Kite and is accepted, but the response times out at 7 s.
  - The ticket shows "confirmed"; the UI toasts "Approval failed".
  - The real order fills, with **no protective GTT** (step 2 never ran), no fill tracking and no reconciliation.
  - The user, told it "failed", creates a new ticket and buys again.
  - The same applies to `_execute_exit`: the exit may have executed, but the original GTT is not deleted, and the orphaned GTT later triggers a SELL.
- **Impact:** An unprotected, untracked real position, plus a likely duplicate.
- **Recommended correction:**
  - Wrap every broker call in `_execute_live` and `_execute_exit` with a final `except Exception` branch that returns an `outcome_unknown` result (still `success=False`).
  - Persist `status='confirmed'` plus a new `submission_state='unknown'` column, or reuse `entry_fill_status='unknown'`.
  - Tag orders (P0-1). Add a step to `order_monitor` that, for `confirmed` or `unknown` tickets older than 30 s, looks up `kite.orders()` by `tag`: adopt the order id if found; mark `failed` if absent after N minutes.
  - Show an explicit **UNKNOWN — do not resubmit** badge.
- **Status:** Proven by code reading (exception types verified in the installed SDK).
- **Regression risk:** Low to medium. New state handling is needed in the monitor.

### P0-3 — Global execution mode is applied to existing positions; DRY RUN "exits" silently close LIVE positions in the app
- **Component:** Execution mode / kill switch / exits / breakeven
- **Files and functions:**
  - `backend/routes/tickets.py:confirm_and_execute_ticket` L444-445, L479-506
  - `backend/execution.py:execute_ticket` L102-104
  - `backend/routes/kill_switch.py:emergency_exit_confirm` L81-114
  - `backend/strategy_engine.py:resolve_stop_adjustment` L614-654
  - `backend/risk_engine.list_open_positions` L225-245
- **Problem:** A ticket's mode is stamped at confirm time from the *global* `execution_settings.mode`. Exit tickets close positions from `list_open_positions`, and those are **always `mode='live'`**. With the global mode on `dry_run`, an exit ticket for a live position is:
  - simulated (`_simulate`);
  - marked `executed`;
  - then `closed_at` is stamped on the **live** original (L492-495).

  `journal.record_closed_trade` then raises the mode-mismatch `ValueError`, which is caught and logged (L503-506). After that:
  - the trailing engine ignores the position (`closed_at IS NULL` filter);
  - reconciliation's GTT and position checks ignore it;
  - the kill switch no longer lists it.

  `resolve_stop_adjustment` has the same pattern. In dry-run mode it updates `current_stop_loss` in the DB but never calls `modify_gtt`, so the UI shows a breakeven stop the broker does not have.
- **Failure scenario:** The user trades live for a week, then switches the toggle to DRY RUN to "test something". A market scare follows; they press **Emergency Exit All** (the dialog even states it "respects the current Dry Run mode") and type EXIT ALL.
  - Every live position is shown as exited, but **nothing was sold**.
  - All monitoring of those positions stops, while the GTTs still exist at the broker with stale quantities and prices.
- **Impact:** The user believes they are flat while fully exposed; protective logic is detached.
- **Recommended correction:**
  - A closing ticket must inherit `mode` from the ticket it closes, not from the global setting: set it at ticket creation and refuse to confirm on mismatch.
  - `resolve_stop_adjustment` must act according to `ticket['mode']`.
  - The global mode should only decide how *new entries* execute.
- **Status:** Proven by code reading.
- **Regression risk:** Low. A few-line change in three places, easy to test with the existing `FakeKite`.

### P0-4 — Confirmation does not re-validate: draft tickets bypass risk checks, and stale exits send second SELLs
- **Component:** Execution gate / risk
- **Files and functions:**
  - `backend/routes/tickets.py`: `_OPEN_STATUSES` L23, `confirm_and_execute_ticket` L437-450
  - `backend/risk_engine.py`: `run_risk_checks_and_transition` L444-456, `build_portfolio_context` L35-43
  - `backend/routes/kill_switch.py` L86-110
  - `backend/strategy_engine.py:_has_pending_exit` L404-412
- **Problem:** There are four related gaps.
  1. **`draft` is confirmable.** A ticket stays `draft` if its risk check raises anything other than `RiskCheckUnavailableError`. `build_portfolio_context` only converts `KiteException`, so a raw `requests` timeout from `kite.holdings()` leaves it `draft` with `risk_check_results NULL`. The UI does not offer Approve on drafts, but the API accepts it (and see P0-8).
  2. **Exit tickets are not re-checked at confirm.** If the position has since been closed by a GTT fire, another exit ticket, or Emergency Exit, confirming the stale exit places another real SELL.
     - Duplicate exit tickets for one position are possible: `run_exit_scan` guards with `_has_pending_exit`, but Emergency Exit and alert-action sells do not.
     - For CNC, the broker rejects a sell beyond holdings, **unless the user also holds that stock outside the app, in which case those shares are sold.**
  3. **Entry risk results are not re-evaluated.** Strategy tickets are created around 16:00 and approved the next morning or later. Several tickets that each passed alone can together exceed `max_open_positions` or `max_total_portfolio_risk_pct`, because only *executed* tickets count.
  4. The exit-validity check (`_check_exit_or_reduction`) does not compare `quantity` with the referenced position's quantity, or its `mode`.
- **Failure scenario:** The strategy creates an exit ticket for INFY (pending). Overnight the INFY stop GTT fires and the trailing engine closes the position. At 09:20 the user approves the stale exit from the Approvals card → a real MARKET SELL for the full quantity is sent.
- **Impact:** Unintended sells (including of shares held outside the app); risk limits exceeded.
- **Recommended correction:** In the conditional update from P0-1, allow only `pending_confirmation`. Immediately before `execute_ticket`:
  - for entries, re-run `risk_engine.run_checks` (one `holdings()` and `margins()` call) and refuse on failure;
  - for exits, re-query the referenced position (`status='executed'`, `closed_at IS NULL`, same `mode`, no other `executed` exit) and refuse otherwise.

  When an exit executes, auto-reject sibling pending exits for the same `closes_ticket_id`.
- **Status:** Proven by code reading.
- **Regression risk:** Low to medium. Re-running checks at confirm adds one Kite round trip.

### P0-5 — Positions protected by an OCO GTT (no trailing) are never recorded as closed
- **Component:** Position lifecycle
- **Files and functions:** `backend/trailing_engine.py:_tick_once` L48-56; `backend/reconciliation.py:_check_gtt_state` L104-135
- **Problem:** Detecting that "the protective GTT fired, so the position is closed" lives only in the trailing engine, which queries `trailing_enabled = 1`. The only other code that sets `closed_at` is the explicit exit in `routes/tickets.py` L493 (verified by searching for `closed_at =`). Affected tickets:
  - manual tickets without trailing (the form default);
  - every `rsi_mean_reversion` ticket (`trailing_distance=None`).

  Their OCO GTTs can stop out or take profit at the broker while the app keeps the position open indefinitely. Reconciliation logs "GTT not active", with detail text claiming "trailing_engine should detect this on its own next tick", which is untrue for these tickets.
- **Failure scenario:** A manual BUY of 50 RELIANCE, with no trailing, hits its OCO stop. The app still counts it:
  - it takes an open-position slot and its risk counts in the total-risk check;
  - it appears in the Emergency Exit preview, and EXIT ALL sends a real MARKET SELL of 50 RELIANCE (rejected, or it sells outside-app holdings);
  - its P&L is never journaled.
- **Impact:** A phantom open position with false risk figures and possible unintended sells.
- **Recommended correction:** Split the GTT-closure watcher out of the trailing ratchet. Run the closure detection in `_process_ticket` L84-98 for **every** live, executed, unclosed ticket with a `kite_gtt_id`; keep the ratchet for trailing tickets only. Combine this with P0-6.
- **Status:** Proven by code reading.
- **Regression risk:** Low. It widens a query and moves one branch.

### P0-6 — "GTT triggered or missing" is treated as "position closed" without confirmation
- **Component:** Position lifecycle / broker reconciliation
- **Files and functions:** `backend/trailing_engine.py:_process_ticket` L84-98, `_find_matching_fill` L114-138, `_handle_gtt_closure` L141-214
- **Problem:** When a GTT is `triggered`, or absent from `get_gtts()` (`None`), the engine synthesises an executed SELL and stamps `closed_at` **even when no matching fill is found**. It then prices the exit at the current LTP and labels it "estimated". But:
  - A triggered GTT only means Kite *placed* a LIMIT order at `stop × 0.995` (`execution.buffered_stop_price`). On a gap-down open below that limit, the order sits unfilled. The position is still held and now has no protection at all.
  - A GTT deleted by the user in Kite web or the mobile app, or expired after a year, is indistinguishable from a trigger.
  - `_find_matching_fill` only matches orders with `filled_quantity == ticket.quantity` in today's order book. Partial fills, or fills whose order dropped off the book, fall through to "estimated".
- **Failure scenario:** Bad results are announced overnight; the stock opens 8 % below the stop. The GTT triggers a SELL LIMIT at stop × 0.995, which does not fill. The app records "closed at LTP (estimated)"; the user sees no open position and no alert, and the stock keeps falling.
- **Impact:** A silent, unprotected position, which is exactly the scenario the GTT exists to prevent.
- **Recommended correction:** Before synthesising a closure, require one of:
  - a matching COMPLETE exit order (allowing partials);
  - the broker's holdings plus positions quantity for the symbol having dropped by at least the ticket quantity (reuse `risk_engine.get_held_quantity`).

  Otherwise set a visible state such as `exit_state='gtt_triggered_unfilled'`, log a reconciliation row, and broadcast `trailing_error` (urgent), but **do not** close.
- **Status:** Proven logic. The gap-down non-fill is standard LIMIT-order behaviour, not observed here.
- **Regression risk:** Medium. Closure timing changes; the existing tests `test_trailing_triggered_gtt_is_detected_as_closed` and `test_trailing_detects_vanished_gtt_and_records_close` need updating.

### P0-7 — Exit orders are treated as filled on placement: protection removed and position closed before the fill
- **Component:** Exit lifecycle
- **Files and functions:** `backend/execution.py:_execute_exit` L318-335; `backend/routes/tickets.py:confirm_and_execute_ticket` L479-506; `backend/journal.py:record_closed_trade`
- **Problem:** Right after `place_order` returns an id (not a fill), `_execute_exit` **deletes the original protective GTT**. `confirm_and_execute_ticket` then stamps `closed_at` on the original and writes a `closed_trades` row at the reference price. The same "order id returned = filled" assumption that S1 correctly avoids for entries is made here for exits. Exits that do not fill immediately:
  - alert-action sells, which allow LIMIT (`models.AlertActionSellRequest`);
  - any exit approved after hours, which goes out as an AMO and waits until 09:15 the next day;
  - MARKET orders during a circuit or freeze.

  Until the order monitor sees REJECTED or CANCELLED (S3), such a position is unprotected and invisible to risk checks, the kill switch, reconciliation and trailing.
- **Failure scenario:** At 20:00 the user approves a strategy exit. The AMO is queued, the stop GTT is deleted immediately, and the position shows closed. The next morning the stock gaps down and the AMO MARKET sell fills at the gapped price. There was a 13-hour window with no stop, although the user believed they were flat. With a LIMIT exit that never fills and is cancelled at the end of the day, the position is re-opened only after the fact, unprotected (the S3 message says so).
- **Impact:** A protection gap and misleading "closed" state; journal rows written before any fill.
- **Recommended correction:** Introduce an **exiting** state: exit `executed` with `entry_fill_status='pending'`.
  - Do not stamp `closed_at` or write the journal until `order_monitor._resolve_exit` sees COMPLETE. The monitor already re-prices the journal; move the insert there.
  - Handle the GTT in one of two ways:
    - **(a)** Keep it until the exit fills, then delete it. A GTT firing in the meantime would be rejected for CNC because the shares are gone; the MIS/NRML oversell risk is avoided by P1-11.
    - **(b)** For MARKET exits during market hours only, keep the current cancel-on-place behaviour and use (a) otherwise.
  - Show EXITING in the UI.
- **Status:** Proven by code reading.
- **Regression risk:** Medium to high. It touches the journal, the monitor, the kill switch and the UI. Do it **after** PK-0 tests exist.

### P0-8 — Cross-site request forgery can approve trades and delete protective GTTs
- **Component:** Security / API
- **Files and functions:** `backend/main.py` (no middleware), plus every route below
- **Problem:** The API has no caller authentication and no Origin, CSRF-token or custom-header check. The following state-changing endpoints take **no request body**, so a browser sends them cross-origin as "simple requests" (`fetch(url, {method:'POST', mode:'no-cors'})` or an auto-submitting form) with no CORS preflight:
  - `POST /api/tickets/{id}/confirm` — places a real order in live mode
  - `POST /api/tickets/{id}/revert-rejection`, `POST /api/tickets/{id}/start_trailing`
  - `POST /api/tickets/cancel-all-pending`
  - `POST /api/gtts/{gtt_id}/cancel` — removes protection
  - `POST /api/orders/{order_id}/cancel`
  - `POST /api/strategies/stop-adjustments/{id}/confirm` — moves the live stop
  - `POST /api/strategies/scan-now`, `POST /api/automation/run-now`, `POST /api/instruments/refresh`, `POST /api/logout`

  CORS only stops the attacker *reading* the response; it does not stop the request. Ticket ids are small sequential integers. JSON-body endpoints are effectively protected, because FastAPI does not parse a `text/plain` body as JSON.
- **Failure scenario:** While the terminal runs in live mode on `127.0.0.1:8000`, the user opens a malicious or compromised page, including an ad frame. Its script loops `POST http://127.0.0.1:8000/api/tickets/1..500/confirm`, and every `pending_confirmation` ticket is executed **without human approval**. That defeats the app's central safety property.
- **Impact:** Real orders placed without consent.
- **Recommended correction:**
  - Add a small middleware that rejects any non-GET request unless it carries a custom header set by `app.js` (e.g. `X-Kite-Terminal: 1`, which forces a preflight cross-origin) **and** has an `Origin`/`Referer` matching the server's own host.
  - Apply the same `Origin` check to the `/ws/ticks` handshake.
  - Before any non-localhost deployment, add real authentication (see Security Assessment).
- **Status:** Proven by code reading; the exploit was not attempted.
- **Regression risk:** Low. `app.js` already routes mutations through a single `fetchJsonMutate` helper (L70-126), so the header can be added in one place.

## P1 findings

### P1-1 — Manual order cancel marks the linked ticket cancelled whatever its role
- **Files and functions:** `backend/routes/orders.py:cancel_order` L97-121
- **Problem:** After `kite.cancel_order` succeeds, the *linked ticket* is set to `status='cancelled'` unconditionally. This is wrong in three cases:
  - **(a)** For an **exit** ticket, the original stays `closed_at`-stamped, with a `closed_trades` row at the reference price. The position is actually still held, and nothing re-opens it, because the order monitor ignores `cancelled` tickets.
  - **(b)** For a partially filled entry, shares are held but the ticket reads "cancelled", with a full-quantity GTT.
  - **(c)** A cancel request that Kite accepts is not a cancellation; the order may fill in the same second.
- **Failure scenario:** The user cancels a slow LIMIT exit from Orders & Trades to re-price it. The app shows the position as closed, with no stop, forever.
- **Correction:** Do not change ticket status here. Let `order_monitor` resolve the order to its terminal state, which already handles CANCELLED correctly for entries (S2) and exits (S3). At most, add a `ticket_events` note.
- **Status:** Proven.
- **Regression risk:** Low.

### P1-2 — Emergency Exit All is not fault-isolated per position
- **Files and functions:** `backend/routes/kill_switch.py:emergency_exit_confirm` L86-114, `_create_emergency_exit_ticket`
- **Problem:**
  - There is no `try/except` around `confirm_and_execute_ticket`, so one unexpected exception (P0-2, or an HTTPException) aborts the loop. The remaining positions are **not exited** and the `emergency_exit_all` system event is never written.
  - Existing pending exit tickets are ignored (duplicate exits; see P0-4).
  - The sell quantity is the ticket quantity even after a partial fill (P1-3).
- **Failure scenario:** In a panic with 6 positions, position 2's `place_order` times out. The request returns 500 and positions 3 to 6 remain open while the user believes the button worked.
- **Correction:** Isolate each position with `try/except Exception`, collect results, always log the system event, and return a per-position outcome table that the UI displays.
- **Status:** Proven.
- **Regression risk:** Low.

### P1-3 — Partial fills are recorded but not modelled
- **Files and functions:** `backend/order_monitor.py:_resolve_entry` L153-168, `_resolve_exit` L207-218; `trade_tickets.quantity`
- **Problem:** On a partially filled entry that ends CANCELLED:
  - `entry_fill_status='partial'` is set and an urgent message says "reduce the GTT to N in Kite";
  - `quantity` stays at the requested amount, so risk, exposure, Emergency Exit quantity, trailing `modify_gtt` quantity and journal quantity all use the unfilled number;
  - no `filled_quantity` column exists.

  A partial exit leaves the original closed, although shares remain.
- **Failure scenario:** 100 shares are requested and 40 filled. Trailing later re-submits the GTT for 100. When it fires, 40 sell (CNC); the other 60 are rejected, or sell outside-app holdings. The journal books 100 shares of P&L.
- **Correction:** Add `filled_quantity`. Use it for everything position-related after the fill. Have the monitor call `modify_gtt` to shrink the GTT, or at least block trailing and journal on `partial` until reviewed.
- **Status:** Proven.
- **Regression risk:** Medium.

### P1-4 — The sector-exposure check is effectively disabled for new entries
- **Files and functions:** `backend/sector_mapping.py:seed_from_holdings` L54-72, `get_sector` L75-81; `backend/risk_engine.py:_check_sector_exposure` L293-306
- **Problem:** `get_sector` reads only the `sector_mapping` table, which is seeded **only from current holdings**. The static `SECTOR_LOOKUP` (about 65 symbols) is never applied to watchlist symbols. An unmapped symbol is marked "skipped" and counts as **passed**. The live DB has **1 mapped row, and 1 of 51 watchlist symbols mapped**. Holdings in unmapped symbols also do not count toward any sector total.
- **Failure scenario:** The breakout scanner proposes TCS, INFY and HCLTECH on the same strong IT day. Each passes a 20 % sector limit trivially because no IT exposure is known, and the portfolio ends up 30 % in IT.
- **Correction:**
  - Seed `sector_mapping` from `SECTOR_LOOKUP` for all watchlist and holding symbols at startup and on watchlist add.
  - Make an unknown sector *fail closed* (require a mapping before approval), or treat all unknown symbols as one "Unmapped" bucket subject to the limit.
- **Status:** Proven (code and DB contents).
- **Regression risk:** Low.

### P1-5 — Gaps in the exposure and portfolio-value basis
- **Files and functions:** `backend/risk_engine.py:build_portfolio_context` L48-63, `_symbol_exposure` L189-213
- **Problem:**
  - **(a)** `holdings_value` and `cost_value` use `h["quantity"]` only and ignore `t1_quantity`. A stock bought *yesterday* sits in `t1_quantity`, and `_symbol_exposure` only adds *same-day* tickets. For a whole trading day, yesterday's buy counts as **zero** exposure, even though `reconciliation` and `get_held_quantity` already know about `t1_quantity`.
  - **(b)** `kite.positions()` is not consulted, so same-day buys made outside the app are invisible.
  - **(c)** `portfolio_value = holdings MTM + margins.equity.net`. If holdings are pledged, the collateral value is counted twice.
  - **(d)** Exposure uses cost basis while `portfolio_value` uses MTM. This is documented and deliberate, but it means a winning position looks smaller than it is.
- **Failure scenario:** Day 1: buy 10 % in X. Day 2: a new X signal passes the 10 % single-stock check again, because yesterday's shares are in `t1_quantity`. Exposure becomes 20 %.
- **Correction:** Include `t1_quantity` in `quantity`, and add the `positions()['net']` quantity for CNC. State the exposure basis explicitly in the UI.
- **Status:** (a) proven; (b) proven; (c) risk, depending on the account's pledges.
- **Regression risk:** Low.

### P1-6 — Account-level safety controls are missing
Each control is listed with the concrete failure it prevents.

| Control | Failure scenario it prevents | Suggested minimal form |
|---|---|---|
| **Trading lock** (true kill switch) | Today "Cancel All Pending" rejects existing tickets, but the next autopilot run or a manual ticket can be approved a minute later. During an incident, there is no way to say "no new orders at all". | `execution_settings.trading_locked` checked in the confirm gate; a toggle in the banner; logged. |
| **Daily loss limit** | Three stop-outs on a gap day, then the user keeps approving signals while tilted. | Block new entries when today's realised P&L from `closed_trades` plus open MTM is below −X % of `portfolio_value`. |
| **Max orders per day / max new entries per day** | A bug or CSRF (P0-8) loops confirms; the autopilot duplicates signals. | A count of `ticket_events` `executed` today (live), checked at confirm. |
| **Absolute max notional per order** | `portfolio_value` is derived from the broker; a margins API glitch, or pledged collateral double counting (P1-5c), inflates it, and every percentage limit scales with it. | A fixed ₹ cap in `risk_settings`. |
| **Price-deviation check** | A signal priced off yesterday's close is approved at 14:00 after a 6 % move. A MARKET manual ticket, or a LIMIT far from market, has no sanity check. | At confirm, fetch LTP and refuse if the LIMIT (or MARKET reference) deviates more than N % from LTP, or if LTP is already beyond the stop. |
| **Stale-signal guard at confirm** | Expiry only runs *when the next scan runs* (`expire_stale_signals`). If the autopilot fails for a day, yesterday's signals stay approvable. | Refuse an entry when `signal_date < last_complete_session_date()`. |
| **Market-hours / AMO opt-in** | Any approval outside 09:15-15:30 silently becomes an AMO (`USE_AMO_WHEN_MARKET_CLOSED=True`), and holidays are not known (`market.is_trading_day` covers weekends only). | Require an explicit "send as AMO" confirmation; add an NSE holiday list. |
| **API-error circuit breaker** | Repeated Kite errors during a broker incident; the user keeps clicking. | After N consecutive broker errors in M minutes, auto-set the trading lock. |

Duplicate-order protection itself is covered by P0-1, P0-2 and P0-4.

### P1-7 — The confirmation is not bound to the mode the user saw
- **Files and functions:** `backend/routes/tickets.py:confirm_ticket`; `backend/execution.py:set_execution_mode`; `frontend/app.js` (`state.executionMode`, `approveTicket` L2679, `executionNote` L2606)
- **Problem:** The UI decides "Approve (dry run)" versus "Place order (LIVE)" from its locally cached `state.executionMode`. The confirm request carries no expected mode, and mode changes are not broadcast over the WebSocket.
- **Failure scenario:** Tab A still says DRY RUN; tab B switched to LIVE an hour ago. The user approves in tab A thinking it is a simulation, and a real order is placed.
- **Correction:** The client sends `expected_mode`; the server returns 409 on mismatch. Broadcast `{"type":"mode_changed"}` from `set_execution_mode`.
- **Status:** Proven.
- **Regression risk:** Low.

### P1-8 — Race between the GTT-closure detector and an explicit exit
- **Files and functions:** `backend/trailing_engine.py:_handle_gtt_closure` L181-208; `backend/routes/tickets.py` L492-506
- **Problem:** The trailing tick reads its ticket list, then makes network calls. If an explicit exit is confirmed in that window, it deletes the GTT and stamps `closed_at`. The tick then sees the GTT missing and synthesises a *second* exit ticket and `closed_trades` row; the unique index is on `sell_ticket_id`, so it does not prevent this. The `closed_at` update at L205 has no `AND closed_at IS NULL` guard.
- **Correction:** `UPDATE … SET closed_at=… WHERE id=? AND closed_at IS NULL`, check the rowcount *before* inserting the synthetic ticket, and do both in one transaction.
- **Status:** Risk (timing-dependent), not proven.
- **Regression risk:** Low.

### P1-9 — The trailing engine blocks the event loop, and one failure aborts every ticket
- **Files and functions:** `backend/trailing_engine.py:run_trailing_loop` L24-35, `_tick_once` L38-75
- **Problem:**
  - `_tick_once` is `async` but calls `kite.get_gtts()`, `kite.ltp()`, `kite.orders()` and `kite.modify_gtt()` synchronously on the event loop. Each call can block for up to 7 s, freezing WebSocket delivery and async routes. The order monitor and reconciliation correctly use `asyncio.to_thread`.
  - `_try_ratchet` catches only `OrderException`, `InputException` and `GeneralException`. A `NetworkException`, `TokenException` or raw `requests` error aborts the whole tick, so later tickets are neither checked nor trailed, and pending DB updates are not committed.
- **Correction:** `await asyncio.to_thread(_tick_sync)` plus a per-ticket `try/except Exception` with logging.
- **Status:** Proven.
- **Regression risk:** Low.

### P1-10 — Orphan and oversized GTTs are not reconciled
- **Files and functions:** `backend/reconciliation.py` (the checks are only app→broker)
- **Problem:** Reconciliation checks that each open ticket's GTT is active. It never checks the reverse:
  - that each **active broker GTT** belongs to an open ticket;
  - that the GTT quantity is no more than the held quantity.

  Orphans are created by failed `delete_gtt` calls (warning text only: `execution.py` L330-335, `order_monitor.py` L176-177), by P0-2, P1-1 and P1-3, and by positions closed manually in Kite.
- **Failure scenario:** A position is closed manually in the Kite app; its stop GTT stays active. Months later the user buys the same stock long-term outside the app, the old GTT triggers, and it sells those shares.
- **Correction:** Add an "orphan GTT" check (an active GTT whose id is not referenced by an open live ticket, or whose quantity exceeds the held quantity), shown with a one-click cancel.
- **Status:** Proven gap.
- **Regression risk:** Low.

### P1-11 — Manual tickets allow products and directions the lifecycle does not model
- **Files and functions:** `backend/models.py:TicketCreateRequest` (`product_type` CNC/MIS/NRML, `transaction_type` BUY/SELL); `backend/execution.py:is_closing_sell` L86-99, `_execute_live`
- **Problem:**
  - **A manual SELL** (source `manual`, no `closes_ticket_id`) is treated as *opening a short*: all six entry checks run, then a SELL order and a protective **BUY** GTT are placed. For CNC:
    - it either fails at the broker; or
    - it sells holdings from outside the app and arms a BUY-back GTT.

    Journal, positions and exits are long-only (`list_open_positions` has `transaction_type='BUY'`).
  - **MIS:** auto square-off at about 15:20 is not modelled (the app would keep the ticket open), and GTT support for MIS is not assumed anywhere.
- **Correction:** Until short and intraday lifecycles are modelled, restrict manual entries to `BUY` + `CNC` server-side, and route sells through the exit or alert-action paths.
- **Status:** Proven that the paths exist; the broker-side outcome is a risk.
- **Regression risk:** Low.

### P1-12 — Secrets and exposure hardening (details under Security Assessment)
- `sessions.access_token` is stored **in plaintext** (`db.py` L24; `kite_client.exchange_request_token` L52-63) next to the Fernet-encrypted `accounts.access_token_enc`, and `get_kite()` reads the plaintext copy (L106). The encryption therefore adds nothing.
- The Fernet key is stored in the same `.env` as `KITE_API_SECRET` (`crypto.ensure_encryption_key`), so anyone who can read `.env` and the DB can decrypt everything.
- There is no caller authentication at all (see P0-8).
- Third-party scripts load from CDNs **without Subresource Integrity** (`index.html` L1372, L1380). A compromised CDN file runs with full same-origin power, including `/confirm`.
- `docs/USER_GUIDE.md` L38 recommends `uvicorn --reload`. A file save mid-execution restarts the process between `place_order` and `place_gtt`.

**Correction:**
- Drop the plaintext column: read from `access_token_enc`, or store only a hash of the plaintext for lookup.
- Move the key to the OS keyring or an environment variable outside the repository directory.
- Add SRI hashes, or vendor the two libraries locally.
- Remove `--reload` from the operating instructions.

**Status:** Proven. **Regression risk:** Low.

### P1-13 — Dry run cannot rehearse the position lifecycle
- **Files and functions:** `backend/risk_engine.list_open_positions` (`mode='live'`); `trailing_engine` / `order_monitor` / `reconciliation` (live only); `strategy_engine.run_exit_scan`
- **Problem:** Dry-run entries are "filled" instantly and then ignored: no exit scan, no trailing, no GTT simulation, no journal. The DB has 2 executed dry-run tickets that will never close, and `closed_trades` is empty. The dry-run journal cannot contain anything except rows from mode-mismatch bugs. The exit, trailing, GTT-closure and reconciliation paths will therefore run **for the first time with real money**.
- **Correction:** A "paper broker" mode that routes live code paths to a simulated broker. `tests/fake_kite.FakeKite` is already about 80 % of one: it has an order book, a GTT book and fills. Fill it from real LTP and trigger GTTs on ticks. This is the single most valuable pre-live investment after the P0 fixes.
- **Status:** Proven (design gap).
- **Regression risk:** Low for existing paths (additive).

### P1-14 — In-app backtest results are optimistic and not comparable with live execution
This is detailed in the Backtester part of Strategy Verification. It is P1 because these numbers are what would be used to decide how much capital to commit. It is not an operational-safety item.

## P2 findings (engineering and robustness)

| ID | Component | Finding | File(s) | Correction |
|---|---|---|---|---|
| P2-1 | DB | No schema version table. About 20 introspection migrations run on every start, including table rebuilds with `PRAGMA foreign_keys=OFF`. A half-applied rebuild after a crash is not detected. | `db.init_db`, `_ensure_*` | Add a `schema_version` table and run migrations once, inside a transaction, after an automatic `.bak` copy (the memory convention). |
| P2-2 | DB | Missing indexes: `trade_tickets(closes_ticket_id)`, `(kite_order_id)`, `(kite_gtt_id)`, `(account_id, symbol, status)`. The correlated `NOT IN (SELECT closes_ticket_id …)` in `_OPEN_POSITION_WHERE` is unindexed. | `db.py`, `risk_engine.py` L222-229 | Add the indexes; negligible today, but free. |
| P2-3 | Data model | `trade_tickets` conflates intent, broker order, position, protection and exit. That is why P0-7, P1-1 and P1-3 exist. | `db.py` | Do **not** redesign. Add one `broker_orders` table (ticket_id, role = entry/exit/gtt_leg, order_id or gtt_id, tag, status, filled_qty, avg_price, timestamps), written by execution and the monitor. Tickets keep their summary columns. |
| P2-4 | Audit trail | `closed_trades` rows are deleted when an exit is rejected (`order_monitor` L238). Draft deletion removes `ticket_events`. No trigger protects executed tickets from mutation. | `order_monitor.py`, `routes/tickets.delete_ticket` | Mark the row void instead of deleting it; add `BEFORE UPDATE` triggers restricting changes to executed tickets' order columns. |
| P2-5 | Market data | Corporate actions: only the last 7 days are re-downloaded (`ohlc.refresh_latest` L150-153). If Kite back-adjusts history after a split or bonus, stored older candles stay unadjusted. The resulting discontinuity corrupts SMA-200, ATR (and so stop width) and Donchian for months. | `ohlc.py` | Detect a close-to-open gap of more than 25 % against Kite's current history for a sample date, then trigger a full re-backfill; or re-download everything weekly. |
| P2-6 | Market clock | `is_trading_day` checks weekends only. On an NSE holiday: approvals become AMOs, `last_complete_session_date` points at the holiday, and signal age reads one day more than reality. | `market.py` | Add a static yearly NSE holiday list. |
| P2-7 | WebSocket | `/ws/ticks` does no Origin check and uses an unbounded `asyncio.Queue` per client. A backgrounded tab accumulates ticks without limit. | `routes/ws.py`, `ticker.py` | Origin check; `maxsize` with drop-oldest. |
| P2-8 | Frontend perf | `renderAll()` runs on every tick message (`app.js` L2513-2521), re-rendering all tables several times a second during market hours. | `app.js` | Throttle to `requestAnimationFrame` or 500 ms; update only cells for the ticked instrument. |
| P2-9 | Logging | stdout only, no rotation, no structured record of each broker request and response (params, order id, latency, exception class). After an incident, `ticket_events` free text is the only trail. | `main.py` | Add a `RotatingFileHandler` and one `broker_audit` logger line per Kite mutating call. |
| P2-10 | Error surface | `str(e)` of Kite exceptions is returned to the client (`orders._call`, `portfolio._call`, `risk_engine` L43). Acceptable locally; on a VM, messages may reveal internals. | routes | Map to codes; log the detail server-side. |
| P2-11 | Autopilot | `data_freshness` treats a run as "skipped" when *no* symbol is fresh, and never retries, even if Kite publishes late. | `automation.py` L148-151 | Retry "skipped" once after 30 min. |
| P2-12 | Order monitor | `_mark_unconfirmed` says "The position is treated as open" for **exit** tickets too, where the original is actually treated as closed. | `order_monitor.py` L124-132 | Branch the message and set a flag on exits. |
| P2-13 | Tests | No test runs the migrations against a copy of the real DB. | `tests/` | Add a fixture that copies the latest `.pre_*_bak` and runs `init_db()`. |

## P3 findings (optional)

| ID | Finding | Where |
|---|---|---|
| P3-1 | The trend_pullback "touch" test is `low <= EMA × (1 + band)` with **no lower bound**, so a bar that crashes 8 % through the EMA also counts as a "pullback to within 1.5 %". Both the docs and the UI text say "within". Spec/implementation mismatch (POTENTIAL IMPROVEMENT). | `strategies/trend_pullback.py` L87-91 |
| P3-2 | "Scan Now" between 15:30 and ~15:45 IST stores a candle whose close may still be the pre-closing-session LTP. It is corrected by the next refresh, but a signal may already exist. | `market.last_complete_session_date` |
| P3-3 | Journal `entry_date` and `exit_date` come from UTC `executed_at[:10]`, so actions between 00:00 and 05:30 IST land on the previous date. | `journal.record_closed_trade` L90-91 |
| P3-4 | Some `innerHTML` templates interpolate Kite-sourced fields without `esc()` (`tradingsymbol` in the trades table, `order_type`, `order_id` in data attributes). Low risk, since the data comes from the broker. | `app.js` L2104-2111, L2156-2160 |
| P3-5 | `is_session_fresh` only checks the login calendar date; a token revoked mid-day (logout on another device) is noticed only on the next API error. Handled reasonably via `TokenException`. | `kite_client.py` |

---

# Strategy Verification

Common facts, verified for all three strategies:
- Signals are evaluated on the **last completed daily bar** after the close (S8).
- Entry is a **LIMIT BUY at close × 1.001**, placed the next session (or queued as an AMO if approved after hours).
- Stops and targets derive from the signal-bar close and ATR-14.
- All levels are snapped to tick.
- Position size is `min(risk-based, capital-based)` from `risk_engine.compute_position_size`, with `portfolio_value` from the broker.
- Duplicate suppression (`strategy_engine._has_existing_strategy_signal`) covers: an open or pending signal for the same symbol and strategy, an executed-and-open position, a risk-rejected signal from today, or the same `signal_date`.
- Ranking and the `max_signals_per_day` cap happen at scan time.
- There is **no cross-strategy suppression**: momentum and pullback can both enter the same symbol on the same day, bounded only by the single-stock exposure check (DESIGN CHOICE; see P1-5 for gaps in that check).
- **Data sufficiency:** `len(df) >= trend_filter_period` and last bar ≤ 5 days stale; a `None` indicator makes the condition fail (fail closed). VERIFIED SAFE.

## momentum_breakout
| Aspect | Implementation | Verdict |
|---|---|---|
| Entry | `close > SMA-200` AND `close > max(close[t-30..t-1])` AND `volume > 3.0 × SMA-20(volume)` | Correct. |
| Donchian | Highest **close** of the prior 30 bars, current bar excluded | VERIFIED SAFE (no leakage). DESIGN CHOICE: close-based, not high-based. It fires earlier than a textbook Donchian of highs; label it in the UI. |
| Volume average | `SMA-20(volume)` **includes the signal bar** | DESIGN CHOICE. It raises the bar slightly: with a 3× spike, the average already contains the spike, so the effective threshold is about 3.4×. It is consistent between live and backtest, so it is not a bug. |
| Stop / target | Entry − 3×ATR14 / entry + 3R | Correct. |
| Trailing | Activates when LTP ≥ target_1; trail = 1.5×ATR (fixed at entry) from the **live LTP** every 45 s | **TRADING ASSUMPTION mismatch**: the backtester trails from the **daily close**, so live stops are tighter than tested. |
| Breakeven | A *proposal* at +1R (not automatic), moving the stop to entry | Correct. The proposal measures gain from **live LTP**, while the backtester has no breakeven at all, so it is untested. |
| Exits | Shared: close < EMA-50 (armed once price closed above it since entry), or held > 15 bars without close ≤ stop or close ≥ target | Correct. The exit ticket is a **next-session MARKET** order after human approval. |
| Ranking | `% above the Donchian level` (most extended first) | TRADING ASSUMPTION. It prefers the most extended bars, which are the likeliest to gap *further* up and leave the 0.1 % buffered LIMIT unfilled. Consider ranking by volume multiple instead (POTENTIAL IMPROVEMENT, not an optimisation request). |
| Exit conflict | The GTT target is never a hard exit with trailing (SINGLE GTT, stop only); thesis and time exits can pre-empt | Coherent. |

## trend_pullback
| Aspect | Implementation | Verdict |
|---|---|---|
| Entry | `close > SMA-200` AND some bar in the last 3 (today included) has `low ≤ that bar's EMA-20 × 1.015` AND `close > prev_close` AND `close > EMA-20` AND `volume > 3 × SMA-20(volume)` | Correct and timing-safe: each bar uses its *own* EMA. |
| Pullback band | No lower bound | **P3-1** spec mismatch (a breakdown counts as a "pullback"). |
| Stop / target / trailing | −3.5×ATR / +4R / 1.5×ATR | Correct. Same live-versus-backtest trailing mismatch as above. |
| Ranking | `(close − EMA)/EMA` | Fine. |
| Robustness note | DESIGN.md itself records that the out-of-sample half was notably weaker (+10.8R in-sample vs +4.4R out-of-sample on 165 trades). With a profit factor of 1.43 on so few trades per half, the edge is within noise. Treat it as unproven. | TRADING ASSUMPTION. |

## rsi_mean_reversion (disabled by default)
| Aspect | Implementation | Verdict |
|---|---|---|
| Entry | `close > SMA-200` (hard) AND `RSI14 < 35` AND (optionally) `RSI14 > prev RSI14` | Correct (Wilder RSI, S10). |
| Stop | `max(10-bar swing low, entry − 1×ATR)`, the tighter of the two; the swing window includes the signal bar | Correct per the documented intent. The stop is placed exactly at the swing low with no buffer, where stops cluster (TRADING ASSUMPTION). |
| Target | +1.5R, **no trailing**, so it gets an **OCO GTT** | **Affected by P0-5**: when its stop or target fires, the app never closes the position. |
| Exit | RSI ≥ 55 first, then the shared exits | Correct. |
| Status | DESIGN.md: no viable configuration net of costs | It should stay disabled. |

## Backtester (`backend/backtest.py`)
Reusing the exact live `evaluate_entry`, `evaluate_exit` and `compute_entry_levels` is a strength (VERIFIED SAFE). Rows before `start_date` are used only for warm-up, which is correct. The **fill model**, however, has these issues:

| # | Issue | Code | Classification | Direction of bias |
|---|---|---|---|---|
| B1 | **Entry assumed filled on the signal bar** at `close × 1.001`, unconditionally. Live, a LIMIT at that price is placed the *next* session and only fills if the next day trades at or below it. Breakouts that gap up and run are *never* filled live but are fully counted in the backtest. | `_simulate_symbol` L130-141 | **BUG** (unrealistic fill / signal-entry timing mismatch) | Optimistic, likely strongly so for `momentum_breakout` |
| B2 | **Strategy exits execute at the signal bar's close** (`return True, day["close"], …`). Live, the exit is a next-session MARKET order after human approval. | `_step_position` L85-100 | **BUG** (impossible same-close execution) | Either direction; not conservative |
| B3 | **Stops fill exactly at the stop price** whenever `low ≤ stop`, including when the bar *opens* below the stop (gap). Live, the GTT's LIMIT at stop × 0.995 fills at the open, or not at all (P0-6). | L66-67, L81-83 | **BUG** (gap risk ignored) | Optimistic |
| B4 | **Trailing stop ratcheted from the bar's close, then tested against the same bar's low.** The low happened before the close was known (intrabar ordering). Live, the ratchet uses intraday LTP every 45 s. | L76-83 | **BUG** (timing) plus a live/backtest mismatch | Mixed |
| B5 | **No transaction costs.** No brokerage (₹0 for CNC delivery at Zerodha), STT (0.1 % each side for delivery), exchange charges, SEBI fee, GST, stamp duty (0.015 % buy), DP charge per sell, or slippage. Round trip is about 0.25-0.30 % before slippage. The 0.40 % study in DESIGN.md is **not reproducible from this repository**. | whole file | **Missing** | Optimistic |
| B6 | **No portfolio constraints.** Every signal on every symbol is taken; `max_signals_per_day`, `max_open_positions`, exposure and capital limits are not applied. Live keeps at most 3 per day per strategy. | `run_backtest` L257-264 | Live/backtest mismatch | Different trade set, so stats are not comparable |
| B7 | **Force-close beyond `end_date`.** A position open at `end_date` closes at `df.iloc[-1]`, the last bar of *all* data, not the `end_date` bar. In walk-forward windows this **leaks future prices into earlier windows**. | `_simulate_symbol` L143-145 | **BUG** (look-ahead leakage) | Either |
| B8 | Survivorship: the universe is the *current* watchlist. | route default | TRADING ASSUMPTION | Optimistic |
| B9 | Corporate actions: see P2-5. | `ohlc.py` | Risk | Either |
| B10 | Drawdown sequenced by `entry_date`, while P&L is realised at exit. | `_compute_core_stats` | Minor | Understates DD |
| B11 | Breakeven proposals are not simulated. | — | Mismatch | — |
| B12 | Same-bar stop/target tie assumes the stop first. | L11-17 | DESIGN CHOICE, conservative | Conservative (good) |

**Robustness reading of the documented results.** Per the instructions, profitability is not judged here, but the quality of the evidence can be. DESIGN.md reports, net of 0.40 %:
- **momentum_breakout:** 137 trades, profit factor 1.43, +16.9R, max drawdown 6.7R;
- **trend_pullback:** 165 trades, profit factor 1.43, +15.2R, max drawdown 5.2R;

over about 2 years and 63 symbols. Before any of this justifies capital:
- **Expectancy is thin:** about +0.12R and +0.09R per trade. B1-B5 are each capable of erasing that, B1 and B3 especially for momentum_breakout.
- **Sample size is small:** 137-165 trades split into two halves gives about 70-80 trades per half. The 95 % CI of the mean R at σ≈1.5R is about ±0.35R, several times the measured expectancy.
- **Parameter selection:** chosen on the same 2024-07..2026-09 window (four sweeps). "Plateau" selection reduces overfitting but does not remove it.
- **Regime:** about 2 years of one market regime; no bear-market segment is evident.
- **Needed before trusting the numbers:**
  - fix B1-B7 in-app;
  - commit the study scripts;
  - report the R distribution, not just totals;
  - report sensitivity tables;
  - track a paper-trading period with *live* fills (P1-13) and compare it with backtest predictions for the same dates.

---

# Risk Engine Assessment

**Position sizing** (`compute_position_size`):
- `floor(portfolio_value × risk% / |entry − stop|)` capped by `floor(portfolio_value × capital% / entry)`. Correct.
- Zero and negative edges are handled: `risk_per_share <= 0` or `entry <= 0` gives 0, and the scan skips quantity 0.
- Uses the reference entry, not the fill. A LIMIT caps the fill price, so this is conservative for entries.

**The six entry checks:**

| Check | Basis | Assessment |
|---|---|---|
| Min R:R | target_1 vs stop | Correct; target_2 is informational only. |
| Capital per trade | `entry × qty / portfolio_value` | Correct. `portfolio_value` includes margin cash (documented). |
| Single-stock exposure | holdings **cost** (settled only) + same-day live tickets + this ticket | **P1-5**: T+1 blind spot and positions ignored. |
| Sector exposure | as above, over mapped symbols | **P1-4**: effectively skipped. |
| Open positions | count of live executed BUYs not closed | Correct, but inflated by P0-5 phantoms and deflated by P0-3 and P0-6 false closures. |
| Total portfolio risk | Σ original `risk_amount` of open live tickets | Conservative: it ignores trailing stops that have moved to breakeven or better, and uses *requested* quantity (P1-3). |

**Other observations:**
- **Exit check** (`_check_exit_or_reduction`):
  - `closes_ticket_id` is validated for existence, BUY, executed and open. There is **no quantity or mode match** (P0-4).
  - Alert-action sells are validated against broker holdings + t1 + positions. VERIFIED SAFE.
- **Dry run vs live:** dry-run tickets never count toward any limit (`mode='live'` filters). Intentional, and VERIFIED SAFE as a separation, but see P1-13.
- **Stale portfolio state:** checks run at ticket *creation*. A strategy scan uses one context for the whole batch (fine), but approval can be much later: P0-4 (3) and P1-6 (stale-signal guard).
- **Simultaneous tickets:** limits are only enforced against *executed* tickets, so N pending tickets that each pass can together breach every limit. Fixed by re-checking at confirm (P0-4) under the atomic transition (P0-1).
- **Cost basis vs MTM:** exposure uses cost and `portfolio_value` uses MTM plus cash. It is documented in the code (`compute_suggested_exit_quantity` docstring) and consistent between check and suggestion, so it is not double counted. It should be shown in the UI next to the numbers.
- **Missing account-level controls:** see P1-6.

---

# Execution Engine Assessment

The critical question was: **does the app ever assume order_id returned == order filled?**
- **Entries: no.** VERIFIED SAFE (S1). The ticket is `executed` with `entry_fill_status='pending'`, trailing waits, and the monitor records the real fill and average price.
- **Exits: yes.** P0-7: the GTT is removed, `closed_at` stamped and the journal written on placement.
- **GTT triggers: yes, analogously.** P0-6: "triggered" is treated as filled.
- **Manual cancels: an accepted cancel is treated as cancelled.** P1-1.

Behaviour by scenario:

| Scenario | Current behaviour | Verdict |
|---|---|---|
| OPEN (unfilled) entry | Monitor keeps polling; entry shown "placed · awaiting fill"; GTT already placed | OK. The GTT exists before the fill. For CNC, an early trigger is rejected by the broker because nothing is held. With AMO entries the GTT sits overnight against no position. Acceptable once P1-11 restricts entries to CNC. |
| COMPLETE entry | fill price and time recorded | VERIFIED SAFE |
| REJECTED / CANCELLED entry, 0 filled | GTT deleted, ticket cancelled, warning | VERIFIED SAFE |
| Partial fill | flagged, not modelled | P1-3 |
| Network timeout after submission | ticket stranded in `confirmed` | **P0-2** |
| NetworkException / TokenException | "UNKNOWN — verify manually", ticket `failed`, no automatic lookup | Partially safe. Needs the tag lookup from P0-2. |
| Duplicate confirmation | both execute | **P0-1** |
| Repeated API calls | no idempotency key or tag | **P0-1** |
| Application restart during execution | if the process dies between `place_order` and the DB update, the ticket stays `confirmed`, as in P0-2; between `place_order` and `place_gtt`, the position is unprotected and unknown | **P0-2** (the same recovery path fixes both) |
| Session expiry during execution | `TokenException` on the order: UNKNOWN (safe); on the GTT: `protection_failed` (safe); in the trailing tick: aborts the tick (P1-9) | Mostly safe |
| Entry filled but protection fails | `protection_failed` + urgent UI | VERIFIED SAFE (S4). There is no automatic retry of the GTT, and a "retry protection" button would help (POTENTIAL IMPROVEMENT). |
| Exit submitted but not filled | position shown closed and unprotected until terminal | **P0-7** |
| GTT cancelled before the exit fills | by design in `_execute_exit` | **P0-7** |
| Stale or orphan GTTs | not detected | **P1-10** |
| Order modified externally in Kite | quantity or price changes are not reflected; the monitor records `filled_quantity` only in messages | P1-3 (the same fix) |
| Position closed externally | reconciliation logs a shortfall (only if the app's expected quantity exceeds the actual); the GTT becomes orphaned; the ticket stays open | Detected, not resolved. Acceptable given "Mark Reviewed", but add a "close in app" action. |
| Broker/DB mismatch | logged, never auto-resolved | By design; fine |

**Server-side idempotency for real-money execution: none exists.** It is required: P0-1 plus the tag in P0-2.

**Can a user action submit the same order twice?**
- Two tabs or two views, confirmed concurrently (P0-1).
- A re-created ticket after a stranded timeout (P0-2).
- Two exit tickets for one position, e.g. a strategy exit plus Emergency Exit (P0-4).
- A cross-site page (P0-8).

Within a single dialog, the UI disables the button during the request (`app.js` L2733-2742, L4645-4650), which blocks a simple double-click. That is a client-side mitigation only.

---

# Broker Reconciliation Assessment

**Existing checks** (`reconciliation.py`, every 180 s, trading account only):
1. **Order status.** Today's executed-and-filled tickets are compared with `order_history`. Useful, but only for "filled" tickets.
2. **GTT active.** Each open live ticket with a `kite_gtt_id` must be `active`. Correct, but the detail text wrongly claims the trailing engine will handle non-trailing tickets (P0-5).
3. **Position shortfall.** The sum of the app's open live quantities per symbol must be no more than holdings + t1 + positions. Correct and pragmatic, since outside-app holdings are tolerated.

Mismatches are deduplicated on (ticket, check_type, actual) and require a manual "Mark Reviewed". Good design.

**Gaps, in priority order:**
- Orphan GTTs from broker to app, and GTT quantity above held quantity (P1-10).
- Tickets stuck in `confirmed` or `unknown` (P0-2).
- Positions whose GTT is `triggered` but still held (P0-6).
- Exiting positions (P0-7).
- Partial fills not reflected in quantities (P1-3).
- Dry-run/live mode inconsistencies: a live position closed by a dry-run exit (P0-3). Detect with "closed_at set but no executed live exit and broker still holds".

**Sources of truth.** The broker is authoritative for orders, fills, GTTs and holdings; the app is authoritative for intent and strategy context. Today the app *derives* "closed" by itself in four places:
- `tickets.confirm_and_execute_ticket`
- `trailing_engine._handle_gtt_closure`
- `order_monitor._resolve_exit` (re-open)
- `orders.cancel_order`

The P0 fixes should concentrate position-closing decisions in the order monitor and GTT watcher, driven by broker state.

---

# Database Assessment

- **Integrity:**
  - `CHECK` constraints on enums and positivity; FKs declared and `PRAGMA foreign_keys=ON` per connection. Good.
  - `closed_trades.sell_ticket_id` is unique. Good.
  - Missing: a uniqueness guard against two *executed* exits for the same `closes_ticket_id`, e.g. a partial unique index `ON trade_tickets(closes_ticket_id) WHERE status='executed' AND closes_ticket_id IS NOT NULL`. This one index backstops P0-4 and P1-8 at the storage layer.
- **Invalid state transitions:**
  - Every transition is an unconditional `UPDATE … SET status=` in application code: confirm, reject, cancel-all, order cancel, monitor. No guard on the prior status.
  - `reject_ticket` and `cancel_all_pending` have the same read-then-write race as P0-1. They are low impact (reject vs confirm race: reject may overwrite `confirmed` to `rejected` while an order is in flight).
  - **Recommendation:** every status write uses `WHERE status IN (…expected…)` and checks the rowcount. It is mechanical and low risk.
- **Transactions and crash consistency:**
  - Each function opens a connection; commits are frequent and mid-operation (e.g. `_handle_gtt_closure` commits the synthetic ticket before the journal).
  - The execution path deliberately commits `confirmed` *before* calling the broker, which is correct: write intent first. But recovery for that state is missing (P0-2).
- **Multiple sources of truth:**
  - `sessions.access_token` vs `accounts.access_token_enc` (P1-12).
  - The position quantity `trade_tickets.quantity` vs the broker's `filled_quantity` (P1-3).
  - `sector_mapping` vs `SECTOR_LOOKUP` (P1-4).
  - Strategy defaults duplicated in `db.py` L815-869 and each strategy module (documented, and checked by a test; acceptable).
- **Indexes:** P2-2.
- **Migrations:** P2-1.
- **Historical records:** P2-4. `backtest_runs` are never deleted; fine.

**Should concepts be separated?**

| Concept | Current home | Separate? |
|---|---|---|
| Trade intent | `trade_tickets` | No. It works. |
| Broker order | columns on the ticket (one order id per ticket) | **Yes, minimally.** Retries, partial fills, cancels-and-re-places and GTT legs are one-to-many and cannot be represented. That is the root of P0-2, P1-1 and P1-3. Add a `broker_orders` table (P2-3). |
| Fill | `filled_price` / `filled_at` on the ticket | Add `filled_quantity` now (P1-3); a separate fills table is not needed. |
| Position | `closed_at` on the entry ticket | Acceptable for long-only, one entry per position. Add an explicit `position_state` (open / exiting / closed / unknown), derived from broker events. |
| Protective order | `kite_gtt_id` on the ticket | Fold into `broker_orders` with role `gtt`. |
| Closed trade | `closed_trades` | Fine as is. |

---

# Security Assessment

**Current posture:** a single-user local app bound to `127.0.0.1` (uvicorn default; USER_GUIDE). Acceptable locally **only once P0-8 is fixed**.

| Item | Finding |
|---|---|
| API key | Plaintext in `.env` and `accounts.api_key`. This is a public identifier, so that is fine. |
| API secret | In `.env` (plaintext) and `accounts.api_secret_enc` (Fernet). The key is in the same `.env`, so the encryption protects only against a DB-only leak. |
| Access token | **Plaintext** in `sessions.access_token` (P1-12). It is valid for a trading day and allows orders. |
| `.env` | Correctly `.gitignore`d, and auto-appended with a generated key (`crypto.ensure_encryption_key`). `load_dotenv()` in `main.py`. |
| SQLite | Untracked (`.gitignore`), but it and seven `.bak` copies contain the plaintext tokens of past sessions. Old tokens expire daily, but the backups also hold the encrypted secret next to the key. |
| Browser exposure | No secrets are sent to the frontend. `session/status` exposes the user name only. |
| Endpoint protection | **None** beyond "a broker session exists". Any local process or web page can call the API (P0-8). |
| CORS | No CORS middleware, so cross-origin *reads* are blocked. Correct, but it does not prevent CSRF writes. |
| Callback | `/callback` accepts any `request_token` (Kite validates it with the secret, so it is safe). No `state` parameter, so login CSRF is possible (low impact locally). |
| Logging | No secrets logged (grep verified); only `crypto` prints the path of the generated key file. |
| Exception leakage | Kite error strings are returned to the client (P2-10); unhandled exceptions give a generic 500 (FastAPI default, no traceback). |
| Third-party JS | CDN scripts without SRI (P1-12). |
| Dev server | `--reload` recommended (P1-12). |

**What MUST change before running on a cloud VM with a static public IP:**
1. **Authentication on every route and on the WebSocket:**
   - at minimum, a strong password login issuing an HttpOnly, Secure, SameSite=Strict session cookie;
   - preferably also network restriction: bind to 127.0.0.1 and reach it only through an SSH tunnel, WireGuard or Tailscale.
   - Kite's static-IP requirement concerns *outbound* API calls. Nothing requires exposing the UI publicly.
2. **CSRF protection** (P0-8) and an **Origin check** on the WebSocket.
3. **TLS** (reverse proxy such as Caddy or nginx) if any port is exposed at all.
4. **Secrets out of the working directory:** environment variables from a systemd unit with 0600 permissions, or a secret manager; drop the plaintext token column.
5. **Firewall:** deny all inbound except SSH (key-only) or the VPN.
6. **Disk:** the DB and backups contain trading history, so use an encrypted volume and restrict backup permissions.
7. **Process:** run under a process supervisor, no `--reload`, and log to rotating files (P2-9).
8. **Kite redirect URL** set to the VM's HTTPS callback, or keep login local through the tunnel.

---

# Testing Assessment

**Inventory:**
- **115 tests pass** (15 s):
  - `test_baseline_api.py` — 36 functions;
  - `test_review_changes.py` — 35;
  - `test_alert_action.py` — 13;
  - `test_signal_detail.py` — 29.
- `tests/e2e_ui.py`: a Playwright smoke/screenshot script against `demo_server.py`; not run as part of pytest.
- `FakeKite` is a solid, in-memory broker with an order book, GTT book, `fail_next` injection and `set_order_state`.

**Failure paths already covered** (good):
- rejected live entry (`test_confirm_live_order_rejected`);
- protection failure (`test_confirm_live_protection_failed`);
- rejected entry cancels the GTT;
- rejected exit re-opens the position;
- triggered or deleted GTT;
- kill switch cancels an unfilled entry;
- partial candle correction;
- autopilot skip and retry logic;
- stale-signal expiry;
- tick snapping.

**Missing tests, by category** (all verified absent by searching test names and bodies):

| Category | Missing |
|---|---|
| Broker mock / failure path | `requests.ReadTimeout` from `place_order` (P0-2); `DataException`; timeout from `place_gtt` after the order; `TokenException` mid-trailing tick; Emergency Exit with one failing position (P1-2) |
| State machine | concurrent confirm (two threads); confirming `draft`; confirming an exit whose position is closed; two exits for one position; exit order OPEN → CANCELLED → position state; partial entry → GTT quantity / journal quantity; manual order cancel of an exit ticket (P1-1) |
| Mode | a dry-run-mode exit of a live position must not close it (P0-3); stale-tab expected-mode mismatch (P1-7); breakeven in dry-run on a live ticket |
| Lifecycle | OCO (non-trailing) GTT fired → position closed (P0-5); triggered GTT with no fill and still held → *not* closed (P0-6); duplicate closure race (P1-8) |
| Security | cross-origin POST without the custom header → 403 (P0-8) |
| Risk | T+1 exposure (P1-5); unmapped-sector policy (P1-4); pending tickets jointly exceeding limits at confirm |
| Regression / DB | `init_db()` on a copy of a real `.pre_*_bak` DB; idempotency of running migrations twice |
| Backtest | entry fill requires the next bar to trade ≤ limit (B1); exits at the next open (B2); gap-through stop (B3); `end_date` force-close (B7) |
| Frontend | lifecycle badge rendering per state; mode banner vs server mode after a broadcast; approve buttons disabled during flight |

**Minimum suite before real-money trading:** one test per P0 finding, written *first* to demonstrate the defect (PK-0), then flipped to assert the fix. Plus:
- the Emergency Exit per-position isolation test;
- the partial-fill quantity test;
- the migration-on-real-DB test;
- a 20-step "day in the life" integration test on `FakeKite`: signal → approve → AMO → fill → GTT → trail → GTT trigger → fill → journal. It should assert the ticket, GTT book and journal state after each step.

---

# UI/UX Assessment

The view taken is that of a trader operating the terminal between 09:15 and 15:30.

**Strengths:**
- **DRY RUN vs LIVE is clearly and redundantly signalled:**
  - a persistent colour-coded banner (`index.html` L43);
  - a header pill;
  - a per-ticket `LIVE` / `DRY RUN` chip;
  - approve buttons that turn red and read "Place order (LIVE)";
  - an execution note explaining regular order vs AMO plus GTT (`app.js` L2606-2614, L4517, L4565);
  - a confirmation dialog for switching to live.
- A risk-check breakdown is shown for every ticket, with skipped checks shown as ⚠, not ✓.
- Urgent alerts persist: protection failed, reconciliation mismatches, trailing errors.
- The Signal Detail view explains every condition.
- Empty states exist ("No tickets yet", etc.).

**Problems:**

1. **Lifecycle states are not visually unambiguous.** Mapping the required states to what the UI shows (`statusBadgeHtml`, `app.js` L2027-2033):

| Required state | Shown today |
|---|---|
| PROPOSED / PENDING | "awaiting approval" ✓ |
| SUBMITTED | "confirmed" for a moment. A **stranded** submission (P0-2) shows "confirmed" indefinitely, which reads as success ✗ |
| OPEN (order working) | "placed · awaiting fill" ✓ |
| PARTIAL | "executed" ✗ (only a toast at the time) |
| FILLED | "executed" ✓ |
| PROTECTED | not shown ✗ |
| UNPROTECTED | a row tint (`row-flagged`) plus a banner alert; no badge in the row ✗ |
| EXITING | the original shows "executed"; the exit ticket shows "placed · awaiting fill" ✗ |
| CLOSED | **"executed"**: a closed position is indistinguishable from an open one in the ticket tables ✗ |
| FAILED | "failed" ✓ |
| UNKNOWN / UNCONFIRMED / ESTIMATED | "executed" ✗ |

   **Recommendation:** compute a single `lifecycle_state` server-side in `_serialize` from status, fill status, closed_at, kite_gtt_id and the exit tickets, and render one colour-coded badge per state everywhere. This is the most important UI change. It is a small backend function plus one frontend renderer.

2. **The mode toggle sits next to the kill switches.** "Live trading" is in the same banner cluster as "Cancel All Pending" and "Emergency Exit All" (`index.html` L48-56). The live switch is dialog-gated, so it is not dangerous, but in a panic the user's eye lands on three unrelated controls. Separate them.
3. **Emergency Exit in DRY RUN** is presented as a normal action (see P0-3). Until that is fixed, the dialog should refuse when live positions exist and the mode is dry run.
4. **Mode in other tabs** goes stale (P1-7).
5. **Information hierarchy:** 11 sidebar sections. The trader's primary question — *what do I hold right now, is each position protected, what is working at the broker* — is spread across Dashboard (broker holdings), Trade Queue (tickets) and Orders & Trades (broker orders and GTTs). A single "Positions" view per open app position would remove most cross-referencing: quantity, fill, current stop, GTT status, LTP, unrealised R, lifecycle badge, exit button.
6. **Typography density:** most text is 10.5-12.5 px, with some 8 px and 10 px (`style.css`). At market pace, anything carrying P&L or state should be at least 13 px with tabular numerals (partly done).
7. **Performance:** a full re-render per tick batch (P2-8) can make buttons shift under the cursor during fast markets.
8. **Toasts for critical events:** `order_update` urgent toasts persist (`timeout: 0`, good), but other outcomes (fills, rejections) disappear. An event-log panel on the dashboard would let the user review what happened while away.
9. **Loading and error states:** the approval path shows "busy" on cards; a failed approval shows only a toast, "Approval failed", even when the order may have gone through (P0-2). It should say "outcome unknown — check Orders before retrying".
10. **Responsiveness:** desktop-first layout; acceptable for a trading terminal.

---

# Technical Debt

- **`frontend/app.js` is a 4,942-line monolith.** No framework is needed. Split it into native ES modules served as `<script type="module">` (api, state, render/tickets, render/orders, signal-detail, kill-switch), keeping the no-build setup. Do it opportunistically, one module per touched feature.
- **Duplicated logic:**
  - identical `_log_event` helpers in four modules;
  - broker `_call` wrappers in `orders.py` and `portfolio.py`;
  - strategy defaults in `db.py` and the modules (tested).

  Consolidate `_log_event` into `backend/ticket_events.py`.
- **`routes/tickets.py`** holds business logic (`confirm_and_execute_ticket`, closing and journaling). After the P0 fixes, move execution orchestration into `execution.py` or a small `lifecycle.py`, so the kill switch, routes and monitor share it without importing a router module (`kill_switch.py` imports from `routes.tickets` today).
- **Sync/async mixing:** the trailing engine is on the event loop (P1-9); the other loops are correctly threaded.
- **Configuration** is Python constants (`config.py`). Fine for single-user use; move risk-relevant toggles (AMO, trading lock) into the DB so they are auditable and visible in the UI.
- **Type safety:** `sqlite3.Row` everywhere, so status strings are free text in about 15 places. A `TicketStatus` `Literal`/`Enum` module would catch typos in transitions.
- **Testability:** good already (FakeKite, pure strategy functions). The main obstacle is module-level singletons (`ticker_manager`, `_consecutive_failures`), which are manageable.
- **Docs drift:** DESIGN.md §5.6 says the trailing engine detects GTT closures for positions in general (it does so only for trailing ones), and §4 says "no live entry is ever left unprotected". Update both with the fixes.

---

# Recommended Implementation Packets

Each packet:
- is independently mergeable and testable;
- starts from the PK-0 tests;
- touches few files;
- must begin with the standard backup: git tag and branch plus a `kite_terminal.db.pre_<packet>_bak` copy.

The order reflects risk reduction per unit of regression risk.

| # | Packet | Fixes | Files | Tests | Regression risk |
|---|---|---|---|---|---|
| **PK-0** | Failure-path tests that *document* current behaviour, marked `xfail(strict=True)` where they demonstrate a defect | all P0 | `tests/test_live_safety.py` (new), `fake_kite.py` (add `raise_on_next(name, requests.ReadTimeout)`) | itself | None |
| **PK-1** | Atomic confirm: conditional `UPDATE … WHERE status='pending_confirmation'`; 409 on rowcount 0; same pattern for reject and cancel-all | P0-1, part of P0-4 (drops `draft`) | `routes/tickets.py` | concurrent-confirm (2 threads), confirm-draft→409 | Low |
| **PK-2** | Kite order `tag=kt<ticket_id>`; catch-all broker exceptions → `outcome_unknown`; monitor resolves `confirmed`/unknown tickets by tag; UI "UNKNOWN — do not resubmit" | P0-2 | `execution.py`, `order_monitor.py`, `db.py` (column), `app.js` (badge) | timeout-then-found, timeout-then-absent | Low-Med |
| **PK-3** | A closing ticket inherits the position's mode; refuse a mismatch at confirm; breakeven acts on the ticket's mode; Emergency Exit refuses in dry run when live positions exist | P0-3 | `routes/tickets.py`, `kill_switch.py`, `strategy_engine.py` | dry-run exit of live position → refused / closes nothing | Low |
| **PK-4** | Re-validate at confirm: re-run entry risk checks; exit → position still open, same mode, no executed sibling; auto-reject sibling exits; partial unique index on executed exits | P0-4 | `routes/tickets.py`, `risk_engine.py`, `db.py` | stale exit refused; joint-limit breach refused | Low-Med |
| **PK-5** | CSRF middleware (custom header + Origin), WS Origin check; add the header in `fetchJsonMutate` | P0-8 | `main.py`, `routes/ws.py`, `app.js` | cross-origin POST → 403; app flows unaffected (existing tests with header) | Low |
| **PK-6** | GTT watcher for all live protected positions (split from the ratchet); run in `to_thread`, per-ticket try/except; conditional `closed_at` update | P0-5, P1-8, P1-9 | `trailing_engine.py` | OCO fired → closed; closure race → single close | Low |
| **PK-7** | Closure requires broker confirmation (fill or held-qty drop); otherwise `gtt_triggered_unfilled` + urgent alert | P0-6 | `trailing_engine.py`, `reconciliation.py` | gap-down non-fill stays open & alerted | Medium |
| **PK-8** | Exit lifecycle: EXITING state; journal and `closed_at` on the exit fill; GTT kept until fill (or cancel-on-place only for in-hours MARKET) | P0-7 | `execution.py`, `routes/tickets.py`, `order_monitor.py`, `journal.py`, `app.js` | exit OPEN→COMPLETE, OPEN→CANCELLED, AMO exit overnight | Medium-High (do last among P0s) |
| PK-9 | Manual order-cancel leaves resolution to the monitor | P1-1 | `routes/orders.py` | cancel exit order → position stays open | Low |
| PK-10 | Emergency Exit per-position isolation and result table | P1-2 | `kill_switch.py`, `app.js` | one failing position doesn't stop others | Low |
| PK-11 | Mode binding: `expected_mode` on confirm + `mode_changed` broadcast | P1-7 | `routes/tickets.py`, `execution.py`, `app.js` | mismatch → 409 | Low |
| PK-12 | Trading lock + daily loss limit + max orders/day + max notional + price-deviation + stale-signal guard (one sub-packet each) | P1-6 | `risk_engine.py`, `db.py`, `routes/*`, `app.js` | one per control | Low each |
| PK-13 | `filled_quantity` + GTT quantity sync on partial | P1-3 | `db.py`, `order_monitor.py`, `risk_engine.py`, `journal.py`, `trailing_engine.py` | partial entry end-to-end | Medium |
| PK-14 | Sector seeding from `SECTOR_LOOKUP` + unknown-sector policy | P1-4 | `sector_mapping.py`, `risk_engine.py`, `routes/watchlist.py` | unmapped symbol handling | Low |
| PK-15 | Exposure basis: `t1_quantity` + positions | P1-5 | `risk_engine.py` | T+1 exposure test | Low |
| PK-16 | Orphan-GTT reconciliation | P1-10 | `reconciliation.py`, `app.js` | orphan detected | Low |
| PK-17 | Restrict manual entries to BUY+CNC | P1-11 | `models.py`, `routes/tickets.py`, `app.js` | SELL/MIS → 400 | Low |
| PK-18 | Secrets & hardening: drop the plaintext token column, key location, SRI, remove `--reload` from docs | P1-12 | `db.py`, `kite_client.py`, `crypto.py`, `index.html`, docs | login/session tests | Low-Med (migration) |
| PK-19 | Server-derived `lifecycle_state` + badges + "Positions" view | UI | `routes/tickets.py`, `app.js`, `style.css` | render tests | Low |
| PK-20 | Paper-broker mode (FakeKite fed by live LTP) | P1-13 | new `paper_broker.py`, `kite_client.py` | lifecycle on paper | Low (additive) |
| PK-21 | Backtest realism: next-bar fill model, next-open strategy exits, gap-aware stops, costs, portfolio cap, `end_date` fix; commit the study scripts | P1-14 / B1-B7 | `backtest.py`, `backtest_analytics.py` | one per item | Low (backtest-only) |
| PK-22 | P2 hygiene: schema_version, indexes, rotation logging, holiday list, WS queue bound, render throttling, corporate-action detection | P2-* | various | per item | Low |

---

## GO / NO-GO checklist for enabling live ordering

This covers **software operational safety only**. It says nothing about whether the strategies will be profitable. Every item must be **GO** before the first real-money order.

### Blocking code fixes (all P0)
- [ ] P0-1: confirm is atomic; a concurrent double-confirm test places exactly one order.
- [ ] P0-2: broker exceptions of every type are caught; orders are tagged; a stranded `confirmed` ticket is resolved by the monitor; the UI shows UNKNOWN.
- [ ] P0-3: an exit's mode equals its position's mode; a dry-run mode can never close a live position.
- [ ] P0-4: only `pending_confirmation` is confirmable; entries are re-checked at confirm; stale or duplicate exits are refused.
- [ ] P0-5: an OCO-protected position is closed in the app when its GTT fills.
- [ ] P0-6: a triggered but unfilled GTT leaves the position open with an urgent alert.
- [ ] P0-7: an exit placed but not filled shows EXITING and keeps protection until the fill.
- [ ] P0-8: a cross-origin POST without the app header is rejected (test proves it).

### Blocking operational prerequisites
- [ ] Static IP registered in the Kite developer console; one `place_order` of **1 share** of a liquid large-cap as a LIMIT far from market, then cancelled, observed end to end (ticket → order book → cancel → monitor → ticket cancelled).
- [ ] Run without `--reload`, under a supervisor that restarts on crash; the startup log shows all 5 loops running.
- [ ] Trading lock (PK-12a) exists and was toggled in a drill.
- [ ] Emergency Exit drill **on the paper broker or with 1 share**: all positions exited, per-position results displayed.
- [ ] Reconciliation drill: a GTT deleted manually in Kite web → the app flags "unprotected" within one cycle and does **not** mark the position closed.
- [ ] Manual entries restricted to BUY + CNC (PK-17).
- [ ] Sector mapping covers every watchlist symbol, or unknown sectors fail closed (PK-14).
- [ ] DB backup taken, and a restore rehearsed once.
- [ ] Full pytest suite green, including the new P0 tests; `init_db()` tested on a copy of the live DB.

### First live phase constraints (recommended)
- [ ] `max_open_positions` ≤ 2, `max_capital_per_trade_pct` ≤ 2 %, absolute max notional set (PK-12).
- [ ] Market-hours only: AMO disabled, or confirmed explicitly per order.
- [ ] Daily review of `reconciliation_log` and of the orphan-GTT list for the first 20 sessions.
- [ ] Every live fill compared against the backtester's prediction for the same signal (feeds PK-21).

**Current verdict: NO-GO.** Eight P0 items are open. None requires a rewrite. PK-0 through PK-8 are estimated at a few focused sessions, each small and independently testable.
