"""Phase 3 stage S2: migration v12 (phase3_protection_journal) in registered mode, and its written
down-migration rollback_schema_v12.

Runs unchanged on today's build (v12 prepared, NOT registered: DD-2) and on the build that registers it
(tests/schema_builds.py; the pin is schema_builds.V12_REGISTERED_IN_THIS_BUILD, test A1). env's database is
created at exactly v11 by the Phase 2 build (the v11_database marker). A test that needs the registered build
runs inside `with _registered(...)` (schema_builds.registered_build: the exact final MIGRATIONS entry, the
late-bound db._migration_v12_protection_journal; a no-op once v12 is registered); one that needs the build before
registration runs inside schema_builds.phase2_build. Both restore MIGRATIONS and LATEST_SCHEMA_VERSION by hand
(never monkeypatch.undo(), which would also undo env's DB_PATH redirect); an autouse fixture asserts every test
leaves the build as it found it.

Groups (docs/PHASE_3_READINESS_REVIEW.md §7.5, the approved S2 plan §9):
  A versioning while unregistered   B registered apply   C registered refusal   D partial failure
  E older builds   F rollback_schema_v12   G rollback ordering (and the Q3 guard of rollback_schema_v11)
  H data isolation

Every database here is a temporary one (the `env` fixture's, or a scratch file in tmp_path). The real
account database is never opened; the Phase 0 guard refuses it on every channel."""

import inspect
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import real_data_guard
import schema_builds
from conftest import as_pre_pk22_database, release_write_authority, ticket_row
from schema_builds import phase2_build
from test_dark_protection_schema import (_broker_read, _build_phase1_dark_schema, _insert_op, _position,
                                         _resolve_manually, _seed_application_data)

pytestmark = pytest.mark.v11_database

REPO = Path(__file__).resolve().parent.parent
V11 = (11, "phase2_instance_coordination")
V12 = (12, "phase3_protection_journal")
V12_TABLES = ["protection_operations", "protection_operation_resolutions"]
V12_OBJECTS = {"protection_operations", "protection_operation_resolutions", "idx_protection_ops_one_open_change",
               "idx_protection_ops_one_open_cancel", "idx_trade_tickets_unique_gtt"}
EVENT_TYPES = ("protection_pending", "protection_submitted", "protection_confirmed",
               "protection_unknown", "protection_adopted", "protection_cancelled",
               "protection_rejected", "protection_not_sent", "protection_absent_after_settle",
               "protection_needs_review", "protection_superseded", "protection_resolved",
               "protection_late_gtt_appeared")


# ---------------------------------------------------------------- the simulated build

def _build(db, migrations):
    """Runs the block as a build whose MIGRATIONS is `migrations`; restores both globals by hand."""
    return schema_builds.build(db, migrations)


def _registered(db, apply=None):
    """The build that registers v12 (M-9): the exact final entry (the late-bound wrapper), or `apply` in its
    place for failure injection. A no-op on a build that already registers v12."""
    return schema_builds.registered_build(db, apply)


@pytest.fixture(autouse=True)
def _build_unchanged_by_the_test():
    from backend import db
    before = (list(db.MIGRATIONS), db.LATEST_SCHEMA_VERSION)
    yield
    assert (db.MIGRATIONS, db.LATEST_SCHEMA_VERSION) == before, "a test left the build changed"


# ---------------------------------------------------------------- helpers

def _conn(env):
    return env.db.get_connection()


def _exec(env, sql, *args):
    conn = _conn(env)
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _rows(env, sql, *args):
    conn = _conn(env)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
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


def _content(path, skip=(), drop_protection_state=False) -> dict:
    """Every row of every table (sqlite_sequence included), for row-level equality. With
    drop_protection_state, trade_tickets is compared without the v12 column."""
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                             "AND name NOT LIKE 'sqlite_stat%' ORDER BY name")]
        out = {}
        for table in tables:
            if table in skip:
                continue
            cur = conn.execute("SELECT * FROM \"" + table.replace("\"", "\"\"") + "\"")
            names = [d[0] for d in cur.description]
            keep = [i for i, n in enumerate(names) if not (drop_protection_state and n == "protection_state")]
            out[table] = sorted(repr(tuple(row[i] for i in keep)) for row in cur.fetchall())
        return out
    finally:
        conn.close()


def _history(env):
    return [(r["version"], r["migration_name"]) for r in _rows(env, "SELECT version, migration_name FROM schema_version "
                                                                     "ORDER BY version")]


def _dark_status(env) -> dict:
    conn = _conn(env)
    try:
        return env.db.dark_protection_schema_status(conn)
    finally:
        conn.close()


def _backups(env) -> list[str]:
    return sorted(p.name for p in env.tmp_path.glob("*_bak*"))


def _snapshot(env):
    path = env.db.DB_PATH
    return _schema(path), _content(path), _backups(env)


