"""Request-level security: Host validation, cross-origin write protection, the
per-launch terminal credential and response security headers.

Four pure ASGI middlewares, registered in ``backend/main.py``. They run in
this order, outermost first, and all of them before routing (the fourth,
``TerminalCredentialGuard`` (SEC-1 P1-F), runs after the three below and is
described with the credential further down):

1. ``SecurityHeadersMiddleware`` (PK-18) adds CSP, anti-framing, nosniff,
   referrer and cache headers to every HTTP response, rejections included.
2. ``TrustedHostGuard`` (PK-18) rejects any HTTP request or WebSocket
   handshake whose ``Host`` header is missing, malformed, duplicated or not an
   allowed hostname. This protects *reads* as well as writes against DNS
   rebinding: a page at ``http://evil.example:8000`` whose name resolves to
   127.0.0.1 is same-origin to itself, so the browser would let it read
   ``GET /api/holdings``; its requests carry ``Host: evil.example:8000`` and
   are refused here before any route runs.
3. ``TerminalOriginGuard`` (PK-5, audit P0-8) rejects cross-origin writes.

Allowed hostnames are ``localhost``, ``127.0.0.1`` and ``::1`` plus any
listed in ``KITE_TERMINAL_ALLOWED_HOSTS`` (comma-separated; wildcards are
refused). Any port is accepted for an allowed name: rebinding is defeated by
the name, and the Origin check below ties a write's Origin port to the Host
port. ``X-Forwarded-Host``, ``Forwarded`` and ``X-Forwarded-Proto`` are never
read by these guards, so a client cannot talk its way past them.

Write policy (PK-5, tightened by SEC-1) for every HTTP request whose method
is not GET/HEAD/OPTIONS:

1. The ``X-Kite-Terminal: 1`` header is mandatory. It is not CORS-safelisted,
   so a foreign page cannot attach it without a preflight, and the app answers
   preflights with no ``Access-Control-Allow-*`` headers (there is no CORS
   middleware), so that preflight fails. It is a static value, so it is never
   treated as authentication.
2. If ``Origin`` is present it must be a terminal origin; if it is absent,
   a present ``Referer`` must be. The header never overrides a foreign origin.
3. SEC-1: with neither present the request is REJECTED. Browsers attach
   ``Origin`` to every same- and cross-origin non-GET fetch, so the UI always
   has one; before SEC-1 a header-bearing request without one was accepted as
   a "non-browser local client", which let any local process write with the
   static header alone. A non-browser client can still forge an Origin: this
   check only stops browser-borne attacks (see docs/DEPLOYMENT.md).

A *terminal origin* has the request's scheme, an allowed hostname and the
port of the request's Host header. Requiring an allowed hostname (rather than
"Origin == Host") is a second, independent defence against DNS rebinding.

The ``/ws/ticks`` handshake gets the Host check and the same Origin check.
The browser WebSocket API cannot send custom headers, so no header is
required there. Browsers always send Origin on a WebSocket handshake, so an
absent Origin is refused (SEC-1).

GET routes are not subject to the write policy, which is what keeps
``/login`` and the Kite ``/callback`` redirect (a top-level navigation from
kite.zerodha.com) working. They are still subject to the Host check, and an
``/api/`` read is refused when Fetch Metadata or Origin shows another origin
asked for it (P2-G R-F2). Every ``/api/`` read except a short public list also
needs the terminal credential, like a write (``read_requires_credential``).
"""

import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

APP_HEADER = "x-kite-terminal"
APP_HEADER_VALUE = "1"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ALLOWED_HOSTS_ENV = "KITE_TERMINAL_ALLOWED_HOSTS"
ERROR_CODE = "CSRF_PROTECTION_FAILED"
HOST_ERROR_CODE = "INVALID_HOST"

_HTTP_SCHEME = {"http": "http", "https": "https", "ws": "http", "wss": "https"}
_DEFAULT_PORT = {"http": 80, "https": 443}
_REJECTED_BODY = json.dumps({"detail": {
    "error_code": ERROR_CODE, "message": "Request rejected: not from the terminal UI.",
}}).encode()
_HOST_REJECTED_BODY = json.dumps({"detail": {
    "error_code": HOST_ERROR_CODE,
    "message": "Request rejected: the Host header is not an allowed terminal address.",
}}).encode()

