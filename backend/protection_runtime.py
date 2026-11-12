"""Phase 3 S5/S8 (DARK): the runtime gates every protection send passes, DD-9, and journal-aware recovery.

S5 part (send gates):
- send_gate(): inside transaction A of every send (row 5/16). DD-8 freshness for every class; the M-6 gate on the
  SHARED RESUMED_AFTER_SUSPEND flag (R2-7 point 3): after a resume, only a `none`-effect send with a post-resume
  read is allowed until a Stage 1 run that started after the resume passes (instance_lock.clear_resume).
- pre_call_gate(): inside transaction B, immediately before the broker call (R2-7 point 8).

S8 part: see the section markers below.
"""

from __future__ import annotations

import time
from datetime import datetime

from backend import instance_lock
from backend.instance_lock import RESUMED_AFTER_SUSPEND
from backend.protection_evidence import Evidence, freshness_problem, takeover_at

DX_NONE = "none"


def send_gate(conn, dx_cls: str, evidence: Evidence, *, now: datetime, monotonic=time.monotonic) -> str | None:
    """None = the send may proceed; otherwise the refusal code. Runs inside the fenced transaction A."""
    gap = clock_gap(now=now, monotonic=monotonic)
    if gap is not None:
        return gap
    problem = freshness_problem(evidence, now=now, takeover=takeover_at(conn), epoch=instance_lock.owner_epoch())
    if problem is not None:
        return problem
    if RESUMED_AFTER_SUSPEND in instance_lock.process_reasons() and dx_cls != DX_NONE:
        return RESUMED_AFTER_SUSPEND
    return None


def pre_call_gate(*, now: datetime, monotonic=time.monotonic) -> str | None:
    """Immediately before the broker call, inside the fenced transaction B (whose epoch check already ran)."""
    if not instance_lock.authority_ok():
        return instance_lock.refusal_code() or "NO_AUTHORITY"
    return clock_gap(now=now, monotonic=monotonic)


# ==== S8: DD-9 and journal-aware recovery ====================================================================

DD9_ACCOUNT_REASONS = frozenset({
    "INSTANCE_NOT_OWNER", "INSTANCE_LOCK_LOST", "INSTANCE_CONFLICT", "ANOTHER_INSTANCE_RUNNING",
    "DATABASE_NOT_ON_LOCAL_DRIVE", "HEARTBEAT_STALE", RESUMED_AFTER_SUSPEND, "NO_SESSION", "PROXY_UNAVAILABLE",
    "BROKER_READ_FAILED", "BROKER_READ_STALE", "BROKER_DATA_INCONSISTENT", "LATE_GTT_APPEARED",
})
# Deliberately NOT refused by DD-9: the conditions the engine exists to fix.
DD9_EXCLUDED = frozenset({"UNRESOLVED_OPERATION", "PROTECTION_GAP", "CLEANUP_REQUIRED", "RECONCILIATION_REQUIRED",
                          "STARTUP_RECONCILIATION_INCOMPLETE"})


def may_increase_exposure(conn, position_id: int, evidence: Evidence, *, now: datetime,
                          monotonic=time.monotonic, attribution=None) -> tuple[bool, list[str]]:
    """DD-9: required for every increases_sell_exposure claim and send. Returns (ok, failed conditions)."""
    from backend import protection, startup_reconciliation
    failed = []
    position = protection.get_position(conn, position_id)
    # 1. the owner, with a current epoch (the fenced CAS re-proves it under the write lock)
    if not instance_lock.authority_ok():
        failed.append("1_NOT_OWNER")
    epoch = instance_lock.owner_epoch()
    # 2. a Stage 1 run passed in this epoch, started after the last login and after the last resume
    passed = conn.execute("SELECT started_at FROM startup_reconciliation_runs WHERE account_id = ? AND instance_epoch = ? "
                          "AND stage1_outcome = 'passed' ORDER BY id DESC LIMIT 1",
                          (position["account_id"], epoch)).fetchone() if epoch is not None else None
    passed_at = startup_reconciliation._parse(passed["started_at"]) if passed is not None else None
    login_at = startup_reconciliation._latest_login(conn, position["account_id"])
    resumed = instance_lock.resumed_at()
    if passed_at is None or (login_at and passed_at < login_at) or (resumed and passed_at < resumed):
        failed.append("2_STAGE1_NOT_PASSED_AFTER_LOGIN_AND_RESUME")
    # 3. a complete read satisfying DD-8
    if freshness_problem(evidence, now=now, takeover=takeover_at(conn), epoch=epoch) is not None:
        failed.append("3_NO_FRESH_COMPLETE_READ")
    # 4. none of the account reasons that say "the app cannot see the broker reliably"
    reasons = set(startup_reconciliation.derive_restriction(conn, position["account_id"], now=now)["reasons"])
    late = conn.execute("SELECT 1 FROM reconciliation_log WHERE resolved = 0 AND issue_code = 'LATE_GTT_APPEARED' "
                        "LIMIT 1").fetchone()
    if late is not None:
        reasons.add("LATE_GTT_APPEARED")
    blocking = sorted((reasons & DD9_ACCOUNT_REASONS) - DD9_EXCLUDED)
    if blocking:
        failed.append("4_ACCOUNT_REASONS:" + ",".join(blocking))
    # 5. attribution exclusive, or shared (create up to the app quantity), per D§6.3
    state = attribution(evidence) if attribution is not None else _attribution_state(conn, position, evidence)
    if state not in ("exclusive", "shared"):
        failed.append(f"5_ATTRIBUTION_{str(state).upper()}")
    # 6. the M-6 clock-gap check
    if clock_gap(now=now, monotonic=monotonic) is not None:
        failed.append("6_CLOCK_GAP")
    # 7. the P§5.3 "provably not created" evidence (M-5) for a create
    if not position.get("kite_gtt_id"):
        problem = protection.create_claim_problem(conn, position, protection.R_FILL, evidence, now=now)
        if problem not in (None, "M5_OPEN_OP_EXISTS"):
            failed.append(f"7_{problem}")
    return (not failed), failed


