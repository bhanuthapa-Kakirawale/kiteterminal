"""Installation identity: an intentional fresh install versus an established installation whose database went missing,
was emptied, replaced or moved (V12 final readiness, Phase C).

User decision (2026-11-11, V12 final readiness): fresh installs are EVIDENCE-BASED. A normal start may still create
the database at the latest schema on a true first run, but only when nothing shows that an installation existed
before. Evidence of a prior installation:
  * the installation record beside the database (<db>.installation.json);
  * the installation REGISTRY record, kept in the user profile (%LOCALAPPDATA%\\KiteTerminal\\installations\\<hash of
    the canonical database path>.json), so it survives the deletion of the database, of its directory or of the whole
    repository folder (overridable with KT_INSTALLATION_REGISTRY_DIR; the test suite points it at a temporary dir).
    Under Microsoft Store (MSIX-packaged) Python the default location is redirected by Windows and is never written:
    KT_INSTALLATION_REGISTRY_DIR must name a directory outside AppData (see "the registry location" below and
    docs/INSTALLATION_REGISTRY.md);
  * any sibling file the terminal or its maintenance commands leave beside an established database: <db>-wal/-shm/
    -journal, <db>.*_bak*, <db>.*_orig*, <db>.*_audit_*.json, <db>.failed_restore_*, <db>.restore_staging_*
    (checked by NAME only, never opened). NOT <db>.instance.lock: the instance lock creates it before init_db on
    every start, a first run included;
  * an in-progress marker of migrate-v12 or restore-backup;
  * ANOTHER installation in this profile's registry whose database (same file name) still exists in another folder
    (the repository copied or moved without its database): OTHER_INSTALLATION_PRESENT.
A zero-byte file or a SQLite file with no table is NEVER taken as a fresh install (it is what a truncated or
re-created file looks like). `python -m backend.db_maintenance init-fresh --confirm-new-installation` is the explicit
way to create a new database where evidence exists.

The record also binds the database's identity: the file id (volume serial + NTFS file index, os.stat st_dev/st_ino;
a file replaced by rename, or deleted and re-created, gets a new one), SQLite's header application_id (a random
per-installation value the terminal stamps after a successful start; a foreign database copied over the file has a
different one) and the newest migration seen (a database older than the last one seen was rolled back by hand).
restore-backup and migrate-v12 re-bind the record themselves; `db_maintenance acknowledge-database --confirm`
re-binds it after an operator has inspected a deliberate replacement or move.

Limits (reported, not hidden): an older backup OF THE SAME INSTALLATION copied over the file in place (same file id,
same application_id, same schema version) is not detected; deleting the database AND every sibling file AND the
registry record (a wiped user profile on a wiped folder) is indistinguishable from a first run.

Records hold no account data: ids, paths, sizes, timestamps, schema version names.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import itertools
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

REGISTRY_ENV = "KT_INSTALLATION_REGISTRY_DIR"
RECORD_SUFFIX = ".installation.json"
MIGRATION_MARKER_SUFFIX = ".v12_migration_in_progress.json"
RESTORE_MARKER_SUFFIX = ".restore_in_progress.json"
IN_PROGRESS_SUFFIXES = (MIGRATION_MARKER_SUFFIX, RESTORE_MARKER_SUFFIX)
FORMAT = 1

# States
FRESH_INSTALL_PERMITTED = "FRESH_INSTALL_PERMITTED"
ESTABLISHED_VALID = "ESTABLISHED_VALID"
LEGACY_UNMARKED = "LEGACY_UNMARKED"
ESTABLISHED_MISSING = "ESTABLISHED_MISSING"
ZERO_BYTE = "ZERO_BYTE"
EMPTY_SQLITE = "EMPTY_SQLITE"
CORRUPT = "CORRUPT"
IDENTITY_CHANGED = "IDENTITY_CHANGED"
SCHEMA_REGRESSED = "SCHEMA_REGRESSED"
PATH_CHANGED = "PATH_CHANGED"
INTERRUPTED_OPERATION = "INTERRUPTED_OPERATION"
RECORD_UNREADABLE = "RECORD_UNREADABLE"
HOT_JOURNAL = "HOT_JOURNAL"
OTHER_INSTALLATION_PRESENT = "OTHER_INSTALLATION_PRESENT"

PERMITTED_STATES = frozenset({FRESH_INSTALL_PERMITTED, ESTABLISHED_VALID, LEGACY_UNMARKED})

_REMEDIATION = {
    ESTABLISHED_MISSING: "The database of an established installation is missing. Do NOT start the terminal (it would "
                         "create an empty database). Restore the newest verified backup with `python -m "
                         "backend.db_maintenance restore-backup --backup FILE --confirm` (runbook: recovery, 'database "
                         "missing'). Only if this really is a new installation: `python -m backend.db_maintenance "
                         "init-fresh --confirm-new-installation --acknowledge-prior-installation`.",
    ZERO_BYTE: "The database file is zero bytes (truncated or re-created). Keep it aside, restore the newest verified "
               "backup with restore-backup. It is never treated as a fresh install.",
    EMPTY_SQLITE: "The database file is a SQLite file with no table (re-created or emptied). Keep it aside, restore "
                  "the newest verified backup with restore-backup. It is never treated as a fresh install.",
    CORRUPT: "The database file is not a readable SQLite database. Do not delete it. Keep it, restore the newest "
             "verified backup with restore-backup, and inspect the damaged copy separately.",
    IDENTITY_CHANGED: "The database file is not the one this installation last used (replaced, re-created, or a "
                      "foreign file). If you replaced it deliberately and have inspected it, run `python -m "
                      "backend.db_maintenance acknowledge-database --confirm`; otherwise restore a verified backup.",
    SCHEMA_REGRESSED: "The database is at an older schema than this installation last used (an old copy was put in "
                      "place). Restore the newest verified backup with restore-backup, or acknowledge-database "
                      "--confirm after inspecting it.",
    PATH_CHANGED: "The database was moved or copied from another location. If the move was deliberate, run `python "
                  "-m backend.db_maintenance acknowledge-database --confirm` after checking it is the right file.",
    INTERRUPTED_OPERATION: "A migrate-v12 or restore-backup did not finish. Read the marker file named above and the "
                           "audit JSON beside the database, follow the runbook's recovery section for that outcome, "
                           "and re-run the same command (it never applies twice).",
    RECORD_UNREADABLE: "An installation record exists but cannot be read. Inspect the file named above; never delete "
                       "it to get past this check.",
    OTHER_INSTALLATION_PRESENT: "This machine already has a Kite Terminal installation whose database exists at the "
                                "path named above (the repository was probably copied or moved WITHOUT its database). "
                                "Run the terminal from that installation, or move its database here with its "
                                "-wal/-shm (terminal stopped) and run acknowledge-database --confirm. Only for a "
                                "deliberate second installation: init-fresh --confirm-new-installation "
                                "--acknowledge-prior-installation.",
    HOT_JOURNAL: "A crashed write left a non-empty rollback journal (<db>-journal) beside a database that runs in WAL "
                 "mode. Do NOT delete it (it holds the pages needed to undo the interrupted write) and do not open "
                 "the database. Manual review is mandatory: keep the database and its -journal together, and restore "
                 "the newest verified backup with restore-backup, which moves the whole set aside intact (runbook: "
                 "recovery, 'hot journal').",
}


class DatabaseInstallationStateRefused(RuntimeError):
    """A start or maintenance command refused: the database is not in a state that may be used or created.
    Raised before any write; nothing was changed."""
    error_code = "DATABASE_INSTALLATION_STATE_REFUSED"

    def __init__(self, message: str, state: str):
        super().__init__(message)
        self.state = state


@dataclass
class Assessment:
    state: str
    reason: str
    path: str
    evidence: list = field(default_factory=list)
    identity: dict | None = None
    record: dict | None = None
    registry_record: dict | None = None

    @property
    def permitted(self) -> bool:
        return self.state in PERMITTED_STATES

    def refusal(self) -> DatabaseInstallationStateRefused:
        return DatabaseInstallationStateRefused(
            f"DATABASE_INSTALLATION_STATE_REFUSED: {self.state}: {self.reason}. Nothing was changed. "
            f"{_REMEDIATION.get(self.state, '')}"
            + (f" Evidence: {', '.join(self.evidence[:12])}." if self.evidence else ""), self.state)

    def as_record(self) -> dict:
        return {"state": self.state, "reason": self.reason, "evidence": list(self.evidence[:50]),
                "identity": self.identity}


# ---- paths ------------------------------------------------------------------------------------------------------

def canonical(path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def default_registry_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "KiteTerminal" / "installations"


def registry_dir() -> Path:
    override = os.environ.get(REGISTRY_ENV)
    return Path(override) if override else default_registry_dir()


def registry_file(path) -> Path:
    return registry_dir() / f"{hashlib.sha256(canonical(path).encode('utf-8')).hexdigest()[:40]}.json"


# ---- the registry location (post-V12 closure, 2026-11-12) -------------------------------------------------------
# Microsoft Store Python runs as an MSIX-PACKAGED app, and Windows virtualizes a packaged app's writes under the user's
# AppData (%LOCALAPPDATA%, %APPDATA%): files it creates there land in the package's private
# %LOCALAPPDATA%\Packages\<package family>\LocalCache\..., are visible only to that package, and are deleted when the
# package is reset or uninstalled. The default registry directory is under %LOCALAPPDATA%, so under Store Python:
#   (a) os.replace of the temporary record onto its final name fails (WinError 17, ERROR_NOT_SAME_DEVICE: observed on
#       the real migration and on every start since, 2026-11-12), leaving a .tmp file in LocalCache; and
#   (b) even a record that did land there would not be the durable, interpreter-independent evidence the registry
#       exists to be.
# So a packaged interpreter never writes the registry under AppData: it needs an explicit KT_INSTALLATION_REGISTRY_DIR
# outside it (or a python.org interpreter). Nothing falls back to another directory on its own. The temp directory is
# not virtualized (verified 2026-11-12: a file written and renamed there by Store Python is visible at its real path to
# an unpackaged process), which is why the test suite's temporary registry works under Store Python.
# Reads are unchanged: assess() reads whatever registry_dir() names, so a start behaves exactly as before.

REPO_ROOT = Path(__file__).resolve().parents[1]
_ERROR_INSUFFICIENT_BUFFER = 122
_APPMODEL_ERROR_NO_PACKAGE = 15700
REGISTRY_REMEDIATION = (
    f"Set the user environment variable {REGISTRY_ENV} to an absolute directory on a fixed local drive, outside the "
    "repository, outside the Python installation and outside %LOCALAPPDATA% / %APPDATA% (for example "
    "%USERPROFILE%\\KiteTerminal\\installations), then open a NEW console so the terminal and every db_maintenance "
    "command see the same value; or rebuild .venv from a python.org Python. Check it with `python -m "
    "backend.installation_state --probe`. The database and the record beside it are not affected "
    "(docs/INSTALLATION_REGISTRY.md).")


class InstallationRegistryUnavailable(OSError):
    """The registry record was NOT written: its directory is unsuitable, or the write itself failed. Raised before
    anything is created in an unsuitable directory. An OSError, so every caller's existing handling (the
    installation.record_failed log, the audit's 'NOT WRITTEN') applies unchanged."""
    error_code = "INSTALLATION_REGISTRY_UNAVAILABLE"


