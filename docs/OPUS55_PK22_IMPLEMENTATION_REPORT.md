# OPUS55 PK-22 Implementation Report: platform hardening (migrations, calendar, data integrity, observability, workers, diagnostics)

| | |
|---|---|
| Packet | **PK-22** only (audit P2-1, P2-2, P2-5, P2-6, P2-7, P2-9, P2-13; plan row PK-22). PK-23 and later were not started. No strategy module or threshold was changed. |
| Baseline before | **1,157 passed**, 0 failed, 0 xfailed, 0 xpassed, measured on the untouched code (`6446c79`) before the first edit. |
| Backups | git tag `backup-pre-pk22` and branch `backup/pre-pk22` at `6446c79`. `backend/kite_terminal.db.pre_pk22_bak` was made with the SQLite online-backup API from a read-only connection; `integrity_check` ok; 28 tables, 18 trade tickets, 50,556 daily candles. It is not committed (`*_bak` is git-ignored). |
| After | **1,239 passed**, 0 failed, 0 xfailed, 0 xpassed, three consecutive times (1,157 + 80 in the three new PK-22 files + 2 new cases of the existing PK-5 write-route inventory, which picked up the new rebuild route). |
| Live DB | `backend/kite_terminal.db` sha256 `1378f8b4…dacf`, identical before and after all work. It was never opened by app code. See *Real-DB Migration Simulation* for one note on its empty `-wal`/`-shm` sidecar files. |

# Executive Summary

PK-22 changes how the terminal starts, runs and reports on itself. It adds no trading feature.

- **Migrations are versioned and transactional.**
  - A `schema_version` table records every migration once.
  - Each pending migration runs in its own transaction, and the database is backed up automatically first.
  - A database written by newer code is refused with `DATABASE_SCHEMA_UNSUPPORTED` before anything is written.
  - A second start issues **zero DDL and changes zero rows**, proven by a statement trace and a whole-database digest.
- **The dashboard-index churn is gone.** `SCHEMA` no longer re-creates `idx_dashboard_panels_account_type`, and migration v2 drops it wherever it had come back. It was present in the real database.
- **The terminal knows the NSE calendar.** `backend/trading_calendar.py` is the one authority for:
  - holidays;
  - sessions (the previous and next trading day, the latest completed session);
  - the IST day boundary.

  The holiday data comes from the four official NSE circulars for 2023–2026 plus the amendments they later received. It was cross-checked against every one of the real database's 754 candle dates, with no mismatch.
- **Stale history is detected, not trusted.** On every refresh, the provider's bars for dates already stored are compared with ours. A consistent disagreement means Kite has back-adjusted the history (a split or bonus): the symbol becomes `NEEDS_REFRESH`. A gap of more than 25% that nothing has confirmed is `SUSPECT`. Flagged symbols are excluded from backtests with the reason, and `NEEDS_REFRESH` ones are skipped by the live scan. A **manual** rebuild re-downloads and replaces the series atomically.
- **Logs are traceable and bounded.**
  - A correlation id follows each request and each background cycle, and `X-Request-ID` is echoed.
  - Critical flows write one stable `event=` line each.
  - The launcher writes a rotating log file (at most about 110 MB), still redacted.
- **Workers are supervised and isolated.**
  - A supervisor never starts a second copy of a loop, records every crash, and cancels and awaits everything at shutdown.
  - One bad ticket, malformed broker row or failing check no longer ends a whole cycle.
  - The per-browser WebSocket queue is bounded: ticks are coalesced, and events are never dropped silently.
  - SQLite waits 10 s for a lock instead of failing after 5.
- **Operators can see the state.** `GET /api/health` (liveness), `GET /api/ready` (safe to trade, with reasons), `GET /api/diagnostics`, and a read-only **System Diagnostics** page. None of them calls the broker or returns a credential.
- **Measured.** The reconciliation cycle is **10× faster** (1,277 → 117 ms on a 2,000-position synthetic book) and a restart is 17.9 → 2.9 ms. No endpoint regressed materially.

Validation:
- **80 new tests.**
- **10/10 focused mutations caught**, with every mutated file restored to its sha256.
- **Repeats:** the PK-22 suites ×10, the migration/restart subset ×20 and the worker/queue subset ×20, all green.
- **Real-DB simulation:** a scratch copy was migrated by the old and the new code and compared table by table; **no existing business row was changed by PK-22**.
- **Browser:** **25/25** under enforced CSP. `e2e_ui.py` is **53/54, identical to the pre-PK-22 code**.
- **Full suite:** green three consecutive times.

One real, pre-existing defect was found by the new tests and fixed: the legacy migration chain dropped three columns on very old databases (see *Deviations From Audit*, item 4).

# Schema Versioning

`backend/db.py`:

- **`schema_version`** columns: `version` (PK), `migration_name`, `applied_at`, `duration_ms`.
- **`MIGRATIONS`**, an ordered, append-only registry. The name is checked against every database's history.

  | v | Name | What it does |
  |---|---|---|
  | 1 | `baseline_pk0_pk21` | Everything before PK-22: `SCHEMA` plus the ~35 introspective `_ensure_*` steps, unchanged in behaviour. A database of any age, or a brand-new one, converges to the same place. |
  | 2 | `pk22_drop_legacy_dashboard_index` | See *Dashboard Index Fix*. |
  | 3 | `pk22_query_indexes` | See *SQLite / DB Performance*. |
  | 4 | `pk22_ohlc_history_integrity` | `ohlc_history_status` and `symbol_transitions`, backfilled once (see *Historical Data Integrity*). |

