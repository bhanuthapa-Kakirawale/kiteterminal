"""Test-suite isolation guard: no test may touch the real account database.

The real database lives at backend/kite_terminal.db. Next to it are its
WAL/SHM files and its backups (kite_terminal.db.pre_pk22_bak,
kite_terminal.db.pre_schema_vN_bak, ...). All of them hold real account
data. Every test works on a temporary database instead.

conftest installs a process-wide audit hook (sys.addaudithook) the moment
the suite is imported. It raises RealAccountDataAccess BEFORE any of these
can reach a protected file:
- open() / io.open() / os.open() (event "open"), so also Path.read_bytes,
  shutil copies and anything else that opens through Python;
- sqlite3.connect, with a path or a file: URI;
- shutil.copyfile.

Because the hook fires before the file is opened, the guard never reads a
protected file itself. tests/test_real_data_guard.py proves each channel
against a decoy file in tmp_path. Audit hooks cannot be removed, which is
the point: no test can switch the guard off.

Listing the backend directory or os.stat() of a path is not reading its
contents and is not intercepted.

Limit: an audit hook sees only this Python process. A CHILD process (a
subprocess the test starts) is outside it. On Windows its arguments even
arrive as one joined command string, and some tests legitimately pass these
paths to `git check-ignore`, which reads no file. Child processes are
therefore not intercepted. Instead, test_real_data_guard.py checks that no
test source names the real backup at all.
"""

import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REAL_DB_DIR = REPO / "backend"
REAL_DB_PREFIX = "kite_terminal.db"

# Decoy paths that tests of the guard itself temporarily treat as
# protected (see protect_also). Never real data.
_extra: set[str] = set()
_installed = False

_URI_DRIVE = re.compile(r"^/+(?=[A-Za-z]:)")


class RealAccountDataAccess(PermissionError):
    """A test tried to touch the real account database or one of its backups."""


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


def is_protected(path) -> bool:
    """Pure path logic: never touches the file system."""
    normalised = _normalised(path)
    if normalised is None:
        return False
    if normalised in _extra:
        return True
    parent, name = os.path.split(normalised)
    return (parent == os.path.normcase(str(REAL_DB_DIR))
            and name.startswith(os.path.normcase(REAL_DB_PREFIX)))


_WATCHED = {"open": 1, "sqlite3.connect": 1, "shutil.copyfile": 2}  # event -> how many leading path args
_PREFIX_LOWER = REAL_DB_PREFIX.lower()


def _could_be_protected(candidate) -> bool:
    """Cheap pre-filter, so the hook does no path work for the thousands of
    unrelated opens a test run makes: a protected path always contains the
    file name prefix, unless it is a decoy (only while a guard test runs)."""
    if _extra:
        return True
    if isinstance(candidate, int):
        return False
    try:
        return _PREFIX_LOWER in os.fsdecode(candidate).lower()
    except TypeError:
        return False


def _hook(event: str, args: tuple) -> None:
    count = _WATCHED.get(event)
    if count is None:
        return
    for candidate in args[:count]:
        if candidate is not None and _could_be_protected(candidate) and is_protected(candidate):
            raise RealAccountDataAccess(
                f"Test isolation: {event} on {os.fsdecode(candidate)!r} refused — the real account database and "
                f"its backups are never read by the test suite (tests/real_data_guard.py).")


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
