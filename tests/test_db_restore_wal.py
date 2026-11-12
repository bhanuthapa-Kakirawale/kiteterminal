"""WAL and sidecar safety, and `db_maintenance restore-backup` (docs/PHASE_3_V12_REGISTRATION_REMEDIATION_REPORT.md
§5). A SQLite database in WAL mode is a SET of files: <db>, <db>-wal (committed transactions not yet copied into
<db>) and <db>-shm. SQLite pairs them by NAME only, so a backup copied over <db> while a stale <db>-wal is still
there gets that old WAL replayed onto it: w05b shows the naive copy yielding a database that passes integrity_check
yet holds the wrong content. restore-backup moves the original set aside together and verifies before it finishes.

Observed on this platform (SQLite 3.50.4) and pinned here: a read-only open of a WAL-mode file with no sidecars
creates an empty -wal and a -shm, which stay after close (w01); a read-only reader sees committed rows that exist
only in the WAL and leaves the WAL's bytes unchanged (w02); an immutable open ignores the WAL (why restore-backup
refuses a backup with a non-empty -wal of its own, w09).

Every file is a temporary one under tmp_path (env's database, or scratch copies of it); the real account database
and its sidecars are never opened (tests/real_data_guard.py)."""

import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import offsite_helpers
import schema_builds
from conftest import release_write_authority
from schema_builds import V11, V12, phase2_build, registered_build
from test_dark_protection_schema import _position, _seed_application_data
from test_schema_v12 import _content
from test_v12_authorization_gate import LOCK_CHILD, _checkpoint

pytestmark = pytest.mark.v11_database

SIDECARS = ("-wal", "-shm", "-journal")


# ---------------------------------------------------------------- helpers

def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sidecars(path) -> dict:
    return {s: Path(f"{path}{s}").stat().st_size for s in SIDECARS if Path(f"{path}{s}").exists()}


def _history(path, *, immutable=False) -> list[tuple]:
    uri = f"file:{Path(path).as_posix()}?mode=ro{'&immutable=1' if immutable else ''}"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return [tuple(r) for r in conn.execute("SELECT version, migration_name FROM schema_version ORDER BY version")]
    finally:
        conn.close()


def _count(path, table, *, immutable=False) -> int:
    uri = f"file:{Path(path).as_posix()}?mode=ro{'&immutable=1' if immutable else ''}"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _wal_mode_header(path) -> bool:
    header = Path(path).read_bytes()[:100]
    return header[18] == 2 and header[19] == 2           # file format read/write version 2 = WAL


def _scratch_copy(env, name) -> Path:
    """A closed, checkpointed copy of env's database (an offline WAL-mode file with no sidecars)."""
    target = env.tmp_path / name
    source = env.db.get_connection()
    try:
        env.db.backup_database(source, target)
    finally:
        source.close()
    assert _sidecars(target) == {} and _wal_mode_header(target)
    return target


