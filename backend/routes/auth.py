import asyncio
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from backend import automation, data_jobs, instruments, kite_client, live_arming, sector_mapping, security, workers
from backend.accounts import get_default_account
from backend.db import get_connection
from backend.ticker import collect_instrument_tokens, ticker_manager
from backend.watchlist import watchlist_instrument_tokens

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/login")
def login(request: Request):
    # P2-G: a login only THIS browser can finish: the state is cookie-bound AND sent through Kite
    # (redirect_params), which must echo it on the callback (R-F1). Supersedes any earlier login.
    state = security.begin_login()
    response = RedirectResponse(url=kite_client.build_login_url(state=state))
    security.set_login_state_cookie(response, state, secure=request.url.scheme == "https")
    return response


def _login_failed(request: Request) -> RedirectResponse:
    response = RedirectResponse(url="/?login_error=1")
    security.clear_login_state_cookie(response, secure=request.url.scheme == "https")
    return response


@router.get("/callback")
async def callback(request: Request):
    request_token = request.query_params.get("request_token")
    status = request.query_params.get("status")

    # P2-G login CSRF: refused before any token exchange unless this browser started the pending
    # login (GET /login) within the last 10 minutes and Kite echoed its state back unchanged (R-F1).
    # Single use: every callback consumes the state.
    if not security.consume_login_state(request.cookies.get(security.LOGIN_STATE_COOKIE),
                                        request.query_params.get(security.LOGIN_STATE_PARAM)):
        logger.warning("Kite callback refused: not the pending login started from this browser (login CSRF guard)")
        return _login_failed(request)

    if not request_token or status != "success":
        return _login_failed(request)

    try:
        await asyncio.to_thread(kite_client.exchange_request_token, request_token)
    except Exception:
        logger.exception("Kite login token exchange failed")
        return _login_failed(request)

    workers.supervisor.spawn("after_login", _after_login())
    # SEC-1 P1-F: a completed Kite login (the user's password and 2FA) is
    # what authorizes this browser during this launch. The cookie travels only
    # in this HttpOnly, SameSite=Strict cookie; P2-G R-F3: the port-isolated
    # terminal token only in this redirect's fragment, to this browser.
    cookie, terminal_token = security.issue_terminal_credentials()
    response = RedirectResponse(url=security.authorized_landing_url(terminal_token))
    security.set_credential_cookie(response, cookie, secure=request.url.scheme == "https")
    security.clear_login_state_cookie(response, secure=request.url.scheme == "https")
    return response


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
def session_status(request: Request):
    """Public (P2-G R-F2): tells any caller only whether a session exists and whether IT is
    authorized; the user's name and login time go to an authorized browser only."""
    account = get_default_account()
    row = kite_client.get_active_session()
    # SEC-1 P1-F + P2-G R-F3: whether THIS browser carries both factors of this launch's
    # credential (it does not after an app restart until the next Kite login). No value is
    # ever returned here.
    cookie = request.cookies.get(security.CREDENTIAL_COOKIE)
    authorized = security.request_authorized(cookie, request.headers.get(security.TOKEN_HEADER))
    connected = row is not None and kite_client.is_session_fresh(row)
    return {
        "connected": connected,
        "user_name": row["user_name"] if connected and authorized else None,
        "login_time": row["login_time"] if connected and authorized else None,
        "account_type": account["account_type"],
        "terminal_authorized": authorized,
        # Session expiry: only the browser whose own credential timed out is told why it is locked.
        "session_expired": security.credential_expired_reason(cookie),
        "session_expired_message": security.credential_expired_message(cookie),
    }


@router.post("/api/logout")
def logout(request: Request, response: Response):
    account = get_default_account()
    conn = get_connection()
    try:
        # SEC-1: no session, nothing armed: LIVE goes back to dry run.
        disarmed = live_arming.disarm_live(conn, account["id"], "logged out of Kite")
        conn.execute(
            "UPDATE sessions SET is_active = 0 WHERE is_active = 1 AND account_id = ?", (account["id"],)
        )
        conn.execute("UPDATE accounts SET access_token_enc = NULL WHERE id = ?", (account["id"],))
        conn.commit()
    finally:
        conn.close()
    if disarmed:
        live_arming.broadcast_mode_reset()
    ticker_manager.stop()
    # SEC-1 P1-F: no Kite session, no terminal credential.
    security.revoke_terminal_credential()
    security.clear_credential_cookie(response, secure=request.url.scheme == "https")
    return {"success": True}
