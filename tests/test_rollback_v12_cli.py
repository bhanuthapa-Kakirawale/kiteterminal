"""`python -m backend.db_maintenance rollback-schema-v12 --confirm`: the written down-migration of v12 as a command
(docs/PHASE_3_V12_REGISTRATION_REMEDIATION_REPORT.md §4).

Runs on either build: env's temporary database is created at exactly v11 by the Phase 2 build (the v11_database
marker), brought to v12 the authorized way inside schema_builds.registered_build (a no-op on a build that registers
v12), and every command runs inside that build. Every database is a temporary file; the real account database, its
backups and .env are never opened (tests/real_data_guard.py). No broker is called (r12, r13)."""

import json
import os
import pathlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import offsite_helpers
import schema_builds
from conftest import release_write_authority
from schema_builds import V11, V12, phase2_build, registered_build
from test_dark_protection_schema import (_broker_read, _insert_op, _position, _resolve_manually,
                                         _seed_application_data)
from test_schema_v12 import EVENT_TYPES, V12_TABLES, _content, _dark_status, _history, _orphan_resolution, _schema
from test_v12_authorization_gate import (LOCK_CHILD, _assert_untouched, _checkpoint, _corrupt_header, _corrupt_pages,
                                         _drop_a_row, _lock_is_free, _sha, _state)

pytestmark = pytest.mark.v11_database

REPO = Path(__file__).resolve().parent.parent
AUDIT_PREFIX = "test.db.v12_rollback_audit_"
NOTHING_NEW = (AUDIT_PREFIX,)


# ---------------------------------------------------------------- helpers

def _cli(env, *argv) -> int:
    from backend import db_maintenance
    return db_maintenance.main(["rollback-schema-v12", *argv, "--db", str(env.db.DB_PATH)])


def _confirmed(env) -> int:
    with registered_build(env.db):
        return _cli(env, "--confirm")


def _audit(env) -> dict:
    audits = sorted(env.tmp_path.glob(f"{AUDIT_PREFIX}*.json"))
    assert audits, "no audit file"
    return json.loads(audits[-1].read_text(encoding="utf-8"))


def _exec(env, sql, *args):
    conn = env.db.get_connection()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _v11_content(path) -> dict:
    """The v11 view of a v12 database: no v12 table, no protection_state column, schema_version without row 12."""
    content = _content(path, skip=V12_TABLES, drop_protection_state=True)
    content["schema_version"] = [r for r in content["schema_version"] if not r.startswith("(12,")]
    return content


@pytest.fixture
def v12(env):
    """The application stopped (env gives up the instance lock), application data seeded, then v12 applied the
    authorized way. Returns env."""
    release_write_authority()
    env.ids = _seed_application_data(env)
    env.read = _broker_read(env)
    schema_builds.to_v12(env.db)
    assert _history(env)[-1] == V12
    return env


