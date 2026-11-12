"""Phase 3 S5 (DARK): sending a protection op to the broker, and associating its response.

docs/PHASE_3_DESIGN_DECISIONS.md rows 5-8, 16, 17c, 26, 27; M-3, M-4, DD-10 G2; R2-7 point 8.

The send is two fenced transactions:

  A. the write-ahead claim of the send: `intent -> sending` (row 5) or `unknown -> sending` (row 16), with every
     send predicate re-evaluated under the write lock (overlay, exit in flight, DX against THIS send's read, the
     runtime gate, DD-8 freshness, DD-9). Committed BEFORE the call, so a crash afterwards leaves `sending` for
     the successor (row 10). If a predicate fails: `intent -> not_sent` (row 5a/25) or `unknown` unchanged.

  B. the call itself, made INSIDE instance_lock.fenced_transaction: BEGIN IMMEDIATE, the epoch check under the
     write lock, a re-read of the op (still `sending`, same op_uuid, same attempt), the pre-call runtime gate, the
     broker call, and the response recorded by the M-4 compare-and-set, all before COMMIT.

Holding B's write lock across the call is a deliberate strengthening of R2-7 point 8 (which ran the pre-call check
outside any transaction): a successor's takeover must write app_instance, so it cannot commit while a send is in
progress. Broker-write authorisation is therefore atomic with respect to ownership changes. What remains is the
network itself: a request authorised at send time can be PROCESSED by the broker after the client gave up (B-31).
That is the ambiguous outcome M-3/M-4 handle (`unknown`, never re-sent, resolved by reads).

Consequences of the held lock, accepted and disclosed: other writers of the same database wait (busy timeout,
SQLITE_BUSY_TIMEOUT_SECONDS) for at most one broker call (bounded by the SDK timeout); a successor's takeover
attempted during a send fails closed and is retried. The broker call must not write the database on another
connection: the paper broker is given THIS connection (its writes join transaction B).

G2 (DD-10): if the response cannot be recorded because authority was lost (FencingError) or the commit failed,
one CRITICAL structured log line is written and NOTHING is written to the database; the successor resolves by
reads.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from kiteconnect.exceptions import InputException, OrderException, PermissionException, TokenException

from backend import config, instance_lock, paper_broker, protection, protection_runtime
from backend.protection import (
    ACKNOWLEDGED, CANCEL, CREATE, DX_CAPS, DX_INCREASES, DX_NONE, DX_REDUCES, DX_REDUNDANT, INTENT, MODIFY,
    NEEDS_REVIEW, NOT_SENT, REJECTED, SENDING, TERMINAL_STATES, UNKNOWN, ProtectionRefused, TransitionRefused,
)
from backend.protection_evidence import Evidence, iso, normalise_gtt_id, utc_now

logger = logging.getLogger(__name__)

# B-26/B-30 classification of a raised exception, per kind. Anything not listed is AMBIGUOUS (`unknown`).
_DEFINITE = {
    CREATE: (OrderException, InputException, PermissionException),
    MODIFY: (OrderException, InputException, PermissionException),
    CANCEL: (OrderException, InputException, PermissionException, TokenException),
}
# Refused inside this process before anything left it: no transition (the op stays `sending`; recovery makes it
# `unknown`, R2-7 point 8; a `sending -> not_sent` transition is not selected).
_LOCAL_REFUSALS = (instance_lock.InstanceNotOwner, paper_broker.ModeIsolationError)


class NoCall(Exception):
    """Raised inside transaction B before the broker call: rolls B back; nothing was sent."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class SendContext:
    """M-4: the immutable correlation context captured when `sending` is committed."""
    op_id: int
    op_uuid: str
    attempt: int
    instance_epoch: int
    kind: str
    request_fingerprint: str


@dataclass
class Outcome:
    result: str                 # 'acknowledged' | 'rejected' | 'unknown' | 'local_refusal'
    result_gtt_id: str | None = None
    error: str | None = None
    raw: object = None


@dataclass
class SendResult:
    op: dict | None
    sent: bool
    code: str
    detail: str = ""


