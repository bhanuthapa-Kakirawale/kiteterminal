# Migration v12 registration and migration runbook — DRAFT (revised 2026-11-11)

> **SUPERSEDED FOR THE MIGRATION ITSELF (V12 final readiness, 2026-11-11).** V12 is now **registered** (the
> "not registered" statements below describe the earlier build), and the operator procedure is
> `output/audits/v12_final_readiness_2026-11-11/OPERATOR_RUNBOOK.md`, with `GO_NO_GO_CHECKLIST.md`. Where this draft
> and that runbook differ, the runbook applies. The material changes:
> - `migrate-v12` requires `--offsite-record <record.json>`: a fresh (under 12 h) PASS verification record of a backup on a
>   **separate physical disk** (`backup --dest <external disk>`), re-verified live and bound to the database's logical
>   digest. `E:\backup` is on the same physical disk (disk 0) as `D:` and is **not** off-machine (§4.2 below is
>   superseded);
> - every writing command refuses a database that is not on an internal fixed local disk (`STORAGE_NOT_FIXED_LOCAL`);
> - a start refuses a missing, zero-byte, empty, corrupt, replaced, moved or mid-maintenance database
>   (`DATABASE_INSTALLATION_STATE_REFUSED`). The empty-database exemption applies only to a true first run with no
>   evidence of an earlier installation (user decision 2026-11-11, evidence-based);
> - migrate-v12 and restore-backup leave an in-progress marker until an unambiguous, audited outcome; a start refuses
>   while one exists;
> - `restore-backup` never renames over an existing file (`RESTORE_NAME_CONFLICT`) and re-joins a split set after a
>   crash.
>
> V12 is registered but has not been applied to the real database. No migration authorization has been exercised.