- **`migrate(conn)`** runs these steps in order:
  1. **History check first.** If the recorded history is not a prefix of `MIGRATIONS` (a newer version, a renamed migration or a gap), it raises `DatabaseSchemaUnsupported` (`error_code = DATABASE_SCHEMA_UNSUPPORTED`) **before any write**. Startup therefore aborts before a loop or a broker session starts.
  2. **Pending migrations on a non-empty database** trigger an automatic online backup to `kite_terminal.db.pre_schema_v<N>_bak` (a timestamp is appended if that name exists). It is verified with `integrity_check`, and a failed backup aborts startup. A brand-new database is not backed up.
  3. **Each migration runs in its own transaction.**
     - The migration connection uses `isolation_level=None` with explicit `BEGIN IMMEDIATE` and `COMMIT`/`ROLLBACK`.
     - `PRAGMA foreign_keys=OFF` is set *around* the transaction, because the PRAGMA is a no-op inside one. The old code toggled it mid-transaction.
     - The legacy `executescript()` calls, which COMMIT first, are replaced by a statement splitter (`sqlite3.complete_statement`).
     - The `schema_version` row is written in the same transaction.
     - A migration that would **add** foreign-key violations is refused and rolled back.
  4. **`PRAGMA optimize`** runs only after a migration was applied.
- **`schema_status(conn)`** is read-only (current, latest, pending, history, supported) and is used by diagnostics and readiness.
- **`db.last_init`** records what this process's start did: the migrations applied and the rows the startup invariants changed.

# Migration Order

- **Startup** runs `migrate()` first, then the **startup invariants**, which run every start but are data-only and write nothing when there is nothing to do:
  - the seed default account and the one-time trading-account upgrade;
  - execution settings, risk settings, account-safety state;
  - the dashboard and watchlist panels;
  - strategy configs and versions;
  - the PK-18 token scrub.
- **Then** the closed-trade backfill runs.
- **Seed change.** `_seed_risk_settings` and `_seed_strategy_configs` now use `INSERT … WHERE NOT EXISTS` instead of `INSERT OR IGNORE`. An ignored insert still advanced the AUTOINCREMENT counter, which is the per-start `sqlite_sequence` write PK-18 noticed.
- **The token scrub stays an every-start invariant** on purpose: if an older code version were ever run against the database again, it would scrub again.

# Startup Idempotency

Test `test_second_startup_makes_no_schema_change_and_writes_nothing`:
- it runs a start, then traces every SQL statement of the next start with `set_trace_callback`;
- it asserts **no CREATE/ALTER/DROP**, `startup_row_changes == 0`, an identical `sqlite_master`, and an identical digest of **every table including `sqlite_sequence`**.

