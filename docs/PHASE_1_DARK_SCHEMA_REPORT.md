# Phase 1: dark schema for the GTT-after-fill lifecycle

**Scope:** Phase 1 of `docs/PROPOSAL_GTT_AFTER_FILL_LIFECYCLE_REVIEW.md` (§12: "migrations,
indexes, event types; nothing writes to them yet"), and nothing else.

**The schema is dark.** It is not registered in `MIGRATIONS`. `init_db()` never applies it,
`LATEST_SCHEMA_VERSION` stays `10`, and no application module reads or writes it. It exists only
on scratch databases, where the Phase 1 tests create it explicitly. The real account database has
not been changed and cannot be changed by this code, because the new functions refuse it (§6.3).

**Not done, by design:**
- no protection engine;
- no GTT creation after fills, and no modify, cancel, adoption, fingerprinting or recovery logic;
- no operation processing, retry workers, instance lock, fencing epoch or startup reconciliation;
- no change to order, entry, exit, Emergency Exit or position behaviour;
- `backend/execution.py` and the PK-25 files are untouched, and `LIVE_ENTRIES_ENABLED` stays `False`;
- no broker contact, scheduler or DMB pipeline;
- nothing staged or committed.

---

## 1. Recorded starting state (2026-11-07)

| Item | Value | How established |
|---|---|---|
| Repository | `D:/Claude_WS/kite-terminal` | `pwd` |
| Branch | `feature/signal-detail-and-strategy-revision` | `git rev-parse --abbrev-ref HEAD` |
| HEAD | `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef` (expected `35a0e07`) | `git rev-parse HEAD` |
| Working tree | 37 tracked files modified, 12 untracked, nothing staged. Identical to the session's reference snapshot. Includes the Phase 0 changes and `docs/PHASE_0_TEST_HARNESS_SAFETY_REPORT.md`. Saved to the scratchpad as `status_before.txt`. | `git status --porcelain=v1 --untracked-files=all`, `git diff --cached --name-only` |
| `backend/execution.py` | identical to HEAD | `git diff --quiet HEAD -- backend/execution.py` |
| PK-25 frozen hashes | **13 / 13 match** (sha256, CRLF→LF, `research/pk25/validation_manifest.json` → `model_freeze.production_file_sha256`) | recomputed with a script |
| `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`) | grep |
| Protected files | `.env`, `backend/ui_prefs.json` and 41 `backend/kite_terminal.db*` files (43 in total). Size and mtime were recorded with `os.stat` before any work; none of them was opened. | scratchpad `protected_stat_before.json` |
| SQLite / Python | SQLite 3.50.4, Python 3.13.14 | `sqlite3.sqlite_version` |

---

## 2. Decisions taken before implementation

The proposal leaves four points open or in tension with this brief. Each was put to the user
before any code was written. The user chose the recommended option every time:

| # | Question | Decision |
|---|---|---|
| D1 | **Activation.** The repository's migration mechanism (PK-22A, `backend/db.py` `migrate()`) is forward-only and applies every `MIGRATIONS` entry automatically at `init_db()`. Registering Phase 1 there would migrate the real account database on the next app start. That conflicts with "never silently modify an existing account database" and with the requirement for a rollback. | **Dark, not registered.** Written as `_m011_*` / `_r011_*` in `db.py` and run through a transaction runner that mirrors `migrate()`, but kept out of `MIGRATIONS`. |
| D2 | **Objects.** Proposal §11 also lists `app_instance`, `instrument_attribution` and an optional `broker_gtt_observations`, without keys, types or seed semantics. Their consumers are Phases 2, 7 and 5. | **Fully specified objects only.** The other three are deferred to the phases that define them. |
| D3 | **Constraints.** §5.2 gives DDL, but some invariants appear only in prose, and `protection_state` has no value set. | **The §5.2 DDL plus CHECKs taken from its prose** (§3.2), and `protection_state` restricted to the 15 §3.3 names or NULL. |
| D4 | **Duplicate `kite_gtt_id`.** §5.2 / Z3 says: skip the index, raise an URGENT finding, block entries. Writing a finding during the migration would be a data write to an existing table. | **Skip the index, log a warning, report the duplicates in the result.** This follows the `_ensure_single_exit_owner_index` precedent. No `reconciliation_log` write; the URGENT finding is deferred to the activating phase. Entries are already blocked by `LIVE_ENTRIES_ENABLED = False`. |

---

## 3. Schema objects introduced

All objects are created in one transaction and removed in one transaction.

