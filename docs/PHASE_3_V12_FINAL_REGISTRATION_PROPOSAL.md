# Migration v12: final registration proposal

> **PROPOSAL ONLY — NOT EXECUTED — NOT AUTHORISATION.** Migration v12 was **not** registered. No migration, rollback
> or preflight was run against the account database or any backup of it. `MIGRATIONS` still ends at
> `(11, "phase2_instance_coordination", …)` and `LATEST_SCHEMA_VERSION` is 11 (verified 2026-11-11).

Supersedes nothing: it completes `docs/PHASE_3_V12_REGISTRATION_RUNBOOK_DRAFT.md` (S2), whose procedure stays the
operational reference. Sources reviewed: `backend/db.py` (`_m012_registered`, `_m012_protection_journal`,
`_r012_protection_journal`, `rollback_schema_v12`, `dark_protection_schema_status`), `tests/test_schema_v12.py`,
the runbook draft, R6 DD-2, DD-6 rev. 2 (R2-4), M-9.

> **REVISED 2026-11-11 (V12 registration remediation, `docs/PHASE_3_V12_REGISTRATION_REMEDIATION_REPORT.md`).**
> §1, §4 and §5 below are corrected:
> - **§1:** the line originally proposed here would raise `NameError` when `backend.db` is imported, because
>   `_m012_protection_journal` is defined after `MIGRATIONS`. The entry is now the late-bound wrapper
>   `_migration_v12_protection_journal`, already defined above the list but not listed. The exact diff is in the
>   remediation report §11.
> - **§1 item 2:** the rollback CLI is now ported into the current `db_maintenance.py`, not copied over it.
> - **§4 and §5:** there is no "first registered start that migrates". A start refuses a pending v12, and only
>   `migrate-v12 --authorize-v12` applies it.
>
> The operational reference is the revised runbook, `docs/PHASE_3_V12_REGISTRATION_RUNBOOK_DRAFT.md`.

## 1. The exact registration change

~~One appended line in `backend/db.py`, after the v11 entry: `(12, "phase3_protection_journal",
_m012_protection_journal)`~~. **Corrected:** that line names a function defined ~900 lines after `MIGRATIONS` and
would raise `NameError` at import. The registration entry is the prepared late-bound wrapper:

```python
    (12, "phase3_protection_journal", _migration_v12_protection_journal),
```

plus comment updates and `tests/schema_builds.V12_REGISTERED_IN_THIS_BUILD = True` (the exact diff: remediation
report §11), with, in the same change (runbook P4, P5):

1. **Dark entry points** — **DONE 2026-11-11 (not registered).** `apply_dark_protection_schema` /
   `rollback_dark_protection_schema` now call `db._require_dark_history`, which requires history v1..v11 of this code
   exactly. Today (LATEST 11) this is identical to the previous check. On a registered build they refuse a v12
   database, so no dark rollback can leave `schema_version` row 12 behind. A mutation restoring the old check
   showed the hazard was real: the dark rollback would have run on a registered v12 database.
2. **`db_maintenance rollback-schema-v12 --confirm`** — built, then **WITHDRAWN** the same day. R6 M-9 kept this
   rollback off the CLI until registration, and `test_schema_v12::test_a2` pinned that. ~~Ready-to-apply copies
   are in the session scratchpad~~: those copies predate `preflight-v12` and `migrate-v12`, so copying them over
   the current file would delete both commands. **Revised 2026-11-11:** the command is now **ported** into the
   current `db_maintenance.py`. It is refused unless the build registers v12, so it does nothing today. It adds an
   independently verified backup, a disk-space check, results classified from the history and an audit record.
   Tests: `tests/test_rollback_v12_cli.py`. `test_a2` now pins that this is the only place the v12 rollback is
   exposed.
   Tests for item 1: `tests/test_v12_registration_prep.py` (4), on temporary databases, with registration simulated
   in-test.
3. **`db_maintenance preflight-v12 --db <backup>`** — **BUILT and tested on 2026-11-11 (temporary databases only;
   never run on the account database or a backup of it).** Exit 0 = PASS, 1 = FAIL, 2 = refused. Details in §12.
4. Static test update: `test_1b` / `MIGRATIONS[-1][0] == 11` becomes the v12 identity check.

## 2. Preconditions (all required, in order)

