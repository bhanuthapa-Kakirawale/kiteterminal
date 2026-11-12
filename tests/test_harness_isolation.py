"""Phase 0 / F-0: the test harness never reads the real .env, never runs on
real Kite configuration, never opens the real account database and never
leaves loopback.

Everything here uses decoy files in tmp_path and sentinel values. The real
.env, backend/kite_terminal.db* and backend/ui_prefs.json are only ever
named as path STRINGS (tests/real_data_guard.is_protected), never opened,
statted or hashed.
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import real_data_guard
from real_data_guard import (
    REAL_DB_DIR, REPO, ExternalNetworkAccess, RealAccountDataAccess, is_protected, protect_also,
)

TESTS = Path(__file__).resolve().parent
SENSITIVE = ("KITE_API_KEY", "KITE_API_SECRET", "KITE_TERMINAL_ENCRYPTION_KEY", "KITE_REST_PROXY")


# ============================================================ C1: the real .env is never loaded
def test_python_dotenv_is_neutralised_for_the_whole_suite():
    import dotenv
    import dotenv.main
    assert real_data_guard.dotenv_neutralised()
    assert real_data_guard.is_guarded_loader(dotenv.load_dotenv)
    assert real_data_guard.is_guarded_loader(dotenv.main.load_dotenv)
    assert real_data_guard.is_guarded_loader(dotenv.dotenv_values)


def test_backend_main_bound_the_guarded_loader_at_import(env):
    """backend/main.py does `from dotenv import load_dotenv; load_dotenv()` at import. The name
    it bound is the guard's stub, so the call it made in this process loaded nothing."""
    import backend.main
    assert real_data_guard.is_guarded_loader(backend.main.load_dotenv)
    implicit = [c for c in real_data_guard.dotenv_calls() if c["mode"] == "implicit"]
    assert implicit and all(c["result"] == "neutralised" for c in implicit)


