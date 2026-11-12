"""restore-backup fault injection (V12 final readiness, Phase F): the 20 scenarios of the readiness brief.

For every failure point: the original database is never silently destroyed, its sidecars stay with it, a stale WAL
can never replay into the restored file, the recovery state is observable (audit + marker), every failure leaves a
recoverable state and no false success is returned. Real crashes (os._exit in a child process at a named checkpoint),
real I/O faults (os.rename / the staging copy failing), temporary databases only (output/audits/.../
RESTORE_RECOVERY_MATRIX.md is the table of these outcomes).
"""

import errno
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import release_write_authority
from schema_builds import V11
from test_dark_protection_schema import _seed_application_data
from test_v12_authorization_gate import LOCK_CHILD, _corrupt_pages

from backend import backup_verification as bv
from backend import installation_state as ist

pytestmark = pytest.mark.v11_database
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def stopped(env):
    release_write_authority()
    return env


def _main(*argv):
    from backend import db_maintenance
    return db_maintenance.main(list(argv))


def _db(env) -> Path:
    return Path(env.db.DB_PATH)


def _digest(path: Path) -> str:
    """The content as SQLite sees it through the path's own sidecars (mode=ro reads the WAL)."""
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        return bv.logical_digest(conn)
    finally:
        conn.close()


