# Phase 3 Stage S2 — implementation report

**Date:** 2026-11-09 · **Branch:** `feature/signal-detail-and-strategy-revision` · **HEAD:**
`35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef` (unchanged; nothing staged, committed, pushed or stashed)

**Result:** S2 is implemented and verified. **Migration v12 is NOT registered**, and **S3 was not started**.

The full standing suite has **one failure**, `tests/test_instance_lock.py::test_L5_the_real_lock_file_is_guarded`.
It is environmental and unrelated to S2 (§6.4). It fails alone as well, and it needs a decision by the user.

---

## 1. Scope and authorisation

The user explicitly authorised Phase 3 **Stage S2 only**, based on the approved S2 implementation plan.
The plan is recorded in `C:\Users\thapa\.claude\plans\pasted-content-id-66b0-authorization-encapsulated-owl.md`
and built on `docs/PHASE_3_DESIGN_DECISIONS.md` (R6: DD-2, DD-6, M-9) and
`docs/PHASE_3_READINESS_REVIEW.md` §§7.3, 7.5, 13.

The four open questions were decided by the user and are followed exactly:

| Q | Decision | Where |
|---|---|---|
| Q2 | Registered `_m012` on a present schema **without** the unique GTT index refuses with `DatabaseMigrationFailed`, even after the duplicates are cleared | §3.1, tests C3, mutation M3 |
| Q3 | Generic foreign-key dependency guard added to `rollback_schema_v11` | §3.3, tests G4–G5, mutations M9–M13 |
| Q4a | Registration runbook draft included, marked DRAFT / NOT EXECUTED | `docs/PHASE_3_V12_REGISTRATION_RUNBOOK_DRAFT.md` |
| Q4b | DD-6/R2-4 preflight code deferred to the registration authorisation; its "open executed BUY with NULL mode" predicate stays **undefined** | runbook §2.2; no code |

**Not authorised and not done:**
- registering v12;
- any change to `MIGRATIONS` or `LATEST_SCHEMA_VERSION`;
- a v12 CLI;
- S3 or the protection engine;
- live activation;
- any migration against a real or existing database.

## 2. Changed files (exactly the four authorised)

| File | Change |
|---|---|
| `backend/db.py` | S1 changes **extended in place**. Starting SHA-256 (with S1) `c8192c2f…31f6`, final `5529fc0e…5990`. Eight hunks, 219 changed lines (§3) |
| `tests/test_schema_v12.py` | **new**, 70 tests |
| `docs/PHASE_3_V12_REGISTRATION_RUNBOOK_DRAFT.md` | **new** |
| `docs/PHASE_3_S2_IMPLEMENTATION_REPORT.md` | **new** (this file) |

**Not modified:**
- `tests/test_dark_protection_schema.py` (S1) and `tests/test_schema_v11.py`;
- `backend/db_maintenance.py`;
- `backend/execution.py`;
- the 13 PK-25 files;
- `backend/live_entry_gate.py`;
- every other entry.

## 3. `backend/db.py`

### 3.1 Registered-mode `_m012_protection_journal`

The signature is `_m012_protection_journal(conn, *, registered: bool = True)`. The default is `True` on purpose:
the future `MIGRATIONS` entry is the bare function (M-9). The test A1 pins it as keyword-only with default `True`.

The registered path runs in the new `_m012_registered(conn)`, inside `migrate()`'s transaction. It never calls
`_run_dark_step`, `_require_no_transaction`, `_require_current_schema`, `_refuse_account_database` or
`_backup_before`. Test B7 checks this with raising spies on all five, and checks that no `*pre_dark_protection*`
file appears.

1. `dark_protection_schema_status(conn)` gives the state. Any `DarkSchemaRefused` raised in the registered
   path is re-raised as `DatabaseMigrationFailed … Nothing was changed (the migration is rolled back)`. That
   covers a missing `ticket_events`, no single allow-list, unexpected columns, and a trigger or view.
2. `present` **with** `unique_gtt_index` → returns `{"action": "already_present"}`. It is a no-op and keeps
   all data (tests B5, B6).
3. `present` **without** the index → `DatabaseMigrationFailed` (**Q2**). The message names
   `idx_trade_tickets_unique_gtt` and says "Nothing was changed".
4. `partial` / `unexpected` → `DatabaseMigrationFailed … found the Phase 3 objects {state} (a partial or
   foreign form; mismatched: …). Nothing was changed`. This mirrors v11.