def test_implicit_discovery_loads_nothing_even_with_a_dotenv_in_the_working_directory(tmp_path, monkeypatch):
    import dotenv
    (tmp_path / ".env").write_text("KITE_API_KEY=decoy-must-not-load\nKITE_PHASE0_DECOY=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    with real_data_guard.recording() as seen:
        assert dotenv.load_dotenv() is False
        assert dotenv.load_dotenv(override=True) is False
        assert dotenv.dotenv_values() == {}
    assert "KITE_API_KEY" not in os.environ and "KITE_PHASE0_DECOY" not in os.environ
    assert not [p for _, p in seen if os.path.basename(p).startswith(".env")]


def test_an_explicit_protected_path_is_refused_before_it_is_opened(tmp_path, monkeypatch):
    import dotenv
    decoy = tmp_path / ".env"
    decoy.write_text("KITE_PHASE0_DECOY=1\n", encoding="utf-8")
    monkeypatch.delenv("KITE_PHASE0_DECOY", raising=False)
    with protect_also(decoy), real_data_guard.recording() as seen:
        with pytest.raises(RealAccountDataAccess, match="Test isolation"):
            dotenv.load_dotenv(decoy)
        with pytest.raises(RealAccountDataAccess, match="Test isolation"):
            dotenv.dotenv_values(str(decoy))
    assert "KITE_PHASE0_DECOY" not in os.environ
    assert not [p for _, p in seen if os.path.basename(p) == ".env"]


def test_an_explicit_temporary_file_still_loads(tmp_path, monkeypatch):
    """Delegation is kept for unprotected paths (db_maintenance tests point crypto.ENV_PATH at
    temporary files)."""
    import dotenv
    path = tmp_path / "scratch.env"
    path.write_text("KITE_PHASE0_SENTINEL=sentinel-value\n", encoding="utf-8")
    monkeypatch.setenv("KITE_PHASE0_SENTINEL", "placeholder")   # so teardown restores "absent"
    monkeypatch.delenv("KITE_PHASE0_SENTINEL")
    assert dotenv.load_dotenv(path) is True
    assert os.environ["KITE_PHASE0_SENTINEL"] == "sentinel-value"


# ============================================================ C2: no real Kite credentials
def test_no_inherited_kite_configuration_outside_the_env_fixture():
    leaked = sorted(k for k in os.environ if k.upper().startswith("KITE_"))
    assert leaked == []
    for name in SENSITIVE:
        assert name not in os.environ


def test_the_env_fixture_runs_on_sentinel_values_only(env):
    assert os.environ["KITE_API_KEY"] == "test_api_key"
    assert os.environ["KITE_API_SECRET"] == "test_api_secret"
    from conftest import TEST_REST_PROXY
    assert os.environ["KITE_REST_PROXY"] == TEST_REST_PROXY
    from backend import crypto, ui_prefs
    assert Path(crypto.ENV_PATH).parent == env.tmp_path and not is_protected(crypto.ENV_PATH)
    assert Path(ui_prefs.PREFS_PATH).parent == env.tmp_path and not is_protected(ui_prefs.PREFS_PATH)


def test_without_env_the_key_and_prefs_files_are_session_temporaries(_session_isolated_files):
    from backend import crypto, ui_prefs
    assert Path(crypto.ENV_PATH).parent == _session_isolated_files
    assert Path(ui_prefs.PREFS_PATH).parent == _session_isolated_files
    assert not is_protected(crypto.ENV_PATH) and not is_protected(ui_prefs.PREFS_PATH)


def test_scrubbing_removes_kite_variables_and_keeps_names_only(monkeypatch):
    monkeypatch.setenv("KITE_PHASE0_SCRUB_SENTINEL", "sentinel-value")
    monkeypatch.setenv("UNRELATED_PHASE0_SENTINEL", "kept")
    removed = real_data_guard.scrub_kite_environment()
    assert removed == ["KITE_PHASE0_SCRUB_SENTINEL"]
    assert "KITE_PHASE0_SCRUB_SENTINEL" not in os.environ
    assert os.environ["UNRELATED_PHASE0_SENTINEL"] == "kept"
    assert "KITE_PHASE0_SCRUB_SENTINEL" in real_data_guard.scrubbed_names()
    assert "sentinel-value" not in repr(real_data_guard.scrubbed_names())


# ============================================================ C3: only the temporary database
def test_database_work_touches_only_the_temporary_database(env):
    with real_data_guard.recording() as seen:
        conn = env.db.get_connection()
        try:
            conn.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()
        finally:
            conn.close()
        assert env.client.get("/api/tickets").status_code == 200
        assert env.client.get("/api/orders").status_code == 200
    connects = [p for event, p in seen if event == "sqlite3.connect"]
    assert connects, "the requests above must have used the database"
    for path in connects:
        assert Path(path).resolve().parent == env.tmp_path.resolve(), path
        assert not is_protected(path)
    assert not [p for _, p in seen if is_protected(p)]


# ============================================================ C4: protected paths are refused
@pytest.mark.parametrize("path", [
    REPO / ".env", REPO / ".env.local", REPO / ".env.production", REPO / ".env.bak", REPO / "backend" / ".env",
    REAL_DB_DIR / "ui_prefs.json",
])
def test_the_real_configuration_is_protected(path):
    assert is_protected(path) and is_protected(str(path))
    if os.name == "nt":
        assert is_protected(str(path).upper())


@pytest.mark.parametrize("path", [
    REPO / ".env.example", REPO / "docs" / ".env", REPO / "tests" / ".env", REPO / "backend" / "envx",
    REPO / "backend" / "ui_prefs.py",
])
def test_templates_and_other_paths_are_not_protected(path):
    assert not is_protected(path)


def test_a_temporary_dotenv_is_not_protected(tmp_path):
    assert not is_protected(tmp_path / ".env")


@pytest.mark.parametrize("channel", [
    lambda p: open(p, "rb"),
    lambda p: open(p, "a", encoding="utf-8"),          # crypto.ensure_encryption_key appends
    lambda p: Path(p).read_text(encoding="utf-8"),
    lambda p: os.open(p, os.O_RDONLY),
], ids=["read", "append", "read_text", "os.open"])
def test_every_channel_to_a_protected_dotenv_is_refused(tmp_path, channel):
    decoy = tmp_path / ".env"
    decoy.write_text("KITE_PHASE0_DECOY=1\n", encoding="utf-8")
    before = decoy.read_bytes()
    with protect_also(decoy), pytest.raises(RealAccountDataAccess, match="Test isolation"):
        channel(decoy)
    assert decoy.read_bytes() == before
    assert real_data_guard.refusals()[-1][2] is True          # logged as a decoy refusal


def test_the_real_database_path_is_refused_and_logged_as_expected(monkeypatch):
    from backend import db
    monkeypatch.setattr(db, "DB_PATH", REAL_DB_DIR / "kite_terminal.db")
    before = len(real_data_guard.unexpected_refusals())
    with real_data_guard.expected(), pytest.raises(RealAccountDataAccess):
        db.get_connection()
    kind, _, decoy, expected = real_data_guard.refusals()[-1]
    assert (kind, decoy, expected) == ("sqlite3.connect", False, True)
    assert len(real_data_guard.unexpected_refusals()) == before


def test_no_unexpected_refusal_so_far():
    """Anything refused outside expected() (e.g. swallowed by app code) fails the session too."""
    assert real_data_guard.unexpected_refusals() == []


# ============================================================ network: nothing beyond loopback
def test_an_outbound_connection_is_refused_before_it_is_made():
    with real_data_guard.expected(), pytest.raises(ExternalNetworkAccess, match="Test isolation"):
        socket.create_connection(("192.0.2.1", 9), timeout=1)      # TEST-NET-1, documentation only
    kind, target, _, expected = real_data_guard.refusals()[-1]
    assert kind == "network" and "192.0.2.1" in target and expected


def test_loopback_is_allowed():
    try:
        socket.create_connection(("127.0.0.1", 9), timeout=1).close()
    except ExternalNetworkAccess:
        pytest.fail("loopback must not be refused")
    except OSError:
        pass                                                        # nothing listens on the discard port


@pytest.mark.parametrize("address, local", [
    (("127.0.0.1", 80), True), (("127.8.9.10", 80), True), (("::1", 80, 0, 0), True), (("localhost", 80), True),
    (("192.0.2.1", 443), False), (("10.0.0.1", 443), False), (("api.kite.trade", 443), False),
    ("/tmp/socket", True),
])
def test_loopback_classification(address, local):
    assert real_data_guard.is_loopback_address(address) is local


# ============================================================ C5: importing the app has no side effects
_CHILD = r"""
import json, logging, os, sys
root, tests = sys.argv[1], sys.argv[2]
sys.path[:0] = [root, tests]
import real_data_guard
real_data_guard.install()
real_data_guard.neutralise_dotenv()
seen = {"env_opens": [], "db_opens": [], "sqlite_connects": [], "socket_connects": []}
def recorder(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        p = os.fsdecode(args[0]); base = os.path.basename(p).lower()
        if base.startswith(".env"):
            seen["env_opens"].append(p)
        if "kite_terminal.db" in base or base == "ui_prefs.json":
            seen["db_opens"].append(p)
    elif event == "sqlite3.connect":
        seen["sqlite_connects"].append(repr(args[0]) if args else "")
    elif event == "socket.connect":
        seen["socket_connects"].append(repr(args[1]) if len(args) > 1 else "")
sys.addaudithook(recorder)
import backend.main
seen["imported"] = "backend.main" in sys.modules
seen["dotenv_calls"] = real_data_guard.dotenv_calls()
seen["refusals"] = real_data_guard.refusals()
seen["kite_names"] = sorted(k for k in os.environ if k.upper().startswith("KITE_"))
seen["file_handlers"] = [type(h).__name__ for h in logging.getLogger().handlers
                         if isinstance(h, logging.FileHandler)]
print("PHASE0_RESULT " + json.dumps(seen))
"""


def test_importing_backend_main_reads_no_dotenv_and_opens_no_database():
    child_env = {k: v for k, v in os.environ.items() if not k.upper().startswith("KITE_")}
    child_env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, "-c", _CHILD, str(REPO), str(TESTS)], cwd=REPO, env=child_env,
                       capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert r.returncode == 0, r.stderr[-4000:]
    line = next(x for x in r.stdout.splitlines() if x.startswith("PHASE0_RESULT "))
    seen = json.loads(line[len("PHASE0_RESULT "):])
    assert seen["imported"]
    assert seen["env_opens"] == []
    assert seen["db_opens"] == []
    assert seen["sqlite_connects"] == []
    # Importing kiteconnect's ticker pulls in Twisted, whose reactor connects a socket to ITS OWN
    # loopback listener (a self-pipe "waker"). Nothing else connects, and nothing leaves loopback.
    assert len(seen["socket_connects"]) <= 1
    assert all(c.startswith("('127.0.0.1',") for c in seen["socket_connects"]), seen["socket_connects"]
    assert seen["refusals"] == []
    assert seen["kite_names"] == []
    assert seen["file_handlers"] == []
    # main.py's own load_dotenv() ran, through the stub, and loaded nothing.
    assert seen["dotenv_calls"] == [{"function": "load_dotenv", "mode": "implicit", "result": "neutralised"}]


# ============================================================ the demo server harness
def test_demo_server_neutralises_dotenv_before_any_backend_import():
    src = (TESTS / "demo_server.py").read_text(encoding="utf-8")
    neutralise = src.index("real_data_guard.neutralise_dotenv()")
    install = src.index("real_data_guard.install()")
    first_backend = min(i for i in (src.find("from backend"), src.find("import backend")) if i >= 0)
    assert install < first_backend and neutralise < first_backend
    assert neutralise < src.index("from fake_kite import")
