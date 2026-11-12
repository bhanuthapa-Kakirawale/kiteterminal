"""LIVE-0: Live Readiness — the live trading pre-flight.

GET  /api/live-readiness?broker=false|true   READY / NOT_READY with every check.
                                             Broker reads (read-only, one
                                             snapshot) only when broker=true.
POST /api/live-readiness/static-ip           confirm the Kite-whitelisted static IP
POST /api/live-readiness/static-ip/clear     withdraw that confirmation
POST /api/live-readiness/egress-check        observe this machine's outbound IP

The GET never writes, switches the execution mode or unlocks trading. The
POSTs are deliberate user acts, CSRF-guarded like every other write, and
work without a Kite session (the IP can be set before logging in).
"""

from fastapi import APIRouter, HTTPException

from backend import live_preflight
from backend.accounts import get_trading_account
from backend.db import get_connection
from backend.models import StaticIpConfirmRequest
from backend.instance_lock import WRITE_AUTHORITY

router = APIRouter()


def _trading_account():
    try:
        return get_trading_account()
    except PermissionError:
        raise HTTPException(403, {"error_code": "NOT_TRADING_ACCOUNT",
                                  "message": "The default account is not the execution-enabled trading account."})


@router.get("/api/live-readiness")
def live_readiness(broker: bool = False, refresh: bool = False):
    return {"data": live_preflight.evaluate(broker=broker, refresh=refresh)}


@router.post("/api/live-readiness/static-ip", dependencies=WRITE_AUTHORITY)
def confirm_static_ip(req: StaticIpConfirmRequest):
    account = _trading_account()
    if not req.confirm:
        raise HTTPException(400, {"error_code": "CONFIRMATION_REQUIRED",
                                  "message": "Tick the confirmation: this IP is whitelisted in the Kite developer "
                                             "console for this app."})
    conn = get_connection()
    try:
        try:
            status = live_preflight.confirm_static_ip(conn, account["id"], req.ip)
        except ValueError as e:
            raise HTTPException(400, {"error_code": "INVALID_STATIC_IP", "message": str(e)})
        return {"data": status}
    finally:
        conn.close()


@router.post("/api/live-readiness/static-ip/clear", dependencies=WRITE_AUTHORITY)
def clear_static_ip():
    account = _trading_account()
    conn = get_connection()
    try:
        return {"data": live_preflight.clear_static_ip(conn, account["id"])}
    finally:
        conn.close()


@router.post("/api/live-readiness/egress-check", dependencies=WRITE_AUTHORITY)
def egress_check():
    account = _trading_account()
    conn = get_connection()
    try:
        return {"data": live_preflight.check_egress_ip(conn, account["id"])}
    finally:
        conn.close()
