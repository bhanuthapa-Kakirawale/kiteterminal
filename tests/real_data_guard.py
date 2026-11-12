"""Test-suite isolation guard: no test may touch the real account data, the
real configuration, or the network beyond loopback.

Protected files (all of them hold real account data or secrets):
- backend/kite_terminal.db, its WAL/SHM files and its backups
  (kite_terminal.db.pre_pk22_bak, kite_terminal.db.pre_schema_vN_bak, ...);
- the real configuration: the repository's .env and any .env.* next to it
  (except the committed placeholder template .env.example), and backend/.env;
- backend/ui_prefs.json, the user's own UI preferences.
Every test works on a temporary database and temporary files instead.

conftest installs a process-wide audit hook (sys.addaudithook) the moment
the suite is imported. It raises RealAccountDataAccess BEFORE any of these
can reach a protected file:
- open() / io.open() / os.open() (event "open"), so also Path.read_bytes,
  shutil copies and anything else that opens through Python;
- sqlite3.connect, with a path or a file: URI;
- shutil.copyfile.
The same hook raises ExternalNetworkAccess for socket.connect to anything
but loopback (or a Unix socket): no test can reach Kite, or any other host.

Because the hook fires before the file is opened, the guard never reads a
protected file itself. tests/test_real_data_guard.py and
tests/test_harness_isolation.py prove each channel against decoy files in
tmp_path. Audit hooks cannot be removed, which is the point: no test can
switch the guard off. Every refusal is also logged (refusals()), so a refusal
that application code swallowed with a broad `except` still fails the
session (conftest.pytest_sessionfinish); a test that triggers one on purpose
wraps it in expected().

F-0 (docs/PROPOSAL_GTT_AFTER_FILL_LIFECYCLE_REVIEW.md): backend/main.py calls
load_dotenv() at import, which would read the real .env into the test
process. neutralise_dotenv() replaces python-dotenv's loaders before anything
imports backend.main: implicit discovery loads nothing, an explicit protected
path is refused, an explicit temporary file still loads.
scrub_kite_environment() removes every KITE_* variable the test process
inherited from the shell, so no test can consume real configuration that way
either.

Listing the backend directory or os.stat() of a path is not reading its
contents and is not intercepted.

Limit: an audit hook sees only this Python process. A CHILD process (a
subprocess the test starts) is outside it. On Windows its arguments even
arrive as one joined command string, and some tests legitimately pass these
paths to `git check-ignore`, which reads no file. Child processes are
therefore not intercepted. Instead, test_real_data_guard.py checks that no
test source names the real backup at all, and a child that imports the app
(test_harness_isolation.py, tests/demo_server.py) installs this guard itself.
"""

import ipaddress
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REAL_DB_DIR = REPO / "backend"
REAL_DB_PREFIX = "kite_terminal.db"
REAL_ENV_DIRS = (REPO, REPO / "backend")
ENV_NAME = ".env"
ENV_TEMPLATES = (".env.example",)          # committed placeholders, no secrets: tests read them
REAL_UI_PREFS = REAL_DB_DIR / "ui_prefs.json"
SCRUBBED_PREFIX = "KITE_"
# V12 final readiness, Phase C: the real installation registry (backend/installation_state.default_registry_dir,
# in the user profile). The suite points KT_INSTALLATION_REGISTRY_DIR at a temporary directory (conftest); nothing
# may read or write the real one. Computed here from LOCALAPPDATA, independent of that override.
REAL_REGISTRY_DIR = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share")) / "KiteTerminal"
REGISTRY_ENV = "KT_INSTALLATION_REGISTRY_DIR"


def configured_registry_dirs() -> list[Path]:
    """Post-V12 closure (2026-11-12): the operator's production registry directory, when it is configured as a
    persistent Windows environment variable (user or machine). Read from the Windows registry, NOT from this
    process's environment: conftest replaces the process value with a temporary directory, and child processes
    inherit that temporary value, which must stay writable. Relative or empty values are ignored (the application
    refuses them anyway)."""
    if os.name != "nt":
        return []
    import winreg
    found = []
    for hive, key in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _kind = winreg.QueryValueEx(handle, REGISTRY_ENV)
        except OSError:
            continue
        path = Path(os.path.expandvars(str(value))) if value else None
        if path is not None and path.is_absolute():
            found.append(path)
    return found


CONFIGURED_REGISTRY_DIRS = configured_registry_dirs()


class RealAccountDataAccess(PermissionError):
    """A test tried to touch the real account database, its backups, the real
    .env or the real UI preferences."""


class ExternalNetworkAccess(PermissionError):
    """A test tried to connect to a host other than loopback."""