def packaged_process() -> bool:
    """True when this interpreter runs as an MSIX-packaged app (Microsoft Store Python). GetCurrentPackageFullName
    answers ERROR_INSUFFICIENT_BUFFER for a packaged process and APPMODEL_ERROR_NO_PACKAGE for any other; when the
    API is unavailable, the interpreter's install path decides (Store Python lives under ...\\WindowsApps\\)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        length = ctypes.c_uint32(0)
        rc = ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(length), None)
    except (AttributeError, OSError):
        rc = None
    if rc == _ERROR_INSUFFICIENT_BUFFER:
        return True
    if rc == _APPMODEL_ERROR_NO_PACKAGE:
        return False
    return any(f"{os.sep}windowsapps{os.sep}" in os.path.normcase(f"{p}{os.sep}")
               for p in (sys.base_prefix, getattr(sys, "_base_executable", "") or ""))


_realpath = os.path.realpath   # a seam for the tests (the MSIX redirection cannot be produced on demand)


def _forms(path) -> set[str]:
    """Both spellings of a path: as written (absolute, normalised) and as resolved. Both are needed: for a packaged
    process os.path.realpath FOLLOWS the MSIX redirection (observed 2026-11-12: %LOCALAPPDATA%\\KiteTerminal resolved
    to D:\\WpSystem\\<SID>\\AppData\\Local\\Packages\\<family>\\LocalCache\\Local\\KiteTerminal, on another volume,
    which is exactly why the rename failed with ERROR_NOT_SAME_DEVICE), so the resolved form alone hides it."""
    text = os.fspath(path)
    return {os.path.normcase(os.path.abspath(text)), os.path.normcase(_realpath(text))}


def _inside(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives
        return False


def _inside_any(path, parents) -> str | None:
    """The first of `parents` (each in both spellings) that contains `path` (in either spelling), or None."""
    mine = _forms(path)
    for parent in parents:
        if parent and any(_inside(m, p) for m in mine for p in _forms(parent)):
            return str(parent)
    return None


def _python_installation_dirs() -> list[str]:
    dirs = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix, os.path.dirname(sys.executable)}
    base_executable = getattr(sys, "_base_executable", None)
    if base_executable:
        dirs.add(os.path.dirname(base_executable))
    return sorted(d for d in dirs if d)


def _redirected_roots() -> list[str]:
    """The directories Windows redirects for a packaged process: the user's AppData (Local and Roaming)."""
    return [os.environ[v] for v in ("LOCALAPPDATA", "APPDATA") if os.environ.get(v)]


