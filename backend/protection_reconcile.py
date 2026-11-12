"""Phase 3 S6 (DARK): resolving protection ops from broker reads (R6 rows 9-18, 23; DD-8 settle; M-2; M-5).

reconcile_op() applies ONE complete read to ONE op inside a fenced journal transaction. It never concludes
anything from an incomplete read (§4.2 "never allowed": any transition driven by an incomplete read), never treats
"not listed" as "gone" (B-08/B-28), never auto-unlinks a linked GTT (M-5) and never auto-deletes a late GTT (M-2).

Settle (DD-8 rev. 2 / R2-5): a read R counts for an `unknown` create only if it is complete, shows no fingerprint
match and no unlinked active SELL GTT on the instrument, and
  (a) R.started_at >= T0 = max(sent_at + PROTECTION_SETTLE_SECONDS, unknown_at, reset_at), reset_at = the latest of
      the current takeover and the last resume (G4: an epoch change or a resume restarts the count);
  (b) the second counted read starts >= SETTLE_READ_SPACING_SECONDS after the first one's get_gtts COMPLETED
      (R2.started_at - R1.gtts_at), and within one process also on the monotonic clock.
Timestamps are compared as parsed instants. The counted reads and their timestamps are kept in `evidence`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from backend import config, instance_lock, protection
from backend.protection import (
    ABSENT_AFTER_SETTLE, ACKNOWLEDGED, ADOPTED, CANCEL, CONFIRMED, CREATE, MODIFY, NEEDS_REVIEW, SENDING, UNKNOWN,
)
from backend.protection_evidence import (
    GTT_ACTIVE, GTT_REMOVED_STATUSES, GTT_TRIGGERED, Evidence, iso, normalise_gtt_id, parse_time, takeover_at, utc_now,
)

logger = logging.getLogger(__name__)

assert config.PROTECTION_SETTLE_SECONDS >= config.PROTECTION_INFLIGHT_GRACE_SECONDS, "DD-8: SETTLE >= GRACE"


def _linked_ids(conn) -> set[str]:
    return {normalise_gtt_id(r[0]) for r in conn.execute("SELECT kite_gtt_id FROM trade_tickets WHERE kite_gtt_id IS NOT NULL")}


def unlinked_active_sells(conn, position: dict, evidence: Evidence) -> list:
    linked = _linked_ids(conn)
    return [g for g in evidence.sell_gtts_on(position["exchange"], position["symbol"])
            if g.id not in linked and g.status == GTT_ACTIVE]


def _settle_reads(op: dict) -> list[dict]:
    reads, basis = [], None
    for entry in protection.evidence_entries(op):
        if not isinstance(entry, dict):
            continue
        if entry.get("settle_reset"):
            reads = []
            basis = entry.get("basis")
        elif "settle_read" in entry:
            reads.append(entry)
    return reads


def _settle_basis(conn) -> dict:
    return {"epoch": instance_lock.owner_epoch(), "resume_seq": instance_lock.resume_seq()}


def _last_basis(op: dict) -> dict | None:
    basis = None
    for entry in protection.evidence_entries(op):
        if isinstance(entry, dict) and entry.get("settle_reset"):
            basis = entry.get("basis")
    return basis


def _unknown_at(op: dict) -> datetime | None:
    at = None
    for entry in protection.evidence_entries(op):
        if isinstance(entry, dict) and entry.get("to") == UNKNOWN:
            at = parse_time(entry.get("unknown_at") or entry.get("at"))
    return at


def settle_t0(conn, op: dict) -> datetime:
    sent = parse_time(op["sent_at"])
    candidates = [sent + timedelta(seconds=config.PROTECTION_SETTLE_SECONDS)]
    for extra in (_unknown_at(op), takeover_at(conn), instance_lock.resumed_at()):
        if extra is not None:
            candidates.append(extra)
    return max(candidates)


def settle_counts(conn, op: dict, evidence: Evidence) -> tuple[bool, str]:
    """Whether `evidence` may be counted as the next settle read of `op` (already shown clean)."""
    if evidence.read_id is None or not evidence.complete:
        return False, "READ_INCOMPLETE"
    if evidence.started_at < settle_t0(conn, op):
        return False, "BEFORE_T0"
    reads = _settle_reads(op)
    if any(r["settle_read"] == evidence.read_id for r in reads):
        return False, "ALREADY_COUNTED"
    if reads:
        last = reads[-1]
        last_gtts = parse_time(last.get("gtts_at")) or parse_time(last.get("started_at"))
        if (evidence.started_at - last_gtts).total_seconds() < config.SETTLE_READ_SPACING_SECONDS:
            return False, "SPACING_WALL"
        if last.get("epoch") == evidence.epoch and last.get("mono_gtts") is not None and evidence.started_mono is not None:
            if evidence.started_mono - last["mono_gtts"] < config.SETTLE_READ_SPACING_SECONDS:
                return False, "SPACING_MONOTONIC"
    return True, "COUNTED"


def _maybe_reset_settle(conn, op: dict, now: datetime) -> dict:
    """G4 / DD-10: a resume or an epoch change since the last settle basis restarts the count."""
    basis = _settle_basis(conn)
    if _last_basis(op) != basis:
        op = protection.annotate(conn, op, {"settle_reset": True, "basis": basis}, sets={"complete_reads_after": 0},
                                 expect_state=(UNKNOWN,), now=now)
    return op


# ---- the per-op reconciliation ----------------------------------------------------------------------------

def reconcile_op(conn, op_id: int, evidence: Evidence, *, now: datetime | None = None) -> dict:
    """One read applied to one op, inside its own journal transaction. Returns {'action', 'op'}."""
    now = now or utc_now()
    with protection.journal_transaction(conn):
        op = protection.get_op(conn, op_id)
        if op is None:
            return {"action": "OP_MISSING", "op": None}
        if not evidence.complete or evidence.read_id is None:
            return {"action": "READ_INCOMPLETE", "op": op}       # nothing is ever concluded from it
        state = op["state"]
        if state == SENDING:
            return _recover_sending(conn, op, now)
        if state == ACKNOWLEDGED:
            return _readback(conn, op, evidence, now)
        if state == UNKNOWN:
            if op["kind"] == CREATE:
                return _resolve_unknown_create(conn, op, evidence, now)
            return _resolve_unknown_target(conn, op, evidence, now)
        return {"action": "NOTHING", "op": op}


def _recover_sending(conn, op: dict, now: datetime) -> dict:
    """Row 9 (own epoch within the grace: unchanged) and row 10 (another epoch, or past the grace: unknown)."""
    sent = parse_time(op["sent_at"])
    own = op["instance_epoch"] == instance_lock.owner_epoch() and \
        _sending_epoch(op) == instance_lock.owner_epoch()
    if own and sent is not None and now - sent <= timedelta(seconds=config.PROTECTION_INFLIGHT_GRACE_SECONDS):
        return {"action": "IN_FLIGHT_GRACE", "op": op}
    op = protection.transition(conn, op, UNKNOWN, from_states=(SENDING,), attempt=op["attempts"],
                               sets={"complete_reads_after": 0},
                               evidence={"recovered": "older_epoch" if not own else "past_grace", "unknown_at": iso(now),
                                         "settle_reset": True, "basis": _settle_basis(conn)}, now=now,
                               event_detail={"recovered": True})
    return {"action": "SENDING_TO_UNKNOWN", "op": op}


def _sending_epoch(op: dict) -> int | None:
    epoch = None
    for entry in protection.evidence_entries(op):
        if isinstance(entry, dict) and "send" in entry:
            epoch = entry.get("epoch")
    return epoch


def _needs_review(conn, op: dict, why: str, now: datetime, frm: tuple) -> dict:
    op = protection.transition(conn, op, NEEDS_REVIEW, from_states=frm, evidence={"review": why}, now=now,
                               event_detail={"review": why})
    return {"action": f"NEEDS_REVIEW:{why}", "op": op}


def _readback(conn, op: dict, evidence: Evidence, now: datetime) -> dict:
    """Rows 11-13 (and 15b for a modify)."""
    position = protection.get_position(conn, op["position_id"])
    key = op["result_gtt_id"] if op["kind"] == CREATE else op["target_gtt_id"]
    g = evidence.gtt(key)
    request = protection.payload(op)
    if op["kind"] == CANCEL:
        if g is not None and g.status in GTT_REMOVED_STATUSES:
            return _confirm(conn, op, evidence, now, ACKNOWLEDGED)
        if g is not None and g.status == GTT_TRIGGERED:
            return _needs_review(conn, op, "CANCEL_TARGET_TRIGGERED", now, (ACKNOWLEDGED,))
        if g is not None and not g.status_known:
            return _needs_review(conn, op, "TARGET_STATUS_UNKNOWN", now, (ACKNOWLEDGED,))
        return {"action": "READBACK_PENDING", "op": op}           # active (G4) or missing (G5, B-28): continue
    if g is None:
        if op["kind"] == MODIFY:
            return _needs_review(conn, op, "MODIFY_TARGET_MISSING", now, (ACKNOWLEDGED,))
        return {"action": "READBACK_PENDING", "op": op}           # B-08 lag: never "absent"
    if op["kind"] == CREATE:
        elsewhere = conn.execute("SELECT id FROM trade_tickets WHERE kite_gtt_id = ? AND id != ?",
                                 (op["result_gtt_id"], position["id"])).fetchone()
        if elsewhere is not None and op["superseded_at"] is None:
            return _needs_review(conn, op, "ID_LINKED_ELSEWHERE", now, (ACKNOWLEDGED,))
    if g.status != GTT_ACTIVE:
        return _needs_review(conn, op, f"STATUS_{g.status or 'UNKNOWN'}", now, (ACKNOWLEDGED,))
    if g.instrument() != (str(position["exchange"]).upper(), str(position["symbol"]).upper()):
        return _needs_review(conn, op, "WRONG_INSTRUMENT", now, (ACKNOWLEDGED,))
    if any(l.transaction_type != "SELL" for l in g.legs):
        return _needs_review(conn, op, "BUY_LEG", now, (ACKNOWLEDGED,))
    if any(l.product != "CNC" for l in g.legs):
        return _needs_review(conn, op, "NON_CNC", now, (ACKNOWLEDGED,))
    if protection.matches_fingerprint(g, protection.fingerprint(request)):
        return _confirm(conn, op, evidence, now, ACKNOWLEDGED)
    return {"action": "READBACK_PENDING", "op": op}               # old values still listed (lag): keep reading


def _confirm(conn, op: dict, evidence: Evidence, now: datetime, frm: str) -> dict:
    op = protection.transition(conn, op, CONFIRMED, from_states=(frm,), evidence={"confirmed_by": evidence.read_id},
                               now=now)
    protection.refresh_protection_state(conn, op["position_id"], evidence, now=now)
    return {"action": "CONFIRMED", "op": op}


def _resolve_unknown_create(conn, op: dict, evidence: Evidence, now: datetime) -> dict:
    """Rows 14 (adopt), 18 (needs_review) and 17 (settle -> absent_after_settle)."""
    position = protection.get_position(conn, op["position_id"])
    fp = protection.fingerprint(protection.payload(op))
    unlinked = unlinked_active_sells(conn, position, evidence)
    matches = [g for g in unlinked if protection.matches_fingerprint(g, fp)]
    others = [g for g in unlinked if g not in matches]
    if len(matches) == 1 and not others:
        gid = matches[0].id
        sets = {"result_gtt_id": gid}
        if op["superseded_at"] is None:
            if not protection.link_gtt(conn, position["id"], gid):
                return _needs_review(conn, op, "ADOPTION_LINK_REFUSED", now, (UNKNOWN,))
        op = protection.transition(conn, op, ADOPTED, from_states=(UNKNOWN,), sets=sets,
                                   evidence={"adopted": gid, "read_id": evidence.read_id, "inference": "fingerprint"},
                                   now=now, event_detail={"gtt_id": gid})
        if op["superseded_at"] is not None:
            protection.resolve_superseded_result(conn, op, gid, app_proven=False, now=now)
        protection.refresh_protection_state(conn, position["id"], evidence, now=now)
        return {"action": "ADOPTED", "op": op}
    if matches or others:
        why = "MULTIPLE_FINGERPRINT_MATCHES" if len(matches) > 1 else "UNLINKED_SELL_GTT_ON_INSTRUMENT"
        out = _needs_review(conn, op, why, now, (UNKNOWN,))
        out["candidates"] = [g.id for g in unlinked]
        return out
    op = _maybe_reset_settle(conn, op, now)
    counts, why = settle_counts(conn, op, evidence)
    if not counts:
        return {"action": f"SETTLE_NOT_COUNTED:{why}", "op": op}
    n = op["complete_reads_after"] + 1
    entry = {"settle_read": evidence.read_id, "started_at": iso(evidence.started_at),
             "gtts_at": iso(evidence.gtts_at) if evidence.gtts_at else None, "epoch": evidence.epoch,
             "mono_started": evidence.started_mono, "mono_gtts": evidence.gtts_mono}
    if n >= config.SETTLE_MIN_COMPLETE_READS:
        op = protection.transition(conn, op, ABSENT_AFTER_SETTLE, from_states=(UNKNOWN,),
                                   sets={"complete_reads_after": n}, evidence=entry, now=now)
        protection.refresh_protection_state(conn, position["id"], evidence, now=now)
        return {"action": "ABSENT_AFTER_SETTLE", "op": op}
    op = protection.annotate(conn, op, entry, sets={"complete_reads_after": n}, expect_state=(UNKNOWN,), now=now)
    return {"action": f"SETTLE_COUNTED:{n}", "op": op}


def _resolve_unknown_target(conn, op: dict, evidence: Evidence, now: datetime) -> dict:
    """Rows 15, 15a, 15b for a modify or cancel whose outcome is unknown."""
    g = evidence.gtt(op["target_gtt_id"])
    if op["kind"] == CANCEL:
        if g is not None and g.status in GTT_REMOVED_STATUSES:
            return _confirm(conn, op, evidence, now, UNKNOWN)
        if g is not None and g.status == GTT_TRIGGERED:
            return _needs_review(conn, op, "CANCEL_TARGET_TRIGGERED", now, (UNKNOWN,))
        if g is not None and not g.status_known:
            return _needs_review(conn, op, "TARGET_STATUS_UNKNOWN", now, (UNKNOWN,))
        if g is not None:
            return {"action": "STILL_ACTIVE", "op": op}         # a retry may follow (M-3, row 16)
        op = _maybe_reset_settle(conn, op, now)
        counts, why = settle_counts(conn, op, evidence)
        if not counts:
            return {"action": f"MISSING_SETTLE_NOT_COUNTED:{why}", "op": op}
        n = op["complete_reads_after"] + 1
        entry = {"settle_read": evidence.read_id, "started_at": iso(evidence.started_at),
                 "gtts_at": iso(evidence.gtts_at) if evidence.gtts_at else None, "epoch": evidence.epoch,
                 "mono_started": evidence.started_mono, "mono_gtts": evidence.gtts_mono}
        if n >= config.SETTLE_MIN_COMPLETE_READS:
            op = protection.annotate(conn, op, entry, sets={"complete_reads_after": n}, expect_state=(UNKNOWN,), now=now)
            return _needs_review(conn, op, "CANCEL_TARGET_MISSING_LISTING_UNVERIFIED", now, (UNKNOWN,))
        op = protection.annotate(conn, op, entry, sets={"complete_reads_after": n}, expect_state=(UNKNOWN,), now=now)
        return {"action": f"MISSING_SETTLE_COUNTED:{n}", "op": op}
    # modify
    if g is None:
        return _needs_review(conn, op, "MODIFY_TARGET_MISSING", now, (UNKNOWN,))
    if g.status != GTT_ACTIVE:
        return _needs_review(conn, op, f"MODIFY_TARGET_{g.status or 'UNKNOWN'}", now, (UNKNOWN,))
    if protection.matches_fingerprint(g, protection.fingerprint(protection.payload(op))):
        return _confirm(conn, op, evidence, now, UNKNOWN)
    return {"action": "PRESTATE_OR_OTHER", "op": op}


# ---- whole-account helpers ----------------------------------------------------------------------------------

def reconcile_all(conn, evidence: Evidence, *, now: datetime | None = None) -> list[dict]:
    """Applies one read to every open op of the read's account (each op in its own transaction)."""
    ids = [r[0] for r in conn.execute(
        f"SELECT id FROM protection_operations WHERE account_id = ? AND state IN ({protection.db._sql_list(protection.OPEN_STATES)}) "
        "ORDER BY id", (evidence.account_id,))]
    return [reconcile_op(conn, op_id, evidence, now=now) | {"op_id": op_id} for op_id in ids]


