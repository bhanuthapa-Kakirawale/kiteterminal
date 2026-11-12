# Phase 2 implementation report: cross-process coordination and startup restriction

**Status:** see §15. `LIVE_ENTRIES_ENABLED` is `False`, and live trading stays **NO-GO**.

This implements the approved scope of `docs/PHASE_2_DESIGN_REVIEW.md` §13 ("the design", cited as D§n).
The user authorised Phase 2, including registering migration v11 and its effect on the real account
database on the next application start. During this work:

- the real account database was never opened, read, copied, migrated or connected to;
- `.env` was never read;
- no broker was contacted.

## 0. Starting state and preservation rules (recorded 2026-11-07, before any edit)

| Item | Value |
|---|---|
| Repository / branch | `D:/Claude_WS/kite-terminal` / `feature/signal-detail-and-strategy-revision` |
| HEAD | `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef` |
| Working tree | 53 entries: the 52 recorded in D§0 (38 ` M` + 14 `??`) plus `?? docs/PHASE_2_DESIGN_REVIEW.md`, written by the design review itself (D§15 row 2). With ignored files: 104 lines. Saved to the session scratchpad (`status_before*.txt`). |
| `backend/execution.py` | identical to HEAD |
| PK-25 frozen hashes | 13 / 13 match (sha256, CRLF→LF, `research/pk25/validation_manifest.json` → `model_freeze`) |
| `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`) |
| Protected files | `.env`, `backend/ui_prefs.json` and 41 `backend/kite_terminal.db*`: size and mtime recorded with `os.stat` only, never opened |
| Pre-edit copies | Every pre-existing file was copied to the scratchpad before its first edit, so each edit is auditable as a diff |
| Standing "back up before changes" practice (git tag and branch, plus a database copy) | **Not done this time, deliberately.** This task forbids copying or opening the account database and restricts git to read-only inspection. The operator backup before the first start (§11) replaces the database half. No git ref was created. |

Edits to pre-existing files were surgical (§2).

- Nothing was staged, committed, stashed, reset, checked out or cleaned.
- No frozen file was edited.

One incident was found and corrected during the work. A scripted edit read `backend/paper_broker.py` in
text mode and wrote it back with LF line endings, which normalised the whole CRLF file. It was restored
to CRLF the same minute. Its pre-edit content was then reconstructed by removing exactly my additions,
and that reconstruction is byte-identical to the baseline sha256. The same check passed for
`backend/config.py`. From then on, pre-existing files were edited only with the line-ending-preserving
editor, and a scan confirmed that no edited file has mixed line endings.

## 1. Implementation summary

The design's three layers (D§7) are implemented, together with the startup restriction (D§8).

1. **OS instance lock** (`backend/instance_lock.py`):
   - an exclusive lock on `<resolved DB_PATH>.instance.lock`: `msvcrt.locking(LK_NBLCK)` on byte 0 on
     Windows, `fcntl.flock(LOCK_EX|LOCK_NB)` on POSIX;
   - taken before `init_db`;
   - the handle is not inheritable, so a child process cannot keep it alive;
   - released on shutdown, through `atexit`, and by the OS on process death;
   - liveness is never decided by a PID.
2. **Fencing epoch and heartbeat** (migration v11, the `app_instance` singleton):
   - `takeover()` increments the epoch once per start under `BEGIN IMMEDIATE`, and waits out the lease
     after an unclean predecessor;
   - `fenced_transaction()` re-checks `(epoch, instance_uuid)` under the write lock in every Phase 2
     coordination transaction;
   - a 15 s fenced heartbeat detects another epoch (`INSTANCE_LOCK_LOST`), a foreign writer
     (`INSTANCE_CONFLICT`), a suspend/resume (`RESUMED_AFTER_SUSPEND`) and prolonged write failure
     (`HEARTBEAT_STALE`).
3. **Process write authority:** one in-memory flag gates:
   - the broker fence at `KiteConnect._request`, plus the non-SDK and paper wrappers;
   - `require_write_authority` on all 48 write routes;
   - the five background loops, at the start of every cycle.

   Authority is never regained within a process.

**Startup** follows D§8.2: lock → `init_db` → fenced takeover, which persists
`RESTRICTED(STARTUP_RECONCILIATION_INCOMPLETE [, PREVIOUS_INSTANCE_UNCLEAN])` in the same transaction →
loops and heartbeat → Stage 1, read-only.

- Stage 1 is retried 30/60/120/300 s until it passes, and re-run on every login and resume.
- Stage 2 is an explicit no-op.

**Entry restriction.** RESTRICTED adds a refusal of **new live entries** only, through
`confirm_time_blockers` and the new code `ACCOUNT_RESTRICTED` (409, never permanent). Exits,
cancellations, Emergency Exit and protection upkeep never pass that gate. That was verified in the
code (§8) and by tests.

**Non-owner mode.** A process that cannot take the lock is read-only. It runs no `init_db`, no ticker
and no loops. It serves a status page at `/`; every other request, including `/login` and `/callback`,
gets `503 ANOTHER_INSTANCE_RUNNING`.

**Decisions within the design's latitude** (confirmed with the user before implementation):

- v11 contains only the five Phase 2 tables. `instrument_attribution` and `broker_quantity_observations`
  are deferred to Phase 7. Stage 1 evaluates attribution in memory and reports it only.
- The restriction lives in a new `account_restriction` table, the §8.1 alternative to adding columns to
  `account_safety_state`.