def _temp_roots() -> list[str]:
    """The temp directories, from the environment only. NOT tempfile.gettempdir(): its first call creates and
    deletes a probe file in the temp directory, and this module's location rules must stay pure path logic."""
    return [os.environ[v] for v in ("TMPDIR", "TEMP", "TMP") if os.environ.get(v)]


def _resolves_into_package_cache(path) -> bool:
    """The resolved path lies in a packaged app's private store (...\\Packages\\<family>\\LocalCache\\...)."""
    parts = os.path.normcase(_realpath(os.fspath(path))).split(os.sep)
    return any(parts[i] == "packages" and parts[i + 2] == "localcache" for i in range(len(parts) - 2))


def registry_location_problem(directory=None) -> str | None:
    """Why the registry record may NOT be written in `directory` (default: registry_dir()), or None when it may.
    Path rules only: nothing is created or opened."""
    override = os.environ.get(REGISTRY_ENV)
    directory = Path(directory) if directory is not None else registry_dir()
    source = f"{REGISTRY_ENV}={override!r}" if override else "the default registry directory"
    if not directory.is_absolute():
        return f"{source} is not an absolute path (it would depend on the current directory)"
    if _inside_any(directory, [REPO_ROOT]):
        return (f"{source} ({directory}) is inside the repository ({REPO_ROOT}); the registry must survive the "
                "deletion of the repository folder")
    python_dir = _inside_any(directory, _python_installation_dirs())
    if python_dir:
        return f"{source} ({directory}) is inside the Python installation ({python_dir})"
    if packaged_process():
        root = None if _inside_any(directory, _temp_roots()) else _inside_any(directory, _redirected_roots())
        if root or _resolves_into_package_cache(directory):
            return (f"{source} ({directory}) is under {root or 'a packaged app store'}, which Windows redirects for "
                    f"this Microsoft Store (MSIX-packaged) Python ({sys.base_prefix}) into the package's private "
                    f"LocalCache (here: {_realpath(os.fspath(directory))}): the record cannot be renamed into place "
                    "there (WinError 17) and would not survive a reset of that Python")
    return None