| Object | Source in proposal | Purpose |
|---|---|---|
| table `protection_operations` | §5.2.3 DDL | write-ahead journal of every GTT write |
| unique partial index `idx_protection_ops_one_open` | §5.2.3 | at most one open operation per position |
| unique partial index `idx_trade_tickets_unique_gtt` | §5.2.3 | a GTT protects at most one ticket |
| column `trade_tickets.protection_state` | §5.2.3 (prose), §3.3 | denormalised Protection-region state |
| 6 `ticket_events.event_type` values | §5.2.3 (prose) | `protection_pending`, `protection_submitted`, `protection_confirmed`, `protection_unknown`, `protection_adopted`, `protection_cancelled` |

Not introduced (D2): `app_instance`, `instrument_attribution`, `broker_gtt_observations`, any
`protection_state` index, and any lookup index the proposal does not define. The exit-owner index
predicate is unchanged, because §11 extends it only if a GTT-exit owner state is added, and none is.

### 3.1 `protection_operations`: field definitions

The columns, types, nullability and defaults are exactly the §5.2.3 DDL (test 3 asserts each
`PRAGMA table_info` row):

| Column | Type | Null | Default | Constraint |
|---|---|---|---|---|
| `id` | INTEGER | PK | AUTOINCREMENT | |
| `op_uuid` | TEXT | NOT NULL | | UNIQUE |
| `account_id` | INTEGER | NOT NULL | | FK `accounts(id)` |
| `position_id` | INTEGER | NOT NULL | | FK `trade_tickets(id)` |
| `mode` | TEXT | NOT NULL | | IN (`live`, `dry_run`) |
| `kind` | TEXT | NOT NULL | | IN (`create`, `modify`, `cancel`) |
| `reason` | TEXT | NOT NULL | | free text (§5.2 lists examples ending "…") |
| `state` | TEXT | NOT NULL | | IN (`intent`, `sending`, `acknowledged`, `confirmed`, `rejected`, `unknown`, `adopted`, `abandoned`, `superseded`, `needs_review`) |
| `basis_filled_quantity` | INTEGER | NOT NULL | | ≥ 0 * |
| `basis_exits_filled` | INTEGER | NOT NULL | | ≥ 0 * |
| `requested_quantity` | INTEGER | NOT NULL | | ≥ 0 |
| `request_fingerprint` | TEXT | NOT NULL | | `json_valid` * |
| `request_payload` | TEXT | NOT NULL | | `json_valid` * |
| `target_gtt_id` | INTEGER | NULL | | required for modify / cancel * |
| `result_gtt_id` | INTEGER | NULL | | required for an acknowledged, confirmed or adopted create * |
| `instance_epoch` | INTEGER | NOT NULL | | |
| `attempts` | INTEGER | NOT NULL | 0 | ≥ 0 *; ≤ 1 for a create |
| `complete_reads_after` | INTEGER | NOT NULL | 0 | ≥ 0 * |
| `created_at` | TEXT | NOT NULL | `datetime('now')` | |
| `sent_at` | TEXT | NULL | | NULL in `intent` *; set in every sent state * |
| `acknowledged_at`, `resolved_at`, `last_checked_at` | TEXT | NULL | | |
| `evidence` | TEXT | NULL | | NULL or `json_valid` * |

\* = a CHECK taken from the proposal's prose (D3), not written in its DDL:

| CHECK | Source |
|---|---|
| `kind = 'create' OR target_gtt_id IS NOT NULL` | §5.2.3 comment "target_gtt_id -- modify / cancel"; §5.5 "modify targets a known id" |
| `state != 'intent' OR sent_at IS NULL` | §3.7 and §8 step 3: an `intent` op with `sent_at IS NULL` "has sent nothing" |
| `state NOT IN ('sending','acknowledged','confirmed','rejected','unknown','adopted') OR sent_at IS NOT NULL` | §3.7: `sending` is committed immediately before the call; every later state follows it |
| `kind != 'create' OR state NOT IN ('acknowledged','confirmed','adopted') OR result_gtt_id IS NOT NULL` | §3.7: SUBMITTED → ACKNOWLEDGED links the returned id; adoption links a found id |
| counters and bases `≥ 0` | quantities and counts |
| `json_valid(...)` on fingerprint, payload, evidence | §5.2.3 comments: "canonical JSON", "JSON of the reads" |

`abandoned`, `superseded` and `needs_review` impose no `sent_at` rule. `abandoned` covers both
"not sent" (NULL) and "absent after settle" (sent); see §8, question Q2.

`target_gtt_id` and `result_gtt_id` have INTEGER affinity, like `trade_tickets.kite_gtt_id`. A paper
id (`'PAPER-G-<n>'`, §5.8) is therefore stored as text, which test 7c asserts.

