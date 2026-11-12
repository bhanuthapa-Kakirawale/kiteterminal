"""Phase 3 S3: the preamble of EVERY child process the multi-process harness starts. Not a test module.

Import it FIRST, before any other import that could touch a file or the network (and before any
`backend` import). tests/test_mp_harness_selftest.py checks this statically for every S3 child entry
script. On import it:

1. puts tests/ and the repository root on sys.path;
2. installs the Phase 0 isolation guard in THIS process (tests/real_data_guard.py): a child is outside
   the parent's audit hook, so it must install its own;
3. neutralises python-dotenv and scrubs every inherited KITE_* variable;
4. starts recording every open / sqlite3.connect of this process.

write_report() (called by the child after every command, and at exit) writes this process's guard
evidence as JSON to the path in KT_CHILD_GUARD_REPORT: refusals, unexpected refusals, and the data
paths it opened. The parent reads the report after the child has stopped and fails the test (and, via
tests/conftest.py, the session) on any unexpected refusal or protected path. This is the parent-side
assertion of docs/PHASE_2_DESIGN_REVIEW.md §10.1, which Phase 2 never built.

A child killed by TerminateProcess cannot write a final report; its last report is the one written after
its last completed command. A child that crashes on purpose (os._exit) writes it first.

    python tests/child_bootstrap.py --sleep <seconds>     a guarded sleeper (the W2 grandchild)
"""

import atexit
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(REPO), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import real_data_guard  # noqa: E402

real_data_guard.install()
real_data_guard.neutralise_dotenv()
SCRUBBED = real_data_guard.scrub_kite_environment()

REPORT_ENV = "KT_CHILD_GUARD_REPORT"
ROLE_ENV = "KT_CHILD_ROLE"

_recording = real_data_guard.recording()
_opened = _recording.__enter__()  # recorded for the life of the process
_STARTED = time.time()

# Opens of the interpreter, the standard library, site-packages and the repository's own source are not
# data: they are left out of the report (the full count is still reported).
_CODE_SUFFIXES = (".py", ".pyc", ".pyd", ".dll", ".so", ".pth", ".zip", ".egg-link", ".typed", ".json.gz")
_CODE_ROOTS = tuple(os.path.normcase(os.path.abspath(p)) for p in {sys.prefix, sys.base_prefix, sys.exec_prefix})


class UnsafePath(RuntimeError):
    """A child was given a path outside its tmp root, or a protected path. It refuses to start."""


def _is_code_open(path: str) -> bool:
    normalised = os.path.normcase(os.path.abspath(path))
    return normalised.endswith(_CODE_SUFFIXES) or normalised.startswith(_CODE_ROOTS)


def require_safe_paths(root, *paths) -> None:
    """Every path must sit under `root` (the test's tmp_path) and be neither protected itself nor the
    instance-lock path of a protected file. Pure path logic: nothing is opened or stat'ed."""
    base = os.path.normcase(os.path.realpath(root))
    for raw in paths:
        if raw is None:
            continue
        path = os.path.normcase(os.path.realpath(raw))
        lock = path + ".instance.lock"
        if real_data_guard.is_protected(raw) or real_data_guard.is_protected(path) or real_data_guard.is_protected(lock):
            raise UnsafePath(f"refusing a protected path: {raw}")
        if os.path.commonpath([base, path]) != base:
            raise UnsafePath(f"refusing a path outside the test's tmp root {root}: {raw}")


def report() -> dict:
    data_opens = sorted({p for _, p in _opened if not _is_code_open(p)})
    refusals = real_data_guard.refusals()
    return {
        "pid": os.getpid(),
        "role": os.environ.get(ROLE_ENV, "?"),
        "started": _STARTED,
        "written": time.time(),
        "guard_installed": real_data_guard.installed(),
        "dotenv_neutralised": real_data_guard.dotenv_neutralised(),
        "scrubbed_kite_names": list(SCRUBBED),
        "kite_vars_at_report": sorted(k for k in os.environ if k.upper().startswith("KITE_")),
        "refusals": [list(r) for r in refusals],
        "unexpected": [list(r) for r in real_data_guard.unexpected_refusals()],
        "opens_total": len(_opened),
        "data_opens": data_opens[:500],
        "protected_opens": [p for p in data_opens if real_data_guard.is_protected(p)],
    }


def write_report() -> dict | None:
    """Atomically writes this process's report to $KT_CHILD_GUARD_REPORT (a no-op without it)."""
    target = os.environ.get(REPORT_ENV)
    data = report()
    if not target:
        return data
    tmp = target + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(tmp, target)
    return data


atexit.register(write_report)


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--sleep":
    time.sleep(float(sys.argv[2]))