def record_file(path) -> Path:
    return Path(f"{os.fspath(path)}{RECORD_SUFFIX}")


def marker_file(path, suffix: str) -> Path:
    return Path(f"{os.fspath(path)}{suffix}")


# ---- facts ------------------------------------------------------------------------------------------------------

def file_identity(path) -> dict | None:
    """The identity of the file now at `path` (no open): None when there is none."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return {"st_dev": st.st_dev, "st_ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def same_file(a: dict | None, b: dict | None) -> bool:
    return bool(a and b) and (a.get("st_dev"), a.get("st_ino")) == (b.get("st_dev"), b.get("st_ino"))


def _read_json(path: Path) -> dict | None:
    """None when absent. Anything else that prevents reading it (permissions, bad JSON) is raised: an unreadable
    record is never taken as no record."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return data


def _sibling_names(path: Path) -> list[str]:
    try:
        return sorted(os.listdir(path.parent))
    except FileNotFoundError:
        return []


def prior_installation_evidence(path) -> list[str]:
    """Names (only) of the files beside `path` that an established installation leaves behind."""
    path = Path(path)
    name = os.path.normcase(path.name)
    found = []
    for entry in _sibling_names(path):
        e = os.path.normcase(entry)
        if e in (name + "-wal", name + "-shm", name + "-journal"):
            found.append(entry)
        elif e.startswith(name + "."):
            tail = e[len(name) + 1:]
            # Not <db>.instance.lock: the instance lock creates it BEFORE init_db on every start (a first run too).
            if ("_bak" in tail or "_orig" in tail or "_audit_" in tail
                    or tail.startswith(("failed_restore", "restore_staging", "pre_restore"))
                    or tail == RECORD_SUFFIX.lstrip(".") or ("in_progress" in tail and tail.endswith(".json"))):
                found.append(entry)
    return found


def other_installations(path) -> list[str]:
    """Canonical database paths of OTHER installations of the terminal in this profile's registry: a database with
    the SAME file name as `path` (the terminal's database name is a code constant, kite_terminal.db) in another
    folder, which still exists. Names and existence only. An unreadable registry entry counts as one (fail closed)."""
    here = canonical(path)
    name = os.path.basename(here)
    found = []
    try:
        entries = sorted(registry_dir().glob("*.json"))
    except OSError:
        return []
    for entry in entries:
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
            other = data.get("canonical_path") if isinstance(data, dict) else None
        except (OSError, ValueError):
            found.append(f"unreadable registry entry {entry.name}")
            continue
        if other and other != here and os.path.basename(other) == name and os.path.exists(other):
            found.append(other)
    return found


