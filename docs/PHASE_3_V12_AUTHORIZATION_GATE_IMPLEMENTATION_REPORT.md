# Phase 3: V12 authorization gate — implementation report

> **V12 was NOT registered. No database was migrated.** `MIGRATIONS` still ends at
> `(11, "phase2_instance_coordination", …)` and `LATEST_SCHEMA_VERSION` is 11. The production application was not
> started. `.env`, credentials, the account database and its backups were not opened. Every test database was
> temporary. Nothing was staged, committed, pushed or merged.

Date: 2026-11-11. Branch `feature/signal-detail-and-strategy-revision`, HEAD
`35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef` (unchanged).

## 1. What changed and why

Until now `init_db()` applied every pending migration, both on every app start and through
`db_maintenance migrate`. Appending v12 to `MIGRATIONS` (registration) would therefore have migrated the trading
database at the next start, with no operator step. The gate separates the two decisions:

- **Registered but pending → a start refuses.** `init_db()` raises `DatabaseMigrationAuthorizationRequired`
  (`DATABASE_MIGRATION_AUTHORIZATION_REQUIRED`) before any write. `main.on_startup` then releases the instance lock,
  logs `event=startup.refused code=DATABASE_MIGRATION_AUTHORIZATION_REQUIRED` and the operator instruction, and
  re-raises. uvicorn aborts startup: no takeover, no loops, no ticker, no broker session.
- **Only an explicit command migrates.** `python -m backend.db_maintenance migrate-v12 --authorize-v12`, with the app
  stopped.

User decisions (2026-11-11):
- **Empty databases are exempt.** A missing or brand-new file with no user tables holds nothing to protect, so it is
  created straight at the latest schema.
- **A pre-v11 database is refused.** On a registered build, a start refuses it with no writes, and the command needs
  history exactly v1..v11. A pre-v11 database must first be brought to v11 with the Phase 2 build.

## 2. Changed files

SHA-256 values are of the working-tree files at the end of this work. Line counts are against the pre-edit copies
saved in the session scratchpad. Line endings were preserved: LF files stayed LF and CRLF files stayed CRLF, with no
mixed endings.

| File | Change | +/− |
|---|---|---|
| `backend/db.py` | The gate (`DatabaseMigrationAuthorizationRequired`, `_AUTHORIZATION_GATED_MIGRATIONS`, `V12MigrationAuthorization`, `_valid_v12_authorization`, `require_startup_migration_authorization`), `migrate(conn, *, v12_authorization=None)`, the in-transaction R2-4 re-check, `V12_PREFLIGHT_OPEN_NULL_MODE_SQL`, the `init_db` precheck, and `v12_schema_problems` (read-only, placed in the dark section) | +126 / −2 |
| `backend/main.py` | `except db.DatabaseMigrationAuthorizationRequired`: release the lock, log, re-raise | +7 / −0 |
| `backend/db_maintenance.py` | `migrate-v12 --authorize-v12`; `migrate` reports the refusal (exit 1); `_R2_4_OPEN_NULL_MODE_SQL` becomes an alias of `db.V12_PREFLIGHT_OPEN_NULL_MODE_SQL` (one definition) | +292 / −8 |
| `tests/test_v12_authorization_gate.py` | **New**: 43 tests (§5) | new |
| `tests/test_schema_v12.py` | Under the simulated registration, v12 is applied through `_authorized_migrate` (`db.migrate` with the authorization) instead of `init_db()`; assertions otherwise unchanged | +21 / −11 |
| `tests/test_v12_registration_prep.py` | `_to_v12`: the same change | +5 / −2 |
| `tests/test_platform_pk22.py` | The FK-guard test appends a placeholder `latest + 1` = 12, which is now gated. While it collides, the test asserts the start refuses it, then reaches the same FK guard through the authorized `migrate()` | +14 / −2 |
| `tests/test_mp_harness_selftest.py` | H15 strips a SQLite `file:` URI (the new read-only precheck) to its file part, with `real_data_guard`'s own regex, before its path assertion | +5 / −1 |
| `docs/PHASE_3_V12_REGISTRATION_RUNBOOK_DRAFT.md` | An "AMENDED" note at the top: a start no longer applies v12 | +9 / −0 |
| `docs/PHASE_3_V12_AUTHORIZATION_GATE_IMPLEMENTATION_REPORT.md` | **New**: this report | new |

