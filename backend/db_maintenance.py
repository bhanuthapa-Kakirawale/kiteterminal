"""Database maintenance commands (PK-22G). Nothing here runs automatically.

    python -m backend.db_maintenance status      schema version, pending migrations, size
    python -m backend.db_maintenance integrity   PRAGMA integrity_check + foreign_key_check
    python -m backend.db_maintenance backup      online backup to kite_terminal.db.manual_<time>_bak
    python -m backend.db_maintenance analyze     refresh query-planner statistics (ANALYZE)
    python -m backend.db_maintenance vacuum      compact the file — STOP THE APP FIRST
    python -m backend.db_maintenance migrate     apply pending migrations — STOP THE APP FIRST
    python -m backend.db_maintenance rollback-schema-v11 --confirm
                                                 undo migration v11 (Phase 2) — STOP THE APP FIRST
    python -m backend.db_maintenance preflight-v12 --db <operator backup>
                                                 the read-only v12 registration preflight (DD-6, R2-4);
                                                 refuses the live kite_terminal.db
    python -m backend.db_maintenance migrate-v12 --authorize-v12 [--db PATH]
                                                 the ONLY way migration 12 is applied to a database holding
                                                 data (a normal start refuses it) — STOP THE APP FIRST.
                                                 This build registers migration 12.
    python -m backend.db_maintenance rollback-schema-v12 --confirm [--db PATH]
                                                 undo migration 12 (Phase 3) — STOP THE APP FIRST; refused
                                                 unless this build registers v12, the history is exactly
                                                 v1..v12 and the journal holds no data
    python -m backend.db_maintenance restore-backup --backup FILE --confirm [--db PATH]
                                                 replace the database with a verified backup — STOP THE APP
                                                 FIRST; stale -wal/-shm sidecars can never be replayed onto it
                                                 and the original is kept until the restore verifies

The schema changes: `migrate` runs exactly what an app start runs
(backend/db.init_db, with its own verified pre-migration backup), minus the
server and workers (LIVE-0), and refuses migration 12; `migrate-v12` applies
migration 12; the two rollback commands remove v12 / v11. `restore-backup`
replaces the whole file. `--db PATH` points any command at another file (for
example a copy).

Phase 2 (docs/PHASE_2_DESIGN_REVIEW.md §7.5): the commands that write the
database (analyze, vacuum, migrate, rollback-schema-v11, rollback-schema-v12,
restore-backup) take the terminal's instance lock (`<db>.instance.lock`)
without waiting, and refuse while it is held: the terminal, or another
maintenance command, is running. status, integrity and backup open the
database read-only (mode=ro) and never write it. migrate-v12 takes the same
lock itself, after its argument check (it is not in WRITING_COMMANDS: its
--authorize-v12 is checked before any filesystem access); the --confirm (and
--backup) of rollback-schema-v12 and restore-backup are also checked before
any filesystem access.

migrate-v12, rollback-schema-v12 and restore-backup check the free disk space
before their first backup or copy, and write an audit record (stdout/stderr,
a JSON file beside the database, a log line) for every outcome; the result is
decided from the migration history actually read afterwards
(docs/PHASE_3_V12_REGISTRATION_RUNBOOK_DRAFT.md).

V12 final readiness (output/audits/v12_final_readiness_2026-11-11/):
    python -m backend.db_maintenance migrate-v12 --authorize-v12 --offsite-record FILE [--db PATH]
    python -m backend.db_maintenance verify-backup --backup FILE [--db PATH] [--record-out FILE]
                                                 read-only: the off-machine backup verification record
    python -m backend.db_maintenance backup --dest DIR [--record-out FILE]
                                                 online backup into DIR (e.g. an external disk), verified,
                                                 with its verification record
    python -m backend.db_maintenance init-fresh --confirm-new-installation [--acknowledge-prior-installation]
                                                 the explicit way to create a NEW database where evidence of
                                                 an earlier installation exists (a normal start refuses)
    python -m backend.db_maintenance acknowledge-database --confirm
                                                 re-bind the installation record after an inspected, deliberate
                                                 replacement or move of the database
Every command that writes the database first refuses a target that is not on an internal fixed local disk
(backend/storage_classification.py), before the instance lock, any backup or any write; every one of them (except
restore-backup, the recovery path, and `migrate`, whose init_db checks it) also refuses a database whose installation
state is not sound (backend/installation_state.py). migrate-v12 additionally requires a FRESH off-machine backup
verification record, re-verifies that backup LIVE (integrity, SHA-256, logical digest equal to the target's, a
separate physical failure domain), binds the migration to the target's file identity and logical digest (re-checked
right before and INSIDE v12's transaction), writes an in-progress marker before its first write and removes it only
for an unambiguous, audited outcome. restore-backup never renames over an existing file and leaves a marker while
the original set is moved aside.
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from backend import backup_verification, db, installation_state, storage_classification


def _checkpoint(name: str) -> None:
    """A named point in migrate-v12 / restore-backup. Does nothing; the fault-injection tests replace it to crash or
    fail the process at exactly that point (tests/test_restore_fault_injection.py,
    tests/test_migrate_v12_fault_injection.py)."""


def _connect(path: Path):
    import sqlite3
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_readonly(path: Path):
    """status, integrity, backup: the database can be read, never written (mode=ro)."""
    import sqlite3
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


WRITING_COMMANDS = ("analyze", "vacuum", "migrate", "rollback-schema-v11", "rollback-schema-v12", "restore-backup")

# ---- the v12 registration preflight (docs/PHASE_3_V12_FINAL_REGISTRATION_PROPOSAL.md §3) ---------------------
#
# Read-only and gating: (1) DD-6, GTT ids linked to more than one ticket (db._duplicate_gtt_ids); (2) R2-4, open
# executed BUY positions with a NULL mode, the predicate APPROVED by the user on 2026-11-11: the application's one
# open-position definition (risk_engine._OPEN_POSITION_WHERE) over mode IS NULL, for every account. Reported but
# NOT gating: a broader diagnostic count that ignores exits, so a human sees every candidate.
# It runs on the operator backup only: a file named exactly like the live database is refused BEFORE any
# filesystem access (no stat, no open), and the file is opened mode=ro with query_only on.
LIVE_DB_NAME = "kite_terminal.db"
_R2_4_OPEN_NULL_MODE_SQL = db.V12_PREFLIGHT_OPEN_NULL_MODE_SQL  # one definition, shared with migrate()'s re-check
_R2_4_DIAGNOSTIC_SQL = (
    "SELECT id, account_id, symbol FROM trade_tickets "
    "WHERE status = 'executed' AND mode IS NULL AND transaction_type = 'BUY' AND closed_at IS NULL ORDER BY id"
)


def preflight_v12_refusal(path: Path) -> str | None:
    """Pure path check (no filesystem access): the live database is never a preflight target."""
    if path.name == LIVE_DB_NAME:
        return (f"preflight-v12 runs on the operator backup, never on the live database ({LIVE_DB_NAME}). Take the "
                "backup first (python -m backend.db_maintenance backup) and pass it with --db.")
    return None


def preflight_v12_counts(conn) -> dict:
    """The preflight's facts from a read-only connection. Never writes."""
    history = db.migration_history(conn) if db._table_exists(conn, "schema_version") else []
    versions = [(h["version"], h["migration_name"]) for h in history]
    expected_v11 = [(v, name) for v, name, _ in db.MIGRATIONS if v <= 11]
    rows = lambda sql: [dict(r) for r in conn.execute(sql).fetchall()]  # noqa: E731
    return {
        "schema_is_v11": versions == expected_v11,
        "schema_newest": versions[-1] if versions else None,
        "duplicate_gtt_ids": [r[0] for r in conn.execute(
            "SELECT kite_gtt_id FROM trade_tickets WHERE kite_gtt_id IS NOT NULL GROUP BY kite_gtt_id "
            "HAVING COUNT(*) > 1 ORDER BY kite_gtt_id").fetchall()],
        "open_null_mode_buys": rows(_R2_4_OPEN_NULL_MODE_SQL),
        "diagnostic_null_mode_buys": rows(_R2_4_DIAGNOSTIC_SQL),
    }


def _connect_preflight(path: Path):
    """mode=ro AND immutable=1: the operator backup is an offline copy no process uses, so SQLite takes no lock
    and creates no -wal/-shm sidecar (a plain mode=ro open of a WAL-mode file creates both). Not a byte is written."""
    import sqlite3
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def preflight_v12(path: Path) -> int:
    """0: both gating counts are zero and the file is a v11 database of this code. 1: anything else."""
    conn = _connect_preflight(path)
    try:
        conn.execute("PRAGMA query_only = ON")
        facts = preflight_v12_counts(conn)
    finally:
        conn.close()
    dup, open_null, diag = facts["duplicate_gtt_ids"], facts["open_null_mode_buys"], facts["diagnostic_null_mode_buys"]
    print(f"database:                         {path} (opened read-only)")
    print(f"schema v11 of this code:          {'yes' if facts['schema_is_v11'] else 'NO (newest ' + str(facts['schema_newest']) + ')'}")
    print(f"DD-6 duplicate GTT ids:           {len(dup)}{'  ' + str(dup) if dup else ''}")
    print(f"R2-4 open executed BUY, NULL mode: {len(open_null)}"
          + "".join(f"\n  ticket {r['id']} (account {r['account_id']}, {r['symbol']})" for r in open_null))
    print(f"diagnostic (not gating):          {len(diag)} executed BUY with NULL mode and no closed_at"
          + "".join(f"\n  review ticket {r['id']} (account {r['account_id']}, {r['symbol']})" for r in diag))
    ok = facts["schema_is_v11"] and not dup and not open_null
    print("result:                           " + ("PASS (registration may proceed to its next precondition)" if ok else
          "FAIL (registration must wait; resolve the rows above in Ticket History)"))
    return 0 if ok else 1


def rollback_v11(path: Path) -> int:
    """Phase 2 rollback: the written down-migration (backend/db.rollback_schema_v11). Drops only the five
    Phase 2 tables and schema_version row 11, after a verified backup; every trading table is kept. A
    pre-Phase-2 build then accepts the database again. STOP THE APP FIRST (the instance lock enforces it)."""
    conn = _connect(path)
    try:
        result = db.rollback_schema_v11(conn)
    except db.SchemaRollbackRefused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"database:     {path}")
    print("schema:       v11 -> v10 (Phase 2 objects removed; trading data unchanged)")
    print(f"backup:       {result['backup']}")
    return 0