def in_progress_markers(path) -> list[str]:
    return [str(marker_file(path, s)) for s in IN_PROGRESS_SUFFIXES if marker_file(path, s).exists()]


def _sqlite_facts(path) -> dict:
    """Read-only, as the startup gate already opens it (mode=ro, query_only). Raises sqlite3.DatabaseError on a
    file SQLite cannot read."""
    conn = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        conn.execute("PRAGMA query_only = ON")
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                              "AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchall()
        app_id = conn.execute("PRAGMA application_id").fetchone()[0]
        newest = None
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'").fetchone():
            row = conn.execute("SELECT version, migration_name FROM schema_version "
                               "ORDER BY version DESC LIMIT 1").fetchone()
            newest = [row[0], row[1]] if row else None
        return {"has_user_tables": bool(tables), "application_id": app_id, "schema_newest": newest}
    finally:
        conn.close()


# ---- the assessment ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class FreshInstallConfirmation:
    """The explicit operator intent to create a NEW database where evidence of an earlier installation exists.
    Constructed only by `db_maintenance init-fresh --confirm-new-installation --acknowledge-prior-installation`
    (and by tests). A normal start never passes one."""
    issued_by: str
    acknowledge_prior_installation: bool = False


def _valid_fresh_confirmation(confirmation) -> bool:
    return (type(confirmation) is FreshInstallConfirmation and confirmation.acknowledge_prior_installation is True
            and isinstance(confirmation.issued_by, str) and bool(confirmation.issued_by.strip()))


def assess(path, *, ignore_markers: tuple = ()) -> Assessment:
    """What state the database at `path` is in, from the installation's point of view. Read-only: no file is
    created or changed (the read-only SQLite open is the one the startup gate already makes). `ignore_markers`
    (suffixes) lets the command that owns a marker re-run over its own unfinished marker."""
    path = Path(path)
    shown = str(path)
    markers = [m for m in in_progress_markers(path) if not m.endswith(tuple(ignore_markers))]
    if markers:
        return Assessment(INTERRUPTED_OPERATION, f"an unfinished maintenance operation left {markers}", shown,
                          evidence=[os.path.basename(m) for m in markers])
    rec_path, reg_path = record_file(path), registry_file(path)
    try:
        record = _read_json(rec_path)
    except (OSError, ValueError) as e:
        if isinstance(e, PermissionError) and type(e).__name__ != "PermissionError":
            raise  # the test-suite guard (a PermissionError subclass) is never swallowed
        return Assessment(RECORD_UNREADABLE, f"{rec_path} cannot be read ({type(e).__name__}: {e})", shown,
                          evidence=[rec_path.name])
    try:
        registry = _read_json(reg_path)
    except (OSError, ValueError) as e:
        if isinstance(e, PermissionError) and type(e).__name__ != "PermissionError":
            raise
        return Assessment(RECORD_UNREADABLE, f"the registry record {reg_path} cannot be read "
                          f"({type(e).__name__}: {e})", shown, evidence=[str(reg_path)])
    here = canonical(path)
    if record is not None and record.get("canonical_path") and record["canonical_path"] != here:
        return Assessment(PATH_CHANGED, f"the installation record beside the database names "
                          f"{record['canonical_path']}, not {here}", shown, evidence=[rec_path.name],
                          record=record, registry_record=registry)
    evidence = prior_installation_evidence(path)
    if registry is not None:
        evidence.append(f"registry:{reg_path.name}")
    identity = file_identity(path)
    base = dict(path=shown, evidence=evidence, identity=identity, record=record, registry_record=registry)
    if identity is None:
        if evidence:
            return Assessment(ESTABLISHED_MISSING, "no database file, but an installation existed here", **base)
        others = other_installations(path)
        if others:
            base["evidence"] = [f"other installation: {o}" for o in others]
            return Assessment(OTHER_INSTALLATION_PRESENT, f"no database file here, and this machine's installation "
                              f"database exists at {others[0]}", **base)
        return Assessment(FRESH_INSTALL_PERMITTED, "no database file and no evidence of an earlier installation",
                          **base)
    if identity["size"] == 0:
        return Assessment(ZERO_BYTE, "the database file is zero bytes", **base)
    journal = Path(f"{path}-journal")
    if journal.exists() and journal.stat().st_size > 0:
        # Checked before any open: a read-only open cannot replay it (and fails), a read-write one would.
        return Assessment(HOT_JOURNAL, f"{journal.name} is a non-empty rollback journal (an interrupted write)",
                          **base)
    try:
        facts = _sqlite_facts(path)
    except sqlite3.DatabaseError as e:
        return Assessment(CORRUPT, f"SQLite cannot read the file ({type(e).__name__}: {e})", **base)
    base["identity"] = dict(identity, application_id=facts["application_id"], schema_newest=facts["schema_newest"])
    if not facts["has_user_tables"]:
        return Assessment(EMPTY_SQLITE, "the database file holds no table", **base)
    known = [r for r in (registry, record) if r is not None]
    if not known:
        return Assessment(LEGACY_UNMARKED, "an existing database with no installation record yet (it is recorded "
                          "after this start)", **base)
    for rec in known:
        last = rec.get("last_known") or {}
        if last.get("st_ino") is not None and not same_file(last, identity):
            return Assessment(IDENTITY_CHANGED, f"the file id is {identity['st_dev']}:{identity['st_ino']}, the "
                              f"installation last used {last.get('st_dev')}:{last.get('st_ino')} (replaced or "
                              "re-created)", **base)
        expected_app = rec.get("application_id")
        if expected_app and facts["application_id"] not in (0, expected_app):
            return Assessment(IDENTITY_CHANGED, f"the database's application_id is {facts['application_id']}, "
                              f"this installation's is {expected_app} (a foreign database)", **base)
        if expected_app and facts["application_id"] == 0 and last.get("application_id"):
            return Assessment(IDENTITY_CHANGED, "the database has no application_id although this installation "
                              "stamped one (a different file)", **base)
        seen = last.get("schema_newest")
        now = facts["schema_newest"]
        if seen and now and int(now[0]) < int(seen[0]):
            return Assessment(SCHEMA_REGRESSED, f"the database is at schema v{now[0]}, this installation last saw "
                              f"v{seen[0]}", **base)
    return Assessment(ESTABLISHED_VALID, "the database this installation last used", **base)


