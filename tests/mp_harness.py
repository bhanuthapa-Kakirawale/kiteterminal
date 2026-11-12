"""Phase 3 S3: parent-side orchestration of the multi-process harness. Not a test module.

Imported by the S3 test modules (and read by tests/conftest.py for the session summary). It runs in the
pytest process, under the parent's guard, and starts children that install their own (child_bootstrap).

- Harness / the `mp` fixture: one per test. Every child, the shared FakeKite server and every file they
  use live under the test's tmp_path. Teardown always runs: gates released, suspended children resumed,
  then exit -> terminate -> kill, the Windows Job Object closed (which kills anything left, grandchildren
  included), every pid checked dead, and every child's guard report checked.
- Child: a child process spoken to in JSON lines over stdin/stdout. Reader threads drain stdout AND stderr
  (a full pipe would deadlock a child on Windows). Every wait has a deadline; a timeout or a dead child
  raises with the child's stderr tail.
- Job, suspend/resume, pid_alive: Windows primitives through ctypes (stdlib only, documented Win32 calls:
  Job Objects, CreateToolhelp32Snapshot / SuspendThread / ResumeThread). POSIX falls back to signals.

Barriers are atomic mkdir()s the children only test for existence (the Phase 2 L1 lesson: never read a
file another process may be creating or replacing on Windows).
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SERVER_SCRIPT = HERE / "shared_fake_kite.py"
APP_SCRIPT = HERE / "mp_app_child.py"
BOOTSTRAP_SCRIPT = HERE / "child_bootstrap.py"
# Every script the S3 harness runs as a child: each must import child_bootstrap first (checked statically).
CHILD_ENTRY_SCRIPTS = (SERVER_SCRIPT, APP_SCRIPT, BOOTSTRAP_SCRIPT)

DEFAULT_DEADLINE = 30.0
# Start-up (interpreter + backend import) is the slowest step and the most load-sensitive: one run on this
# laptop under desktop load saw a child take more than 60 s to become ready.
READY_DEADLINE = 120.0
GRACE = 5.0

# Session-wide evidence, read by tests/conftest.py (terminal summary and session failure).
ALL_REPORTS: list[dict] = []
MISSING_REPORTS: list[str] = []
LIVE_PIDS: set[int] = set()
ORPHANS: list[int] = []


class HarnessTimeout(AssertionError):
    pass


class ChildDied(AssertionError):
    pass


class ChildFailed(AssertionError):
    pass


# ---------------------------------------------------------------- Windows primitives
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _k32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.c_void_p)
    _k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    _k32.OpenThread.restype = wintypes.HANDLE
    _k32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.SuspendThread.argtypes = (wintypes.HANDLE,)
    _k32.SuspendThread.restype = wintypes.DWORD
    _k32.ResumeThread.argtypes = (wintypes.HANDLE,)
    _k32.ResumeThread.restype = wintypes.DWORD

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                                                    "OtherOperationCount", "ReadTransferCount",
                                                    "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                    ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG),
                    ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]

    _k32.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry))
    _k32.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry))

    class _ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    _k32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
    _k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
    _SNAPPROCESS = 0x2

    _JOB_EXTENDED_LIMITS = 9
    _JOB_PROCESS_ID_LIST = 3
    _KILL_ON_JOB_CLOSE = 0x2000
    _SYNCHRONIZE = 0x00100000
    _QUERY_LIMITED = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _THREAD_SUSPEND_RESUME = 0x0002
    _SNAPTHREAD = 0x4
    _WAIT_TIMEOUT = 0x102
    _INVALID_HANDLE = ctypes.c_void_p(-1).value


class Job:
    """A Windows Job Object with KILL_ON_JOB_CLOSE: closing it kills every process in it, including any
    grandchild a child started. A no-op elsewhere (POSIX children are stopped individually)."""

    def __init__(self, enabled: bool = True):
        self.handle = None
        if os.name != "nt" or not enabled:
            return
        handle = _k32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(handle, _JOB_EXTENDED_LIMITS, ctypes.byref(info), ctypes.sizeof(info)):
            _k32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        self.handle = handle

    def assign(self, proc: subprocess.Popen) -> None:
        if self.handle is None:
            return
        if not _k32.AssignProcessToJobObject(self.handle, int(proc._handle)):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def pids(self) -> list[int]:
        if self.handle is None:
            return []

        class _PidList(ctypes.Structure):
            _fields_ = [("Assigned", wintypes.DWORD), ("InList", wintypes.DWORD),
                        ("Pids", ctypes.c_size_t * 512)]

        data = _PidList()
        if not _k32.QueryInformationJobObject(self.handle, _JOB_PROCESS_ID_LIST, ctypes.byref(data),
                                              ctypes.sizeof(data), None):
            raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
        return [int(data.Pids[i]) for i in range(data.InList)]

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            _k32.CloseHandle(handle)          # KILL_ON_JOB_CLOSE: everything still in the job dies


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        handle = _k32.OpenProcess(_SYNCHRONIZE | _QUERY_LIMITED, False, pid)
        if not handle:
            return False
        try:
            return _k32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
        finally:
            _k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def descendants(roots) -> set[int]:
    """Every live descendant of `roots` (parent links from a Toolhelp process snapshot; /proc elsewhere).

    Why not the Job alone: a venv's python.exe launcher runs the real interpreter outside the Job it was
    assigned to (observed here: only the launchers are listed in the Job). The interpreter dies with its
    launcher, but a grandchild it starts would not. Teardown therefore also kills every descendant it
    can see, and kill_hard() records a child's descendants before they become orphans."""
    parent_of: dict[int, int] = {}
    if os.name == "nt":
        snap = _k32.CreateToolhelp32Snapshot(_SNAPPROCESS, 0)
        if not snap or snap == _INVALID_HANDLE:
            raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(_ProcessEntry)
            ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                parent_of[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = _k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            _k32.CloseHandle(snap)
    else:
        for name in os.listdir("/proc"):
            if name.isdigit():
                try:
                    stat = Path(f"/proc/{name}/stat").read_text()
                    parent_of[int(name)] = int(stat.rsplit(")", 1)[1].split()[1])
                except (OSError, ValueError, IndexError):
                    pass
    found: set[int] = set()
    frontier = set(roots)
    born: dict[int, object] = {}

    def created(pid):
        if pid not in born:
            from datetime import datetime
            from backend.instance_lock import process_created_at
            text = process_created_at(pid)
            born[pid] = datetime.fromisoformat(text) if text else None
        return born[pid]

    def really_child(pid, parent):
        # Windows keeps a dead parent's pid in th32ParentProcessID, and pids are reused: an unrelated, OLDER
        # process can look like a child of a harness process that later received its dead parent's pid (seen
        # in S9: a security-software process was nearly terminated). A child is never older than its parent; a
        # process whose creation time cannot be read is not claimed (teardown's orphan check still reports
        # any real leftover).
        if os.name != "nt":
            return True
        child_at, parent_at = created(pid), created(parent)
        return child_at is not None and parent_at is not None and child_at >= parent_at

    while frontier:
        children = {pid for pid, parent in parent_of.items() if parent in frontier and pid not in found
                    and pid not in roots and really_child(pid, parent)}
        found |= children
        frontier = children
    return found


def terminate_pid(pid: int, code: int = 137) -> None:
    """TerminateProcess (SIGKILL elsewhere): the process dies at once, no cleanup, no atexit."""
    if os.name != "nt":
        os.kill(pid, 9)
        return
    handle = _k32.OpenProcess(_PROCESS_TERMINATE | _SYNCHRONIZE, False, pid)
    if not handle:
        return                                # already gone
    try:
        _k32.TerminateProcess(handle, code)
        _k32.WaitForSingleObject(handle, 10_000)
    finally:
        _k32.CloseHandle(handle)


def suspend(pid: int) -> list[int]:
    """Freezes every thread of `pid` (documented Win32 thread suspension). Threads that appear while
    suspending are caught by re-snapshotting until none is new. Returns the suspended thread ids."""
    if os.name != "nt":
        import signal
        os.kill(pid, signal.SIGSTOP)
        return [pid]
    done: list[int] = []
    while True:
        fresh = [tid for tid in _threads_of(pid) if tid not in done]
        if not fresh:
            return done
        for tid in fresh:
            handle = _k32.OpenThread(_THREAD_SUSPEND_RESUME, False, tid)
            if not handle:
                continue                      # the thread just ended
            try:
                if _k32.SuspendThread(handle) != 0xFFFFFFFF:
                    done.append(tid)
            finally:
                _k32.CloseHandle(handle)


def resume(pid: int, tids: list[int]) -> None:
    if os.name != "nt":
        import signal
        os.kill(pid, signal.SIGCONT)
        return
    for tid in tids:
        handle = _k32.OpenThread(_THREAD_SUSPEND_RESUME, False, tid)
        if handle:
            try:
                _k32.ResumeThread(handle)
            finally:
                _k32.CloseHandle(handle)


def _threads_of(pid: int) -> list[int]:
    snap = _k32.CreateToolhelp32Snapshot(_SNAPTHREAD, 0)
    if not snap or snap == _INVALID_HANDLE:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    try:
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(_ThreadEntry)
        found = []
        ok = _k32.Thread32First(snap, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                found.append(int(entry.th32ThreadID))
            ok = _k32.Thread32Next(snap, ctypes.byref(entry))
        return found
    finally:
        _k32.CloseHandle(snap)


# ---------------------------------------------------------------- children
class Child:
    def __init__(self, harness: "Harness", name: str, argv: list[str], role: str):
        self.harness = harness
        self.name = name
        self.report_path = harness.root / f"{name}.guard.json"
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("KITE_")}
        env.update({"KT_CHILD_GUARD_REPORT": str(self.report_path), "KT_CHILD_ROLE": role,
                    "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
        # The launcher is assigned to the Job right after creation. Creating it SUSPENDED first was tried and
        # removed: the interpreter escaped the Job anyway (descendants() is what reaps it), and resuming
        # through a thread snapshot occasionally left a child suspended for good (two silent start-up stalls).
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env, cwd=str(REPO), text=True, encoding="utf-8", errors="replace", bufsize=1)
        try:
            harness.job.assign(self.proc)
        except OSError:
            pass                              # already gone, or not assignable: descendants() still reaps it
        # On Windows a venv's python.exe is a launcher that runs the real interpreter as ITS child: proc.pid
        # is the launcher. The interpreter (the process that holds locks and handles) reports its own pid
        # when ready (`pid`); kill_hard and suspend act on that one. Both are in the Job.
        self.launcher_pid = self.proc.pid
        self.pid = self.proc.pid
        LIVE_PIDS.add(self.launcher_pid)
        self._messages: queue.Queue = queue.Queue()
        self._inbox: list = []
        self.stdout_tail: deque = deque(maxlen=200)
        self.stderr_tail: deque = deque(maxlen=400)
        self._next_id = 0
        self.suspended: list[int] | None = None
        self._pumps = [threading.Thread(target=self._pump, args=(self.proc.stdout, True), daemon=True),
                       threading.Thread(target=self._pump, args=(self.proc.stderr, False), daemon=True)]
        for t in self._pumps:
            t.start()

    def _pump(self, stream, is_stdout: bool) -> None:
        try:
            for line in stream:
                line = line.rstrip("\n")
                if not is_stdout:
                    self.stderr_tail.append(line)
                    continue
                self.stdout_tail.append(line)
                if line.startswith("{"):
                    try:
                        self._messages.put(json.loads(line))
                    except ValueError:
                        pass
                else:
                    self._messages.put({"text": line})
        except (OSError, ValueError):
            pass
        finally:
            if is_stdout:
                self._messages.put({"eof": True})

    def set_real_pid(self, pid: int) -> None:
        self.pid = int(pid)
        LIVE_PIDS.add(self.pid)

    def diag(self, extra="") -> str:
        return (f"{self.name} (pid {self.pid}, launcher {self.launcher_pid}, returncode {self.proc.poll()}): {extra}\n"
                f"--- stderr tail ---\n" + "\n".join(list(self.stderr_tail)[-40:]) +
                "\n--- stdout tail ---\n" + "\n".join(list(self.stdout_tail)[-10:]))

    def wait(self, predicate, timeout: float = DEFAULT_DEADLINE) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            for i, message in enumerate(self._inbox):
                if predicate(message):
                    return self._inbox.pop(i)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarnessTimeout(self.diag(f"no matching message within {timeout} s"))
            try:
                message = self._messages.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue
            if message.get("eof"):
                self.proc.wait(timeout=GRACE)
                raise ChildDied(self.diag("the child exited before answering"))
            self._inbox.append(message)

    def send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def start(self, op: str, **kwargs) -> int:
        self._next_id += 1
        self.send({"id": self._next_id, "op": op, **kwargs})
        return self._next_id

    def result(self, command_id: int, timeout: float = DEFAULT_DEADLINE, check: bool = True) -> dict:
        reply = self.wait(lambda m: m.get("id") == command_id and "ok" in m, timeout)
        if check and not reply["ok"]:
            raise ChildFailed(self.diag(f"command {command_id} failed: {reply}"))
        return reply

    def cmd(self, op: str, timeout: float = DEFAULT_DEADLINE, check: bool = True, **kwargs) -> dict:
        return self.result(self.start(op, **kwargs), timeout, check)

    def wait_event(self, event: str, timeout: float = DEFAULT_DEADLINE) -> dict:
        return self.wait(lambda m: m.get("event") == event, timeout)

    def wait_exit(self, timeout: float = DEFAULT_DEADLINE) -> int:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise HarnessTimeout(self.diag(f"did not exit within {timeout} s")) from None

    def suspend(self) -> None:
        assert self.suspended is None, "already suspended"
        self.suspended = suspend(self.pid)

    def resume(self) -> None:
        if self.suspended is not None:
            resume(self.pid, self.suspended)
            self.suspended = None

    def kill_hard(self, code: int = 137) -> int:
        """TerminateProcess of the interpreter on Windows (SIGKILL elsewhere): no cleanup, no atexit, no
        report. The launcher then exits with the same code."""
        self.suspended = None                 # a suspended process can be terminated as is
        self.harness.extra_pids |= descendants({self.pid})   # its children are about to become orphans
        terminate_pid(self.pid, code)
        return self.wait_exit(GRACE * 2)

    def stop(self) -> None:
        self.resume()
        if self.proc.poll() is None:
            try:
                self.send({"id": 0, "op": "exit"})
            except (OSError, ValueError):
                pass
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                self.proc.wait(timeout=GRACE)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=GRACE)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=GRACE)
        for t in self._pumps:
            t.join(timeout=GRACE)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def guard_report(self) -> dict | None:
        try:
            return json.loads(self.report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


class Server(Child):
    """The shared FakeKite server child."""

    def __init__(self, harness: "Harness", fake: dict | None, max_hold: float, host: str = "127.0.0.1"):
        config = harness.root / "server.json"
        config.write_text(json.dumps({"root": str(harness.root), "host": host, "fake": fake or {},
                                      "max_hold_seconds": max_hold}), encoding="utf-8")
        super().__init__(harness, "server", [sys.executable, str(SERVER_SCRIPT), str(config)], "server")
        ready = self.wait(lambda m: m.get("text", "").startswith("ready ") or "id" in m, READY_DEADLINE)
        _, self.host, port, pid = ready["text"].split()
        self.port = int(port)
        self.set_real_pid(int(pid))
        self.url = f"http://127.0.0.1:{self.port}"
        from shared_fake_kite import Controller
        self.control = Controller(self.url)

    def stop(self) -> None:
        if self.proc.poll() is None and self.suspended is None:
            try:
                self.control("release_all")
            except Exception:
                pass
        super().stop()


class AppChild(Child):
    """An application process (tests/mp_app_child.py)."""

    def __init__(self, harness: "Harness", name: str, config: dict):
        path = harness.root / f"{name}.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        super().__init__(harness, name, [sys.executable, str(APP_SCRIPT), str(path)], f"app:{name}")
        ready = self.wait(lambda m: m.get("event") == "ready", READY_DEADLINE)
        self.ready = ready
        self.set_real_pid(ready["pid"])


class Harness:
    def __init__(self, tmp_path: Path):
        self.root = Path(tmp_path) / "mp"
        self.root.mkdir(parents=True, exist_ok=True)
        self.job = Job()
        self.children: list[Child] = []
        self.server: Server | None = None
        self.reports: list[dict] = []
        self.allow_missing_reports: set[str] = set()
        self.extra_pids: set[int] = set()     # processes to reap at teardown beyond the direct children
        self._torn_down = False

    def track(self, pid: int) -> None:
        self.extra_pids.add(int(pid))
        LIVE_PIDS.add(int(pid))

    # -- starting things --
    def start_server(self, fake: dict | None = None, max_hold: float = 60.0) -> Server:
        assert self.server is None, "one shared FakeKite per test"
        self.server = Server(self, fake, max_hold)
        self.children.append(self.server)
        return self.server

    def app(self, name: str, env, *, tag: str | None = None, scale: dict | None = None, bypass: bool = False,
            timeout: float = 10.0, db: Path | None = None) -> AppChild:
        """An application child on env's tmp database (or `db`), speaking to the shared FakeKite."""
        assert self.server is not None, "start the shared FakeKite first"
        config = {"root": str(env.tmp_path), "db": str(db or env.db.DB_PATH), "env_path": str(env.tmp_path / ".env"),
                  "key": os.environ["KITE_TERMINAL_ENCRYPTION_KEY"], "server_url": self.server.url,
                  "tag": tag or name, "client_timeout": timeout,
                  # INSTANCE_LEASE_SECONDS stays at the schema minimum (CHECK lease_seconds BETWEEN 15 AND 600); a
                  # successor that must not wait it out shifts its wall clock (set_clock), as Phase 2 E2/P1 did.
                  "scale": {"HEARTBEAT_SECONDS": 0.2, "INSTANCE_LEASE_SECONDS": 15, "SQLITE_BUSY_TIMEOUT_SECONDS": 1,
                            "TAKEOVER_POLL_SECONDS": 0.01, **(scale or {})},
                  "bypass_lock_path": str(self.root / f"{name}.bypass.lock") if bypass else None,
                  "barrier_dir": str(self.barriers())}
        child = AppChild(self, name, config)
        self.children.append(child)
        return child

    def child(self, name: str, argv: list[str], role: str) -> Child:
        child = Child(self, name, argv, role)
        self.children.append(child)
        return child

    def barriers(self) -> Path:
        path = self.root / "barriers"
        path.mkdir(exist_ok=True)
        return path

    def release(self, barrier: str) -> None:
        os.mkdir(self.barriers() / barrier)   # atomic; children only test for existence

    # -- teardown --
    def teardown(self) -> None:
        if self._torn_down:
            return
        self._torn_down = True
        roots = {c.pid for c in self.children} | {c.launcher_pid for c in self.children}
        self.extra_pids |= descendants(roots | self.extra_pids)   # while the parents are alive
        if self.server is not None and self.server.proc.poll() is None and self.server.suspended is None:
            try:
                self.server.control("release_all")
            except Exception:
                pass
        for child in reversed(self.children):     # applications first, the server last
            if child is self.server:
                continue
            child.stop()
        if self.server is not None:
            self.server.stop()
        leftovers = self.job.pids() if self.job.handle is not None else []
        self.job.close()
        pids = roots | set(leftovers) | self.extra_pids
        for pid in sorted(pids):
            if pid_alive(pid):
                terminate_pid(pid, 1)
        deadline = time.monotonic() + GRACE * 2
        while time.monotonic() < deadline and any(pid_alive(p) for p in pids):
            time.sleep(0.05)
        alive = sorted(p for p in pids if pid_alive(p))
        ORPHANS.extend(alive)
        for pid in pids:
            LIVE_PIDS.discard(pid)
        for child in self.children:
            report = child.guard_report()
            if report is None:
                if child.name not in self.allow_missing_reports:
                    MISSING_REPORTS.append(f"{child.name} pid {child.pid}")
                continue
            report["child"] = child.name
            self.reports.append(report)
            ALL_REPORTS.append(report)
        assert not alive, f"processes still alive after teardown: {alive}"

    def guard_problems(self) -> list[str]:
        problems = []
        for report in self.reports:
            name = report["child"]
            if not report["guard_installed"] or not report["dotenv_neutralised"]:
                problems.append(f"{name}: guard not installed / dotenv not neutralised")
            if report["unexpected"]:
                problems.append(f"{name}: unexpected refusals {report['unexpected']}")
            if report["protected_opens"]:
                problems.append(f"{name}: protected paths opened {report['protected_opens']}")
        return problems


MARKERS_DDL = ("CREATE TABLE IF NOT EXISTS mp_markers (id INTEGER PRIMARY KEY AUTOINCREMENT, marker TEXT NOT NULL, "
               "pid INTEGER, epoch INTEGER, kind TEXT, at TEXT NOT NULL DEFAULT (datetime('now')))")


def prepare_app_db(env) -> None:
    """Hands env's tmp database to the application children: the test process stops CLEANLY as owner (so a
    child's takeover needs no lease wait and inherits no PREVIOUS_INSTANCE_UNCLEAN) and releases the lock.
    Adds the harness's marker table (test-only; not an application table)."""
    from backend import instance_lock
    instance_lock.finish_shutdown(timeout=0)
    instance_lock.reset_for_tests()
    conn = env.db.get_connection()
    try:
        conn.execute(MARKERS_DDL)
        conn.commit()
    finally:
        conn.close()


def rows(env, sql: str, *args) -> list[dict]:
    conn = env.db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _session_cleanup() -> None:
    for pid in list(LIVE_PIDS):
        if pid_alive(pid):
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=10)
                else:
                    os.kill(pid, 9)
            except Exception:
                pass


atexit.register(_session_cleanup)


@pytest.fixture
def mp(tmp_path):
    """One multi-process harness per test, torn down (and its guard reports checked) afterwards."""
    harness = Harness(tmp_path)
    try:
        yield harness
    finally:
        harness.teardown()
        problems = harness.guard_problems()
        assert not problems, "child isolation failures: " + "; ".join(problems)


def session_summary() -> dict:
    unexpected = [(r["child"], r["pid"], u) for r in ALL_REPORTS for u in r["unexpected"]]
    protected = [(r["child"], r["pid"], p) for r in ALL_REPORTS for p in r["protected_opens"]]
    return {"children": len(ALL_REPORTS), "refusals": sum(len(r["refusals"]) for r in ALL_REPORTS),
            "expected": sum(1 for r in ALL_REPORTS for x in r["refusals"] if x[3] and not x[2]),
            "decoy": sum(1 for r in ALL_REPORTS for x in r["refusals"] if x[2]),
            "unexpected": unexpected, "protected": protected, "missing_reports": list(MISSING_REPORTS),
            "orphans": list(ORPHANS)}
