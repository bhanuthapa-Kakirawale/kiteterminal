# Phase 3 — V12 registration remediation report

> **V12 IS NOT REGISTERED.**
> - `MIGRATIONS` still ends at `(11, "phase2_instance_coordination", …)` and `LATEST_SCHEMA_VERSION` is 11.
> - The registration diff (§11) is prepared and validated, but **not applied** in the repository.
> - No real database was opened or modified. No broker was called. Nothing was staged or committed.
> - Registering V12, and separately migrating the real database, each need their own explicit authorization.

- **Date:** 2026-11-11.
- **Branch and HEAD:** `feature/signal-detail-and-strategy-revision` @ `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef`, both unchanged.
- **Mandate:** "V12 Registration Preparation — Full Remediation Mandate", answering the V12 registration readiness audit of 2026-11-11.
- **Audit verdict:** authorization gate GO; registration as scoped NO-GO.
- **Audit source:** the audit's final message was recovered from session `ff9bf367`'s transcript, because `/clear` had removed it from this session. Its findings were the work specification.

User decisions taken in this session:

1. **Validation.** Validate the registered build in a **throwaway copy** of the repository's 352 git-visible files: no `.db`, `_bak` or `.env`, which was asserted. The exact diff is applied there and the full suite is run. The diff is never applied in the repository.
2. **The wrapper.** Add the late-bound wrapper to `db.py` now, defined but **not** listed in `MIGRATIONS`.
3. **The GTT writers.** Fix **both** GTT-id writers in `routes/tickets.py`.

---

## 1. Findings addressed

| Audit finding | Resolution | Where |
|---|---|---|
| The proposed `MIGRATIONS` line raises `NameError` at import | Late-bound wrapper `_migration_v12_protection_journal`, defined above `MIGRATIONS`, unlisted | §3 |
| Registration breaks the dark-schema helpers, v11 rollbacks, old-database restarts, duplicate-GTT tests and `_registered()` | `tests/schema_builds.py` (builds, pin, helpers), the `v11_database` marker, and a reworked `journal()`; 20 test files adapted | §2, §8 |
| The saved rollback CLI is stale; copying it would delete `preflight-v12` and `migrate-v12` | **Ported** into the current `db_maintenance.py`, with 44 new tests | §4 |
| WAL/SHM restore hazard (inferred, untested) | **Reproduced**, then made safe with the new `restore-backup` command and 16 tests | §5 |
| No disk-space check | A preflight in `migrate-v12`, `rollback-schema-v12` and `restore-backup` | §6 |
| A failure after COMMIT is labelled `FAILED_ROLLED_BACK` | The result is read back from the committed history | §6 |
| `migrate()`'s own backup failure is mislabelled | `MigrationBackupFailed` gives `BACKUP_FAILED` | §6 |
| `TARGET_INTEGRITY_FAILED`, `ALREADY_APPLIED_VERIFICATION_FAILED` and exit 4 are untested | Tested | §6 |
| The rollback backup has no independent verification and no audit | Independent verification and an audit JSON | §4 |
| `routes/tickets.py:1323` can raise an unhandled `IntegrityError` on a duplicate GTT id | Both writers (`:1292` and `:1323`) handled | §7 |
| DD-6 / R2-4 PASS from the 11:36 snapshot is stale | Recorded as historical only; freshness tested | §7, runbook §4.3 |
| Hidden failures from 85 hard-coded GTT ids | A full registered run found **one** real clash (`test_gtt_reconciliation::test_14b`) and two preflight tests; the 27 files were not swept statically | §8 |
| Stale docs (runbook, proposal, `db_maintenance` docstring, `db.py` comments) | Rewritten or corrected. The `db.py` comments that describe registration change in the diff. | §2 |

Two failures appeared on the registered build that the audit did not predict:

- **`test_mp_coordination::MP_C1`** called `apply_dark_protection_schema` directly. It now uses `journal()`.
- **`test_platform_pk22` hot-query test.** On a v12 database SQLite's planner chooses `idx_trade_tickets_unique_gtt` (covering) for `WHERE kite_gtt_id = ?`. Either index is now accepted. This is benign: it is still an index search, never a scan.

## 2. Files changed

These counts are this task's own changes, measured against the Phase 0 pre-edit copies with line endings normalised. They exclude the user's pre-existing worktree work.