| # | Precondition |
|---|---|
| P1 | Separate, explicit user authorisation for registration (DD-2), given with the first engine wiring |
| P2 | Operator backup taken, `integrity_check: ok`, `foreign_key_check: 0`, status `v11 of v11`, SHA-256 recorded, one copy off-machine (runbook §2.1). **2026-11-11, reported by the operator:** backup `backend\kite_terminal.db.manual_20261111113642_bak`; `integrity_check: ok`; `foreign_key_check: 0 violation(s)`; size 6,594,560 bytes; SHA-256 `76792B2252D65524E05148B5B8A864897825E27043DB957D6420201A51FDA793`. Schema: the preflight's "schema v11 of this code: yes" (history exactly v1..v11, a stricter check than `status`'s `v11 of v11`). Off-machine copy: `E:\backup` (reported by the operator; not opened or hashed by the agent). **P2 complete.** |
| P3 | Preflight (§3) run **on that backup, read-only**, both counts zero. **Reported by the operator on 2026-11-11: PASS** (schema v11 of this code: yes; DD-6 duplicates 0; R2-4 open NULL-mode BUYs 0; diagnostic 0). Run by the operator, not by the agent, on `backend\kite_terminal.db.manual_20261111113642_bak` (opened read-only; `database:` line supplied by the operator) |
| P4 | The §1 items 1–4 implemented and tested (items 1–3 done 2026-11-11; item 4, the static-pin update, belongs to the registration change itself) |
| P5 | Full standing suite green; PK-25 13/13; `execution.py` identical to HEAD; `LIVE_ENTRIES_ENABLED = False`. **2026-11-11, before registration:** 4193 passed, 0 failed, 6 known skips; integrity all OK. To be repeated on the registration change itself |
| P6 | The app stopped; no other process on the database (the instance lock is free) |

## 3. DD-6 / R2-4 preflight — resolution of the open predicate

**Duplicate GTT ids (DD-6):** exactly `_duplicate_gtt_ids`:
`SELECT kite_gtt_id FROM trade_tickets WHERE kite_gtt_id IS NOT NULL GROUP BY kite_gtt_id HAVING COUNT(*) > 1` → 0 rows.