def classify_exception(kind: str, exc: BaseException) -> str:
    if isinstance(exc, _LOCAL_REFUSALS):
        return "local_refusal"
    if isinstance(exc, _DEFINITE[kind]):
        return "rejected"
    return "unknown"


def parse_response(kind: str, response) -> Outcome:
    """A create must return a canonical trigger id; anything else is ambiguous (M-4: a malformed id)."""
    gid = normalise_gtt_id((response or {}).get("trigger_id")) if isinstance(response, dict) else None
    if kind == CREATE and gid is None:
        return Outcome("unknown", None, f"malformed response {response!r}", response)
    return Outcome("acknowledged", gid, None, response)


# ---- DX at the send (rows 5, 16) ------------------------------------------------------------------------

def target_version(op: dict, evidence: Evidence):
    """The target as listed in this read (modify/cancel), or None (create, or not listed)."""
    if op["kind"] == CREATE:
        return None
    return evidence.gtt(op["target_gtt_id"])


def evaluate_dx(conn, op: dict, evidence: Evidence, *, cap_conditions: dict | None = None,
                m2_conditions: dict | None = None) -> protection.DxResult:
    """DX re-evaluated against THIS read (never carried over from the claim or an earlier attempt)."""
    open_qty = protection.open_quantity(conn, op["position_id"]) or 0
    position = protection.get_position(conn, op["position_id"])
    if protection.broker_flat(position, open_qty):
        open_qty = 0
    request = protection.payload(op)
    if op["kind"] == CREATE:
        q0, loosen = 0, False
    else:
        target = target_version(op, evidence)
        if target is None:
            q0 = 0
            loosen = op["kind"] != CANCEL
        else:
            q0 = sum(l.quantity or 0 for l in target.sell_legs())
            loosen = op["kind"] == MODIFY and protection.is_loosen(protection.gtt_as_payload(target), request)
    q1 = 0 if op["kind"] == CANCEL else (protection.sell_quantity_of_payload(request) or 0)
    if op["kind"] == CANCEL:
        loosen = False
    return protection.classify(kind=op["kind"], reason=op["reason"], q0=q0, q1=q1, open_qty=open_qty,
                               loosen=loosen, cap_conditions=cap_conditions, m2_conditions=m2_conditions)


def _automatic(op: dict) -> bool:
    for entry in protection.evidence_entries(op):
        if isinstance(entry, dict) and "claim" in entry:
            return bool(entry["claim"].get("automatic"))
    return False


def dx_permits(op: dict, dx: protection.DxResult, *, retry: bool, may_increase_exposure: bool) -> str | None:
    """None = the class permits this send; otherwise the refusal code (DX automation rules; DD-4; M-1; R3-1)."""
    if op["reason"] == protection.R_ATTRIBUTION_CAP and dx.cls != DX_CAPS:
        return "CAP_NOT_CAPS_SELL_EXPOSURE"            # never sent as none/redundant (R3-1)
    if dx.cls == DX_INCREASES:
        if retry:
            return "INCREASE_NEVER_RETRIED"
        return None if may_increase_exposure else "DD9_REFUSED"
    if retry:
        if op["kind"] == CREATE or (op["kind"] == MODIFY and op["direction"] not in (protection.DOWN, protection.TIGHTEN)):
            return "KIND_NEVER_RETRIED"
        return None if dx.cls in (DX_NONE, DX_REDUNDANT, DX_CAPS) else "RETRY_CLASS_REFUSED"
    if _automatic(op):
        return None if dx.cls in (DX_NONE, DX_REDUNDANT, DX_CAPS) else "AUTOMATIC_CLASS_REFUSED"
    return None   # an explicit user action or an exit claim: one attempt, any class (DX rules)


# ---- transaction A ---------------------------------------------------------------------------------------

def _refuse_at_send(conn, op: dict, code: str, detail: dict, now: datetime) -> dict:
    """Row 5a/25: intent -> not_sent. An `unknown` op stays `unknown` (evidence only)."""
    entry = {"send_refused": code, **detail}
    if op["state"] == INTENT:
        return protection.transition(conn, op, NOT_SENT, from_states=(INTENT,), evidence=entry, now=now,
                                     event_detail={"refused": code})
    return protection.annotate(conn, op, entry, expect_state=(op["state"],), now=now)