def migrate(path: Path) -> int:
    """LIVE-0: bring a database current deliberately, without starting the
    server, the workers or the ticker — exactly the application's own
    initialisation (backend/db.init_db: the verified pre-migration backup,
    the ordered migrations, the startup invariants). STOP THE APP FIRST.

    The encryption key must come from .env: init_db() would otherwise mint a
    new key, and the stored (encrypted) Kite token could never be read again.
    """
    import os

    from dotenv import load_dotenv

    from backend import crypto
    load_dotenv(crypto.ENV_PATH)
    if not os.environ.get(crypto.ENV_VAR):
        print(f"{crypto.ENV_VAR} is not set (no .env at {crypto.ENV_PATH}): refusing to migrate — a new key "
              f"would make the stored Kite token unreadable.", file=sys.stderr)
        return 2
    db.DB_PATH = path
    conn = _connect(path)
    try:
        before = db.schema_status(conn)
    finally:
        conn.close()
    if not before["supported"]:
        print(f"refused: {before['error']}", file=sys.stderr)
        return 1
    try:
        db.init_db()
    except db.DatabaseMigrationAuthorizationRequired as e:
        # Never a way round the gate: migration 12 is applied only by migrate-v12 --authorize-v12.
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(f"database:            {path}")
    print(f"schema:              v{before['current_version']} -> v{db.last_init['schema_version']}")
    print(f"applied migrations:  {db.last_init['applied_migrations'] or 'none'}")
    print(f"startup row changes: {db.last_init['startup_row_changes']}")
    return 0


# ---- migrate-v12: the authorized v12 migration (docs/PHASE_3_V12_AUTHORIZATION_GATE_IMPLEMENTATION_REPORT.md) ----
#
# A normal start (and `migrate`) refuses migration 12 on a database holding data
# (db.DatabaseMigrationAuthorizationRequired). This command is the only way it is applied, in this order: the
# --authorize-v12 flag (before any filesystem access), the instance lock (refused while the app runs), this build
# registers v12, history exactly v1..v11 of this code, target integrity, a fresh backup verified independently, the
# DD-6 / R2-4 preflight on the migration target itself, db.migrate() with a V12MigrationAuthorization (one
# transaction; the preflight re-checked inside it), then verification of the result and an audit record (stdout,
# a JSON file beside the database, a log line). No broker module is imported, no .env is read, no startup invariant
# runs. Exit codes: 0 applied / already applied, 1 refused or rolled back, 2 usage, 3 lock held, 4 migrated but the
# post-migration verification failed (restore the verified backup).

logger = logging.getLogger(__name__)

V12_COMMAND = "migrate-v12"
V12_AUTHORIZE_FLAG = "--authorize-v12"
V12_ISSUER = "db_maintenance migrate-v12 --authorize-v12"
EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_LOCKED, EXIT_VERIFICATION_FAILED = 0, 1, 2, 3, 4


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _history_pairs(conn) -> list[tuple[int, str]]:
    return [(h["version"], h["migration_name"]) for h in db.migration_history(conn)]


def _expected_history(upto: int) -> list[tuple[int, str]]:
    return [(version, name) for version, name, _ in db.MIGRATIONS if version <= upto]


def v12_registered() -> bool:
    """This build lists migration 12, exactly as v12's name, as its newest migration."""
    return (len(db.MIGRATIONS) == 12 and tuple(db.MIGRATIONS[-1][:2]) == (12, db._V12_NAME)
            and db._AUTHORIZATION_GATED_MIGRATIONS.get(12) == db._V12_NAME)


def _integrity(conn) -> list[str]:
    return [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]


def _fk_violations(conn) -> int:
    return len(conn.execute("PRAGMA foreign_key_check").fetchall())


def _row_counts(conn) -> dict:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_stat%' ORDER BY name")]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {db._quoted_identifier(t)}").fetchone()[0] for t in tables}


def _new_stamp() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S%f")


def _verify_v12_backup(backup: Path, target_counts: dict, target_fk: int, expected_version: int = 11,
                       target_digest: str | None = None) -> dict:
    """Independent of backup_database's own check: reopens the copy read-only (immutable: an offline file, no
    sidecar) and compares it with the target it was taken from (history exactly v1..expected_version; with
    `target_digest`, the logical content digest too: every row of every table, not only the counts)."""
    facts = {"path": str(backup), "size": backup.stat().st_size, "sha256": _file_sha256(backup), "problems": []}
    conn = _connect_preflight(backup)
    try:
        conn.execute("PRAGMA query_only = ON")
        integrity = _integrity(conn)
        history = _history_pairs(conn)
        fk = _fk_violations(conn)
        counts = _row_counts(conn)
        digest = backup_verification.logical_digest(conn) if target_digest is not None else None
    finally:
        conn.close()
    if target_digest is not None:
        facts["logical_digest"] = digest
        if digest != target_digest:
            facts["problems"].append("the backup's logical content digest differs from the target's")
    facts.update({"integrity_check": integrity[:10], "foreign_key_violations": fk})
    if integrity != ["ok"]:
        facts["problems"].append(f"integrity_check {integrity[:10]}")
    if history != _expected_history(expected_version):
        facts["problems"].append(f"history ends at {history[-1] if history else None}, not v{expected_version}")
    if fk != target_fk:
        facts["problems"].append(f"{fk} foreign key violation(s), the target has {target_fk}")
    if counts != target_counts:
        differ = sorted(t for t in set(counts) | set(target_counts) if counts.get(t) != target_counts.get(t))
        facts["problems"].append(f"row counts differ from the target in {differ}")
    return facts


def _new_files(before: set, pattern_parent: Path, pattern: str) -> list[dict]:
    return [{"path": str(p), "sha256": _file_sha256(p)} for p in sorted(pattern_parent.glob(pattern))
            if p not in before]


_AUDIT_KEYS = {
    "v12_migration": ("result", "database", "detail", "history_before", "history_after", "disk", "backup",
                      "migrate_backups", "preflight", "verification", "error", "audit_file", "exit_code"),
    "v12_rollback": ("result", "database", "detail", "history_before", "history_after", "held_data", "disk",
                     "backup", "rollback_backups", "verification", "error", "audit_file", "exit_code"),
    "restore": ("result", "database", "detail", "backup", "history_before", "history_after", "disk", "moved_aside",
                "restored", "verification", "recovery", "error", "audit_file", "exit_code"),
}


def _write_audit_file(audit_file: Path, text: str) -> None:
    with open(audit_file, "x", encoding="utf-8") as handle:  # never over an existing file
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _read_audit_file(audit_file: Path) -> dict:
    return json.loads(audit_file.read_text(encoding="utf-8"))


