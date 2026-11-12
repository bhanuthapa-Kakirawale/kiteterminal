"""The V12 authorization gate (docs/PHASE_3_V12_AUTHORIZATION_GATE_IMPLEMENTATION_REPORT.md).

Registering migration 12 and migrating the trading database are separate decisions. On a build that registers v12:
  - a normal start (main.on_startup -> init_db) never applies it to a database holding data: it refuses with
    DATABASE_MIGRATION_AUTHORIZATION_REQUIRED before any write, releases the instance lock and starts nothing;
  - `db_maintenance migrate` refuses it the same way;
  - only `db_maintenance migrate-v12 --authorize-v12` applies it: lock, history, verified backup, the DD-6 / R2-4
    preflight on the target, one transaction, verification, audit.

Runs unchanged on today's build (v12 prepared, NOT registered) and on the build that registers it
(tests/schema_builds.py): env's database is created at exactly v11 by the Phase 2 build (v11_database), the
registered build is test_schema_v12._registered (a no-op once v12 is registered), the build before registration is
schema_builds.phase2_build, and an autouse fixture checks every test leaves the build as it found it. Every database
here is a temporary one (env's, or a scratch file in tmp_path); the real account database, its backups and .env are
never opened (real_data_guard)."""

import asyncio
import hashlib
import inspect
import json
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import schema_builds
from conftest import release_write_authority
from schema_builds import phase2_build
from test_dark_protection_schema import _position, _seed_application_data
from test_schema_v12 import (V11, V12, V12_OBJECTS, V12_TABLES, _build, _content, _dark_status, _history,
                             _registered, _rollback_v11, _schema)

pytestmark = pytest.mark.v11_database

REPO = Path(__file__).resolve().parent.parent
LOCK_CHILD = REPO / "tests" / "instance_lock_child.py"
CODE = "DATABASE_MIGRATION_AUTHORIZATION_REQUIRED"


@pytest.fixture(autouse=True)
def _build_unchanged_by_the_test():
    from backend import db
    before = (list(db.MIGRATIONS), db.LATEST_SCHEMA_VERSION)
    yield
    assert (db.MIGRATIONS, db.LATEST_SCHEMA_VERSION) == before, "a test left the build changed"


# ---------------------------------------------------------------- helpers

def _checkpoint(path) -> None:
    """Folds the WAL into the main file, so the main file alone is the database (byte comparisons)."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _files(env) -> list[str]:
    """Every backup and audit file beside env's database."""
    return sorted(p.name for p in env.tmp_path.iterdir() if "_bak" in p.name or "audit" in p.name)


def _state(env) -> dict:
    path = env.db.DB_PATH
    _checkpoint(path)
    return {"sha": _sha(path), "schema": _schema(path), "content": _content(path), "files": _files(env)}


def _assert_untouched(env, before, *, allowed_new=()):
    path = env.db.DB_PATH
    assert _sha(path) == before["sha"], "the database file changed"
    assert _schema(path) == before["schema"] and _content(path) == before["content"]
    new = sorted(set(_files(env)) - set(before["files"]))
    assert all(name.startswith(allowed_new) for name in new), new


def _run(env, *argv) -> int:
    from backend import db_maintenance
    if "--authorize-v12" in argv and "--offsite-record" not in argv:
        # V12 final readiness, Phase E: migrate-v12 requires a fresh off-machine backup record of this exact content
        # (tests/offsite_helpers.py: the real `backup --dest` path; only the destination disk class is simulated).
        import offsite_helpers
        root = env.tmp_path / "offsite_disk"
        with offsite_helpers.offsite_disk(root):
            record = offsite_helpers.make_record(env.db.DB_PATH, root)
            return db_maintenance.main(["migrate-v12", *argv, "--offsite-record", str(record),
                                        "--db", str(env.db.DB_PATH)])
    return db_maintenance.main(["migrate-v12", *argv, "--db", str(env.db.DB_PATH)])


def _authorized(env) -> int:
    return _run(env, "--authorize-v12")


def _audit(env) -> dict:
    audits = sorted(env.tmp_path.glob("test.db.v12_migration_audit_*.json"))
    assert audits, "no audit file"
    return json.loads(audits[-1].read_text(encoding="utf-8"))


