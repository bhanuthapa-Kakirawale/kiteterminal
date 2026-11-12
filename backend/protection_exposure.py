"""Phase 3 S6 (DARK): the instrument-wide set of potentially active sell sources 𝒢, G_max and S_agg.

docs/PHASE_3_DESIGN_DECISIONS.md M-7: R3-2 (membership, deduplication), R4 (w(m)), R5-1 (id-less create),
R5-2/R6 (condition (e) over T(m); exposure over O(m)), R3-3 (the removal conditions (a)-(e)).

    S_agg = Σ w(m) over 𝒢,   w(m) = max{ L(v) : v ∈ V(m) } + Σ{ r(o) : o ∈ O(m) }

- Identity is the canonical broker GTT id (per account and mode). Different ids are never merged; an id-less create
  is keyed op:<op_id> and never merged; an ambiguous identity makes the evaluation fail closed.
- L(v) sums EVERY potentially executable SELL leg of a version (OCO is never assumed). Alternative versions of one
  GTT (listed, create payload, old and requested modify) are combined by MAXIMUM, never summed.
- O(m) ⊆ T(m): T(m) is every triggered SELL order of m (open, partial, terminal, cancelled, rejected) plus every
  order the evidence says should exist but is not visible (B-13). Only O(m) feeds exposure; T(m) decides removal.
- A member leaves 𝒢 only by (a) an explicit deleted/cancelled listing, (b) a confirmed cancel not contradicted by
  the read, (c) a deleted_in_kite_verified resolution not contradicted, (d) an id-less create that ended with no GTT
  (it is then simply not a member), or (e) rev. 6 for a triggered GTT. Nothing else removes a member.
- Any ambiguity in identity, version, leg quantity, order status, remaining quantity or completeness makes
  S_agg UNKNOWN (None). S_agg is never capped at holdings or at the open quantity.

Read-only. evaluate() writes nothing; the Emergency Exit engine (S7) raises any finding it reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend import db, protection
from backend.protection import CANCEL, CONFIRMED, CREATE, MODIFY, OPEN_STATES, RESOLVED_MANUAL
from backend.protection_evidence import (
    GTT_REMOVED_STATUSES, GTT_TRIGGERED, ORDER_TERMINAL, ORDER_WORKING, Evidence, normalise_gtt_id,
)

SOURCES = {
    1: "linked protective GTT of an open position",
    2: "GTT create result (any op state), late-response id or resolution link",
    3: "open modify target",
    4: "open cancel target",
    5: "target/result of an unknown, needs_review or superseded op",
    6: "broker-listed SELL GTT on the instrument not explicitly removed",
    7: "unknown create outcome (id unknowable)",
    8: "triggered GTT whose triggered-order set is not proven complete and terminal",
    9: "other local evidence (closed position's link, findings)",
}
_OPEN_CHANGE_STATES = ("sending", "unknown", "needs_review", "acknowledged")


@dataclass
class Member:
    key: str                               # canonical GTT id, or "op:<op_id>"
    sources: set = field(default_factory=set)
    op_ids: set = field(default_factory=set)
    cancellable: bool = False              # app-proven per M-7 step 3 (linked / acknowledged result / modify target)
    w: int | None = None                   # None = UNKNOWN
    versions: list = field(default_factory=list)
    outstanding: dict = field(default_factory=dict)      # O(m): order id (or "expected:<leg>") -> r(o)
    triggered_set: list = field(default_factory=list)    # T(m) as established
    t_complete: bool = False
    removed_by: str | None = None
    listed_status: str | None = None
    notes: list = field(default_factory=list)

    def report(self) -> dict:
        return {"member": self.key, "sources": sorted(self.sources), "w": self.w, "listed_status": self.listed_status,
                "cancellable": self.cancellable, "op_ids": sorted(self.op_ids), "outstanding": self.outstanding,
                "t_complete": self.t_complete, "notes": self.notes}


@dataclass
class Exposure:
    exchange: str
    tradingsymbol: str
    mode: str
    established: bool
    members: list
    removed: list
    reasons: list
    contradictions: list

    @property
    def g_max(self) -> int:
        return len(self.members)

    @property
    def s_agg(self) -> int | None:
        if any(m.w is None for m in self.members):
            return None
        return sum(m.w for m in self.members)

    def gate_open(self) -> bool:
        """The sale gate's journal part: 𝒢 established, G_max = 0 AND S_agg = 0 (both, so a defect in either
        calculation is caught)."""
        return self.established and self.g_max == 0 and self.s_agg == 0

    def report(self) -> dict:
        return {"instrument": f"{self.exchange}:{self.tradingsymbol}", "mode": self.mode,
                "established": self.established, "g_max": self.g_max, "s_agg": self.s_agg,
                "members": [m.report() for m in self.members], "removed": self.removed, "reasons": self.reasons,
                "contradictions": self.contradictions}


# ---- versions and legs ---------------------------------------------------------------------------------------

def payload_sell_total(p: dict, *, exchange: str, tradingsymbol: str) -> int | None:
    """L(v) of a payload version; None (UNKNOWN) on the R4 case 2 fail-closed list, including another instrument."""
    if str(p.get("exchange", "")).upper() != exchange or str(p.get("tradingsymbol", "")).upper() != tradingsymbol:
        return None
    return protection.sell_quantity_of_payload(p)


def _order_remaining(order) -> int | None:
    if order.quantity is None or order.filled_quantity is None:
        return None
    return max(0, order.quantity - order.filled_quantity)


def _order_terminal_zero(order) -> bool:
    """Items 2-3 of (e): terminal, and nothing left that can fill; any unfilled quantity must be EXPLICITLY reported
    cancelled or rejected by the broker (its status), never inferred."""
    if order.status not in ORDER_TERMINAL or order.quantity is None or order.filled_quantity is None:
        return False
    if order.pending_quantity not in (None, 0):
        return False
    if order.filled_quantity >= order.quantity:
        return True
    return order.status in ("CANCELLED", "REJECTED")


def listed_terms(g, evidence: Evidence, *, removed: bool = False) -> dict:
    """For a listed GTT: L(listed version) over its potentially executable SELL legs, O(m) and T(m).
    removed=True: the GTT is listed explicitly deleted/cancelled, which proves its UNTRIGGERED legs cannot execute
    ((e) item 4); every order it already triggered is still evaluated (items 1-3, 5, 6 still apply)."""
    out = {"L": 0, "O": {}, "T": [], "t_complete": True, "unknown": False, "notes": [], "all_legs_dead": True}
    if any(l.transaction_type not in ("SELL", "BUY") for l in g.legs):
        out["unknown"] = True                        # a leg whose side cannot be read could be a SELL
        out["notes"].append("LEG_SIDE_UNKNOWN")
    sell = g.sell_legs()
    any_trigger_info = any(l.triggered for l in g.legs)
    if g.status == GTT_TRIGGERED and not any_trigger_info:
        # Which leg fired cannot be established: every SELL leg counts, T(m) is not complete (R4, R6).
        out["notes"].append("TRIGGERED_LEG_UNKNOWN")
        out["t_complete"] = False
        out["all_legs_dead"] = False
        for l in sell:
            if l.quantity is None:
                out["unknown"] = True
            else:
                out["L"] += l.quantity
        return out
    for i, l in enumerate(sell):
        if l.quantity is None:
            out["unknown"] = True
            out["all_legs_dead"] = False
            continue
        if not l.triggered:
            if removed:
                continue                           # proven dead by the explicit deleted/cancelled status
            out["L"] += l.quantity                 # potentially executable; never assumed exclusive with another
            out["all_legs_dead"] = False
            continue
        order = evidence.orders.get(l.order_id) if l.order_id else None
        if order is None:
            # A leg evidenced as triggered whose order is not visible (or not placed): expected member of T(m),
            # counted ONCE at its full leg quantity (B-13).
            out["O"][f"expected:{i}:{l.order_id or 'none'}"] = l.quantity
            out["T"].append({"leg": i, "order_id": l.order_id, "visible": False, "placement": l.placement})
            out["t_complete"] = False
            out["all_legs_dead"] = False
            continue
        out["T"].append({"leg": i, "order_id": order.id, "visible": True, "status": order.status})
        if _order_terminal_zero(order):
            continue                               # in T(m), not in O(m): contributes 0
        remaining = _order_remaining(order)
        out["all_legs_dead"] = False
        if remaining is None or (order.status not in ORDER_TERMINAL and order.status not in ORDER_WORKING):
            out["unknown"] = True                    # B-32: an unknown order status is never terminal
            out["notes"].append(f"ORDER_STATUS_OR_QUANTITY_UNKNOWN:{order.id}")
        out["O"][order.id] = remaining
    return out


def fills_reconciled(conn, position_ids: list[int], orders: list) -> bool:
    """(e) item 5: every fill of every order in T(m) is recorded on an app exit with that order id and that filled
    quantity. An order with no fill needs nothing."""
    for order in orders:
        if not order.filled_quantity:
            continue
        row = conn.execute(
            "SELECT filled_quantity, status FROM trade_tickets WHERE kite_order_id = ? AND closes_ticket_id IS NOT NULL",
            (order.id,)).fetchone()
        if row is None or row["status"] != "executed" or row["filled_quantity"] != order.filled_quantity:
            return False
        if position_ids and conn.execute("SELECT closes_ticket_id FROM trade_tickets WHERE kite_order_id = ?",
                                         (order.id,)).fetchone()[0] not in position_ids:
            return False
    return True


# ---- membership ------------------------------------------------------------------------------------------------

def _positions(conn, account_id: int, mode: str, exchange: str, symbol: str) -> tuple[list, list]:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM trade_tickets WHERE account_id = ? AND mode = ? AND UPPER(exchange) = ? AND UPPER(symbol) = ? "
        "AND transaction_type = 'BUY' AND closes_ticket_id IS NULL AND status = 'executed'",
        (account_id, mode, exchange, symbol))]
    open_, closed = [], []
    for p in rows:
        qty = protection.open_quantity(conn, p["id"])
        (closed if protection.broker_flat(p, qty) else open_).append(p)
    return open_, closed


def evaluate(conn, *, account_id: int, mode: str, exchange: str, tradingsymbol: str, evidence: Evidence | None,
             established_problem: str | None = None) -> Exposure:
    """𝒢 for one instrument from the journal and ONE read (R_EE for Emergency Exit). Read-only.
    `established_problem` lets the caller (S7) refuse establishment (no usable R_EE: stale, early, wrong purpose)."""
    exchange, tradingsymbol = str(exchange).upper(), str(tradingsymbol).upper()
    reasons, contradictions = [], []
    members: dict[str, Member] = {}

    def add(key, source: int, *, op_id=None, cancellable=False, note=None):
        m = members.setdefault(key, Member(key))
        m.sources.add(source)
        if op_id is not None:
            m.op_ids.add(op_id)
        m.cancellable = m.cancellable or cancellable
        if note:
            m.notes.append(note)
        return m

    open_pos, closed_pos = _positions(conn, account_id, mode, exchange, tradingsymbol)
    position_ids = [p["id"] for p in open_pos + closed_pos]
    linked_counts: dict[str, int] = {}
    for p in open_pos:
        gid = normalise_gtt_id(p.get("kite_gtt_id"))
        if p.get("kite_gtt_id") is not None and gid is None:
            reasons.append(f"MALFORMED_LINKED_ID:{p['id']}")
        if gid is not None:
            add(gid, 1, cancellable=True)
            linked_counts[gid] = linked_counts.get(gid, 0) + 1
    for p in closed_pos:
        gid = normalise_gtt_id(p.get("kite_gtt_id"))
        if gid is not None and not protection.cleanup_discharged(conn, p["id"], gid, evidence):
            add(gid, 9, note=f"linked to closed position {p['id']}")
    dup = conn.execute("SELECT kite_gtt_id FROM trade_tickets WHERE kite_gtt_id IS NOT NULL GROUP BY kite_gtt_id "
                       "HAVING COUNT(*) > 1").fetchall()
    for r in dup:
        gid = normalise_gtt_id(r[0])
        if gid in members:
            reasons.append(f"AMBIGUOUS_ID_LINKED_TWICE:{gid}")

    ops = [dict(r) for r in conn.execute(
        f"SELECT * FROM protection_operations WHERE position_id IN ({','.join('?' * len(position_ids)) or 'NULL'})",
        position_ids)] if position_ids else []
    for op in ops:
        rid, tid = normalise_gtt_id(op["result_gtt_id"]), normalise_gtt_id(op["target_gtt_id"])
        if op["result_gtt_id"] is not None and rid is None:
            reasons.append(f"MALFORMED_RESULT_ID:op{op['id']}")
        if rid is not None:
            acknowledged = any(isinstance(e, dict) and e.get("response") == "acknowledged" for e in
                               protection.evidence_entries(op))
            add(rid, 2, op_id=op["id"], cancellable=acknowledged and op["state"] != protection.ADOPTED)
        late_ids = {normalise_gtt_id(e.get("result_gtt_id")) for e in protection.evidence_entries(op)
                    if isinstance(e, dict) and e.get("response") == "acknowledged" and e.get("result_gtt_id")}
        late_ids.discard(None)
        for lid in late_ids:
            add(lid, 2, op_id=op["id"])
        if rid is not None and late_ids - {rid}:
            reasons.append(f"AMBIGUOUS_CONFLICTING_IDS:op{op['id']}")
            contradictions.append({"op_id": op["id"], "ids": sorted(late_ids | {rid})})
        if op["state"] == RESOLVED_MANUAL:
            res = protection._resolution(conn, op["id"])
            if res is not None and res["linked_gtt_id"] is not None:
                add(normalise_gtt_id(res["linked_gtt_id"]), 2, op_id=op["id"])
        is_open = op["state"] in OPEN_STATES
        if op["kind"] == MODIFY and is_open and tid is not None:
            add(tid, 3, op_id=op["id"], cancellable=True)
        if op["kind"] == CANCEL and is_open and tid is not None:
            add(tid, 4, op_id=op["id"])
        if op["state"] in ("unknown", "needs_review") or op["superseded_at"] is not None:
            for gid in (tid, rid):
                if gid is not None:
                    add(gid, 5, op_id=op["id"])
        if op["kind"] == CREATE and op["state"] in ("sending", "unknown", "needs_review") and rid is None:
            m = add(f"op:{op['id']}", 7, op_id=op["id"])
            m.notes.append("R5-1: number and identity of resulting GTTs unknown; payload is per-GTT only")

    listed = []
    if evidence is not None:
        listed = [g for g in evidence.gtts.values()
                  if g.instrument() == (exchange, tradingsymbol) and (g.has_sell() or any(l.transaction_type is None for l in g.legs))]
        for g in listed:
            if g.status not in GTT_REMOVED_STATUSES:
                add(g.id, 6)
        if evidence.duplicate_ids:
            reasons.append(f"AMBIGUOUS_LISTING_DUPLICATE_IDS:{list(evidence.duplicate_ids)}")
    for r in conn.execute("SELECT DISTINCT gtt_id FROM reconciliation_log WHERE resolved = 0 AND gtt_id IS NOT NULL "
                          "AND UPPER(COALESCE(symbol, '')) = ?", (tradingsymbol,)).fetchall():
        gid = normalise_gtt_id(r[0])
        if gid is not None:
            add(gid, 9, note="unresolved finding")

    # ---- removal (a)-(e) and w(m) -------------------------------------------------------------------------
    removed = []
    final = []
    for key, m in members.items():
        if key.startswith("op:"):
            m.w = None                                   # R5-1: UNKNOWN, never the payload sum, never a bound
            final.append(m)
            continue
        g = evidence.gtt(key) if evidence is not None else None
        m.listed_status = g.status if g is not None else None
        member_ops = [o for o in ops if key in (normalise_gtt_id(o["result_gtt_id"]), normalise_gtt_id(o["target_gtt_id"]))]
        cancel_confirmed = any(o["kind"] == CANCEL and o["state"] == CONFIRMED and normalise_gtt_id(o["target_gtt_id"]) == key
                               for o in member_ops)
        verified_deleted = False
        for o in member_ops:
            if o["state"] == RESOLVED_MANUAL:
                res = protection._resolution(conn, o["id"])
                if res is not None and res["outcome"] == "deleted_in_kite_verified":
                    verified_deleted = True
        other_status = g is not None and g.status not in GTT_REMOVED_STATUSES
        if g is not None and g.status in GTT_REMOVED_STATUSES:
            if any(l.triggered for l in g.legs):
                # Deleted AFTER it triggered: deleting a GTT does not cancel an order it already placed. (e) item 4:
                # items 1-3, 5 and 6 still apply to every order already triggered.
                terms = listed_terms(g, evidence, removed=True)
                visible = [evidence.orders[t["order_id"]] for t in terms["T"] if t.get("visible")]
                if (terms["t_complete"] and not terms["O"] and not terms["unknown"]
                        and all(_order_terminal_zero(o) for o in visible)
                        and fills_reconciled(conn, position_ids, visible)):
                    m.removed_by = "a"
                else:
                    m.outstanding, m.triggered_set, m.t_complete = terms["O"], terms["T"], terms["t_complete"]
                    o_values = list(terms["O"].values())
                    m.w = None if terms["unknown"] or any(v is None for v in o_values) else sum(o_values)
                    m.notes.append("deleted after trigger: its triggered orders still apply ((e) item 4)")
                    m.sources.add(8)
                    final.append(m)
                    continue
            else:
                m.removed_by = "a"
        elif cancel_confirmed and not other_status:
            m.removed_by = "b"
        elif verified_deleted and not other_status:
            m.removed_by = "c"
        if cancel_confirmed and other_status:
            contradictions.append({"member": key, "code": protection.CONFIRMED_CANCEL_CONTRADICTED,
                                   "listed_status": g.status})
            m.notes.append("D/G2: confirmed cancel contradicted by the read")
        if m.removed_by:
            removed.append({"member": key, "by": m.removed_by, "sources": sorted(m.sources)})
            continue

        # V(m): the listed version, every create payload with this id, and old/requested of unresolved modifies.
        unknown = False
        version_totals = []
        terms = None
        if g is not None:
            if not g.status_known:
                unknown = True
                m.notes.append("B-32: unknown status")
            terms = listed_terms(g, evidence)
            unknown = unknown or terms["unknown"]
            version_totals.append(terms["L"])
            m.outstanding = terms["O"]
            m.triggered_set = terms["T"]
        for o in member_ops:
            p = protection.payload(o)
            if o["kind"] == CREATE and normalise_gtt_id(o["result_gtt_id"]) == key:
                total = payload_sell_total(p, exchange=exchange, tradingsymbol=tradingsymbol)
                if total is None:
                    unknown = True
                    m.notes.append(f"payload of op {o['id']} malformed")
                else:
                    version_totals.append(total)
            if o["kind"] == MODIFY and o["state"] in _OPEN_CHANGE_STATES and normalise_gtt_id(o["target_gtt_id"]) == key:
                requested = payload_sell_total(p, exchange=exchange, tradingsymbol=tradingsymbol)
                from backend.protection_send import last_prestate
                prestate = last_prestate(o)
                old = payload_sell_total(prestate, exchange=exchange, tradingsymbol=tradingsymbol) if prestate else None
                if requested is None or old is None:
                    unknown = True
                    m.notes.append(f"unresolved modify op {o['id']}: a version cannot be established")
                else:
                    version_totals.extend([old, requested])
        if not version_totals and not unknown:
            unknown = True
            m.notes.append("no version of this GTT can be established")
        o_values = list(m.outstanding.values())
        if unknown or any(v is None for v in o_values):
            m.w = None
        else:
            m.w = max(version_totals) + sum(o_values)

        # (e) rev. 6: a triggered member leaves 𝒢 only on complete, unambiguous evidence about T(m).
        if g is not None and g.status == GTT_TRIGGERED and terms is not None:
            unresolved_modify = any(o["kind"] == MODIFY and o["state"] in OPEN_STATES for o in member_ops)
            visible = [evidence.orders[t["order_id"]] for t in terms["T"] if t.get("visible")]
            m.t_complete = terms["t_complete"]
            e_holds = (terms["t_complete"] and terms["all_legs_dead"] and not terms["O"] and not terms["unknown"]
                       and all(_order_terminal_zero(o) for o in visible) and len(visible) == len(g.sell_legs())
                       and not unresolved_modify and fills_reconciled(conn, position_ids, visible))
            if e_holds:
                m.removed_by = "e"
                removed.append({"member": key, "by": "e", "sources": sorted(m.sources)})
                continue
            m.sources.add(8)
        final.append(m)

    established = evidence is not None and evidence.complete and evidence.read_id is not None and not established_problem
    if evidence is None:
        reasons.append("NO_READ")
    elif not evidence.complete:
        reasons.append("READ_INCOMPLETE")
    if established_problem:
        reasons.append(established_problem)
    if any(r.startswith(("AMBIGUOUS", "MALFORMED")) for r in reasons):
        established = False
    return Exposure(exchange, tradingsymbol, mode, established, sorted(final, key=lambda m: m.key), removed,
                    reasons, contradictions)