- Rollback is a written down-migration, `db_maintenance rollback-schema-v11`.
- The two standing DMB test exclusions are approved.
- WIN-INT: run the safe subset here; L3-subst, L6 and R8 stay PENDING.
- In Live Readiness, RESTRICTED shows as a non-blocking warning, so LIVE arming is unchanged.

## 2. Files changed and why

**New production modules**

| File | Purpose |
|---|---|
| `backend/instance_lock.py` | lock, local-drive check, takeover/epoch, fenced transactions, heartbeat and suspend detection, authority flag, `require_write_authority`/`WRITE_AUTHORITY`, `broker_write` (fence and in-flight counter), shutdown record, non-owner status page (`InstanceOwnershipGuard`), diagnostics |
| `backend/startup_reconciliation.py` | Stage 1 (inventory, connectivity, one broker read, attribution report, classification, inconsistency detection, persisted run), the Stage 2 no-op, `derive_restriction`, persistence, supervision (`run_supervised`, `request_run`, `note_resume`) |

**Edited production files.** The ⁺ mark means the file was already modified in the worktree before
Phase 2; those edits were additive except where a line had to change.

| File | Change |
|---|---|
| `backend/db.py`⁺ | migration v11 (+227 lines / −0 vs the pre-edit copy): the DDL, `phase2_schema_status`, `_migration_v11_instance_coordination`, `rollback_schema_v11`, `SchemaRollbackRefused` |
| `backend/kite_client.py` | `fence_broker` and `_fenced_request`, installed by `create_kite_connect` |
| `backend/paper_broker.py`⁺ | `_owner_only` on its 5 write methods |
| `backend/main.py` | the D§8.2 startup order, the D§7.6 shutdown, the non-owner middleware |
| `backend/reconciliation.py`⁺ | per-cycle authority guard; `CLASSIFICATION_LOCK` around `_run_cycle`; the opt-in `single_read` flag on `_check_broker_gtts` (default unchanged); the end-of-cycle restriction refresh |
| `backend/order_monitor.py`⁺, `trailing_engine.py`⁺, `automation.py`, `data_jobs.py` | a one-line authority guard at the start of each cycle |
| `backend/live_preflight.py` | `restriction_check` in `confirm_time_blockers` (blocking) and in `evaluate` (warning) |
| `backend/account_safety.py` | the `ACCOUNT_RESTRICTED` failure, listed after the existing failures |
| `backend/diagnostics.py` | readiness blocker (not owner) and warning (RESTRICTED); the `instance` section of the report |
| `backend/db_maintenance.py` | the lock for analyze/vacuum/migrate/rollback; `mode=ro` for status/integrity/backup; the `rollback-schema-v11 --confirm` command |
| `backend/config.py`⁺ | Phase 2 constants (+20 / −0) |
| `backend/routes/*.py` (17 files, 3 of them ⁺) | `dependencies=WRITE_AUTHORITY` on 45 mutating routes plus the three writing GETs; `routes/auth.py` also requests a Stage 1 run after a login |
| `.gitignore` | `backend/*.instance.lock` |

**Tests**

| File | Change |
|---|---|
| `tests/conftest.py`⁺ | `env` takes the real lock and runs the real takeover, seeds a passed Stage 1, fences FakeKite and records Stage 1 requests; `owner_process` fixture; `grant_write_authority`, `seed_stage1_passed`, `release_write_authority`; `make_live_ready` re-seeds |
| New suites | `tests/test_schema_v11.py`, `test_instance_lock.py`, `test_broker_write_fence.py`, `test_write_authority_routes.py`, `test_startup_reconciliation.py`, `test_cli_instance_lock.py`; the child helper `tests/instance_lock_child.py`; the manual runner `tests/winint_phase2.py` |
| Existing tests changed (each listed in §9.3) | `test_dark_protection_schema.py`⁺, `test_live_preflight.py`, `test_platform_pk22.py`⁺, `test_ticket_integrity.py`, `test_broker_writes_switch.py`, `test_proxy_mandatory.py`⁺, `test_account_safety.py`, `test_account_safety_integration.py`, `test_manual_ticket_review.py`, `demo_server.py`⁺ (one fence line) |

**Docs:** `docs/DEPLOYMENT.md`, `docs/DMB_KITE_OPERATIONAL_RUNBOOK.md` (new §10), `docs/TESTING.md`⁺
(new §1.6), and this report.

**Not touched:**

- `backend/execution.py` and the 12 other PK-25 files (`strategy_engine.py` and `volume_research.py`
  among them);
- `backend/live_entry_gate.py`;
- the frontend;
- the Phase 1 dark-schema code. Only one comment line *before* the dark section was worded so that
  Phase 1's isolation test still passes.

## 3. Migration v11

Registered as `(11, "phase2_instance_coordination", _migration_v11_instance_coordination)`, so
`LATEST_SCHEMA_VERSION = 11`.

- The function is deliberately named outside the `_mNNN_` pattern: `_m011_` remains the marker of the
  Phase 1 dark journal functions (Phase 1 test 11b). Those functions stay unregistered, and are to be
  renumbered to v12 before Phase 3 activates them (§13).
- It runs inside the existing `migrate()`. That brings, unchanged:
  - the verified backup `<db>.pre_schema_v11_bak`;
  - `BEGIN IMMEDIATE`, with foreign keys off around the transaction;
  - the foreign-key-violation check;
  - the `schema_version` row;
  - rollback on any `BaseException`.

**DDL.** It follows the design verbatim for:

- `app_instance` (D§5.1), seeded with `(1, 0, 'none')`;
- `app_instance_history` (D§5.1);
- `broker_reads` (D§6.2);
- `startup_reconciliation_runs` (D§8.1).