def require_startup_ok(path, fresh_install: FreshInstallConfirmation | None = None) -> Assessment:
    """For a normal start: refuses (DatabaseInstallationStateRefused) unless the state is permitted. Only an explicit
    FreshInstallConfirmation turns ESTABLISHED_MISSING into a (new) fresh install; nothing turns a zero-byte, empty,
    corrupt, replaced or interrupted database into one."""
    assessment = assess(path)
    if assessment.state in (ESTABLISHED_MISSING, OTHER_INSTALLATION_PRESENT) and \
            _valid_fresh_confirmation(fresh_install):
        assessment.state = FRESH_INSTALL_PERMITTED
        assessment.reason = (f"an explicit new installation ({fresh_install.issued_by}); the earlier installation's "
                             f"evidence was acknowledged: {', '.join(assessment.evidence[:12])}")
        return assessment
    if not assessment.permitted:
        logger.critical("event=installation.refused state=%s db=%s", assessment.state, Path(path).name)
        raise assessment.refusal()
    return assessment


# ---- writing the records ----------------------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


_REPLACE_ATTEMPTS = 20
_REPLACE_DELAY_S = 0.05


def _replace(source: Path, target: Path) -> None:
    """os.replace, retried briefly while Windows reports a sharing violation (another process is reading the target
    at that instant: a concurrent start or command). Any other error is raised at once; so is the test-suite guard
    (a PermissionError subclass), which is never retried or swallowed."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError as e:
            if type(e) is not PermissionError or attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY_S)


_TEMPORARY_SEQUENCE = itertools.count(1)   # next() is atomic under the GIL: unique per call within the process


def _create_temporary(target: Path):
    """An exclusively created temporary beside `target`: <name>.tmp<pid>.<n>. Unique per call (pid + a per-process
    sequence, so concurrent threads never share one); a leftover of a crashed process that had the same pid is skipped,
    never overwritten. Kept short: the name stays within MAX_PATH wherever the old <name>.tmp<pid> did, give or take
    a few characters."""
    for _ in range(1000):
        temporary = target.with_name(f"{target.name}.tmp{os.getpid()}.{next(_TEMPORARY_SEQUENCE)}")
        try:
            return temporary, open(temporary, "x", encoding="utf-8")
        except FileExistsError:
            continue
    raise FileExistsError(f"no free temporary name beside {target}")


def _atomic_write_json(target: Path, data: dict) -> None:
    """Write a temporary file, fsync it, rename it onto `target`: a reader sees the old record or the new one, never a
    partial one. The temporary name is unique per call, so concurrent writers never share one, and it is removed when
    the write or the rename fails, so a failure leaves nothing behind."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary, handle = _create_temporary(target)
    try:
        with handle:
            handle.write(json.dumps(data, indent=2, default=str))
            handle.flush()
            os.fsync(handle.fileno())
        _replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _write_registry(target: Path, data: dict) -> None:
    """The registry record, only in a suitable directory (registry_location_problem): refused before anything is
    created otherwise. Any failure is raised as InstallationRegistryUnavailable naming the cause and the fix."""
    problem = registry_location_problem(target.parent)
    if problem:
        raise InstallationRegistryUnavailable(
            f"{InstallationRegistryUnavailable.error_code}: the installation registry record was NOT written: "
            f"{problem}. {REGISTRY_REMEDIATION}")
    try:
        _atomic_write_json(target, data)
    except OSError as e:
        if isinstance(e, PermissionError) and type(e).__name__ != "PermissionError":
            raise  # the test-suite guard (a PermissionError subclass) is never re-labelled
        raise InstallationRegistryUnavailable(
            f"{InstallationRegistryUnavailable.error_code}: the installation registry record was NOT written to "
            f"{target} ({type(e).__name__}: {e}). {REGISTRY_REMEDIATION}") from e