def begin_send(conn, op_id: int, evidence: Evidence, *, retry: bool = False, may_increase_exposure: bool = False,
               cap_conditions: dict | None = None, m2_conditions: dict | None = None,
               now: datetime | None = None, monotonic=time.monotonic) -> tuple[dict, SendContext | None, str]:
    """Transaction A. Returns (op, context or None, code). A context means `sending` is committed."""
    now = now or utc_now()
    with protection.journal_transaction(conn):
        op = protection.get_op(conn, op_id)
        if op is None:
            raise ProtectionRefused("OP_MISSING", str(op_id))
        expected = UNKNOWN if retry else INTENT
        if op["state"] != expected:
            return op, None, f"NOT_{expected.upper()}"
        if op["superseded_at"] is not None:
            return _refuse_at_send(conn, op, "SUPERSEDED", {}, now), None, "SUPERSEDED"
        if op["kind"] != CANCEL and protection.exit_in_flight(conn, op["position_id"]):
            return _refuse_at_send(conn, op, "EXIT_IN_FLIGHT", {}, now), None, "EXIT_IN_FLIGHT"
        dx = evaluate_dx(conn, op, evidence, cap_conditions=cap_conditions, m2_conditions=m2_conditions)
        gate = protection_runtime.send_gate(conn, dx.cls, evidence, now=now, monotonic=monotonic)
        if gate is not None:
            return _refuse_at_send(conn, op, gate, {"dx": dx.record(evidence.read_id)}, now), None, gate
        code = dx_permits(op, dx, retry=retry, may_increase_exposure=may_increase_exposure)
        if code is not None:
            return _refuse_at_send(conn, op, code, {"dx": dx.record(evidence.read_id)}, now), None, code
        if retry:
            code = retry_problem(op, evidence, now=now)
            if code is not None:
                return _refuse_at_send(conn, op, code, {"dx": dx.record(evidence.read_id)}, now), None, code
        target = target_version(op, evidence)
        if op["kind"] != CREATE and target is None:
            return _refuse_at_send(conn, op, "TARGET_NOT_LISTED", {}, now), None, "TARGET_NOT_LISTED"
        attempt = op["attempts"] + 1
        entry = {"send": attempt, "dx": dx.record(evidence.read_id), "epoch": instance_lock.owner_epoch(),
                 "mono": monotonic(), "prestate": protection.gtt_as_payload(target) if target is not None else None,
                 "prestate_status": target.status if target is not None else None}
        op = protection.transition(conn, op, SENDING, from_states=(expected,), attempt=op["attempts"],
                                   sets={"attempts": attempt, "sent_at": iso(now), "last_checked_at": iso(now)},
                                   evidence=entry, now=now, event_detail={"attempt": attempt, "dx": dx.cls})
        ctx = SendContext(op["id"], op["op_uuid"], attempt, int(instance_lock.owner_epoch()), op["kind"],
                          op["request_fingerprint"])
    return op, ctx, "SENDING"


def retry_problem(op: dict, evidence: Evidence, *, now: datetime) -> str | None:
    """M-3 conditions 2-3 (condition 1 is dx_permits): a complete read started after the ambiguous attempt's
    sent_at + grace shows the target in its expected pre-state; the backoff elapsed; attempts < the cap."""
    from backend.protection_evidence import parse_time
    if op["attempts"] >= config.PROTECTION_RETRY_MAX_ATTEMPTS:
        return "RETRY_CAP_REACHED"
    sent = parse_time(op["sent_at"])
    if sent is None or evidence.started_at < sent + timedelta(seconds=config.PROTECTION_INFLIGHT_GRACE_SECONDS):
        return "READ_NOT_AFTER_GRACE"
    backoff = config.PROTECTION_RETRY_BACKOFF_SECONDS[min(op["attempts"], len(config.PROTECTION_RETRY_BACKOFF_SECONDS)) - 1]
    if now < sent + timedelta(seconds=backoff):
        return "BACKOFF"
    target = evidence.gtt(op["target_gtt_id"])
    if target is None:
        return "TARGET_NOT_LISTED"          # missing is never proof of anything (B-08/B-28)
    if target.status != "active":
        return "TARGET_NOT_IN_PRESTATE"
    if op["kind"] == MODIFY:
        prestate = last_prestate(op)
        if prestate is None or protection.fingerprint(prestate) != protection.fingerprint(protection.gtt_as_payload(target)):
            return "TARGET_NOT_IN_PRESTATE"
    return None