5. `absent` → the **duplicate-GTT check comes first, before any DDL**: `_duplicate_gtt_ids`, the same query as
   before. Any duplicate → `DatabaseMigrationFailed` naming the ids and saying the database stays at v11.
   Otherwise the path:
   - creates the four objects;
   - runs `ALTER TABLE … ADD COLUMN protection_state`;
   - rebuilds `ticket_events` with the suffix;
   - executes `_UNIQUE_GTT_INDEX_DDL` **directly** (never skipped);
   - verifies that exactly the five object names were added, the state is `present` and `unique_gtt_index`
     is True.

**Dark path preserved.** `apply_dark_protection_schema` now passes
`lambda c: _m012_protection_journal(c, registered=False)`. The `registered=False` body is the S1 body,
unchanged:
- it refuses unless the objects are `absent`;
- `_ensure_unique_gtt_index` skips the index and warns on duplicates.

One refactor: the duplicate query moved into `_duplicate_gtt_ids`, and `_ensure_unique_gtt_index` calls it.
The query text and its behaviour are identical.

The dark entry points keep their account-database guard, transaction guard and current-schema guard. The S1
suite passes unmodified, including `1b`, `10h`, `11`, `11b` and `12`.

### 3.2 `rollback_schema_v12(conn)` (new, in the dark section)

It is modelled on `rollback_schema_v11`. In order:

1. `conn.in_transaction` → `SchemaRollbackRefused`.
2. `expected = [(v, n) for v, n, _ in MIGRATIONS if v <= 12]`. It refuses unless all three hold:
   - `len(expected) == 12`;
   - `expected[-1] == (12, "phase3_protection_journal")`;
   - `history == expected`.

   On the current build it therefore **always refuses**, with "… this build does not register migration 12",
   and is inert. Without the length and name checks, an unregistered v11 database would match a v1..v11
   `expected` (test A3, mutation M6b).
3. `dark_protection_schema_status` must report `present` with `unique_gtt_index` True. A `DarkSchemaRefused`
   from the shape checks becomes `SchemaRollbackRefused`.
4. Any held data refuses: journal rows, resolution rows (orphans included), non-NULL `protection_state`, or any
   of the 13 `protection_*` events. This uses `_protection_held_data`, the counts extracted from
   `rollback_dark_protection_schema` with identical keys, order and SQL. The dark rollback now calls the same
   helper.
5. Every refusal above comes **before** the backup. For a file database, the verified backup is
   `<db>.pre_rollback_v12_bak`.
6. One transaction:
   - isolation `None`;
   - the foreign-key setting recorded, then `PRAGMA foreign_keys=OFF`;
   - `BEGIN IMMEDIATE`;
   - the held data **re-checked under the write lock** (see below);
   - `_r012_protection_journal`;
   - `DELETE FROM schema_version WHERE version = 12`;
   - verify `_check_history(...) == 11`, the dark state `absent`, and no new foreign-key violations;
   - `COMMIT`.

   On `BaseException` it runs `ROLLBACK`. The previous foreign-key setting and isolation level are always
   restored (test F4).
7. It returns `{"rolled_back_to": 11, "backup": <path or None>}` and logs
   `event=schema_rollback.applied from=12 to=11 backup=…` (test F1).

The docstring states that the caller must hold the instance lock. There is **no CLI**.

Per the plan (§8, Q-minor default) there is no account-database guard. The function is inert while v12 is
unregistered, and H1 proves it refuses an account-database stand-in with no write and no backup.

*Beyond the plan's literal step list (a tightening only):* the in-transaction re-check of held data. The
checks in step 4 run before `BEGIN IMMEDIATE`, and the re-check closes that window.

`_r012_protection_journal` is unchanged.

### 3.3 Q3: generic foreign-key dependency guard in `rollback_schema_v11`

`_tables_referencing_phase2(conn)` is read-only and sits **before** the dark marker, so it uses no dark names
(S1 `11b` passes unmodified). It works as follows:

- It enumerates `sqlite_master` tables with `name NOT LIKE 'sqlite\_%' ESCAPE '\'`, which excludes SQLite's
  internal tables.
- It skips the Phase 2 tables themselves, compared case-insensitively.
- For each remaining table it runs `PRAGMA foreign_key_list("<name>")`. The name is quoted by
  `_quoted_identifier`, which doubles embedded `"`.
- It collects every table with a foreign key whose target, compared case-insensitively, is in `PHASE2_TABLES`.

`rollback_schema_v11` calls the guard after the history and `phase2_schema_status` checks and **before** the
backup. Any dependant → `SchemaRollbackRefused("… table(s) [...] have foreign keys into the Phase 2 tables;
dropping those would leave them dangling. Roll back the dependent schema(s) first. Nothing was changed.")`.

