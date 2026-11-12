"""Schema builds for the tests: the same suite runs on today's build (V12 prepared but NOT registered) and,
unchanged, on the build that registers migration 12 (docs/PHASE_3_V12_REGISTRATION_REMEDIATION_REPORT.md §11).

V12_REGISTERED_IN_THIS_BUILD is the one pin of the registration state: every test that asserts "the latest schema
version" reads it, and the registration diff flips it together with backend/db.py's MIGRATIONS entry. Everything
else here works in either state:

  phase2_build(db)        MIGRATIONS[:11] (the Phase 2 build, before migration 12 was registered)
  registered_build(db)    the build that registers v12 with the exact final entry; a no-op once it is registered
  authorized_migrate(db)  db.migrate() with the V12 authorization (what migrate-v12 --authorize-v12 runs)
  to_v11(db) / to_v12(db) bring env's temporary database to exactly v11 / v12
  restart_old_database(db) the next start of an old-database simulation (as_pre_pk22_database, a dropped version
                          row): the Phase 2 build brings it to v11, the registered build then applies v12 the
                          authorized way, as an operator would

Every database these helpers touch is a test's temporary file (env's db.DB_PATH, or a path under tmp_path); the
real account database is refused by the session guard (tests/real_data_guard.py). Both build helpers restore
MIGRATIONS and LATEST_SCHEMA_VERSION by hand, never with monkeypatch.undo() (which would also undo env's DB_PATH)."""

import sqlite3
from contextlib import contextmanager, nullcontext
from pathlib import Path

V12_REGISTERED_IN_THIS_BUILD = True

V11 = (11, "phase2_instance_coordination")
V12 = (12, "phase3_protection_journal")
V12_ISSUER = "tests/schema_builds.py"


def expected_latest() -> int:
    return 12 if V12_REGISTERED_IN_THIS_BUILD else 11


def is_registered(db) -> bool:
    return len(db.MIGRATIONS) >= 12 and tuple(db.MIGRATIONS[11][:2]) == V12


def final_v12_entry(db) -> tuple:
    """The MIGRATIONS entry the registration diff appends: the late-bound wrapper, never the bare function."""
    return (12, V12[1], db._migration_v12_protection_journal)


@contextmanager
def build(db, migrations):
    """Runs the block as a build whose MIGRATIONS is `migrations`; restores both globals by hand."""
    saved = (db.MIGRATIONS, db.LATEST_SCHEMA_VERSION)
    db.MIGRATIONS = list(migrations)
    db.LATEST_SCHEMA_VERSION = db.MIGRATIONS[-1][0]
    try:
        yield
    finally:
        db.MIGRATIONS, db.LATEST_SCHEMA_VERSION = saved


def phase2_build(db):
    """The Phase 2 build: migrations 1..11 only. On today's build this changes nothing."""
    return build(db, db.MIGRATIONS[:11])


def phase2_build_patched(monkeypatch, db) -> None:
    """The Phase 2 build for a whole test that ALSO monkeypatches MIGRATIONS itself: set through monkeypatch, so the
    undo order is right (the test's own patch is undone first, back to this build, then this one, back to the
    real build). A hand-restored build plus a monkeypatched MIGRATIONS would let monkeypatch's late undo leave the
    truncated list behind for every later test (conftest's _build_restored fixture fails such a test)."""
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:11])
    monkeypatch.setattr(db, "LATEST_SCHEMA_VERSION", db.MIGRATIONS[-1][0])


def registered_build(db, apply=None):
    """The build that registers migration 12. With `apply`, v12's function is replaced (failure injection);
    otherwise it is the exact final entry, and on a build that already registers v12 nothing changes."""
    if apply is not None:
        return build(db, db.MIGRATIONS[:11] + [(12, V12[1], apply)])
    if is_registered(db):
        return nullcontext()
    return build(db, db.MIGRATIONS + [final_v12_entry(db)])


def _open(db, path=None):
    if path is None:
        return db.get_connection()
    conn = sqlite3.connect(Path(path))
    conn.row_factory = sqlite3.Row
    return conn


def history(db, path=None) -> list[tuple[int, str]]:
    conn = _open(db, path)
    try:
        return [(h["version"], h["migration_name"]) for h in db.migration_history(conn)]
    finally:
        conn.close()


def authorized_migrate(db, path=None, issued_by=V12_ISSUER) -> list[int]:
    """db.migrate() with the V12 authorization on a temporary database (default: db.DB_PATH). The caller
    chooses the build (registered_build); on the Phase 2 build there is nothing gated to authorize."""
    conn = _open(db, path)
    try:
        return db.migrate(conn, v12_authorization=db.V12MigrationAuthorization(issued_by=issued_by))
    finally:
        conn.close()


def _remove_new_backups(directory: Path, before: set) -> None:
    """The rollback/migration backups these helpers' own steps write are test artefacts, not part of the state a
    test sets up: removed, so the temporary directory looks exactly as it would on the other build."""
    for p in directory.glob("*_bak*"):
        if p not in before:
            p.unlink()


def to_v11(db) -> None:
    """env's temporary database at exactly v11. On a v12 database (the registered build) the written
    down-migration (rollback_schema_v12) removes the empty journal; on today's build env is already v11."""
    if history(db)[-1:] == [V11]:
        return
    assert history(db)[-1:] == [V12], history(db)
    directory = Path(db.DB_PATH).parent
    before = set(directory.glob("*_bak*"))
    with registered_build(db):
        conn = db.get_connection()
        try:
            db.rollback_schema_v12(conn)
        finally:
            conn.close()
    _remove_new_backups(directory, before)
    assert history(db)[-1:] == [V11]


def to_v12(db, *, keep_backup=True) -> None:
    """env's temporary database at exactly v12, applied the authorized way inside the registered build."""
    if history(db)[-1:] == [V12]:
        return
    directory = Path(db.DB_PATH).parent
    before = set(directory.glob("*_bak*"))
    with registered_build(db):
        assert authorized_migrate(db)[-1] == 12
    if not keep_backup:
        _remove_new_backups(directory, before)
    assert history(db)[-1:] == [V12]


def as_legacy_installation(db) -> None:
    """env's temporary database as an OLD installation that predates the installation records (V12 final readiness,
    Phase C). A test that rewinds the schema by hand simulates an old database restarting on new code; a real upgrade
    has no installation record yet (LEGACY_UNMARKED), while a rewound database WITH a record is what an old copy put
    in place by hand looks like, and is refused (SCHEMA_REGRESSED, tests/test_installation_state.py i11c). So the
    simulation forgets the throwaway records of the temporary database first."""
    from backend import installation_state
    installation_state.forget(db.DB_PATH)


def restart_old_database(db) -> list[int]:
    """The next start of a simulated old database (no schema_version, or a version row removed). Since the
    authorization gate a normal start never applies v12 to a database holding data, so: the Phase 2 build's
    start brings it to v11 (re-running the legacy migrations, as before), then on the registered build v12 is
    applied the authorized way and the start completes. Returns every version applied, in order."""
    with phase2_build(db):
        db.init_db()
        applied = list(db.last_init["applied_migrations"])
    if is_registered(db):
        applied += authorized_migrate(db)
        db.init_db()
    return applied
