"""Cross-origin write protection (audit P0-8).

The terminal is a single-user app served on a loopback address. A browser
will send a "simple" cross-origin POST (a no-cors fetch or an auto-submitting
form) without any CORS preflight, so the same-origin policy alone does not
stop another web page from confirming a ticket or deleting a protective GTT.
This middleware rejects such requests before routing.

Policy for every HTTP request whose method is not GET/HEAD/OPTIONS:

1. The ``X-Kite-Terminal: 1`` header is mandatory. It is not CORS-safelisted,
   so a foreign page cannot attach it without a preflight, and the app answers
   preflights with no ``Access-Control-Allow-*`` headers (there is no CORS
   middleware), so that preflight fails.
2. If ``Origin`` is present it must be a terminal origin; if it is absent,
   a present ``Referer`` must be. The header never overrides a foreign origin.
3. With neither present the request is accepted: browsers always attach
   ``Origin`` to cross-origin non-GET requests, so a header-bearing request
   without one comes from a non-browser local client (tests, tooling).

A *terminal origin* has the request's scheme, a loopback hostname and the
port of the request's Host header. Deriving the port from Host keeps any
``--port`` working; requiring a loopback hostname (rather than "Origin ==
Host") defeats DNS rebinding, where a foreign name resolves to 127.0.0.1.

The ``/ws/ticks`` handshake gets the same Origin check. The browser WebSocket
API cannot send custom headers, so no header is required there, and an absent
Origin (a non-browser client) is accepted.

GET routes are never checked, which is what keeps ``/login`` and the Kite
``/callback`` redirect (a top-level navigation from kite.zerodha.com) working.
"""

import json
import logging
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

APP_HEADER = "x-kite-terminal"
APP_HEADER_VALUE = "1"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ERROR_CODE = "CSRF_PROTECTION_FAILED"

_HTTP_SCHEME = {"http": "http", "https": "https", "ws": "http", "wss": "https"}
_DEFAULT_PORT = {"http": 80, "https": 443}
_REJECTED_BODY = json.dumps({"detail": {
    "error_code": ERROR_CODE, "message": "Request rejected: not from the terminal UI.",
}}).encode()


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
    return theirs[0] == ours[0] and theirs[1] in LOOPBACK_HOSTS and theirs[2] == ours[2]


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
        return None
    if not is_terminal_origin(source, scope["scheme"], headers.get("host", "")):
        return f"foreign origin {source[:100]!r}"
    return None


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
        await send({"type": "http.response.start", "status": 403, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(_REJECTED_BODY)).encode()),
        ]})
        await send({"type": "http.response.body", "body": _REJECTED_BODY})