def _held_writer(path):
    """A connection that keeps committed rows in the WAL (no automatic checkpoint) while it stays open."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    return conn


def _restore(env, backup, *extra, db=None) -> int:
    from backend import db_maintenance
    return db_maintenance.main(["restore-backup", "--backup", str(backup), *extra,
                                "--db", str(db or env.db.DB_PATH)])


def _audit(env) -> dict:
    audits = sorted(env.tmp_path.glob("test.db.restore_audit_*.json"))
    assert audits, "no audit file"
    return json.loads(audits[-1].read_text(encoding="utf-8"))


def _verified_v11_backup(env) -> Path:
    """The operator's backup: `db_maintenance backup` (online backup + integrity check) of env's v11 database."""
    from backend import db_maintenance
    assert db_maintenance.main(["backup", "--db", str(env.db.DB_PATH)]) == 0
    [backup] = sorted(env.tmp_path.glob("test.db.manual_*_bak"))
    assert _sidecars(backup) == {} and _history(backup, immutable=True)[-1] == V11
    return backup


def _crash_set_at_v12(env) -> int:
    """env's database brought to v12, then left as a CRASHED set: rows committed only in its -wal, with -wal and
    -shm on disk under the database's own name and no process holding them. Returns the WAL-only ticket id."""
    path = env.db.DB_PATH
    _checkpoint(path)
    writer = _held_writer(path)                            # held from BEFORE the migration: v12 lands in the WAL
    try:
        schema_builds.to_v12(env.db)
        wal_only = writer.execute(
            "INSERT INTO trade_tickets (account_id, symbol, exchange, instrument_token, transaction_type, quantity, "
            "entry_price, entry_order_type, stop_loss, target_1, product_type, rationale, status) VALUES "
            "((SELECT id FROM accounts WHERE is_default = 1), 'INFY', 'NSE', 408065, 'BUY', 1, 1500, 'LIMIT', 1400, "
            "1600, 'CNC', 'only in the WAL', 'pending_confirmation')").lastrowid
        writer.commit()
        crash = env.tmp_path / "crash"
        crash.mkdir()
        for s in ("", "-wal", "-shm"):
            shutil.copyfile(f"{path}{s}", crash / f"set{s}")
    finally:
        writer.close()                                     # this checkpoints and removes the live -wal
    for s in ("", "-wal", "-shm"):
        shutil.copyfile(crash / f"set{s}", f"{path}{s}")   # the crashed set, back under the database's name
    assert _sidecars(path).get("-wal", 0) > 0
    return wal_only


@pytest.fixture
def stopped(env):
    release_write_authority()
    return env


# ================================================================ startup check and WAL reads

def test_w01_the_read_only_startup_check_on_a_wal_file_without_sidecars_never_writes_it(stopped):
    path = _scratch_copy(stopped, "wal_db.db")
    sha = _sha(path)
    with registered_build(stopped.db):
        with pytest.raises(stopped.db.DatabaseMigrationAuthorizationRequired):
            stopped.db.require_startup_migration_authorization(path)
    with phase2_build(stopped.db):
        stopped.db.require_startup_migration_authorization(path)   # v11 on the Phase 2 build: nothing pending
    assert _sha(path) == sha                                          # the main file: not a byte
    # Observed: the mode=ro open creates an EMPTY -wal and a -shm, which stay after close (harmless: no frame).
    assert _sidecars(path).get("-wal", 0) == 0 and "-journal" not in _sidecars(path)


def test_w02_with_a_non_empty_wal_the_startup_check_reads_the_committed_history_and_leaves_the_wal(stopped):
    path = _scratch_copy(stopped, "wal_db.db")
    writer = _held_writer(path)
    try:
        writer.execute("DELETE FROM schema_version WHERE version = 11")   # committed in the WAL only
        writer.commit()
        main_sha, wal_sha = _sha(path), _sha(f"{path}-wal")
        assert _history(path, immutable=True)[-1] == V11                  # the main file alone still says v11
        with registered_build(stopped.db):
            with pytest.raises(stopped.db.DatabaseMigrationAuthorizationRequired, match="schema version 10"):
                stopped.db.require_startup_migration_authorization(path)  # it read the WAL: v10, not v11
        assert (_sha(path), _sha(f"{path}-wal")) == (main_sha, wal_sha)
    finally:
        writer.close()