`account_restriction` is built from the D§8.1 field list:

```sql
CREATE TABLE account_restriction (
  account_id          INTEGER PRIMARY KEY REFERENCES accounts(id),
  restriction_state   TEXT NOT NULL DEFAULT 'RESTRICTED' CHECK (restriction_state IN ('NORMAL','RESTRICTED')),
  restriction_reasons TEXT NOT NULL DEFAULT '["STARTUP_RECONCILIATION_INCOMPLETE"]'
                        CHECK (json_valid(restriction_reasons) AND json_type(restriction_reasons) = 'array'),
  restriction_since   TEXT,
  restriction_epoch   INTEGER,
  last_startup_run_id INTEGER REFERENCES startup_reconciliation_runs(id),
  updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (restriction_state = 'RESTRICTED' OR restriction_reasons = '[]')
)
```

The full DDL text is in `backend/db.py` (`_APP_INSTANCE_DDL` … `_ACCOUNT_RESTRICTION_DDL`). The stored
SQL is checked against it by `phase2_schema_status`.

**Safety properties, each tested (§9)**

- **Additive.** No existing table, index or row changes. Tested by a row-level comparison of every table
  (`sqlite_sequence` included) on a v10 database with trading data, before and after.
- **Transactional.** An exception or `KeyboardInterrupt` half-way leaves v10 byte-identical. So does
  `os._exit(137)` in a child process half-way through; the next start migrates cleanly.
