"""Phase 3 S4 (DARK): the protection operation journal and its state machine.

docs/PHASE_3_DESIGN_DECISIONS.md (R6) is the specification; section and row numbers below refer to it.

This is the ONLY module that writes protection_operations, protection_operation_resolutions,
trade_tickets.protection_state or a protection_* ticket event (DD-5; a static test enforces it), and the only
module that names the DD-3 reason literals. Every write runs inside journal_transaction(), which is
instance_lock.fenced_transaction (BEGIN IMMEDIATE, then the epoch check under the write lock): a stale process
cannot write the journal (§4.2 "never allowed": any write by a non-current epoch).

What is enforced here:
- one ALLOWED_TRANSITIONS table (§4.2); every state change is ONE compare-and-set UPDATE keyed on
  (id, op_uuid, state IN <allowed from>[, attempts]); rowcount 0 = refused, nothing written (DD-5);
- terminal states have no outgoing transition (§4.2); the supersede overlay is never a state (Q1);
- claims are INSERT ... SELECT from trade_tickets, so account_id and mode cannot differ from the position's
  and a NULL-mode ticket can never be journaled (DD-5, R2-4);
- every transition appends exactly one protection_* event in the same transaction (DD-7);
- the DX effect class is computed, recorded in `evidence` with the read it relied on, and never stored as a
  column (DX; rev. 3 precedence, R3-1);
- resolved_manual describes the op, never the position: the position's protection_state is re-derived from
  the resolution's evidence read and is never PROTECTION_CONFIRMED by outcome alone (R2-6).

Nothing here sends a broker request (protection_send.py, S5) or decides an Emergency Exit sale
(emergency_exit_engine.py, S7). Not registered and not wired into any live path (DD-2, RR §14 item 5).
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from backend import config, db, instance_lock, position_quantity
from backend.protection_evidence import (
    GTT_ACTIVE, GTT_REMOVED_STATUSES, GTT_TRIGGERED, Evidence, iso, normalise_gtt_id, parse_time, utc_now,
)

logger = logging.getLogger(__name__)

# ---- vocabulary (db.py holds the DDL; these must match it) ---------------------------------------------

STATES = db.PROTECTION_OPERATION_STATES
OPEN_STATES = db.PROTECTION_OPEN_OPERATION_STATES
TERMINAL_STATES = tuple(s for s in STATES if s not in OPEN_STATES)
INTENT, SENDING, ACKNOWLEDGED, UNKNOWN, NEEDS_REVIEW = OPEN_STATES
CONFIRMED, ADOPTED, REJECTED, NOT_SENT, ABSENT_AFTER_SETTLE, RESOLVED_MANUAL = (
    "confirmed", "adopted", "rejected", "not_sent", "absent_after_settle", "resolved_manual")

CREATE, MODIFY, CANCEL = "create", "modify", "cancel"
UP, DOWN, TIGHTEN, LOOSEN = "up", "down", "tighten", "loosen"

# DD-3 reasons. The ONLY place these literals appear outside db.py's DDL (static test).
R_FILL, R_USER_RECREATE = "fill", "user_recreate"
R_FILL_RESIZE, R_EXIT_FILL, R_TRAILING, R_ATTRIBUTION_CAP, R_USER_MODIFY = (
    "fill_resize", "exit_fill", "trailing", "attribution_cap", "user_modify")
R_POSITION_CLOSED, R_SUPERSEDE_CLEANUP, R_DUPLICATE_CLEANUP, R_EXIT_PREPARE, R_USER_CANCEL = (
    "position_closed", "supersede_cleanup", "duplicate_cleanup", "exit_prepare", "user_cancel")
REASONS = {
    CREATE: (R_FILL, R_USER_RECREATE),
    MODIFY: (R_FILL_RESIZE, R_EXIT_FILL, R_TRAILING, R_ATTRIBUTION_CAP, R_USER_MODIFY),
    CANCEL: (R_POSITION_CLOSED, R_SUPERSEDE_CLEANUP, R_DUPLICATE_CLEANUP, R_EXIT_PREPARE, R_USER_CANCEL),
}
FIXED_DIRECTION = {R_FILL_RESIZE: UP, R_EXIT_FILL: DOWN, R_ATTRIBUTION_CAP: DOWN, R_TRAILING: TIGHTEN}
USER_REASONS = frozenset({R_USER_RECREATE, R_USER_MODIFY, R_USER_CANCEL})

SUPERSEDED_BY = ("emergency_exit", "exit", "close", "user")
RESOLUTION_OUTCOMES = ("linked_gtt", "no_app_gtt", "deleted_in_kite_verified")

# DX effect classes (never stored as a column).
DX_NONE, DX_REDUNDANT, DX_REDUCES, DX_INCREASES, DX_CAPS = (
    "none", "redundant", "reduces_protection", "increases_sell_exposure", "caps_sell_exposure")

# ---- §4.2 transition table ------------------------------------------------------------------------------
# (from, to) -> the R6 rows that use it. Nothing leaves a terminal state. The overlay is not a transition.
ALLOWED_TRANSITIONS: dict[tuple[str, str], tuple[str, ...]] = {
    (INTENT, SENDING): ("5",),
    (INTENT, NOT_SENT): ("2", "3", "4", "5a", "25"),
    (SENDING, ACKNOWLEDGED): ("6",),
    (SENDING, REJECTED): ("7",),
    (SENDING, UNKNOWN): ("8", "10"),
    (ACKNOWLEDGED, CONFIRMED): ("11",),
    (ACKNOWLEDGED, NEEDS_REVIEW): ("12", "15b"),
    (UNKNOWN, ADOPTED): ("14",),
    (UNKNOWN, CONFIRMED): ("15",),
    (UNKNOWN, NEEDS_REVIEW): ("15a", "15b", "18"),
    (UNKNOWN, SENDING): ("16",),
    (UNKNOWN, ABSENT_AFTER_SETTLE): ("17",),
    (UNKNOWN, ACKNOWLEDGED): ("17c",),
    (UNKNOWN, REJECTED): ("17c",),
    (NEEDS_REVIEW, RESOLVED_MANUAL): ("22",),
}

EVENT_FOR_STATE = {
    INTENT: "protection_pending", SENDING: "protection_submitted", ACKNOWLEDGED: "protection_submitted",
    UNKNOWN: "protection_unknown", NEEDS_REVIEW: "protection_needs_review", ADOPTED: "protection_adopted",
    REJECTED: "protection_rejected", NOT_SENT: "protection_not_sent",
    ABSENT_AFTER_SETTLE: "protection_absent_after_settle", RESOLVED_MANUAL: "protection_resolved",
}
EV_SUPERSEDED, EV_LATE_GTT = "protection_superseded", "protection_late_gtt_appeared"

# Findings raised through the existing reconciliation_log path (M-4, DD-7 rev. 2).
LATE_GTT_APPEARED = "LATE_GTT_APPEARED"
LATE_RESPONSE_CONTRADICTS = "LATE_RESPONSE_CONTRADICTS"
LINKED_GTT_MISSING = "LINKED_GTT_MISSING"
GTT_REMOVED_EXTERNALLY = "GTT_REMOVED_EXTERNALLY"
CONFIRMED_CANCEL_CONTRADICTED = "CONFIRMED_CANCEL_CONTRADICTED"
CANCEL_REFUSAL_CONTRADICTED = "CANCEL_REFUSAL_CONTRADICTED"


class ProtectionRefused(RuntimeError):
    """A journal request refused before anything was written."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class TransitionRefused(ProtectionRefused):
    pass