def _assert_unchanged(env, snapshot, *, allowed_new_backups=()):
    schema, content, backups = snapshot
    assert _schema(env.db.DB_PATH) == schema
    assert _content(env.db.DB_PATH) == content
    new = sorted(set(_backups(env)) - set(backups))
    assert all(name.startswith(allowed_new_backups) for name in new) if allowed_new_backups else new == [], new
    assert set(backups) <= set(_backups(env))


def _apply_dark(env) -> dict:
    conn = _conn(env)
    try:
        return env.db.apply_dark_protection_schema(conn)
    finally:
        conn.close()


def _rollback_v12(env) -> dict:
    conn = _conn(env)
    try:
        return env.db.rollback_schema_v12(conn)
    finally:
        conn.close()


def _rollback_v11(env) -> dict:
    conn = _conn(env)
    try:
        return env.db.rollback_schema_v11(conn)
    finally:
        conn.close()


def _rollback_dark(env) -> dict:
    conn = _conn(env)
    try:
        return env.db.rollback_dark_protection_schema(conn)
    finally:
        conn.close()


def _settle(env):
    """One start of the Phase 2 build first: the existing PK-18 invariant scrubs env's plaintext session token
    (and re-encrypts the account secret), so later row comparisons see only what v12 does."""
    with phase2_build(env.db):
        env.db.init_db()
    assert env.db.last_init["applied_migrations"] == []


def _authorized_migrate(env) -> list[int]:
    """Inside a registered build: db.migrate() with the V12 authorization, which is what
    `db_maintenance migrate-v12 --authorize-v12` runs. Since the authorization gate
    (docs/PHASE_3_V12_AUTHORIZATION_GATE_IMPLEMENTATION_REPORT.md) a normal start (init_db) refuses v12
    on a database holding data, so these tests apply it the authorized way; migrate() is otherwise the
    same code init_db ran before the gate."""
    conn = _conn(env)
    try:
        return env.db.migrate(conn, v12_authorization=env.db.V12MigrationAuthorization(issued_by="test_schema_v12"))
    finally:
        conn.close()


def _migrate_to_v12(env):
    """Inside a registered build: applies v12 to env's (v11) database."""
    assert _authorized_migrate(env) == [12]
    assert _history(env)[-1] == V12


def _resolution_with_read(env) -> tuple[int, int]:
    """A needs_review op resolved manually against a fresh broker_reads row: (op id, read id)."""
    op = _insert_op(env, _position(env), kind="modify", state="needs_review")
    read = _broker_read(env)
    _resolve_manually(env, op, evidence_read_id=read)
    return op, read


def _orphan_resolution(env):
    conn = _conn(env)
    try:  # foreign keys off on this scratch connection only: a resolution whose op and read do not exist
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO protection_operation_resolutions (op_id, outcome, reason, evidence_read_id, "
                     "resolver, instance_epoch) VALUES (4242, 'no_app_gtt', 'orphan', 4343, 'operator', 1)")
        conn.commit()
    finally:
        conn.close()


def _dark_reference_schema(env) -> dict:
    """The schema apply_dark_protection_schema gives a verified copy of env's current (v11) database."""
    reference = env.tmp_path / "dark_reference.db"
    source = _conn(env)
    try:
        env.db.backup_database(source, reference)
    finally:
        source.close()
    conn = sqlite3.connect(reference)
    conn.row_factory = sqlite3.Row
    try:
        env.db.apply_dark_protection_schema(conn)
    finally:
        conn.close()
    return _schema(reference)


# ================================================================ A: versioning while unregistered

def test_a1_the_registration_state_is_exactly_the_pinned_one():
    """The one registration pin (schema_builds.V12_REGISTERED_IN_THIS_BUILD; the registration diff flips it).
    Unregistered: MIGRATIONS ends at v11 and LATEST_SCHEMA_VERSION is 11. Registered: v12 is listed through the
    late-bound wrapper, never the bare function (which MIGRATIONS cannot name: it is defined after the list)."""
    from backend import db
    expected = schema_builds.expected_latest()
    assert db.LATEST_SCHEMA_VERSION == expected
    assert [v for v, _, _ in db.MIGRATIONS] == list(range(1, expected + 1))
    assert schema_builds.is_registered(db) is schema_builds.V12_REGISTERED_IN_THIS_BUILD
    assert db.MIGRATIONS[10][:2] == V11
    if schema_builds.V12_REGISTERED_IN_THIS_BUILD:
        assert db.MIGRATIONS[11] == (12, "phase3_protection_journal", db._migration_v12_protection_journal)
    assert not any(fn in (db._m012_protection_journal, db._r012_protection_journal) for _, _, fn in db.MIGRATIONS)
    # The wrapper is the entry; it calls the bare function, whose default is registered mode (plan §4).
    registered = inspect.signature(db._m012_protection_journal).parameters["registered"]
    assert registered.kind is inspect.Parameter.KEYWORD_ONLY and registered.default is True
    assert db._AUTHORIZATION_GATED_MIGRATIONS == {12: "phase3_protection_journal"}


