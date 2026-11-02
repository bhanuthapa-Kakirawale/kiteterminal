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
kite.zerodha.com) working. They are still subject to the Host check.
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


def rejection_reason(scope) -> str | None:
    """None if the request may proceed, else a short reason for the log."""
    headers = _headers(scope)
    if scope["type"] == "http":
        if scope["method"].upper() in SAFE_METHODS:
            return None
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
    "message": "This terminal session is not authorized for changes: log in to Zerodha again from this browser. "
               "Nothing was changed.",
}}).encode()


class _TerminalCredential:
    def __init__(self) -> None:
        self._digest: bytes | None = None
        self._lock = threading.Lock()

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        from backend.redaction import register_secret
        register_secret(token)
        with self._lock:
            self._digest = hashlib.sha256(token.encode("ascii")).digest()
        return token

    def revoke(self) -> None:
        with self._lock:
            self._digest = None

    def valid(self, presented: str | None) -> bool:
        with self._lock:
            expected = self._digest
        if expected is None or not presented:
            return False
        try:
            candidate = hashlib.sha256(presented.encode("ascii")).digest()
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(candidate, expected)


_credential = _TerminalCredential()


def issue_terminal_credential() -> str:
    """A new credential for this launch, replacing any previous one. Called by
    the Kite login callback only (and by tests / the demo server)."""
    return _credential.issue()


def revoke_terminal_credential() -> None:
    _credential.revoke()


def terminal_credential_valid(presented: str | None) -> bool:
    return _credential.valid(presented)


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


def credential_rejection_reason(scope) -> str | None:
    """None if the request needs no credential or carries the current one."""
    if scope["type"] == "http" and scope["method"].upper() in SAFE_METHODS:
        return None
    presented = request_cookie(scope, CREDENTIAL_COOKIE)
    if presented is None:
        return "missing terminal credential"
    if not terminal_credential_valid(presented):
        return "invalid or expired terminal credential"
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
