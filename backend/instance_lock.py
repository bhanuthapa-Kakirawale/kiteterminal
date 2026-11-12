"""Phase 2: single-instance ownership of the account database (docs/PHASE_2_DESIGN_REVIEW.md §5, §7).

Three layers, each needed and none sufficient alone:

1. An OS exclusive lock on `<DB_PATH>.instance.lock`, taken before init_db. The kernel picks one
   winner atomically and releases the lock when the holder's handle closes or the process dies, so
   liveness is never decided by a PID (a reused PID cannot fake an owner). Windows:
   msvcrt.locking(LK_NBLCK) on byte 0; POSIX: flock(LOCK_EX | LOCK_NB). A process that cannot take
   it is a NON-OWNER: no init_db, no loops, no ticker, every write refused, a status page only.
2. A fencing epoch in the singleton app_instance row (migration v11), incremented once per
   acquisition under BEGIN IMMEDIATE and checked inside every Phase 2 coordination transaction
   (fenced_transaction) and by the heartbeat. It catches a lock bypass (a network or synced
   folder, another machine, an older build on a copy): a process whose epoch is no longer current
   drops its authority and never writes coordination state again.
3. Process write authority: one in-memory flag (authority_ok) gating the broker write fence
   (kite_client, PaperBroker), the write routes (require_write_authority) and the background loops
   (loop_may_run). It is never re-acquired in the same process: recovery is a restart.

A database token cannot recall a broker request that already left the process; that is covered by
the existing ambiguous-outcome handling (and, from Phase 3, the write-ahead journal), not here.

Stdlib only (msvcrt / ctypes / winreg on Windows, fcntl on POSIX); no pywin32.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import html
import json
import logging
import os
import socket
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, HTTPException
from kiteconnect.exceptions import PermissionException

from backend import config

logger = logging.getLogger(__name__)

# ---- roles --------------------------------------------------------------------------------------
NONE = "none"                    # nothing attempted in this process (tests, CLIs): no authority
LOCKED = "locked"                # OS lock held, takeover not yet done: no authority yet
OWNER = "owner"                  # lock held and epoch current: the only role with authority
NON_OWNER = "non_owner"          # another process holds the lock
CONFLICT = "conflict"            # a foreign writer advanced the heartbeat while we held the lock
DRIVE_REFUSED = "drive_refused"  # the database is not on a fixed local drive
LOST = "lost"                    # was owner; a fenced transaction found another epoch
RELEASED = "released"            # clean shutdown done (a later acquire in the same process is allowed)

# Roles served by the non-owner guard: a status page, and 503 for everything else.
READ_ONLY_ROLES = frozenset({NON_OWNER, CONFLICT, DRIVE_REFUSED})

# ---- reason codes (design §8.5) -------------------------------------------------------------------
ANOTHER_INSTANCE_RUNNING = "ANOTHER_INSTANCE_RUNNING"
INSTANCE_NOT_OWNER = "INSTANCE_NOT_OWNER"
INSTANCE_LOCK_LOST = "INSTANCE_LOCK_LOST"
INSTANCE_CONFLICT = "INSTANCE_CONFLICT"
DATABASE_NOT_ON_LOCAL_DRIVE = "DATABASE_NOT_ON_LOCAL_DRIVE"
HEARTBEAT_STALE = "HEARTBEAT_STALE"
RESUMED_AFTER_SUSPEND = "RESUMED_AFTER_SUSPEND"
STARTUP_RECONCILIATION_INCOMPLETE = "STARTUP_RECONCILIATION_INCOMPLETE"
PREVIOUS_INSTANCE_UNCLEAN = "PREVIOUS_INSTANCE_UNCLEAN"

LIVE_OWNER_STATES = ("starting", "owner", "stopping")
LOCK_SUFFIX = ".instance.lock"

_MESSAGES = {
    ANOTHER_INSTANCE_RUNNING: "Another Kite Terminal process owns this database. This process is read-only: "
                              "use the running instance, or stop it first.",
    INSTANCE_NOT_OWNER: "This process does not own the database (not started, or shutting down). "
                        "Nothing was changed.",
    INSTANCE_LOCK_LOST: "This process lost ownership of the database (another instance took over). It no "
                        "longer writes anything. Restart it after checking which instance is running.",
    INSTANCE_CONFLICT: "URGENT: another writer is using this database outside the instance lock (a network or "
                       "synced folder, another machine, or an older build). This process stays read-only. "
                       "Stop every other copy, then restart.",
    DATABASE_NOT_ON_LOCAL_DRIVE: "The database is not on a fixed local drive, where the instance lock cannot be "
                                 "trusted. This process stays read-only. Move the database to a local drive.",
}


class InstanceNotOwner(PermissionException):
    """A broker write refused locally: this process has no write authority. Nothing was sent.

    A PermissionException subclass on purpose: the order paths (execution.DEFINITE_REJECTIONS) record
    it as a definite rejection, never as an UNKNOWN outcome."""

    def __init__(self, route: str, method: str, reason: str):
        super().__init__(f"{reason}: {method} {route} was refused locally and never sent to Kite. "
                         f"{_MESSAGES.get(reason, '')}", code=403)
        self.reason = reason


class FencingError(RuntimeError):
    """A fenced transaction was refused: no authority, or the epoch is no longer ours."""
    error_code = INSTANCE_LOCK_LOST


class WrongDatabase(FencingError):
    """A fenced transaction on a connection to a different file than the one locked. Not evidence of
    another owner (authority is kept); the transaction is simply refused."""
    error_code = "INSTANCE_WRONG_DATABASE"


# ---- process state --------------------------------------------------------------------------------

class _State:
    def __init__(self) -> None:
        self.role = NONE
        self.reason: str | None = None
        self.detail: str | None = None
        self.lock_fd: int | None = None
        self.lock_path: Path | None = None
        self.db_path: Path | None = None
        self.epoch: int | None = None
        self.instance_uuid: str | None = None
        self.heartbeat_seq = 0
        self.started_at: str | None = None
        self.previous_unclean = False
        self.process_reasons: set[str] = set()
        self.resumed_at: datetime | None = None
        self.last_hb_wall: datetime | None = None
        self.last_hb_mono: float | None = None
        self.last_hb_ok_mono: float | None = None
        self.last_hb_ok_at: str | None = None
        self.drive_problem: str | None = None
        self.warnings: list[str] = []
        self.shutting_down = False
        self.in_flight = 0
        self.cv = threading.Condition(threading.RLock())
        self.logged_loops: set[str] = set()
        # Phase 3 M-6 / R2-7: one shared detector baseline (last_hb_wall / last_hb_mono above), one resume_seq
        # increment per detected gap, and the highest resume_seq the heartbeat loop has noted (note_resume).
        self.resume_seq = 0
        self.last_noted_seq = 0


_state = _State()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat()


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def lock_path_for(db_path) -> Path:
    """`<resolved DB path>.instance.lock`: derived from the database file, so a test's tmp database gets
    a tmp lock, and every path alias of the same database names the same lock file."""
    resolved = Path(os.path.realpath(db_path))
    return resolved.with_name(resolved.name + LOCK_SUFFIX)


# ---- the OS lock ----------------------------------------------------------------------------------

def _os_try_lock(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _os_unlock_and_close(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def try_lock_file(path) -> int | None:
    """Opens (creating) `path` and takes the exclusive lock without waiting. Returns the descriptor,
    or None when another handle holds it (in this or any other process). The descriptor is not
    inherited by child processes, so a child can never keep the lock alive after its parent dies."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0) \
        | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(path), flags, 0o600)
    if _os_try_lock(fd):
        return fd
    os.close(fd)
    return None


