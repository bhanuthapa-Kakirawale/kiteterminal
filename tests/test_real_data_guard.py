"""The test suite can never read the real account database or its backups.

tests/real_data_guard.py installs an audit hook for the whole suite (from
conftest). These tests prove it without going near real data:
- the protected-path predicate is checked on path STRINGS only;
- every access channel is exercised against a decoy file in tmp_path that is
  temporarily treated as protected.
The real backend/kite_terminal.db* files are never opened, statted or
hashed here.
"""

import io
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

import real_data_guard
from real_data_guard import REAL_DB_DIR, RealAccountDataAccess, is_protected, protect_also

TESTS = Path(__file__).resolve().parent


def test_the_guard_is_installed_for_the_whole_suite():
    assert real_data_guard.installed()


@pytest.mark.parametrize("name", [
    "kite_terminal.db", "kite_terminal.db-wal", "kite_terminal.db-shm", "kite_terminal.db.pre_pk22_bak",
    "kite_terminal.db.pre_pk18_bak", "kite_terminal.db.pre_schema_v6_bak", "kite_terminal.db.pre_schema_v6_bak.2026",
])
def test_the_real_database_and_every_backup_are_protected(name):
    path = REAL_DB_DIR / name
    assert is_protected(path) and is_protected(str(path))
    assert is_protected(f"file:{path.as_posix()}?mode=ro")             # a SQLite URI
    assert is_protected(f"file:///{path.as_posix()}?mode=ro&immutable=1")
    if os.name == "nt":
        assert is_protected(str(path).upper())                           # case-insensitive file system


@pytest.mark.parametrize("path", [":memory:", "", "file::memory:?cache=shared"])
def test_in_memory_databases_are_not_protected(path):
    assert not is_protected(path)


def test_other_files_are_not_protected(tmp_path):
    for path in (tmp_path / "kite_terminal.db", REAL_DB_DIR / "db.py", REAL_DB_DIR / "other.db",
                 REAL_DB_DIR / "calendar_data" / "kite_terminal.db"):
        assert not is_protected(path), path


@pytest.fixture
def decoy(tmp_path):
    """A harmless file the guard temporarily treats exactly like the real
    backup."""
    path = tmp_path / "decoy.db"
    sqlite3.connect(path).close()          # created BEFORE it is protected
    with protect_also(path):
        yield path
    assert not is_protected(path)


@pytest.mark.parametrize("channel", [
    lambda p: open(p, "rb"),
    lambda p: io.open(p, "rb"),
    lambda p: os.open(p, os.O_RDONLY),
    lambda p: Path(p).read_bytes(),
    lambda p: shutil.copyfile(p, Path(p).with_suffix(".copy")),
    lambda p: shutil.copy(p, Path(p).with_suffix(".copy2")),
    lambda p: sqlite3.connect(p),
    lambda p: sqlite3.connect(str(p)),
    lambda p: sqlite3.connect(f"file:{Path(p).as_posix()}?mode=ro", uri=True),
], ids=["open", "io.open", "os.open", "read_bytes", "copyfile", "copy", "sqlite_path", "sqlite_str", "sqlite_uri"])
def test_every_access_channel_is_refused_before_the_file_is_touched(decoy, channel):
    with pytest.raises(RealAccountDataAccess, match="Test isolation"):
        channel(decoy)
    assert not decoy.with_suffix(".copy").exists() and not decoy.with_suffix(".copy2").exists()


def test_the_backend_database_path_of_the_app_is_refused(monkeypatch):
    """If any test forgot to point backend.db at a temporary file, the app's
    own connection attempt is refused before SQLite opens anything."""
    from backend import db
    real_default = REAL_DB_DIR / "kite_terminal.db"
    assert is_protected(real_default)
    monkeypatch.setattr(db, "DB_PATH", real_default)
    with pytest.raises(RealAccountDataAccess):
        db.get_connection()


def test_the_env_fixture_works_on_a_temporary_database(env):
    assert not is_protected(env.db.DB_PATH)
    assert Path(env.db.DB_PATH).parent == env.tmp_path


def test_no_test_refers_to_the_real_backup_file():
    """The migration-compatibility test uses tests/fixtures/pre_pk22_schema.sql,
    never a real backup. Only the guard and its tests may name it. This static check also
    covers what the audit hook cannot see: a child process given the path."""
    allowed = {"real_data_guard.py", "test_real_data_guard.py"}
    offenders = [p.name for p in TESTS.rglob("*.py")
                 if p.name not in allowed and "pre_pk22_bak" in p.read_text(encoding="utf-8")]
    assert offenders == []
