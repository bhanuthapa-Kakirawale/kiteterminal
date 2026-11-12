"""Phase 2 controlled Windows integration checks that cannot run in the normal suite (WIN-INT, design §10.2).
Not a pytest module. Run by hand on a controlled Windows machine, from a scratch checkout, with the app
STOPPED. Every database here is a fresh scratch database in a temporary folder; the account database,
.env and the broker are never touched (the Phase 0 guard is installed first, as in every child process).

    python tests/winint_phase2.py l3-subst X:          # X: = a FREE drive letter; mapped and removed again
    python tests/winint_phase2.py l6-share \\\\host\\share\\dir   # a writable network share (or a mapped-drive path)
    python tests/winint_phase2.py r8-sleep             # then sleep or hibernate the machine for >= 2 minutes
    python tests/winint_phase2.py l2-timing            # optional: release timing after a hard kill (also in CI)

Each check prints PASS or FAIL with its evidence. Record the output in the Phase 2 report.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import real_data_guard  # noqa: E402

real_data_guard.install()
real_data_guard.neutralise_dotenv()
real_data_guard.scrub_kite_environment()

CHILD = HERE / "instance_lock_child.py"


def _scratch_app(folder: Path) -> Path:
    from cryptography.fernet import Fernet
    os.environ["KITE_TERMINAL_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    os.environ["KITE_API_KEY"], os.environ["KITE_API_SECRET"] = "test_api_key", "test_api_secret"
    from backend import crypto, db, installation_state
    crypto.ENV_PATH = folder / ".env"
    db.DB_PATH = folder / "winint.db"
    # V12 final readiness, Phase C: the installation registry stays in the scratch folder (inherited by the child
    # processes); the real one in the user profile is protected by the guard.
    os.environ.setdefault(installation_state.REGISTRY_ENV, str(folder / "installation_registry"))
    db.init_db()
    return db.DB_PATH


def _hold(db: Path) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, str(CHILD), "hold", json.dumps({"db": str(db)})],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "LOCKED"
    return proc


def _result(ok: bool, text: str) -> int:
    print(("PASS " if ok else "FAIL ") + text)
    return 0 if ok else 1


def l3_subst(drive: str) -> int:
    from backend import instance_lock
    drive = drive.rstrip("\\").upper()
    folder = Path(tempfile.mkdtemp(prefix="kt_winint_l3_"))
    db = folder / "alias.db"
    db.write_bytes(b"")
    made = subprocess.run(["subst", drive, str(folder)], capture_output=True, text=True)
    if made.returncode != 0:
        return _result(False, f"subst {drive} failed: {made.stdout} {made.stderr}")
    try:
        holder = _hold(db)
        try:
            alias = Path(drive + "\\alias.db")
            role = instance_lock.acquire(alias)
            same_file = instance_lock.lock_path_for(alias) == instance_lock.lock_path_for(db)
            return _result(role == instance_lock.NON_OWNER,
                           f"L3 subst alias {alias} -> role {role} (lock path resolved to the same file: {same_file})")
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)
            instance_lock.reset_for_tests()
    finally:
        subprocess.run(["subst", drive, "/D"], capture_output=True)


def l6_share(share_dir: str) -> int:
    from backend import instance_lock
    db = Path(share_dir) / "kt_winint_l6.db"
    problem = instance_lock.local_drive_problem(db)
    role = instance_lock.acquire(db)
    instance_lock.reset_for_tests()
    return _result(role == instance_lock.DRIVE_REFUSED and problem is not None,
                   f"L6 {db}: role {role}; reason: {problem}")


def r8_sleep() -> int:
    from backend import config, instance_lock
    folder = Path(tempfile.mkdtemp(prefix="kt_winint_r8_"))
    db = _scratch_app(folder)
    assert instance_lock.acquire(db) == instance_lock.LOCKED
    took = instance_lock.takeover(poll_seconds=0.5)
    print(f"owner epoch {took['epoch']} on {db}. Heartbeat every {config.HEARTBEAT_SECONDS} s.")
    print("NOW put the machine to sleep or hibernate for at least 2 minutes, wake it, and wait for the result.")
    other_attempted = False
    deadline = time.monotonic() + 3600
    while time.monotonic() < deadline:
        time.sleep(config.HEARTBEAT_SECONDS)
        result = instance_lock.heartbeat_once()
        print(time.strftime("%H:%M:%S"), json.dumps(result), instance_lock.status()["process_reasons"])
        if result["resumed"]:
            # While suspended the lock stayed held: a second process still cannot take it after the resume.
            probe = subprocess.run([sys.executable, str(CHILD), "hold", json.dumps({"db": str(db), "exit_code": 0})],
                                   capture_output=True, text=True, timeout=120)
            other_attempted = True
            busy = probe.stdout.strip() == "BUSY"
            ok = instance_lock.RESUMED_AFTER_SUSPEND in instance_lock.process_reasons() and busy and result["ok"]
            instance_lock.finish_shutdown(timeout=1)
            return _result(ok, f"R8 resume detected (RESUMED_AFTER_SUSPEND set: "
                               f"{instance_lock.RESUMED_AFTER_SUSPEND in instance_lock.process_reasons()}); "
                               f"lock still held across the suspend (second process got {probe.stdout.strip()!r}); "
                               f"heartbeat ok after resume: {result['ok']}")
    instance_lock.finish_shutdown(timeout=1)
    return _result(False, f"R8 no resume detected within an hour (second-process probe attempted: {other_attempted})")


def l2_timing() -> int:
    from backend import instance_lock
    folder = Path(tempfile.mkdtemp(prefix="kt_winint_l2_"))
    db = folder / "crash.db"
    db.write_bytes(b"")
    timings = []
    for _ in range(20):
        out = subprocess.run([sys.executable, str(CHILD), "hold", json.dumps({"db": str(db), "exit_code": 137})],
                             capture_output=True, text=True, timeout=120)
        assert out.stdout.strip() == "LOCKED"
        started = time.monotonic()
        fd = None
        while fd is None and time.monotonic() - started < 60:
            fd = instance_lock.try_lock_file(instance_lock.lock_path_for(db))
        timings.append(time.monotonic() - started)
        if fd is not None:
            instance_lock.release_lock_file(fd)
    return _result(max(timings) < 60, f"L2 release after os._exit(137), 20 runs: max {max(timings) * 1000:.1f} ms, "
                                      f"median {sorted(timings)[10] * 1000:.1f} ms")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "l3-subst" and len(sys.argv) == 3:
        sys.exit(l3_subst(sys.argv[2]))
    if command == "l6-share" and len(sys.argv) == 3:
        sys.exit(l6_share(sys.argv[2]))
    if command == "r8-sleep":
        sys.exit(r8_sleep())
    if command == "l2-timing":
        sys.exit(l2_timing())
    print(__doc__)
    sys.exit(2)