def release_lock_file(fd: int) -> None:
    _os_unlock_and_close(fd)


class InstanceLockHeld(RuntimeError):
    """A maintenance command found the instance lock held by another process. Nothing was changed."""
    error_code = ANOTHER_INSTANCE_RUNNING


@contextlib.contextmanager
def held_for_maintenance(db_path):
    """For the database-writing CLIs (db_maintenance): holds the same instance lock for the duration,
    or raises InstanceLockHeld when it is held (the app, or another CLI, is running). Never waits."""
    path = lock_path_for(db_path)
    fd = try_lock_file(path)
    if fd is None:
        raise InstanceLockHeld(f"{ANOTHER_INSTANCE_RUNNING}: {path.name} is held by another process (the terminal "
                           f"or another maintenance command). Stop it first; nothing was changed.")
    try:
        yield path
    finally:
        release_lock_file(fd)


# ---- local-drive check (§7.2) ---------------------------------------------------------------------

DRIVE_FIXED = 3
_DRIVE_TYPES = {0: "unknown", 1: "no root directory", 2: "removable", 3: "fixed", 4: "network (remote)",
                5: "CD-ROM", 6: "RAM disk"}
_NETWORK_FILESYSTEMS = frozenset({"nfs", "nfs4", "cifs", "smbfs", "smb3", "sshfs", "fuse.sshfs", "9p", "afs",
                                  "ceph", "glusterfs", "davfs", "fuse.rclone", "fuse.s3fs"})


def _drive_type(root: str) -> int:
    import ctypes
    return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))


def _posix_filesystem(path: str) -> str | None:
    try:
        with open("/proc/mounts", encoding="utf-8") as handle:
            mounts = [line.split()[:3] for line in handle if len(line.split()) >= 3]
    except OSError:
        return None
    best, fstype = "", None
    for _dev, mount, kind in mounts:
        mount = mount.replace("\\040", " ")
        if (path == mount or path.startswith(mount.rstrip("/") + "/")) and len(mount) > len(best):
            best, fstype = mount, kind
    return fstype


def local_drive_problem(db_path) -> str | None:
    """None when the database sits on a fixed local drive; otherwise why not. A byte-range lock is
    unreliable on network shares, so such a database is refused (read-only), not trusted."""
    resolved = os.path.realpath(db_path)
    if os.name == "nt":
        if resolved.startswith("\\\\"):
            return f"the database path is a network (UNC) path: {resolved}"
        drive = os.path.splitdrive(resolved)[0]
        if len(drive) != 2 or drive[1] != ":":
            return f"the database path has no drive letter: {resolved}"
        kind = _drive_type(drive + "\\")
        if kind != DRIVE_FIXED:
            return f"drive {drive} is a {_DRIVE_TYPES.get(kind, kind)} drive, not a fixed local drive"
        return None
    fstype = _posix_filesystem(resolved)
    if fstype in _NETWORK_FILESYSTEMS:
        return f"the database is on a network filesystem ({fstype})"
    return None