def last_prestate(op: dict) -> dict | None:
    for entry in reversed(protection.evidence_entries(op)):
        if isinstance(entry, dict) and entry.get("prestate") is not None:
            return entry["prestate"]
    return None


# ---- transaction B ---------------------------------------------------------------------------------------

def _broker_for(conn, op: dict, kite):
    broker = paper_broker.broker_for(op["mode"], kite, conn, op["account_id"])
    paper_broker.check_mode(broker, op["mode"])
    return broker


def _call(broker, op: dict):
    request = protection.payload(op)
    if op["kind"] == CREATE:
        return broker.place_gtt(request["trigger_type"], request["tradingsymbol"], request["exchange"],
                                request["trigger_values"], request["last_price"], request["orders"])
    if op["kind"] == MODIFY:
        return broker.modify_gtt(op["target_gtt_id"], request["trigger_type"], request["tradingsymbol"],
                                 request["exchange"], request["trigger_values"], request["last_price"],
                                 request["orders"])
    return broker.delete_gtt(op["target_gtt_id"])


def g2_log(ctx: SendContext, outcome: Outcome, why: str) -> None:
    logger.critical(
        "event=protection.response_after_authority_loss op_id=%s op_uuid=%s attempt=%s epoch=%s kind=%s outcome=%s "
        "result_gtt_id=%s fingerprint=%s why=%s: NOT recorded (no database write); the successor resolves by reads",
        ctx.op_id, ctx.op_uuid, ctx.attempt, ctx.instance_epoch, ctx.kind, outcome.result, outcome.result_gtt_id,
        ctx.request_fingerprint, why)


def perform_send(conn, ctx: SendContext, kite, *, now=utc_now, monotonic=time.monotonic,
                 on_call=None) -> SendResult:
    """Transaction B (see the module docstring). `on_call` is a test hook run inside B, immediately before the
    broker call (after every check)."""
    outcome: Outcome | None = None
    try:
        with protection.journal_transaction(conn):
            op = protection.get_op(conn, ctx.op_id)
            if op is None or op["op_uuid"] != ctx.op_uuid or op["state"] != SENDING or op["attempts"] != ctx.attempt:
                raise NoCall("STATE_CHANGED", f"op {ctx.op_id} is no longer sending attempt {ctx.attempt}")
            if instance_lock.owner_epoch() != ctx.instance_epoch:
                raise NoCall("EPOCH_CHANGED", f"context epoch {ctx.instance_epoch}")
            gate = protection_runtime.pre_call_gate(now=now(), monotonic=monotonic)
            if gate is not None:
                raise NoCall(gate, "pre-call runtime gate (R2-7 point 8)")
            broker = _broker_for(conn, op, kite)
            if on_call is not None:
                on_call(ctx)
            try:
                response = _call(broker, op)
                outcome = parse_response(op["kind"], response)
            except Exception as e:   # noqa: BLE001 - every broker error is classified, none is swallowed silently
                outcome = Outcome(classify_exception(op["kind"], e), None, f"{type(e).__name__}: {e}")
            if outcome.result == "local_refusal":
                raise NoCall("LOCAL_REFUSAL", outcome.error or "")
            recorded = record_response(conn, ctx, outcome, now=now())
        return SendResult(recorded, True, outcome.result.upper())
    except NoCall as e:
        logger.critical("event=protection.send_not_made op_id=%s attempt=%s code=%s detail=%s: the op stays %s",
                        ctx.op_id, ctx.attempt, e.code, e.detail, SENDING)
        return SendResult(_read(conn, ctx.op_id), False, e.code, e.detail)
    except instance_lock.FencingError as e:
        if outcome is not None:            # the call happened, the record could not commit
            g2_log(ctx, outcome, f"{type(e).__name__}: {e}")
            return SendResult(None, True, "G2_NOT_RECORDED", str(e))
        logger.critical("event=protection.send_not_made op_id=%s attempt=%s code=AUTHORITY_LOST detail=%s",
                        ctx.op_id, ctx.attempt, e)
        return SendResult(None, False, "AUTHORITY_LOST", str(e))
    except Exception as e:  # noqa: BLE001 - e.g. the COMMIT itself failed after the call
        if outcome is not None:
            g2_log(ctx, outcome, f"{type(e).__name__}: {e}")
            return SendResult(None, True, "G2_NOT_RECORDED", str(e))
        raise


