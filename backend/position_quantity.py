"""PK-13: how many shares a position actually holds.

trade_tickets.quantity is ORDER INTENT — what was requested. A broker order
can fill partially, so every position-related operation uses these instead:

  filled_quantity   broker-confirmed cumulative fill of that ticket's order:
                    for an entry the shares bought, for an exit ticket the
                    shares that exit sold. 0 while a live order has filled
                    nothing; NULL = unknown (never submitted, or a historical
                    partial whose fill was never recorded — never guessed).
  open quantity     entry filled_quantity - the shares its executed exits
                    (explicit, Emergency, strategy, or a synthesized GTT
                    exit) have sold. What the app may sell, protect, risk,
                    reconcile and journal. NULL when any input is unknown.
  risk quantity     for the NEW-entry limits only: the requested quantity
                    while the entry order is still working (the rest can fill
                    at any moment), the open quantity once it has ended.
  protection qty    what the protective GTT should cover: the requested
                    quantity while the entry has filled nothing yet (the GTT
                    is placed with the order, before any fill), otherwise
                    the open quantity.

Every module takes these figures from here — the arithmetic lives nowhere
else. The one-entry / one-exit-at-a-time model is kept: at most one exit
owns a position while its order is in flight (execution.EXIT_IN_FLIGHT_SQL).

This module imports only execution at load time (risk_engine imports it);
the rest is imported where it is used.
"""

import logging
import sqlite3
import threading
from dataclasses import dataclass

from backend import execution

logger = logging.getLogger(__name__)


# ---- SQL fragments -----------------------------------------------------------
#
# `ref` is how the outer query names the POSITION row: the table name when it
# is not aliased, else its alias. The inner exit rows are aliased `x`, so an
# unaliased outer `trade_tickets.id` still resolves to the position.

def filled_sql(ref: str = "trade_tickets") -> str:
    """A ticket's filled quantity. A row written without one (from before
    PK-13, or inserted directly) follows the migration's backfill rule
    (db._ensure_filled_quantity_column): an executed ticket whose fill is
    proven — or predates fill tracking — filled its whole quantity, a live
    order still 'pending' has filled nothing; anything else is unknown."""
    return (
        f"COALESCE({ref}.filled_quantity, CASE WHEN {ref}.status = 'executed' THEN CASE "
        f"WHEN COALESCE({ref}.entry_fill_status, 'filled') = 'filled' THEN {ref}.quantity "
        f"WHEN {ref}.entry_fill_status = 'pending' THEN 0 END END)"
    )


def exits_filled_sql(ref: str = "trade_tickets") -> str:
    """Shares sold by the position's executed exits (EXITING ones count what
    they have filled so far). NULL if any of them has an unknown fill."""
    return (
        f"(SELECT CASE WHEN SUM({filled_sql('x')} IS NULL) > 0 THEN NULL "
        f"ELSE COALESCE(SUM({filled_sql('x')}), 0) END "
        f"FROM trade_tickets x WHERE x.closes_ticket_id = {ref}.id AND x.status = 'executed')"
    )


def open_quantity_sql(ref: str = "trade_tickets") -> str:
    return f"({filled_sql(ref)} - {exits_filled_sql(ref)})"


def risk_quantity_sql(ref: str = "trade_tickets") -> str:
    # A working (or vanished-from-the-book) entry can still fill up to its
    # requested size; an unknown fill counts as requested. Never below 0.
    return (
        f"MAX(0, CASE WHEN COALESCE({ref}.entry_fill_status, '') IN ('pending', 'unconfirmed') "
        f"THEN {ref}.quantity ELSE COALESCE({filled_sql(ref)}, {ref}.quantity) END "
        f"- COALESCE({exits_filled_sql(ref)}, 0))"
    )


def filled(row) -> int | None:
    """filled_sql for a row already read."""
    if row["filled_quantity"] is not None:
        return row["filled_quantity"]
    if row["status"] == "executed":
        if (row["entry_fill_status"] or "filled") == "filled":
            return row["quantity"]
        if row["entry_fill_status"] == "pending":
            return 0
    return None


# ---- reads --------------------------------------------------------------------

def open_quantity(conn: sqlite3.Connection, position_id: int) -> int | None:
    row = conn.execute(
        f"SELECT {open_quantity_sql()} AS q FROM trade_tickets WHERE id = ?", (position_id,)
    ).fetchone()
    return None if row is None else row["q"]


def exits_filled(conn: sqlite3.Connection, position_id: int) -> int | None:
    row = conn.execute(
        f"SELECT {exits_filled_sql()} AS q FROM trade_tickets WHERE id = ?", (position_id,)
    ).fetchone()
    return None if row is None else row["q"]