def _authorized_backups(env) -> list[Path]:
    return sorted(env.tmp_path.glob("test.db.pre_v12_authorized_*_bak"))


def _lock_is_free(db_path) -> bool:
    from backend import instance_lock
    fd = instance_lock.try_lock_file(instance_lock.lock_path_for(db_path))
    if fd is None:
        return False
    instance_lock.release_lock_file(fd)
    return True


def _conn(env):
    return env.db.get_connection()


def _migrate_with(env, authorization):
    conn = _conn(env)
    try:
        return env.db.migrate(conn, v12_authorization=authorization)
    finally:
        conn.close()


@pytest.fixture
def stopped(env):
    """The application stopped: env's own process gives up the instance lock (a CLI then takes it)."""
    release_write_authority()
    return env


@pytest.fixture
def startup_spies(env, monkeypatch):
    """main.on_startup with every step after init_db recorded instead of run."""
    from backend import crypto, instance_lock, kite_client, main, server, workers
    from backend.ticker import ticker_manager
    calls = []
    real_key = crypto.ensure_encryption_key
    monkeypatch.setattr(server, "enforce_runtime_policy", lambda *a, **k: None)
    monkeypatch.setattr(crypto, "ensure_encryption_key", lambda: (calls.append("ensure_encryption_key"), real_key())[1])
    monkeypatch.setattr(instance_lock, "takeover", lambda **k: calls.append("takeover"))
    monkeypatch.setattr(workers.supervisor, "start", lambda loops: calls.append(("loops", sorted(loops))))
    monkeypatch.setattr(workers.supervisor, "spawn", lambda *a, **k: calls.append("spawn"))
    monkeypatch.setattr(kite_client, "get_kite", lambda *a, **k: calls.append("get_kite"))
    monkeypatch.setattr(ticker_manager, "start", lambda *a, **k: calls.append("ticker"))
    monkeypatch.setattr(main.sector_mapping, "seed_known_symbols", lambda: calls.append("sector_seed"))
    release_write_authority()
    return calls


# ================================================================ startup: V12 pending

def test_01_v12_pending_at_startup_is_refused_with_the_authorization_code(env, startup_spies, caplog):
    from backend import main
    _seed_application_data(env)
    with _registered(env.db):
        with caplog.at_level(logging.INFO):
            with pytest.raises(env.db.DatabaseMigrationAuthorizationRequired) as refused:
                asyncio.run(main.on_startup())
    assert refused.value.error_code == CODE and str(refused.value).startswith(CODE)
    assert "migrate-v12 --authorize-v12" in str(refused.value) and "Nothing was changed" in str(refused.value)
    assert f"event=startup.refused code={CODE}" in caplog.text
    # Nothing after init_db ran, and init_db refused before minting a key.
    assert startup_spies == []


def test_02_startup_refusal_migrates_nothing_and_preserves_every_byte(env, startup_spies):
    from backend import main
    _seed_application_data(env)
    before = _state(env)
    with _registered(env.db):
        with pytest.raises(env.db.DatabaseMigrationAuthorizationRequired):
            asyncio.run(main.on_startup())
    _assert_untouched(env, before)                     # main file sha256, schema, every row; no backup written
    assert _history(env)[-1] == V11 and _dark_status(env)["state"] == "absent"
    assert not (env.tmp_path / ".env").exists()


def test_03_the_instance_lock_is_released_after_the_refusal(env, startup_spies):
    from backend import instance_lock, main
    with _registered(env.db):
        with pytest.raises(env.db.DatabaseMigrationAuthorizationRequired):
            asyncio.run(main.on_startup())
    assert instance_lock.status()["role"] == instance_lock.RELEASED
    assert not instance_lock.authority_ok()
    assert _lock_is_free(env.db.DB_PATH)