class ClaimRefused(ProtectionRefused):
    pass


# ---- the fenced journal transaction ---------------------------------------------------------------------

_tx = threading.local()


@contextlib.contextmanager
def journal_transaction(conn: sqlite3.Connection):
    """instance_lock.fenced_transaction plus a marker that every journal write checks: no journal write can
    happen outside a fenced transaction on this thread (§4.2, RR §14 item 7)."""
    if not journal_available(conn):
        raise ProtectionRefused("JOURNAL_ABSENT", "the dark protection schema is not present on this database")
    with instance_lock.fenced_transaction(conn):
        previous = getattr(_tx, "conn", None)
        _tx.conn = conn
        try:
            yield conn
        finally:
            _tx.conn = previous


def _require_tx(conn: sqlite3.Connection) -> None:
    if getattr(_tx, "conn", None) is not conn or not conn.in_transaction:
        raise ProtectionRefused("UNFENCED_JOURNAL_WRITE", "journal writes run only inside journal_transaction()")


def journal_available(conn: sqlite3.Connection) -> bool:
    return db.dark_protection_schema_status(conn)["state"] == "present"


# ---- reads ----------------------------------------------------------------------------------------------

def get_op(conn: sqlite3.Connection, op_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM protection_operations WHERE id = ?", (op_id,)).fetchone()
    return None if row is None else dict(row)


def ops_for_position(conn: sqlite3.Connection, position_id: int, *, open_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM protection_operations WHERE position_id = ?"
    if open_only:
        sql += f" AND state IN ({db._sql_list(OPEN_STATES)})"
    return [dict(r) for r in conn.execute(sql + " ORDER BY id", (position_id,)).fetchall()]


def evidence_entries(op: dict) -> list:
    try:
        value = json.loads(op.get("evidence") or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else [value]


def payload(op: dict) -> dict:
    try:
        value = json.loads(op["request_payload"])
    except (TypeError, ValueError, KeyError):
        return {}
    return value if isinstance(value, dict) else {}


def get_position(conn: sqlite3.Connection, position_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (position_id,)).fetchone()
    return None if row is None else dict(row)


def open_quantity(conn: sqlite3.Connection, position_id: int) -> int | None:
    return position_quantity.open_quantity(conn, position_id)


def exit_in_flight(conn: sqlite3.Connection, position_id: int) -> bool:
    return conn.execute(f"SELECT 1 FROM trade_tickets WHERE closes_ticket_id = ? AND {db._EXIT_OWNER_PREDICATE}",
                        (position_id,)).fetchone() is not None


def broker_flat(position: dict, open_qty: int | None) -> bool:
    """Broker-proven flat in the app's records: closed by the existing close path (closed_at set) and an open
    quantity of exactly 0. An unknown quantity is never flat."""
    return bool(position.get("closed_at")) and open_qty == 0


# ---- events and evidence --------------------------------------------------------------------------------

def _event(conn: sqlite3.Connection, position_id: int, event_type: str, detail: dict) -> None:
    conn.execute("INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, ?, ?)",
                 (position_id, event_type, json.dumps(detail, sort_keys=True, default=str)))


def _appended(op: dict, entry: dict | None) -> str | None:
    if entry is None:
        return op.get("evidence")
    entries = evidence_entries(op)
    entries.append(entry)
    return json.dumps(entries, sort_keys=True, default=str)


# ---- the compare-and-set ---------------------------------------------------------------------------------

def transition(conn: sqlite3.Connection, op: dict, to_state: str, *, from_states: tuple[str, ...],
               attempt: int | None = None, sets: dict | None = None, evidence: dict | None = None,
               extra_where: str = "", extra_args: tuple = (), now: datetime | None = None,
               event_detail: dict | None = None) -> dict:
    """ONE CAS. Refuses an illegal (from, to) pair before touching the database, and a lost race (rowcount 0)
    after: TransitionRefused either way, nothing written. Returns the op as now stored."""
    _require_tx(conn)
    for frm in from_states:
        if (frm, to_state) not in ALLOWED_TRANSITIONS:
            raise TransitionRefused("ILLEGAL_TRANSITION", f"{frm} -> {to_state} is not in §4.2")
    now = now or utc_now()
    assignments = {"state": to_state, "updated_at": iso(now)}
    if to_state in TERMINAL_STATES:
        assignments["resolved_at"] = iso(now)
    assignments.update(sets or {})
    fresh = get_op(conn, op["id"])
    if fresh is None:
        raise TransitionRefused("OP_MISSING", str(op["id"]))
    if evidence is not None:
        assignments["evidence"] = _appended(fresh, evidence | {"at": iso(now), "to": to_state})
    cols = ", ".join(f"{k} = ?" for k in assignments)
    where = f"id = ? AND op_uuid = ? AND state IN ({','.join('?' * len(from_states))})"
    args = [*assignments.values(), op["id"], op["op_uuid"], *from_states]
    if attempt is not None:
        where += " AND attempts = ?"
        args.append(attempt)
    if extra_where:
        where += f" AND ({extra_where})"
        args.extend(extra_args)
    changed = conn.execute(f"UPDATE protection_operations SET {cols} WHERE {where}", args).rowcount
    if changed != 1:
        raise TransitionRefused("CAS_FAILED", f"op {op['id']} is no longer in {from_states}"
                                              f"{'' if attempt is None else f' at attempt {attempt}'}")
    event_type = EVENT_FOR_STATE.get(to_state)
    if to_state == CONFIRMED:
        event_type = "protection_cancelled" if op["kind"] == CANCEL else "protection_confirmed"
    _event(conn, op["position_id"], event_type, {"op_id": op["id"], "op_uuid": op["op_uuid"], "kind": op["kind"],
                                                 "from": fresh["state"], "to": to_state, **(event_detail or {})})
    return get_op(conn, op["id"])


def annotate(conn: sqlite3.Connection, op: dict, entry: dict, *, sets: dict | None = None,
             expect_state: tuple[str, ...] | None = None, now: datetime | None = None) -> dict:
    """Appends evidence (and optionally sets counters) WITHOUT a state change: settle counts, late responses
    on terminal ops (rows 26/27), effect-class records. Keyed like a CAS on (id, op_uuid[, state])."""
    _require_tx(conn)
    now = now or utc_now()
    fresh = get_op(conn, op["id"])
    assignments = {"evidence": _appended(fresh, entry | {"at": iso(now)}), "updated_at": iso(now),
                   **(sets or {})}
    if "state" in assignments or "resolved_at" in assignments:
        raise ProtectionRefused("ANNOTATE_CHANGES_STATE")
    where, args = "id = ? AND op_uuid = ?", [op["id"], op["op_uuid"]]
    if expect_state:
        where += f" AND state IN ({','.join('?' * len(expect_state))})"
        args.extend(expect_state)
    cols = ", ".join(f"{k} = ?" for k in assignments)
    if conn.execute(f"UPDATE protection_operations SET {cols} WHERE {where}",
                    [*assignments.values(), *args]).rowcount != 1:
        raise TransitionRefused("CAS_FAILED", f"op {op['id']} changed under the annotation")
    return get_op(conn, op["id"])


# ---- DX: dual-exposure classification (rev. 3 precedence, R3-1) -----------------------------------------

@dataclass(frozen=True)
class DxResult:
    cls: str
    delta_s: int
    delta_p: int
    detail: dict = field(default_factory=dict)

    def record(self, read_id: int | None) -> dict:
        return {"dx": self.cls, "delta_s": self.delta_s, "delta_p": self.delta_p, "read_id": read_id, **self.detail}


CAP_CONDITIONS = ("quantity_decrease_only", "attribution_broken", "requested_equals_q_cap", "q_cap_in_range",
                  "target_linked_active_prestate", "no_exit_in_flight", "no_unresolved_resume")
M2_CONDITIONS = ("target_app_proven", "different_linked_gtt", "survivor_protects_full_quantity",
                 "no_exit_or_change_open")


def classify(*, kind: str, reason: str, q0: int, q1: int, open_qty: int, loosen: bool,
             cap_conditions: dict | None = None, m2_conditions: dict | None = None) -> DxResult:
    """DX rev. 3. q0 = the target's current SELL quantity in the send-time read (0 for a create), q1 = the
    requested SELL quantity (0 for a cancel), N = the app open quantity. ΔS = q1 - q0; ΔP = C0 - C1 with
    C = min(q, N); any trigger/limit loosen counts as ΔP > 0. Pure: the caller supplies facts from ONE read."""
    n = max(0, int(open_qty))
    delta_s = int(q1) - int(q0)
    delta_p = min(int(q0), n) - min(int(q1), n)
    if loosen:
        delta_p = max(delta_p, 0) + 1
    detail = {"q0": q0, "q1": q1, "open_qty": n, "loosen": loosen}
    # (1) any increase of sell exposure
    if delta_s > 0:
        return DxResult(DX_INCREASES, delta_s, delta_p, detail)
    # (2) attribution_cap decreases: caps_sell_exposure only with all seven conditions, else reduces_protection;
    #     steps 3-5 are never consulted for such an op.
    if reason == R_ATTRIBUTION_CAP and delta_s < 0:
        conditions = {c: bool((cap_conditions or {}).get(c)) for c in CAP_CONDITIONS}
        detail["cap_conditions"] = conditions
        return DxResult(DX_CAPS if all(conditions.values()) else DX_REDUCES, delta_s, delta_p, detail)
    # (3) increases neither axis
    if delta_s <= 0 and delta_p <= 0:
        return DxResult(DX_NONE, delta_s, delta_p, detail)
    # (4) M-2 duplicate cleanup
    if delta_p > 0 and reason == R_DUPLICATE_CLEANUP:
        conditions = {c: bool((m2_conditions or {}).get(c)) for c in M2_CONDITIONS}
        detail["m2_conditions"] = conditions
        if all(conditions.values()):
            return DxResult(DX_REDUNDANT, delta_s, delta_p, detail)
    # (5)
    return DxResult(DX_REDUCES, delta_s, delta_p, detail)


def sell_quantity_of_payload(p: dict) -> int | None:
    """L(v) of a request payload: the sum of its SELL legs; None if the payload is not well formed (R4 case 2
    fail-closed list: no leg list, a leg without type or quantity, a leg count inconsistent with the type)."""
    legs = p.get("orders")
    if not isinstance(legs, list) or not legs:
        return None
    expected = {"single": 1, "two-leg": 2}.get(p.get("trigger_type"))
    if expected is None or len(legs) != expected:
        return None
    total = 0
    for leg in legs:
        if not isinstance(leg, dict) or leg.get("transaction_type") not in ("SELL", "BUY"):
            return None
        q = leg.get("quantity")
        if isinstance(q, bool) or not isinstance(q, int) or q < 0:
            return None
        if leg["transaction_type"] == "SELL":
            total += q
    return total


def is_loosen(old: dict | None, new: dict | None) -> bool:
    """A long position's stop is loosened when a trigger or a SELL leg's limit moves DOWN (further from the
    market). Unknown shapes are treated as a loosen (fail closed)."""
    if not old or not new:
        return True
    try:
        old_t, new_t = [float(x) for x in old.get("trigger_values") or []], [float(x) for x in new.get("trigger_values") or []]
        old_p = [float(l["price"]) for l in old.get("orders") or [] if l.get("transaction_type") == "SELL"]
        new_p = [float(l["price"]) for l in new.get("orders") or [] if l.get("transaction_type") == "SELL"]
    except (TypeError, ValueError, KeyError):
        return True
    if len(old_t) != len(new_t) or len(old_p) != len(new_p):
        return True
    return any(n < o for o, n in zip(old_t, new_t)) or any(n < o for o, n in zip(old_p, new_p))


def direction_of(old: dict, new: dict) -> str:
    """DD-4: classified by the riskiest component: any quantity increase -> up; else any loosen -> loosen;
    else any quantity decrease -> down; else tighten."""
    q_old, q_new = sell_quantity_of_payload(old), sell_quantity_of_payload(new)
    if q_old is None or q_new is None:
        return UP  # unknown quantities: the riskiest reading
    if q_new > q_old:
        return UP
    if is_loosen(old, new):
        return LOOSEN
    if q_new < q_old:
        return DOWN
    return TIGHTEN


def gtt_as_payload(gtt) -> dict:
    """A listed GttView in request-payload shape (for comparisons with a request)."""
    return {"trigger_type": gtt.trigger_type, "trigger_values": list(gtt.trigger_values),
            "exchange": gtt.exchange, "tradingsymbol": gtt.tradingsymbol,
            "orders": [{"transaction_type": l.transaction_type, "quantity": l.quantity, "product": l.product,
                        "order_type": l.order_type, "price": l.price} for l in gtt.legs]}


def fingerprint(p: dict) -> dict:
    """The request fingerprint (P§5): instrument, trigger type and values, and every leg's side, quantity,
    product, order type and price. Excludes broker-side fields (B-09)."""
    return {
        "exchange": str(p.get("exchange") or "").upper(), "tradingsymbol": str(p.get("tradingsymbol") or "").upper(),
        "trigger_type": p.get("trigger_type"),
        "trigger_values": [float(x) for x in p.get("trigger_values") or []],
        "orders": [{"transaction_type": l.get("transaction_type"), "quantity": l.get("quantity"),
                    "product": l.get("product"), "order_type": l.get("order_type"),
                    "price": float(l["price"]) if isinstance(l.get("price"), (int, float)) else l.get("price")}
                   for l in p.get("orders") or []],
    }


def matches_fingerprint(gtt, fp: dict) -> bool:
    try:
        return fingerprint(gtt_as_payload(gtt)) == fp
    except (TypeError, ValueError):
        return False


# ---- claims (row 1) -------------------------------------------------------------------------------------

def _validate_shape(kind: str, reason: str, direction: str | None) -> None:
    if kind not in REASONS or reason not in REASONS[kind]:
        raise ClaimRefused("REASON_NOT_ALLOWED", f"{kind}/{reason}")
    if (kind == MODIFY) != (direction is not None):
        raise ClaimRefused("DIRECTION_SHAPE", f"{kind} with direction {direction!r}")
    if direction is not None and direction not in (UP, DOWN, TIGHTEN, LOOSEN):
        raise ClaimRefused("DIRECTION_SHAPE", repr(direction))
    fixed = FIXED_DIRECTION.get(reason)
    if fixed is not None and direction != fixed:
        raise ClaimRefused("DIRECTION_SHAPE", f"{reason} requires direction {fixed}")


def _last_create(conn: sqlite3.Connection, position_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM protection_operations WHERE position_id = ? AND kind = 'create' "
                       "ORDER BY id DESC LIMIT 1", (position_id,)).fetchone()
    return None if row is None else dict(row)


def _resolution(conn: sqlite3.Connection, op_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM protection_operation_resolutions WHERE op_id = ?", (op_id,)).fetchone()
    return None if row is None else dict(row)


def create_claim_problem(conn: sqlite3.Connection, position: dict, reason: str, evidence: Evidence | None,
                         *, now: datetime) -> str | None:
    """M-5 conditions 1-4 (condition 5, DD-9, is the caller's may_increase_exposure). None = claimable."""
    if position.get("kite_gtt_id") is not None:
        return "M5_LINKED_GTT_EXISTS"
    if ops_for_position(conn, position["id"], open_only=True):
        return "M5_OPEN_OP_EXISTS"
    watch_since = iso(now - timedelta(days=config.LATE_MATCH_WATCH_DAYS))
    absent = conn.execute("SELECT 1 FROM protection_operations WHERE position_id = ? AND state = 'absent_after_settle' "
                          "AND resolved_at >= ?", (position["id"], watch_since)).fetchone()
    if absent is not None and reason != R_USER_RECREATE:
        return "M5_ABSENT_AFTER_SETTLE_WATCH"
    last = _last_create(conn, position["id"])
    if last is not None and reason != R_USER_RECREATE:
        if last["state"] == NOT_SENT:
            if last["superseded_at"] is not None:
                return "M5_LAST_CREATE_SUPERSEDED"
        elif last["state"] in (REJECTED, ABSENT_AFTER_SETTLE):
            return "M5_USER_RECREATE_REQUIRED"
        elif last["state"] == RESOLVED_MANUAL:
            res = _resolution(conn, last["id"])
            if res is None or res["outcome"] in ("no_app_gtt", "deleted_in_kite_verified"):
                return "M5_USER_RECREATE_REQUIRED"
        else:
            return "M5_LAST_CREATE_NOT_PROVABLY_ABSENT"
    if evidence is None or not evidence.complete:
        return "M5_NO_COMPLETE_READ"
    linked = {normalise_gtt_id(r[0]) for r in conn.execute(
        "SELECT kite_gtt_id FROM trade_tickets WHERE kite_gtt_id IS NOT NULL")}
    for g in evidence.sell_gtts_on(position["exchange"], position["symbol"]):
        if g.id not in linked and g.status != "deleted" and g.status != "cancelled":
            return "M5_UNLINKED_SELL_GTT_ON_INSTRUMENT"
    return None


def claim(conn: sqlite3.Connection, *, position_id: int, kind: str, reason: str, request: dict,
          direction: str | None = None, target_gtt_id=None, automatic: bool = False,
          dx: DxResult | None = None, evidence: Evidence | None = None, may_increase_exposure: bool = False,
          now: datetime | None = None) -> dict:
    """Row 1. Must run inside journal_transaction(). The INSERT ... SELECT copies account_id and mode from the
    position (DD-5, R2-4); every claim predicate is in its WHERE clause or checked before it under the same
    write lock. Raises ClaimRefused (nothing written) or sqlite3.IntegrityError (the partition index: the
    caller's transaction rolls back)."""
    _require_tx(conn)
    now = now or utc_now()
    _validate_shape(kind, reason, direction)
    position = get_position(conn, position_id)
    if position is None:
        raise ClaimRefused("POSITION_MISSING", str(position_id))
    if position.get("mode") is None:
        raise ClaimRefused("MODE_NULL", "a NULL-mode ticket can never be journaled (DD-5, R2-4)")
    requested = sell_quantity_of_payload(request)
    if requested is None and kind != CANCEL:
        raise ClaimRefused("PAYLOAD_MALFORMED", "the request payload is not a well-formed GTT request")
    if kind != CREATE and normalise_gtt_id(target_gtt_id) is None:
        raise ClaimRefused("TARGET_REQUIRED", "a modify or cancel names its GTT")
    if kind in (CREATE, MODIFY):
        if conn.execute("SELECT 1 FROM protection_operations WHERE position_id = ? AND kind = 'cancel' "
                        f"AND state IN ({db._sql_list(OPEN_STATES)})", (position_id,)).fetchone():
            raise ClaimRefused("OPEN_CANCEL_ON_POSITION")
        if exit_in_flight(conn, position_id):
            raise ClaimRefused("EXIT_IN_FLIGHT")
        if conn.execute("SELECT 1 FROM protection_operations WHERE position_id = ? AND superseded_at IS NOT NULL "
                        f"AND state IN ({db._sql_list(OPEN_STATES)})", (position_id,)).fetchone():
            raise ClaimRefused("OPEN_SUPERSEDED_OP")
    if kind == CREATE:
        problem = create_claim_problem(conn, position, reason, evidence, now=now)
        if problem:
            raise ClaimRefused(problem)
    if dx is not None and dx.cls == DX_INCREASES and not may_increase_exposure:
        raise ClaimRefused("DD9_REFUSED", "increases_sell_exposure needs the DD-9 predicate")
    if automatic:
        if reason in USER_REASONS:
            raise ClaimRefused("USER_REASON_NOT_AUTOMATIC", reason)
        if dx is None:
            raise ClaimRefused("DX_REQUIRED", "an automatic claim needs a DX evaluation")
        if reason == R_ATTRIBUTION_CAP and dx.cls != DX_CAPS:
            raise ClaimRefused("CAP_NOT_CAPS_SELL_EXPOSURE", dx.cls)
        if kind == CANCEL and dx.cls not in (DX_NONE, DX_REDUNDANT):
            raise ClaimRefused("CANCEL_NOT_AUTOMATIC", dx.cls)
        if kind == MODIFY and reason != R_ATTRIBUTION_CAP and dx.cls not in (DX_NONE, DX_REDUNDANT, DX_INCREASES):
            raise ClaimRefused("MODIFY_NOT_AUTOMATIC", dx.cls)
        if kind == CANCEL:
            problem = reclaim_bound_problem(conn, position_id, target_gtt_id)
            if problem:
                raise ClaimRefused(problem)
    if kind != CREATE:
        target = normalise_gtt_id(target_gtt_id)
        elsewhere = conn.execute("SELECT id FROM trade_tickets WHERE kite_gtt_id IS NOT NULL AND kite_gtt_id = ? "
                                 "AND id != ?", (target_gtt_id, position_id)).fetchone()
        if elsewhere is not None:
            raise ClaimRefused("TARGET_LINKED_ELSEWHERE", f"GTT {target} is linked to ticket {elsewhere[0]}")
    op_uuid = str(uuid.uuid4())
    epoch = instance_lock.owner_epoch()
    entry = {"claim": {"automatic": automatic, "reason": reason, "at": iso(now)}}
    if dx is not None:
        entry["claim"]["dx"] = dx.record(evidence.read_id if evidence else None)
    filled = position_quantity.filled(position) or 0
    exits = position_quantity.exits_filled(conn, position_id) or 0
    cursor = conn.execute(
        "INSERT INTO protection_operations (op_uuid, account_id, position_id, mode, kind, reason, direction, state, "
        "basis_filled_quantity, basis_exits_filled, requested_quantity, request_fingerprint, request_payload, "
        "target_gtt_id, instance_epoch, evidence, created_at, updated_at) "
        "SELECT ?, t.account_id, t.id, t.mode, ?, ?, ?, 'intent', ?, ?, ?, ?, ?, ?, ?, ?, ?, ? "
        "FROM trade_tickets t WHERE t.id = ? AND t.mode IS NOT NULL",
        (op_uuid, kind, reason, direction, filled, exits, requested or 0,
         json.dumps(fingerprint(request) if kind != CANCEL else {"cancel": normalise_gtt_id(target_gtt_id)}),
         json.dumps(request), target_gtt_id, epoch, json.dumps([entry]), iso(now), iso(now), position_id))
    if cursor.rowcount != 1:
        raise ClaimRefused("CLAIM_PREDICATE_FAILED")
    op = get_op(conn, cursor.lastrowid)
    _event(conn, position_id, EVENT_FOR_STATE[INTENT], {"op_id": op["id"], "op_uuid": op_uuid, "kind": kind,
                                                       "reason": reason, "to": INTENT})
    return op


def reclaim_bound_problem(conn: sqlite3.Connection, position_id: int, target_gtt_id) -> str | None:
    """M-1 rev. 2: across all cancel ops for (position, target), SUM(attempts) <= PROTECTION_RETRY_MAX_ATTEMPTS."""
    total = conn.execute("SELECT COALESCE(SUM(attempts), 0) FROM protection_operations WHERE position_id = ? "
                         "AND kind = 'cancel' AND target_gtt_id = ?", (position_id, target_gtt_id)).fetchone()[0]
    return "RECLAIM_BOUND_REACHED" if total >= config.PROTECTION_RETRY_MAX_ATTEMPTS else None


def follow_up_cancel_reason(conn: sqlite3.Connection, position_id: int, target_gtt_id) -> str:
    """M-1 rev. 2: a new cancel takes supersede_cleanup when its target is a superseded op's result."""
    row = conn.execute("SELECT 1 FROM protection_operations WHERE position_id = ? AND superseded_at IS NOT NULL "
                       "AND result_gtt_id = ?", (position_id, target_gtt_id)).fetchone()
    return R_SUPERSEDE_CLEANUP if row else R_POSITION_CLOSED


def flat_cancel_action(op: dict) -> str:
    """The M-1 rev. 2 state table: what may happen to a cancel op when its effect is re-evaluated as `none`."""
    state = op["state"]
    return {INTENT: "send", SENDING: "wait", ACKNOWLEDGED: "wait", UNKNOWN: "same_op_retry_if_m3",
            NEEDS_REVIEW: "human", NOT_SENT: "new_op_if_active", CONFIRMED: "done",
            RESOLVED_MANUAL: "per_q3"}.get(state, "rejected_rule" if state == REJECTED else "none")


# ---- the supersede overlay (Q1; M-7 step 2) -------------------------------------------------------------

def supersede_open_changes(conn: sqlite3.Connection, position_id: int, by_kind: str, by_ref: str,
                           *, now: datetime | None = None) -> list[dict]:
    """Sets the overlay on every open create/modify op of the position. An `intent` becomes `not_sent` with the
    overlay (row 4); the others keep their state (row 19). Open cancels are never superseded."""
    _require_tx(conn)
    if by_kind not in SUPERSEDED_BY or not str(by_ref or "").strip():
        raise ProtectionRefused("SUPERSEDE_SHAPE", f"{by_kind!r}/{by_ref!r}")
    now = now or utc_now()
    out = []
    overlay = {"superseded_at": iso(now), "superseded_by_kind": by_kind, "superseded_by_ref": str(by_ref)}
    for op in ops_for_position(conn, position_id, open_only=True):
        if op["kind"] == CANCEL or op["superseded_at"] is not None:
            continue
        if op["state"] == INTENT:
            out.append(transition(conn, op, NOT_SENT, from_states=(INTENT,), sets=overlay,
                                  evidence={"superseded": by_kind, "ref": by_ref}, now=now,
                                  event_detail={"superseded_by": by_kind}))
        else:
            cols = ", ".join(f"{k} = ?" for k in overlay)
            changed = conn.execute(
                f"UPDATE protection_operations SET {cols}, updated_at = ? WHERE id = ? AND op_uuid = ? AND state = ? "
                "AND superseded_at IS NULL", (*overlay.values(), iso(now), op["id"], op["op_uuid"], op["state"])).rowcount
            if changed != 1:
                raise TransitionRefused("CAS_FAILED", f"op {op['id']} changed under the supersede")
            out.append(get_op(conn, op["id"]))
        _event(conn, position_id, EV_SUPERSEDED, {"op_id": op["id"], "op_uuid": op["op_uuid"], "by": by_kind,
                                                  "ref": str(by_ref), "state": out[-1]["state"]})
    return out


def _open_cancel_exists(conn: sqlite3.Connection, position_id: int, gtt_id) -> bool:
    return conn.execute(f"SELECT 1 FROM protection_operations WHERE position_id = ? AND kind = 'cancel' "
                        f"AND target_gtt_id = ? AND state IN ({db._sql_list(OPEN_STATES)})",
                        (position_id, gtt_id)).fetchone() is not None


def resolve_superseded_result(conn: sqlite3.Connection, op: dict, gtt_id, *, app_proven: bool,
                              now: datetime | None = None) -> dict:
    """§4.3 (Q1 c): a superseded op's resolution revealed a GTT id. The action is re-derived from the position
    at THIS moment. A cleanup cancel is claimed only if none is open for the id; a claim refusal never loses
    the result already recorded (it is noted in evidence instead)."""
    _require_tx(conn)
    now = now or utc_now()
    position = get_position(conn, op["position_id"])
    open_qty = open_quantity(conn, position["id"])
    request = payload(op)
    result = {"gtt_id": str(gtt_id)}

    def cleanup(automatic: bool, dx_cls: str) -> dict:
        if _open_cancel_exists(conn, position["id"], gtt_id):
            return {"action": "cancel_already_open"}
        q = sell_quantity_of_payload(request) or 0
        dx = classify(kind=CANCEL, reason=R_SUPERSEDE_CLEANUP, q0=q, q1=0,
                      open_qty=0 if dx_cls == DX_NONE else (open_qty or 0), loosen=False)
        try:
            c = claim(conn, position_id=position["id"], kind=CANCEL, reason=R_SUPERSEDE_CLEANUP, request={},
                      target_gtt_id=gtt_id, automatic=automatic, dx=dx, now=now)
        except ClaimRefused as e:
            return {"action": "cleanup_claim_refused", "code": e.code}
        return {"action": "cleanup_cancel", "cancel_op_id": c["id"], "dx": dx.cls}

    if broker_flat(position, open_qty):
        result.update(cleanup(True, DX_NONE))
    elif exit_in_flight(conn, position["id"]):
        result.update(cleanup(False, DX_REDUCES))       # one attempt inside the exit claim
    elif position.get("kite_gtt_id") is None:
        fits = (sell_quantity_of_payload(request) is not None and request.get("orders")
                and all(l.get("transaction_type") == "SELL" and l.get("product") == "CNC" and
                        (l.get("quantity") or 0) <= (open_qty or 0) for l in request["orders"])
                and str(request.get("tradingsymbol", "")).upper() == str(position["symbol"]).upper()
                and str(request.get("exchange", "")).upper() == str(position["exchange"]).upper())
        if fits and link_gtt(conn, position["id"], gtt_id):
            result["action"] = "linked_overlay_lapsed"
        else:
            result["action"] = "needs_review_shape_or_link"
    elif app_proven:
        result["action"] = "duplicate_candidate_m2"       # M-2 is evaluated against a fresh read (S6/S7)
    else:
        result["action"] = "needs_review_adopted_duplicate"
    fresh = get_op(conn, op["id"])
    annotate(conn, fresh, {"superseded_resolution": result}, now=now)
    if result["action"].startswith("needs_review"):
        fresh = get_op(conn, op["id"])
        if fresh["state"] in (ACKNOWLEDGED, UNKNOWN):
            transition(conn, fresh, NEEDS_REVIEW, from_states=(fresh["state"],),
                       evidence={"review": result["action"]}, now=now, event_detail={"review": result["action"]})
        else:
            raise_finding(conn, position, "SUPERSEDED_RESULT_NEEDS_REVIEW", result["action"], gtt_id=gtt_id)
            conn.execute("UPDATE trade_tickets SET protection_state = 'PROTECTION_NEEDS_REVIEW' WHERE id = ?",
                         (position["id"],))
    return result


# ---- result ids and links (DD-6 guard) -------------------------------------------------------------------

def link_gtt(conn: sqlite3.Connection, position_id: int, gtt_id, *, expect_current=None) -> bool:
    """The DD-6 link CAS: sets trade_tickets.kite_gtt_id only if no OTHER ticket links the id (independent of
    whether idx_trade_tickets_unique_gtt exists) and the current link is as expected. True if linked."""
    _require_tx(conn)
    cond = "kite_gtt_id IS NULL" if expect_current is None else "kite_gtt_id = ?"
    args = [gtt_id, position_id] + ([] if expect_current is None else [expect_current]) + [gtt_id, position_id]
    return conn.execute(
        f"UPDATE trade_tickets SET kite_gtt_id = ? WHERE id = ? AND ({cond}) AND NOT EXISTS (SELECT 1 FROM trade_tickets "
        "WHERE kite_gtt_id = ? AND id != ?)", args).rowcount == 1


def unlink_gtt(conn: sqlite3.Connection, position_id: int, gtt_id) -> bool:
    """Only for a HUMAN resolution (R2-6: M-5 forbids automatic unlinking)."""
    _require_tx(conn)
    return conn.execute("UPDATE trade_tickets SET kite_gtt_id = NULL WHERE id = ? AND kite_gtt_id = ?",
                        (position_id, gtt_id)).rowcount == 1


def raise_finding(conn: sqlite3.Connection, position: dict, code: str, detail: str, *, gtt_id=None) -> None:
    """URGENT finding through the existing reconciliation_log path (M-4; DD-7 rev. 2)."""
    from backend import reconciliation
    reconciliation.log_mismatch(conn, position["id"], "order_status", expected="protection consistent",
                                actual=code, detail=f"{position.get('symbol')}: {code} — {detail}",
                                issue_code=code, severity=reconciliation.URGENT, symbol=position.get("symbol"),
                                gtt_id=gtt_id, suggested_action=reconciliation.CHECK_KITE)


# ---- derived position protection state (DD-7, R2-6, P§3.3) ----------------------------------------------

def intended_shape(conn: sqlite3.Connection, position_id: int, gtt_id) -> dict | None:
    """The request payload of the newest op that produced or confirmed this GTT (create/modify confirmed or
    adopted, or a linked_gtt resolution of such an op). None: the intended shape is not known."""
    key = normalise_gtt_id(gtt_id)
    for op in reversed(ops_for_position(conn, position_id)):
        if op["kind"] == CANCEL:
            continue
        produced = normalise_gtt_id(op["result_gtt_id"]) == key or normalise_gtt_id(op["target_gtt_id"]) == key
        if not produced:
            continue
        if op["state"] in (CONFIRMED, ADOPTED):
            return payload(op)
        if op["state"] == RESOLVED_MANUAL:
            res = _resolution(conn, op["id"])
            if res is not None and res["outcome"] == "linked_gtt" and normalise_gtt_id(res["linked_gtt_id"]) == key:
                return payload(op)
    return None


def shape_problem(gtt, position: dict, open_qty: int, intended: dict | None) -> str | None:
    """P:256 for a listed GTT. None = the full CONFIRMED shape holds."""
    if gtt.instrument() != (str(position["exchange"]).upper(), str(position["symbol"]).upper()):
        return "MISMATCH_INSTRUMENT"
    if not gtt.legs or any(l.transaction_type != "SELL" for l in gtt.legs):
        return "MISMATCH_SIDE"
    if any(l.product != "CNC" for l in gtt.legs):
        return "MISMATCH_PRODUCT"
    if any(l.quantity != open_qty for l in gtt.legs):
        return "RESIZE"
    if intended is None:
        return "INTENDED_SHAPE_UNKNOWN"
    want = fingerprint(intended)
    got = fingerprint(gtt_as_payload(gtt))
    if want["trigger_values"] != got["trigger_values"] or \
            [l["price"] for l in want["orders"]] != [l["price"] for l in got["orders"]]:
        return "MISMATCH_PRICES"
    return None


def cleanup_discharged(conn: sqlite3.Connection, position_id: int, gtt_id, evidence: Evidence | None) -> bool:
    """DD-7 (rev. 2): the id is confirmed deleted/cancelled (a confirmed cancel op, or an explicit status in the
    read), consumed by its own trigger fill (the position was closed by it), or a manual resolution recorded
    no_app_gtt / deleted_in_kite_verified with a complete evidence read."""
    key = normalise_gtt_id(gtt_id)
    for op in ops_for_position(conn, position_id):
        if op["kind"] == CANCEL and normalise_gtt_id(op["target_gtt_id"]) == key and op["state"] == CONFIRMED:
            return True
        if op["state"] == RESOLVED_MANUAL and key in (normalise_gtt_id(op["target_gtt_id"]),
                                                     normalise_gtt_id(op["result_gtt_id"])):
            res = _resolution(conn, op["id"])
            if res is not None and res["outcome"] in ("no_app_gtt", "deleted_in_kite_verified"):
                return True
    if evidence is not None and evidence.complete:
        g = evidence.gtt(key)
        if g is not None and g.status in GTT_REMOVED_STATUSES:
            return True
    position = get_position(conn, position_id) or {}
    return position.get("exit_reason") == "gtt_triggered"


def derive_protection(conn: sqlite3.Connection, position_id: int, evidence: Evidence | None = None,
                      *, now: datetime | None = None) -> dict:
    """Read-only. {'state': one of db.PROTECTION_STATES, 'cleanup_required': bool, 'reason': str}. Derived from
    persisted facts plus one read; never PROTECTION_CONFIRMED without a complete, fresh read showing the full
    P:256 shape. CLOSED_CLEANUP_REQUIRED is derived (cleanup_required), never stored (DD-7)."""
    now = now or utc_now()
    position = get_position(conn, position_id)
    if position is None:
        raise ProtectionRefused("POSITION_MISSING", str(position_id))
    open_qty = open_quantity(conn, position_id)
    ops = ops_for_position(conn, position_id)
    open_ops = [o for o in ops if o["state"] in OPEN_STATES]
    gtt_id = position.get("kite_gtt_id")
    fresh = evidence is not None and evidence.complete and \
        (now - evidence.started_at).total_seconds() <= config.GTT_OBSERVATION_MAX_AGE_SECONDS

    if broker_flat(position, open_qty):
        pending = bool(open_ops) or (gtt_id is not None and not cleanup_discharged(conn, position_id, gtt_id, evidence))
        return {"state": "PROTECTION_NOT_REQUIRED", "cleanup_required": pending,
                "reason": "CLOSED_CLEANUP_REQUIRED" if pending else "POSITION_CLOSED"}
    filled = position_quantity.filled(position)
    if open_qty is None or filled is None:
        return {"state": "PROTECTION_UNKNOWN", "cleanup_required": False, "reason": "QUANTITY_UNKNOWN"}
    if not filled:
        return {"state": "PROTECTION_NOT_REQUIRED", "cleanup_required": False, "reason": "NOTHING_FILLED"}

    out = lambda state, reason: {"state": state, "cleanup_required": False, "reason": reason}  # noqa: E731
    if any(o["state"] == NEEDS_REVIEW for o in open_ops):
        return out("PROTECTION_NEEDS_REVIEW", "OP_NEEDS_REVIEW")
    cancels = [o for o in open_ops if o["kind"] == CANCEL]
    if cancels:
        return out("PROTECTION_CANCEL_UNKNOWN" if any(o["state"] == UNKNOWN for o in cancels)
                   else "PROTECTION_CANCEL_PENDING", "OPEN_CANCEL")
    change = next((o for o in open_ops if o["kind"] != CANCEL), None)
    if change is not None:
        return out({INTENT: "PROTECTION_INTENT", SENDING: "PROTECTION_SUBMITTED", ACKNOWLEDGED: "PROTECTION_ACKNOWLEDGED",
                    UNKNOWN: "PROTECTION_UNKNOWN"}[change["state"]], f"OPEN_{change['kind'].upper()}")
    last = next((o for o in reversed(ops) if o["kind"] != CANCEL), None)
    if last is not None and last["kind"] == MODIFY and last["reason"] == R_ATTRIBUTION_CAP and last["state"] == CONFIRMED \
            and last["requested_quantity"] < open_qty:
        return out("PROTECTION_NEEDS_REVIEW", "CAP_BELOW_OPEN_QUANTITY")
    if gtt_id is None:
        if last is not None and (last["state"] in (REJECTED, ABSENT_AFTER_SETTLE) or (
                last["state"] == RESOLVED_MANUAL and (_resolution(conn, last["id"]) or {}).get("outcome") != "linked_gtt")):
            return out("PROTECTION_FAILED", f"LAST_{last['kind'].upper()}_{last['state'].upper()}")
        return out("PROTECTION_PENDING", "NO_LINKED_GTT")
    if not fresh:
        return out("PROTECTION_STALE", "NO_FRESH_COMPLETE_READ")
    g = evidence.gtt(gtt_id)
    if g is None:
        return out("PROTECTION_UNKNOWN", "LINKED_GTT_NOT_LISTED")       # never auto-unlink (M-5)
    if g.status == GTT_TRIGGERED:
        return out("PROTECTION_TRIGGERED", "GTT_TRIGGERED")
    if g.status in GTT_REMOVED_STATUSES or g.status == "expired":
        return out("PROTECTION_FAILED", "GTT_REMOVED")
    if g.status != GTT_ACTIVE:
        return out("PROTECTION_NEEDS_REVIEW", f"GTT_STATUS_{g.status or 'UNKNOWN'}")   # B-32
    problem = shape_problem(g, position, open_qty, intended_shape(conn, position_id, gtt_id))
    if problem is None:
        return out("PROTECTION_CONFIRMED", "SHAPE_VERIFIED")
    if problem == "RESIZE":
        return out("PROTECTION_RESIZE_PENDING", problem)
    if problem == "INTENDED_SHAPE_UNKNOWN":
        return out("PROTECTION_UNKNOWN", problem)
    return out("PROTECTION_MISMATCH", problem)


def refresh_protection_state(conn: sqlite3.Connection, position_id: int, evidence: Evidence | None = None,
                             *, now: datetime | None = None) -> dict:
    """Persists the derived state into trade_tickets.protection_state (inside journal_transaction)."""
    _require_tx(conn)
    derived = derive_protection(conn, position_id, evidence, now=now)
    conn.execute("UPDATE trade_tickets SET protection_state = ? WHERE id = ?", (derived["state"], position_id))
    return derived


# ---- manual resolution (Q3, row 22, R2-6) ---------------------------------------------------------------

def resolve_manual(conn: sqlite3.Connection, op_id: int, *, outcome: str, reason: str, resolver: str,
                   evidence: Evidence, linked_gtt_id=None, now: datetime | None = None) -> dict:
    """Row 22. Only from needs_review; needs a complete read taken for the resolution (purpose 'resolution',
    fresh, post-resume, post-takeover). Inserts the resolution row, CASes the op, applies the R2-6 action, and
    re-derives the position's protection_state from THAT read. Returns {'op','resolution_id','protection'}."""
    _require_tx(conn)
    from backend.protection_evidence import freshness_problem, takeover_at
    now = now or utc_now()
    if outcome not in RESOLUTION_OUTCOMES:
        raise ProtectionRefused("RESOLUTION_OUTCOME", repr(outcome))
    if (outcome == "linked_gtt") != (normalise_gtt_id(linked_gtt_id) is not None):
        raise ProtectionRefused("RESOLUTION_LINK_SHAPE", "linked_gtt needs an id, and only linked_gtt may carry one")
    if not str(reason or "").strip() or not str(resolver or "").strip():
        raise ProtectionRefused("RESOLUTION_BLANK")
    if evidence is None or evidence.purpose != "resolution":
        raise ProtectionRefused("RESOLUTION_READ_REQUIRED", "a read taken for this resolution is required")
    problem = freshness_problem(evidence, now=now, takeover=takeover_at(conn), epoch=instance_lock.owner_epoch())
    if problem:
        raise ProtectionRefused("RESOLUTION_READ_UNUSABLE", problem)
    op = get_op(conn, op_id)
    if op is None:
        raise ProtectionRefused("OP_MISSING", str(op_id))
    if op["state"] != NEEDS_REVIEW:
        raise TransitionRefused("ILLEGAL_TRANSITION", f"{op['state']} -> resolved_manual (Q3: needs_review only)")
    position = get_position(conn, op["position_id"])
    open_qty = open_quantity(conn, op["position_id"])
    flat = broker_flat(position, open_qty)
    if outcome == "linked_gtt":
        g = evidence.gtt(linked_gtt_id)
        if g is None or g.status != GTT_ACTIVE:
            raise ProtectionRefused("RESOLUTION_LINK_NOT_ACTIVE", "the linked GTT must be active in the evidence read")
        if g.instrument() != (str(position["exchange"]).upper(), str(position["symbol"]).upper()) or not g.has_sell():
            raise ProtectionRefused("RESOLUTION_LINK_SHAPE_MISMATCH")
    if outcome == "deleted_in_kite_verified":
        key = op["target_gtt_id"] if op["target_gtt_id"] is not None else op["result_gtt_id"]
        g = evidence.gtt(key) if key is not None else None
        if g is None or g.status not in GTT_REMOVED_STATUSES:
            raise ProtectionRefused("RESOLUTION_DELETE_NOT_EXPLICIT", "B-32: needs an explicit deleted/cancelled status")
    res_id = conn.execute(
        "INSERT INTO protection_operation_resolutions (op_id, outcome, linked_gtt_id, reason, evidence_read_id, "
        "resolver, instance_epoch, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (op_id, outcome, linked_gtt_id, reason, evidence.read_id, resolver, instance_lock.owner_epoch(),
         iso(now))).lastrowid
    action = {}
    target = op["target_gtt_id"] if op["kind"] != CREATE else op["result_gtt_id"]
    if outcome == "linked_gtt":
        current = position.get("kite_gtt_id")
        if normalise_gtt_id(current) != normalise_gtt_id(linked_gtt_id):
            if current is not None and not unlink_gtt(conn, position["id"], current):
                raise TransitionRefused("CAS_FAILED", "the position's link changed under the resolution")
            if not link_gtt(conn, position["id"], linked_gtt_id):
                raise ProtectionRefused("RESOLUTION_LINK_REFUSED", "DD-6: the id is linked to another ticket")
        action["linked"] = str(linked_gtt_id)
    elif target is not None and normalise_gtt_id(position.get("kite_gtt_id")) == normalise_gtt_id(target):
        unlink_gtt(conn, position["id"], position["kite_gtt_id"])   # a HUMAN decision, recorded (R2-6)
        action["unlinked"] = str(target)
    if outcome == "no_app_gtt":
        action["late_match_watch_days"] = config.LATE_MATCH_WATCH_DAYS
    op = transition(conn, op, RESOLVED_MANUAL, from_states=(NEEDS_REVIEW,), sets={"resolution_id": res_id},
                    evidence={"resolution": outcome, "read_id": evidence.read_id, "resolver": resolver,
                              "action": action}, now=now,
                    event_detail={"resolution_id": res_id, "outcome": outcome, "action": action})
    cleanup = None
    if outcome == "linked_gtt" and flat:
        # R2-6, flat + linked_gtt: record the id, then an automatic position_closed cancel (`none`: no shares).
        g = evidence.gtt(linked_gtt_id)
        q0 = sum(l.quantity or 0 for l in g.sell_legs())
        dx = classify(kind=CANCEL, reason=R_POSITION_CLOSED, q0=q0, q1=0, open_qty=0, loosen=False)
        cleanup = claim(conn, position_id=position["id"], kind=CANCEL, reason=R_POSITION_CLOSED, request={},
                        target_gtt_id=linked_gtt_id, automatic=True, dx=dx, evidence=evidence, now=now)
    protection = refresh_protection_state(conn, position["id"], evidence, now=now)
    if outcome != "linked_gtt" and not flat and protection["state"] == "PROTECTION_PENDING":
        # R2-6: once a human took part, a missing stop is FAILED (PENDING would invite an automatic create).
        conn.execute("UPDATE trade_tickets SET protection_state = 'PROTECTION_FAILED' WHERE id = ?", (position["id"],))
        protection = {**protection, "state": "PROTECTION_FAILED", "reason": "MANUAL_NO_GTT"}
    return {"op": op, "resolution_id": res_id, "protection": protection, "action": action, "flat": flat,
            "cleanup_op": cleanup}