# hostname[:port] or [ipv6][:port]. Letters, digits, dots and hyphens only:
# no userinfo, path, whitespace or percent-encoding can get through.
_HOST_RE = re.compile(r"(?:\[(?P<ip6>[0-9a-f:.]+)\]|(?P<name>[a-z0-9.-]+))(?::(?P<port>[0-9]{1,5}))?")
_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


# ---------------------------------------------------------------- Host policy

def parse_host(value: str | None) -> tuple[str, int | None] | None:
    """(hostname, port) of a Host header value, or None if malformed.

    The hostname is lower-cased; an IPv6 literal is returned without
    brackets in compressed form. A trailing dot, an empty label, a label
    starting or ending with a hyphen, and an out-of-range port are malformed.
    """
    if not value or not value.isascii():
        return None
    m = _HOST_RE.fullmatch(value.lower())
    if m is None:
        return None
    port = None
    if m["port"] is not None:
        port = int(m["port"])
        if not 1 <= port <= 65535:
            return None
    if m["ip6"] is not None:
        try:
            return str(ipaddress.IPv6Address(m["ip6"])), port
        except ValueError:
            return None
    name = m["name"]
    if len(name) > 253 or not all(_LABEL_RE.fullmatch(label) for label in name.split(".")):
        return None
    return name, port


def _parse_allowed_entry(item: str) -> str | None:
    try:
        return str(ipaddress.ip_address(item))  # bare IPv4/IPv6 literal
    except ValueError:
        pass
    parsed = parse_host(item)
    return parsed[0] if parsed else None


@functools.lru_cache(maxsize=16)
def _allowed_from(raw: str) -> frozenset[str]:
    extra = set()
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        name = None if "*" in item else _parse_allowed_entry(item)
        if name is None:
            logger.warning("Ignoring invalid %s entry %r (wildcards are not allowed)", ALLOWED_HOSTS_ENV, item[:100])
            continue
        extra.add(name)
    return LOOPBACK_HOSTS | extra


def allowed_hostnames() -> frozenset[str]:
    """Loopback names plus the configured ones. Read on every call (cached
    on the raw string) so a changed environment takes effect immediately."""
    return _allowed_from(os.environ.get(ALLOWED_HOSTS_ENV, ""))


def _host_values(scope) -> list[bytes]:
    return [v for k, v in scope.get("headers", []) if k.lower() == b"host"]


RUNTIME_PORT_ENV = "KITE_TERMINAL_RUNTIME_PORT"   # exported by backend.server's launcher


def expected_port() -> int | None:
    """The port this process serves on, when the supported launcher (or the demo server) said so.
    P2-G R-F3: the Host authority must then name exactly that port, not just an allowed name."""
    raw = os.environ.get(RUNTIME_PORT_ENV, "")
    return int(raw) if raw.isdigit() else None


def host_rejection_reason(scope) -> str | None:
    """None if the request's Host header is an allowed terminal address."""
    values = _host_values(scope)
    if not values:
        return "missing Host header"
    if len(values) > 1:
        return "multiple Host headers"
    value = values[0].decode("latin-1")
    parsed = parse_host(value)
    if parsed is None:
        return f"malformed Host {value[:100]!r}"
    if parsed[0] not in allowed_hostnames():
        return f"untrusted Host {value[:100]!r}"
    port = expected_port()
    scheme = _HTTP_SCHEME.get(scope.get("scheme", "http"), "http")
    if port is not None and (parsed[1] or _DEFAULT_PORT[scheme]) != port:
        return f"Host {value[:100]!r} does not name this terminal's port {port}"
    return None


def trusted_host(scope) -> str | None:
    """The request's Host header if it passed the Host policy, else None."""
    if host_rejection_reason(scope) is not None:
        return None
    return _host_values(scope)[0].decode("latin-1").lower()


# ---------------------------------------------------------------- Origin policy (PK-5)