def _read(conn, op_id):
    try:
        return protection.get_op(conn, op_id)
    except Exception:  # noqa: BLE001
        return None


# ---- M-4: associating a response with its op ------------------------------------------------------------

def record_response(conn, ctx: SendContext, outcome: Outcome, *, now: datetime | None = None) -> dict:
    """M-4 inside an open journal transaction. Every write is keyed on (op_id, op_uuid, attempt, state)."""
    now = now or utc_now()
    op = protection.get_op(conn, ctx.op_id)
    if op is None or op["op_uuid"] != ctx.op_uuid:
        raise ProtectionRefused("OP_IDENTITY", f"no op {ctx.op_id} with uuid {ctx.op_uuid}")
    entry = {"response": outcome.result, "attempt": ctx.attempt, "result_gtt_id": outcome.result_gtt_id,
             "error": outcome.error, "epoch": ctx.instance_epoch}
    current_attempt = op["attempts"] == ctx.attempt
    if not current_attempt:                                   # a later retry was sent: evidence only (row 27)
        return protection.annotate(conn, op, entry | {"stale_attempt": True}, now=now)
    if op["state"] in TERMINAL_STATES:                         # rows 26/27
        return _late_on_terminal(conn, op, outcome, entry, now)
    if op["state"] not in (SENDING, UNKNOWN):
        return protection.annotate(conn, op, entry | {"ignored_in_state": op["state"]}, now=now)
    frm = op["state"]
    if outcome.result == "rejected":
        return protection.transition(conn, op, REJECTED, from_states=(frm,), attempt=ctx.attempt, evidence=entry,
                                     now=now, event_detail={"error": outcome.error})
    if outcome.result == "unknown":
        if frm == UNKNOWN:
            return protection.annotate(conn, op, entry, expect_state=(UNKNOWN,), now=now)
        return protection.transition(conn, op, UNKNOWN, from_states=(SENDING,), attempt=ctx.attempt,
                                     evidence=entry | {"unknown_at": iso(now), "settle_reset": True},
                                     sets={"complete_reads_after": 0}, now=now, event_detail={"error": outcome.error})
    # acknowledged
    sets = {"acknowledged_at": iso(now)}
    if op["kind"] == CREATE:
        sets["result_gtt_id"] = outcome.result_gtt_id
    contradiction = None
    if op["kind"] != CREATE and outcome.result_gtt_id is not None and \
            outcome.result_gtt_id != protection.normalise_gtt_id(op["target_gtt_id"]):
        contradiction = "RESPONSE_ID_DIFFERS_FROM_TARGET"
    op = protection.transition(conn, op, ACKNOWLEDGED, from_states=(frm,), attempt=ctx.attempt, sets=sets,
                               evidence=entry, now=now, event_detail={"result_gtt_id": outcome.result_gtt_id})
    if op["kind"] == CREATE and op["superseded_at"] is None:
        if not protection.link_gtt(conn, op["position_id"], outcome.result_gtt_id):
            contradiction = "ID_LINKED_ELSEWHERE_OR_POSITION_LINKED"
    if contradiction:
        op = protection.transition(conn, op, NEEDS_REVIEW, from_states=(ACKNOWLEDGED,), evidence={"review": contradiction},
                                   now=now, event_detail={"review": contradiction})
    elif op["superseded_at"] is not None and op["kind"] == CREATE:
        protection.resolve_superseded_result(conn, op, outcome.result_gtt_id, app_proven=True, now=now)
    return protection.get_op(conn, op["id"])


