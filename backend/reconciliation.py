import asyncio
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from backend import config, execution, instruments, kite_client, market, position_quantity, risk_engine
from backend.accounts import get_default_account
from backend.db import get_connection

logger = logging.getLogger(__name__)

# ---- PK-16: structured findings ----------------------------------------------
#
# check_type stays the broad side ('order_status' / 'position_state');
# issue_code names the specific finding. Findings written before PK-16, and
# those of other modules, carry no issue_code.

GTT_MISSING = "GTT_MISSING"
GTT_QUANTITY_TOO_HIGH = "GTT_QUANTITY_TOO_HIGH"
GTT_QUANTITY_TOO_LOW = "GTT_QUANTITY_TOO_LOW"
GTT_PRICE_MISMATCH = "GTT_PRICE_MISMATCH"
ORPHAN_GTT = "ORPHAN_GTT"
GTT_ON_CLOSED_POSITION = "GTT_ON_CLOSED_POSITION"
MULTIPLE_ACTIVE_GTTS = "MULTIPLE_ACTIVE_GTTS"
UNLINKED_POSSIBLE_GTT = "UNLINKED_POSSIBLE_GTT"
AMBIGUOUS_GTT_OWNERSHIP = "AMBIGUOUS_GTT_OWNERSHIP"
BROKER_POSITION_SHORTFALL = "BROKER_POSITION_SHORTFALL"

# PK-19: codes for findings other modules record against a position, so the
# position lifecycle (backend/lifecycle.py) can read them as data instead of
# interpreting their text. Recording a code changes nothing about how a
# finding is de-duplicated or reviewed.
PROTECTION_MISSING = "PROTECTION_MISSING"                      # no active protective GTT
PROTECTION_QUANTITY_MISMATCH = "PROTECTION_QUANTITY_MISMATCH"  # GTT could not be resized to the shares held
POSITION_QUANTITY_REVIEW = "POSITION_QUANTITY_REVIEW"          # open quantity unknown, or negative
POSSIBLE_DOUBLE_SELL = "POSSIBLE_DOUBLE_SELL"                  # exit filled AND the GTT triggered
ORPHANED_GTT_AFTER_EXIT = "ORPHANED_GTT_AFTER_EXIT"            # position closed, its GTT delete failed

# Kite -> app findings. A reviewed one stays silent while the broker GTT
# (quantity, trigger, status) and the app positions it is judged against
# are unchanged: an orphan that is really the user's own GTT for a long-term
# holding is acknowledged once, not every cycle. Any change is a new finding.
REVIEW_SUPPRESSED_CODES = frozenset({
    ORPHAN_GTT, GTT_ON_CLOSED_POSITION, MULTIPLE_ACTIVE_GTTS, UNLINKED_POSSIBLE_GTT, AMBIGUOUS_GTT_OWNERSHIP,
})

URGENT = "urgent"
CHECK_KITE, CANCEL_IN_KITE, REVIEW_POSITION = "CHECK KITE", "CANCEL IN KITE", "REVIEW POSITION"

# What the last cycle could see (GET /api/reconciliation/status). In memory
# only: a fresh process reports 'not_run' until its first cycle.
last_cycle: dict = {}

# GTT_PRICE_MISMATCH needs the same difference on two consecutive cycles:
# the trailing ratchet moves the broker trigger a moment before it commits
# current_stop_loss, and one cycle may land in between.
_price_seen: dict[int, tuple] = {}

# Kite -> app findings need the same fingerprint on two consecutive cycles
# that could read the GTT book. The app's own writes are not atomic with the
# broker: an entry's GTT exists a moment before its kite_gtt_id is stored,
# and a closed position's closed_at is committed a moment before its GTT is
# deleted (journal.close_position_for_exit, then remove_protective_gtt). A
# cycle landing in such a gap must not raise an urgent false alarm.
_reverse_seen: set[tuple] = set()


def reset_cycle_memory() -> None:
    """Forgets every cross-cycle sighting and the last status (tests)."""
    _price_seen.clear()
    _reverse_seen.clear()
    last_cycle.clear()