@pytest.fixture
def held_by_another_process(v12):
    child = subprocess.Popen([sys.executable, str(LOCK_CHILD), "hold", json.dumps({"db": str(v12.db.DB_PATH)})],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "LOCKED"
    yield v12
    child.stdin.close()
    child.wait(timeout=30)


def _assert_still_v12(env, before, allowed_new=NOTHING_NEW):
    _assert_untouched(env, before, allowed_new=allowed_new)
    assert _history(env)[-1] == V12
    conn = env.db.get_connection()
    try:
        assert env.db.v12_schema_problems(conn, empty_journal=False) == []
    finally:
        conn.close()


# ================================================================ argument and lock refusals

def test_r01_without_confirm_it_is_refused_before_any_filesystem_access(v12, capsys):
    from backend import db_maintenance, instance_lock
    before = _state(v12)

    def no_filesystem(*args, **kwargs):
        raise AssertionError("filesystem accessed before the --confirm check")

    with registered_build(v12.db), pytest.MonkeyPatch.context() as mp:   # its own context: env's patches stay
        mp.setattr(pathlib.Path, "exists", no_filesystem)
        mp.setattr(pathlib.Path, "stat", no_filesystem)
        mp.setattr(sqlite3, "connect", no_filesystem)
        mp.setattr(instance_lock, "held_for_maintenance", no_filesystem)
        mp.setattr(db_maintenance, "_connect", no_filesystem)
        code = _cli(v12)
    assert code == 2
    assert "needs --confirm" in capsys.readouterr().err
    _assert_still_v12(v12, before, allowed_new=())          # no audit file either: nothing was opened


def test_r02_it_refuses_while_the_application_holds_the_lock(held_by_another_process, capsys):
    env = held_by_another_process
    before = _state(env)
    assert _confirmed(env) == 3
    assert "ANOTHER_INSTANCE_RUNNING" in capsys.readouterr().err
    _assert_still_v12(env, before, allowed_new=())          # no backup, no audit, not a byte


def test_r02b_rollback_schema_v12_is_a_lock_taking_writing_command():
    from backend import db_maintenance
    assert "rollback-schema-v12" in db_maintenance.WRITING_COMMANDS


# ================================================================ success: row-by-row preservation

def test_r03_a_rollback_keeps_every_v11_row_and_is_backed_up_verified_and_audited(v12, capsys):
    env = v12
    path = env.db.DB_PATH
    _checkpoint(path)
    v11_view, schema_before = _v11_content(path), _schema(path)
    assert _confirmed(env) == 0
    assert "ROLLED_BACK" in capsys.readouterr().out
    assert _history(env)[-1] == V11 and _dark_status(env)["state"] == "absent"
    assert _content(path) == v11_view                            # every row of every table, sqlite_sequence included
    assert set(schema_before) - set(_schema(path)) == {
        "protection_operations", "protection_operation_resolutions", "idx_protection_ops_one_open_change",
        "idx_protection_ops_one_open_cancel", "idx_trade_tickets_unique_gtt"}
    new_event = _exec(env, "INSERT INTO ticket_events (ticket_id, event_type) VALUES (?, 'modified')", env.ids["buy"])
    assert new_event == env.ids["gone_event"] + 1                # the AUTOINCREMENT high-water mark survived
    audit = _audit(env)
    assert audit["result"] == "ROLLED_BACK" and audit["exit_code"] == 0
    assert audit["history_before"] == list(V12) and audit["history_after"] == list(V11)
    assert audit["verification"]["problems"] == [] and audit["verification"]["integrity_check"] == ["ok"]
    assert audit["disk"]["ok"] is True
    [backup] = sorted(env.tmp_path.glob("test.db.pre_rollback_v12_authorized_*_bak"))
    assert audit["backup"]["path"] == str(backup) and audit["backup"]["sha256"] == _sha(backup)
    assert audit["backup"]["problems"] == []
    assert _history_of(backup)[-1] == V12                        # the v12 image, kept
    [own] = audit["rollback_backups"]                            # db.rollback_schema_v12's own backup
    assert Path(own["path"]).name.startswith("test.db.pre_rollback_v12_bak")
    assert _lock_is_free(path)
    with phase2_build(env.db):                                   # the Phase 2 build accepts it again
        env.db.init_db()
        assert env.db.last_init["applied_migrations"] == []


def _history_of(path) -> list:
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        return [tuple(r) for r in conn.execute("SELECT version, migration_name FROM schema_version ORDER BY version")]
    finally:
        conn.close()


# ================================================================ backup refusals

def test_r04_a_backup_failure_prevents_the_rollback(v12, monkeypatch):
    before = _state(v12)

    def failing(conn, destination):
        raise OSError("simulated: disk full")

    monkeypatch.setattr(v12.db, "backup_database", failing)
    assert _confirmed(v12) == 1
    audit = _audit(v12)
    assert audit["result"] == "BACKUP_FAILED" and "disk full" in audit["error"]
    _assert_still_v12(v12, before)
    assert _lock_is_free(v12.db.DB_PATH)


@pytest.mark.parametrize("damage", [_corrupt_header, _corrupt_pages, _drop_a_row],
                         ids=["header_destroyed", "pages_corrupted", "valid_but_not_the_target"])
def test_r05_a_backup_that_fails_its_independent_verification_prevents_the_rollback(v12, monkeypatch, damage):
    before = _state(v12)
    real = v12.db.backup_database

    def damaged(conn, destination):
        written = real(conn, destination)
        damage(written)
        return written

    monkeypatch.setattr(v12.db, "backup_database", damaged)
    assert _confirmed(v12) == 1
    audit = _audit(v12)
    assert audit["result"] == "BACKUP_VERIFICATION_FAILED" and "nothing was rolled back" in audit["detail"]
    _assert_still_v12(v12, before, allowed_new=("test.db.pre_rollback_v12_authorized_", AUDIT_PREFIX))
    assert not list(v12.tmp_path.glob("test.db.pre_rollback_v12_bak*"))   # db.rollback_schema_v12 never reached


def test_r05b_an_unreadable_backup_is_a_verification_failure(v12, monkeypatch):
    from backend import db_maintenance
    before = _state(v12)
    monkeypatch.setattr(db_maintenance, "_verify_v12_backup",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.DatabaseError("file is not a database")))
    assert _confirmed(v12) == 1
    audit = _audit(v12)
    assert audit["result"] == "BACKUP_VERIFICATION_FAILED" and "could not be verified" in audit["detail"]
    _assert_still_v12(v12, before, allowed_new=("test.db.pre_rollback_v12_authorized_", AUDIT_PREFIX))