No table name is hard-coded. G5 covers a test-made table, a quoted, mixed-case identifier, and a table that
references only non-Phase-2 tables, which still rolls back. On a plain v11 database it finds nothing:
`test_schema_v11.py` passes unmodified, including its rollback and CLI tests. No other existing rollback check
changed.

## 4. Migration and versioning confirmation

- `MIGRATIONS` has 11 entries, the last `(11, 'phase2_instance_coordination', _migration_v11_instance_coordination)`.
  `LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]` = 11. Checked with an AST read of the final file and by test A1.
- No v12 entry exists, and `_m012` / `_r012` are not in `MIGRATIONS`.
- `db_maintenance.WRITING_COMMANDS` is still `("analyze", "vacuum", "migrate", "rollback-schema-v11")`.
  `rollback-schema-v12` is rejected by argparse (A2).
- No backend module other than `db.py` mentions `rollback_schema_v12`, `_m012_registered` or
  `phase3_protection_journal` (A2, and a grep).
- Startup migration behaviour is unchanged.
- Registration was simulated **only inside tests**:
  - `db.MIGRATIONS = db.MIGRATIONS + [(12, "phase3_protection_journal", db._m012_protection_journal)]` and
    `db.LATEST_SCHEMA_VERSION = 12`, inside a `_build` context manager that restores both by hand in
    `finally`;
  - never `monkeypatch.undo()`;
  - an autouse fixture asserts after every test that both are back at v11.

## 5. Tests: `tests/test_schema_v12.py` (70)

Every database is under the test's `tmp_path`. Helpers come from `test_schema_v11.py` and from S1 by import.
The `os._exit` child installs the Phase 0 guard (`install`, `neutralise_dotenv`, `scrub_kite_environment`)
before any database work.

| Group | Tests | Covers |
|---|---|---|
| A versioning | 4 | A1 not registered, plus the `registered` default; A2 no CLI and no other module; A3 inert on v11, dark absent and present, unchanged, no backup |
| B registered apply | 7 | B1 v11→v12, stored schema equals the dark apply's; B2 every row, `broker_reads`, `sqlite_sequence` and the `ticket_events` high-water mark kept, backup holds v11; B3 v10→v12, `[11, 12]`, a single `pre_schema_v12_bak` holding v10; B4 idempotent ×3; B5 present no-op keeps journal and resolution rows; B6 lost `schema_version` re-run keeps the data; B7 no dark-path calls |
| C registered refusal | 14 | C1 duplicates (live int, `PAPER-G-n`) named, unique ids not named; C2 six foreign or partial forms, Phase 1-era, one real object; C3 present without the index, duplicates remaining or cleared (Q2); C4 extra column, trigger. Each leaves schema and content unchanged at v11 (only `pre_schema_v12_bak` added) |
| D partial failure | 4 | D1 `RuntimeError` / `KeyboardInterrupt` after the rebuild → v11, then a clean apply; D2 `os._exit(137)` child → v11, then a clean apply; D3 v10→v12 with v12 failing → v11 committed, the Phase 2 build accepts it, the backup holds v10 |
| E older builds | 2 | `MIGRATIONS[:11]` and `[:10]` → `newer than this code supports` |
| F `rollback_schema_v12` | 29 | F1 v12→v11: SQL, rows and `broker_reads` kept, high-water mark kept, no orphan sequences, `integrity_check` ok, `foreign_key_check` empty, backup holds v12, log event; F2 unregistered build accepts; F3 24 refusals (open transaction, history v11 / newer / tampered name, objects partial / unexpected, index missing, journal row, resolution row, orphan resolution row, `protection_state`, each of the 13 events), each with no write and no backup; F3b the length check alone; F4 `KeyboardInterrupt` → v12 intact, settings restored; F5 repeat refuses |
| G ordering | 8 | G1 v11 rollback refuses v12; G2 v12→v11→v10, trading kept, v10 build accepts; G3 resolutions citing `broker_reads` block both rollbacks, `broker_reads` unchanged; G4 Q3 refuses with the dark schema present (empty and with resolutions), names `protection_operation_resolutions`, no backup, then dark rollback → v11 rollback succeeds; G5 the guard is generic (3 cases) |
| H isolation | 2 | H1 account-database stand-in: refused, no write, no backup; H2 `real_data_guard.recording()`: every `sqlite3.connect` under `tmp_path`, no `kite_terminal.db`, `unexpected_refusals() == []` |