def test_04_init_db_and_migrate_both_refuse_and_write_nothing(env):
    _seed_application_data(env)
    before = _state(env)
    with _registered(env.db):
        with pytest.raises(env.db.DatabaseMigrationAuthorizationRequired, match=CODE):
            env.db.init_db()
        with pytest.raises(env.db.DatabaseMigrationAuthorizationRequired, match=CODE):
            _migrate_with(env, None)                    # migrate()'s own check: the second line of defence
    _assert_untouched(env, before)
    assert _history(env)[-1] == V11


def test_05_generic_db_maintenance_migrate_refuses_v12(stopped, capsys):
    from backend import db_maintenance
    env = stopped
    before = _state(env)
    with _registered(env.db):
        assert db_maintenance.main(["migrate", "--db", str(env.db.DB_PATH)]) == 1
    assert CODE in capsys.readouterr().err
    _assert_untouched(env, before)


def test_06_the_precheck_runs_before_anything_else_in_init_db():
    from backend import db
    source = inspect.getsource(db.init_db)
    body = source[source.index('"""') if '"""' in source else 0:]
    lines = [line.strip() for line in body.splitlines()[1:] if line.strip() and not line.strip().startswith("#")]
    # V12 final readiness, Phase C: the read-only installation-state check comes first, then this gate's read-only
    # precheck; both run before the key can be minted, the journal mode set, a backup taken or anything migrated.
    assert lines[:3] == ["assessment = installation_state.require_startup_ok(DB_PATH, fresh_install)",
                         "require_startup_migration_authorization(DB_PATH)",
                         "crypto.ensure_encryption_key()"]
    assert "v12_authorization" not in source and "V12MigrationAuthorization" not in source


# ================================================================ authorization is explicit and V12-only

@pytest.mark.parametrize("authorization", [
    None, True, "yes", 12, {"version": 12},
    SimpleNamespace(version=12, name="phase3_protection_journal", issued_by="lookalike"),
    "v13", "other_name", "blank_issuer", "subclass",
], ids=["none", "true", "string", "int", "dict", "lookalike", "version_13", "other_name", "blank_issuer", "subclass"])
def test_07_only_a_valid_v12_authorization_unlocks_migration_12(env, authorization):
    from backend import db
    if authorization == "v13":
        authorization = db.V12MigrationAuthorization(issued_by="t", version=13)
    elif authorization == "other_name":
        authorization = db.V12MigrationAuthorization(issued_by="t", name="something_else")
    elif authorization == "blank_issuer":
        authorization = db.V12MigrationAuthorization(issued_by="  ")
    elif authorization == "subclass":
        class Sub(db.V12MigrationAuthorization):
            pass
        authorization = Sub(issued_by="t")
    before = _state(env)
    with _registered(env.db):
        with pytest.raises(db.DatabaseMigrationAuthorizationRequired):
            _migrate_with(env, authorization)
    _assert_untouched(env, before)


def test_08_the_gate_is_keyed_on_version_12_only_and_no_generic_bypass_exists():
    from backend import db
    assert db._AUTHORIZATION_GATED_MIGRATIONS == {12: db._V12_NAME}
    # v12_transaction_guard (V12 final readiness, Phase E) only adds a check inside v12's transaction and is ignored
    # without a valid authorization (tests/test_migrate_v12_hardening.py proves it authorizes nothing).
    assert list(inspect.signature(db.migrate).parameters) == ["conn", "v12_authorization", "v12_transaction_guard"]
    assert "v12_authorization" not in inspect.signature(db.init_db).parameters
    # main.py never constructs or passes an authorization; db_maintenance is the only backend constructor.
    constructors = [p.relative_to(REPO).as_posix() for p in sorted((REPO / "backend").rglob("*.py"))
                    if p.name != "db.py" and "V12MigrationAuthorization(" in p.read_text(encoding="utf-8")]
    assert constructors == ["backend/db_maintenance.py"]
    assert "V12MigrationAuthorization" not in (REPO / "backend" / "main.py").read_text(encoding="utf-8")


def test_09_a_renamed_v12_entry_is_gated_all_the_same(env):
    before = _state(env)
    with _build(env.db, env.db.MIGRATIONS + [(12, "renamed_v12", env.db._m012_protection_journal)]):
        with pytest.raises(env.db.DatabaseMigrationAuthorizationRequired):
            env.db.init_db()
    _assert_untouched(env, before)