def test_r05c_the_rollbacks_own_backup_failing_is_backup_failed_not_a_rollback_failure(v12, monkeypatch):
    before = _state(v12)
    real = v12.db.backup_database

    def own_fails(conn, destination):
        if ".pre_rollback_v12_bak" in destination.name:          # db.rollback_schema_v12's own, not the command's
            raise OSError("simulated: second backup failed")
        return real(conn, destination)

    monkeypatch.setattr(v12.db, "backup_database", own_fails)
    assert _confirmed(v12) == 1
    audit = _audit(v12)
    assert audit["result"] == "BACKUP_FAILED" and "MigrationBackupFailed" in audit["error"]
    assert audit["history_after"] == list(V12)
    _assert_still_v12(v12, before, allowed_new=("test.db.pre_rollback_v12_authorized_", AUDIT_PREFIX))


def test_r05d_too_little_disk_space_refuses_before_any_backup(v12, monkeypatch):
    from backend import db_maintenance
    before = _state(v12)
    monkeypatch.setattr(db_maintenance.shutil, "disk_usage", lambda p: type("U", (), {"free": 1024})())
    assert _confirmed(v12) == 1
    audit = _audit(v12)
    assert audit["result"] == "DISK_SPACE_INSUFFICIENT" and audit["disk"]["ok"] is False
    assert audit["disk"]["needed_bytes"] > audit["disk"]["free_bytes"] == 1024
    _assert_still_v12(v12, before)


# ================================================================ held-data refusals

def _held_journal_row(env):
    _insert_op(env, _position(env), state="intent")


def _held_resolution(env):
    op = _insert_op(env, _position(env), kind="modify", state="needs_review")
    _resolve_manually(env, op, evidence_read_id=env.read)


def _held_orphan_resolution(env):
    _orphan_resolution(env)


def _held_protection_state(env):
    _exec(env, "UPDATE trade_tickets SET protection_state = 'PROTECTION_PENDING' WHERE id = ?", env.ids["buy"])


HELD = [_held_journal_row, _held_resolution, _held_orphan_resolution, _held_protection_state]


@pytest.mark.parametrize("hold", HELD, ids=["journal_row", "resolution", "orphan_resolution", "protection_state"])
def test_r06_any_held_protection_data_refuses_the_rollback(v12, hold):
    hold(v12)
    before = _state(v12)
    assert _confirmed(v12) == 1
    audit = _audit(v12)
    assert audit["result"] == "REFUSED_HELD_DATA" and any(audit["held_data"].values())
    _assert_still_v12(v12, before)                               # no backup taken, not a byte changed