| File | Kind | +/− | What |
|---|---|---|---|
| `backend/db.py` | modified | +42 / −2 | late-bound wrapper (unlisted); `MigrationBackupFailed`; `V12_TABLE_NAMES`, `v12_objects_state` |
| `backend/db_maintenance.py` | modified | +549 / −33 | `rollback-schema-v12`; `restore-backup`; disk-space preflight; history-based classification; shared audit writer; docstring |
| `backend/routes/tickets.py` | modified | +63 / −6 | both GTT writers keep DD-6 (§7) |
| `tests/schema_builds.py` | new | +156 | builds, pin, helpers (§3) |
| `tests/test_rollback_v12_cli.py` | new | +520 | 44 tests (§4) |
| `tests/test_db_restore_wal.py` | new | +435 | 16 tests (§5) |
| `tests/test_migrate_v12_failure_paths.py` | new | +232 | 14 tests (§6) |
| `tests/test_gtt_unique_insertion.py` | new | +190 | 12 tests (§7) |
| `tests/conftest.py` | modified | +40 / −3 | `v11_database` marker, `v11_env`, `_build_restored` guard |
| `tests/protection_helpers.py` | modified | +8 / −1 | `journal()` works on either build |
| `tests/test_schema_v12.py` | modified | +70 / −46 | registered_build, pin test a1, a1b, a2 rewritten, Phase 2 build where the subject is it |
| `tests/test_v12_authorization_gate.py` | modified | +28 / −12 | marker, build guard, test_24 allows `shutil`, 25/26 Phase 2 build, 30 pin, child entry |
| `tests/test_v12_registration_prep.py` | modified | +15 / −8 | marker, build guard, RP03 Phase 2 build |
| `tests/test_dark_protection_schema.py` | modified | +34 / −8 | marker, 1b pin, 2b/12 Phase 2 build, 11b wrapper allowance |
| `tests/test_schema_v11.py` | modified | +19 / −3 | the whole module as the Phase 2 build (monkeypatch-based) |
| `tests/test_platform_pk22.py` | modified | +29 / −13 | pins, `restart_old_database`, backup names, index alternative |
| `tests/test_live_preflight.py` | modified | +19 / −11 | pin; `migrate` test as the Phase 2 build |
| `tests/test_cli_instance_lock.py` | modified | +15 / −3 | lock matrix: rollback-schema-v12, restore-backup |
| `tests/test_preflight_v12.py` | modified | +4 / −0 | marker (v11 backups, duplicates possible) |
| `tests/test_protection_s4.py`, `test_protection_s9_static.py`, `test_ticket_integrity.py` | modified | small | pins read `expected_latest()`; S4_T22 marker |
| `tests/test_mp_coordination.py` | modified | +2 / −2 | MP_C1 uses `journal()` |
| `tests/test_live_safety.py`, `test_partial_fills.py` | modified | +3/−2, +2/−1 | `restart_old_database` |
| `tests/test_gtt_reconciliation.py` | modified | +2 / −0 | test_14b marker (legacy duplicate state is v11-only) |
| `docs/PHASE_3_V12_REGISTRATION_RUNBOOK_DRAFT.md` | rewritten | +272 / −166 | the revised runbook |
| `docs/PHASE_3_V12_FINAL_REGISTRATION_PROPOSAL.md` | modified | +36 / −12 | §1, §4, §5 corrected |
| `docs/TESTING.md`, `DEPLOYMENT.md`, `DMB_KITE_OPERATIONAL_RUNBOOK.md` | modified | +29, +12, +13 | short entries |
| `docs/PHASE_3_V12_REGISTRATION_REMEDIATION_REPORT.md` | new | — | this report |

Totals: 32 files, +3,138 / −339.

- **Not changed** (verified in §10): `backend/execution.py`, `backend/live_entry_gate.py`, `backend/main.py` and the 13 PK-25 files.
- **Line endings:** every file keeps its original style.
- **One correction:** `tests/test_live_safety.py` originally had one CRLF line among 2,043 LF lines, and the Edit tool normalised the whole file. It was rebuilt at byte level from its pre-edit copy, keeping that single CRLF. Its diff is now +3/−2.

## 3. Migration architecture

```python
# backend/db.py, just above MIGRATIONS (added now; deliberately NOT listed)
def _migration_v12_protection_journal(conn: sqlite3.Connection) -> dict:
    return _m012_protection_journal(conn)
```

**Why a wrapper.** `MIGRATIONS` is built when the module is imported, and `_m012_protection_journal` is defined ~900 lines later. The wrapper looks the name up only when it is called.

What that gives:

- **The import succeeds.** In the registered copy, `from backend import db` gives `LATEST_SCHEMA_VERSION == 12` and `MIGRATIONS[-1][2].__name__ == "_migration_v12_protection_journal"`.
- **Failure injection still works.** A test that substitutes `_m012_protection_journal` reaches the code through the entry (test A1b).
- **It follows the existing pattern** of `_migration_v11_instance_coordination`.
- **`registered=True` stays the default.** The wrapper passes nothing, so the bare function's keyword default applies.

**Rejected alternative:** moving `MIGRATIONS` below line ~3145. That is a larger diff and would move module-level state that other code reads.

**Function-identity checks:**
- `test_schema_v12::test_a1` pins `MIGRATIONS[11] == (12, "phase3_protection_journal", db._migration_v12_protection_journal)` once registered, and that no dark function is ever an entry.
- `test_dark_protection_schema::test_11b` now allows exactly one reference before the dark section: the wrapper's `return` line, checked by AST to be a single `return _m012_protection_journal(conn)`.