The real-DB copy behaves the same: its second and third starts applied nothing and changed 0 rows. A migration interrupted halfway (a simulated crash in v4, after v4's own DDL) leaves the database at v3, with v4's DDL rolled back too. The next start resumes, and exactly `[4]` is applied.

# Dashboard Index Fix

- **Root cause (PK-20 finding).**
  - `SCHEMA` contained `CREATE UNIQUE INDEX IF NOT EXISTS idx_dashboard_panels_account_type ON dashboard_panels(account_id, panel_type)`.
  - `_ensure_dashboard_panels_screen_column` dropped it only on the run where it added `screen`.
  - Every later start re-created it, and never dropped it again.
- **Effect.** Every restarted database, **the real one included** (confirmed on the scratch copy), carried a unique index that forbids the same panel type on two screens, which is exactly what the `screen` migration exists to allow.
- **Fix.**
  - The line is removed from `SCHEMA`.
  - The screen step always ensures the `(account_id, screen, panel_type)` index.
  - **v2 drops the legacy index** wherever it exists. This is an intended, documented change.
- **Tests.**
  - A fresh database never has the legacy index, across starts.
  - A database carrying it loses it, and the same panel type can then exist on two screens.
  - Mutation M2 (re-creating it on start) is caught by 4 tests.

# Trading Calendar

`backend/trading_calendar.py` is now the only place that knows the NSE calendar.

- **Terms:**
  - A *trading day* is a regular session day: a weekday that is not a listed holiday.
  - A *special session* is Muhurat trading, a budget-day or disaster-recovery weekend session. It produces a daily candle, but its **hours are not modelled**: the market is never reported open during one, and it is never an AMO target.
  - A *covered year* is a year present in the data file.
- **API:**
  - `is_trading_day`, `is_holiday`, `holiday_name`, `special_session`, `has_exchange_session`;
  - `previous_trading_day`, `next_trading_day`, `trading_days_between` (numpy `busday_count`'s `[start, end)` convention, holiday-aware);
  - `latest_completed_session`, `next_session_date`, `is_market_session_open`, `session_state` (`WEEKEND`/`HOLIDAY`/`PRE_OPEN`/`OPEN`/`CLOSED`);
  - `ist_day_start_utc_sql`, `coverage`, `calendar_status`;
  - `use_calendar(...)` (a test override).
- **Uncovered year.** Weekdays are assumed to trade (the old behaviour), and `CALENDAR_YEAR_UNCOVERED` is raised as a readiness warning, in December for the coming year too.

**Callers migrated** (no semantics changed except where holidays matter):

| Where | Before | Now |
|---|---|---|
| `market.is_trading_day` / `is_market_open` / `last_complete_session_date` | Weekends only | Thin delegates. Every caller, and every test that pins `market.is_market_open`, works unchanged. |
| IST offset | Declared in 5 modules | `kite_client`, `risk_engine`, `trailing_engine` and `order_monitor` import the calendar's. |
| IST-midnight cutoff | 3 copies; `risk_engine._same_day_ist_cutoff_utc` could not be pinned | One `ist_day_start_utc_sql`. The risk_engine helper takes an optional `now`. |
| `paper_broker._next_session` (paper AMO target) | Weekday walk | `next_session_date`: holidays and special sessions skipped |
| `journal` `holding_period_trading_days` | `numpy.busday_count` | `trading_days_between` (new journal rows only; stored rows are untouched) |
| `signal_chart.signal_age_sessions` | Calendar days, labelled "session(s)" in the UI | A real session count (a Friday signal on Monday evening is 1, not 3) |
| Header market chip (`app.js`) | Weekday + time only | Defers to the server's `session_state`/`holiday_name` in `/api/account-safety/status`; the tooltip names the holiday |

**Deliberately unchanged, and tested:**
- the PK-12 daily entry count and daily loss still reset at **IST midnight** (a calendar day, not a session);
- `strategy_engine._is_stale` still counts calendar days against `MAX_DATA_STALENESS_DAYS`;
- the backtester is bar-driven and needs no calendar: a holiday simply has no bar.

**Yearly update.** `python -m backend.trading_calendar check [--verify-db <copy.db>]` validates the file and cross-checks it against candles, opening the copy read-only and immutable. The procedure is in `docs/DEPLOYMENT.md`.

# Holiday Handling

**Data** (`backend/calendar_data/nse_equity_holidays.json`) was transcribed from the official NSE Capital Market circulars, which were read at development time and are **never fetched at runtime**:

| Year | Circular | Weekday holidays | Amendments included |
|---|---|---|---|
| 2023 | NSE/CMTR/54757 (150/2022) | 15 | Bakri Id moved 28 → 29 June |
| 2024 | NSE/CMTR/59722 (154/2023) | 17 | 22-Jan (declared holiday; Saturday 20-Jan traded), 20-May (general elections, Mumbai), 20-Nov (Maharashtra elections) |
| 2025 | NSE/CMTR/65587 (170/2024) | 14 | — |
| 2026 | NSE/CMTR/71775 (172/2025) | 16 | 15-Jan (Maharashtra municipal elections) |

There are 9 special sessions:
- Muhurat: 2023-11-12, 2024-11-01, 2025-10-21, 2026-11-08;
- Saturday sessions: 2024-01-20, 2024-03-02, 2024-05-18;
- budget days: 2025-02-01 (Saturday), 2026-02-01 (Sunday).

**Cross-check against the real candles.** On the backup, covering 72 instruments from 2023-09-07 to 2026-09-22:
- every weekday with no candles is a listed holiday;
- every weekend candle is a listed special session;
- Muhurat on a weekday holiday (2024-11-01, 2025-10-21) has candles and is correctly a holiday for regular hours.

**754 of 754 dates agree.** The amendments were confirmed this way, not from memory.

**Tests** (`tests/test_trading_calendar.py`, 22 tests, fixed IST datetimes, with 2026-09-14 Ganesh Chaturthi as the real-holiday fixture):
- **Days:** a weekday, a weekend and a holiday; Friday → Monday; holiday Monday → Tuesday.
- **Sessions:** the latest completed session across a holiday; session states; special sessions are never open.
- **Counting:** trading-day counting.
- **Callers:** a stale signal across a holiday (it stays valid until Tuesday's close, and would have been stale without the calendar); a paper AMO placed on Friday evening before a holiday targets Tuesday, stays pending on the holiday and fills on Tuesday; backtest next-session across a holiday; the autopilot does not run for a holiday; the daily count resets at IST midnight; journal trading days; signal age in sessions.
- **Data file and CLI:** malformed data files are refused; the override restores; the CLI cross-check detects a candle on a holiday.

**Existing tests adjusted** (test-only, test intent kept):
- `test_review_changes::test_market_clock` pinned 2026-09-14 as "an ordinary Monday"; it is Ganesh Chaturthi, so the test moved one week later.
- `conftest.last_weekday` now returns the last *trading* day.
- `fake_kite.make_candles` is deliberately left weekday-only: it is synthetic, and the PK-21 golden fixture depends on its exact dates.

# Historical Data Integrity

- **Metadata.** `backend/ohlc_integrity.py` and v4 add `ohlc_history_status`, one row per instrument:
  - `symbol`, `provider`, `first_date`/`last_date`, `bar_count`;
  - `downloaded_at`, `refreshed_at`, `data_hash` (sha256 of the stored rows);
  - `status` (`OK`/`SUSPECT`/`NEEDS_REFRESH`), `status_reason` (JSON), `status_at`, `verified_gaps`.

  `ohlc_meta` keeps its single job, "how far back history was requested", unchanged.
- **Symbol changes.** `symbol_transitions` records a tradingsymbol change for **one** instrument token. Series of different tokens are never merged.
- **Detection during `refresh_latest`**, before re-fetched candles overwrite stored ones:
  - The provider's closes for dates already stored are compared with ours.
  - **Two or more dates** differing by more than `HISTORY_ADJUSTMENT_TOLERANCE_PCT` (2%), **all by one consistent factor** (within 0.5%), mean the history was back-adjusted: the symbol becomes `NEEDS_REFRESH`, with the ratio and dates.
  - The **newest stored date is excluded**, because a candle captured soon after the close can still be revised, and one date is never proof.
  - A new open-vs-previous-close gap beyond 25% that no rebuild has verified marks an `OK` symbol `SUSPECT`.
  - Only a rebuild clears a flag.
- **Migration backfill.** v4 fills the table once for every stored instrument, with the gap scan. The real copy: **72 instruments, all OK, no symbol transitions**.

# Corporate Action Refresh

- **Manual only (user decision).** `POST /api/data/history/{token}/rebuild` returns 202 and queues a `rebuild` job; an unknown token returns 404. The **Rebuild history** button in System Diagnostics calls it. Nothing is rebuilt at startup or automatically.
- **`ohlc.rebuild_history`:**
  1. It downloads the whole history into memory first, at least as far back as what is stored.
  2. Only then does it replace the series, in **one transaction**, by deleting and inserting.
  3. The symbol is marked `OK`, and any gap that survives a fresh download is recorded as verified, because it is real.

  A failed or empty download raises and leaves the stored data and flag untouched (tested). Indicators are recomputed afterwards.
- **Consumers:**
  - **Backtest:** `run_backtest` excludes `SUSPECT`/`NEEDS_REFRESH` symbols before loading. They appear in `universe.excluded_symbols` (symbol, status, reason, since) with a warning: "INFY EXCLUDED: its stored price history is NEEDS_REFRESH …".
  - **Live scan:** `run_entry_scan` skips `NEEDS_REFRESH` symbols (reported in `diagnostics.history_needs_refresh`). This is a data-eligibility gate only; no strategy rule changed. `SUSPECT` is *not* skipped live: a real crash must not block the scanner.
- **Data-job queue.** Pending jobs are de-duplicated by (type, token). Indicator computation now runs off the event loop.

# Logging / Correlation

`backend/obs.py`:

- **Correlation id** (a `ContextVar`). `CorrelationIdMiddleware` (pure ASGI, outermost) gives every HTTP request and WebSocket an id and echoes it as `X-Request-ID`. A client-supplied id is kept only if it is a plain 8–64 character token. Even a request refused by the Host guard carries one.
  - **Background cycles** set their own: `om-<n>`, `tr-<n>`, `rc-<n>`. `asyncio.to_thread` copies the context into the worker thread.
  - **Formatter.** Every log line shows `[cid]`.
- **Structured events.** `log_event(logger, "order.submitting", ticket_id=…, …)` writes one stable line: `event=… account_id=… ticket_id=… position_id=… symbol=… mode=… order_id=… order_tag=… cid=…`, passed through the PK-18 redactor.

  | Flow | Events |
  |---|---|
  | Approval and execution | `approval.claimed`, `approval.result_superseded`, `order.submitting`, `order.result` (outcome `executed`/`execution_failed`/`submission_unknown`/`protection_failed`, with the order and GTT ids) |
  | Order monitor | `order.resolved` (fill, partial, exit; `order_monitor.ticket_failed` on isolation) |
  | Reconciliation | `reconciliation.finding` (now carries `ticket_id`, `issue_code`, `symbol`, `gtt_id`) |
  | Account safety | `safety.trading_locked`/`unlocked`, `safety.breaker_tripped`/`reset` |
  | Emergency Exit | `emergency_exit.completed` |
  | Data | `history.needs_refresh`, `history.suspect`, `history.rebuilt`, `history.symbol_changed` |
  | Runtime | `migration.backup`, `migration.applied`, `migration.failed`, `startup.database`, `startup.complete`, `worker.*`, `task.failed` |

- **Trace of one confirmation** (tested end to end):
  1. The HTTP request's `cid` appears on `approval.claimed` → `order.submitting` → `order.result`, which share the `ticket_id` and `order_tag`.
  2. The order monitor's later cycle logs `order.resolved` with the same `ticket_id` and `order_id` under its own `om-…` id.
- **Rotation (user decision).**
  - The launcher (`python -m backend.server`) sets `KITE_TERMINAL_LOG_FILE=logs/kite_terminal.log` by default.
  - A `RotatingFileHandler` of 10 MB × (10 + 1) files is attached to the root and uvicorn loggers.
  - `off` disables it. Bare uvicorn and tests stay on the console.
  - `logs/` is git-ignored.
- **Tests:**
  - file output is redacted, both a structured field named `access_token` and a registered secret inside an exception;
  - identifiers are stable;
  - rotation stays within its bound;
  - the launcher default is set;
  - request ids are issued and unsafe ones replaced;
  - the confirm flow is traced by one cid.

# Worker Robustness

**`backend/workers.py`, the supervisor:**
- `start()` starts each named loop at most once. A live loop is never duplicated ("a second startup event in the same process"), and a dead one is restarted.
- Every task has a done-callback: an unexpected exit is logged (`worker.crashed` or `worker.exited`) and recorded.
- **One-off tasks** go through `spawn()` and are tracked, so shutdown cancels them too: post-login work, a manual autopilot run, the startup data sync.
- **`stop(timeout)`** cancels and **awaits** everything with a bounded wait (10 s). The shutdown hook is now async.
- **Documented limit.** A cycle inside `asyncio.to_thread` finishes its current broker or DB call before its loop exits.

**`backend/worker_health.py`** records, per worker:
- task state;
- cycles, failures and consecutive failures;
- the last start, last success and last error (exception class plus redacted text);
- the duration.

Every loop wraps each cycle.

**Per-item isolation** (audit of every loop):

| Worker | Before | Now |
|---|---|---|
| Order monitor | One exception, or a malformed row in the order book (`o["order_id"]`), ended the cycle for every ticket | Each ticket is its own unit: rolled back alone and retried next cycle. `TokenException` still ends the cycle. Malformed book rows are skipped and counted. Live and paper were already separate. |
| Trailing / GTT watcher | Per-ticket isolation already (PK-6), but a failed live GTT book skipped all paper positions | Live and paper halves are independent. Malformed GTT rows are skipped. |
| Reconciliation | One transaction per cycle: any exception lost every finding. Malformed holdings or GTT rows killed it. | Each check runs in its own SAVEPOINT, so other checks' findings are kept and failures are reported (`failed_checks`). Malformed rows are skipped. |
| Paper exchange | Per-step commits, but no isolation | Each order or GTT is its own transaction. |
| Ticker callback | A malformed tick dropped the batch | Skipped. |
| Automation | A DB error in `_mark_interrupted_runs` at startup ended the task **permanently and silently** | Inside the retrying loop; its DB reads run off the event loop. |
| Data jobs | Per-job already | Unchanged, plus health, de-duplication and indicators off the loop. |

**Tests:**
- a bad ticket plus malformed book rows do not stop the monitor cycle;
- repeated `database is locked` errors are recovered from, with the counters checked;
- reconciliation keeps the other checks' findings;
- malformed broker rows are skipped;
- the paper half runs after a live failure;
- malformed ticks are skipped;
- the supervisor never duplicates, records crashes, stops everything and restarts after a stop;
- the real app `on_startup` twice gives one set of workers, and `on_shutdown` stops them.

# Queue Bounds / Backpressure

`backend/tick_queue.ClientQueue` replaces each browser socket's unbounded `asyncio.Queue` (audit P2-7):
- **Ticks are coalesced.** Only the latest tick per instrument is kept, bounded by an instrument cap (5,000; the stalest instrument is dropped first). Pending ticks go out as one message.
- **Events are never dropped silently.** Events include `order_update`, `trailing_error`, `tickets_changed`, `mode_changed` and `auth_error`. They form a FIFO delivered before ticks, bounded at 1,000. If a client falls that far behind, the socket is closed with **1013** instead of an event being lost.
- **Resync on reconnect.** `app.js` re-reads state (`scheduleRefresh()` + account safety) on every **re**connect, because events are prompts and the database is the truth.

`TickerManager` now guards its listener set with a lock (it is read from KiteTicker's and the workers' threads) and ignores a closed event loop.

**Tests:**
- 100,000 ticks over 50 instruments stay at 50 entries, and the newest price per instrument is kept;
- the instrument cap holds;
- events are ordered first and an overflow raises instead of dropping;
- the real endpoint closes with 1013.

# SQLite / DB Performance

- **Connections.** `get_connection()` uses an explicit `timeout` of 10 s plus `PRAGMA busy_timeout=10000`; before, an implicit 5 s was followed by "database is locked". WAL, foreign keys and synchronous mode are unchanged. A test holds a write lock for 0.4 s in another thread and proves a writer waits instead of failing.
- **Indexes (v3), each justified by a measured hot path** and asserted with `EXPLAIN QUERY PLAN`:

  | Index | Serves |
  |---|---|
  | `trade_tickets(closes_ticket_id)` | The correlated open-quantity subquery (per row), `_OPEN_POSITION_WHERE`'s `NOT IN`, and every per-position exit lookup in the monitor, trailing, reconciliation and kill switch |
  | `trade_tickets(account_id, mode, status)` | The monitor's `pending_tickets`/`unresolved_submissions`, trailing's `monitored_positions`, reconciliation and risk queries |
  | `trade_tickets(kite_order_id)` / `(kite_gtt_id)` | `_linked_ticket_map` on every `/api/orders`, `/api/trades` and `/api/gtts`, and the cancel routes |

  **Audited and not indexed:**
  - `closed_trades`, which already has `(account_id, mode)` and unique buy/sell indexes;
  - `reconciliation_log` (`resolved`, and `issue_code, gtt_id` exist);
  - `paper_orders`/`paper_gtts` (account+status and tag exist);
  - `system_events`, which is small and has the account index;
  - `sector_mapping`, whose PK is the symbol, on a table of about 50 rows;
  - `order_tag`, which never appears in a WHERE clause (matched in Python).
- **N+1 removed.** Reconciliation and trailing opened a **new connection per position** for the tick size (400 per cycle). `instruments.get_tick_size(…, conn)` reuses the cycle's connection.
- **VACUUM/ANALYZE.** Never automatic; `PRAGMA optimize` runs only after an applied migration. `python -m backend.db_maintenance {status,integrity,backup,analyze,vacuum}` is added and documented, with vacuum "app stopped" only, and tested.

# Operational Diagnostics

**`backend/diagnostics.py`, `backend/routes/diagnostics.py`, `backend/version.py`.** The version module reads the commit from `.git` files with no subprocess, and passes `FastAPI(version=…)`. `GET /api/diagnostics` reports:
- **app:** version, commit, branch, Python;
- **schema:** status and history, and what this process's start did;
- **database:** integrity via `quick_check`, cached 10 min, `?refresh=true` to force;
- **workers:** health for every worker (critical flag, task alive, cycles, last success, failures in a row, last redacted error), and the last cycle of the order monitor, reconciliation, trailing, data jobs and automation;
- **reconciliation status;**
- **paper broker:** order and GTT counts by status;
- **broker session:** `VALID`/`STALE`/`MISSING` **from the stored login time only**, with `verified_with_broker: false`, because no broker call is made;
- **account safety:** lock, breaker, today's live entries;
- **market:** session state, holiday, latest and next session, calendar coverage, sources;
- **historical-data health:** counts, flagged symbols, symbol transitions;
- **logging:** the file and rotation;
- **security runtime:** bind, exposure, reload, workers, allowed hosts.

**UI.** A **System Diagnostics** nav entry and view (`frontend/diagnostics.js`, all values set via `textContent`). It shows a verdict banner (READY / NOT READY TO TRADE), blockers and warnings, the sections above, a "Re-check (incl. DB integrity)" button, and the per-symbol **Rebuild history** button.

**Secrets.** No access token, API secret or key, request/public token, encryption key or raw broker payload is returned.
- **Automatic crawl.** The PK-18 GET-route crawl covers the new routes automatically.
- **Explicit test.** `test_diagnostics_report_is_complete_and_leaks_no_secret` plants sentinel values in the encrypted token, the API secret, the public token and the environment, and asserts none appears. It also asserts that no broker call was made.
- **Mutation.** M7, leaking the token, is caught.

# Health / Readiness

- **`GET /api/health`** is liveness: always 200 while the process serves requests, with a trivial DB read.
- **`GET /api/ready`** answers 200 when ready and **503** otherwise, with `blockers[]` and `warnings[]`. **The market does not have to be open.**
  - **Blockers:**
    - `SCHEMA_UNSUPPORTED`, `SCHEMA_NOT_CURRENT`;
    - `DB_INTEGRITY_FAILED`;
    - `WORKER_NOT_RUNNING:<order_monitor|trailing|reconciliation>`;
    - `BROKER_SESSION_MISSING`/`STALE`;
    - `TRADING_LOCKED`, `BREAKER_OPEN`;
    - `NOT_A_TRADING_ACCOUNT`.
  - **Warnings:** `WORKER_FAILING:<name>` (3+ failed cycles in a row), `CALENDAR_YEAR_UNCOVERED`, `HISTORY_NEEDS_REFRESH`, `LOG_FILE_DISABLED`.
- **Tests:** each blocker flips readiness, and health stays 200.

# Restart Safety

`tests/test_restart_pk22.py` has 9 scenarios, each built through the real API. A "restart" is `init_db()` ×2 (asserting the second applies nothing and changes 0 rows) plus fresh process memory. Then the monitor, the paper exchange, trailing and reconciliation run again, and each scenario asserts that state was preserved, nothing was duplicated and no safety state was reset.

| # | Scenario | Proven after restart |
|---|---|---|
| 1–2 | Clean DB | Identical `sqlite_master`, v1–v4 |
| 3 | Pending live order | Still pending with the same order id, **no new order or GTT**; fills once when the broker fills |
| 4 | UNKNOWN submission (accepted, response lost) | Adopted by tag, **exactly one `place_order` ever** |
| 5 | OPEN paper order | Survives, fills once at the observed price, one paper order, zero Kite writes |
| 6 | Partial position (4 of 10) | Quantity 4 kept, GTT still sized 4, no re-resize or second GTT |
| 7 | GTT reconciliation finding (protective GTT missing at broker) | The same unresolved findings, **none duplicated or resolved**; the position stays open; no GTT placed behind the user's back |
| 8 | Breaker open | Still open with its reason; a live approval is refused `BROKER_CIRCUIT_OPEN`; readiness blocks |
| 9 | Trading lock | Still locked; an approval is refused `TRADING_LOCKED` |
| + | EXITING position | Journaled **exactly once**, across two restarts |

Duplicate workers after a restart or reload are covered by the supervisor and app-startup tests.

# Real-DB Migration Simulation

Script `realdb_sim.py` (in the session scratchpad):
- **Copies.** Three scratch copies of `kite_terminal.db.pre_pk22_bak`, a byte copy of the live DB made at the start: *before* (untouched), *old* (migrated by the pre-PK-22 code from `git archive backup-pre-pk22`) and *new* (the PK-22 code, **three starts**).
- **Secrets.** The real encryption key was used in-process only and never printed; `crypto.ENV_PATH` pointed at a scratch file.
- **Comparison.** Every table was compared over common columns by primary key.
- **Clean-up.** The copies were deleted afterwards, because they hold real data.

| Check | Result |
|---|---|
| Schema history | v1 `baseline_pk0_pk21`, v2, v3, v4, applied once; run 1 applied `[1,2,3,4]` and changed 16 rows; runs 2 and 3 applied nothing and changed **0** |
| Schema stable across restarts | yes |
| Auto-backup | `new.db.pre_schema_v4_bak` created, `integrity_check` ok |
| Integrity / FK | ok / 0 violations |
| PK-18 token scrub | 15 plaintext session tokens → 0; the account keeps its encrypted token |
| PK-20 paper tables, PK-16 reconciliation columns | present (all 8 columns) |
| Trade tickets | 18 → 18, **no value changed**; `filled_quantity` backfilled on the 2 executed tickets |
| Daily candles | 50,556 → 50,556, unchanged |
| History status | 72 instruments, all `OK`; no symbol transitions |
| Legacy dashboard index | present before (1) → dropped (0) |
| Calendar cross-check on the migrated copy | agrees |

**Before → new: the complete list of differences, all intended.** Because the real DB was last started before PK-12, it also receives the pending PK-12…PK-21 migrations:
- **New tables:** `account_safety_state` (1 seeded row), `broker_failure_log`, `paper_orders`, `paper_gtts`, `schema_version`, `ohlc_history_status`, `symbol_transitions`.
- **New nullable or defaulted columns**, with no existing value changed:
  - `backtest_runs` +7, `backtest_trades` +17;
  - `reconciliation_log` +8;
  - `risk_settings` +7, with PK-12 defaults;
  - `trade_tickets` +4.
- **`sessions.access_token`:** 15 values blanked (the PK-18 scrub).
- **`sector_mapping`:** 1 → 52 rows (the PK-14 startup seed; the old code seeds the same).
- **Indexes:** 11 added (PK-12…PK-22), 1 dropped (the legacy dashboard index).

**Old code → new code: PK-22's own effect.** Only the PK-22 tables and indexes differ, plus two timestamp artefacts of seeding at different moments: `account_safety_state.updated_at` (1 row) and `sector_mapping.added_at`. PK-20 and PK-21 recorded the same artefact. **No existing business row was changed by PK-22.**

**Live DB.** The sha256 is `1378f8b4…dacf` before and after all work. **Note:** the live database's `-wal` (0 bytes, i.e. empty) and `-shm` (index) sidecar files were present at step 0 and are gone now. SQLite removes them when the last connection closes, which happened when the step-0 read-only backup connection closed. No data was involved: the WAL was empty, and the main file is byte-identical. The same effect later created and removed sidecars next to the backup file. The calendar CLI now opens copies with `immutable=1`, which never creates them.

# Performance Before / After

Script `perf_measure.py`:
- **Synthetic book:** 2,000 positions (400 open with GTTs, 1,600 closed with their exits) plus 50 pending live orders, 4,050 tickets in total;
- **Startup:** measured on a fresh DB and on the real copy;
- **Timings:** medians;
- **Code under test:** the untouched code (before) and the final code (after).

| Measurement | Before | After | |
|---|---|---|---|
| Startup, brand-new DB (first) | 870.9 ms | 609.5 ms | first-run cost is dominated by key generation and imports |
| Startup, restart (fresh DB) | 17.9 ms | **2.9 ms** | nothing re-run |
| Startup, real copy (first) | 82.2 ms | 394.9 ms | **one-time**: includes the new automatic backup of the 6 MB file and the v4 history backfill (hashing 50,556 candles) |
| Startup, real copy (restart) | 17.4 ms | **2.5 ms** | |
| Reconciliation cycle | 1,277.4 ms | **116.7 ms** | 11× — the `closes_ticket_id` index plus the tick-size N+1 |
| Order-monitor cycle | 40.8 ms | 40.0 ms | flat |
| `GET /api/app-positions` | 109.6 ms | 102.8 ms | flat |
| `GET /api/dashboard-panels` | 8.0 ms | 7.6 ms | |
| `GET /api/watchlist` | 10.2 ms | 10.2 ms | |
| `GET /api/strategies/backtests` | 7.8 ms | 8.0 ms | |
| `GET /api/orders` | 154.5 ms | 173.0 ms | noise-level; the cost is FakeKite deep-copying 4,050 orders |
| `GET /api/gtts` | 31.9 ms | 33.0 ms | |
| `GET /api/tickets` (3,650 tickets) | 1,153.9 ms | 1,147.9 ms | profiled: linear per-ticket serialization and lifecycle; the DB is 0.05 s; no N+1 — left as is |

There is no material regression. The only slower item is the one-time first migration, which buys a verified backup.

# Tests Added

**New files:**

| File | Tests | Covers |
|---|---|---|
| `tests/test_platform_pk22.py` | 49 | A (versioning, refusal ×3, interruption, FK guard, auto-backup, a synthetic early-era DB, a copy of the real backup), B (index churn, hot-query plans), D (detection rules, refresh, rebuild, suspect gap, symbol change, backtest exclusion, live scan, API, queue de-duplication), E (redacted structured file logs, rotation, launcher default, request ids, confirm trace), F (monitor isolation, transient recovery, reconciliation isolation, malformed rows, trailing halves, ticker, supervisor, app startup/shutdown, tick queue, WS 1013), G (busy wait, maintenance CLI), H (health, readiness blockers, diagnostics completeness and secrets) |
| `tests/test_trading_calendar.py` | 22 | C, above |
| `tests/test_restart_pk22.py` | 9 | I, above |

**Existing tests changed** (test-only; no assertion weakened):

| Test | Change | Why |
|---|---|---|
| `test_review_changes::test_market_clock` | 2026-09-14 → 2026-09-21 (and its Friday and weekend) | 09-14 is an NSE holiday |
| `test_live_safety::test_p0_4_exit_owner_index_migration…`, `test_partial_fills::test_migration_backfills…` | Call `conftest.as_pre_pk22_database(db)` before the re-run `init_db()` | They imitate an old DB by editing the schema; migrations now run once per DB |
| `test_security_hardening` `launcher` fixture | Also isolates `KITE_TERMINAL_LOG_FILE` | The launcher now sets it; the environment must not leak between tests |
| `conftest.py` | `as_pre_pk22_database`; `last_weekday` skips holidays | — |

# Mutation Results

Script `mutations.py`:
- **Scope.** Each mutant was applied to one file, and the three PK-22 test files were run.
- **Restore.** The original bytes were restored in a `finally` block and **sha256-verified** (all 10 matched).

| # | Mutation | File | Result | First catching tests |
|---|---|---|---|---|
| M1 | Schema version not advanced (the version row never written) | `db.py` | **caught**, 20 | brand-new DB, real-backup copy, old-DB forward, FK guard |
| M2 | Second startup re-creates the legacy dashboard index | `db.py` | **caught**, 4 | fresh-DB index, restarted-DB index, second-startup no-DDL, old DB |
| M3 | Holiday treated as trading day | `trading_calendar.py` | **caught**, 12 | Friday→Tuesday, autopilot holiday, journal days, CLI verify |
| M4a | Stale corporate-action history accepted (detection off) | `ohlc_integrity.py` | **caught**, 3 | back-adjusted refresh, detection rules, rebuild |
| M4b | Flagged history used anyway (exclusion off) | `ohlc_integrity.py` | **caught**, 2 | backtest exclusion, live-scan gate |
| M5 | A worker exception kills the whole monitor cycle | `order_monitor.py` | **caught**, 1 | bad ticket / malformed order |
| M6 | Unbounded tick queue restored (no coalescing, no cap) | `tick_queue.py` | **caught**, 1 | bounded queue, latest tick |
| M7 | Diagnostics endpoint leaks the access token | `diagnostics.py` | **caught**, 1 | diagnostics secrets |
| M8 | Newer database schema accepted | `db.py` | **caught**, 1 | refusal `[newer_version]` |
| M9 | Migrations run outside a transaction (extra) | `db.py` | **caught**, 2 | interrupted migration, FK guard |

**10/10 caught.**

# Targeted Repeats

All on the final code:

| Run | Result |
|---|---|
| PK-22 suites (3 files) ×10 | **10/10 green** (80 passed each, 8–9 s) |
| Migration/restart subset ×20 | **20/20 green** (28 passed each) |
| Worker/queue subset ×20 | **20/20 green** (17 passed each) |

No flake was seen. Clocks are pinned. The only waits are the 0.4 s lock holder in the SQLite test and short `asyncio` sleeps inside the worker-loop and supervisor tests.

# Browser Validation

**Setup:**
- `tests/demo_server.py --market-open` (FakeKite, throwaway DB), with its output sent to a file.
- Playwright with Edge headless, **CSP enforced**, and a `securitypolicyviolation` listener installed before any page script.
- A `NEEDS_REFRESH` row was inserted for INFY so the rebuild path could be exercised.

**Result: 25/25**:
- `/api/health` 200 with `X-Request-ID`, and `/api/ready` with blockers and warnings (no market-open requirement);
- the diagnostics verdict and all 7 sections;
- schema "4 of 4", integrity ok, critical workers running, calendar coverage 2023–2026, no credential text;
- the flagged INFY row, where **Rebuild history** reports "Rebuild queued". The server log then shows `event=history.rebuilt … bars=760`: the rebuild ran end to end;
- Re-check re-renders;
- a WebSocket reconnect re-reads account-safety state;
- **zero CSP violations, zero console or page errors, zero 4xx/5xx.**

Screenshots are in the session scratchpad (`browser22/`).

**Harness lesson.** Two earlier attempts froze mid-run. The script had started the demo server with `stdout=PIPE` and never read it. Once the server's output passed Windows' ~4 KB pipe buffer, every log call blocked and the event loop stopped. A 45-second latency probe of a demo server with its output discarded showed no stall (worst 50 ms). I briefly attributed the stall to the WebSocket handler, but a test showed that change could not be justified, so it was reverted and not shipped.

**Regression (`tests/e2e_ui.py`).**
- **Default demo.** It aborts at the first approval (the AMO dialog opens instead of the confirm dialog) **identically on the untouched pre-PK-22 code** at the same hour. This is a pre-existing time-of-day dependency, like PK-21's four pytest cases.
- **Clocks aligned.** With the demo server pinned open and the browser's clock set to 11:00 IST (a wrapper using Playwright `page.clock.set_system_time`; `e2e_ui.py` unchanged), **both trees score 53/54**, with the same known failure ("risk-blocked signal listed separately"), identical to the PK-18/20/21 baseline.

# Full Suite ×3

Three consecutive runs of the whole suite, after the last change of any kind:

| Run | Result |
|---|---|
| 1 | **1,239 passed**, 0 failed (197 s; started 22:15 IST, after market hours) |
| 2 | **1,239 passed**, 0 failed (197 s) |
| 3 | **1,239 passed**, 0 failed (199 s) |

1,239 = the 1,157 baseline + 80 new PK-22 tests + 2 automatic cases in `test_csrf_protection.py` (132 → 134: its write-route inventory now includes `POST /api/data/history/{token}/rebuild`, proving that route is behind the CSRF guard); 0 xfailed, 0 xpassed. The live DB sha256 afterwards is `1378f8b4…dacf`.

# Known Limitations

1. **Special sessions are listed but not modelled.** Muhurat trading and weekend budget/DR sessions produce candles, but the market is reported closed during them: no regular orders are sent, and no AMO targets them. No hours were invented.
2. **The holiday list needs a yearly update**, and any mid-year amendment added by hand. A missing year falls back to weekdays with a readiness warning. The 2026 list is verified against candles only through 2026-09-22.
3. **Corporate-action detection needs overlap.** It sees a back-adjustment only through bars already stored, which every refresh re-fetches. A split that Kite has *not* back-adjusted looks like a real gap (`SUSPECT`) until a rebuild confirms it. Dividends are not adjusted.
4. **Rebuild needs a Kite session and is manual.** A flagged symbol stays excluded or skipped until someone presses Rebuild.
5. **Broker login status in diagnostics comes from the stored login time.** It does not verify with Kite (`verified_with_broker: false`).
6. **Worker shutdown cannot interrupt a blocking call.** A cycle inside `to_thread` finishes its current broker/DB call; the wait is bounded at 10 s.
7. **Log redaction covers the handlers present at startup** (PK-18 limitation, unchanged). The rotating file is one of them.
8. **The daily entry and loss counts still reset at IST midnight**, deliberately (calendar day, not session).
9. **`/api/tickets` is linear in the number of tickets** (about 1.1 s at 3,650). It has no N+1 and is not a DB cost; pagination would be the fix if the history grows that large.
10. **`e2e_ui.py` depends on the time of day** (pre-existing). It needs server and browser clocks to agree the market is open.

# Deferred Work

- Frontend render throttling on tick bursts (audit P2-8). It was in the audit's PK-22 row but not in this packet; server-side coalescing now bounds what the page receives.
- Modelling special-session hours, if the circulars' timings are ever to be traded through the app.
- Optional: automatic rebuild of `NEEDS_REFRESH` symbols during the daily sync (the user chose manual for now).
- A pagination or summary mode for `/api/tickets`.
- A time-independent `e2e_ui.py` (pin both clocks inside the script).
- The audit's `broker_orders` table and `BEFORE UPDATE` audit triggers (P2-3, P2-4) remain out of scope.

# Deviations From Audit

1. **P2-1 said to run migrations "inside a transaction".** Done per migration rather than one transaction for all, so a failure keeps the earlier completed versions and resumes from there. The automatic `.bak` follows the memory convention's naming (`.pre_schema_v<N>_bak`).
2. **The plan said to extend `ohlc_meta`.** Done instead as a separate `ohlc_history_status` table: `ohlc_meta`'s "requested from" semantics drive the refresh logic, and a status row per instrument is a different concern.
3. **P2-5 said to "trigger a full re-backfill" on detection.** The user decided on flag plus manual rebuild. The detection compares all overlapping stored dates rather than one sampled date.
4. **Legacy-chain fix (a real bug the new tests found).** `_ensure_alert_action_source` rebuilds `trade_tickets` from a column list that predates `entry_evaluation`, `kite_chart_url` and `kite_chart_status`. On a database old enough to need that rebuild, those columns were added and then **dropped again, permanently**. v1 now re-ensures them right after the rebuild. It is a no-op on every current database; the real copy was unaffected.
5. **P2-7 said `maxsize` with drop-oldest.** Done as coalescing for ticks and a never-silent bounded FIFO for events, because the same queue carries the order and GTT alerts the UI must not miss.
6. **P2-9 said to log "one `broker_audit` line per Kite mutating call".** Implemented as `order.submitting` / `order.result` around every order execution, plus the GTT and monitor events. There is no separate per-call wrapper around every KiteConnect method.
7. **Readiness requires the critical workers to run.** Test and demo processes therefore report `WORKER_NOT_RUNNING` unless their loops are started.
8. **`signal_age_sessions` now counts sessions** (it was calendar days under a "sessions" label). This fixes the displayed number on weekends as well as holidays.
9. `docs/OPUS55_AUDIT_REPORT.md` was not modified.