### 3.2 Indexes

```sql
CREATE UNIQUE INDEX idx_protection_ops_one_open ON protection_operations(position_id)
  WHERE state IN ('intent', 'sending', 'acknowledged', 'unknown', 'needs_review');
CREATE UNIQUE INDEX idx_trade_tickets_unique_gtt ON trade_tickets(kite_gtt_id)
  WHERE kite_gtt_id IS NOT NULL;
```

These are verbatim from §5.2.3. `needs_review` is open on purpose (§5.2.3): it blocks every
automated write on the position.

### 3.3 `trade_tickets.protection_state` and `ticket_events`

- `protection_state TEXT CHECK (protection_state IS NULL OR protection_state IN (<the 15 §3.3 names>))`.
  It is added with a plain `ALTER TABLE ADD COLUMN`. Every existing row is `NULL`, meaning "not
  tracked by the journal".
- `ticket_events` is rebuilt (the existing `_ensure_*_event_type` pattern) with the six types
  appended to the end of its allow-list. Every row, the index `idx_ticket_events_ticket` and the
  AUTOINCREMENT high-water mark are kept, including the high-water row's position in
  `sqlite_sequence`.

---

## 4. Migration and rollback design

Code: `backend/db.py:2287-2700`, appended after `init_db()`. The only other change to `db.py` is
`import re`. `migrate()`, `MIGRATIONS`, `init_db()` and every existing function are byte-identical:
the diff is 417 added lines and 0 removed.

| Function | Role |
|---|---|
| `dark_protection_schema_status(conn)` | Read-only. `absent`, `present` (every object exactly in the form this code creates), `partial`, or `unexpected` (a same-named object in another form). |
| `apply_dark_protection_schema(conn)` | Checks, takes a backup, then runs `_m011_protection_journal_dark` in one transaction. |
| `rollback_dark_protection_schema(conn)` | Checks, takes a backup, then runs `_r011_protection_journal_dark` in one transaction. |
| `_run_dark_step` | Mirrors `migrate()`: foreign keys off around the transaction, `BEGIN IMMEDIATE`, no new FK violations, `COMMIT`, and `ROLLBACK` on any exception including `BaseException`. Restores the connection's previous `foreign_keys` setting and isolation level. |
| `DarkSchemaRefused` | `error_code = "DARK_SCHEMA_REFUSED"`. Raised before anything is written. |

**Checks before any write, in order:**
1. the connection is not the account database or one of its backups (§6.3);
2. the connection has no open transaction;
3. the schema history is exactly `LATEST_SCHEMA_VERSION` (`_check_history`; a database from newer
   or different code raises `DatabaseSchemaUnsupported`);
4. the status is not `partial` or `unexpected`.

Inside the transaction, `_m011` re-checks `absent`, then:
- verifies that `ticket_events` has exactly its five known columns and no trigger or view;
- creates the objects;
- verifies that the row count is unchanged and that the stored SQL is as intended;
- ends with a `present` check.

**Idempotency.** On `present` with the unique index, apply returns `already_present` and writes
nothing, not even a backup. On `present` without the unique index (skipped earlier because of
duplicates), apply re-checks only that index and creates it once the duplicates are gone. No
`schema_version` row is ever written: one would make every current build refuse the database
(`_check_history`).

**Rollback** removes exactly what apply creates:
- both indexes, the table, and the column (SQLite ≥ 3.35 `DROP COLUMN`);
- the six event types, removed from the allow-list. Because apply appended an exact suffix and
  rollback removes that same suffix, the stored `CREATE TABLE` text of `ticket_events`, and of
  `trade_tickets`, is restored **byte for byte** (test 8).

Rollback **refuses**, writing nothing, while the dark schema holds any data: a journal row, a
non-NULL `protection_state`, or a `protection_*` event. So it can never delete application data.
On `absent` it returns `already_absent`.

**Backups.** As `migrate()` does, both apply and rollback take a verified online backup
(`backup_database`, `PRAGMA integrity_check`) of a file database before changing it:
`<db>.pre_dark_protection_schema_bak` and `<db>.pre_dark_protection_rollback_bak`. Neither runs on
the account database.

---

## 5. Test coverage and exact results

New file: `tests/test_dark_protection_schema.py`. It has 42 test functions, 141 collected tests
after parametrisation. Every test uses the Phase 0 `env` fixture (temporary database, FakeKite,
guard active). Test 12 also uses a second scratch database in the same `tmp_path`.