# ================================================================ migrate-v12: argument and lock refusals

def test_10_the_command_without_the_flag_is_rejected_before_any_filesystem_access(env, monkeypatch, capsys):
    from backend import db_maintenance
    target = env.tmp_path / "never_opened.db"
    touched = []
    real_exists = db_maintenance.Path.exists
    monkeypatch.setattr(db_maintenance.Path, "exists", lambda self: (touched.append(self), real_exists(self))[1])
    with _registered(env.db):
        assert db_maintenance.main(["migrate-v12", "--db", str(target)]) == 2
        assert db_maintenance.main(["migrate-v12"]) == 2               # nor with the default database
    assert "--authorize-v12" in capsys.readouterr().err
    assert touched == []
    assert not list(env.tmp_path.glob("never_opened.db*"))             # no lock file, no database


def test_11_the_flag_authorizes_nothing_but_migrate_v12(stopped, capsys):
    from backend import db_maintenance
    env = stopped
    before = _state(env)
    with _registered(env.db):
        for command in ("migrate", "status", "backup"):
            assert db_maintenance.main([command, "--authorize-v12", "--db", str(env.db.DB_PATH)]) == 2
    assert "migrate-v12 only" in capsys.readouterr().err
    _assert_untouched(env, before)


@pytest.fixture
def held_by_another_process(env):
    """The terminal running in another process: a child holds the database's instance lock."""
    release_write_authority()
    child = subprocess.Popen([sys.executable, str(LOCK_CHILD), "hold", json.dumps({"db": str(env.db.DB_PATH)})],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "LOCKED"
    yield env
    child.stdin.close()
    child.wait(timeout=30)


def test_12_the_command_refuses_while_the_application_runs(held_by_another_process, capsys):
    env = held_by_another_process
    before = _state(env)
    with _registered(env.db):
        assert _authorized(env) == 3
    assert "ANOTHER_INSTANCE_RUNNING" in capsys.readouterr().err
    _assert_untouched(env, before)                     # no backup, no audit, not a byte


# ================================================================ migrate-v12: backup refusals

def test_13_a_backup_failure_prevents_the_migration(stopped, monkeypatch):
    env = stopped
    _seed_application_data(env)
    before = _state(env)

    def failing(conn, destination):
        raise OSError("simulated: disk full")

    monkeypatch.setattr(env.db, "backup_database", failing)
    with _registered(env.db):
        assert _authorized(env) == 1
    audit = _audit(env)
    assert audit["result"] == "BACKUP_FAILED" and "disk full" in audit["error"] and audit["exit_code"] == 1
    _assert_untouched(env, before, allowed_new=("test.db.v12_migration_audit_",))
    assert _history(env)[-1] == V11 and _dark_status(env)["state"] == "absent"
    assert _lock_is_free(env.db.DB_PATH)


def _corrupt_header(path: Path):
    data = bytearray(path.read_bytes())
    data[0:100] = b"\xAB" * 100
    path.write_bytes(bytes(data))


def _corrupt_pages(path: Path):
    data = bytearray(path.read_bytes())
    page = int.from_bytes(data[16:18], "big") or 65536
    for start in range(page, len(data), page * 2):    # every other page after the first
        data[start:start + page] = b"\x5A" * min(page, len(data) - start)
    path.write_bytes(bytes(data))


def _drop_a_row(path: Path):
    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM ticket_events WHERE id = (SELECT MAX(id) FROM ticket_events)")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("damage", [_corrupt_header, _corrupt_pages, _drop_a_row],
                         ids=["header_destroyed", "pages_corrupted", "valid_but_not_the_target"])
def test_14_a_backup_that_fails_its_independent_verification_prevents_the_migration(stopped, monkeypatch, damage):
    env = stopped
    _seed_application_data(env)
    before = _state(env)
    real = env.db.backup_database

    def damaged(conn, destination):
        written = real(conn, destination)           # backup_database's own check passes...
        damage(written)                            # ...then the copy on disk is damaged
        return written

    monkeypatch.setattr(env.db, "backup_database", damaged)
    with _registered(env.db):
        assert _authorized(env) == 1
    audit = _audit(env)
    assert audit["result"] == "BACKUP_VERIFICATION_FAILED" and "nothing was migrated" in audit["detail"]
    _assert_untouched(env, before, allowed_new=("test.db.pre_v12_authorized_", "test.db.v12_migration_audit_"))
    assert not list(env.tmp_path.glob("test.db.pre_schema_v12_bak*"))   # migrate() never reached
    assert _history(env)[-1] == V11 and _dark_status(env)["state"] == "absent"