Not changed:
- `backend/execution.py` is byte-identical to HEAD.
- All 13 PK-25 hash-pinned files match `research/pk25/validation_manifest.json` (13/13).
- `LIVE_ENTRIES_ENABLED = False` (source constant).
- Every other pre-existing dirty or untracked file is unchanged against the Step 0 snapshot (§8).

## 3. The gate (backend/db.py)

- **`_AUTHORIZATION_GATED_MIGRATIONS = {12: "phase3_protection_journal"}`**, keyed by **version**. A v12 entry
  registered under another name is gated all the same (test 09).
- **`V12MigrationAuthorization(issued_by, version=12, name="phase3_protection_journal")`**: a frozen dataclass and
  the only key. `_valid_v12_authorization` requires all of:
  - that exact type (not a subclass or a lookalike);
  - `version == 12`;
  - the v12 name;
  - a non-blank `issued_by`.

  `True`, strings, dicts and integers are refused (test 07). There is no generic flag. `init_db` takes no
  authorization parameter. `main.py` never constructs one, and the only backend constructor is
  `db_maintenance.migrate_v12` (test 08).
- **`require_startup_migration_authorization(path)`** is the first statement of `init_db()`, ahead of
  `crypto.ensure_encryption_key()` and `get_connection()`. A missing file → return (fresh install). Otherwise it opens
  `file:<db>?mode=ro` with `PRAGMA query_only = ON` and runs `_check_history`, so a foreign or newer history still
  raises `DATABASE_SCHEMA_UNSUPPORTED`. It refuses if a gated version is pending and the database has a user table.
  It always closes its connection.
- **`migrate(conn, *, v12_authorization=None)`** is the second line of defence, used by every other caller. The same
  refusal comes right after `_check_history`, before the backup and before any write. Inside v12's own
  `BEGIN IMMEDIATE` transaction, before `apply`, the approved R2-4 query is re-run under the write lock: an open
  executed BUY with a NULL mode → `DatabaseMigrationFailed` naming the tickets, rolled back. Duplicate GTT ids are
  already re-checked in that transaction by `_m012_registered`.
- **On the unregistered build nothing is gated**, so v1..v11 run exactly as before (test 26 replaces the refusal
  builder with one that fails the test, then runs v10→v11 by start, by `migrate`, and by rollback-and-forward).

## 4. The command: `migrate-v12 --authorize-v12`

```
python -m backend.db_maintenance migrate-v12 --authorize-v12 [--db PATH]   # STOP THE APP FIRST
```