def _attribution_state(conn, position, evidence: Evidence) -> str:
    from backend import startup_reconciliation
    if evidence is None or evidence.raw is None or evidence.held is None:
        return "unknown"
    try:
        report = startup_reconciliation.evaluate_attribution(conn, position["account_id"], evidence.raw)
    except Exception:  # noqa: BLE001
        return "unknown"
    key = f"{str(position['exchange']).upper()}:{str(position['symbol']).upper()}"
    states = [r["state"] for r in report if r["instrument"].upper() == key]
    return states[0] if len(states) == 1 else "unknown"


def recover_journal(conn, account_id: int, *, now: datetime) -> dict:
    """Journal-aware recovery after a takeover (rows 3 and 10; M-5): an `intent` claimed by an older epoch never
    left that process -> not_sent; a `sending` of an older epoch may have been processed -> unknown (settle count
    reset, never re-sent); an Emergency Exit ticket of an older epoch that was never submitted -> cancelled (it
    owns the position but nothing was sent). Anything submitted (`submitting`/`unknown`) is left for the existing
    UNRESOLVED_OPERATION path. Writes nothing from a broker read; idempotent."""
    import json
    from backend import protection
    epoch = instance_lock.owner_epoch()
    out = {"intent_to_not_sent": [], "sending_to_unknown": [], "ee_tickets_released": []}
    with protection.journal_transaction(conn):
        for row in conn.execute("SELECT * FROM protection_operations WHERE account_id = ? AND state IN ('intent', 'sending') "
                                "ORDER BY id", (account_id,)).fetchall():
            op = dict(row)
            send_epoch = None
            for entry in protection.evidence_entries(op):
                if isinstance(entry, dict) and "send" in entry:
                    send_epoch = entry.get("epoch")
            if op["state"] == "intent" and op["instance_epoch"] != epoch:
                protection.transition(conn, op, "not_sent", from_states=("intent",),
                                      evidence={"recovered": "intent_of_older_epoch"}, now=now,
                                      event_detail={"recovered": True})
                out["intent_to_not_sent"].append(op["id"])
            elif op["state"] == "sending" and (send_epoch if send_epoch is not None else op["instance_epoch"]) != epoch:
                protection.transition(conn, op, "unknown", from_states=("sending",), attempt=op["attempts"],
                                      sets={"complete_reads_after": 0},
                                      evidence={"recovered": "sending_of_older_epoch", "unknown_at": now.isoformat(),
                                                "settle_reset": True}, now=now, event_detail={"recovered": True})
                out["sending_to_unknown"].append(op["id"])
        for row in conn.execute("SELECT id, risk_check_results FROM trade_tickets WHERE account_id = ? AND status = 'confirmed' "
                                "AND closes_ticket_id IS NOT NULL AND submission_state IS NULL AND kite_order_id IS NULL",
                                (account_id,)).fetchall():
            try:
                mark = (json.loads(row["risk_check_results"] or "{}") or {}).get("ee_engine")
            except (TypeError, ValueError):
                mark = None
            if isinstance(mark, dict) and mark.get("epoch") != epoch:
                conn.execute("UPDATE trade_tickets SET status = 'cancelled', rationale = rationale || ' — released by "
                             "recovery: never submitted' WHERE id = ? AND status = 'confirmed' AND submission_state IS NULL",
                             (row["id"],))
                out["ee_tickets_released"].append(row["id"])
    return out


def clock_gap(*, now: datetime, monotonic=time.monotonic) -> str | None:
    """M-6: the synchronous clock-gap check on the shared heartbeat baseline (S8). A gap runs the in-memory resume
    path exactly once and refuses this send."""
    return RESUMED_AFTER_SUSPEND if instance_lock.clock_gap_check(now=lambda: now, monotonic=monotonic)["gap"] else None