def late_match_watch(conn, evidence: Evidence, *, now: datetime | None = None) -> list[dict]:
    """Row 23 and DD-7 rev. 2: a GTT appearing after absent_after_settle, or after a no_app_gtt resolution, within
    LATE_MATCH_WATCH_DAYS. Never auto-deletes (M-2): URGENT finding, the position PROTECTION_NEEDS_REVIEW, the
    terminal op unchanged. For a broker-flat position whose no_app_gtt id reappears ACTIVE, an automatic
    position_closed cancel is claimed (`none`)."""
    now = now or utc_now()
    if not evidence.complete:
        return []
    found = []
    since = iso(now - timedelta(days=config.LATE_MATCH_WATCH_DAYS))
    with protection.journal_transaction(conn):
        linked = _linked_ids(conn)
        for row in conn.execute("SELECT * FROM protection_operations WHERE state = ? AND resolved_at >= ? AND account_id = ?",
                                (ABSENT_AFTER_SETTLE, since, evidence.account_id)).fetchall():
            op = dict(row)
            if any(isinstance(e, dict) and e.get("late_gtt") for e in protection.evidence_entries(op)):
                continue
            fp = protection.fingerprint(protection.payload(op))
            hits = [g for g in evidence.gtts.values() if g.status == GTT_ACTIVE and g.id not in linked
                    and protection.matches_fingerprint(g, fp)]
            if hits:
                position = protection.get_position(conn, op["position_id"])
                protection.annotate(conn, op, {"late_gtt": [g.id for g in hits], "read_id": evidence.read_id}, now=now)
                protection.raise_finding(conn, position, protection.LATE_GTT_APPEARED,
                                         f"GTT(s) {[g.id for g in hits]} match op {op['id']} after absent_after_settle; "
                                         "never deleted automatically", gtt_id=hits[0].id)
                protection._event(conn, position["id"], protection.EV_LATE_GTT, {"op_id": op["id"], "gtt_ids": [g.id for g in hits]})
                conn.execute("UPDATE trade_tickets SET protection_state = 'PROTECTION_NEEDS_REVIEW' WHERE id = ?",
                             (position["id"],))
                found.append({"op_id": op["id"], "gtt_ids": [g.id for g in hits]})
        for row in conn.execute(
                "SELECT r.*, o.position_id, o.target_gtt_id, o.result_gtt_id, o.id AS oid, o.op_uuid FROM "
                "protection_operation_resolutions r JOIN protection_operations o ON o.id = r.op_id "
                "WHERE r.outcome = 'no_app_gtt' AND r.created_at >= ? AND o.account_id = ?",
                (since, evidence.account_id)).fetchall():
            gid = row["target_gtt_id"] if row["target_gtt_id"] is not None else row["result_gtt_id"]
            g = evidence.gtt(gid) if gid is not None else None
            if g is None or g.status != GTT_ACTIVE:
                continue
            op = protection.get_op(conn, row["oid"])
            if any(isinstance(e, dict) and e.get("late_gtt") for e in protection.evidence_entries(op)):
                continue
            position = protection.get_position(conn, row["position_id"])
            protection.annotate(conn, op, {"late_gtt": [g.id], "read_id": evidence.read_id}, now=now)
            protection.raise_finding(conn, position, protection.LATE_GTT_APPEARED,
                                     f"GTT {g.id} resolved no_app_gtt is listed ACTIVE", gtt_id=g.id)
            protection._event(conn, position["id"], protection.EV_LATE_GTT, {"op_id": op["id"], "gtt_ids": [g.id]})
            entry = {"op_id": op["id"], "gtt_ids": [g.id]}
            if protection.broker_flat(position, protection.open_quantity(conn, position["id"])):
                q0 = sum(l.quantity or 0 for l in g.sell_legs())
                dx = protection.classify(kind=CANCEL, reason=protection.R_POSITION_CLOSED, q0=q0, q1=0, open_qty=0,
                                         loosen=False)
                if not protection._open_cancel_exists(conn, position["id"], g.id):
                    entry["cancel_op"] = protection.claim(conn, position_id=position["id"], kind=CANCEL,
                                                          reason=protection.R_POSITION_CLOSED, request={}, target_gtt_id=g.id,
                                                          automatic=True, dx=dx, evidence=evidence, now=now)["id"]
            else:
                conn.execute("UPDATE trade_tickets SET protection_state = 'PROTECTION_NEEDS_REVIEW' WHERE id = ?",
                             (position["id"],))
            found.append(entry)
    return found