# ================================================================ migrate-v12: preflight refusals

def test_15_an_open_null_mode_buy_fails_the_preflight(stopped):
    env = stopped
    _seed_application_data(env)
    null_mode = _position(env, mode=None, symbol="INFY")
    before = _state(env)
    with _registered(env.db):
        assert _authorized(env) == 1
    audit = _audit(env)
    assert audit["result"] == "PREFLIGHT_FAILED"
    assert [r["id"] for r in audit["preflight"]["open_null_mode_buys"]] == [null_mode]
    assert audit["preflight"]["duplicate_gtt_ids"] == []
    _assert_untouched(env, before, allowed_new=("test.db.pre_v12_authorized_", "test.db.v12_migration_audit_"))
    assert _history(env)[-1] == V11 and _dark_status(env)["state"] == "absent"


def test_16_the_null_mode_check_is_repeated_inside_the_v12_transaction(stopped, monkeypatch):
    """Even if the preflight were bypassed, migrate() refuses an open NULL-mode BUY under the write lock."""
    from backend import db_maintenance
    env = stopped
    null_mode = _position(env, mode=None)
    before = _state(env)
    real = db_maintenance.preflight_v12_counts
    monkeypatch.setattr(db_maintenance, "preflight_v12_counts",
                        lambda conn: {**real(conn), "open_null_mode_buys": []})
    with _registered(env.db):
        assert _authorized(env) == 1
    audit = _audit(env)
    assert audit["result"] == "FAILED_ROLLED_BACK" and audit["history_after"] == list(V11)
    assert "DatabaseMigrationFailed" in audit["error"] and f"[{null_mode}]" in audit["error"]
    _assert_untouched(env, before, allowed_new=("test.db.pre_v12_authorized_", "test.db.pre_schema_v12_bak",
                                                "test.db.v12_migration_audit_"))


@pytest.mark.parametrize("gtt_id", [77, "PAPER-G-9"], ids=["live", "paper"])
def test_17_duplicate_gtt_ids_fail_the_preflight(stopped, gtt_id):
    env = stopped
    mode = "dry_run" if isinstance(gtt_id, str) else "live"
    _position(env, mode=mode, kite_gtt_id=gtt_id)
    _position(env, mode=mode, kite_gtt_id=gtt_id)
    _position(env, kite_gtt_id=5150)
    before = _state(env)
    with _registered(env.db):
        assert _authorized(env) == 1
    audit = _audit(env)
    assert audit["result"] == "PREFLIGHT_FAILED" and audit["preflight"]["duplicate_gtt_ids"] == [gtt_id]
    _assert_untouched(env, before, allowed_new=("test.db.pre_v12_authorized_", "test.db.v12_migration_audit_"))
    assert _history(env)[-1] == V11


# ================================================================ migrate-v12: success, repeat, recovery