def test_a1b_the_wrapper_reaches_whatever_m012_is_bound_when_it_is_called(env, monkeypatch):
    """Late binding: the entry looks _m012_protection_journal up at call time, so it cannot raise NameError at
    import and a substitute installed later (failure injection) is what runs."""
    calls = []
    monkeypatch.setattr(env.db, "_m012_protection_journal", lambda conn: calls.append(conn) or {"action": "stub"})
    assert env.db._migration_v12_protection_journal("conn") == {"action": "stub"} and calls == ["conn"]


def test_a2_the_v12_rollback_is_exposed_only_as_db_maintenance_rollback_schema_v12():
    from backend import db_maintenance
    assert [c for c in db_maintenance.WRITING_COMMANDS if "12" in c] == ["rollback-schema-v12"]
    offenders = []
    for path in sorted((REPO / "backend").rglob("*.py")):
        if path.name == "db.py":
            continue
        text = path.read_text(encoding="utf-8")
        hits = [n for n in ("rollback_schema_v12", "_m012_registered", "phase3_protection_journal") if n in text]
        if path.name == "db_maintenance.py":
            hits = [n for n in hits if n != "rollback_schema_v12"]   # the command calls it, nothing else
        if hits:
            offenders.append((str(path.relative_to(REPO)), hits))
    assert offenders == []


@pytest.mark.parametrize("dark", [False, True], ids=["dark_absent", "dark_present"])
def test_a3_the_v12_rollback_is_inert_on_the_unregistered_build(env, dark):
    """Without the registration (length and name) checks, a v11 database carrying the dark schema
    would match a v1..v11 `expected` and lose its dark objects."""
    if dark:
        _apply_dark(env)
    release_write_authority()
    snapshot = _snapshot(env)
    with phase2_build(env.db):
        with pytest.raises(env.db.SchemaRollbackRefused, match="does not register migration 12"):
            _rollback_v12(env)
    _assert_unchanged(env, snapshot)
    assert _dark_status(env)["state"] == ("present" if dark else "absent")


# ================================================================ B: registered apply (simulated)

def test_b1_a_v11_database_migrates_to_exactly_the_v12_schema(env):
    reference = _dark_reference_schema(env)
    with _registered(env.db):
        _migrate_to_v12(env)
        status = _dark_status(env)
        assert status["state"] == "present" and status["unique_gtt_index"] is True and status["mismatched"] == []
        conn = _conn(env)
        try:
            assert env.db.schema_status(conn)["up_to_date"] is True
        finally:
            conn.close()
    # The same stored schema as the dark apply creates (schema_version rows differ, not its CREATE).
    assert _schema(env.db.DB_PATH) == reference


def test_b2_v11_to_v12_keeps_every_row_and_backs_up_the_v11_image(env):
    ids = _seed_application_data(env)
    read = _broker_read(env)
    _settle(env)
    path = env.db.DB_PATH
    before_schema, before_content = _schema(path), _content(path)
    assert _content(path)["broker_reads"]
    with _registered(env.db):
        _migrate_to_v12(env)
    after_schema = _schema(path)
    assert set(after_schema) - set(before_schema) == V12_OBJECTS
    assert {k: v for k, v in after_schema.items() if k not in V12_OBJECTS | {"ticket_events", "trade_tickets"}} == \
        {k: v for k, v in before_schema.items() if k not in ("ticket_events", "trade_tickets")}
    after = _content(path, skip=V12_TABLES, drop_protection_state=True)
    assert {k: v for k, v in after.items() if k != "schema_version"} == \
        {k: v for k, v in before_content.items() if k != "schema_version"}   # sqlite_sequence included
    assert _rows(env, "SELECT id FROM broker_reads WHERE id = ?", read) == [{"id": read}]
    assert ticket_row(env.db, ids["paper"])["kite_gtt_id"] == "PAPER-G-7"
    new_event = _exec(env, "INSERT INTO ticket_events (ticket_id, event_type) VALUES (?, 'modified')", ids["buy"])
    assert new_event == ids["gone_event"] + 1                 # the AUTOINCREMENT high-water mark survived
    [backup] = sorted(env.tmp_path.glob("test.db.pre_schema_v12_bak*"))
    assert sqlite3.connect(backup).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert _schema(backup) == before_schema and _content(backup) == before_content


def test_b3_v10_to_v12_in_one_start_takes_a_single_backup_of_the_v10_image(env):
    _seed_application_data(env)
    release_write_authority()
    _rollback_v11(env)
    path = env.db.DB_PATH
    v10_schema, v10_content = _schema(path), _content(path)
    assert _history(env)[-1][0] == 10
    with _registered(env.db):
        assert _authorized_migrate(env) == [11, 12]
        assert _history(env)[-2:] == [V11, V12]
    assert not list(env.tmp_path.glob("test.db.pre_schema_v11_bak*"))
    [backup] = sorted(env.tmp_path.glob("test.db.pre_schema_v12_bak*"))   # named after the target version
    assert _schema(backup) == v10_schema and _content(backup) == v10_content
    assert _dark_status(env)["state"] == "present"


