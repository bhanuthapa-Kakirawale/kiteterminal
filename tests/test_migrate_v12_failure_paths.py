"""migrate-v12 failure paths the authorization-gate suite did not cover, and DD-6 / R2-4 freshness
(docs/PHASE_3_V12_REGISTRATION_REMEDIATION_REPORT.md §5, §7).

The result is decided from the migration history and the v12 objects actually read back after a failure, never
assumed: FAILED_ROLLED_BACK only for a database that is still exactly v11 with no v12 object; a committed v12 whose
call still raised is MIGRATED_VERIFICATION_FAILED (exit 4); migrate()'s own backup failing is BACKUP_FAILED; an
unconfirmable state is STATE_UNKNOWN (exit 4). Every database is env's temporary one, created at v11 by the Phase 2
build (v11_database); commands run inside schema_builds.registered_build."""

from pathlib import Path

import pytest

from schema_builds import V11, V12, registered_build
from test_dark_protection_schema import _position, _seed_application_data
from test_schema_v12 import _dark_status, _history
from test_v12_authorization_gate import _assert_untouched, _audit, _authorized, _state, stopped  # noqa: F401

pytestmark = pytest.mark.v11_database

AUDIT = "test.db.v12_migration_audit_"


def _migrate(env) -> int:
    with registered_build(env.db):
        return _authorized(env)


# ================================================================ refusals before anything is written

def test_f01_too_little_disk_space_refuses_before_any_backup(stopped, monkeypatch):
    from backend import db_maintenance
    env = stopped
    _seed_application_data(env)
    before = _state(env)
    monkeypatch.setattr(db_maintenance.shutil, "disk_usage", lambda p: type("U", (), {"free": 4096})())
    assert _migrate(env) == 1
    audit = _audit(env)
    assert audit["result"] == "DISK_SPACE_INSUFFICIENT" and audit["disk"]["ok"] is False
    size = env.db.DB_PATH.stat().st_size
    assert audit["disk"]["needed_bytes"] >= 3 * size + db_maintenance.DISK_HEADROOM_BYTES
    _assert_untouched(env, before, allowed_new=(AUDIT,))       # no backup was written
    assert _history(env)[-1] == V11


def test_f01b_the_space_needed_counts_a_non_empty_wal(stopped):
    from backend import db_maintenance
    env = stopped
    path = env.db.DB_PATH
    wal = Path(f"{path}-wal")
    size = path.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    assert db_maintenance._sqlite_size(path) == size


def test_f02_a_target_failing_integrity_is_refused_and_nothing_is_written(stopped, monkeypatch):
    from backend import db_maintenance
    env = stopped
    before = _state(env)
    monkeypatch.setattr(db_maintenance, "_integrity", lambda conn: ["*** in database main *** simulated"])
    assert _migrate(env) == 1
    audit = _audit(env)
    assert audit["result"] == "TARGET_INTEGRITY_FAILED" and "nothing was changed" in audit["detail"]
    _assert_untouched(env, before, allowed_new=(AUDIT,))


def test_f03_a_v12_database_that_does_not_verify_is_already_applied_verification_failed(stopped):
    env = stopped
    assert _migrate(env) == 0
    conn = env.db.get_connection()
    try:
        conn.execute("DROP INDEX idx_protection_ops_one_open_cancel")
        conn.commit()
    finally:
        conn.close()
    before = _state(env)
    assert _migrate(env) == 4
    audit = _audit(env)
    assert audit["result"] == "ALREADY_APPLIED_VERIFICATION_FAILED" and audit["exit_code"] == 4
    assert audit["verification"]["problems"]
    _assert_untouched(env, before, allowed_new=(AUDIT,))       # nothing was written on the repeat run


# ================================================================ migrate()'s own backup