def _sqlite_newest(conn) -> list | None:
    try:
        row = conn.execute("SELECT version, migration_name FROM schema_version ORDER BY version DESC "
                           "LIMIT 1").fetchone()
    except sqlite3.DatabaseError:
        return None
    return [row[0], row[1]] if row else None


def record(path, *, by: str, conn=None, new_installation: bool = False, stamp_application_id: bool = False,
           note: str | None = None) -> dict:
    """Writes (or re-binds) both records for the database now at `path`. With `conn` (a writable connection to
    it) and stamp_application_id, a database whose header application_id is 0 gets this installation's value.
    Raises OSError when a record cannot be written (the caller decides whether that is fatal)."""
    path = Path(path)
    previous = None
    for source in (registry_file(path), record_file(path)):
        try:
            previous = previous or _read_json(source)
        except (OSError, ValueError):  # an unreadable old record: a new one replaces it (assess refused it first)
            pass
    if new_installation or previous is None:
        install_id = secrets.token_hex(16)
        application_id = secrets.randbelow(2 ** 31 - 2) + 1
        created_at = _now()
    else:
        install_id = previous.get("install_id") or secrets.token_hex(16)
        application_id = previous.get("application_id") or (secrets.randbelow(2 ** 31 - 2) + 1)
        created_at = previous.get("created_at") or _now()
    current_app = None
    newest = None
    if conn is not None:
        current_app = conn.execute("PRAGMA application_id").fetchone()[0]
        if stamp_application_id and current_app == 0:
            conn.execute(f"PRAGMA application_id = {int(application_id)}")
            current_app = application_id
        newest = _sqlite_newest(conn)
        if current_app and current_app != application_id:
            application_id = current_app  # acknowledge / adopt: the file's own value is the truth from now on
    identity = file_identity(path) or {}
    data = {
        "format": FORMAT, "install_id": install_id, "created_at": created_at, "canonical_path": canonical(path),
        "database_name": path.name, "application_id": application_id,
        "last_known": dict(identity, application_id=current_app, schema_newest=newest,
                           observed_at=_now(), by=by),
    }
    if note:
        data["note"] = note
    history = list((previous or {}).get("history", []))[-19:] if not new_installation else []
    history.append({"at": _now(), "by": by, "st_ino": identity.get("st_ino"), "schema_newest": newest})
    data["history"] = history
    _atomic_write_json(record_file(path), data)
    _write_registry(registry_file(path), data)
    logger.info("event=installation.recorded by=%s db=%s new=%s registry=%s", by, path.name, new_installation,
                registry_dir())
    return data


def record_after_startup(path, assessment: Assessment, conn) -> None:
    """After a successful init_db: record a fresh install, adopt a legacy database, refresh an established one.
    A record that cannot be written is logged (the database itself is fine), never silently ignored."""
    try:
        record(path, by="startup", conn=conn, stamp_application_id=True,
               new_installation=assessment.state == FRESH_INSTALL_PERMITTED)
        conn.commit()
    except OSError as e:
        logger.error("event=installation.record_failed db=%s error=%s: %s", Path(path).name, type(e).__name__, e)


def has_record(path) -> bool:
    return record_file(path).exists() or registry_file(path).exists()


def note_schema_change(path, *, by: str, conn=None) -> None:
    """A deliberate schema change made by the terminal's own code (a rollback): re-bind an existing record. No
    record, nothing to do. A record that cannot be written is logged; the next start then refuses (fail closed)."""
    try:
        if has_record(path):
            record(path, by=by, conn=conn)
    except OSError as e:
        logger.error("event=installation.record_failed db=%s by=%s error=%s: %s", Path(path).name, by,
                     type(e).__name__, e)


