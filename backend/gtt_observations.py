"""The broker's last-seen state of each protective GTT, for honest display.

The position lifecycle (backend/lifecycle.py) is derived from persisted
state only and makes no broker call. What the database knows about a GTT is
the app's own record: the id it stored and the quantity it placed or last
resized it to. That record never proves protection. This module keeps what a
broker READ actually reported the last time any loop read the GTT book: its
status, its instrument and every leg, with the time it was seen. Written only
by reads that already happen (the reconciliation cycle, the GTT watcher, the
order monitor's protection check, the resize read-back, a cancellation's
read-back, the GTT page); never by a broker write, and never from a write's
acknowledgement — an accepted modify only invalidates what was seen.

Process memory on purpose (no schema change): after a restart nothing has
been observed, so every position shows UNVERIFIED until the next read of the
book. Nothing is ever shown PROTECTED from the app's record alone.
"""

import threading
from datetime import datetime, timezone

_lock = threading.Lock()
_seen: dict[str, dict] = {}

MISSING = "missing"   # an expected GTT absent from a successful, complete book read


def _now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


def _token(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        token = int(value)
    except (TypeError, ValueError):
        return None
    return token or None   # 0 / missing: the broker did not say


def _quantity(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != int(value):
        return None
    return int(value)


def _legs(gtt: dict) -> list[dict]:
    legs = []
    for leg in gtt.get("orders") or []:
        if not isinstance(leg, dict):
            legs.append({"transaction_type": None, "quantity": None, "exchange": None, "tradingsymbol": None})
            continue
        legs.append({
            "transaction_type": _text(leg.get("transaction_type")),
            "quantity": _quantity(leg.get("quantity")),
            "exchange": _text(leg.get("exchange")),
            "tradingsymbol": _text(leg.get("tradingsymbol")),
        })
    return legs


def observation(gtt: dict, at: str) -> dict:
    """What one GTT dict, as a broker lists it, says: the stored observation shape."""
    condition = gtt.get("condition") if isinstance(gtt.get("condition"), dict) else {}
    legs = _legs(gtt)
    quantities = [leg["quantity"] for leg in legs if leg["quantity"] is not None]
    return {
        "status": str(gtt.get("status") or "").strip().lower() or None,
        "exchange": _text(condition.get("exchange")),
        "tradingsymbol": _text(condition.get("tradingsymbol")),
        "instrument_token": _token(condition.get("instrument_token")),
        "legs": legs,
        # An OCO's legs cover the same shares (only one fires): the largest leg is what it can sell.
        "quantity": max(quantities) if quantities else None,
        "observed_at": at,
    }


def record_book(gtts, observed_at: str | None = None, *, expected=()) -> None:
    """Every GTT in a COMPLETE book just read from one broker (Kite or the
    paper broker). `expected` are GTT ids the caller looked for: one absent
    from the book is recorded as MISSING at this read."""
    at = observed_at or _now_sql()
    listed = set()
    with _lock:
        for g in gtts or []:
            if not isinstance(g, dict) or g.get("id") is None:
                continue
            listed.add(str(g["id"]))
            _seen[str(g["id"])] = observation(g, at)
        for gtt_id in expected or ():
            if gtt_id is None or str(gtt_id) in listed:
                continue
            _seen[str(gtt_id)] = {"status": MISSING, "exchange": None, "tradingsymbol": None,
                                  "instrument_token": None, "legs": [], "quantity": None, "observed_at": at}


def invalidate(gtt_id) -> None:
    """The GTT was just changed at the broker (an accepted modify): what was
    seen before no longer describes it. Until the next read it is unobserved."""
    if gtt_id is None:
        return
    with _lock:
        _seen.pop(str(gtt_id), None)


def get(gtt_id) -> dict | None:
    if gtt_id is None:
        return None
    with _lock:
        seen = _seen.get(str(gtt_id))
        return {**seen, "legs": [dict(leg) for leg in seen["legs"]]} if seen else None


def reset() -> None:
    with _lock:
        _seen.clear()