def entry_still_working(position) -> bool:
    return position["entry_fill_status"] in ("pending", "unconfirmed")


def protection_quantity(conn: sqlite3.Connection, position) -> int | None:
    if entry_still_working(position) and not filled(position):
        return position["quantity"]  # the GTT placed with the order, before any fill
    return open_quantity(conn, position["id"])


# ---- monotonic fill recording ------------------------------------------------

INCREASED, UNCHANGED, STALE, EXCEEDS_REQUESTED, INVALID = (
    "increased", "unchanged", "stale", "exceeds_requested", "invalid")


def record_fill(conn: sqlite3.Connection, ticket, broker_filled) -> str:
    """Records the broker's cumulative filled quantity for a ticket's order.
    Monotonic: a compare-and-set that only ever RAISES filled_quantity, so a
    stale lower figure (an older poll, a slower writer) never overwrites a
    newer larger one. A figure above the requested quantity is impossible
    for a correct broker report: it is never written (the caller flags it).
    The caller commits."""
    try:
        reported = int(broker_filled or 0)
    except (TypeError, ValueError):
        return INVALID
    if reported < 0:
        return INVALID
    if reported > ticket["quantity"]:
        return EXCEEDS_REQUESTED
    if conn.execute(
        "UPDATE trade_tickets SET filled_quantity = ? "
        "WHERE id = ? AND (filled_quantity IS NULL OR filled_quantity < ?)",
        (reported, ticket["id"], reported),
    ).rowcount == 1:
        return INCREASED
    current = conn.execute("SELECT filled_quantity FROM trade_tickets WHERE id = ?", (ticket["id"],)).fetchone()
    return UNCHANGED if current is not None and current["filled_quantity"] == reported else STALE


def flag(conn: sqlite3.Connection, ticket_id: int | None, check_type: str, expected: str, actual: str,
         detail: str) -> bool:
    """A de-duplicated finding: one reconciliation_log row (and one ticket
    event) per distinct unresolved `actual`. True if it is new."""
    from backend import reconciliation
    if not reconciliation.log_mismatch(conn, ticket_id, check_type, expected=expected, actual=actual, detail=detail):
        return False
    if ticket_id is not None:
        _log_event(conn, ticket_id, "modified", detail)
    return True


def _log_event(conn: sqlite3.Connection, ticket_id: int, event_type: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, ?, ?)", (ticket_id, event_type, detail)
    )


# ---- protective GTT quantity -------------------------------------------------