def test_f04_migrates_own_backup_failing_is_backup_failed_never_failed_rolled_back(stopped, monkeypatch):
    env = stopped
    _seed_application_data(env)
    before = _state(env)
    real = env.db.backup_database

    def second_fails(conn, destination):
        if ".pre_schema_v12_bak" in destination.name:              # migrate()'s own, after the verified one
            raise OSError("simulated: the second backup failed")
        return real(conn, destination)

    monkeypatch.setattr(env.db, "backup_database", second_fails)
    assert _migrate(env) == 1
    audit = _audit(env)
    assert audit["result"] == "BACKUP_FAILED" and "MigrationBackupFailed" in audit["error"]
    assert audit["history_after"] == list(V11) and "before its transaction began" in audit["detail"]
    _assert_untouched(env, before, allowed_new=("test.db.pre_v12_authorized_", AUDIT))
    assert _dark_status(env)["state"] == "absent"


def test_f04b_migrate_raises_migration_backup_failed_a_database_migration_failed(env, monkeypatch):
    """The subclass keeps every existing caller's except DatabaseMigrationFailed working."""
    assert issubclass(env.db.MigrationBackupFailed, env.db.DatabaseMigrationFailed)
    monkeypatch.setattr(env.db, "backup_database", lambda c, d: (_ for _ in ()).throw(OSError("simulated")))
    conn = env.db.get_connection()
    try:
        with registered_build(env.db):
            with pytest.raises(env.db.MigrationBackupFailed, match="nothing was migrated"):
                env.db.migrate(conn, v12_authorization=env.db.V12MigrationAuthorization(issued_by="test"))
    finally:
        conn.close()
    assert _history(env)[-1] == V11


# ================================================================ after COMMIT

def test_f05_a_failure_after_commit_is_migrated_verification_failed_never_rolled_back(stopped, monkeypatch):
    env = stopped
    _seed_application_data(env)
    real = env.db.migrate

    def committed_then_failed(conn, **kwargs):
        real(conn, **kwargs)                                         # migration 12 COMMITTED
        raise RuntimeError("simulated: PRAGMA optimize failed")

    monkeypatch.setattr(env.db, "migrate", committed_then_failed)
    assert _migrate(env) == 4
    audit = _audit(env)
    assert audit["result"] == "MIGRATED_VERIFICATION_FAILED" and audit["exit_code"] == 4
    assert audit["history_after"] == list(V12) and "COMMITTED" in audit["detail"]
    assert audit["result"] != "FAILED_ROLLED_BACK"
    assert _history(env)[-1] == V12


