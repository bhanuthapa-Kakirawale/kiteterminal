"""C4: cancelling a GTT, and saying only what the broker confirms.

Kite (or the paper broker) acknowledging delete_gtt is not proof that the GTT
is gone, and a failed or lost response is not proof that it is still there.
cancel() makes the one delete request, then always reads the GTT book back
(recording it as a broker observation, backend/gtt_observations.py) and
classifies the outcome:

  CANCEL_CONFIRMED         the read-back lists the GTT as deleted / cancelled.
  CANCEL_FAILED            the read-back lists it ACTIVE (still armed), or the
                           request was definitively refused (an explicit
                           order/input/permission/token error) and no
                           read-back was possible.
  CANCEL_OUTCOME_UNKNOWN   anything else: the read-back failed after an
                           acknowledged or ambiguous request, the GTT is absent
                           from the book, or it is in another state (e.g.
                           triggered — it fired, it was not cancelled).

Nothing is retried and nothing is re-sent: callers record a persistent
reconciliation finding (GTT_CANCEL_FAILED / GTT_CANCEL_OUTCOME_UNKNOWN) and
an urgent alert for anything but CONFIRMED, and keep the GTT id so
reconciliation keeps watching it. Only CONFIRMED may be described as
"cancelled" / "removed".
"""

import logging
from dataclasses import dataclass

from kiteconnect.exceptions import InputException, OrderException, PermissionException, TokenException

logger = logging.getLogger(__name__)

CONFIRMED = "CANCEL_CONFIRMED"
FAILED = "CANCEL_FAILED"
UNKNOWN = "CANCEL_OUTCOME_UNKNOWN"

_CANCELLED_STATUSES = ("deleted", "cancelled")
# Kite answered and refused: the request was not processed.
_DEFINITE_REFUSALS = (OrderException, InputException, PermissionException, TokenException)


@dataclass
class CancelOutcome:
    state: str
    gtt_id: object
    broker_status: str | None    # the read-back's status for the GTT; 'missing' if absent; None if unread
    detail: str
    error: BaseException | None = None

    @property
    def confirmed(self) -> bool:
        return self.state == CONFIRMED


def cancel(broker, gtt_id) -> CancelOutcome:
    error = None
    try:
        broker.delete_gtt(gtt_id)
    except Exception as e:
        error = e
    status = None
    readback_error = None
    try:
        book = [g for g in broker.get_gtts() if isinstance(g, dict) and g.get("id") is not None]
        from backend import gtt_observations
        gtt_observations.record_book(book, expected=[gtt_id])
        listed = next((g for g in book if g["id"] == gtt_id), None)
        status = "missing" if listed is None else str(listed.get("status") or "active").strip().lower()
    except Exception as e:
        readback_error = e
    sent = "Kite acknowledged the delete" if error is None else f"the delete raised {type(error).__name__}: {error}"

    if status in _CANCELLED_STATUSES:
        outcome = CancelOutcome(CONFIRMED, gtt_id, status, f"GTT {gtt_id} cancelled: the broker lists it {status}.",
                                error)
    elif status == "active":
        outcome = CancelOutcome(FAILED, gtt_id, status,
                                f"GTT {gtt_id} was NOT cancelled: {sent}, and the broker still lists it ACTIVE.", error)
    elif status is None and error is not None and isinstance(error, _DEFINITE_REFUSALS):
        outcome = CancelOutcome(FAILED, gtt_id, None,
                                f"GTT {gtt_id} was NOT cancelled: the broker refused the delete "
                                f"({type(error).__name__}: {error}); the GTT book could not be read back "
                                f"({type(readback_error).__name__}: {readback_error}).", error)
    elif status is None:
        outcome = CancelOutcome(UNKNOWN, gtt_id, None,
                                f"GTT {gtt_id} cancellation outcome UNKNOWN: {sent}, but the GTT book could not be "
                                f"read back to confirm it ({type(readback_error).__name__}: {readback_error}).", error)
    else:
        outcome = CancelOutcome(UNKNOWN, gtt_id, status,
                                f"GTT {gtt_id} cancellation not confirmed: {sent}, and the broker lists it "
                                f"{'nowhere in its GTT book' if status == 'missing' else status}.", error)
    if outcome.confirmed:
        logger.info("event=gtt.cancel gtt_id=%s outcome=%s", gtt_id, outcome.state)
    else:
        logger.warning("event=gtt.cancel gtt_id=%s outcome=%s broker_status=%s: %s", gtt_id, outcome.state,
                       outcome.broker_status, outcome.detail)
    return outcome


def record_finding(conn, outcome: CancelOutcome, *, ticket_id, symbol, context: str) -> bool:
    """The persistent reconciliation finding for a cancellation that was not confirmed.
    Returns True if a new row was written. Caller commits."""
    from backend import reconciliation
    if outcome.confirmed:
        return False
    failed = outcome.state == FAILED
    code = reconciliation.GTT_CANCEL_FAILED if failed else reconciliation.GTT_CANCEL_OUTCOME_UNKNOWN
    action = (f"Delete GTT {outcome.gtt_id} in Kite NOW" if failed else
              f"Check GTT {outcome.gtt_id} in Kite now and delete it if it is still active")
    detail = (f"{symbol}: {'GTT CANCELLATION FAILED' if failed else 'GTT CANCELLATION OUTCOME UNKNOWN'} — "
              f"{context}. {outcome.detail} It could still fire against shares that are gone or held outside "
              f"this position. {action}. Nothing was retried.")
    return reconciliation.log_mismatch(
        conn, ticket_id, "order_status", expected=f"GTT {outcome.gtt_id} cancelled",
        actual=f"GTT {outcome.gtt_id} {outcome.state} (broker status {outcome.broker_status or 'unread'})",
        detail=detail, issue_code=code, severity=reconciliation.URGENT, symbol=symbol, gtt_id=outcome.gtt_id,
        suggested_action=reconciliation.CANCEL_IN_KITE if failed else reconciliation.CHECK_KITE,
    )