def _finish_audit(path: Path, audit: dict, kind: str) -> bool:
    """Every outcome of migrate-v12 / rollback-schema-v12 / restore-backup: a JSON file beside the database
    (<db>.<kind>_audit_<µs>.json), the result on stdout (success) or stderr, and one log line. The file is read back
    and parsed (V12 final readiness, Phase E): True only when the written record is readable and carries this result.
    False is printed as AUDIT_WRITE_FAILED and keeps any in-progress marker (the outcome is then not 'finished')."""
    audit["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    audit_file = path.with_name(f"{path.name}.{kind}_audit_{_new_stamp()}.json")
    audit["audit_file"] = str(audit_file)
    readable = False
    try:
        _write_audit_file(audit_file, json.dumps(audit, indent=2, default=str))
        back = _read_audit_file(audit_file)
        readable = back.get("result") == audit["result"] and back.get("exit_code") == audit["exit_code"]
        if not readable:
            audit["audit_file"] = f"NOT READABLE ({audit_file} does not read back as written)"
    except (OSError, ValueError) as e:
        audit["audit_file"] = f"NOT WRITTEN ({type(e).__name__}: {e})"
    out = sys.stdout if audit["exit_code"] == EXIT_OK and readable else sys.stderr
    if not readable:
        print(f"AUDIT_WRITE_FAILED: the audit record could not be written and read back ({audit['audit_file']}). "
              "The result below is the command's real outcome; copy this output into the evidence file by hand.",
              file=sys.stderr)
    for key in _AUDIT_KEYS[kind]:
        value = audit.get(key)
        if value in (None, [], {}):
            continue
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        print(f"{key}:".ljust(17) + text, file=out)
    logger.warning("event=%s.result result=%s exit_code=%s db=%s audit=%s",
                   kind, audit["result"], audit["exit_code"], path.name, audit["audit_file"])
    return readable


def _finish_v12(path: Path, audit: dict) -> bool:
    return _finish_audit(path, audit, "v12_migration")


# ---- in-progress markers, pre-lock checks, identity binding (V12 final readiness, Phases C/E/F) -------------------

# A migrate-v12 outcome after which the database needs a human (or a re-run) keeps its in-progress marker, so a
# normal start refuses until it is resolved. Every other outcome is unambiguous (nothing written, or v12 verified).
V12_MARKER_KEPT = frozenset({"INCOMPLETE", "MIGRATED_VERIFICATION_FAILED", "ALREADY_APPLIED_VERIFICATION_FAILED",
                             "STATE_UNKNOWN"})
RESTORE_MARKER_KEPT = frozenset({"INCOMPLETE", "RESTORE_FAILED_MANUAL_RECOVERY"})
OFFSITE_FLAG = "--offsite-record"


class DatabaseIdentityChanged(RuntimeError):
    error_code = "DATABASE_IDENTITY_CHANGED"


def _supersede(marker: Path, audit: dict, key: str) -> None:
    """An unfinished earlier run's marker is kept as evidence under a new name, never overwritten or deleted."""
    if marker.exists():
        kept = marker.with_name(f"{marker.name}.superseded_{_new_stamp()}")
        os.rename(marker, kept)
        try:
            content = json.loads(kept.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            content = f"unreadable ({type(e).__name__})"
        audit.setdefault(key, []).append({"superseded_as": str(kept), "content": content})


def _begin_marker(path: Path, suffix: str, audit: dict, content: dict) -> Path:
    _supersede(installation_state.marker_file(path, suffix), audit, "previous_markers")
    marker = installation_state.write_marker(path, suffix, content)
    audit["in_progress_marker"] = str(marker)
    return marker


def _end_marker(marker: Path | None, audit: dict, audit_ok: bool, kept_results: frozenset) -> None:
    if marker is None:
        return
    if audit_ok and audit.get("result") not in kept_results:
        installation_state.clear_marker(marker)
        return
    try:
        installation_state.update_marker(marker, {"result": audit.get("result"), "audit_file": audit.get("audit_file"),
                                                  "detail": audit.get("detail")})
    except (OSError, ValueError):
        pass
    print(f"in-progress marker KEPT: {marker} (result {audit.get('result')}). A normal start refuses until this is "
          "resolved: follow the runbook's recovery section for this result, then re-run the command.", file=sys.stderr)


def _identity_problem(path: Path, pre: dict) -> str | None:
    """The target is still the file the pre-lock checks classified and identified: still on fixed local storage,
    same volume serial and file index. None when it is."""
    verdict = storage_classification.classify_path(path)
    if not verdict.acceptable_for_database:
        return f"the database storage is now {verdict.category} ({verdict.reason})"
    now = installation_state.file_identity(path)
    if not installation_state.same_file(now, pre.get("identity")):
        return (f"the database file changed identity since the pre-lock checks (was "
                f"{(pre.get('identity') or {}).get('st_ino')}, now {(now or {}).get('st_ino')}): it was replaced")
    return None


def _pre_lock_checks(path: Path, *, ignore_markers: tuple = (), refuse_states=True) -> tuple[dict, str | None]:
    """Before the instance lock and any write: installation state and a pending rollback journal. Returns
    (facts for the audit, refusal or None). The storage check already ran (main). refuse_states: True refuses every
    state that is not permitted, a set refuses those states only, False refuses none."""
    assessment = installation_state.assess(path, ignore_markers=ignore_markers)
    facts = {"assessment": assessment.as_record(), "identity": installation_state.file_identity(path)}
    if refuse_states is True:
        refused = not assessment.permitted
    else:
        refused = bool(refuse_states) and assessment.state in refuse_states
    if refused:
        return facts, str(assessment.refusal())
    journal = Path(f"{path}-journal")
    if journal.exists() and journal.stat().st_size > 0:
        return facts, (f"HOT_JOURNAL_PRESENT: {journal.name} is not empty: a crashed write left a rollback journal "
                       "that SQLite would replay on the next open. Nothing was opened or changed. Do not delete it; "
                       "follow the runbook (recovery, 'hot journal').")
    return facts, None


# Free space each command needs beside the database, in multiples of its current size (main file plus -wal):
# migrate-v12 and rollback-schema-v12 write two full backups (the command's own, verified independently, and the
# one db.migrate() / db.rollback_schema_v12() takes) and their transaction can grow the WAL by up to about one
# database again; restore-backup writes one staging copy of the backup. Plus a fixed headroom.
DISK_HEADROOM_BYTES = 64 * 1024 * 1024
DISK_COPIES = {"v12_migration": 3, "v12_rollback": 3, "restore": 2}


def _sqlite_size(path: Path) -> int:
    size = path.stat().st_size if path.exists() else 0
    wal = Path(f"{path}-wal")
    return size + (wal.stat().st_size if wal.exists() else 0)


def _disk_space(directory: Path, size: int, copies: int) -> dict:
    """The free-space preflight, before any backup or copy is written. `ok` False refuses the command."""
    needed = copies * size + DISK_HEADROOM_BYTES
    free = shutil.disk_usage(directory).free
    return {"directory": str(directory), "free_bytes": free, "needed_bytes": needed, "ok": free >= needed}


def _post_failure_history(path: Path):
    """The migration history as committed, read on a fresh read-only connection after a failure (it reads the
    WAL, unlike an immutable open). None when it cannot be read at all."""
    try:
        conn = _connect_readonly(path)
        try:
            conn.execute("PRAGMA query_only = ON")
            return _history_pairs(conn)
        finally:
            conn.close()
    except Exception:
        return None


def _set(audit: dict, result: str, exit_code: int, detail: str | None = None) -> int:
    audit.update({"result": result, "exit_code": exit_code})
    if detail:
        audit["detail"] = detail
    return exit_code


def _verify_after_v12(conn, counts_before: dict, fk_before: int, *, just_migrated: bool = True) -> list[str]:
    """The post-migration checks. just_migrated=False (a repeat run on a v12 database) keeps the schema, integrity
    and foreign-key checks but not those that only hold right after the migration (empty journal)."""
    problems = []
    history = _history_pairs(conn)
    if history != _expected_history(12):
        problems.append(f"history ends at {history[-1] if history else None}, not v12")
    if not db.schema_status(conn)["up_to_date"]:
        problems.append("schema_status is not up to date")
    problems += db.v12_schema_problems(conn, empty_journal=just_migrated)
    integrity = _integrity(conn)
    if integrity != ["ok"]:
        problems.append(f"integrity_check {integrity[:10]}")
    fk_after = _fk_violations(conn)
    if fk_after > fk_before:
        problems.append(f"foreign key violations rose from {fk_before} to {fk_after}")
    if not just_migrated:
        return problems
    counts_after = _row_counts(conn)
    for table, count in counts_before.items():
        expected = count + 1 if table == "schema_version" else count
        if counts_after.get(table) != expected:
            problems.append(f"{table} has {counts_after.get(table)} row(s), expected {expected}")
    for table in sorted(set(counts_after) - set(counts_before)):
        if counts_after[table]:
            problems.append(f"new table {table} holds {counts_after[table]} row(s)")
    return problems


def _migrate_v12(conn, path: Path, audit: dict, offsite_record: Path | None = None, pre: dict | None = None) -> int:
    pre = pre if pre is not None else {"identity": installation_state.file_identity(path)}
    if not v12_registered():
        return _set(audit, "V12_NOT_REGISTERED", EXIT_REFUSED,
                    "this build does not register migration 12; nothing to apply and nothing was changed")
    try:
        history = _history_pairs(conn)
        db._check_history(db.migration_history(conn))
    except db.DatabaseSchemaUnsupported as e:
        return _set(audit, "HISTORY_REFUSED", EXIT_REFUSED, str(e))
    audit["history_before"] = history[-1] if history else None
    if history == _expected_history(12):
        problems = _verify_after_v12(conn, {}, _fk_violations(conn), just_migrated=False)
        audit["verification"] = {"problems": problems}
        if problems:
            return _set(audit, "ALREADY_APPLIED_VERIFICATION_FAILED", EXIT_VERIFICATION_FAILED,
                        "the database is at v12 but does not verify; inspect it or restore a verified backup")
        audit["history_after"] = audit["history_before"]
        return _set(audit, "ALREADY_APPLIED", EXIT_OK, "the database is already at v12; nothing was changed")
    if history != _expected_history(11):
        return _set(audit, "HISTORY_REFUSED", EXIT_REFUSED,
                    f"the history must be exactly v1..v11 of this code; it ends at {audit['history_before']}. "
                    "A database below v11 is brought to v11 with the Phase 2 build first. Nothing was changed.")

    integrity = _integrity(conn)
    if integrity != ["ok"]:
        return _set(audit, "TARGET_INTEGRITY_FAILED", EXIT_REFUSED,
                    f"the target fails PRAGMA integrity_check ({integrity[:10]}); nothing was changed")
    fk_before, counts_before = _fk_violations(conn), _row_counts(conn)
    target_digest = backup_verification.logical_digest(conn)
    audit["target_before"] = {"integrity_check": "ok", "foreign_key_violations": fk_before, "row_counts": counts_before,
                              "logical_digest": target_digest}

    audit["disk"] = _disk_space(path.parent, _sqlite_size(path), DISK_COPIES["v12_migration"])
    if not audit["disk"]["ok"]:
        return _set(audit, "DISK_SPACE_INSUFFICIENT", EXIT_REFUSED,
                    f"{audit['disk']['free_bytes']} bytes free beside the database, {audit['disk']['needed_bytes']} "
                    "needed for the two backups and the migration; nothing was written")
    _checkpoint("v12.before_local_backup")
    try:
        backup = db.backup_database(conn, path.with_name(f"{path.name}.pre_v12_authorized_{_new_stamp()}_bak"))
    except Exception as e:  # an I/O error, or backup_database's own integrity refusal
        audit["error"] = f"{type(e).__name__}: {e}"
        return _set(audit, "BACKUP_FAILED", EXIT_REFUSED, "the pre-migration backup failed; nothing was migrated")
    try:
        audit["backup"] = _verify_v12_backup(backup, counts_before, fk_before, target_digest=target_digest)
    except Exception as e:  # unreadable, not a database, ...
        audit["backup"] = {"path": str(backup)}
        audit["error"] = f"{type(e).__name__}: {e}"
        return _set(audit, "BACKUP_VERIFICATION_FAILED", EXIT_REFUSED,
                    "the pre-migration backup could not be verified; nothing was migrated")
    if audit["backup"]["problems"]:
        return _set(audit, "BACKUP_VERIFICATION_FAILED", EXIT_REFUSED,
                    "the pre-migration backup does not match the target; nothing was migrated")

    # The off-machine backup (V12 final readiness, Phase D/E): never a historical result. The recorded backup is
    # re-verified NOW against the target, and must be a copy of exactly the content about to be migrated.
    _checkpoint("v12.before_offsite_check")
    if offsite_record is None:
        return _set(audit, "OFFSITE_BACKUP_REFUSED", EXIT_REFUSED,
                    f"no off-machine backup verification record ({OFFSITE_FLAG}); nothing was migrated")
    offsite = backup_verification.check_offsite_record(offsite_record, path)
    audit["offsite"] = {k: offsite.get(k) for k in ("record_file", "record", "live", "problems", "ok")}
    if offsite.get("ok") and offsite.get("target_logical_digest") != target_digest:
        offsite["ok"] = False
        offsite.setdefault("problems", []).append("the target's content changed between its own digest and the "
                                                  "off-machine re-verification")
        audit["offsite"]["problems"] = offsite["problems"]
    if not offsite.get("ok"):
        return _set(audit, "OFFSITE_BACKUP_REFUSED", EXIT_REFUSED,
                    "the off-machine backup is not verified for this exact database now: "
                    f"{'; '.join(offsite.get('problems', []))}. Take a new backup to the external disk / share with "
                    "`backup --dest DIR`, then run migrate-v12 again with its new record. Nothing was migrated.")

    conn.execute("PRAGMA query_only = ON")  # the preflight on the migration target itself, read-only
    try:
        facts = preflight_v12_counts(conn)
    finally:
        conn.execute("PRAGMA query_only = OFF")
    audit["preflight"] = {"schema_is_v11": facts["schema_is_v11"], "duplicate_gtt_ids": facts["duplicate_gtt_ids"],
                          "open_null_mode_buys": facts["open_null_mode_buys"],
                          "diagnostic_null_mode_buys": facts["diagnostic_null_mode_buys"]}
    if not facts["schema_is_v11"] or facts["duplicate_gtt_ids"] or facts["open_null_mode_buys"]:
        return _set(audit, "PREFLIGHT_FAILED", EXIT_REFUSED,
                    "the DD-6 / R2-4 preflight failed on the target; resolve the listed tickets in Ticket History. "
                    "Nothing was migrated.")

    # Time of check / time of use: the file migrated is the file every check above examined.
    identity_problem = _identity_problem(path, pre)
    if identity_problem:
        return _set(audit, "DATABASE_IDENTITY_CHANGED", EXIT_REFUSED,
                    f"{identity_problem}. Nothing was migrated; stop and inspect the database (runbook: recovery).")

    def guard(c) -> None:
        """Inside v12's own BEGIN IMMEDIATE transaction, under the write lock, before any DDL (db.migrate)."""
        _checkpoint("v12.in_transaction")
        problem = _identity_problem(path, pre)
        if problem:
            raise DatabaseIdentityChanged(f"DATABASE_IDENTITY_CHANGED: {problem}")
        if backup_verification.logical_digest(c) != target_digest:
            raise DatabaseIdentityChanged("DATABASE_IDENTITY_CHANGED: the database content changed after the "
                                          "off-machine backup was verified")

    migrate_backups_before = set(path.parent.glob(f"{path.name}.pre_schema_v12_bak*"))
    try:
        applied = db.migrate(conn, v12_authorization=db.V12MigrationAuthorization(issued_by=V12_ISSUER),
                             v12_transaction_guard=guard)
    except BaseException as e:
        audit["error"] = f"{type(e).__name__}: {e}"
        audit["migrate_backups"] = _new_files(migrate_backups_before, path.parent, f"{path.name}.pre_schema_v12_bak*")
        exit_code = _classify_migrate_failure(conn, path, audit, e, counts_before, fk_before)
        if not isinstance(e, Exception):
            raise
        return exit_code
    audit["applied_migrations"] = applied
    audit["migrate_backups"] = _new_files(migrate_backups_before, path.parent, f"{path.name}.pre_schema_v12_bak*")
    after = _history_pairs(conn)
    audit["history_after"] = after[-1] if after else None
    problems = _verify_after_v12(conn, counts_before, fk_before)
    if applied != [12]:
        problems.insert(0, f"migrate() applied {applied}, expected [12]")
    audit["verification"] = {"problems": problems, "integrity_check": _integrity(conn)[:10],
                             "foreign_key_violations": _fk_violations(conn)}
    if problems:
        return _set(audit, "MIGRATED_VERIFICATION_FAILED", EXIT_VERIFICATION_FAILED,
                    f"migration 12 committed but its verification failed; stop and restore the verified backup "
                    f"{audit['backup']['path']}")
    try:
        installation_state.record(path, by="migrate-v12", conn=conn)
        audit["installation_record"] = "re-bound"
    except OSError as e:  # the database is fine; the next start re-binds a legacy record
        audit["installation_record"] = f"NOT WRITTEN ({type(e).__name__}: {e})"
    return _set(audit, "V12_APPLIED", EXIT_OK, "migration 12 applied and verified")


def _classify_migrate_failure(conn, path: Path, audit: dict, error: BaseException, counts_before: dict,
                              fk_before: int) -> int:
    """db.migrate() raised: what actually happened is read back from the committed history, never assumed. Only a
    database that still has exactly v1..v11 and none of the v12 objects is FAILED_ROLLED_BACK (or BACKUP_FAILED,
    when migrate()'s own backup failed before its transaction began). A committed v12 whose call still raised
    (after COMMIT: PRAGMA optimize, an interrupt) is MIGRATED_VERIFICATION_FAILED; anything else, STATE_UNKNOWN."""
    after = _post_failure_history(path)
    audit["history_after"] = (after[-1] if after else None) if after is not None else "unreadable"
    if after == _expected_history(12):
        try:
            problems = _verify_after_v12(conn, counts_before, fk_before)
        except Exception as e:  # noqa: BLE001 — recorded, the result stands
            problems = [f"verification could not run: {type(e).__name__}: {e}"]
        audit["verification"] = {"problems": problems or ["migrate() raised after migration 12 committed"]}
        return _set(audit, "MIGRATED_VERIFICATION_FAILED", EXIT_VERIFICATION_FAILED,
                    f"migration 12 COMMITTED but the migration call then failed; the database is at v12. Stop, "
                    f"inspect it, and restore the verified backup {audit['backup']['path']} with restore-backup if "
                    "it does not verify")
    if after == _expected_history(11):
        try:
            objects = db.v12_objects_state(conn)
        except Exception as e:  # noqa: BLE001
            objects = f"unreadable ({type(e).__name__})"
        if objects == "absent":
            if isinstance(error, db.MigrationBackupFailed):
                return _set(audit, "BACKUP_FAILED", EXIT_REFUSED,
                            "migrate()'s own pre-migration backup failed before its transaction began; nothing was "
                            "migrated and the database stays at v11")
            return _set(audit, "FAILED_ROLLED_BACK", EXIT_REFUSED,
                        "migration 12 failed and was rolled back; the database stays at v11 (verified: history v11, "
                        "no v12 object)")
        audit["verification"] = {"problems": [f"history is v11 but the v12 objects are {objects}"]}
    return _set(audit, "STATE_UNKNOWN", EXIT_VERIFICATION_FAILED,
                f"migration 12 failed and the database state could not be confirmed (history {audit['history_after']}); "
                f"stop, inspect it, and restore the verified backup {audit['backup']['path']} with restore-backup")


def migrate_v12(path: Path, offsite_record: Path | None = None, pre: dict | None = None) -> int:
    """The authorized v12 migration; the caller holds the instance lock. Every outcome is audited. An in-progress
    marker is written before the first write (it also proves the audit location is writable) and removed only for an
    unambiguous outcome whose audit record read back."""
    audit = {"command": f"{V12_COMMAND} {V12_AUTHORIZE_FLAG} {OFFSITE_FLAG} {offsite_record}", "database": str(path),
             "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
             "result": "INCOMPLETE", "exit_code": EXIT_REFUSED, "pre_lock": pre}
    marker = None
    try:
        marker = _begin_marker(path, installation_state.MIGRATION_MARKER_SUFFIX, audit,
                               {"command": V12_COMMAND, "database": str(path), "started_at": audit["started_at"],
                                "meaning": "migrate-v12 started and has not reached an unambiguous, audited end"})
    except OSError as e:
        audit["error"] = f"{type(e).__name__}: {e}"
        _set(audit, "AUDIT_LOCATION_UNWRITABLE", EXIT_REFUSED,
             "the in-progress marker could not be written beside the database; nothing was opened or changed")
        _finish_v12(path, audit)
        return EXIT_REFUSED
    audit_ok = False
    try:
        conn = _connect(path)
        try:
            return _migrate_v12(conn, path, audit, offsite_record, pre)
        finally:
            conn.close()
    finally:
        audit_ok = _finish_v12(path, audit)
        _end_marker(marker, audit, audit_ok, V12_MARKER_KEPT)


# ---- rollback-schema-v12: the written down-migration of v12 as a command -------------------------------------
#
# python -m backend.db_maintenance rollback-schema-v12 --confirm [--db PATH]. --confirm is checked before any
# filesystem access; the instance lock is taken by main() (WRITING_COMMANDS): refused while the app runs. Then, each
# refusal audited and writing nothing: this build registers v12; history exactly v1..v12 of this code; the v12
# objects exactly v12's; target integrity; no protection data held (journal rows, resolution rows, orphans
# included, protection states on tickets, protection_* events; db.rollback_schema_v12 re-checks it under the write
# lock);
# free disk space; a fresh backup verified independently (history v12, row counts, FK count). Then
# db.rollback_schema_v12 (its own backup, one transaction), the result read back from the committed history, and
# the post-rollback verification: history exactly v1..v11, no v12 object, integrity ok, FK violations not
# increased, every v11 table's row count unchanged. Exit codes as migrate-v12.

ROLLBACK_V12_COMMAND = "rollback-schema-v12"
_V12_TABLES = db.V12_TABLE_NAMES


def _verify_after_rollback_v12(conn, counts_before: dict, fk_before: int) -> list[str]:
    problems = []
    history = _history_pairs(conn)
    if history != _expected_history(11):
        problems.append(f"history ends at {history[-1] if history else None}, not v11")
    try:
        state = db.v12_objects_state(conn)
    except Exception as e:  # noqa: BLE001
        state = f"unreadable ({type(e).__name__}: {e})"
    if state != "absent":
        problems.append(f"the v12 objects are {state}, not absent")
    integrity = _integrity(conn)
    if integrity != ["ok"]:
        problems.append(f"integrity_check {integrity[:10]}")
    fk_after = _fk_violations(conn)
    if fk_after > fk_before:
        problems.append(f"foreign key violations rose from {fk_before} to {fk_after}")
    counts_after = _row_counts(conn)
    for table, count in counts_before.items():
        if table in _V12_TABLES:
            if table in counts_after:
                problems.append(f"v12 table {table} still exists")
            continue
        expected = count - 1 if table == "schema_version" else count
        if counts_after.get(table) != expected:
            problems.append(f"{table} has {counts_after.get(table)} row(s), expected {expected}")
    for table in sorted(set(counts_after) - set(counts_before)):
        problems.append(f"unexpected new table {table}")
    return problems


def _classify_rollback_failure(conn, path: Path, audit: dict, error: BaseException, counts_before: dict,
                               fk_before: int) -> int:
    after = _post_failure_history(path)
    audit["history_after"] = (after[-1] if after else None) if after is not None else "unreadable"
    if after == _expected_history(12):
        try:
            intact = db.v12_schema_problems(conn, empty_journal=False) == []
        except Exception:  # noqa: BLE001
            intact = False
        if intact:
            if isinstance(error, db.MigrationBackupFailed):
                return _set(audit, "BACKUP_FAILED", EXIT_REFUSED,
                            "rollback_schema_v12's own pre-rollback backup failed before its transaction began; "
                            "nothing was rolled back and the database stays at v12")
            if isinstance(error, db.SchemaRollbackRefused):
                return _set(audit, "ROLLBACK_REFUSED", EXIT_REFUSED,
                            "the rollback refused under the write lock (re-checked there); nothing was changed and "
                            "the database stays at v12")
            return _set(audit, "FAILED_ROLLED_BACK", EXIT_REFUSED,
                        "the rollback failed and was rolled back; the database stays at v12 (verified: history v12, "
                        "the v12 objects intact)")
    if after == _expected_history(11):
        problems = _verify_after_rollback_v12(conn, counts_before, fk_before)
        audit["verification"] = {"problems": problems or ["rollback_schema_v12 raised after its COMMIT"]}
        return _set(audit, "ROLLBACK_VERIFICATION_FAILED", EXIT_VERIFICATION_FAILED,
                    f"the rollback COMMITTED (the database is at v11) but the call then failed; inspect it, and "
                    f"restore the verified backup {audit['backup']['path']} with restore-backup if it does not verify")
    return _set(audit, "STATE_UNKNOWN", EXIT_VERIFICATION_FAILED,
                f"the rollback failed and the database state could not be confirmed (history {audit['history_after']}); "
                f"stop, inspect it, and restore the verified backup {audit['backup']['path']} with restore-backup")


def _rollback_v12(conn, path: Path, audit: dict) -> int:
    if not v12_registered():
        return _set(audit, "V12_NOT_REGISTERED", EXIT_REFUSED,
                    "this build does not register migration 12; there is no v12 to roll back and nothing was changed")
    try:
        history = _history_pairs(conn)
        db._check_history(db.migration_history(conn))
    except db.DatabaseSchemaUnsupported as e:
        return _set(audit, "HISTORY_REFUSED", EXIT_REFUSED, str(e))
    audit["history_before"] = history[-1] if history else None
    if history != _expected_history(12):
        return _set(audit, "HISTORY_REFUSED", EXIT_REFUSED,
                    f"the history must be exactly v1..v12 of this code; it ends at {audit['history_before']}. "
                    "Nothing was changed.")
    schema_problems = db.v12_schema_problems(conn, empty_journal=False)
    if schema_problems:
        audit["verification"] = {"problems": schema_problems}
        return _set(audit, "V12_SCHEMA_REFUSED", EXIT_REFUSED,
                    "the v12 objects are not exactly v12's; nothing was changed (restore a verified backup instead)")
    integrity = _integrity(conn)
    if integrity != ["ok"]:
        return _set(audit, "TARGET_INTEGRITY_FAILED", EXIT_REFUSED,
                    f"the target fails PRAGMA integrity_check ({integrity[:10]}); nothing was changed")
    held = db._protection_held_data(conn)
    if any(held.values()):
        audit["held_data"] = held
        return _set(audit, "REFUSED_HELD_DATA", EXIT_REFUSED,
                    "the protection journal holds data and a rollback would delete it; nothing was changed. The way "
                    "back from here is restoring a verified backup (restore-backup)")
    fk_before, counts_before = _fk_violations(conn), _row_counts(conn)
    target_digest = backup_verification.logical_digest(conn)
    audit["target_before"] = {"integrity_check": "ok", "foreign_key_violations": fk_before, "row_counts": counts_before,
                              "logical_digest": target_digest}
    audit["disk"] = _disk_space(path.parent, _sqlite_size(path), DISK_COPIES["v12_rollback"])
    if not audit["disk"]["ok"]:
        return _set(audit, "DISK_SPACE_INSUFFICIENT", EXIT_REFUSED,
                    f"{audit['disk']['free_bytes']} bytes free beside the database, {audit['disk']['needed_bytes']} "
                    "needed for the two backups and the rollback; nothing was written")
    try:
        backup = db.backup_database(conn, path.with_name(f"{path.name}.pre_rollback_v12_authorized_{_new_stamp()}_bak"))
    except Exception as e:
        audit["error"] = f"{type(e).__name__}: {e}"
        return _set(audit, "BACKUP_FAILED", EXIT_REFUSED, "the pre-rollback backup failed; nothing was rolled back")
    try:
        audit["backup"] = _verify_v12_backup(backup, counts_before, fk_before, expected_version=12,
                                             target_digest=target_digest)
    except Exception as e:
        audit["backup"] = {"path": str(backup)}
        audit["error"] = f"{type(e).__name__}: {e}"
        return _set(audit, "BACKUP_VERIFICATION_FAILED", EXIT_REFUSED,
                    "the pre-rollback backup could not be verified; nothing was rolled back")
    if audit["backup"]["problems"]:
        return _set(audit, "BACKUP_VERIFICATION_FAILED", EXIT_REFUSED,
                    "the pre-rollback backup does not match the target; nothing was rolled back")

    pattern = f"{path.name}.pre_rollback_v12_bak*"
    rollback_backups_before = set(path.parent.glob(pattern))
    try:
        db.rollback_schema_v12(conn)
    except BaseException as e:
        audit["error"] = f"{type(e).__name__}: {e}"
        audit["rollback_backups"] = _new_files(rollback_backups_before, path.parent, pattern)
        exit_code = _classify_rollback_failure(conn, path, audit, e, counts_before, fk_before)
        if not isinstance(e, Exception):
            raise
        return exit_code
    audit["rollback_backups"] = _new_files(rollback_backups_before, path.parent, pattern)
    after = _history_pairs(conn)
    audit["history_after"] = after[-1] if after else None
    problems = _verify_after_rollback_v12(conn, counts_before, fk_before)
    audit["verification"] = {"problems": problems, "integrity_check": _integrity(conn)[:10],
                             "foreign_key_violations": _fk_violations(conn)}
    if problems:
        return _set(audit, "ROLLBACK_VERIFICATION_FAILED", EXIT_VERIFICATION_FAILED,
                    f"the rollback committed but its verification failed; stop and restore the verified backup "
                    f"{audit['backup']['path']} with restore-backup")
    return _set(audit, "ROLLED_BACK", EXIT_OK,
                "migration 12 rolled back and verified: schema v11, every v11 table's rows kept; a Phase 2 (v11) "
                "build accepts the database again")


def rollback_v12(path: Path) -> int:
    """rollback-schema-v12 --confirm; the caller (main) holds the instance lock. Every outcome is audited."""
    audit = {"command": f"{ROLLBACK_V12_COMMAND} --confirm", "database": str(path),
             "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
             "result": "INCOMPLETE", "exit_code": EXIT_REFUSED}
    conn = _connect(path)
    try:
        return _rollback_v12(conn, path, audit)
    finally:
        conn.close()
        _finish_audit(path, audit, "v12_rollback")


# ---- restore-backup: the restore procedure, safe against stale WAL/SHM sidecars ------------------------------
#
# python -m backend.db_maintenance restore-backup --backup FILE --confirm [--db PATH]
#
# A SQLite database in WAL mode is a SET of files: <db>, <db>-wal (committed transactions not yet copied into
# <db>) and <db>-shm (an index of the WAL). SQLite pairs them by name only. Copying a backup over <db> while the
# old <db>-wal is still there would let SQLite replay the OLD database's WAL onto the restored image on the next
# open: a corrupt or silently wrong database. So the restore never writes over the target in place:
#   1. --backup and --confirm are checked before any filesystem access; main() takes the instance lock (app stopped)
#   2. the backup is verified, opened immutable (no lock, no sidecar): it must have no -wal/-journal of its own,
#      pass integrity_check and carry a history this build accepts; its FK count, row counts and SHA-256 are kept
#   3. free disk space for a staging copy
#   4. the backup is byte-copied to <db>.restore_staging_<µs> and that copy re-verified (SHA-256 and content)
#   5. the original SET is moved aside together, renamed to <db>.pre_restore_<µs>_orig (+ the same -wal, -shm,
#      -journal suffixes): its WAL stays paired with its own main file, so the original remains complete and
#      openable, and no stale sidecar keeps the target's name
#   6. the staging copy is moved into place (os.replace); no <db>-wal/-shm/-journal may then exist
#   7. post-restore verification: SHA-256 equal to the backup, integrity ok, history, FK count, row counts equal
#   8. on any failure after step 5 the restored file is set aside as <db>.failed_restore_<µs> and the original set
#      is renamed back (RESTORE_VERIFICATION_FAILED); if even that fails: RESTORE_FAILED_MANUAL_RECOVERY, with every
#      file name in the audit record (docs/PHASE_3_V12_REGISTRATION_RUNBOOK_DRAFT.md, recovery)

RESTORE_COMMAND = "restore-backup"
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def _with_suffix(path: Path, suffix: str) -> Path:
    return Path(f"{path}{suffix}")


def _inspect_offline(path: Path) -> dict:
    """An offline file (a backup, a staging copy, the restored file before any open), opened immutable and
    read-only: no lock, no sidecar is created and no WAL is read (so the caller first proves none exists)."""
    conn = _connect_preflight(path)
    try:
        conn.execute("PRAGMA query_only = ON")
        return {"integrity_check": _integrity(conn)[:10], "history": _history_pairs(conn),
                "foreign_key_violations": _fk_violations(conn), "row_counts": _row_counts(conn)}
    finally:
        conn.close()


def _history_problem(history: list) -> str | None:
    if not history:
        return "the file has no schema history"
    try:
        db._check_history([{"version": v, "migration_name": n} for v, n in history])
    except db.DatabaseSchemaUnsupported as e:
        return str(e)
    return None


def _rename_no_overwrite(source, destination) -> None:
    """A rename that never replaces an existing file (V12 final readiness, Phase F): on Windows os.rename already
    refuses an existing destination; the explicit check covers POSIX (where rename would overwrite). The instance
    lock is held, so nothing else creates the destination in between."""
    if os.path.lexists(destination):
        raise FileExistsError(17, "refusing to rename over an existing file", str(destination))
    os.rename(source, destination)


def _move_set(base: Path, target_base: Path, moved: list) -> None:
    """Renames `base` and its existing sidecars to `target_base` (+ the same suffixes), main file first; appends
    (source, destination) to `moved` as each one succeeds. Never renames over an existing file: a stale file under a
    destination name would otherwise pair a foreign -wal with the moved-aside original."""
    for suffix in ("",) + SQLITE_SIDECARS:
        source = _with_suffix(base, suffix)
        if source.exists():
            destination = _with_suffix(target_base, suffix)
            _rename_no_overwrite(source, destination)
            moved.append((str(source), str(destination)))
            _checkpoint(f"restore.moved_aside{suffix or '_main'}")


def _move_back(moved: list) -> list[str]:
    failures = []
    for source, destination in reversed(moved):
        try:
            _rename_no_overwrite(destination, source)
        except OSError as e:
            failures.append(f"{destination} -> {source}: {e}")
    return failures


def _complete_interrupted_move(path: Path, audit: dict) -> None:
    """A restore that crashed between moving the original's main file and its sidecars left <db>-wal/-shm/-journal
    under the database's name while the main file is under the moved-aside name its marker records. Before anything
    else moves, those sidecars rejoin their own main file (never another set). Only when the marker names a moved-aside
    main file that exists and no main file is at the database's name; never over an existing file."""
    marker = installation_state.marker_file(path, installation_state.RESTORE_MARKER_SUFFIX)
    if not marker.exists():
        return
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        audit["previous_marker_unreadable"] = f"{type(e).__name__}: {e}"
        return
    original = data.get("original_set_moved_aside_as")
    if not original or path.exists() or not Path(original).exists():
        return
    completed = []
    for suffix in SQLITE_SIDECARS:
        source, destination = _with_suffix(path, suffix), _with_suffix(Path(original), suffix)
        if source.exists() and not os.path.lexists(destination):
            _rename_no_overwrite(source, destination)
            completed.append((str(source), str(destination)))
    if completed:
        audit["completed_interrupted_move"] = completed


def _restore_names(path: Path, stamp: str) -> dict:
    staging = path.with_name(f"{path.name}.restore_staging_{stamp}")
    original = path.with_name(f"{path.name}.pre_restore_{stamp}_orig")
    failed = path.with_name(f"{path.name}.failed_restore_{stamp}")
    planned = [staging, _with_suffix(failed, ".staging"), failed] + [_with_suffix(original, s)
                                                                     for s in ("",) + SQLITE_SIDECARS]
    return {"staging": staging, "original": original, "failed": failed, "planned": planned}


def _restore(path: Path, backup: Path, audit: dict, state: dict | None = None) -> int:
    state = state if state is not None else {}
    stamp = _new_stamp()
    names = _restore_names(path, stamp)
    if not backup.is_file():
        return _set(audit, "RESTORE_REFUSED", EXIT_REFUSED, f"no backup file at {backup}; nothing was changed")
    stray = [str(_with_suffix(backup, s)) for s in ("-wal", "-journal") if _with_suffix(backup, s).exists()
             and _with_suffix(backup, s).stat().st_size > 0]
    if stray:
        return _set(audit, "BACKUP_VERIFICATION_FAILED", EXIT_REFUSED,
                    f"the backup has non-empty sidecar(s) {stray}: it is not a closed, self-contained file (an open "
                    "or crashed database). Use a backup written by db_maintenance; nothing was changed")
    try:
        facts = _inspect_offline(backup)
    except Exception as e:
        audit["error"] = f"{type(e).__name__}: {e}"
        return _set(audit, "BACKUP_VERIFICATION_FAILED", EXIT_REFUSED,
                    "the backup could not be read as a SQLite database; nothing was changed")
    backup_sha = _file_sha256(backup)
    audit["backup"] = {"path": str(backup), "size": backup.stat().st_size, "sha256": backup_sha,
                       "integrity_check": facts["integrity_check"], "history": facts["history"][-1:] or None,
                       "foreign_key_violations": facts["foreign_key_violations"]}
    problem = (f"integrity_check {facts['integrity_check']}" if facts["integrity_check"] != ["ok"]
               else _history_problem(facts["history"]))
    if problem:
        return _set(audit, "BACKUP_VERIFICATION_FAILED", EXIT_REFUSED,
                    f"the backup does not verify ({problem}); nothing was changed")
    if path.exists():
        try:
            before = _post_failure_history(path)
        except Exception:  # noqa: BLE001
            before = None
        audit["history_before"] = (before[-1] if before else None) if before is not None else "unreadable"
    audit["disk"] = _disk_space(path.parent, backup.stat().st_size, DISK_COPIES["restore"])
    if not audit["disk"]["ok"]:
        return _set(audit, "DISK_SPACE_INSUFFICIENT", EXIT_REFUSED,
                    f"{audit['disk']['free_bytes']} bytes free beside the database, {audit['disk']['needed_bytes']} "
                    "needed for the staging copy; nothing was changed")

    staging = names["staging"]
    taken = [str(p) for p in names["planned"] if os.path.lexists(p)]
    if taken:
        return _set(audit, "RESTORE_NAME_CONFLICT", EXIT_REFUSED,
                    f"file(s) {taken} already exist under the names this restore would use; nothing was changed. "
                    "Move them out of the way after checking what they are, then run the restore again")
    try:
        _copy_backup(backup, staging)
        _checkpoint("restore.copied_to_staging")
        staged = _inspect_offline(staging) if _file_sha256(staging) == backup_sha else None
    except Exception as e:
        staged = None
        audit["error"] = f"{type(e).__name__}: {e}"
    if staged != facts:
        try:
            if staging.exists():
                staging.unlink()
        except OSError as e:
            audit["staging_left"] = f"{staging} ({type(e).__name__}: {e})"
        return _set(audit, "RESTORE_REFUSED", EXIT_REFUSED,
                    "the staging copy of the backup did not match it (an incomplete or failed copy); the target was "
                    "not touched")

    original = names["original"]
    try:
        _complete_interrupted_move(path, audit)
    except OSError as e:
        try:
            staging.unlink()
        except OSError:
            pass
        audit["error"] = f"{type(e).__name__}: {e}"
        return _set(audit, "RESTORE_REFUSED", EXIT_REFUSED,
                    "an earlier interrupted restore left the original's sidecars under the database's name and they "
                    "could not be re-joined to it; nothing else was changed. Follow the runbook (recovery, "
                    "'interrupted restore') by hand")
    # The restore marker: from here until the end, the original set may be under its moved-aside name. A crash
    # leaves the marker, so a normal start refuses and the operator knows exactly where the original is.
    try:
        state["marker"] = _begin_marker(path, installation_state.RESTORE_MARKER_SUFFIX, audit, {
            "command": RESTORE_COMMAND, "database": str(path), "backup": str(backup), "backup_sha256": backup_sha,
            "original_set_moved_aside_as": str(original), "staging": str(staging), "failed_restore": str(names["failed"]),
            "meaning": "restore-backup started; if this file remains, the original database set may be under "
                       "original_set_moved_aside_as (+ -wal/-shm/-journal). Never delete it."})
    except OSError as e:
        try:
            staging.unlink()
        except OSError:
            pass
        audit["error"] = f"{type(e).__name__}: {e}"
        return _set(audit, "RESTORE_REFUSED", EXIT_REFUSED,
                    "the restore marker could not be written beside the database; the target was not touched")
    _checkpoint("restore.marker_written")
    moved: list = []
    try:
        _move_set(path, original, moved)
    except OSError as e:
        failures = _move_back(moved)
        try:
            staging.unlink()
        except OSError as unlink_error:
            audit["staging_left"] = f"{staging} ({type(unlink_error).__name__}: {unlink_error})"
        audit["error"] = f"{type(e).__name__}: {e}"
        if failures:
            audit["recovery"] = {"moved": moved, "not_moved_back": failures}
            return _set(audit, "RESTORE_FAILED_MANUAL_RECOVERY", EXIT_VERIFICATION_FAILED,
                        "moving the original aside failed part-way and could not be undone; rename the files listed "
                        "under recovery back by hand before starting anything")
        return _set(audit, "RESTORE_IN_USE", EXIT_REFUSED,
                    "the database (or one of its sidecars) could not be moved: it is in use. Stop every process "
                    "that has it open; nothing was changed")
    audit["moved_aside"] = moved
    _checkpoint("restore.after_move_aside")

    verification: list[str] = []
    try:
        _rename_no_overwrite(staging, path)
        _checkpoint("restore.staging_in_place")
        left = [str(_with_suffix(path, s)) for s in SQLITE_SIDECARS if _with_suffix(path, s).exists()]
        if left:
            verification.append(f"sidecar(s) {left} exist beside the restored file")
        else:
            restored = _inspect_offline(path)
            restored_sha = _file_sha256(path)
            audit["restored"] = {"path": str(path), "sha256": restored_sha,
                                 "integrity_check": restored["integrity_check"],
                                 "history": restored["history"][-1:] or None,
                                 "foreign_key_violations": restored["foreign_key_violations"]}
            if restored_sha != backup_sha:
                verification.append("the restored file's SHA-256 differs from the backup's")
            if restored["integrity_check"] != ["ok"]:
                verification.append(f"integrity_check {restored['integrity_check']}")
            if restored["history"] != facts["history"]:
                verification.append(f"history ends at {restored['history'][-1:]}, the backup's at "
                                    f"{facts['history'][-1:]}")
            if restored["foreign_key_violations"] != facts["foreign_key_violations"]:
                verification.append("foreign key violations differ from the backup's")
            if restored["row_counts"] != facts["row_counts"]:
                verification.append("row counts differ from the backup's")
    except Exception as e:  # noqa: BLE001 — undone below
        verification.append(f"{type(e).__name__}: {e}")
    audit["verification"] = {"problems": verification}
    if not verification:
        audit["history_after"] = facts["history"][-1] if facts["history"] else None
        # The restored file is the installation's database from now on (Phase C): re-bind the record.
        try:
            ro = _connect_preflight(path)
            try:
                installation_state.record(path, by="restore-backup", conn=ro)
            finally:
                ro.close()
            audit["installation_record"] = "re-bound"
        except (OSError, Exception) as e:  # noqa: BLE001 — recorded; the next start then refuses (fail closed)
            audit["installation_record"] = f"NOT WRITTEN ({type(e).__name__}: {e})"
        state["restored"] = True
        return _set(audit, "RESTORED", EXIT_OK,
                    f"restored and verified; the original database set is kept as {original.name} (+ sidecars) until "
                    "you delete it")

    # Undo: the restored file aside, the original set back under its own name.
    _checkpoint("restore.before_undo")
    failed = names["failed"]
    failures = []
    for p in (path, staging):
        if p.exists():
            try:
                _rename_no_overwrite(p, failed if p == path else _with_suffix(failed, ".staging"))
            except OSError as e:
                failures.append(f"{p}: {e}")
    if not failures:
        failures = _move_back(moved)
    _checkpoint("restore.after_undo")
    if failures:
        audit["recovery"] = {"moved": moved, "not_moved_back": failures}
        return _set(audit, "RESTORE_FAILED_MANUAL_RECOVERY", EXIT_VERIFICATION_FAILED,
                    f"the restore did not verify and putting the original back failed; the original set is "
                    f"{original.name} (+ sidecars). Rename it back to {path.name} by hand (with its -wal/-shm) "
                    "before starting anything")
    after = _post_failure_history(path)
    audit["history_after"] = (after[-1] if after else None) if after is not None else "unreadable"
    audit["recovery"] = {"original_restored": True, "failed_restore_kept_as": str(failed)}
    return _set(audit, "RESTORE_VERIFICATION_FAILED", EXIT_VERIFICATION_FAILED,
                "the restored file did not verify; the original database set was put back unchanged")


def _copy_backup(backup: Path, staging: Path) -> None:
    """The staging copy, created exclusively (never over an existing file)."""
    with open(backup, "rb") as source, open(staging, "xb") as target:
        shutil.copyfileobj(source, target, 1 << 20)
        target.flush()
        os.fsync(target.fileno())


def restore_backup(path: Path, backup: Path, pre: dict | None = None) -> int:
    """restore-backup --backup FILE --confirm; the caller (main) holds the instance lock. Every outcome is audited.
    After RESTORED (audited), the restore marker is removed and any migrate-v12 marker is archived as resolved: the
    database is now the verified backup."""
    audit = {"command": f"{RESTORE_COMMAND} --backup {backup} --confirm", "database": str(path),
             "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
             "result": "INCOMPLETE", "exit_code": EXIT_REFUSED, "pre_lock": pre}
    state: dict = {}
    try:
        return _restore(path, backup, audit, state)
    finally:
        audit_ok = _finish_audit(path, audit, "restore")
        _end_marker(state.get("marker"), audit, audit_ok, RESTORE_MARKER_KEPT)
        if state.get("restored") and audit_ok:
            migration_marker = installation_state.marker_file(path, installation_state.MIGRATION_MARKER_SUFFIX)
            if migration_marker.exists():
                try:
                    os.rename(migration_marker, migration_marker.with_name(
                        f"{migration_marker.name}.resolved_by_restore_{_new_stamp()}"))
                except OSError as e:
                    print(f"the migrate-v12 marker {migration_marker} could not be archived ({e}); a normal start "
                          "refuses until it is moved aside by hand", file=sys.stderr)


VERIFY_BACKUP_COMMAND = "verify-backup"
INIT_FRESH_COMMAND = "init-fresh"
ACKNOWLEDGE_COMMAND = "acknowledge-database"
STORAGE_CHECK_COMMAND = "storage-check"


def storage_check(path: Path, backup: Path | None) -> int:
    """storage-check [--db PATH] [--backup FILE_OR_DIR]: read-only. Where the database (and a backup destination)
    physically live; exit 0 only when the database is on fixed local storage (and the destination, if given, is in a
    separate physical failure domain). Opens no file."""
    verdict = storage_classification.classify_path(path)
    print(f"database:        {path}")
    for key, value in verdict.as_record().items():
        print(f"  {key}:".ljust(26) + str(value))
    ok = verdict.acceptable_for_database
    if not ok:
        print(f"  remediation:            {verdict.remediation}")
    if backup is not None:
        dest = storage_classification.classify_path(backup)
        domain = backup_verification.failure_domain(verdict, dest)
        print(f"backup location: {backup}")
        for key, value in dest.as_record().items():
            print(f"  {key}:".ljust(26) + str(value))
        print(f"  same_physical_disk:     {domain['same_physical_disk']}")
        print(f"  separate_storage:       {domain['separate_physical_storage']} ({domain['reason']})")
        ok = ok and domain["separate_physical_storage"]
    print("result:          " + ("OK" if ok else "NOT OK"))
    return EXIT_OK if ok else EXIT_REFUSED

# The installation states a generic writing command refuses before its lock (migrate-v12 refuses every state that
# is not permitted; restore-backup, the recovery path, refuses none; SCHEMA_REGRESSED is left to each command's own
# exact-history check).
GENERIC_REFUSED_STATES = frozenset({
    installation_state.INTERRUPTED_OPERATION, installation_state.RECORD_UNREADABLE, installation_state.ZERO_BYTE,
    installation_state.EMPTY_SQLITE, installation_state.CORRUPT, installation_state.IDENTITY_CHANGED,
    installation_state.PATH_CHANGED, installation_state.ESTABLISHED_MISSING, installation_state.HOT_JOURNAL,
    installation_state.OTHER_INSTALLATION_PRESENT})


def _storage_refusal(path: Path):
    verdict = storage_classification.classify_path(path)
    return verdict, verdict.refusal()


def _print_verification(record: dict, out: Path | None) -> None:
    b = record.get("backup") or {}
    fd = record.get("failure_domain") or {}
    lines = [("result", record.get("result")), ("backup", b.get("path")), ("size", b.get("size")),
             ("sha256", b.get("sha256")), ("integrity_check", b.get("integrity_check")),
             ("schema", b.get("schema_newest")), ("logical_digest", b.get("logical_digest")),
             ("destination", f"{(b.get('storage') or {}).get('category')} "
                             f"(disks {(b.get('storage') or {}).get('physical_disks')})"),
             ("same_disk_as_db", fd.get("same_physical_disk")), ("separation", fd.get("reason")),
             ("matches_db", (record.get("source") or {}).get("matches_backup")),
             ("problems", record.get("problems") or "none"), ("record", out),
             ("attestation", record.get("operator_attestation_required"))]
    stream = sys.stdout if record.get("result") == backup_verification.PASS else sys.stderr
    for key, value in lines:
        print(f"{key}:".ljust(17) + str(value), file=stream)


def verify_backup(path: Path, backup: Path, record_out: Path | None) -> int:
    """verify-backup: read-only on the backup and on the database; writes only the verification record."""
    record = backup_verification.verify_backup_file(backup, source_db=path if path.exists() else None,
                                                    command=VERIFY_BACKUP_COMMAND)
    out = record_out or path.with_name(f"{path.name}.backup_verification_{_new_stamp()}.json")
    try:
        backup_verification.write_record(record, out)
    except (OSError, ValueError) as e:
        print(f"refused: the verification record could not be written to {out} ({type(e).__name__}: {e})",
              file=sys.stderr)
        _print_verification(record, None)
        return EXIT_REFUSED
    _print_verification(record, out)
    return EXIT_OK if record["result"] == backup_verification.PASS else EXIT_REFUSED


def backup_to(path: Path, dest: Path, record_out: Path | None) -> int:
    """backup --dest DIR: an online backup (read-only source) written into DIR, then verified against the database,
    with its verification record. Exit 0 only when the backup is valid as an off-machine copy (PASS)."""
    verdict = storage_classification.classify_path(dest)
    print(f"destination:     {dest} ({verdict.category}: {verdict.reason})")
    if not dest.is_dir():
        print(f"refused: {dest} is not an existing directory. Nothing was written.", file=sys.stderr)
        return EXIT_USAGE
    target = dest / f"{path.name}.manual_{_new_stamp()}_bak"
    if os.path.lexists(target):
        print(f"refused: {target} already exists. Nothing was written.", file=sys.stderr)
        return EXIT_REFUSED
    conn = _connect_readonly(path)
    try:
        written = db.backup_database(conn, target)
    finally:
        conn.close()
    record = backup_verification.verify_backup_file(written, source_db=path, command="backup --dest")
    out = record_out or written.with_name(f"{written.name}.verification.json")
    try:
        backup_verification.write_record(record, out)
    except (OSError, ValueError) as e:
        print(f"the backup {written} was written but its verification record could not be ({type(e).__name__}: "
              f"{e}); run verify-backup on it", file=sys.stderr)
        _print_verification(record, None)
        return EXIT_REFUSED
    _print_verification(record, out)
    if record["result"] != backup_verification.PASS:
        print(f"the backup {written} was written, but it is NOT valid as an off-machine copy "
              f"({record['result']}). Use an external disk or a share on another machine.", file=sys.stderr)
        return EXIT_REFUSED
    return EXIT_OK


def init_fresh(path: Path, acknowledge_prior: bool) -> int:
    """init-fresh --confirm-new-installation: the explicit fresh install (Phase C). Never touches a database that
    holds tables; a zero-byte / empty file is moved aside (kept), never deleted."""
    import os as _os

    from dotenv import load_dotenv

    from backend import crypto
    assessment = installation_state.assess(path)
    print(f"database:        {path}")
    print(f"state:           {assessment.state} ({assessment.reason})")
    if assessment.evidence:
        print(f"evidence:        {', '.join(assessment.evidence[:20])}")
    confirmation = None
    if assessment.state == installation_state.FRESH_INSTALL_PERMITTED:
        pass
    elif assessment.state in (installation_state.ESTABLISHED_MISSING, installation_state.OTHER_INSTALLATION_PRESENT,
                              installation_state.ZERO_BYTE, installation_state.EMPTY_SQLITE):
        if not acknowledge_prior:
            print(f"refused: there is evidence of an earlier installation ({assessment.state}). If its database was "
                  "lost by accident, restore the newest verified backup with restore-backup instead. Only if this "
                  "really is a new installation, add --acknowledge-prior-installation. Nothing was changed.",
                  file=sys.stderr)
            return EXIT_REFUSED
        if assessment.state in (installation_state.ZERO_BYTE, installation_state.EMPTY_SQLITE):
            aside = path.with_name(f"{path.name}.replaced_by_init_fresh_{_new_stamp()}")
            moved: list = []
            try:
                _move_set(path, aside, moved)
            except OSError as e:
                failures = _move_back(moved)
                print(f"refused: the {assessment.state.lower()} file could not be moved aside ({e})"
                      + (f"; NOT moved back: {failures}" if failures else "; nothing was changed"), file=sys.stderr)
                return EXIT_REFUSED
            print(f"kept aside:      {aside.name} (+ sidecars): the {assessment.state.lower()} file, not deleted")
        confirmation = installation_state.FreshInstallConfirmation(
            issued_by="db_maintenance init-fresh --confirm-new-installation --acknowledge-prior-installation",
            acknowledge_prior_installation=True)
    else:
        print(f"refused: init-fresh never replaces a database in state {assessment.state} ({assessment.reason}). "
              "Nothing was changed.", file=sys.stderr)
        return EXIT_REFUSED
    load_dotenv(crypto.ENV_PATH)  # an existing installation's key is reused, never replaced by a second one
    if not _os.environ.get(crypto.ENV_VAR):
        print(f"note:            no {crypto.ENV_VAR} configured; a new key is generated into {crypto.ENV_PATH}")
    db.DB_PATH = path
    try:
        db.init_db(fresh_install=confirmation)
    except (db.DatabaseInstallationStateRefused, db.DatabaseMigrationAuthorizationRequired,
            db.DatabaseSchemaUnsupported) as e:
        print(f"refused: {e}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"created:         a new database at schema v{db.last_init['schema_version']} "
          f"(migrations {db.last_init['applied_migrations']})")
    return EXIT_OK


ACKNOWLEDGEABLE_STATES = frozenset({
    installation_state.IDENTITY_CHANGED, installation_state.SCHEMA_REGRESSED, installation_state.PATH_CHANGED,
    installation_state.LEGACY_UNMARKED, installation_state.ESTABLISHED_VALID, installation_state.RECORD_UNREADABLE})


def acknowledge_database(path: Path) -> int:
    """acknowledge-database --confirm: re-binds the installation record to the database now at `path`, after the
    operator inspected a deliberate replacement or move. The database must verify (integrity, an accepted
    history); a missing, empty, corrupt or mid-maintenance database is refused (restore it instead)."""
    assessment = installation_state.assess(path)
    print(f"database:        {path}")
    print(f"state:           {assessment.state} ({assessment.reason})")
    if assessment.state not in ACKNOWLEDGEABLE_STATES or not path.is_file():
        print(f"refused: {assessment.state} cannot be acknowledged; "
              f"{installation_state._REMEDIATION.get(assessment.state, 'restore a verified backup')} Nothing was "
              "changed.", file=sys.stderr)
        return EXIT_REFUSED
    conn = _connect_readonly(path)
    try:
        conn.execute("PRAGMA query_only = ON")
        integrity = _integrity(conn)
        history = _history_pairs(conn)
        problem = (f"integrity_check {integrity[:10]}" if integrity != ["ok"] else _history_problem(history))
        if problem:
            print(f"refused: the database does not verify ({problem}); restore a verified backup instead. Nothing "
                  "was changed.", file=sys.stderr)
            return EXIT_REFUSED
        if assessment.state == installation_state.RECORD_UNREADABLE:
            for unreadable in (installation_state.record_file(path), installation_state.registry_file(path)):
                if unreadable.exists():
                    kept = unreadable.with_name(f"{unreadable.name}.superseded_{_new_stamp()}")
                    os.rename(unreadable, kept)
                    print(f"kept aside:      {kept}")
        data = installation_state.record(path, by=ACKNOWLEDGE_COMMAND, conn=conn,
                                         note=f"acknowledged {assessment.state}: {assessment.reason}")
    finally:
        conn.close()
    print(f"schema:          {history[-1] if history else None}")
    print(f"size:            {path.stat().st_size} bytes")
    print(f"re-bound:        install {data['install_id']}, file id {data['last_known'].get('st_ino')}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.db_maintenance")
    parser.add_argument("command", choices=["status", "integrity", "backup", "analyze", "vacuum", "migrate",
                                            "rollback-schema-v11", "preflight-v12", V12_COMMAND,
                                            ROLLBACK_V12_COMMAND, RESTORE_COMMAND, VERIFY_BACKUP_COMMAND,
                                            INIT_FRESH_COMMAND, ACKNOWLEDGE_COMMAND, STORAGE_CHECK_COMMAND])
    parser.add_argument("--db", type=Path, default=None, help="database file (default: the app's)")
    parser.add_argument("--confirm", action="store_true",
                        help=f"required by rollback-schema-v11, {ROLLBACK_V12_COMMAND}, {RESTORE_COMMAND} and "
                             f"{ACKNOWLEDGE_COMMAND}")
    parser.add_argument("--backup", type=Path, default=None,
                        help=f"required by {RESTORE_COMMAND} and {VERIFY_BACKUP_COMMAND}: the backup file")
    parser.add_argument(V12_AUTHORIZE_FLAG, dest="authorize_v12", action="store_true",
                        help=f"required by {V12_COMMAND}: the explicit operator authorization for migration 12")
    parser.add_argument(OFFSITE_FLAG, dest="offsite_record", type=Path, default=None,
                        help=f"required by {V12_COMMAND}: a fresh PASS record of the off-machine backup "
                             f"({VERIFY_BACKUP_COMMAND} or backup --dest)")
    parser.add_argument("--dest", type=Path, default=None, help="backup only: write the backup into this directory")
    parser.add_argument("--record-out", dest="record_out", type=Path, default=None,
                        help=f"backup --dest / {VERIFY_BACKUP_COMMAND}: where to write the verification record")
    parser.add_argument("--confirm-new-installation", dest="confirm_new_installation", action="store_true",
                        help=f"required by {INIT_FRESH_COMMAND}")
    parser.add_argument("--acknowledge-prior-installation", dest="acknowledge_prior_installation",
                        action="store_true", help=f"{INIT_FRESH_COMMAND}: create a new database although evidence "
                                                  "of an earlier installation exists")
    args = parser.parse_args(argv)
    # Flag ownership, checked before ANY filesystem access.
    if args.authorize_v12 and args.command != V12_COMMAND:
        print(f"{V12_AUTHORIZE_FLAG} belongs to {V12_COMMAND} only; it authorizes nothing else.", file=sys.stderr)
        return EXIT_USAGE
    owned = [(args.offsite_record is not None, OFFSITE_FLAG, (V12_COMMAND,)),
             (args.dest is not None, "--dest", ("backup",)),
             (args.record_out is not None, "--record-out", ("backup", VERIFY_BACKUP_COMMAND)),
             (args.confirm_new_installation or args.acknowledge_prior_installation,
              "--confirm-new-installation / --acknowledge-prior-installation", (INIT_FRESH_COMMAND,))]
    for given, flag, commands in owned:
        if given and args.command not in commands:
            print(f"{flag} belongs to {' / '.join(commands)} only.", file=sys.stderr)
            return EXIT_USAGE
    if args.command == V12_COMMAND:
        # Checked before ANY filesystem access.
        if not args.authorize_v12:
            print(f"refused: {V12_COMMAND} needs {V12_AUTHORIZE_FLAG}, the explicit operator authorization for "
                  "migration 12. Nothing was opened or changed.", file=sys.stderr)
            return EXIT_USAGE
        if args.offsite_record is None:
            print(f"refused: {V12_COMMAND} needs {OFFSITE_FLAG} FILE, a fresh PASS verification record of the "
                  f"off-machine backup ({VERIFY_BACKUP_COMMAND} or backup --dest). Nothing was opened or changed.",
                  file=sys.stderr)
            return EXIT_USAGE
        path = args.db if args.db is not None else db.DB_PATH
        verdict, refusal = _storage_refusal(path)  # before the lock file, any backup or any write
        if refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return EXIT_REFUSED
        if not path.exists():
            print(f"No database at {path}", file=sys.stderr)
            return EXIT_USAGE
        pre, refusal = _pre_lock_checks(path, ignore_markers=(installation_state.MIGRATION_MARKER_SUFFIX,))
        if refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return EXIT_REFUSED
        pre["storage"] = verdict.as_record()
        from backend import instance_lock
        try:
            with instance_lock.held_for_maintenance(path):
                return migrate_v12(path, args.offsite_record, pre)
        except instance_lock.InstanceLockHeld as e:
            print(f"refused: {e}", file=sys.stderr)
            return EXIT_LOCKED
    if args.backup is not None and args.command not in (RESTORE_COMMAND, VERIFY_BACKUP_COMMAND, STORAGE_CHECK_COMMAND):
        print(f"--backup belongs to {RESTORE_COMMAND} / {VERIFY_BACKUP_COMMAND} / {STORAGE_CHECK_COMMAND} only.",
              file=sys.stderr)
        return EXIT_USAGE
    if args.command == STORAGE_CHECK_COMMAND:
        return storage_check(args.db if args.db is not None else db.DB_PATH, args.backup)
    if args.command == VERIFY_BACKUP_COMMAND:
        if args.backup is None:
            print(f"refused: {VERIFY_BACKUP_COMMAND} needs --backup FILE. Nothing was opened.", file=sys.stderr)
            return EXIT_USAGE
        return verify_backup(args.db if args.db is not None else db.DB_PATH, args.backup, args.record_out)
    if args.command == INIT_FRESH_COMMAND:
        if not args.confirm_new_installation:
            print(f"refused: {INIT_FRESH_COMMAND} needs --confirm-new-installation. Nothing was opened or changed.",
                  file=sys.stderr)
            return EXIT_USAGE
        path = args.db if args.db is not None else db.DB_PATH
        verdict, refusal = _storage_refusal(path)
        if refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return EXIT_REFUSED
        from backend import instance_lock
        try:
            with instance_lock.held_for_maintenance(path):
                return init_fresh(path, args.acknowledge_prior_installation)
        except instance_lock.InstanceLockHeld as e:
            print(f"refused: {e}", file=sys.stderr)
            return EXIT_LOCKED
    if args.command == ACKNOWLEDGE_COMMAND:
        if not args.confirm:
            print(f"refused: {ACKNOWLEDGE_COMMAND} needs --confirm. Nothing was opened or changed.", file=sys.stderr)
            return EXIT_USAGE
        path = args.db if args.db is not None else db.DB_PATH
        verdict, refusal = _storage_refusal(path)
        if refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return EXIT_REFUSED
        from backend import instance_lock
        try:
            with instance_lock.held_for_maintenance(path):
                return acknowledge_database(path)
        except instance_lock.InstanceLockHeld as e:
            print(f"refused: {e}", file=sys.stderr)
            return EXIT_LOCKED
    if args.command == ROLLBACK_V12_COMMAND and not args.confirm:
        # Checked before ANY filesystem access.
        print(f"refused: {ROLLBACK_V12_COMMAND} needs --confirm (it removes the Phase 3 protection journal; see "
              "docs/PHASE_3_V12_REGISTRATION_RUNBOOK_DRAFT.md). Nothing was opened or changed.", file=sys.stderr)
        return EXIT_USAGE
    if args.command == RESTORE_COMMAND:
        # Checked before ANY filesystem access.
        if not args.confirm or args.backup is None:
            print(f"refused: {RESTORE_COMMAND} needs --backup FILE and --confirm (it replaces the whole database "
                  "file). Nothing was opened or changed.", file=sys.stderr)
            return EXIT_USAGE
        path = args.db if args.db is not None else db.DB_PATH
        if os.path.normcase(os.path.abspath(args.backup)) == os.path.normcase(os.path.abspath(path)):
            print(f"refused: the backup and the database are the same file ({path}).", file=sys.stderr)
            return EXIT_USAGE
        verdict, refusal = _storage_refusal(path)  # before the lock file or any copy
        if refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return EXIT_REFUSED
        # The recovery path: every installation state is allowed (a missing, empty or replaced database is exactly
        # what a restore repairs); the state is recorded in the audit.
        pre, _journal = _pre_lock_checks(path, ignore_markers=installation_state.IN_PROGRESS_SUFFIXES,
                                         refuse_states=False)
        pre["storage"] = verdict.as_record()
        from backend import instance_lock
        try:
            with instance_lock.held_for_maintenance(path):
                return restore_backup(path, args.backup, pre)
        except instance_lock.InstanceLockHeld as e:
            print(f"refused: {e}", file=sys.stderr)
            return EXIT_LOCKED
    if args.command == "preflight-v12":
        # Checked before ANY filesystem access: an explicit --db, and never the live database.
        if args.db is None:
            print("preflight-v12 needs --db <operator backup>; it never defaults to the live database.",
                  file=sys.stderr)
            return 2
        refusal = preflight_v12_refusal(args.db)
        if refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return 2
    path: Path = args.db if args.db is not None else db.DB_PATH
    if not path.exists():
        print(f"No database at {path}", file=sys.stderr)
        return 2
    if args.command == "rollback-schema-v11" and not args.confirm:
        print("rollback-schema-v11 needs --confirm (it removes the Phase 2 tables; see the Phase 2 report).",
              file=sys.stderr)
        return 2
    if args.command in WRITING_COMMANDS:
        verdict, refusal = _storage_refusal(path)  # before the lock file or any write
        if refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return EXIT_REFUSED
        if args.command != "migrate":  # migrate runs init_db, which checks the installation state itself
            _facts, refusal = _pre_lock_checks(path, refuse_states=GENERIC_REFUSED_STATES)
            if refusal:
                print(f"refused: {refusal}", file=sys.stderr)
                return EXIT_REFUSED
        from backend import instance_lock
        try:
            with instance_lock.held_for_maintenance(path):
                return _run(args.command, path)
        except instance_lock.InstanceLockHeld as e:
            print(f"refused: {e}", file=sys.stderr)
            return 3
    if args.command == "backup" and args.dest is not None:
        return backup_to(path, args.dest, args.record_out)
    return _run(args.command, path)


def _run(command: str, path: Path) -> int:
    if command == "migrate":
        return migrate(path)
    if command == "rollback-schema-v11":
        return rollback_v11(path)
    if command == ROLLBACK_V12_COMMAND:
        return rollback_v12(path)
    if command == "preflight-v12":
        return preflight_v12(path)
    conn = _connect(path) if command in WRITING_COMMANDS else _connect_readonly(path)
    try:
        if command == "status":
            status = db.schema_status(conn)
            print(f"database:  {path} ({path.stat().st_size / 1_048_576:.1f} MB)")
            print(f"schema:    v{status['current_version']} of v{status['latest_version']}"
                  f"{'' if status['supported'] else '  UNSUPPORTED: ' + str(status['error'])}")
            pending = ", ".join("{} {}".format(p["version"], p["name"]) for p in status["pending"])
            print(f"pending:   {pending or 'none'}")
            for h in status["history"]:
                print(f"  applied v{h['version']} {h['migration_name']} at {h['applied_at']} ({h['duration_ms']} ms)")
            return 0 if status["supported"] else 1
        if command == "integrity":
            result = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            print(f"integrity_check: {', '.join(result[:10])}")
            print(f"foreign_key_check: {len(fk)} violation(s)")
            return 0 if result == ["ok"] and not fk else 1
        if command == "backup":
            target = path.with_name(f"{path.name}.manual_{time.strftime('%Y%m%d%H%M%S')}_bak")
            written = db.backup_database(conn, target)
            print(f"backup written and verified: {written}")
            return 0
        if command == "analyze":
            conn.execute("ANALYZE")
            conn.commit()
            print("ANALYZE done.")
            return 0
        if command == "vacuum":
            print("VACUUM rewrites the whole file; the app must be stopped. Running...")
            conn.execute("VACUUM")
            print(f"VACUUM done: {path.stat().st_size / 1_048_576:.1f} MB")
            return 0
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
