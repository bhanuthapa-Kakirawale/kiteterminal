"""Phase 2 migration v11 (phase2_instance_coordination): apply, idempotency, refusal of partial or
foreign states, interruption (exception, KeyboardInterrupt, os._exit in a child process), backups, and
the written down-migration (rollback_schema_v11 / db_maintenance rollback-schema-v11).

Every test runs as the Phase 2 build (MIGRATIONS 1..11, tests/schema_builds.phase2_build): on a build that
registers migration 12 these are still the v11 behaviours an operator relies on to bring a database to v11
before migrate-v12, and env's database is created by that build (v11_database).

Every database here is a temporary one (the `env` fixture's, or a scratch file in tmp_path). The real
account database is never opened; the Phase 0 guard refuses it on every channel."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import release_write_authority
from schema_builds import phase2_build_patched

pytestmark = pytest.mark.v11_database


@pytest.fixture(autouse=True)
def _phase2_build(monkeypatch):
    # Through monkeypatch: these tests monkeypatch MIGRATIONS themselves (schema_builds.phase2_build_patched).
    from backend import db
    phase2_build_patched(monkeypatch, db)


PHASE2 =["app_instance", "app_instance_history", "broker_reads", "startup_reconciliation_runs",
          "account_restriction"]
REPO = Path(__file__).resolve().parent.parent


def _conn(env):
    return env.db.get_connection()


def _rows(env, sql, *args):
    conn = _conn(env)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _exec(env, sql, *args):
    conn = _conn(env)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _schema(path) -> dict:
    conn = sqlite3.connect(path)
    try:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex%' "
            "AND name NOT LIKE 'sqlite_stat%' ORDER BY name")}   # SQLite's own (PRAGMA optimize after a migration)
    finally:
        conn.close()


def _content(path, skip=()) -> dict:
    """Every row of every table (sqlite_sequence included), for row-level equality."""
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                             "AND name NOT LIKE 'sqlite_stat%' ORDER BY name")]
        return {t: sorted(map(repr, conn.execute(f'SELECT * FROM "{t}"').fetchall())) for t in tables if t not in skip}
    finally:
        conn.close()


def _history(env):
    return [(r["version"], r["migration_name"]) for r in _rows(env, "SELECT version, migration_name FROM schema_version "
                                                                     "ORDER BY version")]


def _status(env):
    conn = _conn(env)
    try:
        return env.db.phase2_schema_status(conn)
    finally:
        conn.close()


def _seed_trading_data(env):
    """Trading rows that must survive v11 and its rollback untouched."""
    conn = _conn(env)
    try:
        account = conn.execute("SELECT id FROM accounts WHERE is_default = 1").fetchone()["id"]
        conn.execute(
            "INSERT INTO trade_tickets (account_id, symbol, exchange, instrument_token, transaction_type, quantity, "
            "entry_price, entry_order_type, stop_loss, target_1, product_type, rationale, status, mode, source) "
            "VALUES (?, 'TCS', 'NSE', 2953217, 'BUY', 5, 3000, 'LIMIT', 2900, 3300, 'CNC', 'seed', 'executed', "
            "'live', 'manual')", (account,))
        conn.execute("INSERT INTO reconciliation_log (check_type, expected, actual, detail, issue_code) "
                     "VALUES ('position_state', 'a', 'b', 'c', 'BROKER_POSITION_SHORTFALL')")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- registration and objects

def test_v11_is_registered_after_v10_and_is_the_latest():
    from backend import db
    assert db.LATEST_SCHEMA_VERSION == 11
    assert [(v, n) for v, n, _ in db.MIGRATIONS[-2:]] == [(10, "ticket_integrity_audit"),
                                                         (11, "phase2_instance_coordination")]
    assert [name for name, _ in db.PHASE2_TABLES] == PHASE2


def test_a_fresh_database_gets_exactly_the_v11_objects_and_the_seed_row(env):
    assert _status(env) == "present"
    assert _history(env)[-1] == (11, "phase2_instance_coordination")
    [row] = _rows(env, "SELECT id, epoch, state, heartbeat_seq, lease_seconds, clean_shutdown FROM app_instance")
    # env's ownership grant took over once: epoch 1, owner.
    assert row["id"] == 1 and row["epoch"] == 1 and row["state"] == "owner" and row["clean_shutdown"] == 0
    for name, ddl in env.db.PHASE2_TABLES:
        stored = _rows(env, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", name)[0]["sql"]
        assert env.db._normalised_sql(stored) == env.db._normalised_sql(ddl)   # the design DDL, verbatim


def test_the_ddl_checks_hold(env):
    conn = _conn(env)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO app_instance (id, epoch, state) VALUES (2, 0, 'none')")   # singleton
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE app_instance SET state = 'running' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE app_instance SET lease_seconds = 5 WHERE id = 1")
        account = conn.execute("SELECT id FROM accounts").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):  # complete=1 needs every endpoint ok
            conn.execute("INSERT INTO broker_reads (account_id, purpose, instance_epoch, started_at, complete, "
                         "orders_error, gtts_malformed) VALUES (?, 'startup', 1, 'x', 1, 'boom', 0)", (account,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO startup_reconciliation_runs (account_id, instance_epoch, trigger, started_at) "
                         "VALUES (?, 1, 'reboot', 'x')", (account,))
        with pytest.raises(sqlite3.IntegrityError):  # NORMAL with reasons is contradictory
            conn.execute("UPDATE account_restriction SET restriction_state = 'NORMAL', "
                         "restriction_reasons = '[\"NO_SESSION\"]'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE account_restriction SET restriction_reasons = 'not json'")
        conn.rollback()
    finally:
        conn.close()


def test_the_default_restriction_is_restricted(env):
    conn = _conn(env)
    try:
        conn.execute("DELETE FROM account_restriction")
        account = conn.execute("SELECT id FROM accounts").fetchone()[0]
        conn.execute("INSERT INTO account_restriction (account_id) VALUES (?)", (account,))
        row = conn.execute("SELECT restriction_state, restriction_reasons FROM account_restriction").fetchone()
        conn.rollback()
    finally:
        conn.close()
    assert row["restriction_state"] == "RESTRICTED"
    assert json.loads(row["restriction_reasons"]) == ["STARTUP_RECONCILIATION_INCOMPLETE"]


# ---------------------------------------------------------------- apply over existing data

def test_v10_to_v11_preserves_every_existing_row_and_object(env):
    _seed_trading_data(env)
    env.db.init_db()   # the existing PK-18 invariant first scrubs env's plaintext session token
    release_write_authority()
    conn = _conn(env)
    env.db.rollback_schema_v11(conn)
    conn.close()
    path = env.db.DB_PATH
    before_schema, before_content = _schema(path), _content(path)
    env.db.init_db()
    assert env.db.last_init["applied_migrations"] == [11]
    after_schema = _schema(path)
    assert {k: v for k, v in after_schema.items() if k not in PHASE2} == before_schema   # nothing else changed
    assert set(after_schema) - set(before_schema) == set(PHASE2)
    after_content = _content(path, skip=PHASE2)
    assert {k: v for k, v in after_content.items() if k != "schema_version"} == \
        {k: v for k, v in before_content.items() if k != "schema_version"}
    assert list(env.tmp_path.glob("test.db.pre_schema_v11_bak*"))                      # the verified backup
    backup = sorted(env.tmp_path.glob("test.db.pre_schema_v11_bak*"))[-1]
    assert sqlite3.connect(backup).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert _schema(backup) == before_schema


def test_reapplying_is_idempotent_and_writes_nothing(env):
    path = env.db.DB_PATH
    env.db.init_db()   # the existing PK-18 invariant first scrubs env's plaintext session token
    schema, content = _schema(path), _content(path)
    for _ in range(3):
        env.db.init_db()
        assert env.db.last_init["applied_migrations"] == []
    assert _schema(path) == schema and _content(path) == content


def test_a_rerun_over_present_objects_keeps_their_data(env):
    """A lost schema_version table re-runs every migration (conftest.as_pre_pk22_database): v11 finds its
    objects exactly as it creates them and keeps their rows (the epoch is never reset)."""
    from conftest import as_pre_pk22_database
    epoch = _rows(env, "SELECT epoch FROM app_instance")[0]["epoch"]
    as_pre_pk22_database(env.db)
    env.db.init_db()
    assert 11 in env.db.last_init["applied_migrations"]
    assert _rows(env, "SELECT epoch FROM app_instance")[0]["epoch"] == epoch


@pytest.mark.parametrize("damage", ["one_table_missing", "foreign_table_form", "extra_seed_row", "view_named_like"])
def test_partial_or_foreign_phase2_objects_are_refused_before_any_write(env, damage):
    release_write_authority()
    conn = _conn(env)
    env.db.rollback_schema_v11(conn)
    if damage == "one_table_missing":
        conn.execute(env.db._APP_INSTANCE_DDL)
        conn.execute(env.db._APP_INSTANCE_SEED)
    elif damage == "foreign_table_form":
        conn.execute("CREATE TABLE broker_reads (id INTEGER PRIMARY KEY, anything TEXT)")
    elif damage == "extra_seed_row":
        for _name, ddl in env.db.PHASE2_TABLES:
            conn.execute(ddl)
        conn.execute("INSERT INTO app_instance (id, epoch, state) VALUES (1, 0, 'none')")
        conn.execute("DELETE FROM app_instance")
    else:
        conn.execute("CREATE VIEW app_instance AS SELECT 1 AS id")
    conn.commit()
    expected = {"one_table_missing": "partial", "foreign_table_form": "unexpected",
                "extra_seed_row": "unexpected", "view_named_like": "unexpected"}[damage]
    assert env.db.phase2_schema_status(conn) == expected
    conn.close()
    schema, content = _schema(env.db.DB_PATH), _content(env.db.DB_PATH)
    with pytest.raises(env.db.DatabaseMigrationFailed, match="partial or foreign form"):
        env.db.init_db()
    assert _schema(env.db.DB_PATH) == schema and _content(env.db.DB_PATH) == content
    assert _history(env)[-1][0] == 10


def test_a_database_from_newer_code_is_refused(env):
    _exec(env, "INSERT INTO schema_version (version, migration_name) VALUES (12, 'from_the_future')")
    with pytest.raises(env.db.DatabaseSchemaUnsupported):
        env.db.init_db()


def test_an_older_build_refuses_a_v11_database(env, monkeypatch):
    """What a pre-Phase-2 build (MIGRATIONS ending at v10) does with a migrated database."""
    monkeypatch.setattr(env.db, "MIGRATIONS", env.db.MIGRATIONS[:10])
    monkeypatch.setattr(env.db, "LATEST_SCHEMA_VERSION", 10)
    with pytest.raises(env.db.DatabaseSchemaUnsupported, match="newer than this code supports"):
        env.db.init_db()


# ---------------------------------------------------------------- interruption

@pytest.mark.parametrize("exc", [RuntimeError("simulated failure"), KeyboardInterrupt()])
def test_an_interrupted_v11_rolls_back_completely(env, monkeypatch, exc):
    release_write_authority()
    conn = _conn(env)
    env.db.rollback_schema_v11(conn)
    conn.close()
    schema, content = _schema(env.db.DB_PATH), _content(env.db.DB_PATH)
    real = env.db._migration_v11_instance_coordination

    def interrupted(c):
        c.execute(env.db._APP_INSTANCE_DDL)   # half-way: one table created
        raise exc

    monkeypatch.setattr(env.db, "MIGRATIONS", [m if m[0] != 11 else (11, m[1], interrupted) for m in env.db.MIGRATIONS])
    with pytest.raises(type(exc)):
        env.db.init_db()
    assert _schema(env.db.DB_PATH) == schema and _content(env.db.DB_PATH) == content
    monkeypatch.setattr(env.db, "MIGRATIONS", [m if m[0] != 11 else (11, m[1], real) for m in env.db.MIGRATIONS])
    env.db.init_db()                                    # the next start applies it cleanly
    assert env.db.last_init["applied_migrations"] == [11] and _status(env) == "present"


_CRASH_CHILD = r"""
import os, sys
sys.path.insert(0, {tests!r}); sys.path.insert(0, {repo!r})
import real_data_guard; real_data_guard.install(); real_data_guard.neutralise_dotenv(); real_data_guard.scrub_kite_environment()
os.environ["KITE_TERMINAL_ENCRYPTION_KEY"] = {key!r}
os.environ["KITE_API_KEY"] = "test_api_key"; os.environ["KITE_API_SECRET"] = "test_api_secret"
from pathlib import Path
from backend import crypto, db
crypto.ENV_PATH = Path({env_path!r}); db.DB_PATH = Path({db_path!r})
def crash(conn):
    for _name, ddl in db.PHASE2_TABLES[:3]:
        conn.execute(ddl)
    sys.stdout.write("CRASHING\n"); sys.stdout.flush()
    os._exit(137)
