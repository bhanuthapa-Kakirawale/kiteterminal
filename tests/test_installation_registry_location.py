"""The installation registry location under Microsoft Store Python (post-V12 closure, 2026-11-12).

Root cause (real V12 migration, D5/E5 deviation): .venv is built on Microsoft Store Python, an MSIX-packaged app.
Windows redirects a packaged app's writes under %LOCALAPPDATA% into the package's private LocalCache, so the registry
record's os.replace failed with WinError 17 and left a .tmp file there. The fix: a packaged interpreter never writes
the registry under AppData (refused before anything is created, with an actionable message); the operator names a
directory with KT_INSTALLATION_REGISTRY_DIR; the atomic write is hardened (unique temporary names, fsync, clean-up on
failure, a bounded retry on a sharing violation). Reads, the record beside the database and the start itself are
unchanged.

Every test runs on temporary files: a temporary database, a per-test registry directory and, where the default
location is exercised, a temporary stand-in for %LOCALAPPDATA%. The real registry, the real database and the network
stay behind tests/real_data_guard.py.
"""

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import real_data_guard
from backend import installation_state as ist

REPO = Path(__file__).resolve().parents[1]
UNAVAILABLE = "INSTALLATION_REGISTRY_UNAVAILABLE"


# ---------------------------------------------------------------------------------------------------- fixtures

@pytest.fixture
def registry(tmp_path, monkeypatch) -> Path:
    """A per-test registry directory (not yet created) named by the override, as an operator would set it."""
    directory = tmp_path / "registry_home" / "KiteTerminal" / "installations"
    monkeypatch.setenv(ist.REGISTRY_ENV, str(directory))
    return directory


@pytest.fixture
def database(tmp_path) -> Path:
    """An existing database made outside the terminal: user tables, no installation record yet (LEGACY_UNMARKED)."""
    folder = tmp_path / "install"
    folder.mkdir()
    path = folder / "kite_terminal_test.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def store_default(tmp_path, monkeypatch) -> Path:
    """Simulates Microsoft Store Python with NO override: a packaged process whose %LOCALAPPDATA% is a temporary
    stand-in (so the default location is never the real one). Returns the default registry directory."""
    fake_local = tmp_path / "fake_profile" / "AppData" / "Local"
    fake_temp = fake_local / "Temp"                            # the real layout: %TEMP% is inside %LOCALAPPDATA%
    fake_temp.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(fake_local))      # first, so the default can never be the real one
    monkeypatch.setenv("APPDATA", str(tmp_path / "fake_profile" / "AppData" / "Roaming"))
    for name in ("TEMP", "TMP"):
        monkeypatch.setenv(name, str(fake_temp))
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv(ist.REGISTRY_ENV, raising=False)
    monkeypatch.setattr(ist, "packaged_process", lambda: True)
    assert ist.registry_dir() == fake_local / "KiteTerminal" / "installations"
    return ist.registry_dir()


@pytest.fixture
def inst(tmp_path, monkeypatch):
    """backend.db on a temporary database in its own folder, with a key and sentinels (no .env written)."""
    from backend import db
    folder = tmp_path / "start"
    folder.mkdir()
    monkeypatch.setenv("KITE_TERMINAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("KITE_API_KEY", "test_api_key")
    monkeypatch.setenv("KITE_API_SECRET", "test_api_secret")
    monkeypatch.setattr(db, "DB_PATH", folder / "kite_terminal_test.db")
    return db


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if ".tmp" in p.name) if directory.exists() else []


# ================================================================ 1. normal creation

def test_r01_record_writes_both_records_identically_in_the_override_directory(registry, database):
    data = ist.record(database, by="test")
    reg, beside = ist.registry_file(database), ist.record_file(database)
    assert reg.parent == registry and reg.exists() and beside.exists()
    assert _json(reg) == _json(beside) == json.loads(json.dumps(data, default=str))
    assert _leftovers(registry) == [] and _leftovers(database.parent) == []
    assert ist.assess(database).state == ist.ESTABLISHED_VALID


# ================================================================ 2. existing registry update