def sync_root_warning(db_path) -> str | None:
    """A database under a OneDrive folder is a warning (P§13 residual risk 8), not a refusal: the
    folder is local, but the sync client may copy or replace the file underneath the app."""
    resolved = os.path.normcase(os.path.realpath(db_path))
    roots = [os.environ.get(name) for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
    for root in filter(None, roots):
        root_norm = os.path.normcase(os.path.realpath(root))
        if resolved == root_norm or resolved.startswith(root_norm.rstrip("\\/") + os.sep):
            return f"the database is inside a OneDrive folder ({root}); move it out of synced folders"
    if "onedrive" in resolved.replace("\\", "/").lower().split("/"):
        return "the database path contains a OneDrive folder; move it out of synced folders"
    return None


# ---- process identity (diagnostics only; never used for liveness) ---------------------------------

def _filetime_to_iso(value: int) -> str:
    return _iso(datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=value // 10))


def process_created_at(pid: int | None = None) -> str | None:
    """The OS creation time (UTC) of `pid` (default: this process), or None when unknowable.
    With the pid it identifies a process: a reused pid has a different creation time."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            own = pid is None or pid == os.getpid()
            handle = kernel32.GetCurrentProcess() if own else kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return None
            try:
                times = [wintypes.FILETIME() for _ in range(4)]
                if not kernel32.GetProcessTimes(wintypes.HANDLE(handle), *[ctypes.byref(t) for t in times]):
                    return None
                created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                return _filetime_to_iso(created)
            finally:
                if not own:
                    kernel32.CloseHandle(wintypes.HANDLE(handle))
        stat = Path(f"/proc/{pid or os.getpid()}/stat").read_text()
        start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
        btime = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                     if line.startswith("btime"))
        return _iso(datetime.fromtimestamp(btime + start_ticks / os.sysconf("SC_CLK_TCK"), tz=timezone.utc))
    except Exception:
        return None


def machine_id() -> str | None:
    try:
        if os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                return str(winreg.QueryValueEx(key, "MachineGuid")[0])
        return Path("/etc/machine-id").read_text().strip() or None
    except Exception:
        return None


def describe_process(pid, created_at) -> str:
    """For the status page and diagnostics: is the recorded owner process still alive? Compares the
    pid AND its creation time, so a reused pid reads as 'a different process'. Diagnostic only."""
    if pid is None:
        return "no process recorded"
    now_created = process_created_at(int(pid))
    if now_created is None:
        return "not running (or not visible to this user)"
    if created_at and now_created == created_at:
        return "the same process is still running"
    return "a different process now has this pid (the recorded process is gone)"


def _version() -> tuple[str | None, str | None]:
    try:
        from backend import version
        return version.APP_VERSION, version.commit_short()
    except Exception:
        return None, None


# ---- connections ----------------------------------------------------------------------------------

def _connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=config.SQLITE_BUSY_TIMEOUT_SECONDS, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(config.SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def read_owner_readonly(db_path) -> dict:
    """The app_instance row through a read-only connection (mode=ro): what a non-owner shows. Never
    writes, never raises."""
    try:
        uri = f"file:{Path(os.path.realpath(db_path)).as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM app_instance WHERE id = 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if row is None:
        return {"error": "no app_instance row"}
    owner = dict(row)
    heartbeat = _parse(owner.get("heartbeat_at"))
    owner["heartbeat_age_seconds"] = round((_utc_now() - heartbeat).total_seconds(), 1) if heartbeat else None
    owner["process"] = describe_process(owner.get("pid"), owner.get("process_created_at"))
    return owner


def _database_file(conn: sqlite3.Connection) -> Path | None:
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main" and row[2]:
            return Path(os.path.realpath(row[2]))
    return None


# ---- acquisition ----------------------------------------------------------------------------------

def _release_at_exit() -> None:
    fd = _state.lock_fd
    if fd is not None:
        _state.lock_fd = None
        _os_unlock_and_close(fd)


atexit.register(_release_at_exit)


def acquire(db_path) -> str:
    """Step 2 of the startup sequence: the local-drive check, then the OS lock. Returns the role:
    LOCKED (go on: init_db, then takeover), or NON_OWNER / DRIVE_REFUSED (read-only, stop).
    Idempotent in-process: a second startup of the same process on the same database keeps its lock.
    A process that lost authority, or met a conflict, never re-acquires: restart it."""
    resolved = Path(os.path.realpath(db_path))
    with _state.cv:
        if _state.lock_fd is not None and _state.db_path == resolved and _state.role in (LOCKED, OWNER):
            return _state.role
        if _state.role in (LOST, CONFLICT):
            return _state.role
        if _state.lock_fd is not None:
            raise RuntimeError(f"this process already holds the instance lock of {_state.db_path}")
        _reset_state_locked()
        _state.db_path = resolved
        _state.lock_path = lock_path_for(resolved)
        warning = sync_root_warning(resolved)
        if warning:
            _state.warnings.append(warning)
            logger.warning("event=instance.sync_root_warning %s", warning)
        problem = local_drive_problem(resolved)
        if problem:
            _state.role, _state.reason, _state.drive_problem = DRIVE_REFUSED, DATABASE_NOT_ON_LOCAL_DRIVE, problem
            logger.critical("event=instance.refused code=%s %s", DATABASE_NOT_ON_LOCAL_DRIVE, problem)
            return _state.role
        fd = try_lock_file(_state.lock_path)
        if fd is None:
            _state.role, _state.reason = NON_OWNER, ANOTHER_INSTANCE_RUNNING
            logger.error("event=instance.non_owner code=%s lock=%s: another process owns this database; this "
                         "process is read-only (no init_db, no loops, every write refused)",
                         ANOTHER_INSTANCE_RUNNING, _state.lock_path)
            return _state.role
        _state.lock_fd, _state.role = fd, LOCKED
        logger.info("event=instance.lock_acquired lock=%s", _state.lock_path)
        return _state.role


def _reset_state_locked() -> None:
    fresh = _State()
    for name, value in vars(fresh).items():
        if name != "cv":
            setattr(_state, name, value)


def takeover(*, now=_utc_now, sleep=time.sleep, monotonic=time.monotonic,
             poll_seconds: float | None = None) -> dict:
    """Step 4 (§7.2 step 3): the fenced epoch takeover, holding the OS lock.

    If the predecessor did not stop cleanly and its heartbeat is younger than its lease, wait
    read-only until the lease has passed without its heartbeat_seq (or anything else) changing. A
    change while WE hold the lock is a second live writer the lock does not see: INSTANCE_CONFLICT
    (permanently read-only). Otherwise, in one BEGIN IMMEDIATE transaction: epoch + 1, our identity,
    the history rows, and RESTRICTED(STARTUP_RECONCILIATION_INCOMPLETE [, PREVIOUS_INSTANCE_UNCLEAN])
    for every account, so an owner never exists without the startup restriction."""
    poll = config.TAKEOVER_POLL_SECONDS if poll_seconds is None else poll_seconds
    with _state.cv:
        if _state.role == OWNER:
            return {"epoch": _state.epoch, "already_owner": True, "previous_unclean": _state.previous_unclean,
                    "conflict": False}
        if _state.role != LOCKED:
            raise FencingError(f"takeover refused: role {_state.role} (the instance lock is not held)")
        db_path = _state.db_path
    conn = _connect(db_path)
    try:
        def snapshot():
            row = conn.execute("SELECT * FROM app_instance WHERE id = 1").fetchone()
            if row is None:
                raise FencingError("takeover refused: app_instance has no row (schema v11 missing?)")
            return dict(row)

        def same(a, b):
            keys = ("epoch", "instance_uuid", "heartbeat_seq", "heartbeat_at", "state", "clean_shutdown")
            return all(a[k] == b[k] for k in keys)

        before = snapshot()
        unclean = before["state"] in LIVE_OWNER_STATES or (before["state"] == "stopped_clean"
                                                             and before["clean_shutdown"] == 0)
        waited = 0.0
        if unclean:
            heartbeat = _parse(before["heartbeat_at"])
            lease = float(before["lease_seconds"])
            age = (now() - heartbeat).total_seconds() if heartbeat else None
            # No heartbeat, or one from the future (clock skew): wait the full lease.
            remaining = lease if age is None or age < 0 else max(0.0, lease - age)
            if remaining > 0:
                logger.warning("event=instance.lease_wait predecessor_epoch=%s state=%s heartbeat_age_s=%s wait_s=%.1f",
                               before["epoch"], before["state"], None if age is None else round(age, 1), remaining)
            started = monotonic()
            deadline = started + remaining
            while monotonic() < deadline:
                sleep(max(0.0, min(poll, deadline - monotonic())))
                if not same(snapshot(), before):
                    return _enter_conflict("the predecessor's app_instance row changed while this process held the "
                                           "instance lock")
            waited = monotonic() - started

        conn.execute("BEGIN IMMEDIATE")
        try:
            current = snapshot()
            if not same(current, before):
                conn.execute("ROLLBACK")
                return _enter_conflict("app_instance changed between the lease wait and the takeover")
            epoch = int(current["epoch"]) + 1
            mine = str(uuid.uuid4())
            at = _iso(now())
            app_version, app_commit = _version()
            identity = dict(pid=os.getpid(), process_created_at=process_created_at(), host=socket.gethostname(),
                            machine_id=machine_id(), lock_path=str(_state.lock_path), db_path=str(db_path),
                            app_version=app_version, app_commit=app_commit)
            changed = conn.execute(
                "UPDATE app_instance SET epoch = ?, instance_uuid = ?, pid = ?, process_created_at = ?, host = ?, "
                "machine_id = ?, lock_path = ?, db_path = ?, app_version = ?, app_commit = ?, state = 'owner', "
                "started_at = ?, heartbeat_at = ?, heartbeat_seq = 0, lease_seconds = ?, clean_shutdown = 0, "
                "shutdown_at = NULL, shutdown_note = NULL, updated_at = datetime('now') WHERE id = 1 AND epoch = ?",
                (epoch, mine, identity["pid"], identity["process_created_at"], identity["host"],
                 identity["machine_id"], identity["lock_path"], identity["db_path"], app_version, app_commit,
                 at, at, int(config.INSTANCE_LEASE_SECONDS), current["epoch"])).rowcount
            if changed != 1:
                raise FencingError("takeover refused: app_instance changed under BEGIN IMMEDIATE")
            if current["state"] == "none":
                previous_end = "none"
            elif not unclean:
                previous_end = "stopped_clean"
            elif current["state"] == "stopped_clean":
                previous_end = "stopped_with_write_in_flight"
            else:
                previous_end = "crashed"
            if current["epoch"] > 0:
                conn.execute(
                    "UPDATE app_instance_history SET end_state = ?, ended_at = COALESCE(ended_at, ?) "
                    "WHERE epoch = ? AND end_state = 'running'",
                    ("crashed_detected" if unclean else "stopped_clean", at, current["epoch"]))
            conn.execute(
                "INSERT INTO app_instance_history (epoch, instance_uuid, pid, process_created_at, host, machine_id, "
                "lock_path, db_path, app_version, app_commit, started_at, end_state, previous_epoch, previous_end, "
                "takeover_wait_s, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)",
                (epoch, mine, identity["pid"], identity["process_created_at"], identity["host"],
                 identity["machine_id"], identity["lock_path"], identity["db_path"], app_version, app_commit, at,
                 current["epoch"] or None, previous_end, round(waited, 3),
                 f"predecessor pid {current['pid']} ({current['state']})" if current["epoch"] else None))
            reasons = [STARTUP_RECONCILIATION_INCOMPLETE] + ([PREVIOUS_INSTANCE_UNCLEAN] if unclean else [])
            for account in conn.execute("SELECT id FROM accounts ORDER BY id").fetchall():
                conn.execute(
                    "INSERT INTO account_restriction (account_id, restriction_state, restriction_reasons, "
                    "restriction_since, restriction_epoch, last_startup_run_id, updated_at) "
                    "VALUES (?, 'RESTRICTED', ?, ?, ?, NULL, datetime('now')) "
                    "ON CONFLICT(account_id) DO UPDATE SET restriction_state = 'RESTRICTED', "
                    "restriction_reasons = excluded.restriction_reasons, restriction_since = excluded.restriction_since, "
                    "restriction_epoch = excluded.restriction_epoch, last_startup_run_id = NULL, "
                    "updated_at = excluded.updated_at",
                    (account["id"], json.dumps(reasons), at, epoch))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    with _state.cv:
        _state.role, _state.reason = OWNER, None
        _state.epoch, _state.instance_uuid, _state.started_at = epoch, mine, at
        _state.heartbeat_seq = 0
        _state.previous_unclean = unclean
        _state.last_hb_wall, _state.last_hb_mono = now(), monotonic()
        _state.last_hb_ok_mono, _state.last_hb_ok_at = _state.last_hb_mono, at
    logger.info("event=instance.owner epoch=%d previous_end=%s waited_s=%.1f restriction=%s",
                epoch, previous_end, waited, ",".join(reasons))
    return {"epoch": epoch, "already_owner": False, "previous_unclean": unclean, "previous_end": previous_end,
            "waited_seconds": waited, "conflict": False, "reasons": reasons}


def _enter_conflict(detail: str) -> dict:
    with _state.cv:
        _state.role, _state.reason, _state.detail = CONFLICT, INSTANCE_CONFLICT, detail
    logger.critical("event=instance.conflict code=%s URGENT: %s. This process stays read-only; stop every other "
                    "copy of the terminal (another machine, a synced or network folder, an older build) and restart.",
                    INSTANCE_CONFLICT, detail)
    return {"epoch": None, "already_owner": False, "previous_unclean": False, "conflict": True, "detail": detail}


# ---- authority ------------------------------------------------------------------------------------

def authority_ok() -> bool:
    return _state.role == OWNER and not _state.shutting_down


def refusal_code() -> str | None:
    """None when this process may write; otherwise the code that explains why not."""
    role = _state.role
    if role == OWNER:
        return INSTANCE_NOT_OWNER if _state.shutting_down else None
    if role == NON_OWNER:
        return ANOTHER_INSTANCE_RUNNING
    if role == DRIVE_REFUSED:
        return DATABASE_NOT_ON_LOCAL_DRIVE
    if role == CONFLICT:
        return INSTANCE_CONFLICT
    if role == LOST:
        return _state.reason or INSTANCE_LOCK_LOST
    return INSTANCE_NOT_OWNER


def refusal_message(code: str) -> str:
    return _MESSAGES.get(code, code)


def require_write_authority() -> None:
    """FastAPI dependency on every write route: 503 unless this process owns the database. Never
    blocks on RESTRICTED (that only refuses new live entries, at confirmation)."""
    code = refusal_code()
    if code is not None:
        raise HTTPException(status_code=503, detail={"error_code": code, "message": refusal_message(code)})


# `dependencies=WRITE_AUTHORITY` on every write route (and the GETs that write: /login, /callback,
# the ticket chart). tests/test_write_authority_routes.py fails on any mutating route without it.
WRITE_AUTHORITY = [Depends(require_write_authority)]


@contextlib.contextmanager
def broker_write(route, method):
    """Wraps one broker write: refused (InstanceNotOwner, nothing sent) without authority; otherwise
    counted as in flight until it returns, so shutdown can wait for it and record it."""
    with _state.cv:
        code = refusal_code()
        if code is not None:
            logger.warning("event=broker_write.refused route=%s method=%s code=%s", route, method, code)
            raise InstanceNotOwner(str(route), str(method).upper(), code)
        _state.in_flight += 1
    try:
        yield
    finally:
        with _state.cv:
            _state.in_flight -= 1
            _state.cv.notify_all()


def loop_may_run(name: str) -> bool:
    """The authority check at the start of every background-loop cycle. False: the loop returns."""
    if authority_ok():
        return True
    if name not in _state.logged_loops:
        _state.logged_loops.add(name)
        logger.error("event=worker.authority_lost worker=%s code=%s: the loop stops", name, refusal_code())
    return False


def lose_authority(code: str, detail: str) -> None:
    """Called the moment a fenced transaction or heartbeat finds another epoch (or a foreign writer).
    Authority drops BEFORE anything else; it is never regained in this process."""
    with _state.cv:
        if _state.role != OWNER:
            return
        _state.role, _state.reason, _state.detail = LOST, code, detail
    logger.critical("event=instance.authority_lost code=%s epoch=%s: %s. Loops stop, write routes return 503, the "
                    "broker fence refuses. Restart this process after checking which instance owns the database.",
                    code, _state.epoch, detail)


def current_identity() -> tuple[int, str]:
    with _state.cv:
        if _state.role != OWNER:
            raise FencingError(f"{refusal_code()}: no write authority")
        return int(_state.epoch), str(_state.instance_uuid)


def owner_epoch() -> int | None:
    return _state.epoch if _state.role == OWNER else None


@contextlib.contextmanager
def fenced_transaction(conn: sqlite3.Connection):
    """A Phase 2 coordination transaction: BEGIN IMMEDIATE, then our (epoch, instance_uuid) must still
    be the row's, checked under the write lock so the check and the writes are atomic against every
    other connection and process. On a mismatch: roll back and drop authority (INSTANCE_LOCK_LOST)."""
    epoch, mine = current_identity()
    if _state.db_path is not None:
        target = _database_file(conn)
        if target is not None and os.path.normcase(str(target)) != os.path.normcase(str(_state.db_path)):
            raise WrongDatabase(f"fenced transaction refused: {target} is not the locked database {_state.db_path}")
    if conn.in_transaction:
        raise FencingError("fenced transaction refused: the connection already has an open transaction")
    previous_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT epoch, instance_uuid FROM app_instance WHERE id = 1").fetchone()
            if row is None or row[0] != epoch or row[1] != mine:
                conn.execute("ROLLBACK")
                found = None if row is None else (row[0], row[1])
                lose_authority(INSTANCE_LOCK_LOST, f"app_instance holds {found}, not this process's epoch {epoch}")
                raise FencingError(f"{INSTANCE_LOCK_LOST}: epoch {epoch} is no longer current ({found})")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = previous_isolation


# ---- heartbeat and suspend detection (§7.4) -------------------------------------------------------

def heartbeat_once(*, now=_utc_now, monotonic=time.monotonic) -> dict:
    """One fenced heartbeat. Detects: another epoch (INSTANCE_LOCK_LOST), a foreign writer with our
    identity (INSTANCE_CONFLICT), a suspend/resume gap (RESUMED_AFTER_SUSPEND: either clock jumped by
    more than 3 heartbeats) and a prolonged failure to write (HEARTBEAT_STALE). A missed heartbeat
    never loses ownership: the OS lock still holds."""
    result = {"ok": False, "resumed": False, "lost": False}
    with _state.cv:
        if _state.role != OWNER or _state.shutting_down:
            return result
        epoch, mine, expected_seq, db_path = _state.epoch, _state.instance_uuid, _state.heartbeat_seq, _state.db_path
        wall, mono = now(), monotonic()
        result["resumed"] = _detect_gap_locked(wall, mono, "heartbeat")
    if result["resumed"]:
        try:
            from backend import gtt_observations
            gtt_observations.reset()
        except Exception:
            logger.exception("Could not reset GTT observations after a resume")
    try:
        conn = _connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE app_instance SET heartbeat_at = ?, heartbeat_seq = heartbeat_seq + 1, "
                "updated_at = datetime('now') WHERE id = 1 AND epoch = ? AND instance_uuid = ? AND heartbeat_seq = ?",
                (_iso(wall), epoch, mine, expected_seq)).rowcount
            if changed != 1:
                row = conn.execute("SELECT epoch, instance_uuid, heartbeat_seq FROM app_instance WHERE id = 1").fetchone()
                conn.execute("ROLLBACK")
                if row is not None and row[0] == epoch and row[1] == mine:
                    lose_authority(INSTANCE_CONFLICT, f"heartbeat_seq is {row[2]}, expected {expected_seq}: another "
                                                      f"writer is using this process's identity")
                else:
                    found = None if row is None else (row[0], row[1])
                    lose_authority(INSTANCE_LOCK_LOST, f"the heartbeat found {found}, not epoch {epoch}")
                result["lost"] = True
                return result
            conn.execute("COMMIT")
        finally:
            conn.close()
    except sqlite3.Error as e:
        with _state.cv:
            stale = _state.last_hb_ok_mono is not None and mono - _state.last_hb_ok_mono > config.INSTANCE_LEASE_SECONDS
            if stale and HEARTBEAT_STALE not in _state.process_reasons:
                _state.process_reasons.add(HEARTBEAT_STALE)
                logger.error("event=instance.heartbeat_stale: no successful heartbeat for more than %s s; new entries "
                             "are refused until one succeeds", config.INSTANCE_LEASE_SECONDS)
        logger.warning("event=instance.heartbeat_failed %s: %s", type(e).__name__, e)
        return result
    with _state.cv:
        _state.heartbeat_seq = expected_seq + 1
        _state.last_hb_ok_mono, _state.last_hb_ok_at = mono, _iso(wall)
        _state.process_reasons.discard(HEARTBEAT_STALE)
    result["ok"] = True
    return result


def _detect_gap_locked(wall: datetime, mono: float, detector: str) -> bool:
    """M-6 / R2-7 point 1, called with _state.cv held: compares BOTH clocks with the one shared baseline and
    refreshes it in the same critical section, so whichever detector (the heartbeat or an engine CAS) enters
    first after a wake sees the gap and every later one does not: exactly one resume_seq per gap, none lost.
    Memory only: no I/O, no other lock (the caller may hold the SQLite write lock; the order is DB lock -> cv)."""
    gap = False
    threshold = 3 * config.HEARTBEAT_SECONDS
    if _state.last_hb_wall is not None and _state.last_hb_mono is not None:
        gap_wall = (wall - _state.last_hb_wall).total_seconds()
        gap_mono = mono - _state.last_hb_mono
        if gap_wall > threshold or gap_mono > threshold:
            gap = True
            _state.resume_seq += 1
            _state.process_reasons.add(RESUMED_AFTER_SUSPEND)
            _state.resumed_at = wall if _state.resumed_at is None else max(_state.resumed_at, wall)
            logger.warning("event=instance.resumed_after_suspend detector=%s resume_seq=%d gap_wall_s=%.1f "
                           "gap_monotonic_s=%.1f: GTT observations marked stale, Stage 1 reconciliation re-runs "
                           "before new entries", detector, _state.resume_seq, gap_wall, gap_mono)
    _state.last_hb_wall, _state.last_hb_mono = wall, mono
    return gap


def clock_gap_check(*, now=_utc_now, monotonic=time.monotonic) -> dict:
    """M-6: the synchronous clock-gap check run first inside every engine send CAS and again immediately before
    the broker call. In-memory updates only (R2-7 point 4): note_resume and gtt_observations.reset() are left
    to the heartbeat loop (it consumes any resume_seq it has not noted), the G4 reset to its consumers."""
    with _state.cv:
        if _state.role != OWNER:
            return {"gap": False, "resume_seq": _state.resume_seq, "owner": False}
        gap = _detect_gap_locked(now(), monotonic(), "engine")
        return {"gap": gap, "resume_seq": _state.resume_seq, "owner": True}


def resume_seq() -> int:
    with _state.cv:
        return _state.resume_seq


def take_unnoted_resume() -> int | None:
    """R2-7 point 2: the resume_seq the heartbeat loop must still note (persist + Stage 1), or None. Marks it
    noted, so note_resume runs once per detected gap whichever detector saw it."""
    with _state.cv:
        if _state.resume_seq > _state.last_noted_seq:
            _state.last_noted_seq = _state.resume_seq
            return _state.resume_seq
        return None


async def run_heartbeat_loop() -> None:
    """The supervisor loop 'instance_heartbeat' (not one of the five trading loops)."""
    while True:
        await asyncio.sleep(config.HEARTBEAT_SECONDS)
        if not loop_may_run("instance_heartbeat"):
            return
        await asyncio.to_thread(heartbeat_once)
        consume_resume()


def consume_resume() -> bool:
    """Notes a resume detected by ANY detector (heartbeat or engine CAS) exactly once: GTT observations reset and
    note_resume (persisted RESUMED_AFTER_SUSPEND + a Stage 1 request). True if one was noted."""
    if take_unnoted_resume() is None:
        return False
    try:
        from backend import gtt_observations
        gtt_observations.reset()
    except Exception:
        logger.exception("Could not reset GTT observations after a resume")
    from backend import startup_reconciliation
    startup_reconciliation.note_resume()
    return True


def process_reasons() -> set[str]:
    with _state.cv:
        return set(_state.process_reasons)


def resumed_at() -> datetime | None:
    return _state.resumed_at


def clear_resume(before: datetime) -> None:
    """A Stage 1 run that passed and started after the resume clears RESUMED_AFTER_SUSPEND."""
    with _state.cv:
        if _state.resumed_at is not None and _state.resumed_at <= before:
            _state.process_reasons.discard(RESUMED_AFTER_SUSPEND)


# ---- shutdown (§7.6) ------------------------------------------------------------------------------

def begin_shutdown() -> None:
    """Step 1: authority drops; from now on the fence refuses new broker writes, routes return 503."""
    with _state.cv:
        _state.shutting_down = True


def finish_shutdown(timeout: float | None = None, *, now=_utc_now) -> dict:
    """Steps 3-5: wait (bounded) for in-flight broker writes, record the shutdown in a fenced
    transaction (clean only when nothing was in flight), then release the lock."""
    timeout = config.SHUTDOWN_INFLIGHT_WAIT_SECONDS if timeout is None else timeout
    with _state.cv:
        _state.shutting_down = True
        _state.cv.wait_for(lambda: _state.in_flight == 0, timeout=timeout)
        in_flight = _state.in_flight
        role, epoch, mine, db_path = _state.role, _state.epoch, _state.instance_uuid, _state.db_path
    recorded = False
    if role == OWNER:
        clean = in_flight == 0
        note = None if clean else f"{in_flight} broker write(s) still in flight at exit"
        at = _iso(now())
        try:
            conn = _connect(db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                changed = conn.execute(
                    "UPDATE app_instance SET state = 'stopped_clean', clean_shutdown = ?, shutdown_at = ?, "
                    "shutdown_note = ?, updated_at = datetime('now') WHERE id = 1 AND epoch = ? AND instance_uuid = ?",
                    (1 if clean else 0, at, note, epoch, mine)).rowcount
                if changed == 1:
                    conn.execute("UPDATE app_instance_history SET ended_at = ?, end_state = 'stopped_clean', "
                                 "note = COALESCE(?, note) WHERE epoch = ?", (at, note, epoch))
                    conn.execute("COMMIT")
                    recorded = True
                else:
                    conn.execute("ROLLBACK")
                    lose_authority(INSTANCE_LOCK_LOST, "the shutdown record found another epoch")
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.error("event=instance.shutdown_record_failed %s: %s (the successor will treat this as unclean)",
                         type(e).__name__, e)
        logger.info("event=instance.shutdown epoch=%s clean=%s in_flight=%d recorded=%s", epoch, clean, in_flight,
                    recorded)
    release()
    return {"recorded": recorded, "in_flight": in_flight}


def release() -> None:
    """Closes the lock handle (the OS releases the lock). Idempotent."""
    with _state.cv:
        fd = _state.lock_fd
        _state.lock_fd = None
        if _state.role in (OWNER, LOCKED):
            _state.role = RELEASED
            _state.shutting_down = False
    if fd is not None:
        _os_unlock_and_close(fd)


def reset_for_tests() -> None:
    """Test harness only: release anything held and forget all process state."""
    release()
    with _state.cv:
        _reset_state_locked()


# ---- diagnostics and the non-owner status page ----------------------------------------------------

def status() -> dict:
    with _state.cv:
        info = {
            "role": _state.role,
            "authority": authority_ok(),
            "refusal_code": refusal_code(),
            "reason_detail": _state.detail,
            "epoch": _state.epoch,
            "instance_uuid": _state.instance_uuid,
            "pid": os.getpid(),
            "started_at": _state.started_at,
            "lock_path": str(_state.lock_path) if _state.lock_path else None,
            "db_path": str(_state.db_path) if _state.db_path else None,
            "drive_problem": _state.drive_problem,
            "warnings": list(_state.warnings),
            "previous_instance_unclean": _state.previous_unclean,
            "process_reasons": sorted(_state.process_reasons),
            "last_heartbeat_ok_at": _state.last_hb_ok_at,
            "heartbeat_seq": _state.heartbeat_seq,
            "broker_writes_in_flight": _state.in_flight,
            "shutting_down": _state.shutting_down,
        }
    if info["role"] in READ_ONLY_ROLES and _state.db_path is not None:
        info["owner"] = read_owner_readonly(_state.db_path)
    return info


def _status_page() -> str:
    info = status()
    owner = info.get("owner") or {}
    code = info["refusal_code"] or ""
    rows = [("This process", f"pid {info['pid']}, read-only ({code})"),
            ("Database", info["db_path"] or "?")]
    if info["drive_problem"]:
        rows.append(("Drive", info["drive_problem"]))
    if owner.get("error"):
        rows.append(("Owner", f"unavailable ({owner['error']})"))
    elif owner:
        rows += [("Owner pid", f"{owner.get('pid')} on {owner.get('host')} ({owner.get('process')})"),
                 ("Owner started", owner.get("started_at")),
                 ("Owner heartbeat", f"{owner.get('heartbeat_at')} ({owner.get('heartbeat_age_seconds')} s ago)"),
                 ("Owner state", owner.get("state"))]
    body = "".join(f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>" for k, v in rows)
    return ("<!doctype html><html><head><meta charset='utf-8'><title>Kite Terminal: read-only</title></head>"
            "<body style='font-family:system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem'>"
            f"<h1>This Kite Terminal process is read-only</h1><p>{html.escape(refusal_message(code))}</p>"
            f"<table cellpadding='6'>{body}</table>"
            "<p>Nothing can be changed from this process: no login, no orders, no exits. Use the owning instance, "
            "or stop it and start this one again.</p></body></html>")


class InstanceOwnershipGuard:
    """ASGI middleware, active only in the read-only roles (non-owner, conflict, drive refused): `GET /`
    is a status page, `GET /api/health` reports the role, and everything else (every API route,
    login, callback, the WebSocket) is refused with 503. An owner, or a process that never attempted
    acquisition (tests), passes straight through; write routes still check require_write_authority."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket") or _state.role not in READ_ONLY_ROLES:
            await self.app(scope, receive, send)
            return
        from starlette.responses import HTMLResponse, JSONResponse
        code = refusal_code() or ANOTHER_INSTANCE_RUNNING
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1013})
            return
        method, path = scope.get("method", "GET").upper(), scope.get("path", "")
        if method in ("GET", "HEAD") and path == "/":
            response = HTMLResponse(_status_page(), status_code=503)
        elif method in ("GET", "HEAD") and path == "/api/health":
            response = JSONResponse({"status": "read_only", "role": _state.role, "error_code": code}, status_code=503)
        else:
            response = JSONResponse({"detail": {"error_code": code, "message": refusal_message(code)}}, status_code=503)
        await response(scope, receive, send)
