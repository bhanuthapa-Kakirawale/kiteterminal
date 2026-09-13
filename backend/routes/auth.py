import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from backend import kite_client
from backend.accounts import get_default_account
from backend.db import get_connection
from backend.ticker import collect_instrument_tokens, ticker_manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/login")
def login():
    return RedirectResponse(url=kite_client.build_login_url())


@router.get("/callback")
def callback(request: Request):
    request_token = request.query_params.get("request_token")
    status = request.query_params.get("status")

    if not request_token or status != "success":
        return RedirectResponse(url="/?login_error=1")

    try:
        kite_client.exchange_request_token(request_token)
    except Exception:
        return RedirectResponse(url="/?login_error=1")

    try:
        kite = kite_client.get_kite()
        account = get_default_account()
        tokens = collect_instrument_tokens(kite)
        ticker_manager.start(account["api_key"], kite.access_token, tokens)
    except Exception:
        # Live ticks are a bonus; don't block a successful login on this.
        logger.exception("Failed to start ticker after login")

    return RedirectResponse(url="/")


@router.get("/api/session/status")
def session_status():
    account = get_default_account()
    row = kite_client.get_active_session()
    if row is not None and kite_client.is_session_fresh(row):
        return {
            "connected": True,
            "user_name": row["user_name"],
            "login_time": row["login_time"],
            "account_type": account["account_type"],
        }
    return {"connected": False, "user_name": None, "login_time": None, "account_type": account["account_type"]}


@router.post("/api/logout")
def logout():
    account = get_default_account()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE sessions SET is_active = 0 WHERE is_active = 1 AND account_id = ?", (account["id"],)
        )
        conn.execute("UPDATE accounts SET access_token_enc = NULL WHERE id = ?", (account["id"],))
        conn.commit()
    finally:
        conn.close()
    ticker_manager.stop()
    return {"success": True}
