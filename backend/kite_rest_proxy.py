"""NET-1: the explicit HTTP(S) proxy for Kite REST calls.

KITE_REST_PROXY=http://host:port routes every KiteConnect REST request
(login token exchange, reads, historical data, order and GTT mutations)
through that proxy, so they leave from its static IP. It is attached only
to KiteConnect clients (kite_client.create_kite_connect): KiteTicker's
WebSocket stays direct, and no process-wide HTTP_PROXY / HTTPS_PROXY is set.

Unset or blank: KiteConnect connects directly for reads, login and market
data only. Every broker write is refused locally on the direct route
(kite_client.DirectRouteKiteConnect), and Live Readiness blocks LIVE: the
proxy is mandatory for LIVE.

Set but malformed: refused. No client is built, and the launcher will not
start. A configured proxy is never silently replaced by a direct route.

Credentials in the URL are refused. The proxy is protected by its network
ACL (WireGuard source address), and refusing userinfo keeps a password out
of logs and API payloads. Error messages never echo the raw value.

Dependency-free so the launcher (backend/server.py) can validate it before
anything else is imported.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

ENV_VAR = "KITE_REST_PROXY"

DIRECT = "DIRECT"
PROXY = "PROXY"
INVALID = "INVALID"

_SCHEMES = ("http", "https")


class KiteRestProxyError(ValueError):
    """KITE_REST_PROXY is set but is not an acceptable proxy URL."""


@dataclass(frozen=True)
class RestProxy:
    scheme: str
    host: str
    port: int

    @property
    def _netloc(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self._netloc}"

    @property
    def display(self) -> str:
        """Sanitized endpoint for logs and the UI: 'host:port'."""
        return self._netloc

    @property
    def requests_proxies(self) -> dict:
        """The `proxies` mapping for requests / KiteConnect(proxies=...)."""
        return {"http": self.url, "https": self.url}


def _invalid(reason: str) -> KiteRestProxyError:
    return KiteRestProxyError(f"{ENV_VAR} is invalid: {reason}. Expected http://host:port or https://host:port.")


def parse(value: str | None) -> RestProxy | None:
    """None for unset/blank (direct), a RestProxy, or KiteRestProxyError."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    if any(ch.isspace() for ch in text):
        raise _invalid("it contains whitespace")
    if "://" not in text:
        raise _invalid("the scheme is missing")
    try:
        parts = urlsplit(text)
    except ValueError:
        raise _invalid("it is not a parseable URL")
    scheme = parts.scheme.lower()
    if scheme not in _SCHEMES:
        raise _invalid("only the http and https schemes are supported")
    if "@" in parts.netloc:
        raise _invalid("credentials in the proxy URL are not supported")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise _invalid("a path, query or fragment is not allowed")
    host = parts.hostname
    if not host:
        raise _invalid("the hostname is empty")
    try:
        port = parts.port
    except ValueError:
        raise _invalid("the port is not a number from 1 to 65535")
    if port is None:
        raise _invalid("the port is missing")
    if not 1 <= port <= 65535:
        raise _invalid("the port is not a number from 1 to 65535")
    return RestProxy(scheme=scheme, host=host, port=port)


def from_env(env: Mapping[str, str] | None = None) -> RestProxy | None:
    env = os.environ if env is None else env
    return parse(env.get(ENV_VAR))


def route_status(env: Mapping[str, str] | None = None) -> dict:
    """Sanitized route summary: never the raw value."""
    try:
        proxy = from_env(env)
    except KiteRestProxyError as e:
        return {"mode": INVALID, "endpoint": None, "error": str(e)}
    if proxy is None:
        return {"mode": DIRECT, "endpoint": None, "error": None}
    return {"mode": PROXY, "endpoint": proxy.display, "error": None}


def describe(status: dict) -> str:
    """One log line for a route_status()."""
    if status["mode"] == PROXY:
        return f"Kite REST proxy enabled: {status['endpoint']}"
    if status["mode"] == DIRECT:
        return f"Kite REST: direct, read-only ({ENV_VAR} not set): broker writes are refused and LIVE is disabled"
    return status["error"]