- **Refuses partial or foreign states.** It classifies its own objects as `absent` / `present` /
  `partial` / `unexpected`:
  - `partial` or `unexpected` raises `DatabaseMigrationFailed` before any write;
  - `present` (exactly v11's objects, for example after a lost `schema_version` table) is a no-op that
    keeps the data, including the epoch.
- **Newer or foreign history:** refused by the existing `_check_history`.
- **Older builds** refuse a v11 database (`DATABASE_SCHEMA_UNSUPPORTED: … newer than this code
  supports`). Tested by running `init_db` with `MIGRATIONS[:10]`.

**Rollback** (`db.rollback_schema_v11`, exposed as `python -m backend.db_maintenance rollback-schema-v11 --confirm`):

1. It refuses, writing nothing, unless:
   - the history is exactly v1..v11 of this code;
   - the objects are exactly v11's;
   - no transaction is open.
2. It takes a verified backup, `<db>.pre_rollback_v11_bak`.
3. In one transaction, it drops the five tables in reverse dependency order, deletes `schema_version`
   row 11, and verifies v10 and no new FK violations.
4. Tested results:
   - the schema equals v10 byte for byte;
   - every trading row is preserved;
   - no orphaned `sqlite_sequence` entries;
   - an interrupted rollback leaves v11 intact;
   - a v10-era `MIGRATIONS[:10]` accepts the database afterwards.

## 4. Startup sequence and lock lifecycle

| Step | Code (`backend/main.py: on_startup`) | On failure |
|---|---|---|
| 1 | `server.enforce_runtime_policy()` (unchanged) | abort |
| 2 | `instance_lock.acquire(db.DB_PATH)`: the local-drive check, then the non-blocking OS lock. Idempotent in-process. | `NON_OWNER` / `DRIVE_REFUSED`: return. No `init_db`, ticker or loops; the guard serves the status page. |
| 3 | `init_db()`: migrations under the lock | lock released, exception re-raised (the startup aborts as today) |
| 4 | `await to_thread(instance_lock.takeover)`: lease wait if needed, then epoch + 1, identity, history, and RESTRICTED in the same transaction | `INSTANCE_CONFLICT`: read-only, return |
| 5 | sector seed and ticker (unchanged), the five loops (unchanged), the `instance_heartbeat` loop | each loop checks authority at the start of every cycle |
| 6–8 | `startup_reconciliation.request_run("startup")`: Stage 1 with retries, then Stage 2 (no-op), then derive and persist | stays RESTRICTED |

**Shutdown** (D§7.6):

1. `begin_shutdown()` drops authority: the fence refuses new writes and routes return 503.
2. The ticker stops and `supervisor.stop()` runs.
3. `finish_shutdown()` waits, at most 10 s, for broker writes still inside the SDK.
4. A fenced transaction records `state='stopped_clean'` and `clean_shutdown = (in_flight == 0)`, with a
   note naming any write still in flight, and closes the history row.
5. The lock is released.

A successor treats `clean_shutdown=0` as unclean.

**Process death.** The OS releases a byte-range lock when the process ends. Measured on this machine
after `os._exit(137)`, over 20 runs: maximum **0.7 ms**, median 0.5 ms (§10, L2).

## 5. Fencing and authority loss

- **Owner proof:** the process holds the OS lock **and** its `(epoch, instance_uuid)` equals the row's
  (in-memory `role == OWNER`).
- **`fenced_transaction(conn)`** opens `BEGIN IMMEDIATE`, then reads the singleton:
  - on a mismatch it rolls back and calls `lose_authority(INSTANCE_LOCK_LOST)` before anything else;
  - a connection to a *different* file is refused (`WrongDatabase`) without dropping authority.

  It is used by restriction writes, Stage 1 writes (broker read, findings, run) and resume persistence.
  The heartbeat and the shutdown record use the equivalent predicate inside their own `UPDATE`.
- **After a loss:**
  - write routes answer 503 with `INSTANCE_LOCK_LOST`;
  - the fence refuses every broker write (`InstanceNotOwner`, a definite rejection);
  - loops return at their next cycle;
  - Stage 1 is skipped;
  - no shutdown record is written;
  - reads keep working.
- **Disclosed residual (D§7.3):** a legacy cycle already past its authority check finishes its local DB
  writes. A broker request already sent cannot be recalled by any local token. Both are covered by the
  existing ambiguous-outcome handling (never re-sending) and by successor reconciliation.
- **Verified by:**
  - in-process E2 (an epoch bump, then the heartbeat → loss, routes, fence, loops, Stage 1, no record);
  - the subprocess E2/P1: two processes through a bypassed lock file. B takes over with epoch 3 and its
    write goes out. A's next heartbeat finds epoch 3, and A's write is refused with FakeKite recording
    nothing. At most one process sends.

## 6. Broker-write-path audit and interception evidence

**Choke point.** `create_kite_connect` is the only place a `KiteConnect` is constructed (`test_14d`
still finds exactly 2 constructions). It installs an instance-level `_request` that calls
`instance_lock.broker_write()` for every non-GET, ahead of the class's own `_request`. So:

- it composes with `DirectRouteKiteConnect` and `ReadOnlyKiteConnect` and leaves the class unchanged
  (the existing `type(kite) is …` tests still pass);
- it covers the plain proxied client, which had no override before;
- it covers every SDK write method, existing or future.

**Write sites** (repository-wide audit; the frozen ones are covered without edits):

| Site | Path to the fence |
|---|---|
| `execution._execute_live` / `_execute_exit` (frozen) | SDK `place_order` / `place_gtt` → fenced `_request`; paper → `PaperBroker` `_owner_only` |
| `strategy_engine.py:677` `modify_gtt` (frozen) | SDK → fenced `_request`; route `/api/strategies/stop-adjustments/{id}/confirm` → `WRITE_AUTHORITY` |
| `trailing_engine.py:811`, `position_quantity.sync_protection` / `remove_protective_gtt`, `gtt_cancellation.cancel` | SDK → fenced; their loops are guarded per cycle |
| `routes/orders.py` `cancel_order` / `cancel_gtt`, `routes/kill_switch` | route dependency, then the fenced SDK or paper broker |
| `margin_preflight.py:172` `order_margins` (a POST) | fenced (a non-owner never needs it) |
| Login `generate_session` (POST `api.token`) | fenced; `/login` and `/callback` also carry `WRITE_AUTHORITY` |
| FakeKite and the demo double | `fence_broker` wraps their write methods (in the `env` fixture and `demo_server`) |

There is no raw HTTP to Kite anywhere in `backend/`, and no `modify_order`, `convert_position` or MF
call.

**Evidence:**

- `test_broker_write_fence.py` (E5/E6):
  - every write method (11 parametrised) is refused before any request is made;
  - `generate_session` is refused;
  - GETs pass;
  - with authority, each write goes out once and is counted as in flight;
  - the fence comes ahead of the narrowing;
  - lost and shutting-down processes are refused;
  - FakeKite and `PaperBroker` writes are refused without authority, and no paper row is written.
- Mutation M3 (the fence bypassed for non-GETs) was caught: 24 failures.

## 7. Startup reconciliation behaviour

Stage 1 (`run_stage1`) is serialised by a lock and checks authority first.

1. **Inventory**, from the database only:
   - unresolved live submissions. `unknown` and `needs_review` always count. `submitting` counts when a
     predecessor claimed it (before this owner's takeover). The current process's in-flight claim is
     left to the existing LIVE-0 check, so the entry being confirmed never refuses itself;
   - unconfirmed fills, working entries, exits in flight;
   - positions with unknown or negative quantity;
   - critical and cleanup findings.

   The dark protection journal is not read: it is not live in Phase 2.
2. **Connectivity:** `get_kite()` (failure → `NO_SESSION`) and `check_rest_route()` (failure →
   `PROXY_UNAVAILABLE`). Either one makes the outcome `blocked`, with no read.
3. **One `broker_reads` row:**
   - `orders` → `get_gtts` → `holdings` → `positions`, each with its timestamp, count and error;
   - malformed GTT rows are counted;
   - `broker_holdings.instrument_quantities` checks well-formedness;
   - a `TokenException` sets `session_expired` and `NO_SESSION`, and the rest are marked
     "not attempted";
   - `complete=1` only if all four reads are well-formed within `STARTUP_READ_SPAN_SECONDS` (30 s);
   - GTT observations are recorded.
4. **Attribution:** evaluated per instrument key and **reported only** in the run's findings.
5. **Classification:**
   - `_check_gtt_state`, `_check_broker_gtts(single_read=True)` and `_check_position_state` run against
     the same read, in `_isolated` savepoints, inside a fenced transaction, under the reconciliation
     `CLASSIFICATION_LOCK`;
   - findings go through `log_mismatch` deduplication;
   - `_check_order_status` is not run here: it reads per ticket, outside the single read. The loop runs
     it.
6. **Inconsistencies** set `BROKER_DATA_INCONSISTENT`:
   - an app position absent at the broker;
   - a fill not reflected after `FILL_VISIBILITY_TOLERANCE_SECONDS` (60 s);
   - a GTT triggered today whose order is missing;
   - unreconcilable broker rows.
7. **Persist:** a `startup_reconciliation_runs` row (outcome, inventory, findings including the Stage 2
   note, `reasons_after`) and the derived restriction. One fenced transaction.

**Outcomes:**

- `passed`: complete, consistent, every check ran;
- `incomplete`: the read was incomplete;
- `failed`: an inconsistency, a check failure or an error;
- `blocked`: no session or no proxy.

A failed, partial or malformed read is never treated as "absent". R2 asserts that no finding results
from a run with a failed endpoint.

**Derived state** (`derive_restriction`, read-only, re-derived at every confirmation, persisted after
Stage 1 and every reconciliation cycle). It is the union of:

- process reasons: the `INSTANCE_*` codes, `HEARTBEAT_STALE`, `RESUMED_AFTER_SUSPEND`;
- sticky persisted reasons. These are changed only by Stage 1 (or by a resume):
  `STARTUP_RECONCILIATION_INCOMPLETE`, `PREVIOUS_INSTANCE_UNCLEAN`, `RESUMED_AFTER_SUSPEND`, `NO_SESSION`,
  `PROXY_UNAVAILABLE`, `BROKER_READ_FAILED`, `BROKER_DATA_INCONSISTENT`;
- "no passed Stage 1 in this epoch after the last login and the last resume";
- `BROKER_READ_STALE`: no complete read, from `broker_reads` or a healthy reconciliation cycle, within
  `GTT_OBSERVATION_MAX_AGE_SECONDS`. This implements the D§8.5 NORMAL condition;
- live local reasons, recomputed every time so that they clear when the cause does:
  `UNRESOLVED_OPERATION`, `RECONCILIATION_REQUIRED`, `CLEANUP_REQUIRED`.

`PROTECTION_GAP` and `LATE_GTT_APPEARED` need the protection journal (Phase 3+) and are never set in
Phase 2.

**Stage 2** is `run_stage2()`: it returns `writes: 0` and the note "no recovery writes are authorised in
this phase", and the run records that note.

## 8. Entry restrictions; exits remain available

- **Where it applies.** `live_preflight.confirm_time_blockers` gains `restriction_check(blocking=True)`.
  `account_safety.check_live_critical_state` maps it to `ACCOUNT_RESTRICTED` (409, not permanent),
  listed after the existing failures so their codes stay primary. These are called only from `precheck`
  and `final_gate`, both reached only `if new_entry` (`routes/tickets.py:1142`, `:1201`; the
  docstring: "Exits never come here").
- **Order of gates.** `final_gate` refuses a live entry at `live_entry_gate.refusal` *before* this check
  while `LIVE_ENTRIES_ENABLED` is `False`. In Phase 2 the restriction is therefore an additional refusal
  that becomes visible only where tests open the gate by monkeypatch.
- **Dry run and paper:** unaffected (tested).
- **Live Readiness:** a non-blocking WARN, so LIVE arming is unchanged.
- **Diagnostics:** `readiness` shows a warning; `report.instance` shows the role, epoch, heartbeat,
  in-flight count, and the derived and persisted restriction with the last run.
- **Tested available while RESTRICTED:**
  - cancel GTT and cancel order through the routes;
  - an explicit exit through ticket confirmation;
  - the exit action;
  - Emergency Exit;
  - order-monitor fill following and protection.

  The existing S9/S10/S9b/S10b tests also show exit and Emergency Exit while every entry control,
  `ACCOUNT_RESTRICTED` now included, blocks.
- **Release** is tested: a refused entry stays `pending_confirmation`; after a passing Stage 1 the same
  ticket executes.

## 9. Tests

### 9.1 Commands

Run from the repository root with `.venv/Scripts/python.exe`, sequentially (pytest-xdist is not
installed; `pytest.ini` has `addopts = -q`).

- **Focused runs**, each new file and each edited existing file: `python -m pytest -p no:cacheprovider
  -o faulthandler_timeout=… <file>`. Results are in §9.2.
- **Full regression**, final: `python -m pytest -rs -rf -p no:cacheprovider -o faulthandler_timeout=300
  --ignore=tests/test_calendar_parity.py --ignore=tests/test_dmb_handoff_roundtrip.py`. The result is in
  §9.4.

### 9.2 New suites (final focused results)

| File | Result | Covers |
|---|---|---|
| `test_schema_v11.py` | 23 passed | registration; exact DDL and checks; v10→v11 additive (row level); idempotent; present re-run keeps data; partial or foreign refusal (4 forms); newer refusal; older-build refusal; interruption by exception, `KeyboardInterrupt` and `os._exit` child; rollback byte-identical and data-preserving; v10 accepts after rollback; rollback refusals (3) and interruption; CLI rollback needs `--confirm` and the lock |
| `test_instance_lock.py` | 43 passed, 3 skipped (WIN-INT PENDING) | L1 (8 persistent processes, 200 rounds with N = 2..8, released together by an atomic `mkdir` barrier, coordinated through pipes; exactly one owner per round; also run 10× in a row = 2000 rounds, all passed), L2 (crash release), L2b, L3 (case and junction aliases), L4 (non-owner: no `init_db`/loops/takeover; status page; 503 everywhere including login and callback), L5, L6 (stubbed drive types, UNC), OneDrive warning, startup order, E1 (100 takeovers), E2 in-process, E2/P1 subprocess, E3 (lease wait, conflict, clean), E4 (PID reuse), S1 (3 processes × 4 threads), S2, C1 (6 crash boundaries in a child, then restart), C2 (clean; write in flight → unclean), heartbeat sequence, conflict, stale and clear, R8 (wall and monotonic jump), loops stop without authority |
| `test_broker_write_fence.py` | 31 passed | E5/E6 (see §6) |
| `test_write_authority_routes.py` | 58 passed | completeness against the route table; every one of the 48 write routes returns 503 without authority (parametrised); a code per loss mode; login and callback refused; an owner's login requests Stage 1; reads unguarded |
| `test_startup_reconciliation.py` | 33 passed | the takeover persists RESTRICTED; R1; no broker write; R2 (4 endpoints, no "absent" conclusions, backoff); R3 (expiry mid-read, no session, no proxy, re-run after login); R4 (3 malformed forms, inconsistency); R5; R6; stale read; local reasons clear; cleanup; restriction and release; dry run unaffected; gate constant `False`; readiness warning only; R7 (cancels, exit, exit action, Emergency Exit, upkeep); attribution report-only; F1 |
| `test_cli_instance_lock.py` | 16 passed | writing commands refuse while another process holds the lock, and run (and release it) when free; read-only commands work and write nothing; the `mode=ro` connection cannot write; `payload-shape` reads only; the research CLIs open databases read-only; the other CLIs never touch the app database; every `__main__` in `backend/` is audited |

### 9.3 Existing tests changed

Every change is listed with its reason. None weakens a guarantee.

- **Version pins 10 → 11:**
  - `test_dark_protection_schema.py` test_1b: still asserts that the dark functions are **not**
    registered, now by identity. test_10 and test_10b re-based on v11;
  - `test_live_preflight.py` (3 lines), `test_platform_pk22.py` (5), `test_ticket_integrity.py` (1).
- **Ownership needed for the code under test (fail-closed default):**
  - `test_broker_writes_switch.py` (module-level `owner_process`: it tests the writes switch, not
    ownership);
  - `test_proxy_mandatory.py::test_10` (an SDK write without `env`);
  - `test_platform_pk22.py::test_worker_recovers…` (runs a loop without `env`).
- **"Stop the app first" before a writing CLI:**
  - `test_live_preflight.py` (two migrate tests);
  - `test_platform_pk22.py::test_maintenance_cli…` now *also* asserts that `analyze` refuses while the
    lock is held.
- **New entry refusal listed in exact-order assertions:** `ACCOUNT_RESTRICTED` after
  `LIVE_PREREQUISITE_MISSING`, because the UNKNOWN ticket those tests plant is also an
  `UNRESOLVED_OPERATION`:
  - `test_account_safety.py::test_12c`;
  - `test_account_safety_integration.py::_block_every_entry_control`;
  - `test_manual_ticket_review.py::test_s9b`.
- **`test_account_safety.py::_ignore_live0_unknown_gate`:** the tests that switch off the LIVE-0
  unknown-submission gate (to count breaker failures or daily slots) now also switch off the same
  condition in the restriction.
- **`test_manual_ticket_review.py::test_a_new_kite_login…[live]`:** after its new Kite login it runs a
  real Stage 1 (`trigger='login'`) before the final live confirm, as the app does on login.

### 9.4 Full regression

**Earlier full runs, disclosed:**

1. **First run**, before the existing-test updates in §9.3: 3214 passed, 12 failed, 3 skipped. All 12
   are explained in §9.3. None was an exit or Emergency Exit regression: the S9/S10 failures were in the
   helper's exact refusal list, which runs before the exit step.
2. **Second run:** 3429 passed, **1 failed** (L1), 6 skipped.
   - L1 had also stalled once in a focused run that ran concurrently with the full suite.
   - Root cause, a **test-harness race on Windows**, not the lock: the racers read barrier files that
     another process was creating or replacing. That produced an empty read in one run, and a
     `PermissionError` sharing violation in a stress run. Heavy file churn also made rounds slow.
   - Fix (test-only): L1's coordination now runs through pipes, and the simultaneous release is an
     atomic `mkdir` that the racers only test for existence. The lock attempt itself is unchanged.
   - Afterwards: 10 consecutive runs passed (2000 rounds, one owner each), about 2.5 s per run.
   - I also found, and stopped, a stray pytest process left over from an early focused run (a loop test
     that hung before `owner_process` existed). It used only temporary databases.
3. **Final run** (after the L1 fix):

**Command:** `.venv/Scripts/python.exe -m pytest -rs -rf -p no:cacheprovider -o faulthandler_timeout=300 --ignore=tests/test_calendar_parity.py --ignore=tests/test_dmb_handoff_roundtrip.py`

**Result: 3430 passed, 0 failed, 0 errors, 6 skipped. Exit code 0, in 748.65 s.** Run sequentially
(no xdist), with nothing else running.

- **Collected:** 3436 tests = the Phase 1 baseline of 3229 + 207 new: 23 + 46 + 31 + 58 + 33 + 16, where
  `test_instance_lock.py`'s 46 includes its 3 placeholders.
- **Skips:**
  - the 3 known `tests/test_dmb_inbox.py:384` symlink skips (`WinError 1314`, the same as Phases 0 and 1);
  - the 3 WIN-INT placeholders: `test_instance_lock.py:194` L3-subst, `:233` L6 real share, `:674` R8
    real sleep. Each skip reason starts `WIN-INT PENDING`.

  Confirmed with `-rs` on those two files, because the `-rf` given after `-rs` replaced the skip summary
  in the full run.
- **Exclusions:** only the two standing DMB interpreter files, approved by the user for this phase. They
  were not run. Nothing else was deselected.
- **Isolation guard summary:**
  ```
  python-dotenv neutralised: True; implicit loads neutralised: 5; explicit loads delegated (temporary files): 5; explicit protected loads refused: 2
  inherited KITE_* variables scrubbed at start: 0
  guard refusals: 19 (decoy 15, expected 4, unexpected 0); network refusals: 1
  ```
  Against Phase 1's baseline, there is one more expected refusal: L5, which deliberately computes the
  real lock path and asserts the guard refuses it. The single network refusal is the deliberate
  TEST-NET-1 test.

### 9.5 Mutation checks (D§10.2)

Each check was run on the real file, with a scratchpad copy taken first. The file was restored
afterwards and its sha256 re-verified. Every mutation was caught.

| Mutation | Tests run | Result |
|---|---|---|
| M1 lock taken after `init_db` | startup order, L4 | caught (2 failed) |
| M2 no epoch check in the heartbeat | E2, heartbeat | caught (3 failed) |
| M3 fence bypass for non-GETs | `test_broker_write_fence.py` | caught (24 failed) |
| M4 non-owner allowed to log in (`/login` dependency removed) | routes, login | caught (3 failed) |
| M5 takeover while the lock is held | L2b, L3, L4 | caught (4 failed) |
| M6 RESTRICTED not persisted at takeover (before the loops) | startup order, takeover, E3, C1, R6 | caught (10 failed) |
| M7 incomplete read treated as complete | R2–R5 | caught (10 failed) |
| M8 a predecessor's `submitting` claim (the Phase 2 analogue of "old-epoch `sending`") not treated as unresolved | C1 | caught (2 failed) |

## 10. Windows integration tests (WIN-INT)

| ID | Status | Evidence |
|---|---|---|
| **L2** lock release after a hard kill | **PASS (local Windows 11, not a dedicated machine)** | In the suite: 5 `os._exit(137)` runs. Manual `tests/winint_phase2.py l2-timing`: 20 runs, max 0.7 ms, median 0.5 ms. |
| **L3** path aliases | **PARTIAL PASS / PENDING.** Different-case path and directory junction: PASS (in the suite). `subst` drive alias: **PENDING** (needs a system-wide drive mapping; the user chose not to run it here). | `python tests/winint_phase2.py l3-subst X:` |
| **L6** non-local drive | **Stubbed PASS** (`GetDriveTypeW` returning removable, remote, CD-ROM, RAM disk, unknown and no-root; UNC paths). Real network share: **PENDING**. | `python tests/winint_phase2.py l6-share \\host\share\dir` |
| **R8** suspend / resume | **Simulated PASS** (wall-clock and monotonic jumps → `RESUMED_AFTER_SUSPEND`, GTT observations reset, Stage 1 re-run, NORMAL only after it passes). Real sleep/hibernate: **PENDING**. | `python tests/winint_phase2.py r8-sleep`, then sleep or hibernate ≥ 2 min |

The WIN-INT procedure:

- run on a controlled Windows machine, from a scratch checkout, with the app stopped;
- the runner installs the Phase 0 guard and uses only scratch databases in a temporary folder;
- record its PASS/FAIL lines in this report.

U3 (`time.monotonic()` across sleep) is answered by R8 on real hardware. The design checks both clocks
either way.

## 11. Migration backup and rollback procedure (operator)

**Before the first start of the Phase 2 build** (app stopped). This must be done by the operator. It was
**not** done during this work.

1. `python -m backend.db_maintenance backup` writes `backend/kite_terminal.db.manual_<time>_bak` and runs
   `PRAGMA integrity_check` on it.
2. `python -m backend.db_maintenance integrity --db backend\kite_terminal.db.manual_<time>_bak`. It must
   print `integrity_check: ok` and `foreign_key_check: 0 violation(s)`.
3. `python -m backend.db_maintenance status --db <that backup>`. It must print `schema:    v10 of v11`
   and list the v10 history.
4. Record the backup's size and SHA-256 (`Get-FileHash <backup> -Algorithm SHA256`) next to the date.
   Keep one copy off this machine.

**First start.** `migrate()` writes and verifies `kite_terminal.db.pre_schema_v11_bak`, then applies
v11.

- Expected log lines: `event=migration.backup schema_from=10 schema_to=11` and
  `event=migration.applied version=11 name=phase2_instance_coordination`.
- The start then continues: the instance lock is already held, then the takeover (epoch 1), RESTRICTED,
  the loops, and Stage 1.

**After the first start, verify:**

- `python -m backend.db_maintenance status` reports `v11 of v11` (with the app stopped, or at any time:
  it is read-only);
- System Diagnostics → `instance` shows `role=owner`, `epoch=1`, and the restriction clears once
  Stage 1 passes.

**Older builds.** Once v11 is applied, every pre-Phase-2 build refuses the database
(`DATABASE_SCHEMA_UNSUPPORTED`). To go back to an older build, use one of:

- **(a) Down-migration**, which keeps every trading row. Stop the app, then run
  `python -m backend.db_maintenance rollback-schema-v11 --confirm`.
  - It writes `…pre_rollback_v11_bak`, drops only the five Phase 2 tables and `schema_version` row 11,
    and verifies v10.
  - Only the coordination audit (instance history, startup runs and reads, the persisted restriction) is
    lost.
- **(b) Restore** `kite_terminal.db.pre_schema_v11_bak` or the manual backup. This loses every trade
  recorded after the backup.

## 12. Known limitations and unresolved broker questions

- **Legacy writes in flight at the moment of fencing** finish their local DB writes (D§7.3, disclosed).
  The optional `sqlite3` authorizer layer (U10) was not built.
- **The OS lock cannot see a copy elsewhere.** A copy of the database on another machine or path is
  caught only by the epoch and heartbeat (`INSTANCE_CONFLICT`, within one heartbeat or at takeover).
  Network and synced folders are refused or warned about.
- **Non-owner GET routes.** A non-owner serves only `/` and `/api/health`. A process that *lost*
  authority still serves read routes. One of them, the ticket chart, writes a cached URL; it is guarded
  like a write route.
- **Stage 1 runs alongside the loops.** It runs while the loops already run (D§8.2, deliberate).
  Classification is serialised with the reconciliation cycle by `CLASSIFICATION_LOCK`.
- **Default account only.** Stage 1 reconciles the default account. Other accounts, if any, stay
  RESTRICTED. The app has a single account today.
- **Attribution is reported, not enforced.** No baseline exists before Phase 7, so "exclusive" there
  means only "the broker total equals the app's quantity and no untagged order today".
- **Persisted restriction.** `HEARTBEAT_STALE` and `INSTANCE_*` are process reasons, held in memory:
  persisting them would need the very authority or database access that failed. Every other reason is
  persisted, and the account is RESTRICTED after any restart anyway.
- **Broker unknowns (unchanged):**
  - U1 DDPI/TPIN for GTT-triggered CNC sells: a live-trading blocker;
  - U5 second-client login invalidation;
  - **U6 proxy replay** (§13);
  - U7 ISIN presence;
  - U8 `get_gtts()` completeness and lag;
  - U14, the remaining P§10 items.
- **WIN-INT L3-subst, L6 real share and R8 real sleep** are PENDING (§10).

## 13. Phase 3 prerequisites

1. **U6.** Establish, from the NET-1 REST proxy's configuration and documentation (not by live broker
   requests), whether the proxy can retry or replay a non-idempotent POST/PUT/DELETE. If it can, that
   must be disabled before any journal-driven write.
2. Apply the D§11.2 dark-schema amendments (Q1–Q3) and renumber the dark `_m011_*`/`_r011_*` functions
   to v12 before registering the journal.
3. Complete the PENDING WIN-INT items (§10).
4. DDPI/TPIN (U1) and P§10 items 3, 4, 5 and 9 remain live-trading blockers. `LIVE_ENTRIES_ENABLED`
   stays `False`.

## 14. Final integrity checks

Run after the final regression (`final_integrity.py`, in the session scratchpad; read-only; protected
files checked by `os.stat` only).

| # | Check | Result |
|---|---|---|
| 1 | Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef`, unchanged |
| 2 | Pre-existing worktree inventory | All 53 original `git status` lines are present with the same status codes. 42 lines were added, all intentional: 31 newly modified files and 11 new files, listed in §2 and in the next row. Of the 53 pre-existing entries, 15 have changed content (files edited on purpose, §2); the other 38 are byte-identical to the baseline sha256. No ignored entry was added or removed (104 → 146 lines total, the difference being the 42 additions). |
| 3 | New files | `backend/instance_lock.py`, `backend/startup_reconciliation.py`, `docs/PHASE_2_IMPLEMENTATION_REPORT.md`, `tests/instance_lock_child.py`, `tests/winint_phase2.py`, and the six new test files |
| 4 | Additive edits | For each pre-existing modified file, the diff against its pre-edit copy removes only changed signature, import, decorator or pin lines (for example `db.py` +227/−0, `config.py` +20/−0, `paper_broker.py` +18/−1, `reconciliation.py` +30/−3, `conftest.py` +69/−2). No edited file has mixed line endings. |
| 5 | `backend/execution.py` | identical to HEAD (`git diff --quiet HEAD -- backend/execution.py`) |
| 6 | PK-25 frozen hashes | **13 / 13 match** |
| 7 | `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`, not edited) |
| 8 | Real `.env` / account database | Not opened, read, copied, migrated or connected to. Size and mtime of all 43 protected files (`.env`, `ui_prefs.json`, 41 `kite_terminal.db*`) are identical before and after. The real lock file `backend/kite_terminal.db.instance.lock` was never created. The guard reports `unexpected 0` in every run. |
| 9 | Broker calls | None. Tests use FakeKite or a recorded HTTP transport only. No Kite MCP tool or broker API was used. Child processes install the guard, which refuses non-loopback connects. |
| 10 | Staging / commits / pushes / stash | Nothing staged (`git diff --cached --name-only` is empty), no commit, no push, no merge, no stash (`git stash list` is empty). No reset, clean, checkout or restore. |
| 11 | Processes | No test process left running. One stray process from an early focused run was found and stopped (§9.4). |

## 15. Status

### **PHASE 2: PASS WITH PENDING WINDOWS TESTS**

**Established:**

- the approved §13 scope is implemented;
- migration v11 is registered and tested for apply, idempotency, refusal, interruption and rollback;
- the full regression passes (3430 passed, 0 failed, 6 explained skips);
- all 8 design mutation checks are caught;
- the CLI audit is complete;
- every integrity check passes;
- exits, cancellations, Emergency Exit and protection upkeep are shown to work while RESTRICTED.

**Pending, so this is not a plain PASS:**

- WIN-INT **L3** subst-drive alias;
- **L6** against a real network share;
- **R8** with a real sleep/hibernate.

All three need a controlled Windows machine (§10, runner `tests/winint_phase2.py`). L2 and the safe part
of L3 pass on this Windows 11 machine, which is not a dedicated test machine.

**Before the first real start:** the operator backup in §11, which has not been done.

**Before Phase 3:** U6 (proxy replay) and the other items in §13.

**Live trading: NO-GO.** `LIVE_ENTRIES_ENABLED = False`. DDPI/TPIN (U1) and P§10 items 3, 4, 5 and 9
remain blockers.