@pytest.mark.parametrize("event_type", EVENT_TYPES)
def test_r06b_each_protection_event_type_refuses_the_rollback(v12, event_type):
    _exec(v12, "INSERT INTO ticket_events (ticket_id, event_type) VALUES (?, ?)", v12.ids["buy"], event_type)
    before = _state(v12)
    assert _confirmed(v12) == 1
    audit = _audit(v12)
    assert audit["result"] == "REFUSED_HELD_DATA" and audit["held_data"]["protection_* ticket_events"] == 1
    _assert_still_v12(v12, before)


def test_r07_data_written_after_the_checks_is_refused_inside_the_write_transaction(v12, monkeypatch):
    """The held-data check is repeated under the write lock: data that appears after every earlier check (here,
    while db.rollback_schema_v12 takes its own backup) is caught there and nothing is changed."""
    real = v12.db.backup_database

    def write_then_backup(conn, destination):
        if ".pre_rollback_v12_bak" in destination.name:
            _held_protection_state(v12)                           # another connection commits a protection_state
        return real(conn, destination)

    monkeypatch.setattr(v12.db, "backup_database", write_then_backup)
    assert _confirmed(v12) == 1
    audit = _audit(v12)
    assert audit["result"] == "ROLLBACK_REFUSED" and "gained data" in audit["error"]
    assert audit["history_after"] == list(V12)
    assert _history(v12)[-1] == V12
    conn = v12.db.get_connection()
    try:
        kept = conn.execute("SELECT protection_state FROM trade_tickets WHERE id = ?", (v12.ids["buy"],)).fetchone()
    finally:
        conn.close()
    assert kept[0] == "PROTECTION_PENDING"                        # the data was kept


# ================================================================ history refusals

def test_r08a_a_v11_database_is_refused(v11_env):
    release_write_authority()
    before = _state(v11_env)
    assert _confirmed(v11_env) == 1
    audit = _audit(v11_env)
    assert audit["result"] == "HISTORY_REFUSED" and audit["history_before"] == list(V11)
    _assert_untouched(v11_env, before, allowed_new=NOTHING_NEW)


def test_r08b_a_renamed_v12_row_is_refused(v12):
    _exec(v12, "UPDATE schema_version SET migration_name = 'renamed' WHERE version = 12")
    before = _state(v12)
    assert _confirmed(v12) == 1
    assert _audit(v12)["result"] == "HISTORY_REFUSED"
    _assert_untouched(v12, before, allowed_new=NOTHING_NEW)


def test_r08c_a_gap_in_the_history_is_refused(v12):
    _exec(v12, "DELETE FROM schema_version WHERE version = 5")
    before = _state(v12)
    assert _confirmed(v12) == 1
    assert _audit(v12)["result"] == "HISTORY_REFUSED"
    _assert_untouched(v12, before, allowed_new=NOTHING_NEW)


def test_r08d_a_build_that_does_not_register_v12_refuses(v12):
    before = _state(v12)
    with phase2_build(v12.db):
        assert _cli(v12, "--confirm") == 1
    assert _audit(v12)["result"] == "V12_NOT_REGISTERED"
    _assert_still_v12(v12, before)


def test_r08e_a_v13_build_refuses(v12):
    before = _state(v12)
    with registered_build(v12.db):
        with schema_builds.build(v12.db, v12.db.MIGRATIONS + [(13, "placeholder", lambda conn: None)]):
            assert _cli(v12, "--confirm") == 1
    assert _audit(v12)["result"] == "V12_NOT_REGISTERED"
    _assert_still_v12(v12, before)


def test_r08f_v12_objects_not_exactly_v12s_are_refused(v12):
    _exec(v12, "DROP INDEX idx_trade_tickets_unique_gtt")
    before = _state(v12)
    assert _confirmed(v12) == 1
    assert _audit(v12)["result"] == "V12_SCHEMA_REFUSED"
    _assert_untouched(v12, before, allowed_new=NOTHING_NEW)