def test_b4_repeated_starts_after_v12_are_idempotent_and_write_nothing(env):
    path = env.db.DB_PATH
    with _registered(env.db):
        _migrate_to_v12(env)
        env.db.init_db()
        schema, content, backups = _schema(path), _content(path), _backups(env)
        for _ in range(3):
            env.db.init_db()
            assert env.db.last_init["applied_migrations"] == []
    assert _schema(path) == schema and _content(path) == content and _backups(env) == backups


def test_b5_present_objects_are_a_no_op_that_keeps_journal_and_resolution_rows(env):
    _apply_dark(env)
    op, read = _resolution_with_read(env)
    pending = _insert_op(env, _position(env), state="intent")
    _exec(env, "UPDATE trade_tickets SET protection_state = 'PROTECTION_PENDING' WHERE id = "
               "(SELECT position_id FROM protection_operations WHERE id = ?)", pending)
    _settle(env)
    path = env.db.DB_PATH
    schema, content = _schema(path), _content(path)
    with _registered(env.db):
        _migrate_to_v12(env)
    assert _schema(path) == schema                              # ticket_events not rebuilt, nothing created
    assert {k: v for k, v in _content(path).items() if k != "schema_version"} == \
        {k: v for k, v in content.items() if k != "schema_version"}
    assert _rows(env, "SELECT state, resolution_id IS NOT NULL AS r FROM protection_operations WHERE id = ?", op) == \
        [{"state": "resolved_manual", "r": 1}]
    assert _rows(env, "SELECT evidence_read_id FROM protection_operation_resolutions") == [{"evidence_read_id": read}]


def test_b6_a_lost_schema_version_rerun_over_v12_keeps_the_data(env):
    with _registered(env.db):
        _migrate_to_v12(env)
        op, read = _resolution_with_read(env)
        _exec(env, "INSERT INTO ticket_events (ticket_id, event_type) VALUES ((SELECT position_id FROM "
                   "protection_operations WHERE id = ?), 'protection_resolved')", op)
        path = env.db.DB_PATH
        schema = _schema(path)
        content = _content(path, skip=["schema_version"])
        as_pre_pk22_database(env.db)
        assert 12 in _authorized_migrate(env)
        assert _history(env)[-1] == V12
    assert _schema(path) == schema
    assert _content(path, skip=["schema_version"]) == content
    assert _dark_status(env)["state"] == "present"


def test_b7_registered_mode_never_takes_the_dark_path(env, monkeypatch):
    calls = []

    def spy(name):
        def refuse(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"registered v12 called {name}")
        return refuse

    for name in ("_run_dark_step", "_backup_before", "_require_current_schema", "_refuse_account_database",
                 "_require_no_transaction"):
        monkeypatch.setattr(env.db, name, spy(name))
    with _registered(env.db):
        _migrate_to_v12(env)
    assert calls == []
    assert _dark_status(env)["state"] == "present"
    assert not list(env.tmp_path.glob("*pre_dark_protection*"))
    assert [p.name for p in env.tmp_path.glob("*_bak*")] == ["test.db.pre_schema_v12_bak"]   # migrate()'s own


# ================================================================ C: registered refusal

def _assert_refused_at_v11(env, snapshot, match):
    with _registered(env.db):
        with pytest.raises(env.db.DatabaseMigrationFailed, match=match):
            _authorized_migrate(env)
    _assert_unchanged(env, snapshot, allowed_new_backups=("test.db.pre_schema_v12_bak",))
    assert _history(env)[-1] == V11


@pytest.mark.parametrize("gtt_id", [77, "PAPER-G-9"], ids=["live", "paper"])
def test_c1_duplicate_gtt_ids_are_refused_and_named(env, gtt_id):
    mode = "dry_run" if isinstance(gtt_id, str) else "live"
    _position(env, mode=mode, kite_gtt_id=gtt_id)
    _position(env, mode=mode, kite_gtt_id=gtt_id)
    _position(env, kite_gtt_id=5150)                          # a unique one is not named
    snapshot = _snapshot(env)
    with _registered(env.db):
        with pytest.raises(env.db.DatabaseMigrationFailed) as refused:
            _authorized_migrate(env)
    assert f"[{gtt_id!r}]" in str(refused.value) and "linked to more than one ticket" in str(refused.value)
    assert "5150" not in str(refused.value)
    _assert_unchanged(env, snapshot, allowed_new_backups=("test.db.pre_schema_v12_bak",))
    assert _history(env)[-1] == V11 and _dark_status(env)["state"] == "absent"
    with phase2_build(env.db):
        env.db.init_db()                                      # the build before registration starts normally
    assert env.db.last_init["applied_migrations"] == []


