"""migrate-v12 real-target preflight hardening (V12 final readiness, Phase E).

Every new refusal happens before the database is written; the migration is bound to the exact file (volume serial
+ file index) and content (logical digest) the checks examined, re-checked right before and INSIDE v12's own
transaction; a fresh off-machine backup is mandatory and re-verified live; an in-progress marker is written before
the first write and removed only for an unambiguous, audited outcome. Every database is env's temporary one at v11
(v11_database); only the storage CLASS of a simulated external disk is faked (tests/offsite_helpers.py).
"""

import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import offsite_helpers
from schema_builds import V11, V12, registered_build
from test_dark_protection_schema import _position, _seed_application_data
from test_schema_v12 import _dark_status, _history
from test_v12_authorization_gate import _assert_untouched, _audit, _lock_is_free, _state, stopped  # noqa: F401

from backend import installation_state as ist
from backend import storage_classification as sc

pytestmark = pytest.mark.v11_database

AUDIT = "test.db.v12_migration_audit_"
LOCAL_BACKUP = "test.db.pre_v12_authorized_"


def _main(*argv):
    from backend import db_maintenance
    return db_maintenance.main(list(argv))


def _db(env) -> Path:
    return Path(env.db.DB_PATH)


def _record(env) -> Path:
    root = env.tmp_path / "offsite_disk"
    with offsite_helpers.offsite_disk(root):
        return offsite_helpers.make_record(_db(env), root)


def _migrate(env, record=None, *extra) -> int:
    root = env.tmp_path / "offsite_disk"
    with offsite_helpers.offsite_disk(root):
        record = record or offsite_helpers.make_record(_db(env), root)
        with registered_build(env.db):
            return _main("migrate-v12", "--authorize-v12", "--offsite-record", str(record), *extra,
                         "--db", str(_db(env)))


def _marker(env, suffix=ist.MIGRATION_MARKER_SUFFIX) -> Path:
    return ist.marker_file(_db(env), suffix)


def _nothing_written_beside(env, before_names: set):
    new = sorted(set(p.name for p in env.tmp_path.iterdir()) - before_names)
    assert all(n.startswith(("test.db-wal", "test.db-shm")) for n in new), new


@contextmanager
def _classified(category, *, only=None):
    """sc.classify_path returns `category` for the database path (or every path when only is None)."""
    previous = sc.classify_path

    def fake(path, adapter=None):
        if only is None or os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(only)):
            return sc.StorageVerdict(category, f"test: simulated {category}", str(path))
        return previous(path, adapter)
    sc.classify_path = fake
    try:
        yield
    finally:
        sc.classify_path = previous


# ================================================================ arguments: before any filesystem access

def test_h01_migrate_v12_without_an_offsite_record_is_refused_before_any_filesystem_access(stopped, monkeypatch,
                                                                                           capsys):
    from backend import db_maintenance
    env = stopped
    touched = []
    real_exists = db_maintenance.Path.exists
    monkeypatch.setattr(db_maintenance.Path, "exists", lambda self: (touched.append(self), real_exists(self))[1])
    monkeypatch.setattr(sc, "classify_path", lambda *a, **k: pytest.fail("classified before the argument check"))
    target = env.tmp_path / "never_opened.db"
    assert db_maintenance.main(["migrate-v12", "--authorize-v12", "--db", str(target)]) == 2
    assert "--offsite-record" in capsys.readouterr().err and touched == []
    assert not list(env.tmp_path.glob("never_opened.db*"))


@pytest.mark.parametrize("command", ["status", "backup", "restore-backup", "rollback-schema-v12", "verify-backup"])
def test_h02_the_offsite_flag_belongs_to_migrate_v12_only(stopped, command, capsys):
    assert _main(command, "--offsite-record", "x.json", "--db", str(_db(stopped))) == 2


# ================================================================ fixed-local-drive: before the lock, any backup, any write

@pytest.mark.parametrize("category", [sc.REMOVABLE, sc.NETWORK_MAPPED, sc.NETWORK_UNC, sc.CLOUD_SYNCED, sc.UNKNOWN,
                                      sc.UNAVAILABLE, sc.VIRTUAL_DISK])
