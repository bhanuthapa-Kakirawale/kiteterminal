"""PK-24: read-only Smart Money Accumulation Score for one symbol.

GET /api/smart-money-score/{symbol}?as_of=YYYY-MM-DD&instrument_token=<int>

Stored history only: no broker call, no write, no order. The score is a
heuristic description of accumulation-like price/volume evidence (see
backend/smart_money_score.py), never a recommendation, and nothing in the
scanner, approval, risk or execution paths reads it.

The symbol is resolved to ONE instrument token exactly as the PK-23 volume
endpoint does (watchlist, then the NSE instrument cache, then stored history
preferring an NSE token) — an NSE and a BSE series of the same symbol are
never combined. The PK-23 features are computed once (through their cache)
and scored; an unusable history is `score: null, confidence:
INSUFFICIENT_DATA`, never a made-up 50.
"""

from datetime import date

from fastapi import APIRouter, HTTPException, Query

from backend import smart_money_score, volume_intelligence
from backend.db import get_connection
from backend.routes.volume_intelligence import resolve_instrument

router = APIRouter()


@router.get("/api/smart-money-score/{symbol}")
def get_smart_money_score(
    symbol: str,
    as_of: str | None = Query(None, description="YYYY-MM-DD; the last stored session on or before it"),
    instrument_token: int | None = Query(None),
):
    if as_of is not None:
        try:
            as_of = date.fromisoformat(as_of).isoformat()
        except ValueError:
            raise HTTPException(422, {"error_code": "INVALID_DATE", "message": "as_of must be YYYY-MM-DD."})
    conn = get_connection()
    try:
        resolved = resolve_instrument(conn, symbol, instrument_token)
        if resolved is None:
            raise HTTPException(404, {"error_code": "UNKNOWN_SYMBOL",
                                      "message": f"No stored history or NSE instrument for {symbol!r}."})
        features = volume_intelligence.features_for_instrument(
            conn, resolved["instrument_token"], resolved["symbol"], as_of=as_of)
    finally:
        conn.close()
    result = smart_money_score.score(features)
    return {"data": {**result, "requested_as_of": features.get("requested_as_of"),
                     "history_status": features.get("history_status"),
                     "freshness": features.get("freshness"), "notes": features.get("notes") or [],
                     "resolved": resolved}}