## 6. Test results (project venv, `PYTHONDONTWRITEBYTECODE=1`)

`pytest.ini` already sets `addopts = -q`, so the brief's extra `-q` hides pytest's count line. Exact counts
were taken from `--junitxml` written to the session scratchpad. The flag adds a report and changes nothing
else.

### 6.1 Focused runs

| Command | Exit | Tests | Fail | Err | Skip | Guard |
|---|---|---|---|---|---|---|
| `tests/test_schema_v12.py` | 0 | 70 | 0 | 0 | 0 | refusals 0 (decoy 0, expected 0, **unexpected 0**); network 0 |
| `tests/test_schema_v12.py tests/test_schema_v11.py tests/test_dark_protection_schema.py` | 0 | 394 | 0 | 0 | 0 | refusals 0 (**unexpected 0**); network 0 |
| `tests/test_platform_pk22.py tests/test_harness_isolation.py tests/test_real_data_guard.py` | 0 | 114 | 0 | 0 | 0 | refusals 18 (decoy 15, expected 3, **unexpected 0**); network 1. These suites exercise the guard on purpose |

### 6.2 Full standing suite

The command was:

```
PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -m pytest -rs -p no:cacheprovider \
  --ignore=tests/test_calendar_parity.py --ignore=tests/test_dmb_handoff_roundtrip.py
```

The result: **exit 1. 3802 collected: 3795 passed, 1 failed, 6 skipped, 0 errors, in 917.9 s.**

The guard reported `refusals 19 (decoy 15, expected 4, unexpected 0); network refusals 1`, i.e. **unexpected
0**. The tested `db.py` and test file match the final files (SHA-256 re-checked).

### 6.3 Skips (6, all pre-existing and environmental)

- `test_dmb_inbox.py:384` ×3: symlinks need a privilege this account does not hold (WinError 1314).
- `test_instance_lock.py:194, :233, :674`: WIN-INT PENDING (L3 subst drive, L6 network share, R8 real sleep).

### 6.4 The one failure: `test_instance_lock.py::test_L5_the_real_lock_file_is_guarded` (not S2)

The test asserts that `backend/kite_terminal.db.instance.lock`, the instance-lock path beside the **real**
account database, does not exist. **It exists.** It also fails when run on its own (1 test, 1 failure). In
that run the guard refused the test's own lock attempt as *expected* (expected 1, unexpected 0), so the test
did not create the file.

It is unrelated to S2:
- the test reads only `instance_lock.lock_path_for` and `db._ACCOUNT_DB_FILE`, and S2 changed neither;
- `backend/instance_lock.py` and `tests/test_instance_lock.py` are byte-identical to their starting hashes;
- every S2 test database is under `tmp_path`, and H2 proves this for the v12 code paths;
- no session command started the application.

A real start of the Phase 2 build would leave exactly this file, but confirming that would mean inspecting
a protected path beside the account database. **I did not stat, open or otherwise access it.**

**User action:** find out whether the application was started from this worktree, and decide whether the
file should be removed before the suite is used as a gate again.

## 7. Mutation results

`scratchpad/mutate.py` ran the checks. Each mutation is one or more exact text replacements in
`backend/db.py`, and every anchor had to occur exactly once. The target tests ran after each one. The
original bytes were written back in `finally`, and the SHA-256 was re-verified after every mutation.

The unmutated baseline over all target tests passed (exit 0, 75/75, guard unexpected 0). **All 20 mutations
were caught.** "Caught" means exit ≠ 0 with test *failures*, not collection errors. `db.py` was
`5529fc0e…5990` before and after the run.

