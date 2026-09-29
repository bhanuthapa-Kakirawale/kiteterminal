"""PK-22D: historical-data health and the explicit rebuild of one symbol.

GET  /api/data/history-health                 status of every stored instrument
POST /api/data/history/{token}/rebuild        queue a full re-download (202)

A rebuild is only ever started here, by the user — never automatically.
It needs a Kite session when the job runs; without one the job is skipped
and logged, and the stored history is left unchanged.
"""

from fastapi import APIRouter, HTTPException

from backend import data_jobs, ohlc_integrity
from backend.db import get_connection

router = APIRouter()


@router.get("/api/data/history-health")
def history_health():
    conn = get_connection()
    try:
        return {"data": {**ohlc_integrity.health(conn), "pending_jobs": data_jobs.pending_jobs()}}
    finally:
        conn.close()


@router.post("/api/data/history/{instrument_token}/rebuild", status_code=202)
async def rebuild_history(instrument_token: int):
    conn = get_connection()
    try:
        row = ohlc_integrity.get_status(conn, instrument_token)
        if row is None:
            row = conn.execute(
                "SELECT tradingsymbol AS symbol FROM instrument_cache WHERE instrument_token = ?", (instrument_token,)
            ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, {"error_code": "UNKNOWN_INSTRUMENT",
                                  "message": f"No stored history or instrument for token {instrument_token}."})
    queued = await data_jobs.enqueue_rebuild(instrument_token, row["symbol"])
    return {"data": {"queued": queued, "instrument_token": instrument_token, "symbol": row["symbol"],
                     "message": "Rebuild queued." if queued else "A rebuild of this symbol is already queued."}}