def test_w03_migrate_v12_on_a_target_whose_newest_rows_are_only_in_its_wal_backs_them_up(stopped):
    from backend import db_maintenance
    env = stopped
    _seed_application_data(env)
    path = env.db.DB_PATH
    _checkpoint(path)
    before = _count(path, "trade_tickets")
    writer = _held_writer(path)
    try:
        for _ in range(3):
            writer.execute("INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES "
                           "((SELECT MIN(id) FROM trade_tickets), 'modified', 'wal only')")
        writer.commit()
        events = _count(path, "ticket_events")
        assert _count(path, "ticket_events", immutable=True) == events - 3   # those rows are in the WAL only
        with registered_build(env.db):
            assert offsite_helpers.run_migrate_v12(path) == 0   # with its required off-machine backup record
    finally:
        writer.close()
    audit = json.loads(sorted(env.tmp_path.glob("test.db.v12_migration_audit_*.json"))[-1].read_text("utf-8"))
    assert audit["backup"]["problems"] == []                              # row counts equal the target's
    backup = Path(audit["backup"]["path"])
    assert _sidecars(backup) == {}                                         # one self-contained file
    assert _count(backup, "ticket_events", immutable=True) == events      # the WAL rows are IN the backup
    assert audit["target_before"]["row_counts"]["ticket_events"] == events
    assert _count(backup, "trade_tickets", immutable=True) == before


def test_w04_a_manual_backup_of_a_database_with_a_non_empty_wal_has_the_targets_row_counts(stopped):
    from backend import db_maintenance
    env = stopped
    _seed_application_data(env)
    path = env.db.DB_PATH
    writer = _held_writer(path)
    try:
        _position(env, symbol="INFY")                                      # committed while the WAL is held
        writer.execute("INSERT INTO ticket_events (ticket_id, event_type) VALUES ((SELECT MIN(id) FROM "
                       "trade_tickets), 'modified')")
        writer.commit()
        expected = {t: _count(path, t) for t in ("trade_tickets", "ticket_events", "schema_version")}
        assert db_maintenance.main(["backup", "--db", str(path)]) == 0
    finally:
        writer.close()
    [backup] = sorted(env.tmp_path.glob("test.db.manual_*_bak"))
    assert {t: _count(backup, t, immutable=True) for t in expected} == expected


# ================================================================ restore-backup

def test_w05_restore_with_stale_sidecars_present_gives_exactly_the_v11_backup(stopped, capsys):
    env = stopped
    _seed_application_data(env)
    path = env.db.DB_PATH
    backup = _verified_v11_backup(env)
    backup_content = _content(backup)
    wal_only = _crash_set_at_v12(env)
    with registered_build(env.db):
        assert _restore(env, backup, "--confirm") == 0
    assert "RESTORED" in capsys.readouterr().out
    assert _sidecars(path) == {}                                    # no stale sidecar kept the database's name
    assert _sha(path) == _sha(backup)                               # byte for byte the backup
    assert _history(path) == _history(backup, immutable=True) and _history(path)[-1] == V11
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    assert _content(path) == backup_content                        # every row of every table
    audit = _audit(env)
    assert audit["result"] == "RESTORED" and audit["exit_code"] == 0
    assert audit["history_before"] == list(V12) and audit["history_after"] == list(V11)
    assert audit["verification"]["problems"] == []
    # The original set is kept whole under one new name, its WAL still paired with it: it opens to v12 with the
    # WAL-only row (nothing of it was lost or replayed elsewhere).
    [original] = [Path(p) for _, p in audit["moved_aside"] if p.endswith("_orig")]
    assert {Path(p).name for _, p in audit["moved_aside"]} >= {original.name, original.name + "-wal"}
    assert _history(original)[-1] == V12
    conn = sqlite3.connect(original)
    try:
        assert conn.execute("SELECT rationale FROM trade_tickets WHERE id = ?", (wal_only,)).fetchone()[0] == \
            "only in the WAL"
    finally:
        conn.close()
    with registered_build(env.db):                                 # the registered build: v12 pending, refused
        with pytest.raises(env.db.DatabaseMigrationAuthorizationRequired):
            env.db.require_startup_migration_authorization(path)
    with phase2_build(env.db):                                     # the Phase 2 build starts it
        env.db.init_db()
        assert env.db.last_init["applied_migrations"] == []