@pytest.mark.parametrize("ddl", [
    "CREATE TABLE protection_operations (id INTEGER PRIMARY KEY)",                       # foreign form
    "CREATE VIEW protection_operations AS SELECT 1",                                      # another object type
    "CREATE INDEX idx_trade_tickets_unique_gtt ON trade_tickets(kite_gtt_id)",            # a stray Phase 1 name
    "ALTER TABLE trade_tickets ADD COLUMN protection_state TEXT",                         # half applied, foreign
    "CREATE TABLE protection_operation_resolutions (id INTEGER PRIMARY KEY)",             # foreign R6 table
    "CREATE INDEX idx_protection_ops_one_open_cancel ON trade_tickets(id)",               # a stray R6 index name
    "phase1",                                                                             # the Phase 1-era schema
    "partial",                                                                            # one real object only
])
def test_c2_partial_unexpected_and_phase1_era_objects_are_refused(env, ddl):
    if ddl == "phase1":
        _build_phase1_dark_schema(env)
    elif ddl == "partial":
        _exec(env, env.db._PROTECTION_OPERATIONS_DDL)
    else:
        _exec(env, ddl)
    assert _dark_status(env)["state"] in ("partial", "unexpected")
    _assert_refused_at_v11(env, _snapshot(env), "partial or foreign form")


@pytest.mark.parametrize("duplicates_cleared", [False, True], ids=["duplicates_remain", "duplicates_cleared"])
def test_c3_present_without_the_unique_gtt_index_is_refused(env, duplicates_cleared):
    """User decision Q2: not exactly v12's objects, whether or not the duplicates are still there."""
    _position(env, kite_gtt_id=42)
    dup = _position(env, kite_gtt_id=42)
    assert _apply_dark(env)["unique_gtt_index"] == "skipped_duplicate_gtt_ids"
    if duplicates_cleared:
        _exec(env, "UPDATE trade_tickets SET kite_gtt_id = NULL WHERE id = ?", dup)
    status = _dark_status(env)
    assert status["state"] == "present" and status["unique_gtt_index"] is False
    _assert_refused_at_v11(env, _snapshot(env), "present without idx_trade_tickets_unique_gtt")
    assert _dark_status(env)["unique_gtt_index"] is False


@pytest.mark.parametrize("shape,match", [
    ("ALTER TABLE ticket_events ADD COLUMN extra TEXT", "unexpected columns"),
    ("CREATE TRIGGER ticket_events_audit AFTER INSERT ON ticket_events BEGIN SELECT 1; END", "trigger or view"),
], ids=["extra_column", "trigger"])
def test_c4_a_foreign_ticket_events_shape_is_refused(env, shape, match):
    _exec(env, shape)
    _assert_refused_at_v11(env, _snapshot(env), f"migration 12 .*{match}")


# ================================================================ D: partial failure

@pytest.mark.parametrize("exc", [RuntimeError("simulated failure"), KeyboardInterrupt()])
def test_d1_an_interrupted_v12_leaves_v11_intact_and_the_next_start_applies(env, monkeypatch, exc):
    _seed_application_data(env)
    snapshot = _snapshot(env)
    real = env.db._rebuild_ticket_events

    def interrupted(conn, sql):   # after both tables, both indexes, the column and the rebuild
        real(conn, sql)
        raise exc

    monkeypatch.setattr(env.db, "_rebuild_ticket_events", interrupted)
    with _registered(env.db):
        with pytest.raises(type(exc)):
            _authorized_migrate(env)
        _assert_unchanged(env, snapshot, allowed_new_backups=("test.db.pre_schema_v12_bak",))
        assert _history(env)[-1] == V11 and _dark_status(env)["state"] == "absent"
        monkeypatch.setattr(env.db, "_rebuild_ticket_events", real)   # by hand, not monkeypatch.undo()
        _migrate_to_v12(env)
    assert _dark_status(env)["state"] == "present"


_CRASH_CHILD = r"""
import os, sys
sys.path.insert(0, {tests!r}); sys.path.insert(0, {repo!r})
import real_data_guard; real_data_guard.install(); real_data_guard.neutralise_dotenv(); real_data_guard.scrub_kite_environment()
os.environ["KITE_TERMINAL_ENCRYPTION_KEY"] = {key!r}
os.environ["KITE_API_KEY"] = "test_api_key"; os.environ["KITE_API_SECRET"] = "test_api_secret"
from pathlib import Path
from backend import crypto, db
crypto.ENV_PATH = Path({env_path!r}); db.DB_PATH = Path({db_path!r})
real = db._rebuild_ticket_events
def crash(conn, sql):
    real(conn, sql)
    sys.stdout.write("CRASHING\n"); sys.stdout.flush()
    os._exit(137)
db._rebuild_ticket_events = crash
if len(db.MIGRATIONS) == 11:   # a build that does not register v12 yet: the exact final entry
    db.MIGRATIONS = db.MIGRATIONS + [(12, "phase3_protection_journal", db._migration_v12_protection_journal)]
    db.LATEST_SCHEMA_VERSION = 12
db.migrate(db.get_connection(), v12_authorization=db.V12MigrationAuthorization(issued_by="test_schema_v12 d2"))
"""