def test_h03_a_target_not_on_fixed_local_storage_is_refused_before_the_lock_and_any_write(stopped, category, capsys):
    env = stopped
    record = _record(env)
    before, names = _state(env), {p.name for p in env.tmp_path.iterdir()}
    lock = Path(f"{_db(env)}.instance.lock")
    lock_before = lock.stat().st_mtime_ns if lock.exists() else None
    with _classified(category, only=_db(env)):
        with registered_build(env.db):
            code = _main("migrate-v12", "--authorize-v12", "--offsite-record", str(record), "--db", str(_db(env)))
    assert code == 1
    err = capsys.readouterr().err
    assert "STORAGE_NOT_FIXED_LOCAL" in err and category in err and "Nothing was opened or changed" in err
    _assert_untouched(env, before)
    _nothing_written_beside(env, names)
    assert (lock.stat().st_mtime_ns if lock.exists() else None) == lock_before
    assert not _marker(env).exists() and _history(env)[-1] == V11


def test_h04_a_unc_target_is_refused_from_its_text_without_contacting_the_share(stopped, capsys):
    env = stopped
    record = _record(env)
    started = time.monotonic()
    with registered_build(env.db):
        assert _main("migrate-v12", "--authorize-v12", "--offsite-record", str(record),
                     "--db", r"\\203.0.113.9\share\kite_terminal.db") == 1
    assert time.monotonic() - started < 5 and "NETWORK_UNC" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["restore-backup", "--backup", "{bak}", "--confirm"], ["rollback-schema-v12", "--confirm"],
                                  ["analyze"], ["vacuum"], ["acknowledge-database", "--confirm"],
                                  ["init-fresh", "--confirm-new-installation"], ["rollback-schema-v11", "--confirm"]])
def test_h05_every_command_that_writes_the_database_runs_the_storage_check_first(stopped, argv, capsys):
    env = stopped
    bak = env.tmp_path / "some.db_bak"
    bak.write_bytes(b"")
    argv = [a.replace("{bak}", str(bak)) for a in argv]
    before = _state(env)
    with _classified(sc.REMOVABLE, only=_db(env)):
        assert _main(*argv, "--db", str(_db(env))) == 1
    assert "STORAGE_NOT_FIXED_LOCAL" in capsys.readouterr().err
    _assert_untouched(env, before)


# ================================================================ installation state and hot journal: before the lock

def test_h06_a_replaced_database_is_refused_before_the_lock(stopped, capsys):
    env = stopped
    path = _db(env)
    copy = env.tmp_path / "copy.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    shutil.copyfile(path, copy)
    os.replace(copy, path)
    record = _record(env)
    with registered_build(env.db):
        assert _main("migrate-v12", "--authorize-v12", "--offsite-record", str(record), "--db", str(path)) == 1
    assert "IDENTITY_CHANGED" in capsys.readouterr().err and _history(env)[-1] == V11
    assert not list(env.tmp_path.glob(LOCAL_BACKUP + "*"))


def test_h07_a_restore_marker_refuses_migrate_v12(stopped, capsys):
    env = stopped
    record = _record(env)
    _marker(env, ist.RESTORE_MARKER_SUFFIX).write_text("{}")
    with registered_build(env.db):
        assert _main("migrate-v12", "--authorize-v12", "--offsite-record", str(record), "--db", str(_db(env))) == 1
    assert "INTERRUPTED_OPERATION" in capsys.readouterr().err and _history(env)[-1] == V11


def test_h08_a_hot_rollback_journal_is_refused_and_left_untouched(stopped, capsys):
    env = stopped
    record = _record(env)
    journal = Path(f"{_db(env)}-journal")
    journal.write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + b"\x00" * 504)
    with registered_build(env.db):
        assert _main("migrate-v12", "--authorize-v12", "--offsite-record", str(record), "--db", str(_db(env))) == 1
    assert "HOT_JOURNAL" in capsys.readouterr().err
    assert journal.read_bytes()[:8] == b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" and _history(env)[-1] == V11