def m2_conditions(conn, op: dict, evidence: Evidence, *, now: datetime | None = None) -> dict:
    """M-2's four conditions for a duplicate_cleanup cancel, evaluated in ONE read (DD-8 fresh)."""
    from backend.protection_evidence import freshness_problem
    now = now or utc_now()
    position = protection.get_position(conn, op["position_id"])
    target = normalise_gtt_id(op["target_gtt_id"])
    proven = False
    for other in protection.ops_for_position(conn, position["id"]):
        if normalise_gtt_id(other["result_gtt_id"]) != target or other["state"] == ADOPTED:
            continue
        for entry in protection.evidence_entries(other):
            if isinstance(entry, dict) and entry.get("response") == "acknowledged" and \
                    normalise_gtt_id(entry.get("result_gtt_id")) == target:
                proven = True
    linked = position.get("kite_gtt_id")
    open_qty = protection.open_quantity(conn, position["id"]) or 0
    survivor = evidence.gtt(linked) if linked is not None else None
    fresh = freshness_problem(evidence, now=now, takeover=takeover_at(conn), epoch=instance_lock.owner_epoch()) is None
    survives = (fresh and survivor is not None and survivor.status == GTT_ACTIVE and
                protection.shape_problem(survivor, position, open_qty,
                                         protection.intended_shape(conn, position["id"], linked)) is None)
    changes = [o for o in protection.ops_for_position(conn, position["id"], open_only=True) if o["kind"] != CANCEL]
    return {
        "target_app_proven": proven,
        "different_linked_gtt": linked is not None and normalise_gtt_id(linked) != target,
        "survivor_protects_full_quantity": survives,
        "no_exit_or_change_open": not protection.exit_in_flight(conn, position["id"]) and not changes,
    }