db.MIGRATIONS = [m if m[0] != 11 else (11, m[1], crash) for m in db.MIGRATIONS[:11]]   # the Phase 2 build
db.LATEST_SCHEMA_VERSION = 11
db.init_db()
"""


def test_a_process_killed_mid_migration_leaves_v10_and_the_next_start_migrates(env):
    release_write_authority()
    conn = _conn(env)
    env.db.rollback_schema_v11(conn)
    conn.close()
    schema, content = _schema(env.db.DB_PATH), _content(env.db.DB_PATH)
    code = _CRASH_CHILD.format(tests=str(REPO / "tests"), repo=str(REPO), key=os.environ["KITE_TERMINAL_ENCRYPTION_KEY"],
                               env_path=str(env.tmp_path / ".env"), db_path=str(env.db.DB_PATH))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=str(env.tmp_path))
    assert out.returncode == 137 and "CRASHING" in out.stdout, out.stderr[-2000:]
    assert _schema(env.db.DB_PATH) == schema and _content(env.db.DB_PATH) == content   # nothing half-applied
    env.db.init_db()
    assert env.db.last_init["applied_migrations"] == [11] and _status(env) == "present"


# ---------------------------------------------------------------- rollback (the written down-migration)

def test_rollback_restores_the_v10_schema_byte_for_byte_and_keeps_trading_data(env):
    _seed_trading_data(env)
    release_write_authority()
    path = env.db.DB_PATH
    # The v10 reference: the schema a v10 database has (v11 objects are additive).
    v11_schema = _schema(path)
    v10_reference = {k: v for k, v in v11_schema.items() if k not in PHASE2}
    trading_before = _content(path, skip=PHASE2 + ["schema_version", "sqlite_sequence"])
    conn = _conn(env)
    result = env.db.rollback_schema_v11(conn)
    assert env.db.schema_status(conn)["current_version"] == 10
    assert env.db.phase2_schema_status(conn) == "absent"
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
    assert _schema(path) == v10_reference
    assert _content(path, skip=["schema_version", "sqlite_sequence"]) == trading_before
    seq = {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_sequence")}
    assert not seq & set(PHASE2)                          # no orphaned AUTOINCREMENT counters
    assert Path(result["backup"]).exists() and Path(result["backup"]).name.startswith("test.db.pre_rollback_v11_bak")
    assert _schema(result["backup"]) == v11_schema        # the backup is the v11 database


def test_after_rollback_a_v10_build_accepts_the_database(env, monkeypatch):
    release_write_authority()
    conn = _conn(env)
    env.db.rollback_schema_v11(conn)
    conn.close()
    monkeypatch.setattr(env.db, "MIGRATIONS", env.db.MIGRATIONS[:10])
    monkeypatch.setattr(env.db, "LATEST_SCHEMA_VERSION", 10)
    env.db.init_db()
    assert env.db.last_init["applied_migrations"] == []


@pytest.mark.parametrize("state", ["at_v10", "newer", "tampered"])
def test_rollback_refuses_unless_exactly_v11_and_writes_nothing(env, state):
    release_write_authority()
    conn = _conn(env)
    if state == "at_v10":
        env.db.rollback_schema_v11(conn)
    elif state == "newer":
        conn.execute("INSERT INTO schema_version (version, migration_name) VALUES (12, 'x')")
        conn.commit()
    else:
        conn.execute("ALTER TABLE broker_reads ADD COLUMN extra TEXT")
        conn.commit()
    conn.close()
    schema, content = _schema(env.db.DB_PATH), _content(env.db.DB_PATH)
    backups = set(env.tmp_path.glob("*pre_rollback*"))
    conn = _conn(env)
    try:
        with pytest.raises(env.db.SchemaRollbackRefused):
            env.db.rollback_schema_v11(conn)
    finally:
        conn.close()
    assert _schema(env.db.DB_PATH) == schema and _content(env.db.DB_PATH) == content
    assert set(env.tmp_path.glob("*pre_rollback*")) == backups


def test_an_interrupted_rollback_leaves_v11_intact(env, monkeypatch):
    release_write_authority()
    path = env.db.DB_PATH
    schema, content = _schema(path), _content(path)
    real_status = env.db.phase2_schema_status
    calls = {"n": 0}

    def failing_status(conn):
        calls["n"] += 1
        if calls["n"] == 2:            # the check inside the rollback transaction
            raise KeyboardInterrupt
        return real_status(conn)

    monkeypatch.setattr(env.db, "phase2_schema_status", failing_status)
    conn = _conn(env)
    try:
        with pytest.raises(KeyboardInterrupt):
            env.db.rollback_schema_v11(conn)
    finally:
        conn.close()
    assert _schema(path) == schema
    assert _content(path, skip=[]) == content


def test_the_cli_rollback_needs_confirm_and_the_lock(env, capsys):
    from backend import db_maintenance
    path = str(env.db.DB_PATH)
    assert db_maintenance.main(["rollback-schema-v11", "--db", path]) == 2              # no --confirm
    assert db_maintenance.main(["rollback-schema-v11", "--confirm", "--db", path]) == 3  # env holds the lock
    assert "ANOTHER_INSTANCE_RUNNING" in capsys.readouterr().err
    assert _status(env) == "present"
    release_write_authority()
    assert db_maintenance.main(["rollback-schema-v11", "--confirm", "--db", path]) == 0
    assert "v11 -> v10" in capsys.readouterr().out
    assert _status(env) == "absent" and _history(env)[-1][0] == 10
    assert db_maintenance.main(["migrate", "--db", path]) == 0                          # and forward again
    assert _status(env) == "present"

