import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from backend import automation, data_jobs, instruments, kite_client, sector_mapping, workers
from backend.accounts import get_default_account
from backend.db import get_connection
from backend.ticker import collect_instrument_tokens, ticker_manager
from backend.watchlist import watchlist_instrument_tokens

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/login")
def login():
    return RedirectResponse(url=kite_client.build_login_url())


@router.get("/callback")
async def callback(request: Request):
    request_token = request.query_params.get("request_token")
    status = request.query_params.get("status")

    if not request_token or status != "success":
        return RedirectResponse(url="/?login_error=1")

    try:
        await asyncio.to_thread(kite_client.exchange_request_token, request_token)
    except Exception:
        logger.exception("Kite login token exchange failed")
        return RedirectResponse(url="/?login_error=1")

    workers.supervisor.spawn("after_login", _after_login())
    return RedirectResponse(url="/")


async def _after_login() -> None:
    """Everything a fresh session unlocks, done automatically: live ticks for
    holdings AND watchlist (the watchlist used to be left out until its next
    edit), a stale instrument list refreshed, sectors seeded, and market data
    synced — unless the autopilot has a run due, which syncs as its first
    step and is picked up within a minute of login."""
    try:
        kite = kite_client.get_kite()
        account = get_default_account()
        tokens = set(await asyncio.to_thread(collect_instrument_tokens, kite))
        tokens |= set(watchlist_instrument_tokens(account["id"]))
        ticker_manager.start(account["api_key"], kite.access_token, list(tokens))
    except Exception:
        logger.exception("Failed to start ticker after login")
        return
    try:
        await asyncio.to_thread(instruments.refresh_instrument_cache_if_stale, kite)
        await asyncio.to_thread(sector_mapping.seed_from_holdings, kite)
        if not automation.is_due():
            await data_jobs.sync_all_symbols()
    except Exception:
        logger.exception("Post-login refresh failed")


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