def _offline_digest(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        return bv.logical_digest(conn)
    finally:
        conn.close()


def _make_backup(env) -> Path:
    assert _main("backup", "--db", str(_db(env))) == 0
    return sorted(env.tmp_path.glob("test.db.manual_*_bak"))[-1]


def _diverge(env) -> None:
    """The original moves on after the backup, so original and backup are told apart by content."""
    _seed_application_data(env)


def _restore(env, backup: Path) -> int:
    return _main("restore-backup", "--backup", str(backup), "--confirm", "--db", str(_db(env)))


def _audit(env) -> dict:
    audits = sorted(env.tmp_path.glob("test.db.restore_audit_*.json"))
    assert audits, "no restore audit"
    return json.loads(audits[-1].read_text(encoding="utf-8"))


def _originals(env) -> list[Path]:
    return sorted(env.tmp_path.glob("test.db.pre_restore_*_orig"))


def _marker(env) -> Path:
    return ist.marker_file(_db(env), ist.RESTORE_MARKER_SUFFIX)


def _no_sidecars(path: Path) -> bool:
    return not any(Path(f"{path}{s}").exists() for s in ("-wal", "-shm", "-journal"))


def _leave_wal(env, rows: int = 3) -> None:
    """A writer that dies without closing: committed rows only in <db>-wal, a -shm beside it (a crashed terminal)."""
    code = (f"import sqlite3, os\nc = sqlite3.connect({str(_db(env))!r})\nc.execute('PRAGMA wal_autocheckpoint=0')\n"
            f"for i in range({rows}):\n    c.execute(\"INSERT INTO instrument_cache (instrument_token, tradingsymbol, "
            f"exchange) VALUES (?, 'WALROW', 'NSE')\", (880000 + i,))\nc.commit()\nos._exit(0)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert Path(f"{_db(env)}-wal").stat().st_size > 0


_CHILD = r"""
import os, sys
sys.path.insert(0, {tests!r}); sys.path.insert(0, {repo!r})
import real_data_guard; real_data_guard.install(); real_data_guard.neutralise_dotenv(); real_data_guard.scrub_kite_environment()
from backend import db_maintenance
def crash(name):
    if name == {point!r}:
        sys.stdout.write("CRASHING at " + name + "\n"); sys.stdout.flush(); os._exit(137)
db_maintenance._checkpoint = crash
sys.exit(db_maintenance.main(["restore-backup", "--backup", {backup!r}, "--confirm", "--db", {db!r}]))
"""


def _crash_child(env, backup: Path, point: str) -> subprocess.CompletedProcess:
    code = _CHILD.format(tests=str(REPO / "tests"), repo=str(REPO), point=point, backup=str(backup),
                         db=str(_db(env)))
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180,
                          cwd=str(env.tmp_path))


# ================================================================ 1-3: the normal path, with and without sidecars

def test_r01_a_database_with_no_sidecars_is_restored_and_the_original_kept_whole(stopped):
    env = stopped
    backup = _make_backup(env)
    _diverge(env)
    conn = sqlite3.connect(_db(env))
    conn.execute("PRAGMA journal_mode=DELETE")                       # no -wal/-shm at all
    conn.close()
    assert _no_sidecars(_db(env))
    original = _digest(_db(env))
    assert _restore(env, backup) == 0 and _audit(env)["result"] == "RESTORED"
    assert _offline_digest(_db(env)) == _offline_digest(backup) and _no_sidecars(_db(env))
    (kept,) = _originals(env)
    assert _digest(kept) == original and _no_sidecars(kept) and not _marker(env).exists()


def test_r02_a_database_with_wal_and_shm_is_moved_aside_as_one_set_and_its_wal_never_replays(stopped):
    env = stopped
    backup = _make_backup(env)
    _diverge(env)
    _leave_wal(env)
    original = _digest(_db(env))                                     # includes the WAL-only rows
    assert _restore(env, backup) == 0
    assert _no_sidecars(_db(env))                                    # checked before any open of the restored file
    assert _digest(_db(env)) == _offline_digest(backup)              # a normal open: no stale WAL replayed
    (kept,) = _originals(env)
    assert Path(f"{kept}-wal").exists() and _digest(kept) == original                # the WAL went with its file


def test_r03_a_valid_backup_restores_byte_for_byte(stopped):
    env = stopped
    backup = _make_backup(env)
    _diverge(env)
    assert _restore(env, backup) == 0
    assert _db(env).read_bytes() == backup.read_bytes()


# ================================================================ 4-6: what is not a usable backup

def _assert_untouched(env, before_digest, before_files):
    assert _digest(_db(env)) == before_digest
    now = {p.name for p in env.tmp_path.iterdir()}
    assert {n for n in now - before_files if not n.startswith(("test.db.restore_audit_", "test.db-wal", "test.db-shm"))} \
        == set(), now - before_files


def test_r04_a_backup_failing_integrity_check_is_refused_before_anything_changes(stopped):
    env = stopped
    backup = _make_backup(env)
    _corrupt_pages(backup)
    before, files = _digest(_db(env)), {p.name for p in env.tmp_path.iterdir()}
    assert _restore(env, backup) == 1 and _audit(env)["result"] == "BACKUP_VERIFICATION_FAILED"
    _assert_untouched(env, before, files)


@pytest.mark.parametrize("change", ["future_version", "gap_in_history", "no_history"])
def test_r05_a_backup_with_a_schema_this_build_does_not_accept_is_refused(stopped, change):
    env = stopped
    backup = _make_backup(env)
    conn = sqlite3.connect(backup)
    try:
        if change == "future_version":
            conn.execute("INSERT INTO schema_version (version, migration_name, duration_ms) VALUES (99, 'future', 0)")
        elif change == "gap_in_history":
            conn.execute("DELETE FROM schema_version WHERE version = 5")
        else:
            conn.execute("DROP TABLE schema_version")
        conn.commit()
    finally:
        conn.close()
    before, files = _digest(_db(env)), {p.name for p in env.tmp_path.iterdir()}
    assert _restore(env, backup) == 1 and _audit(env)["result"] == "BACKUP_VERIFICATION_FAILED"
    _assert_untouched(env, before, files)


def test_r06_an_incomplete_staging_copy_is_detected_and_the_target_never_touched(stopped, monkeypatch):
    from backend import db_maintenance
    env = stopped
    backup = _make_backup(env)
    _diverge(env)

    def half_copy(source, staging):
        data = Path(source).read_bytes()
        with open(staging, "xb") as f:
            f.write(data[: len(data) // 2])
    monkeypatch.setattr(db_maintenance, "_copy_backup", half_copy)
    before, files = _digest(_db(env)), {p.name for p in env.tmp_path.iterdir()}
    assert _restore(env, backup) == 1 and _audit(env)["result"] == "RESTORE_REFUSED"
    _assert_untouched(env, before, files)
    assert not list(env.tmp_path.glob("test.db.restore_staging_*")) and not _marker(env).exists()


# ================================================================ 7-9, 11: crashes at every step (real process kills)

def test_r07_a_crash_before_the_original_is_moved_leaves_it_in_place_and_a_rerun_restores(stopped):
    env = stopped
    backup = _make_backup(env)
    _diverge(env)
    original = _digest(_db(env))
    out = _crash_child(env, backup, "restore.marker_written")
    assert out.returncode == 137 and "CRASHING" in out.stdout, out.stderr[-2000:]
    assert _digest(_db(env)) == original and _marker(env).exists() and _originals(env) == []
    with pytest.raises(env.db.DatabaseInstallationStateRefused, match="INTERRUPTED_OPERATION"):
        env.db.init_db()
    assert _restore(env, backup) == 0 and not _marker(env).exists()
    assert _audit(env)["previous_markers"]                             # the crashed run's marker kept as evidence
    (kept,) = _originals(env)
    assert _digest(kept) == original


def test_r08_a_crash_after_the_main_file_moved_but_before_its_wal_rejoins_them_on_the_rerun(stopped):
    env = stopped
    backup = _make_backup(env)
    _diverge(env)
    _leave_wal(env)
    original = _digest(_db(env))
    out = _crash_child(env, backup, "restore.moved_aside_main")
    assert out.returncode == 137, out.stderr[-2000:]
    (first,) = _originals(env)
    assert not _db(env).exists() and Path(f"{_db(env)}-wal").exists()      # the WAL was left under the db's name
    assert _marker(env).exists()
    with pytest.raises(env.db.DatabaseInstallationStateRefused, match="INTERRUPTED_OPERATION"):
        env.db.init_db()                                                   # never re-created as a fresh database
    assert _restore(env, backup) == 0
    audit = _audit(env)
    assert audit["result"] == "RESTORED" and audit["completed_interrupted_move"]
    assert _no_sidecars(_db(env))
    assert Path(f"{first}-wal").exists() and _digest(first) == original    # the WAL rejoined ITS main file
    assert _digest(_db(env)) == _offline_digest(backup)


def test_r09_a_crash_after_the_backup_was_copied_changes_nothing_but_a_staging_file(stopped):
    env = stopped
    backup = _make_backup(env)
    _diverge(env)
    original = _digest(_db(env))
    out = _crash_child(env, backup, "restore.copied_to_staging")
    assert out.returncode == 137, out.stderr[-2000:]
    assert _digest(_db(env)) == original and not _marker(env).exists() and _originals(env) == []
    assert list(env.tmp_path.glob("test.db.restore_staging_*"))             # the leftover is observable
    assert _restore(env, backup) == 0


def test_r11_a_crash_after_the_staging_copy_is_in_place_keeps_the_original_aside_and_the_marker(stopped):
    env = stopped
    backup = _make_backup(env)
    _diverge(env)
    _leave_wal(env)
    original = _digest(_db(env))
    out = _crash_child(env, backup, "restore.staging_in_place")
    assert out.returncode == 137, out.stderr[-2000:]
    (first,) = _originals(env)
    assert _digest(first) == original and _marker(env).exists()
    marker = json.loads(_marker(env).read_text(encoding="utf-8"))
    assert marker["original_set_moved_aside_as"] == str(first)              # the operator knows where it is
    assert _restore(env, backup) == 0                                       # a rerun ends in a verified state
    assert _digest(first) == original and _digest(_db(env)) == _offline_digest(backup)


# ================================================================ 10: verification fails after the restore

def test_r10_a_restore_that_does_not_verify_puts_the_original_set_back_with_its_wal(stopped, monkeypatch):
    from backend import db_maintenance
    env = stopped
    backup = _make_backup(env)
    _diverge(env)
    _leave_wal(env)
    original = _digest(_db(env))
    real = db_maintenance._inspect_offline
    monkeypatch.setattr(db_maintenance, "_inspect_offline",
                        lambda p: real(p) if Path(p) != _db(env) else dict(real(p), integrity_check=["simulated"]))
    assert _restore(env, backup) == 4
    audit = _audit(env)
    assert audit["result"] == "RESTORE_VERIFICATION_FAILED" and audit["recovery"]["original_restored"]
    assert Path(f"{_db(env)}-wal").exists() and _digest(_db(env)) == original and not _marker(env).exists()
    assert list(env.tmp_path.glob("test.db.failed_restore_*"))


# ================================================================ 12-15: I/O faults

@pytest.mark.parametrize("error", [OSError(errno.ENOSPC, "No space left on device"), PermissionError(13, "denied")])
def test_r12_r14_a_staging_copy_that_fails_disk_full_or_denied_never_touches_the_target(stopped, monkeypatch, error):
    from backend import db_maintenance
    env = stopped
    backup = _make_backup(env)
    _diverge(env)

    def failing(source, staging):
        with open(staging, "xb") as f:
            f.write(b"partial")
        raise error
    monkeypatch.setattr(db_maintenance, "_copy_backup", failing)
    before, files = _digest(_db(env)), {p.name for p in env.tmp_path.iterdir()}
    assert _restore(env, backup) == 1
    audit = _audit(env)
    assert audit["result"] == "RESTORE_REFUSED" and type(error).__name__ in audit["error"]
    _assert_untouched(env, before, files)
    assert not list(env.tmp_path.glob("test.db.restore_staging_*"))


@pytest.mark.parametrize("fail_on", [1, 2])
def test_r13_permission_denied_while_moving_the_set_aside_moves_everything_back(stopped, monkeypatch, fail_on):
    """fail_on 1: the main file cannot be moved; 2: the main moved, its -wal cannot (moved back, nothing changed)."""
    env = stopped
    backup = _make_backup(env)
    _diverge(env)
    _leave_wal(env)
    original = _digest(_db(env))
    real = os.rename
    calls = {"n": 0}

    def rename(src, dst, *a, **k):
        if ".pre_restore_" in str(dst):
            calls["n"] += 1
            if calls["n"] == fail_on:
                raise PermissionError(13, "simulated: in use", str(src))
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "rename", rename)
    assert _restore(env, backup) == 1
    assert _audit(env)["result"] == "RESTORE_IN_USE"
    monkeypatch.setattr(os, "rename", real)
    assert _digest(_db(env)) == original and Path(f"{_db(env)}-wal").exists() and _originals(env) == []
    assert not _marker(env).exists()


def test_r15_a_destination_on_an_unavailable_drive_is_refused_and_a_missing_backup_too(stopped, capsys):
    import offsite_helpers
    from backend import storage_classification as sc
    env = stopped
    backup = _make_backup(env)
    unavailable = env.tmp_path / "gone"
    with offsite_helpers.offsite_disk(unavailable, category=sc.UNAVAILABLE, disks=()):
        assert _main("restore-backup", "--backup", str(backup), "--confirm",
                     "--db", str(unavailable / "test.db")) == 1
    assert "STORAGE_NOT_FIXED_LOCAL" in capsys.readouterr().err
    assert _restore(env, env.tmp_path / "no_such_backup_bak") == 1 and _audit(env)["result"] == "RESTORE_REFUSED"


# ================================================================ 16-17: stale sidecars and name conflicts

def test_r16_a_stale_hot_journal_beside_the_target_is_moved_aside_with_the_original(stopped):
    env = stopped
    backup = _make_backup(env)
    journal = Path(f"{_db(env)}-journal")
    journal.write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + b"\x02" * 504)
    assert _restore(env, backup) == 0
    (kept,) = _originals(env)
    assert Path(f"{kept}-journal").read_bytes() == journal.read_bytes() if journal.exists() else True
    assert _no_sidecars(_db(env)) and Path(f"{kept}-journal").exists()


@pytest.mark.parametrize("taken", ["-wal", "", "-shm", ".failed", ".staging"])
def test_r17_a_file_already_under_any_name_the_restore_would_use_refuses_it(stopped, monkeypatch, taken):
    """A stale <db>.pre_restore_<stamp>_orig-wal would otherwise pair a FOREIGN WAL with the moved-aside original."""
    from backend import db_maintenance
    env = stopped
    backup = _make_backup(env)
    _diverge(env)
    monkeypatch.setattr(db_maintenance, "_new_stamp", lambda: "20260101000000000000")
    names = db_maintenance._restore_names(_db(env), "20260101000000000000")
    target = {"": names["original"], "-wal": Path(f"{names['original']}-wal"), "-shm": Path(f"{names['original']}-shm"),
              ".failed": names["failed"], ".staging": names["staging"]}[taken]
    target.write_bytes(b"stale foreign file")
    before = _digest(_db(env))
    assert _restore(env, backup) == 1 and _audit(env)["result"] == "RESTORE_NAME_CONFLICT"
    assert _digest(_db(env)) == before and target.read_bytes() == b"stale foreign file"


def test_r17b_the_no_overwrite_rename_refuses_an_existing_destination(tmp_path):
    from backend import db_maintenance
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("a")
    b.write_text("b")
    with pytest.raises(FileExistsError):
        db_maintenance._rename_no_overwrite(a, b)
    assert a.read_text() == "a" and b.read_text() == "b"


# ================================================================ 18: the recovery itself fails

def test_r18_when_putting_the_original_back_also_fails_it_is_manual_recovery_and_nothing_is_lost(stopped, monkeypatch):
    from backend import db_maintenance
    env = stopped
    backup = _make_backup(env)
    _diverge(env)
    _leave_wal(env)
    original = _digest(_db(env))
    real_inspect, real_rename = db_maintenance._inspect_offline, db_maintenance._rename_no_overwrite
    monkeypatch.setattr(db_maintenance, "_inspect_offline",
                        lambda p: real_inspect(p) if Path(p) != _db(env) else dict(real_inspect(p), row_counts={}))

    def rename(src, dst):
        if ".pre_restore_" in str(src):                      # moving the original BACK fails
            raise PermissionError(13, "simulated", str(src))
        return real_rename(src, dst)
    monkeypatch.setattr(db_maintenance, "_rename_no_overwrite", rename)
    assert _restore(env, backup) == 4
    audit = _audit(env)
    assert audit["result"] == "RESTORE_FAILED_MANUAL_RECOVERY" and audit["recovery"]["not_moved_back"]
    assert audit["recovery"]["moved"]                        # every file name, for the operator
    (kept,) = _originals(env)
    assert Path(f"{kept}-wal").exists() and _digest(kept) == original      # complete and openable
    assert _marker(env).exists()                             # kept: a normal start refuses
    with pytest.raises(env.db.DatabaseInstallationStateRefused):
        env.db.init_db()


# ================================================================ 19-20: repeated and concurrent restores

def test_r19_a_repeated_restore_keeps_every_original_under_its_own_name(stopped):
    env = stopped
    backup = _make_backup(env)
    _diverge(env)
    original = _digest(_db(env))
    assert _restore(env, backup) == 0
    assert _restore(env, backup) == 0
    first, second = _originals(env)
    assert _digest(first) == original and _offline_digest(second) == _offline_digest(backup)
    assert _db(env).read_bytes() == backup.read_bytes()


def test_r20_a_concurrent_restore_is_refused_while_another_maintenance_command_holds_the_lock(stopped, monkeypatch):
    """In process: a second restore started while the first is moving files (both on the same database)."""
    from backend import db_maintenance
    env = stopped
    backup = _make_backup(env)
    _diverge(env)
    nested = []

    def second_restore(name):
        if name == "restore.after_move_aside" and not nested:
            nested.append(_restore(env, backup))
    monkeypatch.setattr(db_maintenance, "_checkpoint", second_restore)
    assert _restore(env, backup) == 0
    assert nested == [3]                                     # ANOTHER_INSTANCE_RUNNING: refused, nothing changed
    assert len(_originals(env)) == 1


def test_r20b_a_restore_is_refused_while_another_process_holds_the_lock(stopped):
    env = stopped
    backup = _make_backup(env)
    child = subprocess.Popen([sys.executable, str(LOCK_CHILD), "hold", json.dumps({"db": str(_db(env))})],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "LOCKED"
        before = _digest(_db(env))
        assert _restore(env, backup) == 3
        assert _digest(_db(env)) == before and _originals(env) == []
    finally:
        child.stdin.close()
        child.wait(timeout=30)


def test_r21_a_restored_database_is_rebound_and_starts_and_a_pending_migration_marker_is_resolved(stopped):
    env = stopped
    backup = _make_backup(env)
    _diverge(env)
    ist.marker_file(_db(env), ist.MIGRATION_MARKER_SUFFIX).write_text("{}")   # e.g. MIGRATED_VERIFICATION_FAILED
    assert _restore(env, backup) == 0
    assert not ist.marker_file(_db(env), ist.MIGRATION_MARKER_SUFFIX).exists()
    assert list(env.tmp_path.glob("test.db.v12_migration_in_progress.json.resolved_by_restore_*"))
    assert ist.assess(_db(env)).state == ist.ESTABLISHED_VALID
    with pytest.raises(env.db.DatabaseMigrationAuthorizationRequired):        # a v11 backup: the gate, as always
        env.db.init_db()