| Brief item | Tests |
|---|---|
| 1 creation | `test_1`, `test_1b` (dark: `init_db` does not create it; version stays 10; no `schema_version` row) |
| 2 repetition | `test_2` (×3, schema, content and backups unchanged; no duplicate names), `test_2b` (restart `init_db` over it), `test_2c` (apply / rollback cycles) |
| 3 definitions | `test_3` (every column, type, NOT NULL, default, PK, FK), `test_3b` (both indexes unique and partial, columns, predicates; no extra index), `test_3c` (exactly six event types appended; columns and index kept) |
| 4 uniqueness | `test_4` (op_uuid), `test_4b` (5×5 open-state pairs), `test_4c` (terminal states never block; resolving frees the position), `test_4d` (GTT id, live and paper; NULLs) |
| 5 foreign keys | `test_5` (position, account; a journaled position cannot be deleted) |
| 6 states | `test_6` (10 states × 3 kinds), `test_6b` (8 out-of-enum values), `test_6c` (15 `protection_state` names, NULL; 4 invalid), `test_6d` (event types exist only with the schema) |
| 7 malformed rows | `test_7` (18 inconsistent rows), `test_7b` (13 NOT NULL columns), `test_7c` (the allowed variants, paper id stored as text) |
| 8 rollback | `test_8` (byte-identical schema, integrity and FK checks), `test_8b` (idempotent; no-op when absent), `test_8c` (with the unique index skipped) |
| 9 rollback keeps data | `test_9` (every row of every table, `sqlite_sequence` included, identical before / during / after; no id reuse), `test_9b` (refuses while holding data: 3 cases) |
| 10 failure handling | `test_10` (not fully migrated), `test_10b` (newer version), `test_10c` (4 partial or foreign forms), `test_10d` (unexpected `ticket_events` shape), `test_10e` (failure mid-apply: `RuntimeError` and `KeyboardInterrupt`, full rollback), `test_10f` (failure mid-rollback), `test_10g` (open transaction), `test_10h` (duplicate GTT ids: skip, warn, report, then create once fixed), `test_10i` (connection settings restored) |
| 11 workflows leave it unused | `test_11` (no `backend/` module except `db.py`, and no `frontend/` file, names any dark object), `test_11b` (nothing in `db.py` before the dark section refers to it), `test_11c` (a live lifecycle over the dark schema), `test_11d` (dry-run entry and Emergency Exit over the dark schema) |
| 12 no account data | `test_12` (refuses an account-named database and two backup names; schema unchanged; no backup written), `test_12b` (the guarded path is `backend/kite_terminal.db`), `test_12c` (every `sqlite3.connect` during apply and rollback is under `tmp_path`; no unexpected refusal) |
| Z5 | `test_z5_the_live_entry_gate_constant_is_still_false` |

### 5.1 Commands executed

All commands were run from `D:/Claude_WS/kite-terminal` with `.venv/Scripts/python.exe`.
`pytest.ini` already sets `addopts = -q`.

1. Focused, during development: `python -m pytest tests/test_dark_protection_schema.py -q -x -p no:cacheprovider`.
   The runs and their faults, all corrected before the final run:
   - **Run 1:** 101 passed, then 1 failure (`-x`): a parameter-name clash in my test helper.
   - **Run 2:** 6 failures, from four different faults:
     - a test used a nonexistent column;
     - rollback restored `ticket_events`' `sqlite_sequence` row with its value but at a new row
       position. I fixed the code so the row keeps its rowid;
     - two of those failures were the incident in §6.4: `test_10e` called `monkeypatch.undo()`,
       which also undid the `env` fixture's `DB_PATH` redirect.
   - **Run 3:** hung in `test_10g`. This was a real ordering defect in the code: the open-transaction
     check ran after the pre-change backup, and `conn.backup()` waits on the connection's own write
     lock. I fixed it so the check now runs first, before any backup.
2. Focused, final: `python -m pytest tests/test_dark_protection_schema.py -p no:cacheprovider -o faulthandler_timeout=60`
   → **141 passed, 0 failed, 0 skipped** (23.41 s), exit 0. Guard: `refusals: 0 (decoy 0, expected 0, unexpected 0); network refusals: 0`.
3. Mutation checks: each one changed `backend/db.py`, ran the relevant tests, then restored the
   file from a scratchpad copy and verified its sha256 (`9ef0a9ae…`). Every mutation was caught:

   | Mutation | Result |
   |---|---|
   | M1 `needs_review` dropped from the open-op index | 10 failed |
   | M2 unique GTT index created despite duplicates | 2 failed |
   | M3 rollback skips the holds-data check | 3 failed |
   | M4 `intent` may carry `sent_at` | 1 failed |
   | M5 rebuild loses the `sqlite_sequence` high-water mark | 2 failed |
   | M6 account-database refusal disabled | 1 failed |

