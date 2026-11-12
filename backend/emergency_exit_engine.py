"""Phase 3 S7 (DARK): the Emergency Exit engine API (docs/PHASE_3_DESIGN_DECISIONS.md M-7, R2-3, R3-2, R3-3, R4,
R5-1, R5-2, R6). NOT wired into routes/kill_switch (Phase 6, a separate authorisation and a live blocker).

Emergency Exit never sells because a button was clicked. For one position it:

 1. refuses first on its own preconditions (open, quantity known, valid mode, no exit already in flight); a refused
    position is not superseded and nothing is claimed;
 2. in ONE fenced transaction claims the exit owner (an exit ticket: the existing idx_trade_tickets_one_exit_owner
    makes a second concurrent click fail), sets the supersede overlay on every open create/modify op, and claims an
    `exit_prepare` cancel for every app-proven id (linked, acknowledged result, open modify target) that has no
    open cancel (the per-target cancel index: a re-click sends no duplicate cancel);
 3. sends each claimed cancel ONCE (protection_send: fenced, write-ahead; never retried while the position is open);
 4. waits from T_last, the latest sent_at among the cancels sent in THIS click, until T_last + grace or until every
    relevant cancel is definite; no cancel sent -> no wait;
 5. takes exactly ONE fresh complete read R_EE (purpose emergency_exit) started at or after max(initiation, T_last)
    and satisfying DD-8 i-iii, reconciles the relevant cancels against it, and evaluates 𝒢 (protection_exposure);
 6. sells the open quantity only if 𝒢 is established, G_max = 0, S_agg = 0, attribution is exclusive or shared and
    the caller's E3 check passes. The SELL is write-ahead (`submitting` committed first) and sent inside a held
    fenced transaction; an ambiguous outcome is `unknown` and never re-sent. After every sale the
    POSSIBLE_DOUBLE_SELL watch is armed.

Otherwise NOTHING is sold: the result is `refused`, listing every member (GTT id or op id), its sources and its w,
with the instruction "delete GTT #... in Kite, or wait for the cancel to confirm, then run Emergency Exit again". The
exit ticket that never left the process is cancelled (ownership released). There is no "sell anyway" override and no
deferred or background sale (R3-3): this module has no such parameter, path or scheduler, and a static test says so.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from backend import config, instance_lock, paper_broker, protection, protection_exposure, protection_reconcile, protection_send
from backend.protection import CANCEL, CONFIRMED, MODIFY, OPEN_STATES, REJECTED
from backend.protection_evidence import Evidence, capture, freshness_problem, iso, normalise_gtt_id, takeover_at, utc_now

logger = logging.getLogger(__name__)

REFUSED, SOLD, SELL_UNKNOWN, SELL_REJECTED = "refused", "sell_submitted", "sell_outcome_unknown", "sell_rejected"
INSTRUCTION = "Delete GTT #{ids} in Kite, or wait for the cancel to confirm, then run Emergency Exit again."
POSSIBLE_DOUBLE_SELL_WATCH = "POSSIBLE_DOUBLE_SELL_WATCH"
_EE_MARK = "ee_engine"

_single_flight: dict[tuple, threading.Lock] = {}
_single_flight_guard = threading.Lock()


@dataclass
class EEResult:
    position_id: int
    outcome: str
    reason: str
    exit_ticket_id: int | None = None
    exposure: dict | None = None
    cancels: list = field(default_factory=list)
    instruction: str | None = None
    order_id: str | None = None
    r_ee_read_id: int | None = None
    waited_seconds: float = 0.0
    findings: list = field(default_factory=list)


def _instrument_lock(key: tuple) -> threading.Lock:
    with _single_flight_guard:
        return _single_flight.setdefault(key, threading.Lock())


def _refusal(position_id, reason, **kw) -> EEResult:
    logger.warning("event=emergency_exit.refused position=%s reason=%s", position_id, reason)
    return EEResult(position_id, REFUSED, reason, **kw)


# ---- step 2: the owner claim, the overlay, the cancel claims (one fenced transaction) ----------------------

def _claim_owner_and_cancels(conn, position: dict, open_qty: int, now: datetime) -> tuple[int, list[dict]]:
    with protection.journal_transaction(conn):
        if protection.exit_in_flight(conn, position["id"]):
            raise protection.ClaimRefused("EXIT_IN_FLIGHT")
        mark = json.dumps({_EE_MARK: {"epoch": instance_lock.owner_epoch(), "at": iso(now)}})
        exit_id = conn.execute(
            "INSERT INTO trade_tickets (account_id, symbol, exchange, instrument_token, transaction_type, quantity, "
            "entry_price, entry_order_type, stop_loss, target_1, product_type, rationale, status, mode, closes_ticket_id, "
            "risk_check_results) VALUES (?, ?, ?, ?, 'SELL', ?, ?, 'MARKET', ?, ?, 'CNC', 'Emergency Exit (engine)', "
            "'confirmed', ?, ?, ?)",
            (position["account_id"], position["symbol"], position["exchange"], position["instrument_token"], open_qty,
             position["entry_price"], position["stop_loss"], position["target_1"], position["mode"], position["id"],
             mark)).lastrowid
        protection.supersede_open_changes(conn, position["id"], "emergency_exit", str(exit_id), now=now)
        ids = []
        if position.get("kite_gtt_id") is not None:
            ids.append(position["kite_gtt_id"])
        for op in protection.ops_for_position(conn, position["id"]):
            acknowledged = any(isinstance(e, dict) and e.get("response") == "acknowledged"
                               for e in protection.evidence_entries(op))
            if op["result_gtt_id"] is not None and acknowledged and op["state"] != protection.ADOPTED:
                ids.append(op["result_gtt_id"])
            if op["kind"] == MODIFY and op["state"] in OPEN_STATES:
                ids.append(op["target_gtt_id"])
        claimed, seen = [], set()
        for gid in ids:
            key = normalise_gtt_id(gid)
            if key is None or key in seen:
                continue
            seen.add(key)
            done = any(o["kind"] == CANCEL and normalise_gtt_id(o["target_gtt_id"]) == key and o["state"] == CONFIRMED
                       for o in protection.ops_for_position(conn, position["id"]))
            if done or protection._open_cancel_exists(conn, position["id"], gid):
                continue                                     # step 4: no second op; no new grace
            claimed.append(protection.claim(conn, position_id=position["id"], kind=CANCEL,
                                            reason=protection.R_EXIT_PREPARE, request={}, target_gtt_id=gid,
                                            automatic=False, now=now))
    return exit_id, claimed


def _release_owner(conn, exit_id: int, why: str) -> None:
    """The exit ticket never left the process: cancel it so the position is not held by a dead exit owner."""
    with instance_lock.fenced_transaction(conn):
        conn.execute("UPDATE trade_tickets SET status = 'cancelled', rationale = rationale || ? WHERE id = ? "
                     "AND status = 'confirmed' AND kite_order_id IS NULL AND submission_state IS NULL",
                     (f" — refused: {why}", exit_id))


# ---- step 6: the SELL (write-ahead, fenced across the call) ------------------------------------------------

def _sell(conn, position: dict, exit_id: int, open_qty: int, kite, *, now: datetime, on_sell=None) -> tuple[str, str | None]:
    with instance_lock.fenced_transaction(conn):
        changed = conn.execute("UPDATE trade_tickets SET submission_state = 'submitting', submitted_at = ? WHERE id = ? "
                               "AND status = 'confirmed' AND submission_state IS NULL AND kite_order_id IS NULL",
                               (iso(now), exit_id)).rowcount
        if changed != 1:
            return "SELL_CLAIM_LOST", None
    from kiteconnect.exceptions import InputException, OrderException, PermissionException
    with instance_lock.fenced_transaction(conn):
        row = conn.execute("SELECT submission_state, quantity FROM trade_tickets WHERE id = ?", (exit_id,)).fetchone()
        if row is None or row["submission_state"] != "submitting":
            return "SELL_CLAIM_LOST", None
        broker = paper_broker.broker_for(position["mode"], kite, conn, position["account_id"])
        paper_broker.check_mode(broker, position["mode"])
        if on_sell is not None:
            on_sell()
        try:
            order_id = broker.place_order(variety="regular", exchange=position["exchange"],
                                          tradingsymbol=position["symbol"], transaction_type="SELL", quantity=row["quantity"],
                                          product="CNC", order_type="MARKET")
        except (OrderException, InputException, PermissionException) as e:
            if isinstance(e, instance_lock.InstanceNotOwner):
                raise
            conn.execute("UPDATE trade_tickets SET submission_state = 'rejected', status = 'failed' WHERE id = ?", (exit_id,))
            return SELL_REJECTED, None
        except Exception as e:  # noqa: BLE001 - ambiguous: maybe executed; never re-sent
            conn.execute("UPDATE trade_tickets SET submission_state = 'unknown' WHERE id = ?", (exit_id,))
            logger.critical("event=emergency_exit.sell_unknown exit=%s %s: %s", exit_id, type(e).__name__, e)
            return SELL_UNKNOWN, None
        conn.execute("UPDATE trade_tickets SET submission_state = 'acknowledged', kite_order_id = ?, status = 'executed', "
                     "entry_fill_status = 'pending', executed_at = ? WHERE id = ?", (str(order_id), iso(now), exit_id))
        protection.raise_finding(conn, position, POSSIBLE_DOUBLE_SELL_WATCH,
                                 f"Emergency Exit SELL {order_id} submitted; watching for any GTT order that may still "
                                 "sell the same shares", gtt_id=None)
        return SOLD, str(order_id)


# ---- the whole Emergency Exit for one position --------------------------------------------------------------

def emergency_exit(conn, position_id: int, kite, *, attribution=None, e3_check=None, clock=utc_now,
                   monotonic=time.monotonic, sleep=time.sleep, on_sell=None, after_cancels=None,
                   after_read=None) -> EEResult:
    """One Emergency Exit click for one position (see the module docstring). `attribution(evidence) -> state` and
    `e3_check(evidence) -> refusal or None` are the D§6.3 / E3 hooks (Phase 6/7 wiring); by default attribution is
    evaluated with startup_reconciliation.evaluate_attribution and no E3 refusal is added. `after_cancels` and
    `after_read` are test hooks (crash boundaries)."""
    initiated, initiated_mono = clock(), monotonic()
    position = protection.get_position(conn, position_id)
    if position is None:
        return _refusal(position_id, "POSITION_MISSING")
    if not paper_broker.is_valid_mode(position.get("mode")):
        return _refusal(position_id, "MODE_INVALID")
    open_qty = protection.open_quantity(conn, position_id)
    if open_qty is None or open_qty <= 0 or protection.broker_flat(position, open_qty):
        return _refusal(position_id, "NOT_OPEN_OR_QUANTITY_UNKNOWN")
    key = (position["account_id"], position["mode"], str(position["exchange"]).upper(), str(position["symbol"]).upper())
    lock = _instrument_lock(key)
    if not lock.acquire(blocking=False):
        return _refusal(position_id, "EMERGENCY_EXIT_ALREADY_RUNNING")
    try:
        try:
            exit_id, claimed = _claim_owner_and_cancels(conn, position, open_qty, initiated)
        except protection.ClaimRefused as e:
            return _refusal(position_id, e.code)
        except Exception as e:   # noqa: BLE001 - e.g. the exit-owner index: another click won
            if "UNIQUE" in str(e) or "constraint" in str(e).lower():
                return _refusal(position_id, "EXIT_IN_FLIGHT")
            if "database is locked" in str(e):
                return _refusal(position_id, "DATABASE_BUSY")   # another writer (e.g. a concurrent EE sale)
            raise
        result = EEResult(position_id, REFUSED, "", exit_ticket_id=exit_id)
        try:
            return _run(conn, position, open_qty, exit_id, claimed, kite, result, initiated=initiated,
                        initiated_mono=initiated_mono, attribution=attribution, e3_check=e3_check, clock=clock,
                        monotonic=monotonic, sleep=sleep, on_sell=on_sell, after_cancels=after_cancels,
                        after_read=after_read)
        except instance_lock.FencingError as e:
            result.outcome, result.reason = REFUSED, f"AUTHORITY_LOST: {e}"
            return result
    finally:
        lock.release()


def _run(conn, position, open_qty, exit_id, claimed, kite, result: EEResult, *, initiated, initiated_mono,
         attribution, e3_check, clock, monotonic, sleep, on_sell, after_cancels, after_read) -> EEResult:
    sent_mono = []
    if claimed:
        prewrite = capture(conn, kite, position["account_id"], "prewrite", clock=clock, monotonic=monotonic)
        for op in claimed:
            res = protection_send.send(conn, op["id"], kite, prewrite, now=clock, monotonic=monotonic)
            result.cancels.append({"op_id": op["id"], "target": op["target_gtt_id"], "sent": res.sent, "code": res.code})
            # T_last: every cancel whose `sending` was committed in this click (a send that was then not made only
            # lengthens the wait, which is the safe direction).
            for entry in protection.evidence_entries(protection.get_op(conn, op["id"])):
                if isinstance(entry, dict) and entry.get("send") and entry.get("mono") is not None:
                    sent_mono.append(entry["mono"])
    if after_cancels is not None:
        after_cancels()
    # step 4: the grace runs from T_last, the last relevant cancel SENT IN THIS CLICK; none sent -> no wait.
    relevant = [op["id"] for op in claimed]
    if sent_mono:
        t_last = max(sent_mono)
        deadline = t_last + config.PROTECTION_INFLIGHT_GRACE_SECONDS
        while monotonic() < deadline:
            states = [protection.get_op(conn, i)["state"] for i in relevant]
            if all(s in (CONFIRMED, REJECTED) for s in states):
                break
            sleep(min(0.25, max(0.0, deadline - monotonic())))
        result.waited_seconds = max(0.0, monotonic() - t_last)
    # step 5: exactly one fresh complete read R_EE.
    r_ee = capture(conn, kite, position["account_id"], "emergency_exit", clock=clock, monotonic=monotonic)
    result.r_ee_read_id = r_ee.read_id
    if after_read is not None:
        after_read()
    problem = freshness_problem(r_ee, now=clock(), takeover=takeover_at(conn), epoch=instance_lock.owner_epoch())
    if problem is None and r_ee.started_at < initiated:
        problem = "R_EE_STARTED_BEFORE_INITIATION"
    if problem is None and sent_mono and (r_ee.started_mono is None or r_ee.started_mono < max(sent_mono)):
        problem = "R_EE_STARTED_BEFORE_T_LAST"
    if r_ee.complete:
        for op in protection.ops_for_position(conn, position["id"], open_only=True):
            if op["kind"] == CANCEL:
                protection_reconcile.reconcile_op(conn, op["id"], r_ee, now=clock())
    exposure = protection_exposure.evaluate(conn, account_id=position["account_id"], mode=position["mode"],
                                            exchange=position["exchange"], tradingsymbol=position["symbol"],
                                            evidence=r_ee, established_problem=problem)
    result.exposure = exposure.report()
    _report_contradictions(conn, position, exposure, r_ee, result)
    state = _attribution(conn, position, r_ee) if attribution is None else attribution(r_ee)
    e3 = outstanding_sell_orders(position, r_ee) or (e3_check(r_ee) if e3_check is not None else None)
    if not exposure.gate_open() or state not in ("exclusive", "shared") or e3 is not None:
        reason = ("G22_R_EE_UNUSABLE" if not exposure.established else
                  "GATE_MEMBERS_REMAIN" if exposure.g_max or exposure.s_agg != 0 else
                  f"ATTRIBUTION_{str(state).upper()}" if state not in ("exclusive", "shared") else f"E3:{e3}")
        ids = ", ".join(m["member"] for m in result.exposure["members"]) or "-"
        result.outcome, result.reason = REFUSED, reason
        result.instruction = INSTRUCTION.format(ids=ids)
        _release_owner(conn, exit_id, reason)
        logger.warning("event=emergency_exit.refused position=%s reason=%s g_max=%s s_agg=%s members=%s", position["id"],
                       reason, exposure.g_max, exposure.s_agg, ids)
        return result
    outcome, order_id = _sell(conn, position, exit_id, open_qty, kite, now=clock(), on_sell=on_sell)
    result.outcome, result.reason, result.order_id = outcome, "GATE_OPEN", order_id
    if outcome == "SELL_CLAIM_LOST":
        result.outcome = REFUSED
    return result


def outstanding_sell_orders(position, evidence: Evidence) -> str | None:
    """The engine's part of the E3 rule (routes/kill_switch.py refuses a SELL the app did not place): any SELL order
    on the instrument in R_EE that is not proven terminal refuses the sale. It also covers an order placed by a GTT
    that was deleted after it triggered (deleting a GTT does not cancel its order) and one whose GTT is not listed."""
    from backend.protection_evidence import ORDER_TERMINAL
    key = (str(position["exchange"]).upper(), str(position["symbol"]).upper())
    open_ids = [o.id for o in evidence.orders.values()
                if (str(o.exchange or "").upper(), str(o.tradingsymbol or "").upper()) == key
                and o.transaction_type == "SELL" and o.status not in ORDER_TERMINAL]
    return f"OUTSTANDING_SELL_ORDER:{','.join(open_ids)}" if open_ids else None


def _attribution(conn, position, evidence: Evidence) -> str:
    """D§6.3, report-only before Phase 7 (startup_reconciliation.evaluate_attribution); anything else -> unknown."""
    from backend import startup_reconciliation
    if evidence.raw is None or evidence.held is None:
        return "unknown"
    try:
        report = startup_reconciliation.evaluate_attribution(conn, position["account_id"], evidence.raw)
    except Exception:  # noqa: BLE001
        return "unknown"
    key = f"{str(position['exchange']).upper()}:{str(position['symbol']).upper()}"
    states = [r["state"] for r in report if r["instrument"].upper() == key]
    return states[0] if len(states) == 1 else "unknown"


def _report_contradictions(conn, position, exposure, r_ee: Evidence, result: EEResult) -> None:
    """D / G2 (a confirmed cancel contradicted by an ACTIVE listing) and B-17 (a definite refusal saying the GTT does
    not exist while R_EE lists it ACTIVE): URGENT finding, position PROTECTION_NEEDS_REVIEW. Terminal ops unchanged."""
    codes = [(c["member"], c["code"]) for c in exposure.contradictions if "code" in c]
    for op in protection.ops_for_position(conn, position["id"]):
        if op["kind"] == CANCEL and op["state"] == REJECTED:
            g = r_ee.gtt(op["target_gtt_id"])
            text = json.dumps(protection.evidence_entries(op))
            if g is not None and g.status == "active" and ("not found" in text or "does not exist" in text):
                codes.append((str(op["target_gtt_id"]), protection.CANCEL_REFUSAL_CONTRADICTED))
    if not codes:
        return
    with instance_lock.fenced_transaction(conn):
        for member, code in codes:
            protection.raise_finding(conn, position, code, f"GTT {member}: the broker listing contradicts the journal",
                                     gtt_id=None)
            result.findings.append({"member": member, "code": code})
        conn.execute("UPDATE trade_tickets SET protection_state = 'PROTECTION_NEEDS_REVIEW' WHERE id = ?", (position["id"],))