def test_r02_an_update_keeps_the_identity_and_never_duplicates_the_record(registry, database):
    first = ist.record(database, by="first")
    second = ist.record(database, by="second")
    for key in ("install_id", "application_id", "created_at", "canonical_path"):
        assert second[key] == first[key]
    assert [h["by"] for h in second["history"]] == ["first", "second"]
    assert sorted(p.name for p in registry.iterdir()) == [ist.registry_file(database).name]   # one record per DB


# ================================================================ 3. missing directory

def test_r03_a_missing_registry_directory_is_created(registry, database):
    assert not registry.exists() and not registry.parent.exists()
    ist.record(database, by="test")
    assert registry.is_dir() and ist.registry_file(database).exists()


# ================================================================ 4. unwritable directory

def test_r04_an_unusable_directory_raises_an_actionable_error_and_leaves_nothing(tmp_path, monkeypatch, database):
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("a file where the registry directory should be")
    monkeypatch.setenv(ist.REGISTRY_ENV, str(blocker / "installations"))
    with pytest.raises(ist.InstallationRegistryUnavailable) as e:
        ist.record(database, by="test")
    message = str(e.value)
    assert message.startswith(UNAVAILABLE) and "NOT written" in message and ist.REGISTRY_ENV in message
    assert "python -m backend.installation_state --probe" in message
    assert isinstance(e.value, OSError)                                  # every caller's OSError handling applies
    assert ist.record_file(database).exists()                            # the record beside the DB is unaffected
    assert blocker.read_text() == "a file where the registry directory should be"


def test_r04b_a_persistent_access_denied_is_retried_briefly_then_reported(registry, database, monkeypatch):
    calls = []

    def denied(src, dst):
        calls.append(dst)
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(ist, "_REPLACE_DELAY_S", 0.0)
    ist.record_file(database).parent.mkdir(exist_ok=True)
    real_replace = os.replace
    monkeypatch.setattr(os, "replace",
                        lambda s, d: real_replace(s, d) if Path(d).parent != registry else denied(s, d))
    with pytest.raises(ist.InstallationRegistryUnavailable, match="PermissionError"):
        ist.record(database, by="test")
    assert len(calls) == ist._REPLACE_ATTEMPTS                           # bounded
    assert _leftovers(registry) == []                                    # the temporary file was removed


# ================================================================ 5. Microsoft Store Python

def test_r05_this_interpreter_is_classified_consistently_with_its_installation():
    store = f"{os.sep}windowsapps{os.sep}" in os.path.normcase(sys.base_prefix + os.sep)
    assert ist.packaged_process() is store


def test_r05b_the_fallback_classifies_by_the_installation_path(monkeypatch):
    import ctypes
    monkeypatch.setattr(ctypes, "windll", object(), raising=False)       # no API: the install path decides
    monkeypatch.setattr(sys, "base_prefix",
                        r"C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.13_3.13.1_x64__qbz5n2kfra8p0")
    assert ist.packaged_process() is True
    monkeypatch.setattr(sys, "base_prefix", r"C:\Users\someone\AppData\Local\Programs\Python\Python313")
    monkeypatch.setattr(sys, "_base_executable", r"C:\Users\someone\AppData\Local\Programs\Python\Python313\python.exe",
                        raising=False)
    assert ist.packaged_process() is False


def test_r05c_store_python_never_writes_the_redirected_default_and_says_why(store_default, database):
    problem = ist.registry_location_problem()
    assert problem and "Microsoft Store" in problem and "WinError 17" in problem
    with pytest.raises(ist.InstallationRegistryUnavailable) as e:
        ist.record(database, by="test")
    assert "Microsoft Store" in str(e.value) and ist.REGISTRY_ENV in str(e.value)
    assert not store_default.parent.exists()                             # nothing created under (fake) AppData
    assert ist.record_file(database).exists()                            # the record beside the DB was written


def test_r05d_a_python_org_interpreter_still_uses_the_default_location(store_default, database, monkeypatch):
    monkeypatch.setattr(ist, "packaged_process", lambda: False)
    assert ist.registry_location_problem() is None
    ist.record(database, by="test")
    assert ist.registry_file(database).parent == store_default and ist.registry_file(database).exists()