def test_d2_a_process_killed_mid_v12_leaves_v11_and_the_next_start_migrates(env):
    _seed_application_data(env)
    release_write_authority()
    snapshot = _snapshot(env)
    code = _CRASH_CHILD.format(tests=str(REPO / "tests"), repo=str(REPO), key=os.environ["KITE_TERMINAL_ENCRYPTION_KEY"],
                               env_path=str(env.tmp_path / ".env"), db_path=str(env.db.DB_PATH))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=str(env.tmp_path))
    assert out.returncode == 137 and "CRASHING" in out.stdout, out.stderr[-2000:]
    _assert_unchanged(env, snapshot, allowed_new_backups=("test.db.pre_schema_v12_bak",))   # nothing half-applied
    assert _history(env)[-1] == V11 and _dark_status(env)["state"] == "absent"
    with _registered(env.db):
        _migrate_to_v12(env)
    assert _dark_status(env)["state"] == "present"


def test_d3_v10_to_v12_with_v12_failing_commits_v11_and_keeps_the_v10_backup(env):
    _seed_application_data(env)
    release_write_authority()
    _rollback_v11(env)
    v10_schema = _schema(env.db.DB_PATH)

    def failing(conn):
        conn.execute(env.db._PROTECTION_OPERATIONS_DDL)   # half-way
        raise RuntimeError("v12 failed")

    with _registered(env.db, apply=failing):
        with pytest.raises(RuntimeError, match="v12 failed"):
            _authorized_migrate(env)
    assert _history(env)[-1] == V11                       # v11 committed on its own
    assert _dark_status(env)["state"] == "absent"
    with phase2_build(env.db):
        env.db.init_db()                                  # the Phase 2 build accepts it
    assert env.db.last_init["applied_migrations"] == []
    [backup] = sorted(env.tmp_path.glob("test.db.pre_schema_v12_bak*"))
    assert _schema(backup) == v10_schema


# ================================================================ E: older builds

@pytest.mark.parametrize("keep", [11, 10])
def test_e1_older_builds_refuse_a_v12_database(env, keep):
    with _registered(env.db):
        _migrate_to_v12(env)
        older = env.db.MIGRATIONS[:keep]
    snapshot = _snapshot(env)
    with _build(env.db, older):
        assert env.db.LATEST_SCHEMA_VERSION == keep
        with pytest.raises(env.db.DatabaseSchemaUnsupported, match="newer than this code supports"):
            env.db.init_db()
    _assert_unchanged(env, snapshot)


# ================================================================ F: rollback_schema_v12

