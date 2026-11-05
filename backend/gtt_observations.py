"""The broker's last-seen state of each protective GTT, for honest display.

The position lifecycle (backend/lifecycle.py) is derived from persisted
state only and makes no broker call. What it knows about a GTT is the app's
own record: the id it stored and the quantity it placed or last resized it
to. This module adds what the broker actually reported the last time any
loop read the GTT book: its status and its quantity, with the time it was
seen. Written by reads that already happen (the reconciliation cycle, the
GTT watcher) and by a successful resize; never by a broker write of its own.

Process memory on purpose (no schema change): after a restart a position
shows "not yet observed" until the next cycle reads the GTT book, and its
protection is judged on the app's record meanwhile — which never claims more
protection than the observation would.
"""

import threading
from datetime import datetime, timezone

_lock = threading.Lock()
_seen: dict[str, dict] = {}


def _now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _quantity(gtt: dict) -> int:
    # An OCO's legs cover the same shares (only one fires): the largest leg is what it can sell.
    return max((int(leg.get("quantity") or 0) for leg in gtt.get("orders") or []), default=0)


def record_book(gtts, observed_at: str | None = None) -> None:
    """Every GTT in a book just read from a broker (Kite or the paper broker)."""
    at = observed_at or _now_sql()
    with _lock:
        for g in gtts or []:
            if not isinstance(g, dict) or g.get("id") is None:
                continue
            _seen[str(g["id"])] = {"status": str(g.get("status", "active")), "quantity": _quantity(g),
                                   "observed_at": at}


def record_resize(gtt_id, quantity: int) -> None:
    """A modify_gtt the broker accepted: the GTT now covers `quantity`."""
    with _lock:
        previous = _seen.get(str(gtt_id), {})
        _seen[str(gtt_id)] = {"status": previous.get("status", "active"), "quantity": int(quantity),
                              "observed_at": _now_sql()}


def get(gtt_id) -> dict | None:
    if gtt_id is None:
        return None
    with _lock:
        seen = _seen.get(str(gtt_id))
        return dict(seen) if seen else None


def reset() -> None:
    with _lock:
        _seen.clear()