def test_r05e_store_python_refuses_an_override_under_appdata_but_not_the_temp_directory(store_default, tmp_path,
                                                                                        monkeypatch):
    roaming = Path(os.environ["APPDATA"]) / "KiteTerminal"
    monkeypatch.setenv(ist.REGISTRY_ENV, str(roaming))
    assert "Microsoft Store" in ist.registry_location_problem()
    monkeypatch.setenv(ist.REGISTRY_ENV, str(Path(os.environ["LOCALAPPDATA"]) / "Elsewhere"))
    assert "Microsoft Store" in ist.registry_location_problem()
    monkeypatch.setenv(ist.REGISTRY_ENV, str(Path(os.environ["TEMP"]) / "kt_registry_check"))
    assert ist.registry_location_problem() is None                       # %TEMP% is not virtualized
    monkeypatch.setenv(ist.REGISTRY_ENV, str(tmp_path / "home" / "KiteTerminal" / "installations"))
    assert ist.registry_location_problem() is None


def test_r05e2_the_redirection_is_caught_even_though_realpath_follows_it(store_default, tmp_path, monkeypatch,
                                                                        database):
    """The real shape (2026-11-12): for Store Python, os.path.realpath of %LOCALAPPDATA%\\KiteTerminal\\installations
    returns D:\\WpSystem\\<SID>\\AppData\\Local\\Packages\\<family>\\LocalCache\\Local\\KiteTerminal\\installations, a path
    that is no longer under %LOCALAPPDATA% (nor on its drive). A rule on the resolved path alone missed it."""
    wp = tmp_path / "D_WpSystem" / "S-1-5-21-x" / "AppData" / "Local" / "Packages" / \
        "PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0" / "LocalCache" / "Local"
    local = os.path.normcase(os.environ["LOCALAPPDATA"])
    real = os.path.realpath

    def msix_realpath(p):
        text = os.path.normcase(os.path.abspath(os.fspath(p)))
        if text.startswith(local + os.sep) and not text.startswith(os.path.normcase(os.environ["TEMP"])):
            return str(wp / os.path.relpath(text, local))
        return real(p)

    monkeypatch.setattr(ist, "_realpath", msix_realpath)
    assert "Microsoft Store" in ist.registry_location_problem()                  # the default
    # Each safeguard alone: the written form under AppData although the resolved one is neither under AppData nor in
    # a package store (e.g. a junction out of AppData): the lexical rule must still refuse it.
    elsewhere = tmp_path / "D_elsewhere"
    monkeypatch.setattr(ist, "_realpath", lambda p: str(elsewhere / Path(os.fspath(p)).name)
                        if os.path.normcase(os.path.abspath(os.fspath(p))).startswith(local + os.sep) else real(p))
    assert "Microsoft Store" in ist.registry_location_problem()
    # An override whose written form is outside AppData but which resolves into a package store (a junction, say):
    looks_fine = os.path.normcase(str(tmp_path / "looks_fine" / "installations"))
    monkeypatch.setattr(ist, "_realpath", lambda p: str(wp / "KiteTerminal" / "installations")
                        if os.path.normcase(os.path.abspath(os.fspath(p))) == looks_fine else real(p))
    monkeypatch.setenv(ist.REGISTRY_ENV, str(tmp_path / "looks_fine" / "installations"))
    problem = ist.registry_location_problem()
    assert problem and "Microsoft Store" in problem and "LocalCache" in problem
    with pytest.raises(ist.InstallationRegistryUnavailable):
        ist.record(database, by="test")
    assert not (tmp_path / "looks_fine").exists()


def test_r05e3_in_this_environment_the_real_default_is_refused_when_the_interpreter_is_store_python(monkeypatch):
    """Path logic only on the REAL default location (os.path.realpath; nothing is opened, created or read). This is
    the check that would have caught the realpath blind spot: it runs whenever the suite runs on Store Python."""
    if not ist.packaged_process():
        pytest.skip("not a Microsoft Store (MSIX-packaged) interpreter")
    monkeypatch.delenv(ist.REGISTRY_ENV)
    assert ist.registry_dir() == ist.default_registry_dir()
    problem = ist.registry_location_problem()
    assert problem and "Microsoft Store" in problem