# Every protective-GTT modification of a position (fill resize, trailing
# ratchet, breakeven move) runs under that position's lock and re-reads the
# quantity inside it: whichever runs last uses the newest fill, so a stale
# quantity can never be written back over a resize. All of them run in this
# process (order monitor, GTT watcher, request threads).
_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def protection_lock(position_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(position_id, threading.Lock())


@dataclass
class SyncResult:
    status: str        # in_sync | resized | mismatch | no_position
    message: str = ""  # for the caller's alert / event text
    urgent: bool = False
    new_flag: bool = False  # a new (not already logged) mismatch finding


def gtt_leg_quantities(gtt: dict) -> list[int]:
    return [int(leg.get("quantity") or 0) for leg in gtt.get("orders") or []]


def _legs_text(quantities: list[int]) -> str:
    distinct = sorted(set(quantities))
    return " and ".join(str(q) for q in distinct) or "?"


def sync_protection(conn: sqlite3.Connection, kite, position_id: int, reason: str,
                    gtt_book: dict | None = None) -> SyncResult:
    """Makes the position's protective GTT cover exactly its protection
    quantity — never the unfilled part of an order, never shares already
    sold. Kite supports changing a GTT's quantity through modify_gtt; the
    GTT's own trigger(s), leg prices and last price are kept, only the
    quantity changes. Anything that prevents that — no GTT, a GTT no longer
    active, an unknown or impossible quantity, a failed modify — leaves the
    position OPEN and is an urgent PROTECTION QUANTITY MISMATCH (logged once
    per distinct state). The caller commits."""
    with protection_lock(position_id):
        position = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (position_id,)).fetchone()
        if position is None or position["closed_at"] is not None or position["mode"] != "live":
            return SyncResult("no_position")
        symbol, gtt_id = position["symbol"], position["kite_gtt_id"]
        desired = protection_quantity(conn, position)

        def mismatch(state: str, text: str) -> SyncResult:
            detail = (f"PROTECTION QUANTITY MISMATCH: {symbol} (ticket {position_id}) — {text} The position is kept "
                      f"OPEN. Check its stop-loss in Kite now.")
            new = flag(conn, position_id, "order_status", expected=f"protective GTT covering {desired} share(s)",
                       actual=state, detail=detail)
            return SyncResult("mismatch", detail, urgent=True, new_flag=new)

        if desired is None:
            return mismatch("open quantity unknown",
                            "its open quantity is unknown (an unrecorded partial fill), so its protection cannot be "
                            "sized.")
        if desired < 0:
            return mismatch(f"open quantity {desired}",
                            f"its exits sold more than it bought (open quantity {desired}) — NEEDS REVIEW.")
        if desired == 0:
            # Nothing left to protect. A flat position is closed (and its GTT
            # removed) by close_if_flat once its entry has ended; while the
            # entry order is still working the GTT is left for its next fill.
            return SyncResult("in_sync")
        if gtt_id is None:
            return mismatch("no protective GTT",
                            f"{desired} share(s) are open but it has NO protective GTT ({reason}).")

        if gtt_book is not None and gtt_id in gtt_book:
            gtt = gtt_book[gtt_id]
        else:
            try:
                gtt = next((g for g in kite.get_gtts() if g["id"] == gtt_id), None)
            except Exception as e:
                return mismatch("GTT book unreadable",
                                f"the GTT book could not be read to resize protective GTT {gtt_id} to {desired} "
                                f"({type(e).__name__}: {e}).")
        status = None if gtt is None else gtt.get("status", "active")
        if status != "active":
            return mismatch(f"GTT {gtt_id} {status or 'missing'}; open {desired}",
                            f"protective GTT {gtt_id} is {status or 'missing from Kite'}, so it cannot be resized to "
                            f"the {desired} share(s) now open ({reason}).")
        current = gtt_leg_quantities(gtt)
        if current and all(q == desired for q in current):
            return SyncResult("in_sync")

        condition = gtt.get("condition") or {}
        legs = [{
            "transaction_type": leg["transaction_type"], "quantity": desired,
            "order_type": leg.get("order_type", "LIMIT"), "product": leg.get("product", position["product_type"]),
            "price": leg["price"],
        } for leg in gtt.get("orders") or []]
        try:
            kite.modify_gtt(
                trigger_id=gtt_id,
                trigger_type=gtt.get("type") or (kite.GTT_TYPE_OCO if len(legs) > 1 else kite.GTT_TYPE_SINGLE),
                tradingsymbol=condition.get("tradingsymbol", symbol),
                exchange=condition.get("exchange", position["exchange"]),
                trigger_values=list(condition.get("trigger_values") or []),
                last_price=condition.get("last_price"),
                orders=legs,
            )
        except Exception as e:
            was = _legs_text(current)
            return mismatch(f"GTT {gtt_id} covers {was}; resize to {desired} failed",
                            f"protective GTT {gtt_id} covers {was} share(s) but {desired} are open, and resizing it "
                            f"FAILED ({type(e).__name__}: {e}) ({reason}).")
        was = _legs_text(current)
        message = f"Protective GTT {gtt_id} resized from {was} to {desired} share(s) ({reason})."
        _log_event(conn, position_id, "modified", message)
        return SyncResult("resized", message)


# ---- closing a flat position -------------------------------------------------

def entry_ended(position) -> bool:
    """The entry order can fill no further: it completed, or ended after a
    partial fill (or predates fill tracking)."""
    return position["entry_fill_status"] in (None, "filled", "partial")


@dataclass
class CloseResult:
    closed: bool
    message: str = ""
    urgent: bool = False


