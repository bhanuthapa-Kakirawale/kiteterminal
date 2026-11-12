"""Installation identity: an intentional fresh install versus an established installation whose database went missing,
was emptied, replaced or moved (V12 final readiness, Phase C).

User decision (2026-11-11, V12 final readiness): fresh installs are EVIDENCE-BASED. A normal start may still create
the database at the latest schema on a true first run, but only when nothing shows that an installation existed
before. Evidence of a prior installation:
  * the installation record beside the database (<db>.installation.json);
  * the installation REGISTRY record, kept in the user profile (%LOCALAPPDATA%\\KiteTerminal\\installations\\<hash of
    the canonical database path>.json), so it survives the deletion of the database, of its directory or of the whole
    repository folder (overridable with KT_INSTALLATION_REGISTRY_DIR; the test suite points it at a temporary dir);
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


def _atomic_write_json(target: Path, data: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp{os.getpid()}")
    temporary.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, target)


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
    _atomic_write_json(registry_file(path), data)
    logger.info("event=installation.recorded by=%s db=%s new=%s", by, path.name, new_installation)
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
