import logging
import os
from datetime import datetime
from types import MethodType

from cryptography.fernet import InvalidToken
from fastapi import Depends, HTTPException
from kiteconnect import KiteConnect
from kiteconnect.exceptions import PermissionException

from backend import crypto, instance_lock, kite_rest_proxy
from backend.accounts import get_account, get_default_account, get_trading_account
from backend.db import get_connection
from backend.redaction import register_secret

logger = logging.getLogger(__name__)

# The SDK class itself, bound at import: tests and the demo replace the module name KiteConnect
# with stand-ins, and only a real client is ever narrowed (_narrow).
_SDK_KITE_CONNECT = KiteConnect

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


# ---- broker writes switch (read-only verification, runbook section 1.6) ----------------------
#
# KITE_TERMINAL_BROKER_WRITES=disabled builds every REST client so that it can only READ: the one
# non-GET call left open is the login's request-token exchange (route "api.token"), without which
# no session exists. Every other POST/PUT/DELETE - orders, GTTs, conversions, MF, token
# invalidation, order-margin calculation - is refused inside this process before any request is
# made, whoever calls it (a route, a background worker, Emergency Exit). The refusal is a
# PermissionException subclass, so the order paths record it as a definite rejection (nothing was
# sent), never as an UNKNOWN outcome. Unset or "enabled" keeps normal behaviour; ANY other value
# disables writes (a typo fails safe). The SDK's single request method (kiteconnect 5.x
# KiteConnect._request) is the choke point, so a new SDK write method is covered too.
BROKER_WRITES_ENV = "KITE_TERMINAL_BROKER_WRITES"
_WRITES_DISABLED_ALLOWED_ROUTES = frozenset({"api.token"})


class BrokerWritesDisabled(PermissionException):
    """A broker write refused locally: KITE_TERMINAL_BROKER_WRITES disables writes. Nothing was sent."""

    def __init__(self, route: str, method: str):
        super().__init__(f"Broker writes are disabled on this terminal ({BROKER_WRITES_ENV}): {method} {route} "
                         f"was refused locally and never sent to Kite.", code=403)


def broker_writes_enabled() -> bool:
    return os.environ.get(BROKER_WRITES_ENV, "enabled").strip().lower() in ("", "enabled")


class ReadOnlyKiteConnect(KiteConnect):
    """A KiteConnect that can log in and read, and nothing else."""

    def _request(self, route, method, *args, **kwargs):
        if str(method).upper() != "GET" and route not in _WRITES_DISABLED_ALLOWED_ROUTES:
            logger.warning("event=broker_write.refused route=%s method=%s: %s is not enabled",
                           route, str(method).upper(), BROKER_WRITES_ENV)
            raise BrokerWritesDisabled(route, str(method).upper())
        return super()._request(route, method, *args, **kwargs)


def _restrict_if_writes_disabled(kite: KiteConnect) -> KiteConnect:
    """The factory's client, narrowed to ReadOnlyKiteConnect when writes are disabled. The object is
    still the one KiteConnect(...) built above (same proxy route and session); only its class, and so
    its _request, changes. Construction stays literal in the factory (test_14d)."""
    if not broker_writes_enabled():
        _narrow(kite, ReadOnlyKiteConnect)
    return kite


def _narrow(kite, cls) -> None:
    """Swap a real KiteConnect's class for a restricted subclass (same object, same session and
    route). Only a real KiteConnect is narrowed: a stand-in that is not one (a test or demo double)
    keeps its own methods; giving it the SDK's request path would only break it."""
    if isinstance(kite, _SDK_KITE_CONNECT):
        kite.__class__ = cls


# ---- no direct route for broker writes (proxy mandatory for LIVE) ------------------------------
#
# Without KITE_REST_PROXY the client is built direct, for reads, market data, login and dry run
# only. Every broker write (an order, a GTT, a cancel, an order-margin calculation; anything that
# is not a GET, except the login token exchange) is refused inside this process before any request
# is made, whoever calls it: a route, a background worker, Emergency Exit. A PermissionException
# subclass, so the order paths record a definite refusal (nothing was sent), never an UNKNOWN. The
# writes-disabled switch above, when set, still wins (ReadOnlyKiteConnect).
class DirectRouteWritesRefused(PermissionException):
    """A broker write refused locally because no Kite REST proxy is configured. Nothing was sent."""

    def __init__(self, route: str, method: str):
        super().__init__(f"No Kite REST proxy is configured ({kite_rest_proxy.ENV_VAR}): {method} {route} was "
                         f"refused locally and never sent to Kite. Broker writes go only through the proxy; "
                         f"use Kite web or the Kite app directly if you must act now.", code=403)


class DirectRouteKiteConnect(KiteConnect):
    """A direct-route KiteConnect: it can log in and read, never write."""

    def _request(self, route, method, *args, **kwargs):
        if str(method).upper() != "GET" and route not in _WRITES_DISABLED_ALLOWED_ROUTES:
            logger.warning("event=broker_write.refused route=%s method=%s: no %s configured",
                           route, str(method).upper(), kite_rest_proxy.ENV_VAR)
            raise DirectRouteWritesRefused(route, str(method).upper())
        return super()._request(route, method, *args, **kwargs)


def _no_writes_on_direct_route(kite: KiteConnect) -> KiteConnect:
    _narrow(kite, DirectRouteKiteConnect)
    return kite


