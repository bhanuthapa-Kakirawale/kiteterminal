import logging
from datetime import datetime

from cryptography.fernet import InvalidToken
from fastapi import Depends, HTTPException
from kiteconnect import KiteConnect

from backend import crypto, kite_rest_proxy
from backend.accounts import get_account, get_default_account, get_trading_account
from backend.db import get_connection
from backend.redaction import register_secret

logger = logging.getLogger(__name__)

from backend.trading_calendar import IST  # PK-22C: one IST definition


class NoActiveSessionError(Exception):
    """Raised when there is no active session, or the stored session is stale."""


def _api_key(account) -> str:
    return account["api_key"]


def _api_secret_decrypted(account) -> str:
    secret = crypto.decrypt(account["api_secret_enc"])
    register_secret(secret)
    return secret


def _access_token_decrypted(account) -> str | None:
    """The account's Kite access token. It is stored only Fernet-encrypted in
    accounts.access_token_enc (PK-18); sessions.access_token is always ''."""
    ciphertext = account["access_token_enc"]
    if not ciphertext:
        return None
    try:
        token = crypto.decrypt(ciphertext)
    except InvalidToken:
        logger.warning("Stored access token for account %s cannot be decrypted; a fresh login is needed",
                       account["id"])
        return None
    register_secret(token)
    return token


def create_kite_connect(api_key: str) -> KiteConnect:
    """NET-1: the one place a KiteConnect REST client is built. With
    KITE_REST_PROXY set, every request it makes (login, reads, historical
    data, orders, GTTs) goes through that proxy and nowhere else: an invalid
    value raises KiteRestProxyError before any client exists, and the session
    ignores environment proxy settings (HTTP(S)_PROXY, NO_PROXY, netrc) so
    nothing can bypass or redirect the configured route. KiteTicker is not
    built here and stays direct."""
    proxy = kite_rest_proxy.from_env()
    if proxy is None:
        return KiteConnect(api_key=api_key)
    kite = KiteConnect(api_key=api_key, proxies=proxy.requests_proxies)
    kite.reqsession.trust_env = False
    return kite


def _resolve_account(account_id: int | None):
    if account_id is None:
        return get_default_account()
    return get_account(account_id)


def build_login_url(account_id: int | None = None) -> str:
    account = _resolve_account(account_id)
    kite = create_kite_connect(_api_key(account))
    return kite.login_url()


def exchange_request_token(request_token: str, account_id: int | None = None) -> dict:
    account = _resolve_account(account_id)
    kite = create_kite_connect(_api_key(account))
    register_secret(request_token)
    data = kite.generate_session(request_token, api_secret=_api_secret_decrypted(account))
    register_secret(data["access_token"])

    login_time = datetime.now(IST).isoformat()

    conn = get_connection()
    disarmed = False
    try:
        # SEC-1: a new broker session is not the one Live Readiness checked:
        # LIVE goes back to dry run in the same transaction (live_arming.disarm_live).
        from backend import live_arming
        disarmed = live_arming.disarm_live(conn, account["id"], "a new Kite login replaced the broker session")
        conn.execute(
            "UPDATE sessions SET is_active = 0 WHERE is_active = 1 AND account_id = ?",
            (account["id"],),
        )
        # The token itself is stored only encrypted, on the account row below;
        # the session row keeps '' in its legacy NOT NULL column.
        conn.execute(
            """
            INSERT INTO sessions (access_token, public_token, user_id, user_name, login_time, account_id)
            VALUES ('', ?, ?, ?, ?, ?)
            """,
            (
                data.get("public_token"),
                data.get("user_id"),
                data.get("user_name"),
                login_time,
                account["id"],
            ),
        )
        conn.execute(
            "UPDATE accounts SET access_token_enc = ? WHERE id = ?",
            (crypto.encrypt(data["access_token"]), account["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    if disarmed:
        from backend import live_arming
        live_arming.broadcast_mode_reset()

    return data


def get_active_session(account_id: int | None = None):
    account = _resolve_account(account_id)
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE is_active = 1 AND account_id = ? ORDER BY id DESC LIMIT 1",
            (account["id"],),
        ).fetchone()
        return row
    finally:
        conn.close()


def is_session_fresh(row) -> bool:
    if row is None:
        return False
    try:
        login_time = datetime.fromisoformat(row["login_time"])
    except (TypeError, ValueError):
        return False
    if login_time.tzinfo is None:
        login_time = login_time.replace(tzinfo=IST)
    return login_time.astimezone(IST).date() == datetime.now(IST).date()


def get_kite(account_id: int | None = None) -> KiteConnect:
    account = _resolve_account(account_id)
    row = get_active_session(account["id"])
    if not is_session_fresh(row):
        raise NoActiveSessionError()
    token = _access_token_decrypted(account)
    if not token:
        raise NoActiveSessionError()
    kite = create_kite_connect(_api_key(account))
    kite.set_access_token(token)
    return kite


# The real broker factory, kept by identity: the synthetic test profile
# (backend/integration_profile.py) refuses to activate, and stops counting as
# active, while get_kite is still this function (i.e. no FakeKite installed).
PRODUCTION_GET_KITE = get_kite


def require_kite() -> KiteConnect:
    # Parameterless by design: there is no account-selection UI yet, so this
    # always resolves to the single default account. Once multi-account UI
    # exists, this becomes Depends(lambda: require_kite(account_id)).
    try:
        return get_kite()
    except NoActiveSessionError:
        raise HTTPException(
            status_code=401,
            detail={
                "error_code": "NO_SESSION",
                "message": "Not connected to Zerodha. Please log in.",
            },
        )
    except kite_rest_proxy.KiteRestProxyError as e:
        # Fail closed: a configured proxy that cannot be used is never
        # replaced by a direct connection.
        raise HTTPException(
            status_code=503,
            detail={"error_code": "KITE_REST_PROXY_INVALID", "message": str(e)},
        )


def require_trading_account(kite: KiteConnect = Depends(require_kite)) -> KiteConnect:
    # Hard server-side gate for every ticket/order/execution endpoint: rejects
    # with 403 regardless of what the UI shows or hides.
    try:
        get_trading_account()
    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "NOT_TRADING_ACCOUNT",
                "message": "This account is not enabled for trading.",
            },
        )
    return kite
