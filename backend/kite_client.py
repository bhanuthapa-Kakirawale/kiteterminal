from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from kiteconnect import KiteConnect

from backend import crypto
from backend.accounts import get_account, get_default_account
from backend.db import get_connection

IST = timezone(timedelta(hours=5, minutes=30))


class NoActiveSessionError(Exception):
    """Raised when there is no active session, or the stored session is stale."""


def _api_key(account) -> str:
    return account["api_key"]


def _api_secret_decrypted(account) -> str:
    return crypto.decrypt(account["api_secret_enc"])


def _resolve_account(account_id: int | None):
    if account_id is None:
        return get_default_account()
    return get_account(account_id)


def build_login_url(account_id: int | None = None) -> str:
    account = _resolve_account(account_id)
    kite = KiteConnect(api_key=_api_key(account))
    return kite.login_url()


def exchange_request_token(request_token: str, account_id: int | None = None) -> dict:
    account = _resolve_account(account_id)
    kite = KiteConnect(api_key=_api_key(account))
    data = kite.generate_session(request_token, api_secret=_api_secret_decrypted(account))

    login_time = datetime.now(IST).isoformat()

    conn = get_connection()
    try:
        conn.execute(
            "UPDATE sessions SET is_active = 0 WHERE is_active = 1 AND account_id = ?",
            (account["id"],),
        )
        conn.execute(
            """
            INSERT INTO sessions (access_token, public_token, user_id, user_name, login_time, account_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                data["access_token"],
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
    kite = KiteConnect(api_key=_api_key(account))
    kite.set_access_token(row["access_token"])
    return kite


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