def test_w05b_negative_control_a_naive_copy_over_stale_sidecars_replays_the_old_wal(stopped):
    """Why restore-backup exists: copying the backup over the main file while the crashed set's -wal and -shm keep
    its name yields a database that passes integrity_check but is NOT the backup."""
    env = stopped
    _seed_application_data(env)
    path = env.db.DB_PATH
    backup = _verified_v11_backup(env)
    _crash_set_at_v12(env)
    shutil.copyfile(backup, path)                                  # the naive restore
    conn = sqlite3.connect(path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        newest = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    finally:
        conn.close()
    assert integrity == "ok"                                       # nothing looks wrong...
    assert newest == 12 != _history(backup, immutable=True)[-1][0]  # ...yet it is the replayed v12 state


def test_w06_a_restore_that_does_not_verify_puts_the_original_set_back(stopped, monkeypatch):
    from backend import db_maintenance
    env = stopped
    _seed_application_data(env)
    path = env.db.DB_PATH
    backup = _verified_v11_backup(env)
    _crash_set_at_v12(env)
    original_files = {s: _sha(f"{path}{s}") for s in ("", "-wal") if Path(f"{path}{s}").exists()}
    real = db_maintenance._inspect_offline

    def mismatched_after_restore(p):
        facts = real(p)
        if Path(p) == Path(path):                                  # only the restored file
            facts = {**facts, "integrity_check": ["simulated corruption"]}
        return facts

    monkeypatch.setattr(db_maintenance, "_inspect_offline", mismatched_after_restore)
    with registered_build(env.db):
        assert _restore(env, backup, "--confirm") == 4
    audit = _audit(env)
    assert audit["result"] == "RESTORE_VERIFICATION_FAILED" and audit["recovery"]["original_restored"] is True
    assert {s: _sha(f"{path}{s}") for s in original_files} == original_files   # the original set, byte for byte
    assert _history(path)[-1] == V12                                           # with its own WAL
    assert Path(audit["recovery"]["failed_restore_kept_as"]).exists()


def test_w07_without_confirm_or_backup_it_is_refused_before_any_filesystem_access(stopped, capsys):
    from backend import db_maintenance, instance_lock
    env = stopped
    sha = _sha(env.db.DB_PATH)

    def no_filesystem(*args, **kwargs):
        raise AssertionError("filesystem accessed before the argument check")

    for argv in (["--backup", str(env.tmp_path / "x_bak")], ["--confirm"]):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pathlib.Path, "exists", no_filesystem)
            mp.setattr(pathlib.Path, "is_file", no_filesystem)
            mp.setattr(sqlite3, "connect", no_filesystem)
            mp.setattr(instance_lock, "held_for_maintenance", no_filesystem)
            code = db_maintenance.main(["restore-backup", *argv, "--db", str(env.db.DB_PATH)])
        assert code == 2
    assert "needs --backup FILE and --confirm" in capsys.readouterr().err
    assert _sha(env.db.DB_PATH) == sha and not list(env.tmp_path.glob("*restore*"))


def test_w07b_the_backup_cannot_be_the_database_itself(stopped):
    assert _restore(stopped, stopped.db.DB_PATH, "--confirm") == 2