def test_r05f_a_start_under_store_python_is_unaffected_and_logs_the_actionable_error(store_default, inst, caplog):
    path = Path(inst.DB_PATH)
    with caplog.at_level(logging.ERROR, logger="backend.installation_state"):
        inst.init_db()                                                   # a first run: creates and records the DB
    assert ist.record_file(path).exists() and not store_default.parent.exists()
    failures = [r.getMessage() for r in caplog.records if "installation.record_failed" in r.getMessage()]
    assert failures and UNAVAILABLE in failures[0] and "Microsoft Store" in failures[0]
    inst.init_db()                                                       # the next start is ESTABLISHED_VALID
    assert ist.assess(path).state == ist.ESTABLISHED_VALID


# ================================================================ 6. explicit override

@pytest.mark.parametrize("value, fragment", [
    ("relative\\registry", "not an absolute path"),
    ("C:relative_to_drive", "not an absolute path"),
])
def test_r06_a_relative_override_is_refused(monkeypatch, value, fragment):
    monkeypatch.setenv(ist.REGISTRY_ENV, value)
    assert fragment in ist.registry_location_problem()


def test_r06b_the_override_decides_where_records_are_read_and_written(registry, database, tmp_path, monkeypatch):
    ist.record(database, by="test")
    assert ist.assess(database).registry_record is not None
    monkeypatch.setenv(ist.REGISTRY_ENV, str(tmp_path / "other_registry"))
    assessment = ist.assess(database)
    assert assessment.registry_record is None and assessment.state == ist.ESTABLISHED_VALID   # beside-DB record


# ================================================================ 7. concurrency

def test_r07_concurrent_writers_and_readers_never_see_a_partial_record(tmp_path, registry):
    target = registry / "concurrent.json"
    errors, payloads = [], [{"writer": i, "pad": "x" * 4000} for i in range(8)]

    def write(payload):
        try:
            for _ in range(25):
                ist._write_registry(target, payload)
        except Exception as e:                                           # noqa: BLE001 - collected and asserted
            errors.append(repr(e))

    def read():
        try:
            for _ in range(200):
                if target.exists():
                    data = ist._read_json(target)
                    assert data in payloads, data
        except FileNotFoundError:
            pass
        except PermissionError:
            pass                                                         # a read racing a rename on Windows
        except Exception as e:                                           # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=write, args=(p,)) for p in payloads] + \
              [threading.Thread(target=read) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    assert _json(target) in payloads and _leftovers(registry) == []


def test_r07b_concurrent_processes_writing_the_same_record(registry):
    target = registry / "multi_process.json"
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; "
            "from backend import installation_state as ist; "
            "[ist._write_registry(Path(sys.argv[2]), {'writer': int(sys.argv[3]), 'n': n}) for n in range(40)]")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(REPO), str(target), str(i)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(4)]
    results = [p.communicate(timeout=120) + (p.returncode,) for p in procs]
    assert all(rc == 0 for _o, _e, rc in results), results
    final = _json(target)
    assert final["writer"] in range(4) and final["n"] == 39 and _leftovers(registry) == []


def test_r07c_concurrent_record_updates_keep_one_identity(registry, database):
    first = ist.record(database, by="initial")
    errors = []

    def update(i):
        try:
            ist.record(database, by=f"concurrent-{i}")
        except Exception as e:                                           # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=update, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    for p in (ist.registry_file(database), ist.record_file(database)):
        data = _json(p)
        assert (data["install_id"], data["application_id"], data["created_at"]) == \
               (first["install_id"], first["application_id"], first["created_at"])
    assert sorted(p.name for p in registry.iterdir()) == [ist.registry_file(database).name]


# ================================================================ 8. interrupted atomic write

def test_r08_a_failed_rename_keeps_the_previous_record_and_removes_the_temporary(registry, database, monkeypatch):
    ist.record(database, by="before")
    before = ist.registry_file(database).read_bytes()
    real_replace = os.replace

    def cross_device(src, dst):
        if Path(dst).parent == registry:
            raise OSError(17, "The system cannot move the file to a different disk drive")   # WinError 17's text
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", cross_device)
    with pytest.raises(ist.InstallationRegistryUnavailable, match="different disk drive"):
        ist.record(database, by="after")
    assert ist.registry_file(database).read_bytes() == before and _leftovers(registry) == []


