from fastapi import APIRouter, Depends, HTTPException
from kiteconnect import KiteConnect

from backend import reconciliation, system_events
from backend.accounts import get_trading_account
from backend.db import get_connection
from backend.kite_client import require_trading_account
from backend.models import ReconciliationReviewRequest

router = APIRouter()


@router.get("/api/reconciliation/status")
def reconciliation_status(kite: KiteConnect = Depends(require_trading_account)):
    """PK-16: what the last reconciliation cycle could read — so an
    unreadable GTT book or holdings list is shown as "not checked", never
    mistaken for "nothing wrong"."""
    return {"data": reconciliation.cycle_status()}


@router.get("/api/reconciliation/mismatches")
def list_mismatches(resolved: bool = False, kite: KiteConnect = Depends(require_trading_account)):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM reconciliation_log WHERE resolved = ? ORDER BY checked_at DESC",
            (int(resolved),),
        ).fetchall()
        return {"data": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.post("/api/reconciliation/{mismatch_id}/mark-reviewed")
def mark_reviewed(mismatch_id: int, req: ReconciliationReviewRequest,
                  kite: KiteConnect = Depends(require_trading_account)):
    """SEC-1: an unresolved critical finding blocks every live new entry at
    confirmation (account_safety.check_live_critical_state). Marking it
    reviewed lifts that block, so it needs a reason and is recorded as a
    reconciliation_reviewed system event."""
    reason = req.reason.strip()
    if not reason:
        raise HTTPException(400, {"error_code": "REASON_REQUIRED",
                                  "message": "Say why this finding is resolved: what you checked in Kite."})
    account = get_trading_account()
    conn = get_connection()
    try:
        row = conn.execute("SELECT id, issue_code, symbol, resolved FROM reconciliation_log WHERE id = ?",
                           (mismatch_id,)).fetchone()
        if row is None:
            raise HTTPException(404, {"error_code": "NOT_FOUND", "message": "Reconciliation entry not found"})
        changed = conn.execute("UPDATE reconciliation_log SET resolved = 1 WHERE id = ? AND resolved = 0",
                               (mismatch_id,)).rowcount == 1
        if changed:
            what = " ".join(part for part in (row["issue_code"] or "NEEDS_REVIEW", row["symbol"]) if part)
            system_events.log_event(conn, account["id"], "reconciliation_reviewed",
                                    f"Reconciliation finding #{mismatch_id} ({what}) marked reviewed. "
                                    f"Reason: {reason}")
        conn.commit()
        return {"data": {"id": mismatch_id, "resolved": True}}
    finally:
        conn.close()
