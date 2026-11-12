"""Installation identity: an intentional fresh install versus an established installation whose database is
missing, emptied, replaced, re-created or moved (V12 final readiness, Phase C).

User decision (2026-11-11): evidence-based fresh install. A missing database with no evidence of an earlier
installation is created at the latest schema (v12) by a normal start; everything else that is not the database
this installation last used is refused before any write, and a normal start can never turn an established
installation into a new v12 database. Every test runs on temporary files with a per-test installation registry.
"""

import asyncio
import json
import logging
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import schema_builds
from conftest import release_write_authority
from schema_builds import V11, V12

from backend import installation_state as ist

REFUSED = "DATABASE_INSTALLATION_STATE_REFUSED"


@pytest.fixture
def inst(tmp_path, monkeypatch):
    """A database path in its own directory, a per-test registry and a key (no .env written)."""
    from backend import db
    folder = tmp_path / "install"
    folder.mkdir()
    registry = tmp_path / "registry"
    monkeypatch.setenv(ist.REGISTRY_ENV, str(registry))
    monkeypatch.setenv("KITE_TERMINAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("KITE_API_KEY", "test_api_key")                # sentinels, as the env fixture sets
    monkeypatch.setenv("KITE_API_SECRET", "test_api_secret")
    monkeypatch.setattr(db, "DB_PATH", folder / "kite_terminal_test.db")
    return db


def _path(db) -> Path:
    return Path(db.DB_PATH)


def _history(path) -> list:
    conn = sqlite3.connect(path)
    try:
        return [tuple(r) for r in conn.execute("SELECT version, migration_name FROM schema_version ORDER BY version")]
    finally:
        conn.close()


def _remove_set(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(f"{path}{suffix}")
        if p.exists():
            p.unlink()


def _new_database(path: Path, *, tables: bool = True) -> None:
    """A file made outside the terminal (sqlite3 CLI, another program)."""
    conn = sqlite3.connect(path)
    try:
        if tables:
            conn.execute("CREATE TABLE foreign_table (x INTEGER)")
            conn.execute("INSERT INTO foreign_table VALUES (1)")
        else:
            conn.execute("CREATE TABLE t (x)")
            conn.execute("DROP TABLE t")
        conn.commit()
    finally:
        conn.close()


def _refused(db, state: str):
    with pytest.raises(db.DatabaseInstallationStateRefused) as e:
        db.init_db()
    assert e.value.state == state and str(e.value).startswith(REFUSED) and "Nothing was changed" in str(e.value)
    return e.value


def _install(db) -> dict:
    db.init_db()
    return json.loads(ist.record_file(_path(db)).read_text(encoding="utf-8"))


def _app_id(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA application_id").fetchone()[0]
    finally:
        conn.close()


# ================================================================ 1. explicit (first-run) fresh install

def test_i01_a_first_run_with_no_evidence_creates_the_database_at_the_latest_schema_and_records_it(inst):
    path = _path(inst)
    assert ist.assess(path).state == ist.FRESH_INSTALL_PERMITTED
    record = _install(inst)
    assert _history(path)[-1] == V12 and inst.last_init["applied_migrations"] == list(range(1, 13))
    assert record["canonical_path"] == ist.canonical(path) and record["install_id"]
    assert ist.registry_file(path).exists() and ist.registry_file(path).parent == Path(os.environ[ist.REGISTRY_ENV])
    assert _app_id(path) == record["application_id"] != 0           # the header carries this installation's id
    assert record["last_known"]["st_ino"] == os.stat(path).st_ino
    assert ist.assess(path).state == ist.ESTABLISHED_VALID


def test_i01b_records_hold_no_account_data(inst):
    record = _install(inst)
    assert set(record) == {"format", "install_id", "created_at", "canonical_path", "database_name", "application_id",
                           "last_known", "history"}
    text = ist.record_file(_path(inst)).read_text(encoding="utf-8").lower()
    for forbidden in ("token", "secret", "api_key", "account_id", "symbol"):
        assert forbidden not in text


def test_i01c_init_fresh_on_a_first_run_creates_the_database(inst, capsys):
    from backend import db_maintenance
    path = _path(inst)
    assert db_maintenance.main(["init-fresh", "--confirm-new-installation", "--db", str(path)]) == 0
    assert _history(path)[-1] == V12 and ist.assess(path).state == ist.ESTABLISHED_VALID


def test_i01d_init_fresh_needs_its_flag_before_any_filesystem_access(inst, capsys):
    from backend import db_maintenance
    assert db_maintenance.main(["init-fresh", "--db", str(_path(inst))]) == 2
    assert not _path(inst).parent.joinpath(_path(inst).name + ".instance.lock").exists()


# ================================================================ 2. established, valid

def test_i02_an_established_valid_database_starts_normally_and_is_unchanged_by_the_check(inst):
    _install(inst)
    before = os.stat(_path(inst)).st_ino
    inst.init_db()
    assert inst.last_init["applied_migrations"] == [] and inst.last_init["startup_row_changes"] == 0
    assert os.stat(_path(inst)).st_ino == before and ist.assess(_path(inst)).state == ist.ESTABLISHED_VALID


# ================================================================ 3. established, database missing

def test_i03_a_deleted_database_of_an_established_installation_is_refused_and_not_recreated(inst):
    path = _path(inst)
    _install(inst)
    _remove_set(path)
    err = _refused(inst, ist.ESTABLISHED_MISSING)
    assert "restore-backup" in str(err)
    assert not path.exists()                                         # nothing was created


def test_i03b_the_registry_alone_survives_the_deletion_of_the_whole_folder(inst):
    path = _path(inst)
    _install(inst)
    shutil.rmtree(path.parent)
    path.parent.mkdir()
    err = _refused(inst, ist.ESTABLISHED_MISSING)
    assert "registry:" in str(err) and not path.exists()


def test_i03c_a_legacy_installation_without_records_is_recognised_by_its_backups(inst):
    path = _path(inst)
    Path(f"{path}.pre_ph8_bak").write_bytes(b"x")                    # a backup name an earlier phase left
    _refused(inst, ist.ESTABLISHED_MISSING)
    assert not path.exists()


@pytest.mark.parametrize("sibling", ["-wal", "-shm", ".restore_audit_1.json", ".pre_restore_1_orig",
                                     ".failed_restore_1", ".pre_schema_v12_bak", ".installation.json"])
def test_i03d_any_sibling_an_installation_leaves_is_evidence(inst, sibling):
    path = _path(inst)
    Path(f"{path}{sibling}").write_bytes(b"{}" if sibling.endswith(".json") else b"x")
    assert ist.assess(path).state == ist.ESTABLISHED_MISSING


def test_i03f_the_instance_lock_file_is_not_evidence_because_every_start_creates_it_first(inst):
    path = _path(inst)
    Path(f"{path}.instance.lock").write_bytes(b"")
    assert ist.assess(path).state == ist.FRESH_INSTALL_PERMITTED


def test_i03e_unrelated_files_in_the_folder_are_not_evidence(inst):
    path = _path(inst)
    (path.parent / "notes.txt").write_text("x")
    (path.parent / "other.db_bak").write_text("x")                   # another database's backup
    assert ist.assess(path).state == ist.FRESH_INSTALL_PERMITTED


# ================================================================ 4/5/6. zero-byte, empty SQLite, corrupt

@pytest.mark.parametrize("established", [True, False])
def test_i04_a_zero_byte_file_is_never_a_fresh_install(inst, established):
    path = _path(inst)
    if established:
        _install(inst)
        _remove_set(path)
    path.write_bytes(b"")
    _refused(inst, ist.ZERO_BYTE)
    assert path.read_bytes() == b""


@pytest.mark.parametrize("established", [True, False])
def test_i05_an_empty_sqlite_file_is_never_a_fresh_install(inst, established):
    path = _path(inst)
    if established:
        _install(inst)
        _remove_set(path)
    _new_database(path, tables=False)
    before = path.read_bytes()
    _refused(inst, ist.EMPTY_SQLITE)
    assert path.read_bytes() == before


def test_i06_a_corrupt_file_is_refused_and_kept(inst):
    path = _path(inst)
    _install(inst)
    _remove_set(path)
    path.write_bytes(b"this is not a database" * 400)
    before = path.read_bytes()
    _refused(inst, ist.CORRUPT)
    assert path.read_bytes() == before


# ================================================================ 7/8/9. schema histories pass to the V12 gate

def _install_with_build(inst, migrations):
    with schema_builds.build(inst, migrations):
        inst.init_db()


def test_i07_below_v11_passes_the_installation_check_and_the_v12_gate_still_refuses(inst):
    _install_with_build(inst, inst.MIGRATIONS[:10])
    assert _history(_path(inst))[-1][0] == 10
    with pytest.raises(inst.DatabaseMigrationAuthorizationRequired, match="below 11"):
        inst.init_db()
    assert _history(_path(inst))[-1][0] == 10


def test_i08_a_v11_database_holding_data_is_still_never_migrated_to_v12_by_a_start(inst):
    _install_with_build(inst, inst.MIGRATIONS[:11])
    assert _history(_path(inst))[-1] == V11
    with pytest.raises(inst.DatabaseMigrationAuthorizationRequired):
        inst.init_db()
    assert _history(_path(inst))[-1] == V11


def test_i09_a_v12_database_starts(inst):
    _install(inst)
    inst.init_db()
    assert _history(_path(inst))[-1] == V12


# ================================================================ 10. path changed

def test_i10_a_database_copied_to_another_folder_is_path_changed_until_acknowledged(inst, monkeypatch, capsys):
    from backend import db_maintenance
    path = _path(inst)
    _install(inst)
    moved = path.parent.parent / "moved"
    shutil.copytree(path.parent, moved)
    new_path = moved / path.name
    monkeypatch.setattr(inst, "DB_PATH", new_path)
    _refused(inst, ist.PATH_CHANGED)
    assert db_maintenance.main(["acknowledge-database", "--confirm", "--db", str(new_path)]) == 0
    inst.init_db()
    assert ist.assess(new_path).state == ist.ESTABLISHED_VALID


def test_i10b_a_repository_copied_without_its_database_does_not_start_a_second_empty_installation(inst, monkeypatch,
                                                                                                   capsys):
    """State 10, the path changed WITHOUT the database: nothing at the new path is evidence, but this machine's
    registry knows the installation whose database still exists elsewhere (same database name)."""
    from backend import db_maintenance
    path = _path(inst)
    _install(inst)
    elsewhere = path.parent.parent / "copied_repo" / path.name
    elsewhere.parent.mkdir()
    monkeypatch.setattr(inst, "DB_PATH", elsewhere)
    err = _refused(inst, ist.OTHER_INSTALLATION_PRESENT)
    assert ist.canonical(path) in str(err) and not elsewhere.exists()
    assert db_maintenance.main(["init-fresh", "--confirm-new-installation", "--db", str(elsewhere)]) == 1
    assert db_maintenance.main(["init-fresh", "--confirm-new-installation", "--acknowledge-prior-installation",
                                "--db", str(elsewhere)]) == 0      # a deliberate second installation
    assert _history(elsewhere)[-1] == V12


def test_i10c_a_scratch_database_of_another_name_is_not_another_installation(inst, monkeypatch):
    _install(inst)
    scratch = _path(inst).parent / "scratch_analysis.db"
    assert ist.assess(scratch).state == ist.FRESH_INSTALL_PERMITTED


def test_i10d_an_installation_whose_database_is_gone_is_not_an_obstacle_elsewhere(inst, monkeypatch, tmp_path):
    path = _path(inst)
    _install(inst)
    elsewhere = tmp_path / "new_home" / path.name
    elsewhere.parent.mkdir()
    _remove_set(path)                                   # the old installation's database no longer exists
    assert ist.assess(elsewhere).state == ist.FRESH_INSTALL_PERMITTED


# ================================================================ 11. replaced while the terminal is stopped

def test_i11_a_file_replaced_by_rename_is_identity_changed(inst, tmp_path):
    path = _path(inst)
    _install(inst)
    other = tmp_path / "elsewhere.db"
    shutil.copyfile(path, other)                                     # even an identical copy is another file
    _remove_set(path)
    os.replace(other, path)
    err = _refused(inst, ist.IDENTITY_CHANGED)
    assert "acknowledge-database" in str(err)


def test_i11b_a_foreign_database_copied_over_the_file_in_place_is_identity_changed(inst, tmp_path, monkeypatch):
    path = _path(inst)
    _install(inst)
    foreign = tmp_path / "foreign" / "kite_terminal_test.db"
    foreign.parent.mkdir()
    monkeypatch.setattr(inst, "DB_PATH", foreign)
    registry = os.environ[ist.REGISTRY_ENV]
    monkeypatch.setenv(ist.REGISTRY_ENV, str(tmp_path / "another_machine_registry"))
    inst.init_db()                                     # another machine's installation (its own application_id)
    monkeypatch.setenv(ist.REGISTRY_ENV, registry)
    monkeypatch.setattr(inst, "DB_PATH", path)
    shutil.copyfile(foreign, path)                                   # same file id: copied over in place
    assert os.stat(path).st_ino == json.loads(ist.record_file(path).read_text())["last_known"]["st_ino"]
    err = _refused(inst, ist.IDENTITY_CHANGED)
    assert "application_id" in str(err)


def test_i11c_an_older_v11_copy_put_back_in_place_is_schema_regressed(inst, tmp_path):
    path = _path(inst)
    _install_with_build(inst, inst.MIGRATIONS[:11])
    v11_copy = tmp_path / "v11_copy.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    shutil.copyfile(path, v11_copy)
    with schema_builds.registered_build(inst):
        schema_builds.authorized_migrate(inst)
        inst.init_db()                                               # the installation has now seen v12
    _remove_set(Path(f"{path}-wal"))
    shutil.copyfile(v11_copy, path)                                  # in place: same file id, same application_id
    _refused(inst, ist.SCHEMA_REGRESSED)


def test_i11d_residual_an_older_copy_of_the_same_schema_put_back_in_place_is_not_detected(inst, tmp_path):
    """ACCEPTED_RESIDUAL_RISK (documented): same file id, same application_id, same schema version: the
    installation cannot tell an older copy of itself from the current file. Proven here so it is never assumed."""
    path = _path(inst)
    _install(inst)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    older = tmp_path / "older.db"
    shutil.copyfile(path, older)
    inst.init_db()
    shutil.copyfile(older, path)
    assert ist.assess(path).state == ist.ESTABLISHED_VALID


# ================================================================ 12. re-created after deletion

def test_i12_a_database_recreated_after_deletion_is_identity_changed(inst):
    path = _path(inst)
    _install(inst)
    _remove_set(path)
    _new_database(path, tables=True)
    _refused(inst, ist.IDENTITY_CHANGED)


# ================================================================ interrupted operations, unreadable records

@pytest.mark.parametrize("suffix", [ist.MIGRATION_MARKER_SUFFIX, ist.RESTORE_MARKER_SUFFIX])
def test_i13_an_unfinished_maintenance_marker_refuses_the_start(inst, suffix):
    path = _path(inst)
    _install(inst)
    ist.marker_file(path, suffix).write_text("{}")
    err = _refused(inst, ist.INTERRUPTED_OPERATION)
    assert "re-run the same command" in str(err)


def test_i14_an_unreadable_record_is_refused_never_treated_as_no_record(inst):
    path = _path(inst)
    _install(inst)
    ist.record_file(path).write_text("{ not json")
    _refused(inst, ist.RECORD_UNREADABLE)


def test_i14b_an_unreadable_record_can_be_acknowledged_after_inspection(inst, capsys):
    from backend import db_maintenance
    path = _path(inst)
    _install(inst)
    ist.record_file(path).write_text("{ not json")
    assert db_maintenance.main(["acknowledge-database", "--confirm", "--db", str(path)]) == 0
    assert list(path.parent.glob(f"{path.name}.installation.json.superseded_*"))
    inst.init_db()


def test_i14c_a_hot_rollback_journal_is_refused_before_any_open_and_left_intact(inst):
    path = _path(inst)
    _install(inst)
    journal = Path(f"{path}-journal")
    journal.write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + b"\x01" * 504)
    before = journal.read_bytes()
    err = _refused(inst, ist.HOT_JOURNAL)
    assert "Do NOT delete it" in str(err) and journal.read_bytes() == before


# ================================================================ the core guarantee

@pytest.mark.parametrize("damage", ["deleted", "deleted_with_record", "zero_byte", "empty", "recreated", "corrupt"])
def test_i15_an_established_installation_can_never_silently_become_a_fresh_v12_database(inst, damage):
    path = _path(inst)
    record = _install(inst)
    _remove_set(path)
    if damage == "deleted_with_record":
        ist.record_file(path).unlink()
    elif damage == "zero_byte":
        path.write_bytes(b"")
    elif damage == "empty":
        _new_database(path, tables=False)
    elif damage == "recreated":
        _new_database(path, tables=True)
    elif damage == "corrupt":
        path.write_bytes(b"\x00garbage" * 512)
    snapshot = path.read_bytes() if path.exists() else None
    with pytest.raises(inst.DatabaseInstallationStateRefused):
        inst.init_db()
    assert (path.read_bytes() if path.exists() else None) == snapshot   # nothing created, nothing changed
    registry = json.loads(ist.registry_file(path).read_text(encoding="utf-8"))
    assert registry["install_id"] == record["install_id"]               # the installation is still the old one


def test_i15b_the_v12_gate_still_applies_to_every_existing_database(inst):
    """The installation check never weakens the authorization gate: a v11 database holding data is refused by the
    gate after passing the installation check (LEGACY_UNMARKED included)."""
    _install_with_build(inst, inst.MIGRATIONS[:11])
    for p in (ist.record_file(_path(inst)), ist.registry_file(_path(inst))):
        p.unlink()
    assert ist.assess(_path(inst)).state == ist.LEGACY_UNMARKED
    with pytest.raises(inst.DatabaseMigrationAuthorizationRequired):
        inst.init_db()
    assert _history(_path(inst))[-1] == V11


def test_i15c_a_legacy_database_is_adopted_after_a_successful_start(inst):
    path = _path(inst)
    _install(inst)
    for p in (ist.record_file(path), ist.registry_file(path)):
        p.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA application_id = 0")
    finally:
        conn.close()
    assert ist.assess(path).state == ist.LEGACY_UNMARKED
    inst.init_db()
    assert ist.assess(path).state == ist.ESTABLISHED_VALID and _app_id(path) != 0


# ================================================================ explicit fresh install over evidence

def test_i16_init_fresh_refuses_without_acknowledging_the_earlier_installation(inst, capsys):
    from backend import db_maintenance
    path = _path(inst)
    _install(inst)
    _remove_set(path)
    assert db_maintenance.main(["init-fresh", "--confirm-new-installation", "--db", str(path)]) == 1
    assert "restore-backup" in capsys.readouterr().err and not path.exists()


def test_i16b_init_fresh_with_acknowledgement_creates_a_new_installation(inst, capsys):
    from backend import db_maintenance
    path = _path(inst)
    old = _install(inst)
    _remove_set(path)
    assert db_maintenance.main(["init-fresh", "--confirm-new-installation", "--acknowledge-prior-installation",
                                "--db", str(path)]) == 0
    new = json.loads(ist.record_file(path).read_text(encoding="utf-8"))
    assert new["install_id"] != old["install_id"] and _history(path)[-1] == V12


def test_i16c_init_fresh_keeps_a_zero_byte_file_aside_never_deletes_it(inst, capsys):
    from backend import db_maintenance
    path = _path(inst)
    path.write_bytes(b"")
    assert db_maintenance.main(["init-fresh", "--confirm-new-installation", "--acknowledge-prior-installation",
                                "--db", str(path)]) == 0
    kept = list(path.parent.glob(f"{path.name}.replaced_by_init_fresh_*"))
    assert len(kept) == 1 and kept[0].read_bytes() == b"" and _history(path)[-1] == V12


@pytest.mark.parametrize("flags", [[], ["--acknowledge-prior-installation"]])
def test_i16d_init_fresh_never_replaces_a_database_that_holds_tables(inst, capsys, flags):
    from backend import db_maintenance
    path = _path(inst)
    _install(inst)
    before = path.stat().st_ino
    assert db_maintenance.main(["init-fresh", "--confirm-new-installation", *flags, "--db", str(path)]) == 1
    assert path.stat().st_ino == before and _history(path)[-1] == V12


def test_i16e_a_normal_start_never_passes_a_fresh_install_confirmation():
    import inspect
    from backend import db, main
    assert "fresh_install" not in inspect.getsource(main)
    source = inspect.getsource(db)
    assert source.count("FreshInstallConfirmation(") == 0          # constructed only by db_maintenance init-fresh


def test_i16f_a_forged_confirmation_object_is_ignored(inst):
    path = _path(inst)
    _install(inst)
    _remove_set(path)

    class Forged:
        issued_by = "x"
        acknowledge_prior_installation = True
    with pytest.raises(inst.DatabaseInstallationStateRefused):
        inst.init_db(fresh_install=Forged())
    with pytest.raises(inst.DatabaseInstallationStateRefused):
        inst.init_db(fresh_install=ist.FreshInstallConfirmation(issued_by="t", acknowledge_prior_installation=False))
    assert not path.exists()


# ================================================================ acknowledge-database

def test_i17_acknowledge_database_refuses_a_missing_or_empty_database(inst, capsys):
    from backend import db_maintenance
    path = _path(inst)
    _install(inst)
    _remove_set(path)
    assert db_maintenance.main(["acknowledge-database", "--confirm", "--db", str(path)]) == 1
    path.write_bytes(b"")
    assert db_maintenance.main(["acknowledge-database", "--confirm", "--db", str(path)]) == 1


def test_i17b_acknowledge_database_rebinds_after_an_inspected_replacement(inst, tmp_path, capsys):
    from backend import db_maintenance
    path = _path(inst)
    _install(inst)
    other = tmp_path / "copy.db"
    shutil.copyfile(path, other)
    _remove_set(path)
    os.replace(other, path)
    assert ist.assess(path).state == ist.IDENTITY_CHANGED
    assert db_maintenance.main(["acknowledge-database", "--confirm", "--db", str(path)]) == 0
    inst.init_db()


def test_i17c_acknowledge_database_needs_confirm_before_any_filesystem_access(inst, capsys):
    from backend import db_maintenance
    assert db_maintenance.main(["acknowledge-database", "--db", str(_path(inst))]) == 2


def test_i17d_a_rollback_rebinds_the_schema_so_the_next_start_is_not_schema_regressed(inst):
    path = _path(inst)
    _install(inst)
    with schema_builds.registered_build(inst):
        conn = inst.get_connection()
        try:
            inst.rollback_schema_v12(conn)
        finally:
            conn.close()
    assert ist.assess(path).state == ist.ESTABLISHED_VALID
    with pytest.raises(inst.DatabaseMigrationAuthorizationRequired):   # back at v11: the gate, not the record
        inst.init_db()


# ================================================================ the application start (main.on_startup)

def test_i01e_the_application_first_run_creates_the_database_although_the_lock_file_comes_first(env, monkeypatch):
    """The real start order: instance_lock.acquire creates <db>.instance.lock, THEN init_db runs. A genuine first run
    must still be a fresh install (regression guard: the lock file is not evidence of an earlier installation)."""
    from backend import instance_lock, main, server
    monkeypatch.setattr(server, "enforce_runtime_policy", lambda *a, **k: None)
    monkeypatch.setattr(instance_lock, "takeover", lambda **k: {"conflict": True})   # stop right after init_db
    release_write_authority()
    fresh = env.tmp_path / "first_run" / "kite_terminal_fresh.db"
    fresh.parent.mkdir()
    monkeypatch.setattr(env.db, "DB_PATH", fresh)
    try:
        asyncio.run(main.on_startup())
        assert Path(f"{fresh}.instance.lock").exists()
        assert _history(fresh)[-1] == V12 and ist.assess(fresh).state == ist.ESTABLISHED_VALID
    finally:
        instance_lock.release()



def test_i18_the_application_start_refuses_a_missing_database_and_releases_the_lock(env, monkeypatch, caplog):
    from backend import crypto, instance_lock, kite_client, main, server, workers
    calls = []
    monkeypatch.setattr(server, "enforce_runtime_policy", lambda *a, **k: None)
    monkeypatch.setattr(instance_lock, "takeover", lambda **k: calls.append("takeover"))
    monkeypatch.setattr(workers.supervisor, "start", lambda loops: calls.append("loops"))
    monkeypatch.setattr(kite_client, "get_kite", lambda *a, **k: calls.append("get_kite"))
    real_key = crypto.ensure_encryption_key
    monkeypatch.setattr(crypto, "ensure_encryption_key", lambda: (calls.append("key"), real_key())[1])
    release_write_authority()
    path = Path(env.db.DB_PATH)
    _remove_set(path)
    with caplog.at_level(logging.INFO):
        with pytest.raises(env.db.DatabaseInstallationStateRefused):
            asyncio.run(main.on_startup())
    assert calls == [] and not path.exists()
    assert f"event=startup.refused code={REFUSED}" in caplog.text
    assert instance_lock.status()["role"] == instance_lock.RELEASED and not instance_lock.authority_ok()