def close_if_flat(conn: sqlite3.Connection, kite, position_id: int, closing_exit_id: int,
                  fill_time: str, remove_gtt: bool = True) -> CloseResult:
    """Closes a position once it holds nothing — its entry order has ended
    and its exits have sold every share it bought — at `fill_time` (the
    broker fill of closing_exit_id, the exit that sold the last share):
    closed_at, supersede sibling exits, ONE journal row, then removal of the
    protective GTT (not when the GTT itself sold the shares: remove_gtt=False).
    A position that still holds shares stays open. More sold than bought is
    never closed: NEEDS REVIEW. Commits."""
    from backend import journal
    position = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (position_id,)).fetchone()
    if position is None:
        return CloseResult(False)
    if position["closed_at"] is not None:
        return CloseResult(False, f" Position #{position_id} was already marked closed.")
    remaining = open_quantity(conn, position_id)
    if remaining is None:
        detail = (f"{position['symbol']}: position #{position_id}'s open quantity is unknown (an unrecorded partial "
                  f"fill) — it is kept OPEN; NEEDS REVIEW in Kite.")
        flag(conn, position_id, "position_state", expected="a known open quantity", actual="open quantity unknown",
             detail=detail)
        conn.commit()
        return CloseResult(False, f" {detail}", urgent=True)
    if remaining < 0:
        detail = (f"POSITION QUANTITY NEEDS REVIEW: {position['symbol']} — position #{position_id} bought "
                  f"{filled(position)} but its exits sold {exits_filled(conn, position_id)} "
                  f"(open {remaining}). It is NOT closed and nothing is sent; check Kite's orders and holdings.")
        flag(conn, position_id, "position_state", expected="exits sell at most what the entry bought",
             actual=f"open quantity {remaining}", detail=detail)
        conn.commit()
        return CloseResult(False, f" {detail}", urgent=True)
    if remaining > 0 or not entry_ended(position):
        return CloseResult(False)

    closed = journal.close_position_for_exit(conn, closing_exit_id, position_id, fill_time)
    note, urgent = remove_protective_gtt(conn, kite, closing_exit_id, position_id) if remove_gtt else ("", False)
    conn.commit()
    return CloseResult(closed, note, urgent)


def remove_protective_gtt(conn: sqlite3.Connection, kite, exit_ticket_id: int | None,
                          position_id: int) -> tuple[str, bool]:
    """After the position is flat its GTT protects nothing: remove it.
    Returns (note, urgent). A failure never reopens the position — the
    shares are sold — but an orphaned GTT could fire against nothing, so it
    is an urgent warning. A GTT that has already triggered may have sold a
    second time: flagged, nothing deleted, nothing sent. (Moved here from
    order_monitor by PK-13; behaviour unchanged.)"""
    position = conn.execute("SELECT symbol, kite_gtt_id FROM trade_tickets WHERE id = ?", (position_id,)).fetchone()
    if position is None or position["kite_gtt_id"] is None:
        return "", False
    exit_ticket = (conn.execute("SELECT kite_order_id, rule_checklist FROM trade_tickets WHERE id = ?",
                                (exit_ticket_id,)).fetchone() if exit_ticket_id is not None else None)
    gtt_id, symbol = position["kite_gtt_id"], position["symbol"]
    order_id = exit_ticket["kite_order_id"] if exit_ticket is not None else None
    try:
        status = next((g.get("status", "active") for g in kite.get_gtts() if g["id"] == gtt_id), "missing")
    except Exception:
        status = None  # cannot tell: still try to remove it

    if status == "triggered":
        from backend import reconciliation
        detail = (f"POSSIBLE DOUBLE SELL: {symbol} — exit order {order_id} filled, but protective GTT {gtt_id} has "
                  f"ALSO triggered at Kite and may have sold again. Nothing was deleted or sent. Check Kite's orders "
                  f"and holdings now.")
        reconciliation.log_mismatch(conn, position_id, "order_status",
                                    expected="protective GTT removed after the exit filled",
                                    actual=f"GTT {gtt_id} triggered alongside exit order {order_id}", detail=detail)
        _log_event(conn, position_id, "modified", detail)
        return f" {detail}", True
    if status not in ("active", None):
        return f" Protective GTT {gtt_id} was already {status} at Kite.", False
    try:
        kite.delete_gtt(gtt_id)
    except Exception as e:
        from backend import reconciliation
        detail = (f"ORPHANED GTT: {symbol} — exit order {order_id} filled and the position is closed, but removing "
                  f"its protective GTT {gtt_id} FAILED ({type(e).__name__}: {e}). It could still fire against shares "
                  f"that are gone — delete it in Kite NOW.")
        reconciliation.log_mismatch(conn, position_id, "order_status", expected=f"GTT {gtt_id} removed",
                                    actual=f"GTT {gtt_id} delete failed after exit fill", detail=detail)
        _log_event(conn, position_id, "modified", detail)
        return f" {detail}", True
    conn.execute("UPDATE trade_tickets SET kite_gtt_id = NULL WHERE id = ?", (position_id,))
    _log_event(conn, position_id, "modified", f"Protective GTT {gtt_id} removed after exit order {order_id} filled.")
    return f" Protective GTT {gtt_id} removed.", False


def describe(position, open_qty: int | None) -> str:
    """'Requested 100 · Filled 40 · Open 40' for messages."""
    bought = filled(position)
    return (f"requested {position['quantity']}, filled {'unknown' if bought is None else bought}, "
            f"open {'unknown' if open_qty is None else open_qty}")
