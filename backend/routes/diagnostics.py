"""PK-22H: read-only operational status. No broker call, no credential.

GET /api/health       liveness: always 200 while the process serves requests.
GET /api/ready        readiness to trade: 200 when ready, 503 with blockers.
GET /api/diagnostics  the full System Diagnostics report.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend import diagnostics

router = APIRouter()


@router.get("/api/health")
def health():
    return {"data": diagnostics.health()}


@router.get("/api/ready")
def ready():
    result = diagnostics.readiness()
    return JSONResponse({"data": result}, status_code=200 if result["ready"] else 503)


@router.get("/api/diagnostics")
def diagnostics_report(refresh: bool = False):
    return {"data": diagnostics.report(refresh_integrity=refresh)}