4. Full suite: **NOT COMPLETED.** The command was the same as Phase 0's (which established that it
   is safe):
   `python -m pytest -rs -p no:cacheprovider --ignore=tests/test_calendar_parity.py --ignore=tests/test_dmb_handoff_roundtrip.py`.
   It ran in the background and was **stopped by Claude Code because the machine was critically low
   on memory**. That says nothing about the tests. It had reached about 28 % of the suite: about 930
   tests passed, 3 skipped (the known `test_dmb_inbox.py` symlink skips), and no failure or error
   marker appeared in the progress output. It was not restarted, as instructed when memory is short.
   **No full-suite result is claimed.** The two `--ignore`d DMB interpreter files were not run, by
   rule.

### 5.2 Evidence that existing data is preserved (scratch databases)

- `test_9` seeds positions (live GTT id, paper GTT id, no GTT id), an exit, events of five types, a
  deleted high-id event and a reconciliation finding. It then compares **every row of every table**,
  `sqlite_sequence` included:
  - before apply versus during, ignoring only the new NULL column → identical;
  - before apply versus after rollback → identical.

  The next event id after rollback is the deleted id + 1, so no id is reused.
- `test_8` compares every stored `CREATE` statement before apply and after rollback: identical.
  `PRAGMA integrity_check` returns `ok` and `PRAGMA foreign_key_check` is empty.
- `test_10e` and `test_10f` inject failures after the table rebuild. Schema and content are
  identical to before the attempt.

### 5.3 Evidence that the new tables are unused by existing workflows

- **Statically** (`test_11`, `test_11b`): no file in `backend/` other than `db.py`, and no file in
  `frontend/`, contains any of the following: the table, column or index names; the six event-type
  literals; the function names; the state-tuple names. Within `db.py`, nothing before the dark
  section refers to them, so `init_db`, `migrate` and `MIGRATIONS` cannot reach them.
- **Dynamically** (`test_11c`, `test_11d`), with the dark schema applied to the test database, the
  existing workflows run unchanged through their real entry points:
  - a live entry through create → confirm (the frozen `execution.py` places the GTT, as today);
  - a partial fill, then a complete fill, through the order monitor (legacy `modify_gtt`);
  - a trailing tick and a reconciliation cycle;
  - a full exit that fills (legacy `delete_gtt`, position closed);
  - a dry-run entry and an Emergency Exit.

  Afterwards: 0 `protection_operations` rows, no `sqlite_sequence` row for that table, every
  `protection_state` NULL, and 0 `protection_*` events (apart from the pre-existing
  `protection_failed`).

---

## 6. Evidence of isolation

### 6.1 Broker

No broker contact. The tests use FakeKite only. The Phase 0 network guard recorded 0 network
refusals in the focused run. No Kite MCP tool and no broker API was used.

### 6.2 Real `.env`

Not opened, read, copied or modified by me or by any test. The guard summary of every run shows
`python-dotenv neutralised: True`, and its size and mtime are unchanged (§9).

### 6.3 Real account database

- **The code refuses it.** `_refuse_account_database` refuses any connection whose file sits in
  `backend/` and whose name starts with `kite_terminal.db`, which covers the database and every
  backup. It decides from the resolved path alone, before any read or write. `_ACCOUNT_DB_FILE` is
  fixed at import, so a test's `DB_PATH` redirect cannot move it (`test_12`, `test_12b`, mutation M6).
- **The schema is not registered.** Nothing at startup can apply it (`test_1b`, `test_11b`).
- **The harness guard** (Phase 0) refuses every channel to `backend/kite_terminal.db*` before the
  file is opened.

### 6.4 Incident during development (disclosed)

In focused run 2, `test_10e` called `monkeypatch.undo()`. That reverted the `env` fixture's
`DB_PATH` redirect along with the test's own patch, so the next call to `db.get_connection()`
targeted the real path. The **Phase 0 guard refused both connection attempts before the file was
opened**: `UNEXPECTED REFUSAL: sqlite3.connect …\backend\kite_terminal.db` ×2, and the session was
marked failed.

The test was fixed to restore its patch with `setattr` instead of `undo()`. In every later run the
guard reported 0 refusals. The size and mtime of all 41 `kite_terminal.db*` files are identical to
the baseline taken before any work (§9).

Lesson for later phases: in a test that uses `env`, never call `monkeypatch.undo()`.

---

## 7. Compatibility considerations

- **No effect on any existing database.** No database is migrated, at startup or otherwise.
  `LATEST_SCHEMA_VERSION` is still 10, so `test_platform_pk22.py`, `test_live_preflight.py` and the
  readiness or diagnostics `schema_status` are unaffected.