> **DRAFT — NOT EXECUTED — AUTHORIZES NOTHING.**
>
> - V12 is **not registered**: `MIGRATIONS` ends at v11 and `LATEST_SCHEMA_VERSION` is 11.
> - **Registering** v12 (a code change, §2) and **migrating the real trading database** (§4) are separate
>   decisions. Each needs its own explicit authorization from the user.
> - Nothing in this runbook has been run against the real account database or any of its backups.
>
> This revision replaces the Phase 3 S2 draft. Superseded statements from that draft:
> - "registration is one appended line" (it raised a `NameError` on import, §2.1);
> - "the first start applies v12" (the authorization gate refuses that, §3);
> - "the R2-4 predicate is UNDEFINED" (approved and implemented on 2026-11-11);
> - "v10 → v12 in one start" (now refused, §3);
> - "no rollback command" (now `rollback-schema-v12`, §8).
>
> Sources:
> - `docs/PHASE_3_V12_REGISTRATION_REMEDIATION_REPORT.md` (this revision's evidence and tests);
> - `docs/PHASE_3_V12_AUTHORIZATION_GATE_IMPLEMENTATION_REPORT.md`;
> - `docs/PHASE_3_V12_FINAL_REGISTRATION_PROPOSAL.md`;
> - `docs/PHASE_3_DESIGN_DECISIONS.md` (DD-2, DD-6, R2-4, M-9).

---

## 0. Commands in this runbook

All commands run from the repository root with the application **stopped**.

| Command | Writes? | Purpose |
|---|---|---|
| `python -m backend.db_maintenance status [--db F]` | no (read-only) | Shows the schema version, pending migrations and history. |
| `python -m backend.db_maintenance integrity [--db F]` | no (read-only) | Runs `PRAGMA integrity_check` and `foreign_key_check`. |
| `python -m backend.db_maintenance backup` | a new `_bak` file | Online backup, integrity-checked. |
| `python -m backend.db_maintenance preflight-v12 --db <backup>` | no (immutable open) | The DD-6 / R2-4 preflight. It refuses the live file name. |
| `python -m backend.db_maintenance migrate-v12 --authorize-v12` | yes | **The only way** migration 12 is applied to a database holding data. |
| `python -m backend.db_maintenance rollback-schema-v12 --confirm` | yes | Undoes migration 12 while the journal is empty. |
| `python -m backend.db_maintenance rollback-schema-v11 --confirm` | yes | Undoes migration 11. Only after v12 has been rolled back. |
| `python -m backend.db_maintenance restore-backup --backup F --confirm` | yes | Replaces the database with a verified backup, sidecar-safe (§7). |

How the write commands behave:

- **The lock.** Every write command takes the terminal's instance lock (`<db>.instance.lock`) without waiting. It refuses with exit 3 (`ANOTHER_INSTANCE_RUNNING`) while the app or another maintenance command holds the lock.
- **Argument checks.** `--authorize-v12`, `--confirm` and `--backup` are checked before the database is touched at all.
- **Audit records.** `migrate-v12`, `rollback-schema-v12` and `restore-backup` write an audit record for every outcome: stdout or stderr, plus a JSON file beside the database (`<db>.v12_migration_audit_*.json`, `<db>.v12_rollback_audit_*.json`, `<db>.restore_audit_*.json`).
- **Exit codes:**
  - `0` done or already done;
  - `1` refused, or rolled back (nothing changed);
  - `2` usage error;
  - `3` the lock is held;
  - `4` **committed, or state unknown, and verification failed: stop and follow §9**.

## 1. Preconditions for authorizing the REGISTRATION (the code change)

| # | Precondition | Status (2026-11-11) |
|---|---|---|
| R1 | The forward reference is solved: the late-bound wrapper `_migration_v12_protection_journal` | done, prepared but unlisted (§2.1) |
| R2 | The authorization gate: a start never applies v12 to a database with data | done (gate report) |
| R3 | The rollback CLI `rollback-schema-v12 --confirm`, with its tests | done (remediation report §4) |
| R4 | The restore procedure, safe against stale WAL/SHM | done (§7) |
| R5 | Failure paths classified from the real history (`MIGRATED_VERIFICATION_FAILED`, `STATE_UNKNOWN`, ...) | done (§4.4) |
| R6 | The duplicate-GTT production path handled (`routes/tickets.py`) | done (remediation report §7) |
| R7 | The full standing suite green on BOTH builds (unregistered repo; registered validation copy); PK-25 13/13; `LIVE_ENTRIES_ENABLED = False`; `execution.py` unchanged | see the remediation report §8 |
| R8 | Separate, explicit user authorization to apply the registration diff | **not given** |

## 2. Registration (when separately authorized)

### 2.1 The change

The exact diff is in `docs/PHASE_3_V12_REGISTRATION_REMEDIATION_REPORT.md` §11. It contains:

- **One `MIGRATIONS` entry in `backend/db.py`:**
  ```python
  (12, "phase3_protection_journal", _migration_v12_protection_journal),
  ```
  This is the late-bound wrapper, already defined just above `MIGRATIONS`, not the bare `_m012_protection_journal`. That function is defined ~900 lines later, after the list, so naming it in the list raises `NameError` when `backend.db` is imported. The wrapper looks the function up when it is called.
- **Comment and docstring updates** in `backend/db.py` and `backend/db_maintenance.py`.
- **`tests/schema_builds.py`:** `V12_REGISTERED_IN_THIS_BUILD = True`. This is the one test pin of the registration state.

`_AUTHORIZATION_GATED_MIGRATIONS = {12: "phase3_protection_journal"}` is unchanged. Nothing else changes: not `main.py`, `execution.py`, `live_entry_gate.py` or the PK-25 files.

### 2.2 Registration sequence

1. Confirm the user's registration authorization (R8) and record its date.
2. Snapshot the worktree (git status hashes, pre-edit copies).
3. Apply the diff from the remediation report §11. Nothing else.
4. Run the focused suite, then the full approved suite (§10). Both must show 0 failed, 0 errors and exactly the 6 known skips.
5. Re-check: PK-25 13/13, `execution.py` byte-identical to HEAD, `LIVE_ENTRIES_ENABLED = False`.
6. The user reviews and commits.

Registration alone changes no database. The real database stays at v11 until §4. From then on, a start refuses it (§3).

## 3. Startup behaviour once v12 is registered

| Database | What a normal start (`init_db`) does |
|---|---|
| exactly v11, holding data | **Refuses** with `DATABASE_MIGRATION_AUTHORIZATION_REQUIRED` before any write (no key minted, no journal mode set, no backup, no migration). It releases the instance lock and logs `startup.refused`. The server does not start. The message says: stop the app and run `migrate-v12 --authorize-v12`. |
| below v11 (v10 or older), holding data | Refuses the same way. The message says to bring the database to v11 **with the Phase 2 build** first, then run `migrate-v12`. |
| exactly v12 | Starts normally. |
| a missing file, or a file with no user table | Created directly at v12. There is nothing to protect (user decision, 2026-11-11). |
| a foreign or newer history | `DATABASE_SCHEMA_UNSUPPORTED`, before any write. |

`python -m backend.db_maintenance migrate` (the generic command) refuses v12 in the same way.

## 4. Migrating the real database to v12 (separately authorized, after registration)

### 4.1 Preconditions

1. The user's explicit authorization for the real migration. Registration's authorization does not cover it.
2. The app is stopped: no `python -m backend.server`, no maintenance command running.
3. Enough free space beside the database: at least 3 × (database + `-wal`) + 64 MB. `migrate-v12` checks this itself and refuses with `DISK_SPACE_INSUFFICIENT`.

### 4.2 Operator backup (by the operator)

1. Run `python -m backend.db_maintenance backup`. It writes `backend\kite_terminal.db.manual_<time>_bak` using SQLite's online backup. That backup **includes committed transactions still in the `-wal`**, so it is consistent.
2. Run `python -m backend.db_maintenance integrity --db <that backup>`. It must print `integrity_check: ok` and `foreign_key_check: 0 violation(s)`.
3. Run `python -m backend.db_maintenance status --db <that backup>`. On the registered build it must print `v11 of v12`.
4. Record the backup's size and SHA-256 (`Get-FileHash <backup> -Algorithm SHA256`), and copy it **off this machine**.

   If `E:\backup` is that off-machine copy, record as evidence:
   - its full path;
   - its size;
   - its SHA-256 compared with the source;
   - how it was made: a copy of a `db_maintenance backup` file, never a raw copy of the live `.db` (§6);
   - `integrity` and `preflight-v12` run on a **local copy** of it;
   - whether E: is a removable or off-machine device.

**Never** copy `kite_terminal.db` by hand while the app runs, and never without its `-wal` (§6).

### 4.3 Fresh DD-6 / R2-4 preflight

Run `python -m backend.db_maintenance preflight-v12 --db <the new backup>`. It must print `result: PASS`.

> **DD-6 / R2-4 history:** the PASS recorded for `manual_20261111113642_bak` (11:36 on 2026-11-11, reported by the operator) is **historical evidence only**. It is not an approval of the database as it is today. The preflight above must be run again on a backup taken immediately before the migration. `migrate-v12` then repeats it twice more:
> - on the migration target itself, after its own verified backup;
> - inside the v12 transaction under the write lock: the NULL-mode check in `migrate()`, and the duplicate-GTT check in `_m012_registered`.

### 4.4 The authorized migration

Run `python -m backend.db_maintenance migrate-v12 --authorize-v12`.

It runs in this order. Each refusal writes nothing beyond its audit file.

1. The flag is checked before any filesystem access.
2. The instance lock.
3. This build registers v12.
4. The history is exactly v1..v11 of this code. If it is already exactly v12: `ALREADY_APPLIED`, or `ALREADY_APPLIED_VERIFICATION_FAILED` (exit 4) if it does not verify.
5. The target passes `integrity_check`.
6. Disk space.
7. A fresh backup, `<db>.pre_v12_authorized_<µs>_bak`, **verified independently**. It is reopened immutable and its integrity, v11 history, FK count and every table's row count are compared with the target, and its SHA-256 is recorded.
8. The DD-6 / R2-4 preflight on the target.
9. `db.migrate()` with the V12 authorization. It takes its own backup `<db>.pre_schema_v12_bak` and runs v12 in one `BEGIN IMMEDIATE` transaction, with the preflight repeated inside it.
10. Post-migration verification:
    - history exactly v12;
    - the v12 objects exactly v12's;
    - the journal empty;
    - integrity ok;
    - FK violations not increased;
    - every table's row count unchanged (`schema_version` +1).

| Result | Exit | Meaning | Operator action |
|---|---|---|---|
| `V12_APPLIED` | 0 | Migrated and verified | §5 |
| `ALREADY_APPLIED` | 0 | Already v12; nothing changed | none |
| `V12_NOT_REGISTERED`, `HISTORY_REFUSED`, `TARGET_INTEGRITY_FAILED`, `DISK_SPACE_INSUFFICIENT` | 1 | Refused; nothing changed | Fix the cause. A database below v11 goes through the Phase 2 build first. |
| `BACKUP_FAILED` | 1 | The command's backup, or `migrate()`'s own backup, failed before any transaction; the database is still v11 | Check disk and permissions. Re-run. |
| `BACKUP_VERIFICATION_FAILED` | 1 | The backup did not verify; nothing migrated | Investigate the disk. The bad file is left for inspection. |
| `PREFLIGHT_FAILED` | 1 | DD-6 / R2-4 found tickets; nothing migrated | Resolve the listed tickets in Ticket History. |
| `FAILED_ROLLED_BACK` | 1 | v12 failed and rolled back. **Confirmed** from the committed history: exactly v11, no v12 object | Read `error` in the audit. Re-run after fixing. |
| `MIGRATED_VERIFICATION_FAILED` | **4** | v12 **committed**, but the call failed afterwards or the checks failed | **§9.** Do not start the app. |
| `STATE_UNKNOWN` | **4** | The state could not be confirmed | **§9.** Do not start the app. |

`FAILED_ROLLED_BACK` is never reported once v12 has committed: the result is read back from the committed history, never assumed.

## 5. After `V12_APPLIED`

- `db_maintenance status`: `v12 of v12`.
- `db_maintenance integrity`: `ok`, 0 violations.
- Keep both backups (`pre_v12_authorized_*_bak` and `pre_schema_v12_bak`) and the audit JSON. Record their SHA-256 values with the operator backup.
- Start the app. Nothing reads or writes the v12 objects until the engine wiring that registration accompanies. `LIVE_ENTRIES_ENABLED` stays `False`.

## 6. WAL and sidecar files

The database runs in WAL mode. On disk it is a **set** of files that SQLite pairs **by name**:

- `kite_terminal.db`;
- `kite_terminal.db-wal`: committed transactions not yet copied into the main file;
- `kite_terminal.db-shm`: an index of the WAL.

Observed on this machine (SQLite 3.50.4, Python 3.13.14, in tests):

- A read-only open of a WAL-mode file that has no sidecars creates an **empty** `-wal` and a `-shm`, and they stay after it closes. They are harmless: no frames, and the main file is unchanged. A refused start can therefore leave them behind (`tests/test_db_restore_wal.py` w01).
- A read-only reader sees committed rows that exist only in the `-wal`, and does not change it (w02). The startup check therefore reads the true history.
- An `immutable` open **ignores** the `-wal`. That is why `restore-backup` refuses a backup that has a non-empty `-wal` of its own (w09).
- `db_maintenance backup` and the `migrate-v12` backups use SQLite's online backup. They include WAL-only rows and are single, self-contained files (w03, w04).

**The hazard:** copying a backup over `kite_terminal.db` while an old `kite_terminal.db-wal` is still there makes SQLite **replay that old WAL onto the restored image** the next time the file is opened. In the test (w05b) the result passes `integrity_check`, yet it holds the replayed v12 state instead of the v11 backup.

So:

- **never** restore by copying files;
- **never** delete a `-wal` by hand, because it may hold committed trades;
- **never** copy the `.db` without its `-wal` while it is in use;
- use `restore-backup` (§7).

## 7. Restore procedure

Run `python -m backend.db_maintenance restore-backup --backup <file> --confirm [--db PATH]`.

1. `--backup` and `--confirm` are checked before any filesystem access. A backup that is the database itself is refused. Then the instance lock: the app must be stopped.
2. **Verify the backup.** It is opened immutable. It must have no non-empty `-wal` or `-journal` of its own, must pass `integrity_check`, and must carry a history this build accepts. A v12 backup is refused by a v11 build. A v11 backup is accepted by the registered build, which then requires `migrate-v12` before a start (§3). Its FK count, row counts and SHA-256 are recorded.
3. Disk space for a staging copy.
4. The backup is byte-copied to `<db>.restore_staging_<µs>`, and the copy is re-verified (SHA-256 and content).
5. **The original set is moved aside together:**
   - `kite_terminal.db` → `kite_terminal.db.pre_restore_<µs>_orig`;
   - `-wal` → `..._orig-wal`, `-shm` → `..._orig-shm`, `-journal` → `..._orig-journal`.

   The WAL stays paired with its own main file, so the original remains complete and openable. No stale sidecar is left under the database's name.

   If the database is still open somewhere (Windows refuses the rename), the result is `RESTORE_IN_USE` and nothing is changed.
6. The staging copy is moved into place (`os.replace`). No `-wal`, `-shm` or `-journal` may then exist for it.
7. **Post-restore verification:**
   - the SHA-256 equals the backup's;
   - `integrity_check` ok;
   - the history, FK count and every table's row count equal the backup's.
8. Result:
   - `RESTORED` (0). The original set is kept as `..._orig` until you delete it.
   - `RESTORE_VERIFICATION_FAILED` (4). The restored file is set aside as `<db>.failed_restore_<µs>` and **the original set is renamed back unchanged**.
   - `RESTORE_FAILED_MANUAL_RECOVERY` (4). Even renaming back failed. The audit lists every file. Rename `kite_terminal.db.pre_restore_<µs>_orig` and its `-wal`/`-shm` back to `kite_terminal.db`, `-wal` and `-shm` by hand **before anything opens the database**. Then run `status` and `integrity`.

## 8. Rollback

**Order: v12 first, then v11.** `rollback-schema-v11` refuses a v12 history. Its dependency guard also refuses while `protection_operation_resolutions` references `broker_reads`.

Run `python -m backend.db_maintenance rollback-schema-v12 --confirm`.

It refuses, writing nothing but its audit, unless all of these hold:

- this build registers v12;
- the history is exactly v1..v12;
- the v12 objects are exactly v12's (`V12_SCHEMA_REFUSED` otherwise);
- the target passes integrity;
- **the journal holds no data** (`REFUSED_HELD_DATA`, with the counts): no journal row, no resolution row (orphans included), no `protection_state` value and no `protection_*` event;
- there is enough disk space.

Then it:

1. writes `<db>.pre_rollback_v12_authorized_<µs>_bak`, verified independently (v12 history, row counts, FK count);
2. runs `db.rollback_schema_v12`. That takes its own `pre_rollback_v12_bak`, **re-checks the held data under the write lock** (`ROLLBACK_REFUSED` if anything appeared), drops exactly the v12 objects and row 12 in one transaction, and keeps every v11 row and every AUTOINCREMENT high-water mark;
3. verifies: history v11, no v12 object, integrity ok, FK violations not increased, every v11 table's row count unchanged.

| Result | Exit | Meaning |
|---|---|---|
| `ROLLED_BACK` | 0 | Schema v11. The Phase 2 build accepts it. |
| `FAILED_ROLLED_BACK` / `ROLLBACK_REFUSED` / `BACKUP_FAILED` | 1 | Still v12, objects intact. **Confirmed** from the committed history. |
| `ROLLBACK_VERIFICATION_FAILED` | **4** | The rollback committed (v11) but the call failed afterwards or did not verify: **§9** |
| `STATE_UNKNOWN` | **4** | **§9** |

**Once the journal holds data, the way back is restoring a verified backup (§7).** Every trade recorded after that backup is lost. State this before the registration is authorized.

## 9. Recovery when a command reports exit 4

This covers `MIGRATED_VERIFICATION_FAILED`, `ALREADY_APPLIED_VERIFICATION_FAILED`, `ROLLBACK_VERIFICATION_FAILED`, `STATE_UNKNOWN`, `RESTORE_VERIFICATION_FAILED` and `RESTORE_FAILED_MANUAL_RECOVERY`.

1. **Do not start the application.** Do not delete or move any `-wal`, `-shm` or backup file.
2. Read the audit JSON named in the output. It holds `history_after`, `verification.problems`, `error` and the verified backup's path and SHA-256.
3. Run `python -m backend.db_maintenance status` and `integrity` (both read-only).
4. If the database is not as intended, restore the **independently verified** backup named in the audit (`pre_v12_authorized_*_bak` for a migration, `pre_rollback_v12_authorized_*_bak` for a rollback) with `restore-backup` (§7). Then run `status` and `integrity` again.
5. If `restore-backup` itself reports `RESTORE_FAILED_MANUAL_RECOVERY`, follow §7 step 8 by hand.
6. Record what happened, with the audit files, before anything else is attempted.

## 10. V11 compatibility

- **The Phase 2 (v11) build:**
  - accepts a v11 database;
  - refuses a v12 database with `DATABASE_SCHEMA_UNSUPPORTED ... newer than this code supports`, writing nothing;
  - accepts a database rolled back by `rollback-schema-v12` (`tests/test_rollback_v12_cli.py` r03).
- **The registered build** refuses a v11 database at start until `migrate-v12` runs (§3). Its `rollback-schema-v11` and `migrate` (to v11) remain the Phase 2 procedures.
- **The full cycle** has been tested on temporary databases with every trading row kept (r11): v12 → `rollback-schema-v12` → v11 → `rollback-schema-v11` → v10 → `migrate` (Phase 2 build) → v11 → `migrate-v12` → v12.

## 11. The dark entry points (resolved)

`apply_dark_protection_schema` and `rollback_dark_protection_schema` refuse any history other than exactly v1..v11 (`_require_dark_history`). On a v12 database the journal is created only by migration 12 and removed only by `rollback_schema_v12`. No dark rollback can leave `schema_version` row 12 behind (`tests/test_v12_registration_prep.py` RP01).