| # | Step | Refusal → result code, exit |
|---|---|---|
| 0 | `--authorize-v12` present, checked **before any filesystem access** (no `exists`, no lock file); the flag on any other command is refused | usage, 2 |
| 0b | the database file exists | usage, 2 |
| 1 | the instance lock (`instance_lock.held_for_maintenance`, never waits) | `ANOTHER_INSTANCE_RUNNING`, 3 |
| 2 | this build registers v12 (`MIGRATIONS` has 12 entries, the last is `(12, db._V12_NAME)`) | `V12_NOT_REGISTERED`, 1, **today's state** |
| 3 | history: exactly v1..v12 → read-only re-verification only, no backup, no write; exactly v1..v11 → continue; anything else | `ALREADY_APPLIED`, 0 / `HISTORY_REFUSED`, 1 |
| 4 | target `PRAGMA integrity_check` = ok; record FK violations and every table's row count | `TARGET_INTEGRITY_FAILED`, 1 |
| 5 | fresh backup `<db>.pre_v12_authorized_<stamp>_bak` (`db.backup_database`, with its own integrity check) | `BACKUP_FAILED`, 1 |
| 5b | independent verification of that file, reopened `mode=ro&immutable=1`: integrity ok, history v1..v11, the same FK count and the same row count in every table as the target; SHA-256 and size recorded | `BACKUP_VERIFICATION_FAILED`, 1 |
| 6 | DD-6 / R2-4 preflight **on the migration target**: `preflight_v12_counts` on the locked connection, under `query_only` | `PREFLIGHT_FAILED`, 1 (tickets / GTT ids listed) |
| 7 | `db.migrate(conn, v12_authorization=V12MigrationAuthorization(issued_by="db_maintenance migrate-v12 --authorize-v12"))`: one transaction (migrate()'s own `pre_schema_v12_bak` is also written) | `FAILED_ROLLED_BACK`, 1 (history re-read; a `KeyboardInterrupt` is re-raised after the audit) |
| 8 | verification: history exactly v1..v12 and `schema_status.up_to_date`; v12 objects exactly v12's with `idx_trade_tickets_unique_gtt`; the journal empty (no op, resolution, `protection_state` or `protection_*` event); integrity ok; FK violations not above before; every pre-existing table's row count unchanged (schema_version +1); new tables empty | `MIGRATED_VERIFICATION_FAILED`, 4 |
| 9 | audit (every outcome after the lock): `key: value` lines (stdout on success, stderr otherwise), a JSON file `<db>.v12_migration_audit_<stamp>.json`, and `event=v12_migration.result` in the log | — |

Success → `V12_APPLIED`, exit 0.

The audit records:
- the command, the database path, start and finish times and the result code;
- the history before and after;
- the target's integrity, FK count and row counts;
- the backup's path, size, SHA-256, integrity and FK count, and any problems;
- migrate()'s `pre_schema_v12_bak` with its SHA-256;
- the preflight lists;
- the verification problems;
- the error, if any.

The command:
- imports no broker or trading module (static check, and a fresh-process check, test 22);
- loads no `.env`;
- runs no startup invariant;
- mints no key.

## 5. Tests

`tests/test_v12_authorization_gate.py` has 43 tests (30 functions, some parametrized). Each runs on a temporary
database with registration simulated in-test, and an autouse fixture asserts `MIGRATIONS` ends at v11 afterwards.

| Requirement | Tests |
|---|---|
| V12 pending during normal startup | 01: `main.on_startup()` raises with the code; the log has `event=startup.refused code=…`; nothing after `init_db` ran (takeover, loops, spawn, `get_kite`, ticker, sector seed) and no key minting |
| Startup refusal without migration | 02, 04 (`init_db` and `migrate(conn)` both refuse), 05 (`db_maintenance migrate` → 1) |
| Lock release after refusal | 03: role `released`, no authority, the OS lock can be taken |
| Byte/content preservation | 02, 04: after a WAL checkpoint, the main file's SHA-256, the schema, every row and the set of backup/audit files are identical |
| Explicit authorization required | 07 (10 invalid forms), 08 (no generic bypass; `init_db` has no parameter; the only constructor is db_maintenance), 09 (renamed v12 gated), 06 (precheck is `init_db`'s first statement) |
| Missing authorization flag | 10: exit 2, no `Path.exists` call, no lock or database file; 11: the flag on other commands → 2 |
| Application already running | 12: a child process holds the lock → 3, `ANOTHER_INSTANCE_RUNNING`, not a byte written |
| Backup failure | 13: `OSError` from the backup → `BACKUP_FAILED`, v11 unchanged, lock free |
| Backup integrity failure | 14 (three cases): header destroyed, pages corrupted, and a valid file that does not match the target → `BACKUP_VERIFICATION_FAILED`, migrate() never reached |
| NULL-mode preflight | 15: `PREFLIGHT_FAILED`, the ticket listed; 16: with the preflight bypassed, the in-transaction re-check refuses and rolls back |
| Duplicate GTT-ID preflight | 17 (live and paper ids): `PREFLIGHT_FAILED`, ids named |
| Successful migration | 18: `V12_APPLIED`; exact v12 objects; every row kept; both backups hold the v11 image; the audit is complete and its SHA-256 matches the file; lock released |
| Interrupted migration and recovery | 20: `KeyboardInterrupt` inside v12 → `FAILED_ROLLED_BACK`, v11 intact, lock free, the rerun applies; 21: a process killed (`os._exit`) mid-v12 → v11 intact, the OS frees the lock, the rerun applies |
| Repeat migration | 19: second and third runs → `ALREADY_APPLIED`, main file SHA-256 unchanged, one backup only |
| V11 compatibility | 25 (unregistered: `V12_NOT_REGISTERED`, nothing written), 26 (v10→v11 by start and by `migrate`; rollback-schema-v11 and forward; the gate never consulted), 29 (a foreign history is still `DATABASE_SCHEMA_UNSUPPORTED`, nothing written) |
| Pre-v11 on a registered build | 27: start refuses ("below 11") and the command → `HISTORY_REFUSED` |
| Empty-database exemption | 28: a missing file and a 0-byte file → `init_db` creates v1..v12 |
| No broker calls during migration | 22 (fresh process: `KiteConnect` never constructed or used; no `kite_client`, `execution`, `main`, `paper_broker`, `order_monitor`, `trailing_engine`, `reconciliation`, `automation` or `ticker` module loaded), 23 (FakeKite call log unchanged; `get_kite` fails the test if called), 24 (AST import allow-list of db_maintenance) |
| Boundaries | 30: v12 not registered; `LIVE_ENTRIES_ENABLED = False` in the source |

**Mutation check:** 12 hand mutations, each run against its target tests. **12/12 caught.** Every file was restored
and SHA-256-verified.

| Mutant | Caught by |
|---|---|
| startup precheck removed | test 01 |
| migrate() gate removed | test 04 |
| any truthy authorization accepted | test 07 |
| backup verification skipped | test 14 |
| preflight not gating | test 15 |
| lock not released on refusal | test 03 |
| in-transaction NULL-mode re-check removed | test 16 |
| empty-database exemption removed | test 28 |
| command without the instance lock | test 12 |
| registration check skipped | test 25 |
| flag not required | test 10 |
| precheck blind to v12 | test 01 |

**Results** (counts from `--junitxml`; this suite's isolation summary replaces pytest's totals line):

| Run | Command | Result |
|---|---|---|
| New gate tests | `python -m pytest -p no:cacheprovider tests/test_v12_authorization_gate.py` | **43 passed** |
| Focused | the above + `test_schema_v12`, `test_v12_registration_prep`, `test_preflight_v12`, `test_schema_v11`, `test_cli_instance_lock`, `test_instance_lock`, `test_dark_protection_schema`, `test_platform_pk22`, `test_live_entry_gate` | **577 passed, 0 failed, 3 skipped** (WIN-INT) |
| Full approved suite | `python -m pytest -rs -p no:cacheprovider --ignore=tests/test_calendar_parity.py --ignore=tests/test_dmb_handoff_roundtrip.py` | **4236 passed, 0 failed, 6 skipped** (4242 collected; 1164 s). The baseline was 4193 passed + 6 skips; +43 is the new file |

The six skips are the known ones: 3 symlink-privilege skips (`test_dmb_inbox`) and 3 WIN-INT pending
(`test_instance_lock`). Isolation summary: guard refusals 19 (unexpected 0), network refusals 1 (the same as the
first run), child processes 155 reports with protected opens 0 and orphans 0.

**The first full run found 2 failures, both caused by this change and both fixed before the run above:**
1. `test_platform_pk22::test_a_migration_that_adds_foreign_key_violations_is_refused`. Its placeholder migration
   took version 12, which the gate is keyed on. It was adapted (§2), and the generic FK guard is still asserted.
2. `test_mp_harness_selftest::test_H15`. The harness test read the precheck's `file:…?mode=ro` URI as a literal
   path. The URI is now stripped as `real_data_guard` does; the data file was under `tmp_path` all along.

## 6. Backup handling

- **Taken by the command:** `<db>.pre_v12_authorized_<YYYYmmddHHMMSSffffff>_bak` (never overwritten:
  `backup_database` appends a stamp if the name exists). It is SQLite's online backup, integrity-checked by
  `backup_database`, then verified independently by the command (§4 step 5b). Its SHA-256 is in the audit. Any
  failure stops before the preflight and the migration. The file is left in place for inspection.
- **migrate()'s own** `<db>.pre_schema_v12_bak` (unchanged behaviour, the name the runbook expects) is also written,
  inside step 7. Its SHA-256 is recorded too. Both hold the v11 image (test 18).
- **The operator backup** (runbook §2.1 / proposal P2) remains a separate, earlier precondition. The command does not
  replace it.

## 7. Recovery procedure

| Situation | State | What to do |
|---|---|---|
| A start refused with `DATABASE_MIGRATION_AUTHORIZATION_REQUIRED` | unchanged (v11), lock released | Either run the old (unregistered, Phase 2) build, or, with authorization, stop and run `migrate-v12 --authorize-v12` |
| `V12_NOT_REGISTERED`, `HISTORY_REFUSED`, `TARGET_INTEGRITY_FAILED` | unchanged | Resolve the cause; a pre-v11 database is brought to v11 with the Phase 2 build |
| `BACKUP_FAILED` / `BACKUP_VERIFICATION_FAILED` | unchanged (a damaged backup file may remain) | Fix disk space or permissions, delete or inspect the bad backup, rerun |
| `PREFLIGHT_FAILED` | unchanged (the verified backup remains) | Resolve the listed tickets in Ticket History, rerun |
| `FAILED_ROLLED_BACK`, an interrupt, or a killed process | v11, nothing half-applied (one transaction); the OS releases the lock | Check `db_maintenance status` (v11 of v12), fix the cause, rerun |
| `MIGRATED_VERIFICATION_FAILED` (exit 4) | v12 committed but not as expected | Do not start the app. Restore the verified `pre_v12_authorized_*_bak` (SHA-256 in the audit) over the database with the app stopped, then investigate. `rollback_schema_v12` is not on the CLI before registration |
| Re-running after success | `ALREADY_APPLIED`, nothing written | — |

## 8. Safety checks performed

- **Step 0 snapshot (scratchpad):** pre-edit copies of the touched files, plus the SHA-256 of all 138 `git status`
  entries, `execution.py` and the PK-25 files. Under this brief's restrictions there was no git ref and no DB copy.
- **Final comparison against that snapshot** (`git status --porcelain -uall`, 138 → 140 entries):
  - changed: exactly the 8 intended files of §2;
  - new: exactly `tests/test_v12_authorization_gate.py` and this report;
  - unexpected changes: none; vanished: none;
  - HEAD unchanged;
  - `backend/execution.py`: `git diff --quiet` clean, its CRLF-normalised SHA-256 equal to HEAD's blob, and its
    bytes unchanged since the snapshot;
  - PK-25: 13/13 match the manifest's normalised hashes.
- No test or command touched `backend/kite_terminal.db`, its backups or `.env`. `real_data_guard` reported 0
  unexpected refusals.
- No broker call: FakeKite only, the fence in place, `get_kite` trapped in test 23, and `KiteConnect` trapped in a
  fresh process in test 22.

## 9. Remaining limitations

1. **Pre-v11 path (user decision).** On a registered build, a pre-v11 database cannot be advanced by the registered
   build: it needs the Phase 2 build first. The real database is reported at v11 (proposal P2), so this is not
   expected to bite.
2. **Two migration backups.** The command's verified backup and migrate()'s `pre_schema_v12_bak`. This is deliberate,
   since the runbook expects the latter, but it doubles the disk use.
3. **The empty-database exemption (user decision).** If the database file were deleted, the app would create a fresh
   v12 database, just as it creates a fresh v11 one today.
4. **mode=ro sidecars.** The startup precheck opens the file `mode=ro`. On a WAL database this can create
   `-wal`/`-shm` sidecars if they are absent. The main file is never written: the byte-preservation tests compare it.
5. **The R2-4 in-transaction re-check** runs whenever v12 is applied (on an empty database it finds nothing). It
   also applies inside the existing simulated-registration tests, whose seeded tickets have a mode.
6. **`rollback-schema-v12` is still not on the CLI** (R6 M-9; test_schema_v12 test_a2). It belongs to the
   registration change.
7. **Registration itself is still required, and separately authorized.** Until then the command always ends at
   `V12_NOT_REGISTERED`. The registration change must update the static pins (test_schema_v12 test_a1, the autouse
   "v12 stays unregistered" fixtures) and the placeholder branch in `test_platform_pk22`.
8. **The H15 harness change.** The Phase 0 harness test now understands SQLite URIs, as `real_data_guard` always
   has. The protected-file check itself is unchanged.

## 10. Repository state

- Branch: `feature/signal-detail-and-strategy-revision`
- HEAD: `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef` (unchanged; no commit, no ref created)
- Index: empty (nothing staged)
- `git status --porcelain -uall`: 140 entries = 69 modified tracked (` M`) + 71 untracked (`??`). Before this work
  there were 138 (69 + 69).
  - Tracked files this work modified on top of the existing dirty tree: `backend/db.py`, `backend/main.py`,
    `backend/db_maintenance.py`, `tests/test_platform_pk22.py`.
  - Untracked files this work modified: `tests/test_schema_v12.py`, `tests/test_v12_registration_prep.py`,
    `tests/test_mp_harness_selftest.py`, `docs/PHASE_3_V12_REGISTRATION_RUNBOOK_DRAFT.md`.
  - New untracked files: `tests/test_v12_authorization_gate.py` and this report.
  - Every other entry is byte-identical to the Step 0 snapshot.

**Stopped here.** No registration, no migration of any real database, no commit. V12 registration awaits separate
authorization.