def test_18_a_successful_migration_is_verified_backed_up_and_audited(stopped, capsys):
    env = stopped
    _seed_application_data(env)
    path = env.db.DB_PATH
    before = _state(env)
    with _registered(env.db):
        assert _authorized(env) == 0
        status = _dark_status(env)
        conn = _conn(env)
        try:
            assert env.db.schema_status(conn)["up_to_date"] is True
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            conn.close()
    out = capsys.readouterr().out
    assert "V12_APPLIED" in out and "audit_file:" in out
    assert _history(env)[-1] == V12 and status["state"] == "present" and status["unique_gtt_index"] is True
    assert set(_schema(path)) - set(before["schema"]) == V12_OBJECTS
    after = _content(path, skip=V12_TABLES, drop_protection_state=True)
    assert {k: v for k, v in after.items() if k != "schema_version"} == \
        {k: v for k, v in before["content"].items() if k != "schema_version"}
    audit = _audit(env)
    assert audit["result"] == "V12_APPLIED" and audit["exit_code"] == 0 and audit["applied_migrations"] == [12]
    assert audit["history_before"] == list(V11) and audit["history_after"] == list(V12)
    assert audit["verification"]["problems"] == [] and audit["verification"]["integrity_check"] == ["ok"]
    assert audit["preflight"]["duplicate_gtt_ids"] == [] and audit["preflight"]["open_null_mode_buys"] == []
    [backup] = _authorized_backups(env)
    assert audit["backup"]["path"] == str(backup) and audit["backup"]["sha256"] == _sha(backup)
    assert audit["backup"]["problems"] == [] and audit["backup"]["integrity_check"] == ["ok"]
    assert _schema(backup) == before["schema"] and _content(backup) == before["content"]   # the v11 image
    [migrate_backup] = audit["migrate_backups"]
    assert Path(migrate_backup["path"]).name.startswith("test.db.pre_schema_v12_bak")
    assert _content(Path(migrate_backup["path"])) == before["content"]
    assert _lock_is_free(path)


def test_19_a_repeat_run_changes_nothing_and_takes_no_backup(stopped, capsys):
    env = stopped
    with _registered(env.db):
        assert _authorized(env) == 0
        before = _state(env)
        assert _authorized(env) == 0
        assert _audit(env)["result"] == "ALREADY_APPLIED"
        assert _authorized(env) == 0
    _assert_untouched(env, before, allowed_new=("test.db.v12_migration_audit_",))
    assert len(_authorized_backups(env)) == 1


def test_20_an_interrupted_migration_rolls_back_and_a_rerun_recovers(stopped, monkeypatch):
    env = stopped
    _seed_application_data(env)
    before = _state(env)
    real = env.db._rebuild_ticket_events

    def interrupted(conn, sql):   # after both tables, both indexes, the column and the rebuild
        real(conn, sql)
        raise KeyboardInterrupt()

    monkeypatch.setattr(env.db, "_rebuild_ticket_events", interrupted)
    with _registered(env.db):
        with pytest.raises(KeyboardInterrupt):
            _authorized(env)
        audit = _audit(env)
        assert audit["result"] == "FAILED_ROLLED_BACK" and audit["history_after"] == list(V11)
        assert "KeyboardInterrupt" in audit["error"]
        _assert_untouched(env, before, allowed_new=("test.db.pre_v12_authorized_", "test.db.pre_schema_v12_bak",
                                                    "test.db.v12_migration_audit_"))
        assert _dark_status(env)["state"] == "absent" and _lock_is_free(env.db.DB_PATH)
        monkeypatch.setattr(env.db, "_rebuild_ticket_events", real)   # by hand, not monkeypatch.undo()
        assert _authorized(env) == 0                                   # recovery: the rerun applies it
    assert _audit(env)["result"] == "V12_APPLIED" and _history(env)[-1] == V12


_COMMAND_CHILD = r"""
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
    real = db._rebuild_ticket_events
    def crash(conn, sql):
        real(conn, sql)
        sys.stdout.write("CRASHING\n"); sys.stdout.flush()
        os._exit(137)
    db._rebuild_ticket_events = crash
import offsite_helpers   # the off-machine backup record migrate-v12 requires (only the disk class is simulated)
with offsite_helpers.offsite_disk(Path({db_path!r}).parent / "offsite_disk"):
    code = db_maintenance.main(offsite_helpers.migrate_v12_argv(Path({db_path!r})))
app_modules = sorted(m for m in sys.modules if m in ("backend.kite_client", "backend.execution", "backend.main",
    "backend.paper_broker", "backend.order_monitor", "backend.trailing_engine", "backend.reconciliation",
    "backend.automation", "backend.ticker"))
print("RESULT " + json.dumps({{"code": code, "broker": broker, "app_modules": app_modules}}))
"""