# ================================================================ the off-machine backup: mandatory, fresh, this content

def _edited(record: Path, **changes) -> Path:
    data = json.loads(record.read_text(encoding="utf-8"))
    data.update(changes)
    out = record.with_name(record.stem + f"_edited_{time.monotonic_ns()}.json")
    out.write_text(json.dumps(data), encoding="utf-8")
    return out


@pytest.mark.parametrize("damage", ["stale", "future", "failed", "tampered_backup", "content_changed_since",
                                    "backup_deleted", "not_a_record"])
def test_h09_an_unusable_offsite_backup_refuses_the_migration_after_the_local_backup(stopped, damage, capsys):
    env = stopped
    record = _record(env)
    backup = Path(json.loads(record.read_text(encoding="utf-8"))["backup"]["path"])
    if damage == "stale":
        record = _edited(record, verified_at_epoch=time.time() - 13 * 3600)
    elif damage == "future":
        record = _edited(record, verified_at_epoch=time.time() + 7200)
    elif damage == "failed":
        record = _edited(record, result="FAIL")
    elif damage == "tampered_backup":
        data = bytearray(backup.read_bytes())
        data[-100] ^= 0xFF
        backup.write_bytes(bytes(data))
    elif damage == "content_changed_since":
        _seed_application_data(env)
    elif damage == "backup_deleted":
        backup.unlink()
    elif damage == "not_a_record":
        record.write_text("{}", encoding="utf-8")
    before = _state(env)
    assert _migrate(env, record) == 1
    audit = _audit(env)
    assert audit["result"] == "OFFSITE_BACKUP_REFUSED" and audit["offsite"]["problems"]
    _assert_untouched(env, before, allowed_new=(LOCAL_BACKUP, AUDIT))
    assert _history(env)[-1] == V11 and _dark_status(env)["state"] == "absent"
    assert not _marker(env).exists()                                     # an unambiguous refusal: marker removed


def test_h10_an_offsite_backup_on_the_same_physical_disk_is_refused(stopped, capsys):
    """The real classifier, no simulation: a backup in another folder of this machine's only disk."""
    env = stopped
    local = env.tmp_path / "same_disk_backup"
    local.mkdir()
    assert _main("backup", "--dest", str(local), "--db", str(_db(env))) == 1     # written, but not off-machine
    record = next(local.glob("*.verification.json"))
    with registered_build(env.db):
        assert _main("migrate-v12", "--authorize-v12", "--offsite-record", str(record), "--db", str(_db(env))) == 1
    assert _audit(env)["result"] == "OFFSITE_BACKUP_REFUSED" and _history(env)[-1] == V11


def test_h11_the_offsite_backup_is_re_verified_live_never_taken_from_the_record(stopped, monkeypatch):
    from backend import backup_verification
    env = stopped
    record = _record(env)
    calls = []
    real = backup_verification.verify_backup_file
    monkeypatch.setattr(backup_verification, "verify_backup_file",
                        lambda *a, **k: (calls.append(k.get("command")), real(*a, **k))[1])
    assert _migrate(env, record) == 0
    assert "migrate-v12 offsite re-verification" in calls
    assert _audit(env)["offsite"]["live"]["result"] == "PASS"


# ================================================================ identity binding (time of check / time of use)

def test_h12_the_database_replaced_between_the_pre_lock_checks_and_the_migration_is_refused(stopped, monkeypatch):
    """A realistic swap: the file is replaced (same content, new file) after the pre-lock checks, as the lock is
    taken. The off-machine backup still matches the content, so only the identity binding can catch it."""
    from backend import instance_lock
    env = stopped
    path = _db(env)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    record = _record(env)
    real_lock = instance_lock.held_for_maintenance

    @contextmanager
    def swap_then_lock(db_path):
        copy = env.tmp_path / "swapped.db"
        shutil.copyfile(path, copy)
        os.replace(copy, path)
        with real_lock(db_path) as held:
            yield held
    monkeypatch.setattr(instance_lock, "held_for_maintenance", swap_then_lock)
    assert _migrate(env, record) == 1
    audit = _audit(env)
    assert audit["result"] == "DATABASE_IDENTITY_CHANGED" and _history(env)[-1] == V11
    assert _dark_status(env)["state"] == "absent"


