"""migrate-v12 failure and recovery paths not covered elsewhere (V12 final readiness, Phase G).

The rule under test: a committed v12 is never reported as rolled back; FAILED_ROLLED_BACK only when the history and
the v12 objects read back prove it; anything unconfirmable is STATE_UNKNOWN; a post-commit problem is distinct from
a pre-commit one; no retry applies migration 12 twice; the in-progress marker survives every outcome that needs a
human. Real crashes run in a child process (os._exit, the closest a test gets to power loss); every database is
env's temporary one at v11 (v11_database).
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import offsite_helpers
from schema_builds import V11, V12, registered_build
from test_dark_protection_schema import _seed_application_data
from test_schema_v12 import _dark_status, _history
from test_v12_authorization_gate import _audit, _corrupt_pages, _state, _assert_untouched, stopped  # noqa: F401

from backend import installation_state as ist

pytestmark = pytest.mark.v11_database
REPO = Path(__file__).resolve().parent.parent


def _db(env) -> Path:
    return Path(env.db.DB_PATH)


def _migrate(env) -> int:
    from backend import db_maintenance
    root = env.tmp_path / "offsite_disk"
    with offsite_helpers.offsite_disk(root):
        argv = offsite_helpers.migrate_v12_argv(_db(env), root=root)
        with registered_build(env.db):
            return db_maintenance.main(argv)


def _v12_rows(env) -> int:
    conn = sqlite3.connect(_db(env))
    try:
        return conn.execute("SELECT COUNT(*) FROM schema_version WHERE version = 12").fetchone()[0]
    finally:
        conn.close()


def _marker(env) -> Path:
    return ist.marker_file(_db(env), ist.MIGRATION_MARKER_SUFFIX)


# ================================================================ interrupts before the transaction

@pytest.mark.parametrize("target", ["backup", "offsite", "preflight"])
def test_g01_an_interrupt_before_the_transaction_changes_nothing_keeps_the_marker_and_a_rerun_applies_once(
        stopped, target):
    # The faults are scoped in their own MonkeyPatch context: monkeypatch.undo() would also undo env's DB_PATH.
    from backend import backup_verification, db_maintenance
    env = stopped
    _seed_application_data(env)
    record_root = env.tmp_path / "offsite_disk"
    with pytest.MonkeyPatch.context() as fault:
        interrupt = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())  # noqa: E731
        if target == "backup":
            fault.setattr(env.db, "backup_database", interrupt)
        elif target == "offsite":
            fault.setattr(backup_verification, "check_offsite_record", interrupt)
        else:
            fault.setattr(db_maintenance, "preflight_v12_counts", interrupt)
        with offsite_helpers.offsite_disk(record_root):      # (the record itself is made with the real backup)
            argv = offsite_helpers.migrate_v12_argv(_db(env), root=record_root)
            with registered_build(env.db):
                with pytest.raises(KeyboardInterrupt):
                    db_maintenance.main(argv)
    assert _audit(env)["result"] == "INCOMPLETE" and _marker(env).exists()      # needs a human / a re-run
    assert _history(env)[-1] == V11 and _dark_status(env)["state"] == "absent"
    with pytest.raises(env.db.DatabaseInstallationStateRefused, match="INTERRUPTED_OPERATION"):
        env.db.init_db()
    assert _migrate(env) == 0 and _audit(env)["result"] == "V12_APPLIED" and _v12_rows(env) == 1
    assert not _marker(env).exists()


# ================================================================ failures inside the transaction: proven rollbacks

@pytest.mark.parametrize("step", ["first_object", "alter_table", "unique_index"])
def test_g02_a_ddl_failure_at_any_step_is_rolled_back_and_proven_so(stopped, step):
    env = stopped
    _seed_application_data(env)
    with pytest.MonkeyPatch.context() as fault:
        if step == "first_object":
            objects = list(env.db._DARK_SCHEMA_OBJECTS)
            objects[0] = (objects[0][0], "CREATE TABLE broken (")               # a syntax error, first DDL
            fault.setattr(env.db, "_DARK_SCHEMA_OBJECTS", objects)
        elif step == "alter_table":
            fault.setattr(env.db, "_PROTECTION_STATE_COLUMN_DDL", "no_such_type(")
        else:
            fault.setattr(env.db, "_UNIQUE_GTT_INDEX_DDL",
                          "CREATE UNIQUE INDEX idx_trade_tickets_unique_gtt ON no_such_table(x)")
        assert _migrate(env) == 1
        audit = _audit(env)
        assert audit["result"] == "FAILED_ROLLED_BACK" and audit["history_after"] == list(V11)
    assert _history(env)[-1] == V11 and _dark_status(env)["state"] == "absent" and not _marker(env).exists()
    assert _migrate(env) == 0 and _v12_rows(env) == 1                           # the rerun applies it exactly once


def test_g03_a_partial_state_left_by_a_failed_rollback_is_state_unknown_never_rolled_back(stopped, monkeypatch):
    """migrate() raises AND a v12 object is left committed (as if the ROLLBACK had not taken effect): the history is
    still v11, but the objects are partial, so the result must be STATE_UNKNOWN (exit 4), never FAILED_ROLLED_BACK."""
    env = stopped
    real = env.db.migrate

    def leaves_partial(conn, **kw):
        name, ddl = env.db._DARK_SCHEMA_OBJECTS[0]
        conn.execute(ddl)
        conn.commit()
        raise sqlite3.OperationalError("simulated: ROLLBACK failed (disk I/O error)")
    monkeypatch.setattr(env.db, "migrate", leaves_partial)
    assert _migrate(env) == 4
    audit = _audit(env)
    assert audit["result"] == "STATE_UNKNOWN" and "partial" in json.dumps(audit["verification"])
    assert _marker(env).exists()
    monkeypatch.setattr(env.db, "migrate", real)


# ================================================================ after COMMIT: never "rolled back"

@pytest.mark.parametrize("error", [OSError("disk I/O error after commit"), sqlite3.OperationalError("optimize"),
                                   RuntimeError("anything")])
def test_g04_any_error_after_commit_is_migrated_verification_failed_never_rolled_back(stopped, monkeypatch, error):
    env = stopped
    real = env.db.migrate

    def commits_then_raises(conn, **kw):
        real(conn, **kw)
        raise error
    monkeypatch.setattr(env.db, "migrate", commits_then_raises)
    assert _migrate(env) == 4
    audit = _audit(env)
    assert audit["result"] == "MIGRATED_VERIFICATION_FAILED" and audit["history_after"] == list(V12)
    assert "COMMITTED" in audit["detail"] and _marker(env).exists()
    monkeypatch.setattr(env.db, "migrate", real)
    assert _migrate(env) == 0 and _audit(env)["result"] == "ALREADY_APPLIED" and _v12_rows(env) == 1
    assert not _marker(env).exists()


def test_g05_a_real_post_commit_defect_is_detected_by_the_verification_itself(stopped, monkeypatch):
    """Nothing in the verification is patched: the committed database really lacks the unique GTT index."""
    env = stopped
    real = env.db.migrate

    def commits_then_breaks(conn, **kw):
        applied = real(conn, **kw)
        conn.execute("DROP INDEX idx_trade_tickets_unique_gtt")
        conn.commit()
        return applied
    monkeypatch.setattr(env.db, "migrate", commits_then_breaks)
    assert _migrate(env) == 4
    audit = _audit(env)
    assert audit["result"] == "MIGRATED_VERIFICATION_FAILED" and audit["verification"]["problems"]
    assert _marker(env).exists()


# ================================================================ power loss (process killed) before / after COMMIT

_CHILD = r"""
import os, sys
sys.path.insert(0, {tests!r}); sys.path.insert(0, {repo!r})
import real_data_guard; real_data_guard.install(); real_data_guard.neutralise_dotenv(); real_data_guard.scrub_kite_environment()
os.environ["KITE_TERMINAL_ENCRYPTION_KEY"] = {key!r}
from pathlib import Path
from backend import crypto, db, db_maintenance
crypto.ENV_PATH = Path({env_path!r}); db.DB_PATH = Path({db_path!r})
import offsite_helpers
if {when!r} == "before_commit":
    def crash(name):
        if name == "v12.in_transaction":
            sys.stdout.write("CRASHING\n"); sys.stdout.flush(); os._exit(137)
    db_maintenance._checkpoint = crash