def _split(url: str):
    """(scheme, hostname, port) of an absolute URL, or None if unparsable."""
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in _DEFAULT_PORT or not parts.hostname:
            return None
        return scheme, parts.hostname.lower(), parts.port or _DEFAULT_PORT[scheme]
    except ValueError:
        return None


def is_terminal_origin(origin: str, request_scheme: str, host_header: str) -> bool:
    scheme = _HTTP_SCHEME.get(request_scheme)
    theirs = _split(origin)
    ours = _split(f"{scheme}://{host_header}") if scheme and host_header else None
    if theirs is None or ours is None:
        return False
    return theirs[0] == ours[0] and theirs[1] in allowed_hostnames() and theirs[2] == ours[2]


def _headers(scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


_SAME_ORIGIN_FETCH_SITES = frozenset({"same-origin", "none"})


def _read_rejection_reason(scope, headers: dict) -> str | None:
    """P2-G R-F2: an /api/ read a browser makes from any other origin is refused before it runs.
    Fetch Metadata names the requesting site: another port on 127.0.0.1 is `same-site`, a foreign
    page `cross-site`; only `same-origin` (the UI) and `none` (typed by the user) pass. A present
    Origin must be a terminal origin. Non-browser clients send neither; they still need both
    credential factors (TerminalCredentialGuard). Other paths (the page, assets, /login and the
    /callback navigation that arrives from kite.zerodha.com) are not judged here."""
    if not scope.get("path", "").startswith("/api/"):
        return None
    site = headers.get("sec-fetch-site")
    if site is not None and site.lower() not in _SAME_ORIGIN_FETCH_SITES:
        return f"{site[:20]!r} read"
    origin = headers.get("origin")
    if origin is not None and not is_terminal_origin(origin, scope["scheme"], headers.get("host", "")):
        return f"foreign origin {origin[:100]!r}"
    return None


def rejection_reason(scope) -> str | None:
    """None if the request may proceed, else a short reason for the log."""
    headers = _headers(scope)
    if scope["type"] == "http":
        if scope["method"].upper() in SAFE_METHODS:
            return _read_rejection_reason(scope, headers)
        if headers.get(APP_HEADER) != APP_HEADER_VALUE:
            return "missing application header"
        # A present-but-empty Origin is judged, not treated as absent.
        source = headers["origin"] if "origin" in headers else headers.get("referer")
    else:
        source = headers.get("origin")
    if source is None:
        # SEC-1: never accepted on the strength of the static header alone.
        return "missing Origin and Referer" if scope["type"] == "http" else "missing Origin"
    if not is_terminal_origin(source, scope["scheme"], headers.get("host", "")):
        return f"foreign origin {source[:100]!r}"
    return None


# ---------------------------------------------------------------- middlewares

async def _send_json(send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
    ]})
    await send({"type": "http.response.body", "body": body})


class TrustedHostGuard:
    """Pure ASGI middleware: refuses a bad Host before any route runs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        reason = host_rejection_reason(scope)
        if reason is None:
            return await self.app(scope, receive, send)

        logger.warning("Host guard blocked %s %s: %s",
                       scope.get("method", "WEBSOCKET"), scope.get("path"), reason)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await _send_json(send, 400, _HOST_REJECTED_BODY)


class TerminalOriginGuard:
    """Pure ASGI middleware, so it sees websocket scopes and never buffers bodies."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        reason = rejection_reason(scope)
        if reason is None:
            return await self.app(scope, receive, send)

        logger.warning("CSRF guard blocked %s %s: %s",
                       scope.get("method", "WEBSOCKET"), scope.get("path"), reason)
        if scope["type"] == "websocket":
            # Closing before accept makes the server refuse the handshake (HTTP 403).
            await send({"type": "websocket.close", "code": 1008})
            return
        await _send_json(send, 403, _REJECTED_BODY)