# Decoy paths that tests of the guard itself temporarily treat as
# protected (see protect_also). Never real data.
_extra: set[str] = set()
_installed = False
# (kind, target, decoy, expected) for every refusal, in order.
_refusals: list[tuple[str, str, bool, bool]] = []
_expected_depth = 0
# recording(): (event, path) captured while active.
_recording: list[list[tuple[str, str]]] = []
# neutralise_dotenv(): what python-dotenv was asked to do.
_dotenv_calls: list[dict] = []
_dotenv_real: dict = {}
_scrubbed_names: list[str] = []

_URI_DRIVE = re.compile(r"^/+(?=[A-Za-z]:)")


def _normalised(path) -> str | None:
    if isinstance(path, int):
        return None  # an already-open file descriptor
    try:
        text = os.fsdecode(path)
    except TypeError:
        return None
    if text.startswith("file:"):  # a SQLite URI
        text = _URI_DRIVE.sub("", text[len("file:"):].split("?", 1)[0])
    if text in ("", ":memory:"):
        return None
    return os.path.normcase(os.path.abspath(text))


_DB_DIR = os.path.normcase(str(REAL_DB_DIR))
_ENV_DIRS = {os.path.normcase(str(d)) for d in REAL_ENV_DIRS}
_ENV_TEMPLATES = {os.path.normcase(t) for t in ENV_TEMPLATES}
_UI_PREFS = os.path.normcase(str(REAL_UI_PREFS))
_REGISTRY = os.path.normcase(str(REAL_REGISTRY_DIR))
_REGISTRIES = (_REGISTRY, *(os.path.normcase(os.path.abspath(p)) for p in CONFIGURED_REGISTRY_DIRS))


def _is_real_env(parent: str, name: str) -> bool:
    if parent not in _ENV_DIRS or name in _ENV_TEMPLATES:
        return False
    env = os.path.normcase(ENV_NAME)
    return name == env or name.startswith(env + ".")


def is_protected(path) -> bool:
    """Pure path logic: never touches the file system."""
    normalised = _normalised(path)
    if normalised is None:
        return False
    if normalised in _extra:
        return True
    parent, name = os.path.split(normalised)
    if parent == _DB_DIR and name.startswith(os.path.normcase(REAL_DB_PREFIX)):
        return True
    if any(normalised == r or normalised.startswith(r + os.sep) for r in _REGISTRIES):
        return True
    return _is_real_env(parent, name) or normalised == _UI_PREFS


def _is_decoy(path) -> bool:
    normalised = _normalised(path)
    return normalised is not None and normalised in _extra


# event -> how many leading path args. os.rename also covers os.replace; os.remove / os.mkdir (V12 final readiness):
# a protected file is never renamed, replaced or deleted, and nothing is created in the real registry.
_WATCHED = {"open": 1, "sqlite3.connect": 1, "shutil.copyfile": 2, "os.rename": 2, "os.remove": 1, "os.mkdir": 1}
# A protected path always contains one of these (lower-cased); a decoy is checked separately.
_NEEDLES = (REAL_DB_PREFIX.lower(), ENV_NAME, REAL_UI_PREFS.name.lower(), REAL_REGISTRY_DIR.name.lower(),
            *(p.name.lower() for p in CONFIGURED_REGISTRY_DIRS if p.name))


def _could_be_protected(candidate) -> bool:
    """Cheap pre-filter, so the hook does no path work for the thousands of
    unrelated opens a test run makes: a protected path always contains one of
    the protected file names, unless it is a decoy (only while a guard test
    runs)."""
    if _extra:
        return True
    if isinstance(candidate, int):
        return False
    try:
        text = os.fsdecode(candidate).lower()
    except TypeError:
        return False
    return any(needle in text for needle in _NEEDLES)


def _host_is_local(address) -> bool:
    if not isinstance(address, tuple):
        return True                       # AF_UNIX path (str/bytes): no network
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False                      # any other host name


def _refuse(kind: str, target: str, decoy: bool) -> None:
    _refusals.append((kind, target, decoy, _expected_depth > 0))


def _hook(event: str, args: tuple) -> None:
    if _recording and event in ("open", "sqlite3.connect") and args and args[0] is not None \
            and not isinstance(args[0], int):
        try:
            _recording[-1].append((event, os.fsdecode(args[0])))
        except TypeError:
            pass
    if event == "socket.connect":
        address = args[1] if len(args) > 1 else None
        if not _host_is_local(address):
            _refuse("network", repr(address), False)
            raise ExternalNetworkAccess(
                f"Test isolation: connection to {address!r} refused — the test suite never leaves loopback "
                f"(tests/real_data_guard.py).")
        return
    count = _WATCHED.get(event)
    if count is None:
        return
    for candidate in args[:count]:
        if candidate is not None and _could_be_protected(candidate) and is_protected(candidate):
            target = os.fsdecode(candidate)
            _refuse(event, target, _is_decoy(candidate))
            raise RealAccountDataAccess(
                f"Test isolation: {event} on {target!r} refused — the real account database, its backups, the "
                f"real .env and the real UI preferences are never read by the test suite "
                f"(tests/real_data_guard.py).")


