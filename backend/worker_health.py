"""Liveness and last-error record of every background worker (PK-22F/H).

Process memory, deliberately: it describes THIS process's workers (a fresh
process has run nothing yet). Each loop wraps one cycle in cycle(name); the
supervisor (backend/workers.py) records task state. Diagnostics and the
readiness check read snapshot(). Error text is redacted and truncated — it is
shown in the UI.
"""

import asyncio
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from backend.redaction import redact_text

FAILING_AFTER = 3  # consecutive failed cycles before a worker reads as failing

_lock = threading.Lock()
_workers: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _entry(name: str) -> dict:
    return _workers.setdefault(name, {
        "name": name, "task_state": "not_started", "cycles": 0, "failures": 0, "consecutive_failures": 0,
        "last_started_at": None, "last_ok_at": None, "last_error_at": None, "last_error": None,
        "last_duration_ms": None, "last_detail": None,
    })


def describe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {redact_text(str(exc))[:200]}"


def record_start(name: str) -> None:
    with _lock:
        _entry(name)["last_started_at"] = _now()


def record_success(name: str, duration_ms: float | None = None, detail: dict | None = None) -> None:
    with _lock:
        e = _entry(name)
        e["cycles"] += 1
        e["consecutive_failures"] = 0
        e["last_ok_at"] = _now()
        e["last_duration_ms"] = None if duration_ms is None else round(duration_ms, 1)
        if detail is not None:
            e["last_detail"] = detail


def record_failure(name: str, exc: BaseException) -> None:
    with _lock:
        e = _entry(name)
        e["cycles"] += 1
        e["failures"] += 1
        e["consecutive_failures"] += 1
        e["last_error_at"] = _now()
        e["last_error"] = describe_error(exc)


def set_task_state(name: str, state: str, exc: BaseException | None = None) -> None:
    """running | stopped | crashed | not_started (set by the supervisor)."""
    with _lock:
        e = _entry(name)
        e["task_state"] = state
        if exc is not None:
            e["last_error_at"] = _now()
            e["last_error"] = describe_error(exc)


@contextmanager
def cycle(name: str):
    """One worker cycle: success, or the failure recorded and re-raised.
    Cancellation (shutdown) is neither."""
    record_start(name)
    started = time.perf_counter()
    try:
        yield
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        record_failure(name, exc)
        raise
    record_success(name, (time.perf_counter() - started) * 1000)


def snapshot() -> dict[str, dict]:
    with _lock:
        out = {name: dict(e) for name, e in _workers.items()}
    for e in out.values():
        e["failing"] = e["consecutive_failures"] >= FAILING_AFTER
    return out


def reset() -> None:
    """Tests."""
    with _lock:
        _workers.clear()