def _late_on_terminal(conn, op: dict, outcome: Outcome, entry: dict, now: datetime) -> dict:
    """Rows 26/27: a terminal op never changes; a contradictory late response raises a finding, marks the
    position PROTECTION_NEEDS_REVIEW, and new creates on the instrument are refused by M-5 condition 4."""
    if outcome.result == "unknown":                 # an ambiguous late outcome carries nothing to contradict
        return protection.annotate(conn, op, entry | {"late": "ambiguous"}, now=now)
    if outcome.result == "acknowledged":
        consistent = (op["state"] in (protection.CONFIRMED, protection.ADOPTED)
                      and (op["kind"] != CREATE or normalise_gtt_id(op["result_gtt_id"]) == outcome.result_gtt_id))
    else:                                            # a late definite rejection
        consistent = op["state"] == REJECTED
    if consistent:
        return protection.annotate(conn, op, entry | {"late": "consistent"}, now=now)
    code = (protection.LATE_GTT_APPEARED if op["state"] == protection.ABSENT_AFTER_SETTLE and outcome.result_gtt_id
            else protection.LATE_RESPONSE_CONTRADICTS)
    op = protection.annotate(conn, op, entry | {"late": "contradictory", "finding": code}, now=now)
    position = protection.get_position(conn, op["position_id"])
    protection.raise_finding(conn, position, code, f"late {outcome.result} for terminal op {op['id']} ({op['state']}), "
                             f"returned GTT id {outcome.result_gtt_id}", gtt_id=outcome.result_gtt_id)
    conn.execute("UPDATE trade_tickets SET protection_state = 'PROTECTION_NEEDS_REVIEW' WHERE id = ?", (position["id"],))
    if code == protection.LATE_GTT_APPEARED:
        protection._event(conn, position["id"], protection.EV_LATE_GTT, {"op_id": op["id"], "gtt_id": outcome.result_gtt_id})
    return op


# ---- the whole send --------------------------------------------------------------------------------------

def send(conn, op_id: int, kite, evidence: Evidence, *, may_increase_exposure: bool = False,
         cap_conditions: dict | None = None, m2_conditions: dict | None = None, now=utc_now,
         monotonic=time.monotonic, on_call=None) -> SendResult:
    """Row 5 then the call. Never sends an op whose send predicates fail at the send."""
    op, ctx, code = begin_send(conn, op_id, evidence, may_increase_exposure=may_increase_exposure,
                               cap_conditions=cap_conditions, m2_conditions=m2_conditions, now=now(),
                               monotonic=monotonic)
    if ctx is None:
        return SendResult(op, False, code)
    return perform_send(conn, ctx, kite, now=now, monotonic=monotonic, on_call=on_call)


def retry(conn, op_id: int, kite, evidence: Evidence, *, cap_conditions: dict | None = None,
          m2_conditions: dict | None = None, now=utc_now, monotonic=time.monotonic, on_call=None) -> SendResult:
    """Row 16 (M-3): the only same-op re-send; never for a create, a modify up or a loosen."""
    op, ctx, code = begin_send(conn, op_id, evidence, retry=True, cap_conditions=cap_conditions,
                               m2_conditions=m2_conditions, now=now(), monotonic=monotonic)
    if ctx is None:
        return SendResult(op, False, code)
    return perform_send(conn, ctx, kite, now=now, monotonic=monotonic, on_call=on_call)


def record_late_response(conn, ctx: SendContext, outcome: Outcome, *, now: datetime | None = None) -> SendResult:
    """A response delivered after its call returned (another thread, a resumed thread): one fenced CAS, or the
    G2 log line if this process no longer has authority."""
    try:
        with protection.journal_transaction(conn):
            return SendResult(record_response(conn, ctx, outcome, now=now), True, "RECORDED")
    except instance_lock.FencingError as e:
        g2_log(ctx, outcome, f"{type(e).__name__}: {e}")
        return SendResult(None, True, "G2_NOT_RECORDED", str(e))