def test_h13_the_identity_guard_runs_inside_v12s_transaction_under_the_write_lock(stopped, monkeypatch):
    from backend import db_maintenance
    env = stopped
    real = db_maintenance._identity_problem
    seen = []

    def problem_inside_transaction(path, pre):
        seen.append(len(seen))
        if len(seen) == 2:                       # 1: before db.migrate, 2: the guard inside the transaction
            other = sqlite3.connect(path, timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute("BEGIN IMMEDIATE")            # the migration holds the write lock
            finally:
                other.close()
            return "simulated: the file changed under the write lock"
        return real(path, pre)
    monkeypatch.setattr(db_maintenance, "_identity_problem", problem_inside_transaction)
    assert _migrate(env) == 1
    audit = _audit(env)
    assert audit["result"] == "FAILED_ROLLED_BACK" and "DATABASE_IDENTITY_CHANGED" in audit["error"]
    assert audit["history_after"] == list(V11) and _dark_status(env)["state"] == "absent" and len(seen) == 2


def test_h14_storage_that_changes_after_the_pre_lock_check_is_refused(stopped, monkeypatch):
    from backend import db_maintenance
    env = stopped
    record = _record(env)
    calls = {"n": 0}
    real = sc.classify_path

    def flips(path, adapter=None):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(_db(env))):
            calls["n"] += 1
            if calls["n"] >= 2:                   # the pre-lock check passed; the re-check sees a mapped share
                return sc.StorageVerdict(sc.NETWORK_MAPPED, "test: re-mapped", str(path))
        return real(path, adapter)
    with offsite_helpers.offsite_disk(env.tmp_path / "offsite_disk"):
        inner = sc.classify_path
        sc.classify_path = lambda p, a=None: flips(p, a) if os.path.normcase(os.path.abspath(p)) == \
            os.path.normcase(os.path.abspath(_db(env))) else inner(p, a)
        with registered_build(env.db):
            code = db_maintenance.main(["migrate-v12", "--authorize-v12", "--offsite-record", str(record),
                                        "--db", str(_db(env))])
    assert code == 1 and _audit(env)["result"] == "DATABASE_IDENTITY_CHANGED" and _history(env)[-1] == V11


def test_h15_the_transaction_guard_authorizes_nothing_on_its_own(stopped):
    env = stopped
    _seed_application_data(env)
    called = []
    conn = env.db.get_connection()
    try:
        with registered_build(env.db):
            with pytest.raises(env.db.DatabaseMigrationAuthorizationRequired):
                env.db.migrate(conn, v12_transaction_guard=lambda c: called.append(c))
    finally:
        conn.close()
    assert called == [] and _history(env)[-1] == V11


def test_h16_with_a_valid_authorization_the_guard_runs_once_inside_the_transaction(stopped):
    env = stopped
    states = []
    conn = env.db.get_connection()
    try:
        with registered_build(env.db):
            env.db.migrate(conn, v12_authorization=env.db.V12MigrationAuthorization(issued_by="test"),
                           v12_transaction_guard=lambda c: states.append(c.in_transaction))
    finally:
        conn.close()
    assert states == [True] and _history(env)[-1] == V12


def test_h17_a_local_backup_with_the_right_counts_but_a_changed_value_fails_its_verification(stopped, monkeypatch):
    env = stopped
    _seed_application_data(env)
    real = env.db.backup_database

    def altered(conn, destination):
        written = real(conn, destination)
        c = sqlite3.connect(written)
        try:
            c.execute("UPDATE instrument_cache SET name = 'ALTERED' WHERE rowid = (SELECT MIN(rowid) FROM "
                      "instrument_cache)")
            c.commit()
        finally:
            c.close()
        return written
    record = _record(env)
    monkeypatch.setattr(env.db, "backup_database", altered)
    assert _migrate(env, record) == 1
    audit = _audit(env)
    assert audit["result"] == "BACKUP_VERIFICATION_FAILED"
    assert any("logical content digest" in p for p in audit["backup"]["problems"])


