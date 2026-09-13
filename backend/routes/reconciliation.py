from fastapi import APIRouter, Depends, HTTPException
from kiteconnect import KiteConnect

from backend.db import get_connection
from backend.kite_client import require_trading_account

router = APIRouter()


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
def mark_reviewed(mismatch_id: int, kite: KiteConnect = Depends(require_trading_account)):
    conn = get_connection()
    try:
        row = conn.execute("SELECT id FROM reconciliation_log WHERE id = ?", (mismatch_id,)).fetchone()
        if row is None:
            raise HTTPException(404, {"error_code": "NOT_FOUND", "message": "Reconciliation entry not found"})
        conn.execute("UPDATE reconciliation_log SET resolved = 1 WHERE id = ?", (mismatch_id,))
        conn.commit()
        return {"data": {"id": mismatch_id, "resolved": True}}
    finally:
        conn.close()
