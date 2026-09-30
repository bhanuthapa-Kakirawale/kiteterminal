"""PK-26: read-only Price-Volume Research scanner.

GET /api/research/volume-scan              every watchlist instrument, one batch
GET /api/research/volume-scan.csv          the same rows as CSV
GET /api/research/volume-scan/{token}      one row's full PK-23 / PK-24 evidence

Stored history only: no broker dependency, no broker call, no write, no
order, GTT or ticket change. Descriptive research — never a ranking: the
default order is the symbol, filters are only the ones the caller passes,
and Smart Money Score v1.0 is one reference column carrying PK-25's LIMITED
validation status (see backend/research_scanner.py).

Query parameters shared by the list and CSV routes:
  as_of=YYYY-MM-DD  evidence as of the last stored session on or before it
  preset=<key>      a transparent research preset (AND of PK-23 conditions)
  min_rvol, min_volume_percentile, min_avg_traded_value, min_close_location,
  min_accumulation_10, max_distribution_10, sms_min, sms_max      numbers
  obv, volume, breakout, pullback, signal, tag                    choices
  sort=<field>&order=asc|desc                                    default symbol asc
"""

import time
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from backend import research_scanner as rs
from backend.db import get_connection

router = APIRouter()


def _as_of(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(422, {"error_code": "INVALID_DATE", "message": "as_of must be YYYY-MM-DD."})


def _scan(request: Request) -> tuple[dict, list[dict], list[dict], dict]:
    params = dict(request.query_params)
    as_of = _as_of(params.get("as_of"))
    try:
        conditions = rs.conditions_from_params(params)
        sort = {"key": params.get("sort") or rs.DEFAULT_SORT["key"],
                "order": params.get("order") or rs.DEFAULT_SORT["order"]}
        rs.sort_rows([], sort["key"], sort["order"])     # validates before any work
    except rs.FilterError as e:
        raise HTTPException(422, {"error_code": "INVALID_FILTER", "message": str(e)})
    before = dict(rs.cache_stats)
    started = time.perf_counter()
    conn = get_connection()
    try:
        scan = rs.build_scan(conn, as_of=as_of)
    finally:
        conn.close()
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    rows = rs.sort_rows([r for r in scan["rows"] if rs.matches(r, conditions)], sort["key"], sort["order"])
    info = {"elapsed_ms": elapsed, "cache_hits": rs.cache_stats["hits"] - before["hits"],
            "cache_misses": rs.cache_stats["misses"] - before["misses"]}
    return scan, rows, conditions, {**sort, "is_default": sort == rs.DEFAULT_SORT, **info}


@router.get("/api/research/volume-scan")
def get_volume_scan(request: Request):
    scan, rows, conditions, sort = _scan(request)
    warnings = [rs.SMS_FILTER_WARNING] if rs.uses_score(conditions) else []
    timing = {k: sort.pop(k) for k in ("elapsed_ms", "cache_hits", "cache_misses")}
    return {"data": {
        **rs.meta(),
        "requested_as_of": scan["requested_as_of"],
        "latest_completed_session": scan["latest_completed_session"],
        "universe": scan["universe"],
        "applied": {"preset": request.query_params.get("preset") or None, "conditions": conditions,
                    "sort": sort},
        "total_rows": len(scan["rows"]),
        "matched_rows": len(rows),
        "rows": rows,
        "warnings": warnings,
        "timing": timing,
    }}


@router.get("/api/research/volume-scan.csv")
def get_volume_scan_csv(request: Request):
    scan, rows, _, _ = _scan(request)
    stamp = scan["requested_as_of"] or scan["latest_completed_session"]
    return Response(rs.export_csv(rows), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="price_volume_research_{stamp}.csv"'})


@router.get("/api/research/volume-scan/{instrument_token}")
def get_volume_scan_detail(instrument_token: int, as_of: str | None = None):
    as_of = _as_of(as_of)
    conn = get_connection()
    try:
        scan = rs.build_scan(conn, as_of=as_of, only_token=instrument_token)
    finally:
        conn.close()
    if not scan["rows"]:
        raise HTTPException(404, {"error_code": "NOT_IN_WATCHLIST",
                                  "message": f"Instrument {instrument_token} is not on the watchlist."})
    evidence = scan["evidence"][instrument_token]
    meta = rs.meta()
    return {"data": {
        "row": scan["rows"][0],
        "volume_features": evidence["features"],
        "smart_money": evidence["smart_money"],
        "smart_money_display": meta["smart_money"],
        "scanner": meta["scanner"],
        "latest_completed_session": scan["latest_completed_session"],
    }}