# ---------------------------------------------------------------- per-launch terminal credential (SEC-1 P1-F)
#
# The Host and Origin checks above stop browser-borne attacks only: any local
# program can forge Host, Origin and the static header. The terminal
# credential is what a write (and the /ws/ticks handshake) must also present:
#
# - Issued ONLY when a Kite login completes (routes/auth.py /callback, after a
#   successful request-token exchange — the user's Kite password and 2FA), as
#   an HttpOnly, SameSite=Strict cookie. No URL, page, static asset or GET
#   response ever carries it, and JavaScript cannot read it.
# - Unpredictable (secrets.token_urlsafe(32)) and held only as a SHA-256
#   digest in this process's memory: an application restart forgets it, so a
#   credential from a previous launch never works again. A new login replaces
#   it; logout revokes it. One worker per process (server.REQUIRED_WORKERS),
#   so one in-memory value is the whole truth.
# - Compared in constant time (hmac.compare_digest on the digests).
# - Registered with backend.redaction, so it cannot appear in a log line or an
#   error detail even by accident; the guard itself logs only the reason.
#
# What it stops: any local process that can reach the port but cannot complete
# the user's Kite login nor read the user's browser profile — other Windows
# accounts, sandboxed or less-privileged processes — and a request replayed
# from a previous launch. What it cannot stop: malware running as the same
# Windows user (it can read the browser's cookie store or drive the browser),
# a browser extension with access to this host, a debugger/administrator.
# See docs/DEPLOYMENT.md.

CREDENTIAL_COOKIE = "kt_session"
CREDENTIAL_ERROR_CODE = "TERMINAL_SESSION_REQUIRED"
_CREDENTIAL_REJECTED_BODY = json.dumps({"detail": {
    "error_code": CREDENTIAL_ERROR_CODE,
    "message": "This terminal session is not authorized: log in to Zerodha again from this browser. "
               "Nothing was changed.",
}}).encode()

# ---- the port-isolated second factor (P2-G R-F3) ---------------------------------------------
#
# Cookies are scoped by host, never by port: `kt_session`, set by 127.0.0.1:8000, is sent by the
# browser to EVERY server on 127.0.0.1 (demonstrated in a real browser, tests/e2e_dmb_inbox.py).
# No cookie attribute changes that - not HttpOnly, SameSite, a prefix or a different port. A page
# served by another local process on another port can therefore collect the cookie and replay it.
#
# So the cookie is no longer sufficient on its own. Each login also mints a TERMINAL TOKEN. The
# /callback redirect that completes the login hands it to the page in the URL FRAGMENT
# (`/#kt_token=...`): the response goes only to the browser that passed the login-state check, a
# fragment is never sent to any server nor put in a Referer, and another origin cannot read this
# page's URL. frontend/terminal_auth.js (loaded first) moves it into localStorage, strips it from
# the address bar, and sends it as the `X-Kite-Terminal-Token` header (WebSocket: the
# `kt-token.<token>` subprotocol). Web storage is scoped by ORIGIN - scheme, host AND port - so a
# page on any other port cannot read it. A write, a sensitive read and a WebSocket handshake each
# need both factors; the server holds both only as digests. A cookie collected by another port is
# useless on its own. (A pickup endpoint was rejected: a page on another port polling its own
# server receives the cookie the moment it is set, and could race the real page to the pickup.)
#
# Still outside this boundary (docs/SECURITY_P2G_AUDIT.md): malware running as the same Windows
# user (it can read the browser profile, including web storage, or drive the browser), browser
# extensions with access to this origin, and administrators/debuggers.
TOKEN_HEADER = "x-kite-terminal-token"
TOKEN_FRAGMENT = "kt_token"
WS_SUBPROTOCOL = "kt.v1"
WS_TOKEN_PREFIX = "kt-token."
WS_REVOKED_CLOSE_CODE = 4401


def _digest(value: str | None) -> bytes | None:
    if not value:
        return None
    try:
        return hashlib.sha256(value.encode("ascii")).digest()
    except UnicodeEncodeError:
        return None