def test_f1_rollback_restores_the_v11_schema_and_keeps_every_row(env, caplog):
    ids = _seed_application_data(env)
    read = _broker_read(env)
    _settle(env)
    path = env.db.DB_PATH
    v11_schema, v11_content = _schema(path), _content(path, skip=["schema_version"])
    with _registered(env.db):
        _migrate_to_v12(env)
        v12_schema = _schema(path)
        release_write_authority()
        with caplog.at_level(logging.WARNING, logger="backend.db"):
            result = _rollback_v12(env)
        assert result["rolled_back_to"] == 11
        assert _history(env)[-1] == V11
        conn = _conn(env)
        try:
            assert env.db.schema_status(conn)["current_version"] == 11
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            conn.close()
    assert "event=schema_rollback.applied from=12 to=11" in caplog.text
    assert _schema(path) == v11_schema                     # stored SQL, ticket_events and trade_tickets included
    assert _content(path, skip=["schema_version"]) == v11_content   # trading rows, broker_reads, sqlite_sequence
    assert _rows(env, "SELECT id FROM broker_reads WHERE id = ?", read) == [{"id": read}]
    seq = {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_sequence")}
    assert not seq & set(V12_TABLES)                      # no orphaned AUTOINCREMENT counters
    new_event = _exec(env, "INSERT INTO ticket_events (ticket_id, event_type) VALUES (?, 'modified')", ids["buy"])
    assert new_event == ids["gone_event"] + 1
    backup = Path(result["backup"])
    assert backup.name.startswith("test.db.pre_rollback_v12_bak") and backup.exists()
    assert _schema(backup) == v12_schema                  # the backup is the v12 database


def test_f2_after_the_rollback_the_phase2_build_accepts_the_database(env):
    with _registered(env.db):
        _migrate_to_v12(env)
        release_write_authority()
        _rollback_v12(env)
    with phase2_build(env.db):
        env.db.init_db()
    assert env.db.last_init["applied_migrations"] == [] and _history(env)[-1] == V11


_F3_STATES = ["open_transaction", "history_at_v11", "history_newer", "tampered_v12_name", "objects_partial",
              "objects_unexpected", "unique_index_missing", "journal_row", "resolution_row", "orphan_resolution_row",
              "protection_state"] + [f"event:{t}" for t in EVENT_TYPES]


@pytest.mark.parametrize("state", _F3_STATES)
def test_f3_every_refusal_writes_nothing_and_takes_no_backup(env, state):
    with _registered(env.db):
        _migrate_to_v12(env)
        release_write_authority()
        if state == "history_at_v11":
            _exec(env, "DELETE FROM schema_version WHERE version = 12")
        elif state == "history_newer":
            _exec(env, "INSERT INTO schema_version (version, migration_name) VALUES (13, 'from_the_future')")
        elif state == "tampered_v12_name":
            _exec(env, "UPDATE schema_version SET migration_name = 'something_else' WHERE version = 12")
        elif state == "objects_partial":
            _exec(env, "DROP INDEX idx_protection_ops_one_open_cancel")
        elif state == "objects_unexpected":
            _exec(env, "DROP INDEX idx_protection_ops_one_open_cancel")
            _exec(env, "CREATE INDEX idx_protection_ops_one_open_cancel ON protection_operations(position_id)")
        elif state == "unique_index_missing":
            _exec(env, "DROP INDEX idx_trade_tickets_unique_gtt")
        elif state == "journal_row":
            _insert_op(env, _position(env), state="not_sent")
        elif state == "resolution_row":
            _resolution_with_read(env)
        elif state == "orphan_resolution_row":
            _orphan_resolution(env)
        elif state == "protection_state":
            _exec(env, "UPDATE trade_tickets SET protection_state = 'PROTECTION_PENDING' WHERE id = ?", _position(env))
        elif state.startswith("event:"):
            _exec(env, "INSERT INTO ticket_events (ticket_id, event_type) VALUES (?, ?)", _position(env),
                  state.split(":", 1)[1])
        snapshot = _snapshot(env)
        conn = _conn(env)
        try:
            if state == "open_transaction":
                conn.execute("UPDATE accounts SET id = id")
                assert conn.in_transaction
            with pytest.raises(env.db.SchemaRollbackRefused) as refused:
                env.db.rollback_schema_v12(conn)
            if state == "open_transaction":
                conn.rollback()
        finally:
            conn.close()
        _assert_unchanged(env, snapshot)
    message = str(refused.value)
    assert "Nothing was changed" in message or "open transaction" in message
    if state in ("journal_row", "resolution_row", "orphan_resolution_row", "protection_state") or ":" in state:
        assert "hold data" in message
    if state == "orphan_resolution_row":
        assert "'protection_operations rows': 0, 'protection_operation_resolutions rows': 1" in message


def test_f3b_a_build_with_a_gapped_history_up_to_v12_is_refused_before_any_backup(env):
    """The len(expected) == 12 check on its own: a build listing v12 but not v11, over a database whose
    history matches it, must not get as far as the backup."""
    _apply_dark(env)
    release_write_authority()
    _exec(env, "DELETE FROM schema_version WHERE version = 11")
    _exec(env, "INSERT INTO schema_version (version, migration_name) VALUES (12, 'phase3_protection_journal')")
    snapshot = _snapshot(env)
    gapped = env.db.MIGRATIONS[:10] + [(12, "phase3_protection_journal", env.db._m012_protection_journal)]
    with _build(env.db, gapped):
        with pytest.raises(env.db.SchemaRollbackRefused, match="exactly v1..v12"):
            _rollback_v12(env)
    _assert_unchanged(env, snapshot)


def test_f4_an_interrupted_rollback_leaves_v12_intact(env, monkeypatch):
    with _registered(env.db):
        _migrate_to_v12(env)
        release_write_authority()
        path = env.db.DB_PATH
        schema, content = _schema(path), _content(path)
        real = env.db._r012_protection_journal

        def interrupted(conn):   # after every drop and the ticket_events rebuild
            real(conn)
            raise KeyboardInterrupt

        monkeypatch.setattr(env.db, "_r012_protection_journal", interrupted)
        conn = _conn(env)
        try:
            isolation = conn.isolation_level
            with pytest.raises(KeyboardInterrupt):
                env.db.rollback_schema_v12(conn)
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1 and conn.isolation_level == isolation
            assert not conn.in_transaction
        finally:
            conn.close()
        assert _schema(path) == schema and _content(path) == content
        assert _history(env)[-1] == V12 and _dark_status(env)["state"] == "present"
        monkeypatch.setattr(env.db, "_r012_protection_journal", real)
        assert _rollback_v12(env)["rolled_back_to"] == 11   # and it still rolls back cleanly afterwards


def test_f5_a_second_rollback_refuses_and_writes_nothing(env):
    with _registered(env.db):
        _migrate_to_v12(env)
        release_write_authority()
        _rollback_v12(env)
        snapshot = _snapshot(env)
        with pytest.raises(env.db.SchemaRollbackRefused, match="exactly v1..v12"):
            _rollback_v12(env)
        _assert_unchanged(env, snapshot)


# ================================================================ G: rollback ordering

def test_g1_the_v11_rollback_refuses_a_v12_database(env):
    with _registered(env.db):
        _migrate_to_v12(env)
        release_write_authority()
        snapshot = _snapshot(env)
        with pytest.raises(env.db.SchemaRollbackRefused, match="exactly v1..v11"):
            _rollback_v11(env)
        _assert_unchanged(env, snapshot)


def test_g2_v12_then_v11_rolls_back_to_v10_and_a_v10_build_accepts_it(env):
    _seed_application_data(env)
    _settle(env)
    path = env.db.DB_PATH
    phase2 = [name for name, _ in env.db.PHASE2_TABLES]
    trading = _content(path, skip=phase2 + ["schema_version", "sqlite_sequence"])
    with _registered(env.db):
        _migrate_to_v12(env)
        release_write_authority()
        assert _rollback_v12(env)["rolled_back_to"] == 11
        assert _rollback_v11(env)["rolled_back_to"] == 10
    assert _history(env)[-1][0] == 10
    assert _content(path, skip=["schema_version", "sqlite_sequence"]) == trading
    with _build(env.db, env.db.MIGRATIONS[:10]):
        env.db.init_db()
        assert env.db.last_init["applied_migrations"] == []


def test_g3_resolution_rows_citing_broker_reads_block_both_rollbacks(env):
    with _registered(env.db):
        _migrate_to_v12(env)
        _resolution_with_read(env)
        release_write_authority()
        broker_reads = _content(env.db.DB_PATH)["broker_reads"]
        snapshot = _snapshot(env)
        with pytest.raises(env.db.SchemaRollbackRefused, match="hold data"):
            _rollback_v12(env)
        with pytest.raises(env.db.SchemaRollbackRefused):
            _rollback_v11(env)
        _assert_unchanged(env, snapshot)
    assert _content(env.db.DB_PATH)["broker_reads"] == broker_reads


@pytest.mark.parametrize("with_resolutions", [False, True], ids=["empty", "with_resolution_rows"])
def test_g4_the_v11_rollback_refuses_while_the_dark_schema_depends_on_broker_reads(env, with_resolutions):
    """User decision Q3: on an unregistered v11 database carrying the dark schema, the v11 rollback
    would drop broker_reads underneath protection_operation_resolutions."""
    _apply_dark(env)
    if with_resolutions:
        _resolution_with_read(env)
    release_write_authority()
    snapshot = _snapshot(env)
    with pytest.raises(env.db.SchemaRollbackRefused, match=r"\['protection_operation_resolutions'\].*Roll back "
                                                           r"the dependent schema\(s\) first"):
        _rollback_v11(env)
    _assert_unchanged(env, snapshot)
    assert not list(env.tmp_path.glob("*pre_rollback_v11_bak*"))
    if not with_resolutions:
        _rollback_dark(env)
        assert _rollback_v11(env)["rolled_back_to"] == 10
        assert _history(env)[-1][0] == 10


@pytest.mark.parametrize("ddl,dependant", [
    ("CREATE TABLE later_audit (x INTEGER REFERENCES app_instance(id))", "later_audit"),
    ('CREATE TABLE "odd ""name"" here" (x INTEGER REFERENCES "Broker_Reads"(id))', 'odd "name" here'),
    ("CREATE TABLE unrelated (x INTEGER REFERENCES accounts(id))", None),
], ids=["plain", "quoted_identifier_and_case", "no_phase2_reference"])
def test_g5_the_dependency_guard_is_generic(env, ddl, dependant):
    _exec(env, ddl)
    release_write_authority()
    if dependant is None:
        assert _rollback_v11(env)["rolled_back_to"] == 10   # a reference elsewhere does not block
        return
    snapshot = _snapshot(env)
    with pytest.raises(env.db.SchemaRollbackRefused) as refused:
        _rollback_v11(env)
    assert repr([dependant]) in str(refused.value)
    _assert_unchanged(env, snapshot)


# ================================================================ H: data isolation

def test_h1_an_account_database_stand_in_is_refused_without_writes_or_backup(env, monkeypatch, tmp_path):
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    monkeypatch.setattr(env.db, "_ACCOUNT_DB_FILE", account_dir / "kite_terminal.db")
    monkeypatch.setattr(env.db, "DB_PATH", account_dir / "kite_terminal.db")
    with phase2_build(env.db):                               # the build before registration
        env.db.init_db()
    path = env.db.DB_PATH
    schema, content = _schema(path), _content(path)
    files = sorted(p.name for p in account_dir.iterdir())
    with phase2_build(env.db), pytest.raises(env.db.SchemaRollbackRefused, match="does not register migration 12"):
        _rollback_v12(env)
    assert _schema(path) == schema and _content(path) == content
    assert sorted(p.name for p in account_dir.iterdir()) == files
    assert not list(account_dir.glob("*_bak*"))


def test_h2_registered_apply_and_rollback_open_only_files_under_tmp_path(env):
    _seed_application_data(env)
    release_write_authority()
    with real_data_guard.recording() as seen:
        with _registered(env.db):
            _migrate_to_v12(env)
            _rollback_v12(env)
    connects = [path for event, path in seen if event == "sqlite3.connect"]
    assert connects, "expected the scratch database and its backups to be opened"
    root = str(env.tmp_path.resolve()).lower()
    assert all(str(Path(p).resolve()).lower().startswith(root) for p in connects), connects
    assert not any("kite_terminal.db" in str(p) for _, p in seen)
    assert real_data_guard.unexpected_refusals() == []