**Open executed BUY with NULL mode (R2-4).** The question was whether "open" and "executed" can be established from
authoritative schema and code semantics without inventing a definition. **They can.** The application already has one
definition of an open position, used by the risk limits (`_open_position_count`), `list_open_positions` (the exit
scanner, Stage 1's attribution and inventory) and therefore by every place that decides what is "open":

```sql
-- backend/risk_engine.py _OPEN_POSITION_WHERE (PK-25 frozen; read, not modified)
account_id = ? AND status = 'executed' AND mode = 'live' AND transaction_type = 'BUY'
  AND closed_at IS NULL
  AND id NOT IN (SELECT closes_ticket_id FROM trade_tickets WHERE closes_ticket_id IS NOT NULL
                 AND status = 'executed' AND entry_fill_status IS NULL)
```

It is mode-specific only through `mode = 'live'`. The R2-4 count is the same predicate over `mode IS NULL`:

**APPROVED by the user on 2026-11-11, as proposed below (no amendment).** This approves the predicate only. It
authorises neither registration nor running the preflight on any real database or backup. Both remain separate
decisions under §2 (P1, P3).

```sql
-- R2-4 preflight count (approved 2026-11-11)
SELECT COUNT(*) FROM trade_tickets
 WHERE status = 'executed' AND mode IS NULL AND transaction_type = 'BUY'
   AND closed_at IS NULL
   AND id NOT IN (SELECT closes_ticket_id FROM trade_tickets WHERE closes_ticket_id IS NOT NULL
                  AND status = 'executed' AND entry_fill_status IS NULL);
```

Notes: (a) all accounts, not one (`account_id` dropped), because any such row on any account cannot be journaled;
(b) it includes an executed entry whose fill is still pending (over-inclusion is the fail-safe direction); (c) a
second, broader **diagnostic** count is reported alongside, not gating: `status = 'executed' AND mode IS NULL AND
transaction_type = 'BUY' AND closed_at IS NULL` (ignoring exits), so a human sees every candidate. Both must be zero
to proceed; non-zero waits for a human to resolve the rows in Ticket History (`EXIT_MODE_UNRESOLVED` already refuses
their exits). **Not run** on any real database.

## 4. Backup procedure

**Revised 2026-11-11 (runbook §4.2, §4.4).** The operator backup: `python -m backend.db_maintenance backup` →
`kite_terminal.db.manual_<time>_bak`; `integrity --db <backup>`; `status --db <backup>` reads `v11 of v12` on the
registered build; record size + SHA-256; one copy off-machine. There is no "first registered start" that migrates:
`migrate-v12 --authorize-v12` checks free disk space, writes `pre_v12_authorized_<µs>_bak` and verifies it
**independently** (integrity, v11 history, FK count, every row count, SHA-256); `migrate()` then writes
`pre_schema_v12_bak`. A database below v11 is refused (Phase 2 build first), so the S2 v10 → v12 disclosure no
longer applies. Restoring a backup: only `restore-backup` (runbook §7, WAL/SHM-safe), never a file copy.

## 5. Integrity checks

**Revised 2026-11-11:** `migrate-v12` performs these itself and reports `V12_APPLIED` only when all pass; on a
failure after COMMIT it reports `MIGRATED_VERIFICATION_FAILED` (exit 4, runbook §9), never `FAILED_ROLLED_BACK`.
Before: P2 checks on the backup. After `migrate-v12`: `status` = `v12 of v12`; `integrity` ok with 0
foreign-key violations; `pre_schema_v12_bak` present (record SHA-256); `dark_protection_schema_status` = `present`
with `unique_gtt_index` true; row counts of every pre-existing table unchanged; `ticket_events` high-water mark kept.

## 6. Expected schema changes (exactly; S2 tests B1/B2)

`protection_operations`, `protection_operation_resolutions`, `idx_protection_ops_one_open_change`,
`idx_protection_ops_one_open_cancel`, `idx_trade_tickets_unique_gtt`; `trade_tickets.protection_state` (NULL on every
row); `ticket_events` rebuilt with the 13 `protection_*` event types appended; `schema_version` row 12. Nothing else.

## 7. Rollback dependencies and procedure

- Order: **v12 first, then v11** (`rollback_schema_v11` refuses a v12 history, and refuses while
  `protection_operation_resolutions` references `broker_reads`).
- `rollback_schema_v12` refuses unless the build registers v12, history is exactly v1..v12, the objects are exactly
  v12's, and they hold **no** data. It then backs up (`pre_rollback_v12_bak`), drops the v12 objects and row 12 in one
  transaction, and verifies v11.
- **Once the engine has written any row, rollback means restoring a verified backup** (`pre_schema_v12_bak` or the
  operator backup). Every trade recorded after it is lost. This must be stated to the user before authorisation.

## 8. Failure recovery

Any failure inside v12 (refusal, exception, interrupt, kill) rolls back v12 only; v11 stays committed and the Phase 2
build accepts the database; startup aborts and releases the instance lock. Refusals (nothing changed): duplicate GTT
ids (named); partial or foreign Phase 3 objects; objects present without the unique index; unexpected `ticket_events`
shape; a result that is not exactly v12's objects. Recovery: resolve the cause on a copy, or restore the operator
backup.

## 9. Compatibility consequences

- Builds with `LATEST_SCHEMA_VERSION` 11 (Phase 2) or 10 refuse a v12 database (`DATABASE_SCHEMA_UNSUPPORTED`): after
  registration, **downgrading the application requires the v12 rollback (empty journal) or a backup restore.**
- Nothing in the application reads or writes the v12 objects until the engine is wired (Phases 4–6); registration
  alone changes no trading behaviour. Stage 1's new `open_protection_operations` inventory (S8) starts counting once
  the table exists; with an empty journal it adds nothing.

## 10. Operator confirmations to collect before registration

1. "I have a verified operator backup, its SHA-256 recorded, and an off-machine copy."
2. "The preflight ran read-only on that backup and both counts are zero." (after approving the §3 predicate)
3. "I understand that once the journal holds data, rollback means restoring a backup and losing later trades."
4. "I understand that older builds will refuse the migrated database."
5. "The application is stopped and no other copy is running."

## 12. The preflight command (built 2026-11-11)

`python -m backend.db_maintenance preflight-v12 --db <operator backup>` (`backend/db_maintenance.py`):

- **Gating** (both must be zero, and the file must be a v11 database of this code): the DD-6 duplicate-GTT count and
  the approved R2-4 count (§3). **Reported, not gating:** the broader diagnostic count, each ticket listed for review.
- **Read-only by construction:** opened `mode=ro&immutable=1` with `PRAGMA query_only = ON`; no instance lock; no write
  statement in its code. `immutable=1` matters: a plain `mode=ro` open of a WAL-mode file creates `-wal`/`-shm`
  sidecars (found while testing; the existing `status`/`integrity` commands still do this). The backup is an offline
  copy, so `immutable` is appropriate.
- **Refusals before any filesystem access:** no `--db` (it never defaults to the live database); a file named exactly
  `kite_terminal.db` (the live file), wherever it is.
- **Output:** schema check, each count with the offending ticket ids, symbols and accounts, then `PASS` / `FAIL`.

Tests `tests/test_preflight_v12.py` (11): clean backup passes and every file in its directory is byte-identical
afterwards with no sidecar created; duplicates fail and are named; a NULL-mode open BUY fails and is listed; closed,
exit-closed, live, SELL and unexecuted rows do not count and the diagnostic never gates; **the count equals
`risk_engine._OPEN_POSITION_WHERE` with `mode IS NULL` on the same rows** (drift guard); a non-v11 history fails; the
live name is refused with no `Path.exists` call; `--db` required, missing file refused; the connection cannot write;
no write statement in the code; other commands still default to the app's database. 7 mutations, all caught.

## 11. Statement

Registration was **not** performed. No migration, rollback or preflight was executed against the real account
database, a backup of it, or any non-temporary database. Of the two decisions this document asked for, the §3
predicate was approved on 2026-11-11; authorisation of registration itself is still outstanding.