- **When a later phase activates it** (separate authorisation):
  - register `_m011` as migration 11. It then runs inside `migrate()`'s transaction, so it must not
    call `_run_dark_step` itself;
  - update the two tests that pin version 10;
  - decide whether the duplicate-GTT case should then raise the URGENT finding (D4);
  - a database at v11 will be refused by older builds (`DATABASE_SCHEMA_UNSUPPORTED`) unless the
    rollback is run first. `_r011` would then also have to delete its `schema_version` row, which
    it does not do today because no such row exists.
- **`idx_trade_tickets_unique_gtt` constrains existing write paths** (`_record_execution_result`,
  `_reconcile_late_result`, test fixtures) once it exists. Today no code path puts one GTT id on two
  tickets: the synthesized GTT-exit and Emergency-Exit inserts never copy `kite_gtt_id`, and
  `test_11c`/`test_11d` run with it in place. A real database that already has duplicates keeps
  working, because the index is then skipped.
- **`SELECT * FROM trade_tickets`** readers (`journal`, `lifecycle`, `order_monitor`,
  `position_quantity`, `reconciliation`) see one extra NULL column where the schema exists. They
  access columns by name. The ticket-integrity digest uses an explicit field list, so it is
  unaffected.
- **SQLite features.** `DROP COLUMN` (≥ 3.35) and `json_valid` in CHECKs (built in since 3.38) are
  required. Python 3.13 here ships 3.50.4. An older SQLite would fail the apply inside its
  transaction and leave nothing behind.
- **Paper ids.** `PAPER-G-<n>` text ids share `idx_trade_tickets_unique_gtt` with integer live ids.
  The two namespaces cannot collide (`test_4d`).

---

## 8. Unresolved design questions (for the proposal's owner)

None of these blocked Phase 1. Each is recorded rather than resolved:

| # | Gap | Where | Why it matters later |
|---|---|---|---|
| Q1 | **`superseded`: a state or a flag?** §7.3 says Emergency Exit marks an open op `superseded` ("a local flag; the broker call may still land, so its resolution must cancel any resulting GTT"). As a *state*, it is outside the open-op index and terminal, yet it still needs a resolution, and a second op could be claimed meanwhile. | §5.2.3 enum vs §7.3 | Phase 6: probably needs a separate `superseded` flag column, or `superseded` added to the open-op predicate |
| Q2 | **`abandoned` has two meanings with different consequences.** "Not sent" allows an automatic re-create; "absent after settle" requires the user (§5.3). No column distinguishes them; only `sent_at` (NULL vs set) or `evidence` could. | §5.2.3, §5.3 | Phase 3: decide whether `sent_at IS NULL` is the rule, or add an explicit sub-reason |
| Q3 | **The exit-claim predicate and the open-op index disagree on `needs_review`.** §7.2's exit-claim `NOT EXISTS` uses `('intent','sending','acknowledged','unknown')`; the index includes `needs_review`. | §7.2 vs §5.2.3 | Phase 6: should a `needs_review` op block a manual exit? |
| Q4 | **`reason` has no value set** ("'fill', 'exit_fill', 'trailing', 'cleanup', 'user_recreate', ..."). Left as free text. | §5.2.3 | Phase 3: add a CHECK once the list is final (a table rebuild) |
| Q5 | **No cross-row consistency between the op and its position:** the op's `account_id` and `mode` are not forced to equal the position's. That would need triggers, which the proposal does not mention. | §5.2.3 | Phase 3: engine invariant or trigger |
| Q6 | **State transitions are not enforced by the schema.** For example, nothing stops `confirmed` → `intent`. The proposal defines transitions (§3.6/§3.7) but not storage enforcement. | §3.7 | Phase 3: engine CAS predicates, or triggers |
| Q7 | **`protection_state` value set.** The §3.3 names were used (D3). Whether the column should also carry `CLOSED_CLEANUP_REQUIRED` (a Position-region state, §3.4) or a separate column is undecided. | §3.3, §3.4 | Phase 3 / 6 |
| Q8 | **`target_gtt_id` / `result_gtt_id` declared INTEGER, while paper ids are text** (§5.8). Works through SQLite type affinity, as `kite_gtt_id` already does, but `STRICT` tables would reject it. | §5.2.3 vs §5.8 | Only if the schema ever moves to STRICT |
| Q9 | **No `updated_at`** on operations; the history of transitions lives only in `ticket_events` (new types) and `evidence`. | §5.2.3 | Phase 3 audit needs |
| Q10 | **The deferred objects** `app_instance` (seed row? `holder` format?), `instrument_attribution` (key `(account_id, isin)`? types?) and `broker_gtt_observations` need concrete DDL. | §5.2.2, §6.2, §5.6 | Phases 2, 7, 5 |