def forget(path) -> list[str]:
    """Removes both records (demo/test reset of a throwaway database only; never called by the terminal)."""
    removed = []
    for p in (record_file(path), registry_file(path)):
        try:
            p.unlink()
            removed.append(str(p))
        except FileNotFoundError:
            pass
    return removed


# ---- in-progress markers (migrate-v12, restore-backup) -----------------------------------------------------------

def write_marker(path, suffix: str, content: dict) -> Path:
    """Created exclusively: an existing marker (an unfinished earlier run) is never overwritten."""
    marker = marker_file(path, suffix)
    data = json.dumps(dict(content, written_at=_now(), pid=os.getpid()), indent=2, default=str)
    with open(marker, "x", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return marker


def update_marker(marker: Path, content: dict) -> None:
    data = _read_json(marker) or {}
    data.update(content)
    _atomic_write_json(marker, data)


def clear_marker(marker: Path) -> None:
    try:
        marker.unlink()
    except FileNotFoundError:
        pass


# ---- operator check: python -m backend.installation_state [--db FILE] [--probe] ----------------------------------

DEFAULT_DB = Path(__file__).resolve().parent / "kite_terminal.db"   # the terminal's database (backend/db.py DB_PATH)
_COMPARED = ("install_id", "application_id", "canonical_path")


def check_registry(db_path=None, *, probe: bool = False) -> dict:
    """The registry's state for `db_path` (default: the terminal's database). Reads the two JSON records only (ids,
    paths, timestamps); NEVER opens the database. probe=True also writes and removes one probe file in the registry
    directory with the same atomic write the records use (creating the directory when it is missing), which proves
    the directory is writable and not redirected. Verdict OK only when the registry record exists, is readable and
    names the same installation as the record beside the database."""
    db_path = Path(db_path) if db_path is not None else DEFAULT_DB
    directory = registry_dir()
    problem = registry_location_problem(directory)
    result = {"interpreter": sys.executable, "python_installation": sys.base_prefix,
              "store_python_packaged": packaged_process(), "override": os.environ.get(REGISTRY_ENV) or None,
              "registry_dir": str(directory), "location": problem or "OK", "database": str(db_path),
              "registry_file": str(registry_file(db_path)), "record_file": str(record_file(db_path))}
    if probe:
        if problem:
            result["probe"] = "NOT RUN (location refused)"
        else:
            probe_file = directory / f"kt_registry_probe_{os.getpid()}_{secrets.token_hex(4)}.check"
            try:
                _atomic_write_json(probe_file, {"probe": True})
                result["probe"] = "OK" if _read_json(probe_file) == {"probe": True} else "FAILED (read back differs)"
            except (OSError, ValueError) as e:
                result["probe"] = f"FAILED ({type(e).__name__}: {e})"
            finally:
                try:
                    probe_file.unlink()
                except OSError:
                    pass
    records = {}
    for name, source in (("registry_record", registry_file(db_path)), ("beside_db_record", record_file(db_path))):
        try:
            data = _read_json(source)
        except (OSError, ValueError) as e:
            records[name] = f"UNREADABLE ({type(e).__name__}: {e})"
            continue
        records[name] = None if data is None else dict(
            {k: data.get(k) for k in _COMPARED}, observed_at=(data.get("last_known") or {}).get("observed_at"))
    result.update(records)
    registry, beside = records["registry_record"], records["beside_db_record"]
    if problem:
        verdict = "LOCATION_REFUSED"
    elif str(result.get("probe", "")).startswith("FAILED"):
        verdict = "PROBE_FAILED"
    elif isinstance(registry, str) or isinstance(beside, str):
        verdict = "RECORD_UNREADABLE"
    elif registry is None:
        verdict = "NOT_WRITTEN"
    elif beside is not None and any(registry[k] != beside[k] for k in _COMPARED):
        verdict = "MISMATCH"
    else:
        verdict = "OK"
    result["verdict"] = verdict
    return result


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="python -m backend.installation_state",
        description="Check the installation registry location and record. Never opens the database.")
    parser.add_argument("--db", type=Path, default=None, help="database file (default: the terminal's)")
    parser.add_argument("--probe", action="store_true",
                        help="also write and remove one probe file in the registry directory")
    args = parser.parse_args(argv)
    result = check_registry(args.db, probe=args.probe)
    for key, value in result.items():
        print(f"{key + ':':<22} {value}")
    if result["verdict"] == "LOCATION_REFUSED":
        print(REGISTRY_REMEDIATION)
    elif result["verdict"] == "NOT_WRITTEN":
        print("The location is usable; the record is written by the next terminal start (log: "
              "event=installation.recorded ... registry=<this directory>).")
    return 0 if result["verdict"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