_missing: dict[tuple, list] = {}


def linked_gtt_watch(conn, evidence: Evidence, *, now: datetime | None = None) -> list[dict]:
    """M-5: a linked GTT missing from complete reads is never unlinked and never re-created; after two counted
    complete reads spaced like the settle rule, an URGENT LINKED_GTT_MISSING finding. A linked GTT explicitly
    deleted/cancelled/expired by no app op: PROTECTION_FAILED and GTT_REMOVED_EXTERNALLY. (The missing-count is
    process memory: a restart only delays the finding.)"""
    now = now or utc_now()
    if not evidence.complete:
        return []
    out = []
    with protection.journal_transaction(conn):
        for row in conn.execute("SELECT id, kite_gtt_id, symbol, exchange FROM trade_tickets WHERE kite_gtt_id IS NOT NULL "
                                "AND account_id = ? AND closed_at IS NULL", (evidence.account_id,)).fetchall():
            pos = protection.get_position(conn, row["id"])
            key = (pos["id"], normalise_gtt_id(pos["kite_gtt_id"]))
            g = evidence.gtt(pos["kite_gtt_id"])
            if g is None:
                seen = _missing.setdefault(key, [])
                if not seen or (evidence.started_at - seen[-1]).total_seconds() >= config.SETTLE_READ_SPACING_SECONDS:
                    seen.append(evidence.gtts_at or evidence.started_at)
                if len(seen) == config.SETTLE_MIN_COMPLETE_READS:
                    protection.raise_finding(conn, pos, protection.LINKED_GTT_MISSING,
                                             f"linked GTT {pos['kite_gtt_id']} missing from complete reads; not unlinked, "
                                             "not re-created", gtt_id=pos["kite_gtt_id"])
                    out.append({"position_id": pos["id"], "finding": protection.LINKED_GTT_MISSING})
                continue
            _missing.pop(key, None)
            if g.status in GTT_REMOVED_STATUSES | {"expired"}:
                by_app = any(o["kind"] == CANCEL and normalise_gtt_id(o["target_gtt_id"]) == key[1]
                             for o in protection.ops_for_position(conn, pos["id"]))
                if not by_app:
                    protection.raise_finding(conn, pos, protection.GTT_REMOVED_EXTERNALLY,
                                             f"linked GTT {g.id} is {g.status}; re-create only by the user", gtt_id=g.id)
                    conn.execute("UPDATE trade_tickets SET protection_state = 'PROTECTION_FAILED' WHERE id = ?", (pos["id"],))
                    out.append({"position_id": pos["id"], "finding": protection.GTT_REMOVED_EXTERNALLY})
    return out