---

## 9. Changed files and final integrity checks

### 9.1 Changed and new files (this phase)

| File | Change |
|---|---|
| `backend/db.py` | **modified**: `import re` plus the dark section (`:2287-2700`). Additions only: +417 / −0. It was clean versus HEAD before this phase. |
| `tests/test_dark_protection_schema.py` | **new**: 837 lines, 42 test functions, 141 tests |
| `docs/PHASE_1_DARK_SCHEMA_REPORT.md` | **new**: this report |

No other file was changed. `docs/TESTING.md` was deliberately not touched, so the report is the
only documentation change.

### 9.2 Integrity checks at completion

| # | Check | Result |
|---|---|---|
| 1 | Branch / HEAD | `feature/signal-detail-and-strategy-revision` / `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef`, unchanged |
| 2 | Working tree before / after | Before: 37 modified + 12 untracked (the pre-existing work, Phase 0 included). After: identical, plus ` M backend/db.py`, `?? docs/PHASE_1_DARK_SCHEMA_REPORT.md`, `?? tests/test_dark_protection_schema.py` (`diff status_before.txt status_after.txt` shows only those three lines) |
| 3 | Changed / new files | as §9.1 |
| 4 | `backend/execution.py` | byte-identical to HEAD (`git diff --quiet HEAD -- backend/execution.py`) |
| 5 | PK-25 frozen hashes | **13 / 13 match** (sha256, CRLF→LF, manifest `model_freeze`) |
| 6 | `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`) |
| 7 | Real `.env` / account database | not read, copied or modified: size and mtime of all 43 protected files (`.env`, `ui_prefs.json`, 41 `kite_terminal.db*`) are identical before and after; no file was added or removed in `backend/` (no dark-schema backup there); the only real-path events were the two pre-open refusals in §6.4 |
| 8 | Broker operations | none (FakeKite only; 0 network refusals; no Kite MCP tool used) |
| 9 | Staged / committed | nothing staged (`git diff --cached --name-only` is empty), no commit |

---

## 10. Completion decision

**PHASE 1 PARTIAL.**
- **Established:** the implementation is complete. Schema and migration tests pass (141 / 141),
  rollback is verified byte for byte, existing data is preserved, dark-schema isolation is verified
  (statically and through live and dry-run workflows), all six mutations are caught, and every
  integrity check passes.
- **Unresolved:** the full regression suite did not complete. The system stopped it for low memory
  at about 28 %. It needs to be re-run on request (same command, §5.1 item 4) before this can be
  upgraded to PASS. Because the change is additive and unregistered, a regression is unlikely, but
  that is not evidence.

## 11. Explicit confirmation

- **No trading behaviour changed.** No order, entry, exit, Emergency Exit, protection or position
  code path was modified. The only production file touched is `backend/db.py`, and only by
  additions that nothing calls.
- The schema is dark: not registered, not applied at startup, not referenced by any application
  module, and never applied to the account database.
- No GTT-after-fill engine, GTT creation, modification, cancellation, adoption, fingerprinting,
  recovery, worker, lock, fencing or startup reconciliation was implemented. Phase 2 was not started.
- `backend/execution.py` and every PK-25 frozen file are byte-identical. `LIVE_ENTRIES_ENABLED` is
  `False`.
- No broker operation of any kind took place.
- The real `.env` and the account database were not read, copied or modified (§6.4 describes the
  two connection attempts the guard refused before opening).
- Nothing was staged or committed.

---

## 12. Final verification addendum (2026-11-07)

Sections 1 to 11 above are the original implementation report and are unchanged. This addendum
completes the outstanding item from §5.1 item 4 and §10. It was done in a separate session, as
verification only. No application, test or configuration file was changed.

### 12.1 Pre-run checks

| Check | Result |
|---|---|
| Path / branch / HEAD | `D:/Claude_WS/kite-terminal`, `feature/signal-detail-and-strategy-revision`, `35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef` |
| Working tree | 38 ` M` + 14 `??` (52 entries), nothing staged. This is exactly the §9.2 after-state. |
| Phase 1 files | `backend/db.py` sha256 `9ef0a9ae…` (the same as §5.1 item 3; +417 / −0 versus HEAD), `tests/test_dark_protection_schema.py` sha256 `e799b58e…`; this report is present |
| `backend/execution.py` / PK-25 / `LIVE_ENTRIES_ENABLED` | identical to HEAD / 13 of 13 match (11 `production_file_sha256` + 2 `model_file_sha256`, CRLF→LF) / `False` (`backend/live_entry_gate.py:40`) |
| Protected files | 43 (`.env`, `ui_prefs.json`, 41 `kite_terminal.db*`); size and mtime taken with `os.stat` only, none opened |
| System memory before the run | 15.65 GB total. Available 4.59–4.65 GB (30 s sample), then 4.96 GB right before the start. Commit 74.6–76.5 %, paging file 15 %. No process unusually large (largest: a browser process at 658 MB). The 4 GB threshold was met, so the run went ahead and no process was terminated. |
| Parallelism | pytest 9.1.1; pytest-xdist not installed, so the run was sequential. `pytest.ini` is unchanged (`addopts = -q`). |