def test_r08g_a_target_failing_integrity_is_refused(v12, monkeypatch):
    from backend import db_maintenance
    before = _state(v12)
    monkeypatch.setattr(db_maintenance, "_integrity", lambda conn: ["*** in database main *** simulated"])
    assert _confirmed(v12) == 1
    assert _audit(v12)["result"] == "TARGET_INTEGRITY_FAILED"
    _assert_still_v12(v12, before)


# ================================================================ interruption and post-COMMIT failures

@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_r09_an_interrupted_rollback_is_rolled_back_and_a_rerun_recovers(v12, monkeypatch, error):
    before = _state(v12)
    real = v12.db._r012_protection_journal

    def interrupted(conn):                                         # every v12 object dropped, then interrupted
        real(conn)
        raise error("simulated interruption")

    monkeypatch.setattr(v12.db, "_r012_protection_journal", interrupted)
    if error is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            _confirmed(v12)
    else:
        assert _confirmed(v12) == 1
    audit = _audit(v12)
    assert audit["result"] == "FAILED_ROLLED_BACK" and audit["history_after"] == list(V12)
    _assert_still_v12(v12, before, allowed_new=("test.db.pre_rollback_v12_authorized_", "test.db.pre_rollback_v12_bak",
                                                AUDIT_PREFIX))
    assert _lock_is_free(v12.db.DB_PATH)
    monkeypatch.setattr(v12.db, "_r012_protection_journal", real)  # by hand, not monkeypatch.undo()
    assert _confirmed(v12) == 0
    assert _history(v12)[-1] == V11


def test_r09b_a_failure_after_the_commit_is_never_reported_as_rolled_back(v12, monkeypatch):
    real = v12.db.rollback_schema_v12

    def committed_then_failed(conn):
        real(conn)
        raise RuntimeError("simulated failure after COMMIT")

    monkeypatch.setattr(v12.db, "rollback_schema_v12", committed_then_failed)
    assert _confirmed(v12) == 4
    audit = _audit(v12)
    assert audit["result"] == "ROLLBACK_VERIFICATION_FAILED" and audit["history_after"] == list(V11)
    assert "COMMITTED" in audit["detail"] and audit["exit_code"] == 4


def test_r09c_a_committed_rollback_that_does_not_verify_exits_4(v12, monkeypatch):
    from backend import db_maintenance
    monkeypatch.setattr(db_maintenance, "_verify_after_rollback_v12", lambda *a: ["simulated problem"])
    assert _confirmed(v12) == 4
    audit = _audit(v12)
    assert audit["result"] == "ROLLBACK_VERIFICATION_FAILED" and audit["verification"]["problems"] == ["simulated problem"]
    assert "restore-backup" in audit["detail"]


_ROLLBACK_CHILD = r"""
import json, os, sys
sys.path.insert(0, {tests!r}); sys.path.insert(0, {repo!r})
import real_data_guard; real_data_guard.install(); real_data_guard.neutralise_dotenv(); real_data_guard.scrub_kite_environment()
os.environ["KITE_TERMINAL_ENCRYPTION_KEY"] = {key!r}
from pathlib import Path
import kiteconnect
broker = []
kiteconnect.KiteConnect.__init__ = lambda self, *a, **k: broker.append("KiteConnect()")
kiteconnect.KiteConnect._request = lambda self, *a, **k: broker.append("request")
from backend import crypto, db, db_maintenance
crypto.ENV_PATH = Path({env_path!r}); db.DB_PATH = Path({db_path!r})
if len(db.MIGRATIONS) == 11:   # a build that does not register v12 yet: the exact final entry
    db.MIGRATIONS = db.MIGRATIONS + [(12, db._V12_NAME, db._migration_v12_protection_journal)]
    db.LATEST_SCHEMA_VERSION = 12
if {crash!r}:
    real = db._r012_protection_journal
    def crash(conn):
        real(conn)
        sys.stdout.write("CRASHING\n"); sys.stdout.flush()
        os._exit(137)
    db._r012_protection_journal = crash
code = db_maintenance.main(["rollback-schema-v12", "--confirm", "--db", {db_path!r}])
app_modules = sorted(m for m in sys.modules if m in ("backend.kite_client", "backend.execution", "backend.main",
    "backend.paper_broker", "backend.order_monitor", "backend.trailing_engine", "backend.reconciliation",
    "backend.automation", "backend.ticker"))
print("RESULT " + json.dumps({{"code": code, "broker": broker, "app_modules": app_modules}}))
"""


