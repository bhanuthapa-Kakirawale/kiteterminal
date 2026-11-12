"""PK-12: account-level safety — status, the trading lock and the broker
circuit breaker reset. Every change is an explicit, logged server-side act;
the UI only displays and requests. Writes are CSRF-guarded like every other
write (backend/security.py)."""

from fastapi import APIRouter, Depends
from kiteconnect import KiteConnect

from backend import account_safety
from backend.accounts import get_trading_account
from backend.db import get_connection
from backend.kite_client import require_trading_account
from backend.models import BreakerResetRequest, TradingLockRequest
from backend.instance_lock import WRITE_AUTHORITY

router = APIRouter()


@router.get("/api/account-safety/status")
def get_status(kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        return {"data": account_safety.status(conn, account["id"])}
    finally:
        conn.close()


@router.post("/api/account-safety/trading-lock", dependencies=WRITE_AUTHORITY)
def set_trading_lock(req: TradingLockRequest, kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        changed = account_safety.set_trading_lock(conn, account["id"], req.locked, (req.reason or "").strip() or None)
        return {"data": {"changed": changed, **account_safety.status(conn, account["id"])}}
    finally:
        conn.close()


@router.post("/api/account-safety/breaker/reset", dependencies=WRITE_AUTHORITY)
def reset_breaker(req: BreakerResetRequest, kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        was_open = account_safety.reset_breaker(conn, account["id"], (req.reason or "").strip() or None)
        return {"data": {"changed": was_open, **account_safety.status(conn, account["id"])}}
    finally:
        conn.close()