else:
    real = db.migrate
    def crash_after(conn, **kw):
        real(conn, **kw)
        sys.stdout.write("CRASHING\n"); sys.stdout.flush(); os._exit(137)
    db.migrate = crash_after
with offsite_helpers.offsite_disk(Path({db_path!r}).parent / "offsite_disk"):
    sys.exit(db_maintenance.main(offsite_helpers.migrate_v12_argv(Path({db_path!r}))))
"""


def _killed(env, when: str) -> subprocess.CompletedProcess:
    code = _CHILD.format(tests=str(REPO / "tests"), repo=str(REPO), key=os.environ["KITE_TERMINAL_ENCRYPTION_KEY"],
                         env_path=str(env.tmp_path / ".env"), db_path=str(_db(env)), when=when)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=240,
                          cwd=str(env.tmp_path))


def test_g06_power_loss_inside_the_transaction_leaves_v11_the_marker_and_a_start_refuses(stopped):
    env = stopped
    _seed_application_data(env)
    out = _killed(env, "before_commit")
    assert out.returncode == 137 and "CRASHING" in out.stdout, out.stderr[-3000:]
    assert _history(env)[-1] == V11 and _dark_status(env)["state"] == "absent"
    assert _marker(env).exists() and not list(env.tmp_path.glob("test.db.v12_migration_audit_*"))
    with pytest.raises(env.db.DatabaseInstallationStateRefused, match="INTERRUPTED_OPERATION"):
        env.db.init_db()
    assert _migrate(env) == 0 and _v12_rows(env) == 1 and not _marker(env).exists()


def test_g07_power_loss_right_after_commit_leaves_v12_the_marker_and_a_rerun_verifies_it(stopped):
    env = stopped
    _seed_application_data(env)
    out = _killed(env, "after_commit")
    assert out.returncode == 137 and "CRASHING" in out.stdout, out.stderr[-3000:]
    assert _history(env)[-1] == V12 and _marker(env).exists()
    with pytest.raises(env.db.DatabaseInstallationStateRefused, match="INTERRUPTED_OPERATION"):
        env.db.init_db()                                           # never silently used unverified
    assert _migrate(env) == 0 and _audit(env)["result"] == "ALREADY_APPLIED" and _v12_rows(env) == 1
    assert not _marker(env).exists()
    env.db.init_db()                                               # resolved: it starts


# ================================================================ target integrity: real corruption

def test_g08_a_target_with_real_page_corruption_is_refused_before_any_write(stopped, capsys):
    env = stopped
    _seed_application_data(env)
    conn = sqlite3.connect(_db(env))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    _corrupt_pages(_db(env))
    before = _db(env).read_bytes()
    code = _migrate_or_refused(env)
    assert code == 1 and _db(env).read_bytes() == before
    assert not list(env.tmp_path.glob("test.db.pre_v12_authorized_*"))


def _migrate_or_refused(env) -> int:
    """The record cannot be made from a corrupt database either; the command itself must refuse."""
    from backend import db_maintenance
    root = env.tmp_path / "offsite_disk"
    root.mkdir(exist_ok=True)
    fake_record = root / "unusable.json"
    fake_record.write_text("{}", encoding="utf-8")
    with offsite_helpers.offsite_disk(root):
        with registered_build(env.db):
            return db_maintenance.main(["migrate-v12", "--authorize-v12", "--offsite-record", str(fake_record),
                                        "--db", str(_db(env))])


# ================================================================ audit failures of rollback and restore

def test_g09_a_rollback_whose_audit_cannot_be_written_reports_the_real_outcome(stopped, monkeypatch, capsys):
    from backend import db_maintenance
    env = stopped
    assert _migrate(env) == 0
    monkeypatch.setattr(db_maintenance, "_write_audit_file", lambda *a: (_ for _ in ()).throw(OSError(28, "full")))
    with registered_build(env.db):
        assert db_maintenance.main(["rollback-schema-v12", "--confirm", "--db", str(_db(env))]) == 0
    err = capsys.readouterr().err
    assert "AUDIT_WRITE_FAILED" in err and "ROLLED_BACK" in err and _history(env)[-1] == V11


def test_g10_a_restore_whose_audit_cannot_be_written_keeps_its_marker(stopped, monkeypatch, capsys):
    from backend import db_maintenance
    env = stopped
    assert db_maintenance.main(["backup", "--db", str(_db(env))]) == 0
    backup = sorted(env.tmp_path.glob("test.db.manual_*_bak"))[-1]
    monkeypatch.setattr(db_maintenance, "_write_audit_file", lambda *a: (_ for _ in ()).throw(OSError(28, "full")))
    assert db_maintenance.main(["restore-backup", "--backup", str(backup), "--confirm", "--db", str(_db(env))]) == 0
    assert "AUDIT_WRITE_FAILED" in capsys.readouterr().err
    assert ist.marker_file(_db(env), ist.RESTORE_MARKER_SUFFIX).exists()   # unevidenced: a start refuses
    with pytest.raises(env.db.DatabaseInstallationStateRefused):
        env.db.init_db()


# ================================================================ the result code reflects the history read back

def test_g11_every_exit_code_matches_the_history_actually_read_back(stopped):
    env = stopped
    outcomes = []
    real = env.db.migrate
    with pytest.MonkeyPatch.context() as fault:
        fault.setattr(env.db, "_rebuild_ticket_events",
                      lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated DDL failure")))
        outcomes.append((_migrate(env), _audit(env)["result"], _history(env)[-1]))
    outcomes.append((_migrate(env), _audit(env)["result"], _history(env)[-1]))
    outcomes.append((_migrate(env), _audit(env)["result"], _history(env)[-1]))
    assert outcomes == [(1, "FAILED_ROLLED_BACK", V11), (0, "V12_APPLIED", V12), (0, "ALREADY_APPLIED", V12)]
    assert _v12_rows(env) == 1
    assert real is env.db.migrate