def _command_child(env, *, crash: bool) -> subprocess.CompletedProcess:
    code = _COMMAND_CHILD.format(tests=str(REPO / "tests"), repo=str(REPO),
                                 key=os.environ["KITE_TERMINAL_ENCRYPTION_KEY"], env_path=str(env.tmp_path / ".env"),
                                 db_path=str(env.db.DB_PATH), crash=crash)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180,
                          cwd=str(env.tmp_path))


def test_21_a_process_killed_mid_migration_leaves_v11_and_a_rerun_recovers(stopped):
    env = stopped
    _seed_application_data(env)
    before = _state(env)
    out = _command_child(env, crash=True)
    assert out.returncode == 137 and "CRASHING" in out.stdout, out.stderr[-2000:]
    _assert_untouched(env, before, allowed_new=("test.db.pre_v12_authorized_", "test.db.pre_schema_v12_bak"))
    assert _history(env)[-1] == V11 and _dark_status(env)["state"] == "absent"
    assert _lock_is_free(env.db.DB_PATH)                # the OS released the dead process's lock
    with _registered(env.db):
        assert _authorized(env) == 0
    assert _history(env)[-1] == V12 and _dark_status(env)["state"] == "present"


def test_22_the_migration_makes_no_broker_call_and_loads_no_trading_module(stopped, monkeypatch):
    env = stopped
    _seed_application_data(env)
    # In a fresh process: KiteConnect is never constructed or used, and no broker / trading module is imported.
    out = _command_child(env, crash=False)
    result = json.loads(out.stdout.split("RESULT ", 1)[1])
    assert result == {"code": 0, "broker": [], "app_modules": []}, out.stderr[-2000:]
    assert _history(env)[-1] == V12


def test_23_in_process_the_command_leaves_the_fake_broker_untouched(stopped, monkeypatch):
    from backend import kite_client
    env = stopped
    mark = len(env.kite.calls)
    monkeypatch.setattr(kite_client, "get_kite", lambda *a, **k: pytest.fail("migrate-v12 asked for a broker"))
    with _registered(env.db):
        assert _authorized(env) == 0
    assert env.kite.calls[mark:] == []