# ================================================================ the in-progress marker

def test_h18_a_successful_migration_removes_its_marker_and_rebinds_the_installation(stopped):
    env = stopped
    assert _migrate(env) == 0
    audit = _audit(env)
    assert audit["result"] == "V12_APPLIED" and audit["in_progress_marker"] and not _marker(env).exists()
    record = json.loads(ist.record_file(_db(env)).read_text(encoding="utf-8"))
    assert record["last_known"]["schema_newest"] == list(V12) and record["last_known"]["by"] == "migrate-v12"
    assert ist.assess(_db(env)).state == ist.ESTABLISHED_VALID


def test_h19_a_committed_migration_that_fails_verification_keeps_the_marker_and_a_start_refuses(stopped, monkeypatch):
    from backend import db_maintenance
    env = stopped
    monkeypatch.setattr(db_maintenance, "_verify_after_v12", lambda *a, **k: ["simulated post-commit problem"])
    assert _migrate(env) == 4
    assert _audit(env)["result"] == "MIGRATED_VERIFICATION_FAILED" and _history(env)[-1] == V12
    marker = json.loads(_marker(env).read_text(encoding="utf-8"))
    assert marker["result"] == "MIGRATED_VERIFICATION_FAILED"
    with pytest.raises(env.db.DatabaseInstallationStateRefused, match="INTERRUPTED_OPERATION"):
        env.db.init_db()


def test_h20_a_rerun_over_its_own_unfinished_marker_supersedes_it_and_never_applies_twice(stopped):
    env = stopped
    _marker(env).write_text(json.dumps({"command": "migrate-v12", "simulated": "crash"}), encoding="utf-8")
    assert _migrate(env) == 0
    audit = _audit(env)
    assert audit["result"] == "V12_APPLIED" and audit["previous_markers"][0]["content"]["simulated"] == "crash"
    assert list(env.tmp_path.glob("test.db.v12_migration_in_progress.json.superseded_*"))
    assert not _marker(env).exists()
    assert _migrate(env) == 0 and _audit(env)["result"] == "ALREADY_APPLIED"
    conn = sqlite3.connect(_db(env))
    try:
        assert conn.execute("SELECT COUNT(*) FROM schema_version WHERE version = 12").fetchone()[0] == 1
    finally:
        conn.close()


def test_h21_an_unwritable_marker_location_refuses_before_anything_is_opened(stopped, monkeypatch):
    env = stopped
    record = _record(env)
    before = _state(env)
    monkeypatch.setattr(ist, "write_marker", lambda *a, **k: (_ for _ in ()).throw(PermissionError("read-only")))
    assert _migrate(env, record) == 1
    assert _audit(env)["result"] == "AUDIT_LOCATION_UNWRITABLE"
    _assert_untouched(env, before, allowed_new=(AUDIT,))


@pytest.mark.parametrize("failure", ["write", "read_back"])
def test_h22_an_audit_that_cannot_be_written_or_read_back_keeps_the_marker_and_reports_the_real_outcome(
        stopped, monkeypatch, capsys, failure):
    from backend import db_maintenance
    env = stopped
    if failure == "write":
        monkeypatch.setattr(db_maintenance, "_write_audit_file",
                            lambda *a: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    else:
        monkeypatch.setattr(db_maintenance, "_read_audit_file", lambda *a: {"result": "SOMETHING_ELSE"})
    assert _migrate(env) == 0                                     # the exit code is the migration's real outcome
    err = capsys.readouterr().err
    assert "AUDIT_WRITE_FAILED" in err and "result:" in err and "V12_APPLIED" in err
    assert _history(env)[-1] == V12 and _marker(env).exists()    # not 'finished' until it is evidenced
    with pytest.raises(env.db.DatabaseInstallationStateRefused):
        env.db.init_db()