def test_w08_it_refuses_while_the_application_holds_the_lock(stopped, capsys):
    env = stopped
    backup = _verified_v11_backup(env)
    _checkpoint(env.db.DB_PATH)
    sha = _sha(env.db.DB_PATH)
    child = subprocess.Popen([sys.executable, str(LOCK_CHILD), "hold", json.dumps({"db": str(env.db.DB_PATH)})],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "LOCKED"
        assert _restore(env, backup, "--confirm") == 3
    finally:
        child.stdin.close()
        child.wait(timeout=30)
    assert "ANOTHER_INSTANCE_RUNNING" in capsys.readouterr().err
    assert _sha(env.db.DB_PATH) == sha and not list(env.tmp_path.glob("*restore*"))


def _assert_nothing_moved(env, sha):
    assert _sha(env.db.DB_PATH) == sha
    assert not list(env.tmp_path.glob("test.db.pre_restore_*")) and not list(env.tmp_path.glob("*restore_staging*"))


def test_w09_a_backup_with_a_non_empty_wal_of_its_own_is_refused(stopped):
    env = stopped
    backup = _verified_v11_backup(env)
    Path(f"{backup}-wal").write_bytes(b"\x37\x7f\x06\x82" + b"\x00" * 60)  # a WAL header: not self-contained
    _checkpoint(env.db.DB_PATH)
    sha = _sha(env.db.DB_PATH)
    assert _restore(env, backup, "--confirm") == 1
    assert _audit(env)["result"] == "BACKUP_VERIFICATION_FAILED"
    _assert_nothing_moved(env, sha)


def test_w09b_a_corrupt_backup_is_refused(stopped):
    env = stopped
    backup = _verified_v11_backup(env)
    data = bytearray(backup.read_bytes())
    data[0:100] = b"\xAB" * 100
    backup.write_bytes(bytes(data))
    _checkpoint(env.db.DB_PATH)
    sha = _sha(env.db.DB_PATH)
    assert _restore(env, backup, "--confirm") == 1
    assert _audit(env)["result"] == "BACKUP_VERIFICATION_FAILED"
    _assert_nothing_moved(env, sha)


def test_w09c_a_backup_this_build_cannot_run_is_refused(stopped):
    env = stopped
    schema_builds.to_v12(env.db)
    backup = _verified_v11_backup_any(env)                       # a v12 backup
    schema_builds.to_v11(env.db)
    _checkpoint(env.db.DB_PATH)
    sha = _sha(env.db.DB_PATH)
    with phase2_build(env.db):                                   # the Phase 2 build cannot run a v12 database
        assert _restore(env, backup, "--confirm") == 1
    audit = _audit(env)
    assert audit["result"] == "BACKUP_VERIFICATION_FAILED" and "newer than this code" in audit["detail"]
    _assert_nothing_moved(env, sha)


def _verified_v11_backup_any(env) -> Path:
    from backend import db_maintenance
    assert db_maintenance.main(["backup", "--db", str(env.db.DB_PATH)]) == 0
    return sorted(env.tmp_path.glob("test.db.manual_*_bak"))[-1]


@pytest.mark.skipif(os.name != "nt", reason="Windows refuses to rename a file another handle has open")
def test_w10_a_database_still_open_elsewhere_is_never_replaced(stopped):
    env = stopped
    backup = _verified_v11_backup(env)
    _checkpoint(env.db.DB_PATH)
    sha = _sha(env.db.DB_PATH)
    holder = sqlite3.connect(env.db.DB_PATH)                     # a process that ignored the lock
    try:
        holder.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()
        assert _restore(env, backup, "--confirm") == 1
    finally:
        holder.close()
    assert _audit(env)["result"] == "RESTORE_IN_USE"
    assert _sha(env.db.DB_PATH) == sha
    assert not list(env.tmp_path.glob("test.db.pre_restore_*")) and not list(env.tmp_path.glob("*restore_staging*"))


def test_w11_too_little_disk_space_refuses_before_any_copy(stopped, monkeypatch):
    from backend import db_maintenance
    env = stopped
    backup = _verified_v11_backup(env)
    _checkpoint(env.db.DB_PATH)
    sha = _sha(env.db.DB_PATH)
    monkeypatch.setattr(db_maintenance.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    assert _restore(env, backup, "--confirm") == 1
    assert _audit(env)["result"] == "DISK_SPACE_INSUFFICIENT"
    _assert_nothing_moved(env, sha)


def test_w12_restore_backup_is_a_lock_taking_writing_command_and_imports_no_broker_module():
    from backend import db_maintenance
    assert "restore-backup" in db_maintenance.WRITING_COMMANDS
    assert not any(m in sys.modules and m in db_maintenance.__dict__ for m in ("kite_client", "execution"))