### 12.2 Run

- **Command** (exactly §5.1 item 4, from the repository root):
  `.venv/Scripts/python.exe -m pytest -rs -p no:cacheprovider --ignore=tests/test_calendar_parity.py --ignore=tests/test_dmb_handoff_roundtrip.py`
- **Collected:** 3229 tests in 71 files. The same command with `--collect-only` exited 0.
- **Result:** **3226 passed, 0 failed, 0 errors, 3 skipped**, which accounts for all 3229 collected tests.
  Exit code **0**.
- **Duration:** 526.27 s according to pytest (0:08:46). Wall clock 536 s, from 16:37:34 to 16:46:30 IST.
- **Skips:** the 3 known `tests/test_dmb_inbox.py:384` symlink skips (`WinError 1314`, symlink
  privilege not held), the same as Phase 0. No other skip.
- **Exclusions:** only the two standing `--ignore`d DMB interpreter files
  (`test_calendar_parity.py`, `test_dmb_handoff_roundtrip.py`). They were not run, under the
  "no DMB pipeline" rule. Nothing else was deselected or excluded.
- **Completion:** the suite ran to `[100%]` without interruption. stderr was empty.
- **Memory:** a read-only monitor took 44 samples, one every 10 s. Minimum available memory was
  **4674 MB**. The pytest process tree peaked at **194 MB** working set. Commit stayed at
  72–76 %. The stop rule (under 1500 MB for 3 consecutive samples) never fired. The earlier low-memory
  stop was caused by the machine's overall state, not by the suite.
- **Isolation guard summary**, byte-identical to the clean Phase 0 baseline
  (`docs/PHASE_0_TEST_HARNESS_SAFETY_REPORT.md` §5, run 2):
  ```
  python-dotenv neutralised: True; implicit loads neutralised: 4; explicit loads delegated (temporary files): 4; explicit protected loads refused: 2
  inherited KITE_* variables scrubbed at start: 0
  guard refusals: 18 (decoy 15, expected 3, unexpected 0); network refusals: 1
  ```
  The single network refusal is the deliberate TEST-NET-1 test. The 3 expected refusals are the
  two deliberate real-DB-path tests and that network test.
- **Reconciliation with Phase 0:** Phase 0 ran 3085 passed and 3 skipped, 3088 in total. This run
  has 3229, which is 141 more: exactly the Phase 1 tests in `tests/test_dark_protection_schema.py`.

### 12.3 Integrity checks after the run

| # | Check | Result |
|---|---|---|
| 1 | Branch / HEAD | unchanged (`35a0e07fd24a0dd1eb9e0d3ebf9c4b4c8d089fef`) |
| 2 | Working tree | `git status --porcelain=v1` before and after is identical (52 entries); this addendum is the only edit |
| 3 | `backend/db.py`, `tests/test_dark_protection_schema.py` | sha256 unchanged (`9ef0a9ae…`, `e799b58e…`) |
| 4 | `backend/execution.py` | identical to HEAD (`git diff --quiet HEAD -- backend/execution.py`) |
| 5 | PK-25 frozen hashes | **13 / 13 match** |
| 6 | `LIVE_ENTRIES_ENABLED` | `False` (`backend/live_entry_gate.py:40`) |
| 7 | Real `.env` / account database | not accessed: size and mtime of all 43 protected files identical before and after; `backend/` listing unchanged; guard `unexpected 0` |
| 8 | Broker operations | none: FakeKite only, no Kite MCP tool used, no scheduler or DMB pipeline run |
| 9 | Staged / committed | nothing staged (`git diff --cached --name-only` is empty), no commit, no stash |

### 12.4 Final decision

**PHASE 1 PASS.** The full suite completed: 3226 passed, 0 failed, 3 known skips, exit 0. There
were no unexpected failures and every integrity check passed. This supersedes the PARTIAL decision
in §10, which recorded only that the full suite had not yet completed. Phase 2 has not been started
and waits for separate authorisation.