class _TerminalCredential:
    def __init__(self) -> None:
        self._digest: bytes | None = None          # the cookie
        self._token_digest: bytes | None = None    # the port-isolated terminal token
        self._generation = 0
        self._listeners: set = set()
        self._lock = threading.Lock()

    def issue(self) -> tuple[str, str]:
        token, terminal_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        from backend.redaction import register_secret
        register_secret(token)
        register_secret(terminal_token)
        with self._lock:
            self._digest = _digest(token)
            self._token_digest = _digest(terminal_token)
            self._generation += 1
        self._notify()
        return token, terminal_token

    def revoke(self) -> None:
        with self._lock:
            self._digest = self._token_digest = None
            self._generation += 1
        self._notify()

    @staticmethod
    def _matches(expected: bytes | None, presented: str | None) -> bool:
        candidate = _digest(presented)
        return expected is not None and candidate is not None and hmac.compare_digest(candidate, expected)

    def valid(self, presented: str | None) -> bool:
        with self._lock:
            expected = self._digest
        return self._matches(expected, presented)

    def token_valid(self, presented: str | None) -> bool:
        with self._lock:
            expected = self._token_digest
        return self._matches(expected, presented)

    def generation(self) -> int:
        with self._lock:
            return self._generation

    def add_listener(self, callback) -> None:
        with self._lock:
            self._listeners.add(callback)

    def remove_listener(self, callback) -> None:
        with self._lock:
            self._listeners.discard(callback)

    def _notify(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback()
            except Exception:
                logger.exception("terminal credential listener failed")


_credential = _TerminalCredential()


def issue_terminal_credentials() -> tuple[str, str]:
    """A new credential for this launch, replacing any previous one (and closing every WebSocket
    opened under it): (cookie value, terminal token). Called by the Kite login callback only (and
    by tests / the demo server), which hands both to the browser that completed the login."""
    return _credential.issue()


def issue_terminal_credential() -> str:
    """issue_terminal_credentials, for a caller that needs only the cookie value (tests)."""
    return _credential.issue()[0]


def revoke_terminal_credential() -> None:
    _credential.revoke()


def terminal_credential_valid(presented: str | None) -> bool:
    return _credential.valid(presented)


def terminal_token_valid(presented: str | None) -> bool:
    return _credential.token_valid(presented)


def authorized_landing_url(terminal_token: str) -> str:
    """Where a completed login sends the browser: the UI, with the terminal token in the fragment."""
    return f"/#{TOKEN_FRAGMENT}={terminal_token}"


def credential_generation() -> int:
    return _credential.generation()


def add_credential_listener(callback) -> None:
    """`callback()` runs (in the revoking thread) whenever the credential is issued or revoked."""
    _credential.add_listener(callback)


def remove_credential_listener(callback) -> None:
    _credential.remove_listener(callback)


def request_authorized(cookie: str | None, token: str | None) -> bool:
    """Both factors of the current launch: the cookie and the port-isolated terminal token."""
    return terminal_credential_valid(cookie) and terminal_token_valid(token)


def set_credential_cookie(response, token: str, secure: bool) -> None:
    """HttpOnly (no script can read it), SameSite=Strict (never sent on a
    cross-site request), whole-site path, Secure when served over https."""
    response.set_cookie(CREDENTIAL_COOKIE, token, httponly=True, samesite="strict", path="/", secure=secure)


def clear_credential_cookie(response, secure: bool) -> None:
    response.delete_cookie(CREDENTIAL_COOKIE, path="/", httponly=True, samesite="strict", secure=secure)


def request_cookie(scope, name: str) -> str | None:
    """The value of cookie `name` in the request (first occurrence), or None."""
    for key, value in scope.get("headers", []):
        if key.lower() != b"cookie":
            continue
        parsed = SimpleCookie()
        try:
            parsed.load(value.decode("latin-1"))
        except CookieError:
            continue
        if name in parsed:
            return parsed[name].value
    return None


# ---- sensitive reads (P2-G R-F2) -------------------------------------------------------------
#
# Every /api/ GET carries account, trading, session or research data except these three, so reads
# are DEFAULT-DENY: a new route is protected without anyone remembering to list it. Public:
#   /api/session/status  - whether a session exists and whether THIS browser is authorized; the
#                          user's name and login time only to an authorized browser;
#   /api/health          - liveness (status, database ok, version);
#   /api/config          - static alert thresholds from config.py.
# Outside /api/: the page and its static assets, /login, /callback (each guarded on its own) and
# FastAPI's schema. Classification: docs/SECURITY_P2G_AUDIT.md, R-F2.
PUBLIC_API_READS = frozenset({"/api/session/status", "/api/health", "/api/config"})


def read_requires_credential(path: str) -> bool:
    return path.startswith("/api/") and path not in PUBLIC_API_READS


def _presented_token(scope) -> str | None:
    """The terminal token: the header, or on a WebSocket (a browser cannot set headers there) the
    offered `kt-token.<token>` subprotocol."""
    if scope["type"] == "websocket":
        for protocol in scope.get("subprotocols") or ():
            if protocol.startswith(WS_TOKEN_PREFIX):
                return protocol[len(WS_TOKEN_PREFIX):]
    return _headers(scope).get(TOKEN_HEADER)


def credential_rejection_reason(scope) -> str | None:
    """None if the request needs no credential or carries both current factors."""
    path = scope.get("path", "")
    if scope["type"] == "http":
        method = scope["method"].upper()
        if method == "OPTIONS" or (method in SAFE_METHODS and not read_requires_credential(path)):
            return None
    presented = request_cookie(scope, CREDENTIAL_COOKIE)
    if presented is None:
        return "missing terminal credential"
    if not terminal_credential_valid(presented):
        return "invalid or expired terminal credential"
    token = _presented_token(scope)
    if token is None:
        return "missing terminal token"
    if not terminal_token_valid(token):
        return "invalid or expired terminal token"
    return None


class TerminalCredentialGuard:
    """Pure ASGI middleware, registered inside the Origin guard: a write or
    WebSocket handshake that passed the Host and Origin checks must also
    carry this launch's terminal credential."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        reason = credential_rejection_reason(scope)
        if reason is None:
            return await self.app(scope, receive, send)

        logger.warning("Credential guard blocked %s %s: %s",
                       scope.get("method", "WEBSOCKET"), scope.get("path"), reason)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await _send_json(send, 401, _CREDENTIAL_REJECTED_BODY)


# ---------------------------------------------------------------- login CSRF (P2-G)
#
# GET /callback exchanges whatever request_token it is given. Before P2-G any web page could
# navigate the browser there (a top-level GET is never Origin-checked) with a request token of
# the ATTACKER's choosing: the terminal would replace the user's Kite session with that one and
# hand this browser a fresh terminal credential for it ("login CSRF"). Now /login starts a login
# that only this browser can finish: a single-use, 10-minute login state, held as a digest in
# process memory and bound to the browser by an HttpOnly cookie. /callback refuses (before any
# token exchange) unless the browser presents a pending state; every callback consumes it.
# SameSite=Lax (not Strict): the callback is a top-level navigation FROM kite.zerodha.com, on
# which a Strict cookie would not be sent. Path=/callback: no other route ever receives it.
#
# R-F1 (closed): the cookie alone binds a callback to "a login this browser started", so a forced
# callback landing while the user's own login was pending passed. Now the state ALSO travels
# through Kite: /login sends it in Kite Connect's documented `redirect_params`, which Kite returns
# on the redirect URL ("An optional redirect_params param can be appended to public Kite login
# endpoint, that will be sent back to the redirect URL", kite.trade/docs/connect/v3/user). The
# callback must echo exactly the state in this browser's cookie. An attacker's forced callback
# cannot (the value is in an HttpOnly cookie and in a URL only the user's browser and Kite see).
# Only ONE login is pending at a time: starting a new one supersedes every earlier one, so a stale
# Kite tab from an older attempt can never complete. Single use, 10 minutes.
#
# Real-login validation is outstanding (no real Kite login has been performed). There is NO
# downgrade: the owner forbade a cookie-only fallback (sprint of 2026-11-04), so the former
# KITE_TERMINAL_LOGIN_STATE_ECHO=cookie-only switch is ignored (and reported as an error). If a real
# login shows Kite does not echo the state, the login keeps failing closed (/?login_error=1) and
# real-account use stays NO-GO until a secure, documented alternative is verified.

LOGIN_STATE_COOKIE = "kt_login_state"
LOGIN_STATE_PARAM = "kt_state"
LOGIN_STATE_TTL_SECONDS = 600
LOGIN_STATE_ECHO_ENV = "KITE_TERMINAL_LOGIN_STATE_ECHO"


def login_state_echo_required() -> bool:
    """Always True: the echoed state is required on every callback. A leftover downgrade setting is
    ignored, loudly - it never weakens the binding."""
    mode = os.environ.get(LOGIN_STATE_ECHO_ENV, "required").strip().lower()
    if mode != "required":
        logger.error("%s=%s is not supported and is ignored: the Kite callback must echo this browser's "
                     "login state (P2-G R-F1; no cookie-only fallback)", LOGIN_STATE_ECHO_ENV, mode[:20])
    return True


class _LoginStates:
    def __init__(self) -> None:
        self._pending: tuple[bytes, float] | None = None
        self._lock = threading.Lock()

    def begin(self, now: float) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._pending = (_digest(token), now + LOGIN_STATE_TTL_SECONDS)   # supersedes any earlier one
        return token

    def consume(self, presented: str | None, echoed: str | None, require_echo: bool, now: float) -> bool:
        """Consumes the pending login whatever the outcome, if `presented` is it."""
        digest = _digest(presented)
        if digest is None:
            return False
        with self._lock:
            if self._pending is None or not hmac.compare_digest(self._pending[0], digest):
                return False
            expires, self._pending = self._pending[1], None
        if expires <= now:
            return False
        if require_echo:
            echoed_digest = _digest(echoed)
            return echoed_digest is not None and hmac.compare_digest(echoed_digest, digest)
        return True

    def clear(self) -> None:
        with self._lock:
            self._pending = None


_login_states = _LoginStates()


def begin_login(now: float | None = None) -> str:
    """A new pending login for the browser that asked (GET /login); any earlier one is void."""
    return _login_states.begin(time.time() if now is None else now)


def consume_login_state(presented: str | None, echoed: str | None = None, now: float | None = None) -> bool:
    """True once, for the pending, unexpired login whose state is in this browser's cookie
    (`presented`) and was echoed back by Kite unchanged (`echoed`)."""
    return _login_states.consume(presented, echoed, login_state_echo_required(),
                                 time.time() if now is None else now)


def clear_login_states() -> None:
    _login_states.clear()


def set_login_state_cookie(response, token: str, secure: bool) -> None:
    response.set_cookie(LOGIN_STATE_COOKIE, token, max_age=LOGIN_STATE_TTL_SECONDS, httponly=True,
                        samesite="lax", path="/callback", secure=secure)


def clear_login_state_cookie(response, secure: bool) -> None:
    response.delete_cookie(LOGIN_STATE_COOKIE, path="/callback", httponly=True, samesite="lax", secure=secure)


# style-src keeps 'unsafe-inline' because app.js renders style="" attributes
# through innerHTML; scripts are 'self' only (no inline or external script).
_CSP_TEMPLATE = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'{ws}; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
_STATIC_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
)
# Account data must never sit in a browser or intermediary cache. The UI
# page itself revalidates, so an upgrade is picked up on the next load.
_NO_STORE_EXACT = frozenset({"/login", "/callback"})


def content_security_policy(host: str | None) -> str:
    # Older browsers do not treat ws:/wss: as covered by 'self'. The Host is
    # echoed only after it passed the Host policy, so it cannot inject.
    ws = f" ws://{host} wss://{host}" if host else ""
    return _CSP_TEMPLATE.format(ws=ws)


def cache_policy(path: str) -> bytes | None:
    if path.startswith("/api/") or path in _NO_STORE_EXACT:
        return b"no-store"
    if path == "/":
        return b"no-cache"
    return None


class SecurityHeadersMiddleware:
    """Adds browser security headers to every HTTP response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        added = [*_STATIC_HEADERS,
                 (b"content-security-policy", content_security_policy(trusted_host(scope)).encode("latin-1"))]
        cache = cache_policy(scope.get("path", ""))
        if cache is not None:
            added.append((b"cache-control", cache))
        replaced = {name for name, _ in added}

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in replaced]
                message = {**message, "headers": kept + added}
            await send(message)

        await self.app(scope, receive, send_with_headers)