def test_24_db_maintenance_imports_no_broker_module():
    import ast
    tree = ast.parse((REPO / "backend" / "db_maintenance.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {f"{node.module}.{a.name}" for a in node.names}
    # V12 final readiness: three read-only, broker-free modules (storage classification, installation identity,
    # backup verification); test_23 proves no broker or trading module is imported, transitively, in a child.
    assert imported <= {"argparse", "hashlib", "json", "logging", "sys", "time", "datetime.datetime",
                        "pathlib.Path", "backend.db", "backend.instance_lock", "backend.crypto", "os", "sqlite3",
                        "dotenv.load_dotenv", "shutil", "backend.backup_verification", "backend.installation_state",
                        "backend.storage_classification"}, imported
    for module in ("backup_verification", "installation_state", "storage_classification"):
        source = (REPO / "backend" / f"{module}.py").read_text(encoding="utf-8")
        for forbidden in ("kite_client", "kiteconnect", "execution", "paper_broker", "order_monitor", "main"):
            assert f"import {forbidden}" not in source and f"backend.{forbidden}" not in source, (module, forbidden)


# ================================================================ compatibility and boundaries

def test_25_on_the_unregistered_build_the_command_is_inert(stopped, capsys):
    env = stopped
    before = _state(env)
    with phase2_build(env.db):
        assert _authorized(env) == 1
    assert "V12_NOT_REGISTERED" in capsys.readouterr().err
    assert _audit(env)["result"] == "V12_NOT_REGISTERED"
    _assert_untouched(env, before, allowed_new=("test.db.v12_migration_audit_",))


def test_26_v11_and_earlier_migrations_are_unchanged_and_never_consult_the_gate(stopped, monkeypatch):
    with phase2_build(stopped.db):                             # the build before registration
        _v11_and_earlier_unchanged(stopped, monkeypatch)


def _v11_and_earlier_unchanged(env, monkeypatch):
    from backend import db_maintenance
    _seed_application_data(env)

    def never(*args, **kwargs):
        raise AssertionError("the authorization gate refused on the unregistered build")

    monkeypatch.setattr(env.db, "_authorization_refusal", never)
    _rollback_v11(env)
    assert _history(env)[-1][0] == 10
    env.db.init_db()                                        # a normal start: v10 -> v11 as before
    assert env.db.last_init["applied_migrations"] == [11] and _history(env)[-1] == V11
    _rollback_v11(env)
    assert db_maintenance.main(["migrate", "--db", str(env.db.DB_PATH)]) == 0   # the maintenance path too
    assert _history(env)[-1] == V11
    assert db_maintenance.main(["rollback-schema-v11", "--confirm", "--db", str(env.db.DB_PATH)]) == 0
    assert db_maintenance.main(["migrate", "--db", str(env.db.DB_PATH)]) == 0
    env.db.init_db()
    assert env.db.last_init["applied_migrations"] == [] and _history(env)[-1] == V11


def test_27_a_pre_v11_database_on_a_registered_build_is_refused_everywhere(stopped, startup_spies):
    from backend import main
    env = stopped
    _seed_application_data(env)
    _rollback_v11(env)
    before = _state(env)
    with _registered(env.db):
        with pytest.raises(env.db.DatabaseMigrationAuthorizationRequired, match="below 11"):
            asyncio.run(main.on_startup())
        assert startup_spies == []
        assert _authorized(env) == 1
        assert _audit(env)["result"] == "HISTORY_REFUSED"
    _assert_untouched(env, before, allowed_new=("test.db.v12_migration_audit_",))
    assert _history(env)[-1][0] == 10


def test_28b_a_zero_byte_file_is_never_taken_for_a_fresh_install(env, monkeypatch):
    """V12 final readiness, Phase C (user decision 2026-11-11, evidence-based fresh install): a zero-byte file is
    what a truncated or re-created database looks like, so it is refused before any write, not created at v12."""
    fresh = env.tmp_path / "fresh.db"
    fresh.write_bytes(b"")
    monkeypatch.setattr(env.db, "DB_PATH", fresh)
    with _registered(env.db):
        with pytest.raises(env.db.DatabaseInstallationStateRefused, match="ZERO_BYTE"):
            env.db.init_db()
    assert fresh.read_bytes() == b""
    assert sorted(p.name for p in env.tmp_path.glob("fresh.db*")) == ["fresh.db"]


@pytest.mark.parametrize("kind", ["missing"])
def test_28_a_database_with_no_user_tables_is_created_at_v12_without_authorization(env, monkeypatch, kind):
    # Only a MISSING file with no evidence of an earlier installation is a fresh install now (the zero-byte case
    # is test_28b; the evidence cases are tests/test_installation_state.py).
    fresh = env.tmp_path / "fresh.db"
    monkeypatch.setattr(env.db, "DB_PATH", fresh)
    with _registered(env.db):
        env.db.init_db()
        assert env.db.last_init["applied_migrations"] == list(range(1, 13))
    conn = sqlite3.connect(fresh)
    try:
        assert [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")] == list(range(1, 13))
    finally:
        conn.close()
    assert not list(env.tmp_path.glob("fresh.db*_bak*"))


def test_29_a_foreign_history_is_still_refused_as_unsupported_before_any_write(env):
    with _build(env.db, env.db.MIGRATIONS[:10]):
        before = _state(env)
        with pytest.raises(env.db.DatabaseSchemaUnsupported, match="newer than this code supports"):
            env.db.init_db()
    _assert_untouched(env, before)


def test_30_the_registration_state_is_the_pinned_one_and_live_entries_stay_disabled():
    import re
    from backend import db
    assert schema_builds.is_registered(db) is schema_builds.V12_REGISTERED_IN_THIS_BUILD
    assert db.LATEST_SCHEMA_VERSION == schema_builds.expected_latest()
    assert db.MIGRATIONS[-1][:2] == (V12 if schema_builds.V12_REGISTERED_IN_THIS_BUILD else V11)
    source = (REPO / "backend" / "live_entry_gate.py").read_text(encoding="utf-8")   # the source constant, not
    assert re.search(r"^LIVE_ENTRIES_ENABLED = False$", source, re.MULTILINE)          # env's in-process lift
    assert db._V12_NAME == V12[1]
