"""Background-task supervisor (PK-22F).

Before PK-22 the startup hook created its five loops with bare
asyncio.create_task and the shutdown hook cancelled them without waiting:
a second startup in the same process doubled every loop (two order
monitors, two trailing engines), a loop that died was only visible as
"Task exception was never retrieved" at garbage collection, and shutdown
returned while cycles were still running. The supervisor:

* starts each named loop at most once — a live one is never duplicated;
* records every task's state in worker_health and logs an unexpected exit;
* tracks one-off tasks too (post-login work, a manual autopilot run, the
  startup data sync), so shutdown cancels those as well;
* stops everything with a bounded wait. A cycle already inside
  asyncio.to_thread cannot be interrupted mid-call; it finishes its current
  broker/DB call and the loop then exits at its next await.
"""

import asyncio
import itertools
import logging
from collections.abc import Callable, Coroutine

from backend import worker_health

logger = logging.getLogger(__name__)


def _alive(task: asyncio.Task | None) -> bool:
    # A task of an event loop that has since closed (a finished test client,
    # a reloaded server) can never run again: it is not "alive".
    return task is not None and not task.done() and not task.get_loop().is_closed()


# A dead one of these means positions are not being followed: readiness
# reports it (diagnostics.readiness).
CRITICAL_WORKERS = ("order_monitor", "trailing", "reconciliation")


class Supervisor:
    def __init__(self):
        self._loops: dict[str, asyncio.Task] = {}
        self._one_offs: dict[str, asyncio.Task] = {}
        self._counter = itertools.count(1)

    # -- loops ---------------------------------------------------------------

    def start(self, loops: dict[str, Callable[[], Coroutine]]) -> list[str]:
        """Starts every loop that is not already running; returns the names
        started. Idempotent: calling it again (a second startup event in the
        same process) starts nothing that is alive."""
        started = []
        for name, factory in loops.items():
            if _alive(self._loops.get(name)):
                logger.warning("event=worker.duplicate_start_refused worker=%s", name)
                continue
            task = asyncio.create_task(factory(), name=f"kt-loop:{name}")
            task.add_done_callback(lambda t, n=name: self._loop_done(n, t))
            self._loops[name] = task
            worker_health.set_task_state(name, "running")
            started.append(name)
        if started:
            logger.info("event=worker.started workers=%s", ",".join(started))
        return started

    def _loop_done(self, name: str, task: asyncio.Task) -> None:
        if task.cancelled():
            worker_health.set_task_state(name, "stopped")
            return
        exc = task.exception()
        if exc is not None:
            worker_health.set_task_state(name, "crashed", exc)
            logger.error("event=worker.crashed worker=%s", name, exc_info=exc)
        else:
            worker_health.set_task_state(name, "stopped")
            logger.error("event=worker.exited worker=%s (a loop returned; it should run until shutdown)", name)

    def is_running(self, name: str) -> bool:
        return _alive(self._loops.get(name))

    def states(self) -> dict[str, str]:
        return {name: ("running" if not t.done() else "cancelled" if t.cancelled() else "done")
                for name, t in self._loops.items()}

    # -- one-off tasks -------------------------------------------------------

    def spawn(self, name: str, coro: Coroutine) -> asyncio.Task:
        """A tracked one-off task (cancelled at shutdown; failures logged)."""
        key = f"{name}#{next(self._counter)}"
        task = asyncio.create_task(coro, name=f"kt-task:{key}")
        self._one_offs[key] = task
        task.add_done_callback(lambda t, k=key: self._one_off_done(k, t))
        return task

    def _one_off_done(self, key: str, task: asyncio.Task) -> None:
        self._one_offs.pop(key, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("event=task.failed task=%s", key, exc_info=task.exception())

    def pending_one_offs(self) -> list[str]:
        return sorted(k for k, t in self._one_offs.items() if _alive(t))

    # -- shutdown ------------------------------------------------------------

    async def stop(self, timeout: float = 10.0) -> dict:
        tasks = {**{f"loop:{n}": t for n, t in self._loops.items()},
                 **{f"task:{n}": t for n, t in self._one_offs.items()}}
        live = {name: t for name, t in tasks.items() if _alive(t)}
        for t in live.values():
            t.cancel()
        timed_out: list[str] = []
        if live:
            done, pending = await asyncio.wait(list(live.values()), timeout=timeout)
            timed_out = sorted(name for name, t in live.items() if t in pending)
        if timed_out:
            logger.warning("event=worker.stop_timeout workers=%s timeout_s=%s", ",".join(timed_out), timeout)
        logger.info("event=worker.stopped count=%d timed_out=%d", len(live), len(timed_out))
        self._loops = {n: t for n, t in self._loops.items() if _alive(t)}
        self._one_offs = {n: t for n, t in self._one_offs.items() if _alive(t)}
        return {"stopped": sorted(set(live) - set(timed_out)), "timed_out": timed_out}


supervisor = Supervisor()
