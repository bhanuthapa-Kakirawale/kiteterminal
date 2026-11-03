"""DMB integration profile: PRODUCTION (always, by default) or the isolated SYNTHETIC_TEST profile.

Synthetic Daily Market Byte bundles exist to test the handoff. In PRODUCTION they are refused
for any ticket handoff (and refused at import, so the account database never holds them): an
operator can never start a real ticket from invented evidence.

The SYNTHETIC_TEST profile is the only place a synthetic bundle is actionable. It is NOT a
setting: there is no environment variable, config value, database row, route or UI switch that
turns it on. Only a test harness running in the same process can call
`activate_synthetic_test_profile()` - `tests/demo_server.py --dmb-synthetic-profile` and the
test suite. `python -m backend.server` never does. Activation is refused, and an activated
profile stops counting as active, unless the process is verifiably isolated, re-checked on
every call:

- the database is not the real account database (`backend/kite_terminal.db` or any file next
  to it with that prefix - its backups);
- the broker factory is not the real one (`kite_client.get_kite` has been replaced by a
  FakeKite);
- LIVE is never possible: arming LIVE is refused (`live_arming.set_mode`) and, as a backstop,
  a live new entry is refused at confirmation (`account_safety.check_live_armed`).

Synthetic is decided by the packet's content, not by its `data_origin` label alone
(`dmb_inbox_contract.synthetic_markers`): the synthetic source, a synthetic artifact id or a
`SYNTHETIC` origin each make a packet synthetic. A packet that claims real DMB evidence is
accepted only when every source is in `PRODUCTION_PERMITTED_SOURCES`, which is empty until the
owner confirms private-handoff rights (DMB marks every real source UNRESOLVED): relabelling a
synthetic packet as DMB evidence therefore makes it refused, not actionable.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

PRODUCTION = "PRODUCTION"
SYNTHETIC_TEST = "SYNTHETIC_TEST"

# Real (non-synthetic) sources Kite accepts for a private handoff in PRODUCTION. EMPTY: no real
# source has a confirmed private-handoff right (DMB: UNRESOLVED). Adding a name here IS a
# rights decision and must follow the owner's written confirmation; no code path adds to it.
PRODUCTION_PERMITTED_SOURCES: frozenset = frozenset()

_REAL_DB_DIR = Path(__file__).resolve().parent
_REAL_DB_PREFIX = "kite_terminal.db"


class ProfileRefused(RuntimeError):
    """The synthetic test profile cannot be activated in this process."""


_lock = threading.Lock()
_state: dict = {"active": False, "test_sources": frozenset()}


def _is_real_database(path) -> bool:
    resolved = Path(os.path.abspath(os.fspath(path)))
    return (os.path.normcase(str(resolved.parent)) == os.path.normcase(str(_REAL_DB_DIR))
            and resolved.name.lower().startswith(_REAL_DB_PREFIX))


def isolation_problems() -> list[str]:
    """Why this process is NOT an isolated synthetic test environment ([] when it is)."""
    from backend import db, kite_client
    problems = []
    if _is_real_database(db.DB_PATH):
        problems.append("the database is the real account database")
    if kite_client.get_kite is kite_client.PRODUCTION_GET_KITE:
        problems.append("the real broker client is installed (no FakeKite)")
    return problems


def activate_synthetic_test_profile(test_sources=()) -> None:
    """Called only by a test harness (tests/), never by backend code. `test_sources` are
    invented source names a test bundle may declare as DMB evidence inside this profile only."""
    problems = isolation_problems()
    if problems:
        raise ProfileRefused("The synthetic test profile needs an isolated process: " + "; ".join(problems) + ".")
    with _lock:
        _state.update(active=True, test_sources=frozenset(test_sources))


def deactivate() -> None:
    with _lock:
        _state.update(active=False, test_sources=frozenset())


def synthetic_test_active() -> bool:
    """True only while activated AND the process is still isolated (re-checked on every call)."""
    with _lock:
        if not _state["active"]:
            return False
    return not isolation_problems()


def current() -> str:
    return SYNTHETIC_TEST if synthetic_test_active() else PRODUCTION


def permitted_real_sources() -> frozenset:
    """Source names a non-synthetic packet may rest on in the current profile."""
    if synthetic_test_active():
        with _lock:
            return PRODUCTION_PERMITTED_SOURCES | _state["test_sources"]
    return PRODUCTION_PERMITTED_SOURCES


def status() -> dict:
    active = synthetic_test_active()
    return {"profile": SYNTHETIC_TEST if active else PRODUCTION, "synthetic_handoff_allowed": active,
            "live_allowed": not active}


__all__ = ["PRODUCTION", "SYNTHETIC_TEST", "PRODUCTION_PERMITTED_SOURCES", "ProfileRefused", "isolation_problems",
           "activate_synthetic_test_profile", "deactivate", "synthetic_test_active", "current",
           "permitted_real_sources", "status"]