| # | Mutation | Target | Result |
|---|---|---|---|
| M1 | Registered duplicates skip the index instead of refusing | C1 | caught (2/2 fail) |
| M2 | `present` with the index refuses instead of a no-op | B5, B6 | caught (2/2) |
| M3 | `present` **without** the index treated as a no-op (Q2) | C3 | caught (2/2) |
| M4 | `DELETE … version = 12` dropped | F1, F2 | caught (2/2) |
| M5a | Journal-row count dropped | F3 | caught (1/24) |
| M5b | Resolution-row count dropped | F3 | caught (1/24: orphan) |
| M5c | `protection_state` count dropped | F3 | caught (1/24) |
| M5d | `protection_*` event count dropped | F3 | caught (13/24) |
| M6a | `len(expected) == 12` dropped alone | F3b | caught (1/1) |
| M6b | Length **and** name checks dropped | A3 | caught (2/2) |
| M7 | Dark caller loses `registered=False` | S1 `10h` | caught (1/1) |
| M8 | Registered path calls `_require_current_schema` | B1 | caught (1/1) |
| M9 | Q3 guard removed | G4, G5 | caught (4/5) |
| M10 | Q3 guard placed after the backup | G4 (no-backup assertion) | caught (2/2) |
| M11 | Q3 guard does not skip the Phase 2 tables | G5, `test_schema_v11.py` | caught (16/26) |
| M12 | Q3 identifier unquoted | G5 | caught (1/3) |
| M13 | Q3 compares table names case-sensitively | G5 | caught (1/3) |
| M14 | `rollback_schema_v12` accepts a missing unique index | F3 | caught (1/24) |
| M15 | Registered `DarkSchemaRefused` not converted | C4 | caught (2/2) |
| M16 | Registered partial/unexpected refusal removed | C2 | caught (8/8) |

M1–M10 are the plan's list. M5 is split per count. M6 is split, because with the name check in place the
length check alone is caught only by F3b's gapped-history build. M11–M16 are additions.

One check was **not run**: removing the open-transaction refusal of `rollback_schema_v12`. The online backup
would wait on the connection's own write lock and could hang the run. That refusal is covered positively by
F3 `open_transaction`.

## 8. Worktree and frozen-file integrity (final, read-only)

| Check | Result |
|---|---|
| Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07f…`, unchanged |
| Staged / stash | 0 / 0. Nothing committed or pushed |
| Inventory | 101 at start → 104 entries: the starting 101 plus `?? tests/test_schema_v12.py`, `?? docs/PHASE_3_V12_REGISTRATION_RUNBOOK_DRAFT.md` and `?? docs/PHASE_3_S2_IMPLEMENTATION_REPORT.md`. The automated diff, taken before this report existed, showed exactly the first two |
| Hashes of the 101 starting entries | all byte-identical to the pre-edit snapshot **except `backend/db.py`**, the authorised extension |
| `backend/execution.py` | identical to HEAD (`git diff --quiet`) |
| PK-25 | **13/13** match (sha256, CRLF→LF, `research/pk25/validation_manifest.json` → `model_freeze`) |
| `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`) |

## 9. Safety confirmation

- I did not open, read, copy, hash, stat or otherwise access `.env*`, credentials, the account database, its
  backups, the real lock file (§6.4) or `backend/ui_prefs.json`.
- There were no broker calls and no external network requests. The application was never started.
- No migration ran against a real or existing database. Every migration and rollback ran on pytest
  temporary databases.
- No reset, clean, stash, checkout, branch switch, `git add` or commit.
- Process note: the S2 plan file lives outside the repository. Before `/add-dir` was granted, I read the
  previous session's transcript summaries with Bash, which is also outside the working directories. That
  yielded only the plan's file name. I stopped, the user added the plans directory, and the plan was then
  read through the normal tool.

## 10. Known limitations and deferred registration-gate items

1. **Registration itself** (DD-2) needs its own authorisation, with:
   - the operator backup (Ph2 §11, not done);
   - the DD-6 preflight;
   - a `rollback-schema-v12` CLI (runbook P5).
2. **R2-4 preflight predicate undefined** (Q4b). "Open executed BUY with NULL mode" has no SQL definition yet.
   It must be decided before registration is authorised. No preflight code exists.
3. **Dark entry points after registration.** Once `LATEST_SCHEMA_VERSION = 12`, `rollback_dark_protection_schema`
   on a v12 scratch database would drop the objects and leave `schema_version` row 12 behind. The fix is to
   retire the dark entry points or make them refuse a v12 history in the registration change (runbook §6).
4. **v10→v12 backup naming.** The single `pre_schema_v12_bak` holds the v10 image. This is disclosed in
   runbook §3.2.
5. **Once engine data exists, rollback means restoring a verified backup.** `rollback_schema_v12` refuses any
   held data by design.
6. `db.py` still defines `_normalised_sql` twice (Phase 2 and dark; the later one wins). S2 changed neither
   definition, as the plan required. This is flagged for a later clean-up decision.
7. The environmental L5 failure (§6.4) needs the user's attention.

## 11. Statement

S2 is complete: implementation, tests, mutations, integrity checks and both documents.

- Migration v12 was **not registered**, and `MIGRATIONS` / `LATEST_SCHEMA_VERSION` remain at v11.
- **S3 was not started.**
- No protection engine was implemented, and live entries were not activated.

Work stops here pending separate user authorisation.