async def run_reconciliation_loop() -> None:
    """Same infinite-loop-+-sleep shape as trailing_engine.run_trailing_loop
    (Phase 3). Only checks the trading account (Part B.4 — investment
    accounts have no tickets to reconcile against)."""
    while True:
        try:
            await asyncio.to_thread(_run_cycle)
        except Exception:
            logger.exception("Reconciliation cycle failed")
        await asyncio.sleep(config.RECONCILIATION_INTERVAL_SECONDS)


def cycle_status() -> dict:
    return {"checked_at": None, "gtt_book": "not_run", "gtt_error": None, "holdings": "not_run",
            "holdings_error": None, "active_gtts": None, "new_findings": 0, **last_cycle}


# ---- one broker snapshot per cycle -------------------------------------------

@dataclass
class BrokerSnapshot:
    """Every broker read a cycle needs, made once. None = that read failed
    (its error kept): checks that need it are skipped for the cycle, never
    run against an empty stand-in — an unreadable GTT book is not proof
    that every GTT is missing, nor that none exists."""
    gtts: list | None = None
    gtt_error: str | None = None
    held: dict[str, int] | None = None
    held_error: str | None = None
    held_loaded: bool = False

    def load_gtts(self, kite) -> None:
        try:
            self.gtts = list(kite.get_gtts())
        except Exception as e:
            logger.exception("Could not fetch get_gtts()")
            self.gtts, self.gtt_error = None, f"{type(e).__name__}: {e}"

    def load_held(self, kite) -> None:
        """Shares the broker shows per symbol. Unsettled (T+1) delivery
        quantity sits in t1_quantity, not quantity — ignoring it flagged
        every position the day after it was bought."""
        self.held_loaded = True
        try:
            holdings = kite.holdings()
            positions = kite.positions().get("net", [])
        except Exception as e:
            logger.exception("Could not fetch holdings()/positions() for position-state check")
            self.held, self.held_error = None, f"{type(e).__name__}: {e}"
            return
        held: dict[str, int] = {}
        for h in holdings:
            qty = (h.get("quantity") or 0) + (h.get("t1_quantity") or 0)
            held[h["tradingsymbol"]] = held.get(h["tradingsymbol"], 0) + qty
        for p in positions:
            held[p["tradingsymbol"]] = held.get(p["tradingsymbol"], 0) + (p.get("quantity") or 0)
        self.held = held


def _run_cycle() -> None:
    conn = get_connection()
    try:
        account = get_default_account(conn)
        if account["account_type"] != "trading":
            return
        try:
            kite = kite_client.get_kite()
        except kite_client.NoActiveSessionError:
            last_cycle.update(checked_at=_now(), gtt_book="no_session", holdings="no_session")
            return

        before = _row_count(conn)
        snapshot = BrokerSnapshot()
        snapshot.load_gtts(kite)
        _check_order_status(conn, kite, account)
        if snapshot.gtts is not None:
            _check_gtt_state(conn, kite, account, snapshot)
            _check_broker_gtts(conn, account, snapshot)
        _check_position_state(conn, kite, account, snapshot)
        conn.commit()
        last_cycle.update(
            checked_at=_now(),
            gtt_book="ok" if snapshot.gtts is not None else "unavailable", gtt_error=snapshot.gtt_error,
            holdings=("unavailable" if snapshot.held_error else "ok") if snapshot.held_loaded else "not_needed",
            holdings_error=snapshot.held_error,
            active_gtts=None if snapshot.gtts is None else len(_active(snapshot.gtts)),
            new_findings=_row_count(conn) - before,
        )
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM reconciliation_log").fetchone()["n"]


# ---- recording findings -------------------------------------------------------

def _already_logged(conn: sqlite3.Connection, ticket_id: int | None, check_type: str, actual: str) -> bool:
    # Don't re-log the identical unresolved finding every cycle — only a
    # NEW/changed actual value (or a still-different one) gets a fresh row.
    # This keeps the log meaningful without needing the user to dismiss the
    # same finding repeatedly while it hasn't changed.
    row = conn.execute(
        """
        SELECT 1 FROM reconciliation_log
        WHERE (ticket_id IS ? OR (ticket_id IS NULL AND ? IS NULL))
          AND check_type = ? AND actual = ? AND resolved = 0
        LIMIT 1
        """,
        (ticket_id, ticket_id, check_type, actual),
    ).fetchone()
    return row is not None