**Also in `db.py`** (this task):
- `MigrationBackupFailed(DatabaseMigrationFailed)`, raised when `migrate()`'s or `rollback_schema_v12()`'s own backup fails before any `BEGIN`. Being a subclass, every existing caller is unaffected.
- `V12_TABLE_NAMES` and `v12_objects_state()`, read-only accessors for `db_maintenance`.

`_AUTHORIZATION_GATED_MIGRATIONS`, `init_db`, `migrate()`'s gate and `_m012_registered` are unchanged.

**Test architecture (`tests/schema_builds.py`).** One suite now runs unchanged on both builds:

- **The pin.** `V12_REGISTERED_IN_THIS_BUILD = False` is the only registration-state pin. Every "latest version" test reads `expected_latest()`.
- **Builds.**
  - `phase2_build(db)`: a `MIGRATIONS[:11]` context, restored by hand.
  - `phase2_build_patched(monkeypatch, db)`: the same, for tests that also monkeypatch `MIGRATIONS`, so the undo order is correct.
  - `registered_build(db, apply=None)`: the exact final entry; a no-op once v12 is registered.
- **Databases.**
  - `authorized_migrate`, `to_v11`, `to_v12` (on temporary databases only).
  - `restart_old_database(db)` replaces `init_db()` after `as_pre_pk22_database`. The Phase 2 build brings the database to v11; on the registered build, v12 is then applied the authorized way.
- **Fixtures** in `tests/conftest.py`:
  - the `v11_database` marker and the `v11_env` fixture: `env` creates its database **with the Phase 2 build**, so it is exactly what that build makes, not a v12 database rolled back;
  - the suite-wide autouse guard `_build_restored`: any test that leaves `MIGRATIONS` or `LATEST_SCHEMA_VERSION` changed fails, instead of leaking into later tests. That leak happened once while this work was being developed, and the guard now catches it.
- **The dark-schema helper.** `protection_helpers.journal(env)`: on a v12 database it asserts `v12_schema_problems(empty_journal=False) == []`; on a v11 database it applies the dark schema.

## 4. Rollback CLI

**Command:** `python -m backend.db_maintenance rollback-schema-v12 --confirm [--db PATH]`.

It is a port into the current file; the stale saved copy was not copied over. `preflight-v12`, `migrate-v12`, the V12 gate and every V11 command are unchanged.

Behaviour, in order:

1. **`--confirm`** is checked **before any filesystem access** (test r01 spies `Path.exists`/`stat`, `sqlite3.connect`, the lock and `_connect`).
2. **The lock.** In `WRITING_COMMANDS`, so `held_for_maintenance` applies: exit 3 while the app runs (r02, and the CLI lock matrix).
3. **Refusals, each audited:**
   - `V12_NOT_REGISTERED`;
   - `HISTORY_REFUSED` (not exactly v1..v12, a renamed row, a gap);
   - `V12_SCHEMA_REFUSED` (objects not exactly v12's);
   - `TARGET_INTEGRITY_FAILED`;
   - `REFUSED_HELD_DATA` (with counts): a journal row, a resolution row (orphans included), a `protection_state` value, any of the 13 `protection_*` events;
   - `DISK_SPACE_INSUFFICIENT`.
4. **A fresh backup**, `pre_rollback_v12_authorized_<µs>_bak`, **verified independently**. It is reopened immutable and its integrity, v12 history, FK count, every row count and SHA-256 are checked (`BACKUP_FAILED` / `BACKUP_VERIFICATION_FAILED`).
5. **`db.rollback_schema_v12`.** It takes its own backup, **re-checks held data under the write lock** (`ROLLBACK_REFUSED`, r07), and runs in one transaction.
6. **The result is read back from the committed history.**
   - `FAILED_ROLLED_BACK`: still v12 with the objects intact.
   - `ROLLBACK_VERIFICATION_FAILED` (exit 4): committed to v11 but the call failed afterwards, or verification failed.
   - `STATE_UNKNOWN` (exit 4).
7. **Post-verification:**
   - history v11;
   - no v12 object;
   - integrity ok;
   - FK violations not increased;
   - every v11 table's row count unchanged (`schema_version` −1);
   - no new table.
8. **An audit JSON**, `<db>.v12_rollback_audit_<µs>.json`, plus stdout or stderr and a log line.

**Tests:** `tests/test_rollback_v12_cli.py`, 44 tests, all on temporary databases. They run on either build, with env created at v11 by the Phase 2 build and v12 applied the authorized way.

| Mandate item | Test(s) |
|---|---|
| Missing confirmation | r01 |
| Lock held | r02, r02b; `test_cli_instance_lock` matrix |
| Backup creation failure | r04; r05c (the rollback's own backup) |
| Backup verification failure | r05 (header destroyed, pages corrupted, valid but not the target), r05b (unreadable) |
| Every protection-data refusal condition | r06 (4 kinds incl. an orphan resolution), r06b (each of the 13 event types) |
| Held data re-checked inside the transaction | r07 |
| Invalid migration history | r08a (v11), r08b (renamed row 12), r08c (gap), r08d (unregistered build), r08e (v13 build), r08f (objects), r08g (integrity) |
| Interrupted rollback | r09 (`RuntimeError`, `KeyboardInterrupt`; then a rerun recovers), r10 (child process `os._exit` mid-transaction) |
| Failure after COMMIT never reported as rolled back | r09b, r09c |
| Row-by-row preservation of V11 trading data | r03 (every row of every table, `sqlite_sequence`, the AUTOINCREMENT high-water mark; the Phase 2 build accepts the result) |
| v12 rollback, then v11 rollback, then forward | r11 (v12 → v11 → v10 → `migrate` on the Phase 2 build → v11 → `migrate-v12` → v12, trading rows identical) |
| Disk space | r05d |
| No broker calls | r12 (fresh child: `KiteConnect` never constructed, no trading module imported), r13 (FakeKite untouched) |

The saved RP05–RP07 are rewritten as r08d, r11 and r01/r02. The function-level groups F1–F5 and G1–G5 in `test_schema_v12` are kept.

## 5. WAL / SHM restore safety

**Observed on this machine** (SQLite 3.50.4, Python 3.13.14), on temporary files only:

| # | Behaviour | Test |
|---|---|---|
| 1 | A read-only open of a WAL-mode file with no sidecars creates an **empty** `-wal` and a `-shm` (32 KB), which stay after close. The main file is byte-identical. | w01 |
| 2 | With a non-empty committed WAL, the read-only startup check reads the true history: a row removed only in the WAL gives "schema version 10". The `-wal` bytes are unchanged. | w02 |
| 3 | An `immutable` open **ignores** the WAL. | experiment; why w09 refuses |
| 4 | `migrate-v12` on a target whose newest rows exist only in the WAL: the backup holds them, is a single self-contained file, and its row counts equal the target's. | w03 |
| 5 | `db_maintenance backup` with a non-empty WAL gives the target's row counts. | w04 |
| 6 | **The hazard, reproduced.** A crashed v12 set (`-wal` holding the migration) plus a naive copy of the v11 backup over `kite_terminal.db` replays the stale WAL. The result passes `integrity_check` but **is at v12, not the backup**. | w05b (negative control) |

**The procedure:** `python -m backend.db_maintenance restore-backup --backup FILE --confirm [--db PATH]`. It is documented in full in the runbook §7. In summary:

1. Arguments are checked before any filesystem access, then the lock: the app must be stopped.
2. **The backup is verified before restoring.** Opened immutable, it must have no non-empty `-wal` or `-journal` of its own, pass integrity, and carry a history this build accepts. Its FK count, row counts and SHA-256 are recorded.
3. Disk space.
4. A staging copy, re-verified.
5. **The original set is renamed aside together** (`.db`, `-wal`, `-shm`, `-journal` → `<db>.pre_restore_<µs>_orig` plus suffixes). The original stays complete; its WAL is still paired with it (w05 opens it and finds the WAL-only row). **No stale sidecar can keep the target's name.**
6. The staging copy is moved into place with `os.replace`, and no sidecar may then exist.
7. **Post-restore verification:** SHA-256 equal to the backup's, integrity, history, FK count, row counts.
8. If verification fails, the restored file is set aside and the original set is renamed back (`RESTORE_VERIFICATION_FAILED`, w06). If even that fails, the result is `RESTORE_FAILED_MANUAL_RECOVERY`, with every file name and the manual steps (runbook §7, §9).
9. A database still open elsewhere is never replaced (`RESTORE_IN_USE`, w10).

Further tests: w05 (stale sidecars: exactly the backup, row by row, correct V11 history, integrity ok, FK 0, accepted by the Phase 2 build), w07 (no filesystem access before the argument check), w07b, w08 (lock), w09/w09b/w09c (backup refusals), w11 (disk), w12.

## 6. Backup and failure-path hardening (`migrate-v12`)

- **Disk-space preflight** before the first backup: free space ≥ 3 × (database + `-wal`) + 64 MB, which covers two full backups plus WAL growth. Otherwise `DISK_SPACE_INSUFFICIENT` and nothing is written (f01, f01b). Rollback and restore have the same check (r05d, w11).
- **The result is decided from the committed history and the v12 objects**, read on a fresh read-only connection after any exception from `db.migrate`:

| State read back | Result | Exit |
|---|---|---|
| exactly v12 | `MIGRATED_VERIFICATION_FAILED` (post-checks recorded) | 4 |
| exactly v11, no v12 object, `MigrationBackupFailed` | `BACKUP_FAILED` | 1 |
| exactly v11, no v12 object | `FAILED_ROLLED_BACK` | 1 |
| v11 with v12 objects, or unreadable | `STATE_UNKNOWN` | 4 |

**Tests:** `tests/test_migrate_v12_failure_paths.py`, 14 tests.

| Path | Test |
|---|---|
| `migrate()`'s internal backup failure | f04; f04b (the subclass) |
| Target integrity failure | f02 |
| Already-applied verification failure | f03 |
| Failure after COMMIT | f05 (exception), f06 (`KeyboardInterrupt`, re-raised) |
| Post-checks fail | f07 |
| Unreadable state | f08 |
| History v11 with objects present | f09 |
| A genuine rollback | f10 |
| Fresh preflight on the target | f11 |
| DD-6 inside the transaction | f12 |
| Backup creation failure, backup verification failure, `PREFLIGHT_FAILED` | existing gate tests 13–17, kept |

`FAILED_ROLLED_BACK` is never reported once v12 has committed: f05 and f06 here, r09b for the rollback.

## 7. Production GTT insertion, and DD-6 / R2-4

**Assessment.** On v12, `idx_trade_tickets_unique_gtt` turns a GTT id held by two tickets into `sqlite3.IntegrityError`. The approval route writes a broker GTT id in two places:

- **`_record_execution_result` (`:1292`).** After the broker accepted the order, via `execution.update_ticket_if`. `execution.py` is frozen and unchanged; the handling is in the route. An unhandled error here would be a 500 that leaves a real order and a broker GTT unlinked.
- **`_reconcile_late_result` (`:1323`).** The late attach.

**Evidence on uniqueness:**
- **Paper ids** are `PAPER-G-<n>` from `paper_gtts` (AUTOINCREMENT + UNIQUE). Nothing deletes, drops or resets `paper_gtts` (searched), so paper ids do not repeat within one database.
- **Live ids** are Kite trigger ids. There is **no evidence** that they are unique across accounts or over time, so they are not assumed to be.
- The `trade_tickets.kite_gtt_id` column is `INTEGER` (paper ids are stored as text).

**The response.** Both writers keep DD-6, "a GTT protects at most one ticket", on **any** schema:

- **At `:1292`:** an explicit holder check first. If another ticket holds the id, the same compare-and-set is written **without** the GTT link. The `IntegrityError` is the backstop for a race: only that statement fails, nothing else in the transaction is lost. An `IntegrityError` from any other constraint is re-raised.
- **At `:1323`:** `AND NOT EXISTS (… kite_gtt_id = ? AND id != ?)` in the `UPDATE`. It is a single statement, so it is atomic. The `IntegrityError` is the backstop.
- **In both cases** the order stays tracked, the GTT is **not** linked to a second ticket, and an event plus an **urgent `AMBIGUOUS_GTT_OWNERSHIP`** reconciliation finding (`CHECK KITE`) names the broker GTT and the ticket already holding it. It is never a 500.

**The index is not weakened.**

**Tests:** `tests/test_gtt_unique_insertion.py`, 12 tests. Each runs with the unique index and on a plain v11 database without it. Dry run only: no order or GTT write reaches Kite (g01).

| Case | Test |
|---|---|
| A duplicate returned for an approved order (the real paper path: a fresh env's first paper GTT is `PAPER-G-1`) | g01 |
| A unique id is unchanged | g02 |
| The race that hits the index backstop | g03 |
| A foreign `IntegrityError` is not swallowed | g04 |
| Late attach, duplicate vs unique | g05, g06 |
| **Concurrent insertion**: two threads with their own connections race to attach one id; exactly one links it, the other is flagged, nothing raises | g07 |

**DD-6 / R2-4.** The 11:36 `manual_20261111113642_bak` PASS is recorded as **historical evidence only** (runbook §4.3), not as approval of the current database. The real migration command does three things:

1. It runs the preflight **on its target itself**, freshly, after its own verified backup (f11).
2. It repeats the NULL-mode check **inside the v12 transaction** (gate test 16).
3. It repeats the duplicate-GTT check **inside the transaction** (`_m012_registered`; f12 commits a duplicate after the preflight passed, and it is refused with the database left at v11).

No real database was accessed.

## 8. Focused and full regression results

The earlier 4,236-pass run is **not** used as evidence. Every number below is from this session, after the last code change.

**Command (both builds):**

`python -m pytest -rs -p no:cacheprovider --ignore=tests/test_calendar_parity.py --ignore=tests/test_dmb_handoff_roundtrip.py`

The two standing exclusions are kept. Both are cross-repository Daily Market Byte tests (`docs/TESTING.md`).

| Run | Build | Tests | Passed | Failed / errors | Skipped | Unexpected guard refusals | Protected opens | Orphan processes | Time |
|---|---|---|---|---|---|---|---|---|---|
| Full, repo | V12 **unregistered** (as committed state + this work) | 4,332 | **4,326** | **0** | 6 (the known 6) | 0 | 0 | 0 | 1,488 s |
| Full, registered copy | **registration diff applied** (§11) | 4,332 | **4,326** | **0** | 6 (the known 6) | 0 | 0 | 0 | 1,523 s |

- **The registered copy is exactly what is proposed.** A byte check after the runs showed 348 files identical to the repo, the 3 registration files exactly equal to the repo file with §11 applied, and nothing else different. The only exception is this report, written after the runs started.
- **Isolation summary, identical on both runs:** guard refusals 19 (15 decoy, 4 expected, 0 unexpected); 1 network refusal (expected); 155 child guard reports; 0 missing reports.

**Skips: exactly the six approved ones, explained:**

| Skip | Why |
|---|---|
| 3 × `test_dmb_inbox.py:384` | Creating a symlink needs a privilege this machine lacks (WinError 1314). |
| `test_instance_lock.py:194` (L3) | WIN-INT pending: needs a system-wide `subst` drive mapping on a controlled machine. |
| `test_instance_lock.py:237` (L6) | WIN-INT pending: needs a real network share. |
| `test_instance_lock.py:678` (R8) | WIN-INT pending: needs a real sleep or hibernate. |

There were no other skips and no unexpected failures.

**History of the runs, disclosed:**

- **The first full run on the registered copy**, before the test rework, gave 565 failures in 15 files. All were in files the audit predicted, except `test_mp_coordination::MP_C1` and the `test_platform_pk22` planner case (§1).
- **The focused rerun** on the registered copy then found 44 failures with two causes:
  - **A test-isolation leak this work introduced.** A hand-restored Phase 2 build combined with a monkeypatched `MIGRATIONS`: `monkeypatch`'s late undo left the truncated list in place for every later test. That broke the `11 == 12` pins and every multi-process child. It was fixed with `phase2_build_patched`, and the new suite-wide `_build_restored` guard now fails any such test.
  - **Five new GTT tests depended on the time of day.** A dry-run entry after the market closes needs an AMO confirmation. They now use `market_open`.
- **Two further genuine registered-build cases** then surfaced: `test_dark_protection_schema` 2b and 12, whose subject is the Phase 2 build. They were fixed by running them as that build.
- The focused rerun of the repo found only the same 5 time-of-day failures.

## 9. Mutation-test results

**How they ran:**
- in a clone of the registered copy only, never in the repository;
- one mutation at a time, each followed by its targeted tests;
- each file reverted afterwards, and the three mutated files confirmed byte-identical to the registered copy at the end.

**Result: 21 / 21 killed.**

| Mutation | Killed by |
|---|---|
| `rollback-schema-v12` dropped from `WRITING_COMMANDS` (no lock) | r02, r02b |
| `--confirm` check skipped | r01 |
| Held-data pre-check skipped | r06 (the result would be `ROLLBACK_REFUSED`, not `REFUSED_HELD_DATA`) |
| Held-data re-check inside the write transaction skipped (`db.py`) | r07 |
| Registration check skipped (rollback CLI) | r08d, r08e |
| History check skipped (rollback CLI) | r08a |
| Backup verification skipped (rollback CLI) | r05 |
| Rollback failure always classified `FAILED_ROLLED_BACK` | r09b |
| Wrapper calls dark mode (`registered=False`) | `test_schema_v12` c1 (duplicates would be silently skipped instead of refused), `test_11b` |
| `MIGRATIONS` names the bare `_m012_protection_journal` (the original proposal) | `NameError` at collection: the forward-reference finding, reproduced |
| Gate entry 12 removed | gate tests 01, 04 |
| `init_db`'s startup gate line removed | gate tests 02, 06 |
| `migrate-v12` failure always classified `FAILED_ROLLED_BACK` | f05 |
| Disk-space check bypassed | f01 |
| `migrate()`'s own backup failure not distinguished | f04 |
| Restore: sidecars not moved aside | w05 |
| Restore: a backup with its own `-wal` not refused | w09 |
| Restore: original not put back after a failed verification | w06 |
| `tickets.py` late attach: `NOT EXISTS` guard removed | g05, g07 |
| `tickets.py` main write: holder pre-check removed | g01 |
| `tickets.py` main write: `IntegrityError` catch removed | g03 |

## 10. Final integrity checks

Measured after both full runs, against the Phase 0 snapshot, which recorded sha256 values of all 140 `git status` entries plus pre-edit copies.

| Check | Result |
|---|---|
| V12 unregistered | `MIGRATIONS[-1] == (11, "phase2_instance_coordination")`; the wrapper is not listed |
| `LATEST_SCHEMA_VERSION` | 11 |
| `LIVE_ENTRIES_ENABLED` | `False` |
| `backend/execution.py` | identical to the HEAD blob (line endings normalised); raw SHA-256 unchanged since Phase 0 |
| 13 PK-25 frozen files | 13/13 match `research/pk25/validation_manifest.json` |
| Broker calls | none: no broker module touched; tests used FakeKite or the paper broker; r12 and gate test 22 run fresh children with `KiteConnect` stubbed, giving `broker: []` |
| Real database | never opened. Evidence: `os.stat` (size, mtime_ns) of all 48 `backend/kite_terminal.db*` files is identical before and after; 0 protected opens in both full runs |
| Staged or committed | nothing: the index is empty and no commit was made |
| Branch and HEAD | unchanged (`feature/signal-detail-and-strategy-revision` @ `35a0e07…`) |
| Pre-existing worktree changes | preserved: 148 entries = 140 + 6 new files + 2 tracked files now modified (`test_gtt_reconciliation.py`, `test_live_safety.py`). The 24 changed entries are exactly the files in §2. Every other pre-existing entry has its Phase 0 hash. |
| Line endings | every changed file keeps its original style (`test_live_safety.py`: restored to its original single CRLF line, §2) |
| Orphan processes | 0 (harness summary, both runs) |

## 11. The exact proposed V12 registration diff (NOT APPLIED)

To be applied only after separate explicit authorization (runbook §2.2). These are the only changes. It is the same diff that was applied, and validated, in the registered copy (§8).

```diff
--- a/backend/db.py
+++ b/backend/db.py
@@ -2253,8 +2253,8 @@
             f"DATABASE_MIGRATION_FAILED: migration 11 produced an unexpected schema (added {sorted(added)}).")
 
 
-# Migration 12's MIGRATIONS entry, prepared but deliberately NOT listed below while DD-2 holds (V12 stays
-# unregistered until it is separately authorized; docs/PHASE_3_V12_REGISTRATION_REMEDIATION_REPORT.md §3).
+# Migration 12's MIGRATIONS entry (registered; docs/PHASE_3_V12_REGISTRATION_REMEDIATION_REPORT.md §3). A normal
+# start never applies it to a database holding data: see _AUTHORIZATION_GATED_MIGRATIONS below.
 # Migration 12's own function is defined in the protection-journal section further down this module, after
 # MIGRATIONS, so the list cannot name it directly (a NameError on import). This wrapper looks the name up only
 # when it is called, by which time the whole module has loaded; a test that substitutes that function is reached
@@ -2278,6 +2278,7 @@
     (9, "dmb_research_inbox", _m009_dmb_research_inbox),
     (10, "ticket_integrity_audit", _m010_ticket_integrity_audit),
     (11, "phase2_instance_coordination", _migration_v11_instance_coordination),
+    (12, "phase3_protection_journal", _migration_v12_protection_journal),
 ]
 LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]
 
@@ -2673,16 +2674,16 @@
 # docs/PROPOSAL_GTT_AFTER_FILL_LIFECYCLE_REVIEW.md §5.2 and §12 (Phase 1);
 # docs/PHASE_1_DARK_SCHEMA_REPORT.md.
 #
-# DARK: deliberately NOT in MIGRATIONS. init_db() never applies it,
-# LATEST_SCHEMA_VERSION is unchanged and no schema_version row is written
-# (one would make every current build refuse the database). Nothing in the
-# application reads or writes these objects. Only the explicit
-# apply_dark_protection_schema() / rollback_dark_protection_schema() calls
-# below create or remove them, and both refuse the account database and its
-# backups. A later, separately authorised phase registers the migration.
+# REGISTERED as migration 12 (phase3_protection_journal), applied to a database
+# holding data only by `db_maintenance migrate-v12 --authorize-v12` and removed
+# only by `db_maintenance rollback-schema-v12 --confirm`. The dark entry points
+# apply_dark_protection_schema() / rollback_dark_protection_schema() remain for
+# scratch v11 databases only: they refuse a v12 history, the account database
+# and its backups.
 #
 # Phase 3 stage S1 amended the objects to docs/PHASE_3_DESIGN_DECISIONS.md
-# §4.1 (R6) and renumbered the helpers to v12; still dark (DD-2).
+# §4.1 (R6) and renumbered the helpers to v12; registered as v12 by the
+# separately authorized registration (DD-2).
 #
 # Objects, created and removed all-or-nothing in one transaction:
 #   protection_operations              write-ahead journal of GTT writes (§5.2.3)
@@ -3101,7 +3102,7 @@
     migrate()'s transaction, which takes the backup and rolls everything back on any exception, so it
     takes no backup, opens no transaction and checks no history (that is still v11 here) and, unlike
     the dark entry points, does not refuse the account database: migrating it is what registration is
-    for. Not reachable while MIGRATIONS ends at v11 (DD-2)."""
+    for. Registered as MIGRATIONS[11] through _migration_v12_protection_journal."""
     status = dark_protection_schema_status(conn)
     if status["state"] == "present" and status["unique_gtt_index"]:
         # A re-run over an already migrated database (a lost schema_version table), or a scratch
@@ -3294,8 +3295,8 @@
     phase3_protection_journal, the history is exactly v1..v12 of this code, the Phase 3 objects are
     exactly v12's (unique GTT index included) and they hold no data: no journal row, no resolution row
     (an orphan one included), no protection_state value, no protection_* event. Once they hold data,
-    the way back is restoring a verified backup. While MIGRATIONS ends at v11 (DD-2) it therefore
-    always refuses. Not exposed as a CLI command until registration.
+    the way back is restoring a verified backup. On a build without v12 it always refuses. Exposed
+    as `python -m backend.db_maintenance rollback-schema-v12 --confirm`.
 
     The caller must hold the instance lock. A file database is backed up first (verified,
     <db>.pre_rollback_v12_bak). One transaction: an interruption leaves the v12 database as it was."""
--- a/backend/db_maintenance.py
+++ b/backend/db_maintenance.py
@@ -14,7 +14,7 @@
     python -m backend.db_maintenance migrate-v12 --authorize-v12 [--db PATH]
                                                  the ONLY way migration 12 is applied to a database holding
                                                  data (a normal start refuses it) — STOP THE APP FIRST.
-                                                 Inert until this build registers migration 12.
+                                                 This build registers migration 12.
     python -m backend.db_maintenance rollback-schema-v12 --confirm [--db PATH]
                                                  undo migration 12 (Phase 3) — STOP THE APP FIRST; refused
                                                  unless this build registers v12, the history is exactly
--- a/tests/schema_builds.py
+++ b/tests/schema_builds.py
@@ -21,7 +21,7 @@
 from contextlib import contextmanager, nullcontext
 from pathlib import Path
 
-V12_REGISTERED_IN_THIS_BUILD = False
+V12_REGISTERED_IN_THIS_BUILD = True
 
 V11 = (11, "phase2_instance_coordination")
 V12 = (12, "phase3_protection_journal")
```

## 12. Remaining risks

1. **Live Kite GTT-id uniqueness is unproven.** A duplicate is now handled: tracked, not linked, flagged urgent. The broker GTT then exists without a ticket following it until a human acts.
2. **Read-only sidecars.** A read-only open (a refused start, `status`) can leave an empty `-wal` and a `-shm` beside the database. They are harmless and are moved aside with the set by `restore-backup`.
3. **`migrate-v12` does not run the fixed-local-drive check** that `instance_lock.acquire()` runs (audit risk 9). Unchanged.
4. **The empty-database exemption** (user decision): a deleted live file is re-created at v12.
5. **The duplicate-GTT test sweep was not exhaustive.** The registered full run is the evidence; the 85 hard-coded ids were not each reviewed by hand.
6. **The FK-placeholder test in `test_platform_pk22`.** Its gated branch (placeholder = v12) runs today and becomes unreachable once v12 is registered, when the placeholder becomes v13. It was kept so today's gated path stays tested; the generic branch is tested on the registered build.
7. **The backups share the database's disk.** An off-machine copy (`E:\backup`, evidence list in runbook §4.2) is still required.
8. **Windows-only behaviour.** `restore-backup`'s `RESTORE_IN_USE` relies on Windows refusing to rename an open file (w10, Windows only). On POSIX that protection is the instance lock alone.

## 13. Separate requirements for migrating the real database

None of the following is authorized by this work:

1. The registration diff (§11), separately authorized, applied and committed by the user, with both suites green on the registered build.
2. Separate, explicit authorization to migrate the real database.
3. The app stopped. Enough disk space.
4. A **new** operator backup (`db_maintenance backup`), its `integrity` (ok, 0 FK), its `status` (`v11 of v12`), its SHA-256, and an off-machine copy. For `E:\backup`, the runbook §4.2 evidence list.
5. A **fresh** `preflight-v12 --db <new backup>` reporting PASS. The 11:36 result is historical.
6. `python -m backend.db_maintenance migrate-v12 --authorize-v12`, then the result and recovery handled per runbook §4.4 and §9.
7. `status` = `v12 of v12` and `integrity` ok. Keep both backups and the audit JSON.

## Addendum: V12 final readiness (2026-11-11)

The open items of §12 were taken up in `output/audits/v12_final_readiness_2026-11-11/`:

| §12 item | Now |
|---|---|
| 3. migrate-v12 skips the fixed-local-drive check | done: `backend/storage_classification.py`, run before the lock, any backup or any write, re-checked before and inside the transaction |
| 4. a deleted live file is re-created at v12 | done: evidence-based fresh install (user decision); `backend/installation_state.py` refuses an established installation's missing, empty, replaced or moved database |
| 7. the off-machine copy | the code is done (`backup --dest`, `verify-backup`; migrate-v12 requires `--offsite-record`). `E:\backup` is **refuted** as off-machine: E: and D: are partitions of the same physical disk 0. **OPERATOR_ACTION_REQUIRED** |
| 2. read-only opens leave sidecars | unchanged (accepted residual) |
| 1, 5, 6, 8 | unchanged; see the final independent audit |

§13 step 6 now reads `migrate-v12 --authorize-v12 --offsite-record <record.json>`. The procedure is in that directory's `OPERATOR_RUNBOOK.md`.