def _child(env, *, crash: bool) -> subprocess.CompletedProcess:
    code = _ROLLBACK_CHILD.format(tests=str(REPO / "tests"), repo=str(REPO),
                                  key=os.environ["KITE_TERMINAL_ENCRYPTION_KEY"], env_path=str(env.tmp_path / ".env"),
                                  db_path=str(env.db.DB_PATH), crash=crash)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180,
                          cwd=str(env.tmp_path))


def test_r10_a_process_killed_mid_rollback_leaves_v12_and_a_rerun_recovers(v12):
    before = _state(v12)
    out = _child(v12, crash=True)
    assert out.returncode == 137 and "CRASHING" in out.stdout, out.stderr[-2000:]
    _assert_still_v12(v12, before, allowed_new=("test.db.pre_rollback_v12_authorized_", "test.db.pre_rollback_v12_bak"))
    assert _lock_is_free(v12.db.DB_PATH)
    assert _confirmed(v12) == 0
    assert _history(v12)[-1] == V11


# ================================================================ the full cycle, and no broker

def test_r11_v12_rollback_then_v11_rollback_then_forward_again_keeps_every_row(v12, monkeypatch):
    from backend import db_maintenance
    env = v12
    path = str(env.db.DB_PATH)
    _checkpoint(env.db.DB_PATH)
    v11_view = _v11_content(env.db.DB_PATH)
    assert _confirmed(env) == 0                                                     # v12 -> v11
    assert db_maintenance.main(["rollback-schema-v11", "--confirm", "--db", path]) == 0   # v11 -> v10
    assert _history(env)[-1][0] == 10
    with phase2_build(env.db):
        assert db_maintenance.main(["migrate", "--db", path]) == 0                  # v10 -> v11, the Phase 2 build
    assert _history(env)[-1] == V11
    with registered_build(env.db):
        assert offsite_helpers.run_migrate_v12(Path(path)) == 0   # v11 -> v12 (with its off-machine backup record)
    assert _history(env)[-1] == V12
    after = _v11_content(env.db.DB_PATH)
    # The trading data is exactly as it was. What differs is by design: the v11 rollback drops the five Phase 2
    # coordination tables (their audit rows are lost, docs/PHASE_2_IMPLEMENTATION_REPORT.md) and v11 re-creates
    # them empty; the migration bookkeeping is re-applied with new times; the `migrate` start ran its invariants.
    phase2 = {name for name, _ in env.db.PHASE2_TABLES}
    skip = phase2 | {"schema_version", "sqlite_sequence", "sessions", "accounts"}

    def sequence(content):
        return [r for r in content["sqlite_sequence"] if not any(r.startswith(f"('{t}',") for t in phase2)]

    assert {k: v for k, v in after.items() if k not in skip} == {k: v for k, v in v11_view.items() if k not in skip}
    assert sequence(after) == sequence(v11_view)                     # every trading AUTOINCREMENT high-water mark
    assert [r.split(",")[0] for r in after["schema_version"]] == [r.split(",")[0] for r in v11_view["schema_version"]]


def test_r12_the_rollback_makes_no_broker_call_and_loads_no_trading_module(v12):
    out = _child(v12, crash=False)
    result = json.loads(out.stdout.split("RESULT ", 1)[1])
    assert result == {"code": 0, "broker": [], "app_modules": []}, out.stderr[-2000:]
    assert _history(v12)[-1] == V11


def test_r13_in_process_the_rollback_leaves_the_fake_broker_untouched(v12, monkeypatch):
    from backend import kite_client
    mark = len(v12.kite.calls)
    monkeypatch.setattr(kite_client, "get_kite", lambda *a, **k: pytest.fail("rollback-schema-v12 asked for a broker"))
    assert _confirmed(v12) == 0
    assert v12.kite.calls[mark:] == []