# ---- Phase 2 write fence: only the database owner may send a broker write --------------------------
#
# docs/PHASE_2_DESIGN_REVIEW.md §7.3. Every client the factory builds gets an instance-level _request
# that, for anything but a GET, first asks instance_lock.broker_write(): a process that is not the
# owner of the database (a second instance, a CLI, a test without the ownership grant, a process that
# lost its epoch or is shutting down) is refused with InstanceNotOwner, a PermissionException subclass
# (a definite rejection: nothing was sent), and the write is counted as in flight otherwise. It runs
# AHEAD of the class's own _request, so it composes with DirectRouteKiteConnect/ReadOnlyKiteConnect
# and also covers the plain proxied KiteConnect - and so every SDK write method, including the login
# token exchange and those called from the frozen execution.py and strategy_engine.py. The client's
# class is unchanged. A non-SDK broker (a test or demo double) gets the same check around its write
# methods (fence_broker); the paper broker checks it in its own write methods.
BROKER_WRITE_METHODS = (
    "place_order", "modify_order", "cancel_order", "exit_order", "place_gtt", "modify_gtt", "delete_gtt",
    "convert_position", "order_margins", "basket_order_margins", "place_mf_order", "cancel_mf_order",
    "place_mf_sip", "modify_mf_sip", "cancel_mf_sip", "invalidate_access_token", "invalidate_refresh_token",
    "generate_session", "renew_access_token",
)
_FENCE_MARK = "_kt_write_fence"


def _fenced_request(self, route, method, *args, **kwargs):
    request = type(self)._request  # resolved per call: the class may be narrowed after fencing
    if str(method).upper() == "GET":
        return request(self, route, method, *args, **kwargs)
    with instance_lock.broker_write(route, method):
        return request(self, route, method, *args, **kwargs)


def _guarded_write(broker, name: str, own):
    def write(*args, **kwargs):
        with instance_lock.broker_write(name, "WRITE"):
            target = own if own is not None else getattr(type(broker), name).__get__(broker, type(broker))
            return target(*args, **kwargs)
    write.__name__ = name
    write.__wrapped_broker_write__ = True
    return write


def fence_broker(broker):
    """Installs the write fence on this broker object (idempotent; identity and class unchanged)."""
    if vars(broker).get(_FENCE_MARK):
        return broker
    if isinstance(broker, _SDK_KITE_CONNECT):
        broker._request = MethodType(_fenced_request, broker)
    else:
        for name in BROKER_WRITE_METHODS:
            own = vars(broker).get(name)
            if own is None and not callable(getattr(type(broker), name, None)):
                continue
            setattr(broker, name, _guarded_write(broker, name, own))
    setattr(broker, _FENCE_MARK, True)
    return broker


# ---- U6 M1: no HTTP redirect is ever followed (docs/U6_MITIGATION_REPORT.md) -----------------------
#
# kiteconnect asks requests to follow redirects on every request (hard-coded), and requests follows 30:
# a 307/308 resends the same method and body (an order, a GTT) to the Location URL, and a 301/302/303
# turns it into another request, all without the caller ever seeing the 3xx. With max_redirects = 0 the
# first 3xx raises requests.TooManyRedirects before anything is resent. It is a RequestException, so a
# write's outcome is recorded as UNKNOWN (never retried), exactly like a timeout; reads fail as a broker
# read failure. Kite's REST API is not known to redirect any request (reads included).
def _no_redirects(kite):
    if isinstance(kite, _SDK_KITE_CONNECT):  # a test or demo stand-in has no requests session
        kite.reqsession.max_redirects = 0
    return kite


def create_kite_connect(api_key: str) -> KiteConnect:
    """NET-1: the one place a KiteConnect REST client is built. With
    KITE_REST_PROXY set, every request it makes (login, reads, historical
    data, orders, GTTs) goes through that proxy and nowhere else: an invalid
    value raises KiteRestProxyError before any client exists, and the session
    ignores environment proxy settings (HTTP(S)_PROXY, NO_PROXY, netrc) so
    nothing can bypass or redirect the configured route. Unset, the client is
    direct and read-only (DirectRouteKiteConnect). KiteTicker is not built
    here and stays direct."""
    proxy = kite_rest_proxy.from_env()
    if proxy is None:
        # Direct: reads only. No broker write ever leaves this machine without the proxy.
        return fence_broker(_restrict_if_writes_disabled(_no_writes_on_direct_route(
            _no_redirects(KiteConnect(api_key=api_key)))))
    kite = KiteConnect(api_key=api_key, proxies=proxy.requests_proxies)
    kite.reqsession.trust_env = False
    kite.reqsession.max_redirects = 0  # U6 M1: see _no_redirects
    return fence_broker(_restrict_if_writes_disabled(kite))


def _resolve_account(account_id: int | None):
    if account_id is None:
        return get_default_account()
    return get_account(account_id)


def build_login_url(account_id: int | None = None, state: str | None = None) -> str:
    """Kite's login URL. `state` (P2-G R-F1) travels in Kite Connect's documented `redirect_params`
    (a URL-encoded query string Kite sends back to the redirect URL) as `kt_state=<state>`."""
    account = _resolve_account(account_id)
    kite = create_kite_connect(_api_key(account))
    url = kite.login_url()
    if state:
        from urllib.parse import quote
        url += "&redirect_params=" + quote(f"kt_state={state}", safe="")
    return url


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