def test_f06_an_interrupt_after_commit_is_reported_as_committed_and_re_raised(stopped, monkeypatch):
    env = stopped
    real = env.db.migrate

    def committed_then_interrupted(conn, **kwargs):
        real(conn, **kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(env.db, "migrate", committed_then_interrupted)
    with pytest.raises(KeyboardInterrupt):
        _migrate(env)
    audit = _audit(env)
    assert audit["result"] == "MIGRATED_VERIFICATION_FAILED" and audit["history_after"] == list(V12)


def test_f07_a_committed_migration_failing_its_checks_exits_4(stopped, monkeypatch):
    from backend import db_maintenance
    env = stopped
    monkeypatch.setattr(db_maintenance, "_verify_after_v12", lambda *a, **k: ["simulated problem"])
    assert _migrate(env) == 4
    audit = _audit(env)
    assert audit["result"] == "MIGRATED_VERIFICATION_FAILED" and audit["verification"]["problems"] == ["simulated problem"]
    assert "restore the verified backup" in audit["detail"] and audit["backup"]["path"] in audit["detail"]
    assert _history(env)[-1] == V12


def test_f08_an_unreadable_state_after_a_failure_is_state_unknown(stopped, monkeypatch):
    from backend import db_maintenance
    env = stopped
    monkeypatch.setattr(env.db, "migrate", lambda conn, **k: (_ for _ in ()).throw(RuntimeError("simulated")))
    monkeypatch.setattr(db_maintenance, "_post_failure_history", lambda path: None)
    assert _migrate(env) == 4
    audit = _audit(env)
    assert audit["result"] == "STATE_UNKNOWN" and audit["history_after"] == "unreadable"


def test_f09_v11_history_with_v12_objects_present_is_state_unknown_not_rolled_back(stopped, monkeypatch):
    env = stopped
    monkeypatch.setattr(env.db, "migrate", lambda conn, **k: (_ for _ in ()).throw(RuntimeError("simulated")))
    real = env.db.dark_protection_schema_status
    monkeypatch.setattr(env.db, "dark_protection_schema_status", lambda conn: {**real(conn), "state": "partial"})
    assert _migrate(env) == 4
    audit = _audit(env)
    assert audit["result"] == "STATE_UNKNOWN" and audit["history_after"] == list(V11)


def test_f10_a_genuine_rollback_is_still_failed_rolled_back(stopped, monkeypatch):
    env = stopped
    real = env.db._rebuild_ticket_events
    monkeypatch.setattr(env.db, "_rebuild_ticket_events",
                        lambda conn, sql: (real(conn, sql), (_ for _ in ()).throw(RuntimeError("simulated")))[1])
    assert _migrate(env) == 1
    audit = _audit(env)
    assert audit["result"] == "FAILED_ROLLED_BACK" and audit["history_after"] == list(V11)
    assert "verified: history v11, no v12 object" in audit["detail"]


# ================================================================ DD-6 / R2-4: fresh, on the target, re-checked inside

def test_f11_the_preflight_runs_on_the_migration_target_itself(stopped, monkeypatch):
    from backend import db_maintenance
    env = stopped
    seen = []
    real = db_maintenance.preflight_v12_counts

    def spy(conn):
        seen.append(Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve())
        return real(conn)

    monkeypatch.setattr(db_maintenance, "preflight_v12_counts", spy)
    assert _migrate(env) == 0
    assert seen == [Path(env.db.DB_PATH).resolve()]                 # not a backup, not an earlier snapshot


def test_f12_a_duplicate_gtt_written_after_the_preflight_is_refused_inside_the_transaction(stopped, monkeypatch):
    """DD-6 is repeated under the write lock (_m012_registered): a duplicate that appears after the preflight
    passed is still refused, nothing is changed and the database stays at v11."""
    from backend import db_maintenance
    env = stopped
    real = db_maintenance.preflight_v12_counts

    def passed_then_duplicated(conn):
        facts = real(conn)
        assert facts["duplicate_gtt_ids"] == []
        _position(env, kite_gtt_id=4242)                              # another connection commits two tickets
        _position(env, kite_gtt_id=4242)                              # sharing one GTT id
        return facts

    monkeypatch.setattr(db_maintenance, "preflight_v12_counts", passed_then_duplicated)
    # V12 final readiness, Phase E: the first in-transaction check to fire is now the content binding (any write
    # after the off-machine backup was verified changes the logical digest); f12b proves DD-6's own re-check.
    assert _migrate(env) == 1
    audit = _audit(env)
    assert audit["result"] == "FAILED_ROLLED_BACK" and "DATABASE_IDENTITY_CHANGED" in audit["error"]
    assert _history(env)[-1] == V11 and _dark_status(env)["state"] == "absent"


def test_f12b_without_the_content_guard_dd6_is_still_refused_inside_the_transaction(stopped, monkeypatch):
    """The same race with the content guard removed (db.migrate called without it): DD-6's own re-check under the
    write lock (_m012_registered) refuses the duplicate, nothing is changed and the database stays at v11."""
    from backend import db_maintenance
    env = stopped
    real = db_maintenance.preflight_v12_counts
    real_migrate = env.db.migrate

    def passed_then_duplicated(conn):
        facts = real(conn)
        assert facts["duplicate_gtt_ids"] == []
        _position(env, kite_gtt_id=4242)
        _position(env, kite_gtt_id=4242)
        return facts

    monkeypatch.setattr(db_maintenance, "preflight_v12_counts", passed_then_duplicated)
    monkeypatch.setattr(env.db, "migrate", lambda conn, **kw: real_migrate(conn, v12_authorization=kw["v12_authorization"]))
    assert _migrate(env) == 1
    audit = _audit(env)
    assert audit["result"] == "FAILED_ROLLED_BACK" and "4242" in audit["error"]
    assert "linked to more than one ticket" in audit["error"]
    assert _history(env)[-1] == V11 and _dark_status(env)["state"] == "absent"
