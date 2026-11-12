"""v12 registration companion change (docs/PHASE_3_V12_FINAL_REGISTRATION_PROPOSAL.md §1 item 1), on env's temporary
database only: the dark entry points refuse any history that includes v12, so no dark rollback can leave
schema_version row 12 behind. Runs on either build (tests/schema_builds.py): env starts at exactly v11 (the
v11_database marker), the registered build is test_schema_v12._registered, the build before registration is
schema_builds.phase2_build; every test must leave the build as it found it.

The rollback-schema-v12 CLI (item 2) is tested in tests/test_rollback_v12_cli.py (the saved RP05-RP07, rewritten:
the unregistered build refuses it, r08d; v12 -> v11 -> v10 -> forward again through the Phase 2 build and
migrate-v12, r11; the lock and --confirm, r01/r02)."""

import pytest

from schema_builds import phase2_build
from test_schema_v12 import V11, V12, _registered

pytestmark = pytest.mark.v11_database


@pytest.fixture(autouse=True)
def _build_unchanged_by_the_test():
    from backend import db
    before = (list(db.MIGRATIONS), db.LATEST_SCHEMA_VERSION)
    yield
    assert (db.MIGRATIONS, db.LATEST_SCHEMA_VERSION) == before, "a test left the build changed"


def _conn(env):
    return env.db.get_connection()


def _history(env):
    conn = _conn(env)
    try:
        return [(h["version"], h["migration_name"]) for h in env.db.migration_history(conn)]
    finally:
        conn.close()


def _dark(env):
    conn = _conn(env)
    try:
        return env.db.dark_protection_schema_status(conn)
    finally:
        conn.close()


def _call(env, fn):
    conn = _conn(env)
    try:
        return fn(conn)
    finally:
        conn.close()


def _to_v12(env):
    # db.migrate() with the V12 authorization (what migrate-v12 --authorize-v12 runs): since the authorization gate a
    # normal start (init_db) refuses v12 on a database holding data.
    applied = _call(env, lambda conn: env.db.migrate(
        conn, v12_authorization=env.db.V12MigrationAuthorization(issued_by="test_v12_registration_prep")))
    assert applied == [12] and _history(env)[-1] == V12


def test_RP01_on_a_registered_build_a_dark_rollback_refuses_a_v12_database_and_row_12_stays(env):
    with _registered(env.db):
        _to_v12(env)
        with pytest.raises(env.db.DarkSchemaRefused, match="schema version 12, not 11"):
            _call(env, env.db.rollback_dark_protection_schema)
        with pytest.raises(env.db.DarkSchemaRefused, match="schema version 12, not 11"):
            _call(env, env.db.apply_dark_protection_schema)
        assert _history(env)[-1] == V12 and _dark(env)["state"] == "present"


def test_RP02_on_a_registered_build_a_v11_database_may_still_use_the_dark_path_then_migrates(env):
    with _registered(env.db):
        assert _call(env, env.db.apply_dark_protection_schema)["action"] == "applied"   # history is v1..v11
        assert _history(env)[-1] == V11
        _to_v12(env)                                         # migration 12 finds the objects present: keeps them
        assert _dark(env)["state"] == "present"


def test_RP03_unregistered_build_behaviour_is_unchanged(env):
    with phase2_build(env.db):
        assert _call(env, env.db.apply_dark_protection_schema)["action"] == "applied"
        assert _call(env, env.db.rollback_dark_protection_schema)["action"] == "rolled_back"
    assert _dark(env)["state"] == "absent"


def test_RP04_a_registered_migration_never_takes_the_dark_history_check(env, monkeypatch):
    calls = []
    monkeypatch.setattr(env.db, "_require_dark_history", lambda conn: calls.append(1))
    with _registered(env.db):
        _to_v12(env)
    assert calls == []