def test_r08b_an_interruption_between_write_and_rename_leaves_nothing(registry, database, monkeypatch):
    ist.record(database, by="before")
    before = ist.registry_file(database).read_bytes()

    def interrupted(src, dst):
        raise KeyboardInterrupt

    monkeypatch.setattr(ist, "_replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        ist._write_registry(ist.registry_file(database), {"partial": True})
    assert ist.registry_file(database).read_bytes() == before and _leftovers(registry) == []


def test_r08c_a_temporary_left_by_a_hard_crash_is_never_taken_as_a_record(registry, database):
    ist.record(database, by="before")
    crash = registry / f"{ist.registry_file(database).name}.tmp4242_deadbeef"
    crash.write_text('{"canonical_path": "C:\\\\elsewhere\\\\kite_terminal_test.db"', encoding="utf-8")   # truncated
    assert ist.assess(database).state == ist.ESTABLISHED_VALID
    assert ist.other_installations(database) == []
    ist.record(database, by="after")
    assert crash.exists()                                                # never deleted by the terminal


# ================================================================ 9. malformed registry record

def test_r09_a_malformed_registry_record_fails_closed_until_acknowledged(registry, database):
    ist.record(database, by="before")
    ist.registry_file(database).write_text("{ not json", encoding="utf-8")
    assert ist.assess(database).state == ist.RECORD_UNREADABLE                 # unchanged: fail closed
    assert ist.check_registry(database)["verdict"] == "RECORD_UNREADABLE"
    ist.registry_file(database).write_text("[1, 2]", encoding="utf-8")         # JSON, but not an object
    assert ist.assess(database).state == ist.RECORD_UNREADABLE


# ================================================================ 10. identity preserved across the fix

def test_r10_setting_the_override_later_copies_the_existing_identity_into_the_registry(store_default, database,
                                                                                       tmp_path, monkeypatch):
    """The production transition: records written while the registry was refused (beside-DB only), then the operator
    sets KT_INSTALLATION_REGISTRY_DIR and the next write fills the registry with the SAME installation identity."""
    with pytest.raises(ist.InstallationRegistryUnavailable):
        ist.record(database, by="migrate-v12")
    beside = _json(ist.record_file(database))
    target = tmp_path / "userprofile" / "KiteTerminal" / "installations"
    monkeypatch.setenv(ist.REGISTRY_ENV, str(target))
    assert ist.check_registry(database)["verdict"] == "NOT_WRITTEN"
    ist.record(database, by="startup")
    reg = _json(ist.registry_file(database))
    for key in ("install_id", "application_id", "created_at", "canonical_path"):
        assert reg[key] == beside[key]
    assert ist.check_registry(database, probe=True)["verdict"] == "OK"
    assert ist.assess(database).state == ist.ESTABLISHED_VALID


def test_r10b_a_registry_record_of_another_installation_is_reported_as_a_mismatch(registry, database):
    ist.record(database, by="test")
    data = _json(ist.registry_file(database))
    data["install_id"] = "0" * 32
    ist.registry_file(database).write_text(json.dumps(data), encoding="utf-8")
    assert ist.check_registry(database)["verdict"] == "MISMATCH"


# ================================================================ 11. never the repository or the Python installation

def test_r11_an_override_inside_the_repository_is_refused_before_anything_is_created(monkeypatch, database):
    inside = REPO / "output" / "kt_registry_must_not_exist"
    monkeypatch.setenv(ist.REGISTRY_ENV, str(inside))
    assert "inside the repository" in ist.registry_location_problem()
    with pytest.raises(ist.InstallationRegistryUnavailable, match="inside the repository"):
        ist.record(database, by="test")
    assert not inside.exists()


def test_r11b_an_override_inside_the_python_installation_is_refused(monkeypatch, database):
    for base in (sys.base_prefix, sys.prefix, os.path.dirname(sys.executable)):
        inside = Path(base) / "kt_registry_must_not_exist"
        monkeypatch.setenv(ist.REGISTRY_ENV, str(inside))
        problem = ist.registry_location_problem()
        assert problem and ("Python installation" in problem or "inside the repository" in problem), (base, problem)
        with pytest.raises(ist.InstallationRegistryUnavailable):
            ist.record(database, by="test")
        assert not inside.exists()


def test_r11c_a_normal_write_opens_files_only_in_the_temporary_directories(registry, database, tmp_path):
    with real_data_guard.recording() as seen:
        ist.record(database, by="test")
    root = os.path.normcase(os.path.realpath(tmp_path))
    assert seen and all(os.path.normcase(os.path.realpath(p)).startswith(root) for _event, p in seen), seen


def test_r11d_the_location_rules_create_and_open_nothing(store_default, monkeypatch):
    """Regression (full run 2026-11-12, H15): tempfile.gettempdir()'s first call creates a probe file in %TEMP%. The
    rules must stay pure path logic, so they read the temp directories from the environment."""
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", None)                      # a fresh process: nothing cached
    for packaged in (True, False):
        monkeypatch.setattr(ist, "packaged_process", lambda p=packaged: p)
        with real_data_guard.recording() as seen:
            ist.registry_location_problem()
        assert seen == [], seen
    assert tempfile.tempdir is None                                      # gettempdir() was never called


def test_r08d_the_temporary_name_is_short_and_never_reuses_a_leftover(registry, monkeypatch):
    import itertools
    target = registry / "short.json"
    registry.mkdir(parents=True)
    monkeypatch.setattr(ist, "_TEMPORARY_SEQUENCE", itertools.count(5))
    leftover = registry / f"short.json.tmp{os.getpid()}.5"
    leftover.write_text("a crashed process with the same pid", encoding="utf-8")
    ist._atomic_write_json(target, {"ok": True})
    assert _json(target) == {"ok": True}
    assert leftover.read_text(encoding="utf-8") == "a crashed process with the same pid"   # skipped, not overwritten
    assert sorted(p.name for p in registry.iterdir()) == sorted(["short.json", leftover.name])


# ================================================================ 12. never the production database

def test_r12_the_check_never_opens_the_database(registry, database, capsys):
    ist.record(database, by="test")
    with real_data_guard.recording() as seen:
        code = ist.main(["--db", str(database), "--probe"])
    out = capsys.readouterr().out
    assert code == 0 and "verdict:" in out and "OK" in out
    assert not [p for event, p in seen if event == "sqlite3.connect"]
    assert os.path.normcase(str(database)) not in {os.path.normcase(p) for _e, p in seen}
    assert not list(registry.glob("kt_registry_probe_*"))                # the probe file was removed


def test_r12b_the_check_reports_a_refused_location_with_the_remedy(store_default, database, capsys):
    assert ist.main(["--db", str(database), "--probe"]) == 1
    out = capsys.readouterr().out
    assert "LOCATION_REFUSED" in out and "NOT RUN" in out and ist.REGISTRY_ENV in out
    assert not store_default.parent.exists()


def test_r12c_the_default_database_of_the_check_is_the_terminals_and_is_protected():
    from backend import db
    assert ist.DEFAULT_DB == Path(db.DB_PATH).resolve()
    assert real_data_guard.is_protected(ist.record_file(ist.DEFAULT_DB))  # tests can never read the real record


# ================================================================ test guard: the operator's configured registry

def test_g01_a_configured_production_registry_is_read_from_the_persistent_environment(monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows registry")
    import winreg

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    values = {winreg.HKEY_CURRENT_USER: r"%USERPROFILE%\KiteTerminal\installations",
              winreg.HKEY_LOCAL_MACHINE: "relative\\ignored"}
    monkeypatch.setattr(winreg, "OpenKey", lambda hive, key: (setattr(Key, "hive", hive), Key())[1])
    monkeypatch.setattr(winreg, "QueryValueEx", lambda handle, name: (values[Key.hive], winreg.REG_EXPAND_SZ))
    found = real_data_guard.configured_registry_dirs()
    assert found == [Path(os.path.expandvars(r"%USERPROFILE%\KiteTerminal\installations"))]


def test_g02_every_configured_production_registry_is_protected():
    for directory in real_data_guard.CONFIGURED_REGISTRY_DIRS:
        assert real_data_guard.is_protected(directory / "f335dc6e.json")
    assert not real_data_guard.is_protected(Path(os.environ[ist.REGISTRY_ENV]) / "x.json")   # the session's temp one