def install() -> None:
    global _installed
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True


def installed() -> bool:
    return _installed


@contextmanager
def protect_also(path: Path):
    """Treat a (decoy) path as protected for the duration: lets the guard's
    own tests exercise every channel without going near the real file."""
    key = os.path.normcase(os.path.abspath(path))
    _extra.add(key)
    try:
        yield
    finally:
        _extra.discard(key)


@contextmanager
def expected():
    """Refusals inside this block are deliberate (a test of the guard)."""
    global _expected_depth
    _expected_depth += 1
    try:
        yield
    finally:
        _expected_depth -= 1


def refusals() -> list[tuple[str, str, bool, bool]]:
    return list(_refusals)


def unexpected_refusals() -> list[tuple[str, str, bool, bool]]:
    """Refusals of a REAL protected path or a real outbound connection that no
    test asked for: each one is an isolation failure, even if swallowed."""
    return [r for r in _refusals if not r[2] and not r[3]]


@contextmanager
def recording():
    """Collect (event, path) for every open / sqlite3.connect in the block."""
    seen: list[tuple[str, str]] = []
    _recording.append(seen)
    try:
        yield seen
    finally:
        _recording.remove(seen)


# ---------------------------------------------------------------- python-dotenv
def _guarded(name: str):
    def guarded(dotenv_path=None, stream=None, *args, **kwargs):
        if dotenv_path is None and stream is None:
            # Implicit discovery (find_dotenv) would walk up to the real .env.
            _dotenv_calls.append({"function": name, "mode": "implicit", "result": "neutralised"})
            return {} if name == "dotenv_values" else False
        if stream is None and is_protected(dotenv_path):
            _dotenv_calls.append({"function": name, "mode": "explicit", "result": "refused"})
            _refuse("dotenv", os.fsdecode(dotenv_path), _is_decoy(dotenv_path))
            raise RealAccountDataAccess(
                f"Test isolation: {name}({os.fsdecode(dotenv_path)!r}) refused — the real .env is never loaded "
                f"by the test suite (tests/real_data_guard.py).")
        _dotenv_calls.append({"function": name, "mode": "explicit", "result": "delegated"})
        return _dotenv_real[name](dotenv_path, stream, *args, **kwargs)
    guarded.__name__ = guarded.__qualname__ = f"guarded_{name}"
    guarded.real_data_guard = True
    return guarded


def neutralise_dotenv() -> None:
    """Replace python-dotenv's loaders (package and dotenv.main) with guarded
    ones. Must run before anything does `from dotenv import load_dotenv`."""
    import dotenv
    import dotenv.main
    for name in ("load_dotenv", "dotenv_values"):
        current = getattr(dotenv.main, name)
        if getattr(current, "real_data_guard", False):
            continue
        _dotenv_real[name] = current
        stub = _guarded(name)
        setattr(dotenv.main, name, stub)
        setattr(dotenv, name, stub)


def dotenv_neutralised() -> bool:
    import dotenv
    import dotenv.main
    return all(getattr(getattr(m, n), "real_data_guard", False)
               for m in (dotenv, dotenv.main) for n in ("load_dotenv", "dotenv_values"))


def is_guarded_loader(fn) -> bool:
    return bool(getattr(fn, "real_data_guard", False))


def dotenv_calls() -> list[dict]:
    return [dict(c) for c in _dotenv_calls]


# ---------------------------------------------------------------- environment
def scrub_kite_environment() -> list[str]:
    """Remove every KITE_* variable inherited by this process. Returns (and
    remembers) their NAMES only; values are never kept or printed."""
    names = sorted(k for k in os.environ if k.upper().startswith(SCRUBBED_PREFIX))
    for name in names:
        del os.environ[name]
    _scrubbed_names.extend(n for n in names if n not in _scrubbed_names)
    return names


def scrubbed_names() -> list[str]:
    return list(_scrubbed_names)


def is_loopback_address(address) -> bool:
    return _host_is_local(address)


__all__ = ["RealAccountDataAccess", "ExternalNetworkAccess", "is_protected", "install", "installed", "protect_also",
           "expected", "refusals", "unexpected_refusals", "recording", "neutralise_dotenv", "dotenv_neutralised",
           "is_guarded_loader", "dotenv_calls", "scrub_kite_environment", "scrubbed_names", "is_loopback_address"]