def _already_seen(conn: sqlite3.Connection, issue_code: str, gtt_id: int | None, actual: str) -> bool:
    # Reviewed rows count too (REVIEW_SUPPRESSED_CODES). `actual` is the
    # finding's fingerprint, so any material change still gets a new row.
    row = conn.execute(
        "SELECT 1 FROM reconciliation_log WHERE issue_code = ? AND gtt_id IS ? AND actual = ? LIMIT 1",
        (issue_code, gtt_id, actual),
    ).fetchone()
    return row is not None


def log_mismatch(conn: sqlite3.Connection, ticket_id: int | None, check_type: str, expected: str, actual: str,
                 detail: str, *, issue_code: str | None = None, severity: str | None = None,
                 symbol: str | None = None, gtt_id: int | None = None, expected_quantity: int | None = None,
                 broker_quantity: int | None = None, position_state: str | None = None,
                 suggested_action: str | None = None) -> bool:
    """True if a new row was written; False if the same finding was already
    logged (callers alert only on True). The keyword-only fields are PK-16's
    structured view of the finding; callers that omit them are unchanged."""
    if issue_code in REVIEW_SUPPRESSED_CODES:
        if _already_seen(conn, issue_code, gtt_id, actual):
            return False
    elif _already_logged(conn, ticket_id, check_type, actual):
        return False
    conn.execute(
        """
        INSERT INTO reconciliation_log (ticket_id, check_type, expected, actual, mismatch, detail, issue_code,
                                        severity, symbol, gtt_id, expected_quantity, broker_quantity,
                                        position_state, suggested_action)
        VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ticket_id, check_type, expected, actual, detail, issue_code, severity, symbol, gtt_id, expected_quantity,
         broker_quantity, position_state, suggested_action),
    )
    logger.warning("Reconciliation mismatch (%s%s): expected=%r actual=%r — %s", check_type,
                   f"/{issue_code}" if issue_code else "", expected, actual, detail)
    return True


# ---- order status (unchanged) -------------------------------------------------

def _check_order_status(conn: sqlite3.Connection, kite, account) -> None:
    # Only today's orders can be looked up (the broker's order book covers
    # the current session — older ids just error every cycle), and orders
    # still awaiting a fill are followed by order_monitor.py, not flagged.
    # PK-13: a 'partial' ticket (its order ended after a partial fill) is
    # checked too — against the quantity the app recorded as filled.
    tickets = conn.execute(
        "SELECT * FROM trade_tickets "
        "WHERE account_id = ? AND status = 'executed' AND kite_order_id IS NOT NULL "
        "AND executed_at >= ? AND COALESCE(entry_fill_status, 'filled') IN ('filled', 'partial')",
        (account["id"], risk_engine._same_day_ist_cutoff_utc()),
    ).fetchall()
    for t in tickets:
        try:
            history = kite.order_history(t["kite_order_id"])
        except Exception:
            logger.exception("Could not fetch order_history for order %s", t["kite_order_id"])
            continue
        if not history:
            continue
        latest = history[-1]
        status = latest.get("status")
        filled_qty = latest.get("filled_quantity")
        recorded = position_quantity.filled(t)

        if t["entry_fill_status"] == "partial":
            if filled_qty is not None and filled_qty != recorded:
                log_mismatch(
                    conn, t["id"], "order_status",
                    expected=f"{status}, filled_quantity={recorded} (as recorded)",
                    actual=f"{status}, filled_quantity={filled_qty}",
                    detail=f"{t['symbol']}: TICKET FILLED QUANTITY MISMATCH — order {t['kite_order_id']} reports "
                           f"{filled_qty} filled but the app recorded {recorded}. Check the order in Kite.",
                )
        elif status != "COMPLETE" or (filled_qty is not None and filled_qty != t["quantity"]):
            log_mismatch(
                conn, t["id"], "order_status",
                expected=f"COMPLETE, filled_quantity={t['quantity']}",
                actual=f"{status}, filled_quantity={filled_qty}",
                detail=f"{t['symbol']}: order {t['kite_order_id']} does not match expected fill state",
            )


# ---- GTT helpers ----------------------------------------------------------------

def _active(gtts: list) -> list:
    # get_gtts() also lists triggered/cancelled/expired/deleted GTTs — only
    # 'active' ones are still protecting (or threatening) anything.
    return [g for g in gtts if g.get("status", "active") == "active"]


def _gtt_key(gtt: dict) -> tuple[str, str]:
    condition = gtt.get("condition") or {}
    return condition.get("exchange") or "NSE", condition.get("tradingsymbol") or "?"


def _gtt_quantity(gtt: dict) -> int:
    # One GTT, whatever its legs: an OCO's stop and target legs cover the
    # SAME shares (only one can fire), so it is never counted twice.
    return max(position_quantity.gtt_leg_quantities(gtt), default=0)


def _gtt_sides(gtt: dict) -> set[str]:
    return {leg.get("transaction_type") for leg in gtt.get("orders") or []}


def _num(value) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def _gtt_text(gtt: dict) -> str:
    """'single GTT 9001 (SELL 15, trigger 1400, limit 1393)' for messages."""
    triggers = ", ".join(_num(v) for v in (gtt.get("condition") or {}).get("trigger_values") or []) or "?"
    limits = ", ".join(_num(leg.get("price")) for leg in gtt.get("orders") or []) or "?"
    side = "/".join(sorted(s for s in _gtt_sides(gtt) if s)) or "?"
    return (f"{gtt.get('type') or '?'} GTT {gtt['id']} ({side} {_gtt_quantity(gtt)}, trigger {triggers}, "
            f"limit {limits})")


def _gtt_fingerprint(gtt: dict) -> str:
    """Everything about a broker GTT whose change makes a finding new."""
    condition = gtt.get("condition") or {}
    legs = ",".join(f"{leg.get('transaction_type')}:{int(leg.get('quantity') or 0)}@{_num(leg.get('price'))}"
                    for leg in gtt.get("orders") or [])
    triggers = ",".join(_num(v) for v in condition.get("trigger_values") or [])
    return f"GTT {gtt['id']} {gtt.get('type')} {condition.get('tradingsymbol')} [{legs}] trigger [{triggers}]"


def _position_state(conn: sqlite3.Connection, row) -> str:
    if row["closed_at"] is not None:
        return "closed"
    if row["status"] != "executed":
        return f"entry {row['status']}"
    if execution.is_closing_sell(row):
        return "exit ticket"
    in_flight = conn.execute(
        f"SELECT 1 FROM trade_tickets WHERE closes_ticket_id = ? AND {execution.EXIT_IN_FLIGHT_SQL} LIMIT 1",
        (row["id"],),
    ).fetchone()
    if in_flight is not None:
        return "EXITING"  # PK-8: the original GTT stays until the exit fills
    if position_quantity.entry_still_working(row):
        return "entry pending"
    return "open"


# ---- app -> Kite: each position's own GTT --------------------------------------

def _check_gtt_state(conn: sqlite3.Connection, kite, account, snapshot: BrokerSnapshot) -> None:
    # Deliberately duplicates part of what trailing_engine.py's GTT watcher
    # (every live protected position, trailing or not, since PK-6) already
    # detects on its own tick: a second, independent check catching anything
    # that loop's own cycle might have missed, not wasted work. Detection
    # only — the watcher owns resizing. Logged under check_type='order_status'
    # since a GTT is fundamentally an order construct. Dry-run tickets hold
    # no broker GTT and are never checked.
    tickets = conn.execute(
        "SELECT * FROM trade_tickets "
        "WHERE account_id = ? AND status = 'executed' AND mode = 'live' AND kite_gtt_id IS NOT NULL "
        "AND closed_at IS NULL",
        (account["id"],),
    ).fetchall()
    by_id = {g["id"]: g for g in snapshot.gtts}
    checked = set()
    for t in tickets:
        gtt = by_id.get(t["kite_gtt_id"])
        status = None if gtt is None else gtt.get("status", "active")
        if status != "active":
            # A GTT absent from the list and one listed as e.g. 'triggered'
            # are different findings, so they are logged (and de-duplicated)
            # separately.
            log_mismatch(
                conn, t["id"], "order_status",
                expected=f"GTT {t['kite_gtt_id']} active",
                actual=f"GTT {status or 'missing'} in kite.get_gtts()",
                detail=f"{t['symbol']}: protective GTT {t['kite_gtt_id']} is {status or 'missing'} at Kite. A fired or "
                       f"missing GTT does not prove the shares were sold: the position stays open until the GTT "
                       f"watcher finds a broker fill for it. Check Kite's orders and holdings.",
                issue_code=GTT_MISSING, severity=URGENT, symbol=t["symbol"], gtt_id=t["kite_gtt_id"],
                expected_quantity=position_quantity.protection_quantity(conn, t), broker_quantity=0,
                position_state=_position_state(conn, t), suggested_action=CHECK_KITE,
            )
            continue
        _check_gtt_quantity(conn, t, gtt)
        _check_gtt_price(conn, t, gtt)
        checked.add(t["id"])
    for ticket_id in list(_price_seen):
        if ticket_id not in checked:
            _price_seen.pop(ticket_id, None)


def _check_gtt_quantity(conn: sqlite3.Connection, ticket: sqlite3.Row, gtt: dict) -> None:
    """PK-13: an active protective GTT must cover exactly the shares held —
    an oversized SELL GTT is rejected by Kite for CNC (effectively no
    protection), an undersized one leaves shares unprotected."""
    desired = position_quantity.protection_quantity(conn, ticket)
    legs = position_quantity.gtt_leg_quantities(gtt)
    if desired is None or not legs or all(q == desired for q in legs):
        return
    covered = max(legs)
    difference = abs(covered - desired)
    if covered > desired:
        kind, code = "OVERSIZED", GTT_QUANTITY_TOO_HIGH
        effect = (f"it would try to sell {difference} more share(s) than the position holds — Kite rejects an "
                  f"oversized CNC SELL, so it may not protect anything, or it may sell shares held outside the app.")
    else:
        kind, code = "UNDERSIZED", GTT_QUANTITY_TOO_LOW
        effect = f"{difference} share(s) UNPROTECTED."
    log_mismatch(
        conn, ticket["id"], "order_status",
        expected=f"GTT {ticket['kite_gtt_id']} covers {desired}",
        actual=f"GTT {ticket['kite_gtt_id']} covers {covered}",
        detail=f"{ticket['symbol']}: PROTECTION QUANTITY MISMATCH — protective GTT {ticket['kite_gtt_id']} is "
               f"{kind}: it covers {covered} share(s) but the position holds {desired}; {effect} The GTT watcher "
               f"resizes it; check it in Kite if this persists.",
        issue_code=code, severity=URGENT, symbol=ticket["symbol"], gtt_id=ticket["kite_gtt_id"],
        expected_quantity=desired, broker_quantity=covered, position_state=_position_state(conn, ticket),
        suggested_action=CHECK_KITE,
    )


def _check_gtt_price(conn: sqlite3.Connection, ticket: sqlite3.Row, gtt: dict) -> None:
    """The stop trigger at Kite against the app's current stop. Only where
    the GTT has the structure the app placed — a trailing position's single
    trigger, a fixed position's OCO (stop = the lower trigger of a long) —
    anything else is not compared rather than guessed at."""
    triggers = (gtt.get("condition") or {}).get("trigger_values") or []
    if ticket["trailing_enabled"]:
        broker_stop = triggers[0] if gtt.get("type") == "single" and len(triggers) == 1 else None
    elif gtt.get("type") == "two-leg" and len(triggers) == 2:
        broker_stop = min(triggers) if ticket["transaction_type"] == "BUY" else max(triggers)
    else:
        broker_stop = None
    basis = ticket["current_stop_loss"] or ticket["stop_loss"]
    if broker_stop is None or basis is None:
        _price_seen.pop(ticket["id"], None)
        return
    tick = instruments.get_tick_size(ticket["symbol"], ticket["exchange"])
    expected = market.round_to_tick(basis, tick)
    if abs(float(broker_stop) - expected) <= tick / 2 + 1e-9:
        _price_seen.pop(ticket["id"], None)
        return
    sighting = (float(broker_stop), expected)
    if _price_seen.get(ticket["id"]) != sighting:
        _price_seen[ticket["id"]] = sighting  # first sighting: confirm on the next cycle
        return
    log_mismatch(
        conn, ticket["id"], "order_status",
        expected=f"GTT {gtt['id']} stop trigger {_num(expected)}",
        actual=f"GTT {gtt['id']} stop trigger {_num(broker_stop)}",
        detail=f"{ticket['symbol']}: GTT PRICE MISMATCH — protective GTT {gtt['id']} triggers at "
               f"{_num(broker_stop)} but the app's stop is {_num(expected)} (changed in Kite, or a stop update "
               f"that did not reach the broker). Check the stop in Kite.",
        issue_code=GTT_PRICE_MISMATCH, severity=URGENT, symbol=ticket["symbol"], gtt_id=gtt["id"],
        expected_quantity=position_quantity.protection_quantity(conn, ticket), broker_quantity=_gtt_quantity(gtt),
        position_state=_position_state(conn, ticket), suggested_action=CHECK_KITE,
    )


# ---- Kite -> app: every active broker GTT --------------------------------------
#
# Ownership is the exact kite_gtt_id link and nothing else. Symbol, quantity
# or trigger similarity is shown as a diagnostic, never used to assign a GTT
# to a position, and reconciliation never writes kite_gtt_id.

def _owning(row, legacy_closed: set[int]) -> bool:
    """A live app position that may own a protective GTT: an executed
    entry not yet closed — its entry still working, open, partly sold, or
    EXITING (PK-8 keeps the original GTT while an exit is in flight)."""
    return (row["status"] == "executed" and row["closed_at"] is None and not execution.is_closing_sell(row)
            and row["closes_ticket_id"] is None and row["id"] not in legacy_closed)


def _check_broker_gtts(conn: sqlite3.Connection, account, snapshot: BrokerSnapshot) -> None:
    # Account scope: the GTT book is this trading account's own session's.
    # Kite also reports each GTT's user_id: one that belongs to another
    # user is never judged against this account's tickets.
    session = kite_client.get_active_session(account["id"])
    session_user = session["user_id"] if session is not None else None
    active = [g for g in _active(snapshot.gtts)
              if not (session_user and g.get("user_id") and g.get("user_id") != session_user)]
    seen_now: set[tuple] = set()

    def report(ticket_id: int | None, check_type: str, **finding) -> None:
        key = (finding["issue_code"], finding["gtt_id"], finding["actual"])
        seen_now.add(key)
        if key in _reverse_seen:  # the same finding on the previous cycle too
            log_mismatch(conn, ticket_id, check_type, **finding)

    try:
        _classify_broker_gtts(conn, account, active, report)
    finally:
        _reverse_seen.clear()
        _reverse_seen.update(seen_now)


def _classify_broker_gtts(conn: sqlite3.Connection, account, active: list, report) -> None:
    if not active:
        return

    legacy_closed = {r["closes_ticket_id"] for r in conn.execute(risk_engine._CLOSED_BY_EXECUTED_EXIT)}
    rows = conn.execute(
        "SELECT * FROM trade_tickets WHERE account_id = ? AND COALESCE(mode, 'live') = 'live' "
        "AND (kite_gtt_id IS NOT NULL OR (status = 'executed' AND closed_at IS NULL)) ORDER BY id",
        (account["id"],),
    ).fetchall()
    refs: dict[int, list] = defaultdict(list)
    positions_by_key: dict[tuple, list] = defaultdict(list)
    for r in rows:
        if r["kite_gtt_id"] is not None:
            refs[r["kite_gtt_id"]].append(r)
        if _owning(r, legacy_closed):
            positions_by_key[(r["exchange"], r["symbol"])].append(r)

    active_ids = {g["id"] for g in active}
    unreferenced = defaultdict(list)
    for g in active:
        if not refs.get(g["id"]) and "SELL" in _gtt_sides(g):
            unreferenced[_gtt_key(g)].append(g)

    for g in active:
        owners = [r for r in refs.get(g["id"], []) if _owning(r, legacy_closed)]
        if len(owners) == 1:
            continue  # HEALTHY here; its quantity and price are the forward check's
        if owners:
            _flag_duplicate_link(report, g, owners)
        elif refs.get(g["id"]):
            _flag_closed_position(conn, report, g, refs[g["id"]])
        elif "SELL" in _gtt_sides(g):
            # A BUY GTT the app never placed cannot sell held shares (the
            # user's own buy order) — not protection, not judged here.
            _flag_unreferenced(conn, report, g, positions_by_key.get(_gtt_key(g), []), active_ids,
                               len(unreferenced[_gtt_key(g)]))


def _flag_duplicate_link(report, gtt: dict, owners: list) -> None:
    ids = ", ".join(f"#{r['id']}" for r in owners)
    report(
        None, "order_status",
        expected=f"GTT {gtt['id']} linked to exactly one open position",
        actual=f"{_gtt_fingerprint(gtt)}; linked to open positions {ids}",
        detail=f"{_symbol(gtt)}: AMBIGUOUS GTT OWNERSHIP — {_gtt_text(gtt)} is recorded as the protection of "
               f"{len(owners)} open positions ({ids}). One GTT cannot protect them all; nothing was changed. Review "
               f"the positions and their stops in Kite.",
        issue_code=AMBIGUOUS_GTT_OWNERSHIP, severity=URGENT, symbol=_symbol(gtt), gtt_id=gtt["id"],
        expected_quantity=None, broker_quantity=_gtt_quantity(gtt), position_state="open",
        suggested_action=REVIEW_POSITION,
    )


def _flag_closed_position(conn: sqlite3.Connection, report, gtt: dict, linked: list) -> None:
    latest = linked[-1]
    state = _position_state(conn, latest)
    ids = ", ".join(f"#{r['id']} ({_position_state(conn, r)})" for r in linked)
    report(
        latest["id"], "order_status",
        expected=f"GTT {gtt['id']} removed once its position was no longer open",
        actual=f"{_gtt_fingerprint(gtt)}; linked only to {ids}",
        detail=f"{_symbol(gtt)}: GTT ON CLOSED POSITION — {_gtt_text(gtt)} is still ACTIVE at Kite but the app "
               f"position it protected is no longer open (ticket {ids}). It can fire later and SELL shares bought for "
               f"another purpose. Cancel it in Kite (or Orders & Trades → GTTs) unless you meant to keep it. Nothing "
               f"was cancelled automatically.",
        issue_code=GTT_ON_CLOSED_POSITION, severity=URGENT, symbol=_symbol(gtt), gtt_id=gtt["id"],
        expected_quantity=0, broker_quantity=_gtt_quantity(gtt), position_state=state,
        suggested_action=CANCEL_IN_KITE,
    )


def _flag_unreferenced(conn: sqlite3.Connection, report, gtt: dict, positions: list, active_ids: set[int],
                       unreferenced_on_symbol: int) -> None:
    symbol, fingerprint = _symbol(gtt), _gtt_fingerprint(gtt)
    common = dict(severity=URGENT, symbol=symbol, gtt_id=gtt["id"], broker_quantity=_gtt_quantity(gtt))
    if not positions:
        report(
            None, "order_status",
            expected="every active SELL GTT belongs to an open app position",
            actual=f"{fingerprint}; no open app position in {symbol}",
            detail=f"{symbol}: ORPHAN GTT — {_gtt_text(gtt)} is ACTIVE at Kite but belongs to no open position in "
                   f"this app. It can SELL shares held for another purpose. If it is not a GTT you placed yourself, "
                   f"cancel it in Kite (or Orders & Trades → GTTs). Nothing was cancelled automatically.",
            issue_code=ORPHAN_GTT, expected_quantity=0, position_state="none", suggested_action=CANCEL_IN_KITE,
            **common,
        )
        return

    unprotected = [p for p in positions if p["kite_gtt_id"] not in active_ids]
    candidates = ", ".join(f"#{p['id']}" for p in positions)
    if not unprotected:
        protected_by = ", ".join(f"#{p['id']} by GTT {p['kite_gtt_id']}" for p in positions)
        report(
            None, "order_status",
            expected=f"one protective GTT per open {symbol} position",
            actual=f"{fingerprint}; positions {protected_by}",
            detail=f"{symbol}: MULTIPLE ACTIVE GTTs — {_gtt_text(gtt)} is ACTIVE at Kite in addition to the app's own "
                   f"protection ({protected_by}). Both can fire and SELL the same shares twice. Cancel the extra GTT "
                   f"in Kite unless you meant to keep it. Nothing was cancelled automatically.",
            issue_code=MULTIPLE_ACTIVE_GTTS, expected_quantity=0, position_state=_position_state(conn, positions[0]),
            suggested_action=CANCEL_IN_KITE, **common,
        )
        return

    if len(unprotected) == 1 and unreferenced_on_symbol == 1:
        position = unprotected[0]
        report(
            None, "order_status",
            expected=f"position #{position['id']}'s protection linked by GTT id",
            actual=f"{fingerprint}; possible owner #{position['id']}",
            detail=f"{symbol}: UNLINKED POSSIBLE GTT — {_gtt_text(gtt)} is ACTIVE at Kite and may be the stop of open "
                   f"position #{position['id']}, which has no active protective GTT of its own. The app does NOT "
                   f"link it automatically (symbol matching is not proof of ownership): the position is treated as "
                   f"UNPROTECTED. Review the position and its stop in Kite.",
            issue_code=UNLINKED_POSSIBLE_GTT, expected_quantity=position_quantity.protection_quantity(conn, position),
            position_state=_position_state(conn, position), suggested_action=REVIEW_POSITION, **common,
        )
        return

    report(
        None, "order_status",
        expected=f"one identifiable GTT per open {symbol} position",
        actual=f"{fingerprint}; candidates {candidates}; {unreferenced_on_symbol} unlinked GTT(s)",
        detail=f"{symbol}: AMBIGUOUS GTT OWNERSHIP — {_gtt_text(gtt)} is ACTIVE at Kite and not linked to any "
               f"position; {unreferenced_on_symbol} unlinked GTT(s) and open position(s) {candidates} make its owner "
               f"impossible to tell. Nothing was linked or cancelled. Review the positions and GTTs in Kite.",
        issue_code=AMBIGUOUS_GTT_OWNERSHIP, expected_quantity=None,
        position_state=_position_state(conn, unprotected[0]), suggested_action=REVIEW_POSITION, **common,
    )


def _symbol(gtt: dict) -> str:
    return _gtt_key(gtt)[1]


# ---- broker holdings vs the app's open positions --------------------------------

def _check_position_state(conn: sqlite3.Connection, kite, account, snapshot: BrokerSnapshot | None = None) -> None:
    # PK-13: what each position actually holds — filled, less what its exits
    # sold (a partial exit's residual included) — never the requested
    # quantity. A working entry counts only what has filled so far.
    expected_by_symbol: dict[str, int] = {}
    for pos in risk_engine.list_open_positions(conn, account["id"]):
        held = pos["open_quantity"]
        if not held or held < 0:
            continue  # nothing bought yet (order_monitor follows it), or unknown / NEEDS REVIEW (flagged there)
        expected_by_symbol[pos["symbol"]] = expected_by_symbol.get(pos["symbol"], 0) + held
    if not expected_by_symbol:
        return

    snapshot = snapshot or BrokerSnapshot()
    if not snapshot.held_loaded:
        snapshot.load_held(kite)
    if snapshot.held is None:
        return

    # Only a SHORTFALL is a problem: holding more than the app's tickets
    # account for is normal when the stock is also held outside the app.
    # PK-7: a shortfall never closes a position — fewer shares held does not
    # prove which order sold them.
    for symbol, expected_qty in expected_by_symbol.items():
        actual_qty = snapshot.held.get(symbol, 0)
        if actual_qty < expected_qty:
            log_mismatch(
                conn, None, "position_state",
                expected=f"{expected_qty} shares", actual=f"{actual_qty} shares",
                detail=f"{symbol}: the app's open tickets hold {expected_qty} shares but the broker shows only "
                       f"{actual_qty} — sold outside the app, or an order didn't fill.{_armed_note(snapshot, symbol)}",
                issue_code=BROKER_POSITION_SHORTFALL, severity=URGENT, symbol=symbol,
                expected_quantity=expected_qty, broker_quantity=actual_qty, position_state="open",
                suggested_action=REVIEW_POSITION,
            )


def _armed_note(snapshot: BrokerSnapshot, symbol: str) -> str:
    if snapshot.gtts is None:
        return " (The GTT book could not be read this cycle.)"
    armed = [g for g in _active(snapshot.gtts) if _symbol(g) == symbol and "SELL" in _gtt_sides(g)]
    if not armed:
        return ""
    listed = ", ".join(f"GTT {g['id']} (SELL {_gtt_quantity(g)})" for g in armed)
    return f" Still ARMED at Kite: {listed} — against shares the broker no longer holds. Check them in Kite."
