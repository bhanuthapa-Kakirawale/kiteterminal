"""PK-18: local/private deployment security hardening.

- Host validation (DNS rebinding) on every HTTP request and WebSocket, reads included.
- The runtime launcher: loopback bind, reload off, one worker, proxy headers off by default.
- Secrets: token stored only encrypted, never in an API response, redacted from logs and errors.
- Security headers, CSP, anti-framing and no-store on account data.
- Frontend dependencies vendored, pinned and SRI-checked.

Everything is offline: FakeKite, a throwaway DB and fake KiteConnect classes.
The PK-5 write policy is covered by test_csrf_protection.py and still applies
once a Host is accepted.
"""

import base64
import hashlib
import logging
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend import kite_client as _kite_client_module
from conftest import TERMINAL_ORIGIN, displayed_mode, set_mode, ticket_row
from test_baseline_api import _manual_ticket

# Captured at import, before the env fixture replaces it with the FakeKite factory.
REAL_GET_KITE = _kite_client_module.get_kite

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
APP_HEADER = {"X-Kite-Terminal": "1"}
EVIL = "http://evil.example:8000"
BROKER_WRITES = ("place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt")


def _bare(base_url=TERMINAL_ORIGIN, **kwargs):
    from backend.main import app
    return TestClient(app, base_url=base_url, **kwargs)


def _is_host_reject(r):
    return r.status_code == 400 and r.json()["detail"]["error_code"] == "INVALID_HOST"


def _ticket_count(env):
    conn = env.db.get_connection()
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM trade_tickets").fetchone()["n"]
    finally:
        conn.close()


# ==================================================================== PK-18A Host

@pytest.mark.parametrize("base", ["http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:8765",
                                  "http://LOCALHOST:8000", "http://127.0.0.1"])
def test_local_host_read_allowed(env, base):
    r = _bare(base).get("/api/holdings")
    assert r.status_code == 200, (base, r.text[:200])


@pytest.mark.parametrize("value,expected", [
    ("127.0.0.1:8000", ("127.0.0.1", 8000)),
    ("localhost", ("localhost", None)),
    ("[::1]:8000", ("::1", 8000)),
    ("[0:0:0:0:0:0:0:1]", ("::1", None)),
    ("Terminal.LAN:443", ("terminal.lan", 443)),
    ("", None),
    (None, None),
    ("evil.example:8000/path", None),
    ("user@127.0.0.1:8000", None),
    ("127.0.0.1:8000 ", None),
    ("127.0.0.1 :8000", None),
    ("localhost.", None),                 # trailing dot is a different origin to a browser
    ("local..host", None),
    ("-bad.example", None),
    ("127.0.0.1:0", None),
    ("127.0.0.1:99999", None),
    ("127.0.0.1:80:80", None),
    ("[::1", None),
    ("[not-ipv6]:80", None),
    ("lоcalhost", None),                  # Cyrillic 'о'
    ("localhost%00", None),
    ("localhost\r\nX-Injected: 1", None),
])
def test_parse_host(value, expected):
    from backend.security import parse_host
    assert parse_host(value) == expected


def test_ipv6_loopback_is_an_allowed_host():
    # Starlette's TestClient cannot address an IPv6 host, so the policy is
    # checked on a raw scope.
    from backend.security import host_rejection_reason
    assert host_rejection_reason({"headers": [(b"host", b"[::1]:8000")]}) is None


@pytest.mark.parametrize("path", ["/api/holdings", "/api/positions", "/api/tickets", "/api/journal/stats",
                                  "/api/risk/settings", "/api/session/status", "/api/account-safety/status",
                                  "/", "/static/app.js"])
def test_hostile_host_read_blocked(env, path):
    r = _bare(EVIL).get(path)
    assert _is_host_reject(r), (path, r.status_code, r.text[:200])
    assert env.kite.calls == []  # no route ran, so the broker was never asked


def test_hostile_host_read_blocked_even_with_terminal_origin(env):
    r = _bare(EVIL).get("/api/holdings", headers={"Origin": TERMINAL_ORIGIN, "Referer": TERMINAL_ORIGIN + "/"})
    assert _is_host_reject(r)


@pytest.mark.parametrize("headers", [
    {},
    APP_HEADER,
    {**APP_HEADER, "Origin": EVIL},
    # A loopback Origin on the Host's port satisfies the PK-5 Origin check, so
    # only the Host guard stands between this request and the route.
    {**APP_HEADER, "Origin": TERMINAL_ORIGIN},
], ids=["no_header", "header_no_origin", "header_rebinding_origin", "header_loopback_origin"])
def test_hostile_host_ticket_post_blocked(env, headers):
    before = _ticket_count(env)
    r = _bare(EVIL).post("/api/tickets", json=_manual_ticket(), headers=headers)
    assert _is_host_reject(r)
    assert _ticket_count(env) == before
    assert [n for n, _ in env.kite.calls if n in BROKER_WRITES] == []


def test_hostile_host_confirm_places_nothing(env, monkeypatch):
    from backend import market
    monkeypatch.setattr(market, "is_market_open", lambda at=None: True)
    set_mode(env.db, "live")
    tid = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]["id"]
    r = _bare(EVIL).post(f"/api/tickets/{tid}/confirm", json=displayed_mode(env.db),
                         headers={**APP_HEADER, "Origin": TERMINAL_ORIGIN})
    assert _is_host_reject(r)
    assert env.kite.calls_to("place_order") == []
    assert ticket_row(env.db, tid)["status"] == "pending_confirmation"


def test_dns_rebinding_write_is_rejected_by_host(env):
    # Formerly a PK-5 Origin case: Host and Origin both evil.example:8000.
    r = _bare(EVIL).post("/api/ui-prefs", json={"theme": "dark"}, headers={**APP_HEADER, "Origin": EVIL})
    assert _is_host_reject(r)


@pytest.mark.parametrize("forwarded", [
    {"X-Forwarded-Host": "127.0.0.1:8000"},
    {"Forwarded": "host=127.0.0.1:8000;proto=http"},
    {"X-Forwarded-Host": "localhost:8000", "X-Forwarded-Proto": "http", "X-Forwarded-For": "127.0.0.1"},
])
def test_forwarded_host_cannot_bypass(env, forwarded):
    r = _bare(EVIL).get("/api/holdings", headers=forwarded)
    assert _is_host_reject(r)
    r = _bare(EVIL).post("/api/ui-prefs", json={"theme": "dark"}, headers={**APP_HEADER, **forwarded})
    assert _is_host_reject(r)


def test_forwarded_host_is_not_echoed(env):
    # On an approved Host a forwarded header is simply ignored: it cannot
    # smuggle a hostile name into the CSP or anything else.
    r = _bare().get("/", headers={"X-Forwarded-Host": "evil.example:8000"})
    assert r.status_code == 200
    assert "evil.example" not in r.headers["content-security-policy"]


def test_duplicate_host_headers_rejected(env):
    r = _bare().get("/api/holdings", headers=[("Host", "127.0.0.1:8000"), ("Host", "evil.example:8000")])
    assert _is_host_reject(r)


@pytest.mark.parametrize("host", ["evil.example:8000/x", "127.0.0.1:99999", "user@localhost:8000", "localhost."])
def test_malformed_host_rejected_end_to_end(env, host):
    r = _bare().get("/api/holdings", headers={"Host": host})
    assert _is_host_reject(r), (host, r.status_code)


@pytest.mark.parametrize("scope_type", ["http", "websocket"])
@pytest.mark.parametrize("headers", [[], [(b"host", b"")], [(b"host", b"\xff\xfe")],
                                     [(b"host", b"127.0.0.1"), (b"host", b"127.0.0.1")]],
                         ids=["missing", "empty", "binary", "duplicate_same"])
def test_guard_never_calls_the_app_for_bad_host(scope_type, headers):
    import asyncio
    from backend.security import TrustedHostGuard

    called, sent = [], []

    async def inner(scope, receive, send):
        called.append(scope)

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    scope = {"type": scope_type, "method": "GET", "path": "/api/holdings", "scheme": "http", "headers": headers}
    asyncio.run(TrustedHostGuard(inner)(scope, receive, send))
    assert called == []
    if scope_type == "http":
        assert sent[0]["status"] == 400
    else:
        assert sent == [{"type": "websocket.close", "code": 1008}]


def test_configured_allowed_host(env, monkeypatch):
    base = "http://terminal.lan:8000"
    assert _is_host_reject(_bare(base).get("/api/holdings"))
    monkeypatch.setenv("KITE_TERMINAL_ALLOWED_HOSTS", " terminal.lan , 10.8.0.2 ")
    assert _bare(base).get("/api/holdings").status_code == 200
    assert _bare("http://10.8.0.2:8000").get("/api/holdings").status_code == 200
    # The configured name is also a terminal origin for writes (same port).
    ok = _bare(base).post("/api/ui-prefs", json={"theme": "dark"}, headers={**APP_HEADER, "Origin": base})
    assert ok.status_code == 200, ok.text
    # Other names stay rejected.
    assert _is_host_reject(_bare(EVIL).get("/api/holdings"))


@pytest.mark.parametrize("value", ["*", "*.example", ".example", "evil.*"])
def test_wildcard_allowed_hosts_are_refused(env, monkeypatch, caplog, value):
    from backend import security
    security._allowed_from.cache_clear()  # the warning is logged when a value is first parsed
    monkeypatch.setenv("KITE_TERMINAL_ALLOWED_HOSTS", value)
    with caplog.at_level(logging.WARNING, logger="backend.security"):
        assert _is_host_reject(_bare(EVIL).get("/api/holdings"))
        assert _is_host_reject(_bare("http://sub.example:8000").get("/api/holdings"))
    assert "Ignoring invalid KITE_TERMINAL_ALLOWED_HOSTS entry" in caplog.text


def test_default_allowed_hosts_are_loopback_only(monkeypatch):
    from backend.security import allowed_hostnames
    monkeypatch.delenv("KITE_TERMINAL_ALLOWED_HOSTS", raising=False)
    assert allowed_hostnames() == {"127.0.0.1", "localhost", "::1"}


def test_login_works_on_approved_host(env):
    r = _bare().get("/login", follow_redirects=False)
    assert r.status_code in (302, 307) and "kite.zerodha.com" in r.headers["location"]


def test_login_blocked_on_hostile_host(env):
    assert _is_host_reject(_bare(EVIL).get("/login", follow_redirects=False))


@pytest.fixture
def fake_login(monkeypatch):
    """Replace the network parts of the Kite login round trip."""
    from backend import kite_client
    from backend.routes import auth

    issued = {"access_token": "LIVEtok3nSentinel0000000000000001", "public_token": "pubtok",
              "user_id": "DT0001", "user_name": "Demo Trader"}
    seen = {"request_tokens": [], "set_access_token": []}

    class FakeKiteConnect:
        def __init__(self, api_key):
            self.api_key = api_key

        def login_url(self):
            return f"https://kite.zerodha.com/connect/login?api_key={self.api_key}&v=3"

        def generate_session(self, request_token, api_secret):
            seen["request_tokens"].append(request_token)
            return dict(issued)

        def set_access_token(self, token):
            seen["set_access_token"].append(token)

    async def _no_after_login():
        return None

    monkeypatch.setattr(kite_client, "KiteConnect", FakeKiteConnect)
    monkeypatch.setattr(auth, "_after_login", _no_after_login)
    return issued, seen


def test_callback_works_on_approved_host(env, fake_login):
    issued, seen = fake_login
    r = _bare().get("/callback?request_token=reqTOKENsentinel42&status=success", follow_redirects=False,
                    headers={"Referer": "https://kite.zerodha.com/connect/finish"})
    assert r.status_code in (302, 307) and r.headers["location"] == "/"
    assert seen["request_tokens"] == ["reqTOKENsentinel42"]


def test_callback_blocked_on_hostile_host(env, fake_login):
    issued, seen = fake_login
    r = _bare(EVIL).get("/callback?request_token=reqTOKENsentinel42&status=success", follow_redirects=False)
    assert _is_host_reject(r)
    assert seen["request_tokens"] == []


# ==================================================================== PK-18E WebSocket

@pytest.fixture
def one_tick(monkeypatch):
    from backend.ticker import ticker_manager
    monkeypatch.setattr(ticker_manager, "add_listener", lambda q: q.put_nowait({"type": "tick", "n": 1}))
    monkeypatch.setattr(ticker_manager, "remove_listener", lambda q: None)


WS_OK = TERMINAL_ORIGIN.replace("http://", "ws://") + "/ws/ticks"
WS_EVIL = EVIL.replace("http://", "ws://") + "/ws/ticks"


@pytest.mark.parametrize("url,headers", [
    (WS_OK, {"Origin": TERMINAL_ORIGIN}),
    ("ws://localhost:8000/ws/ticks", {"Origin": "http://localhost:8000"}),
])
def test_ws_approved_host_and_origin_connects(env, one_tick, url, headers):
    with _bare().websocket_connect(url, headers=headers) as ws:
        assert ws.receive_json() == {"type": "tick", "n": 1}


def test_ws_approved_host_without_origin_is_refused(env, one_tick):
    # SEC-1: an approved Host is not enough on its own — a handshake with no
    # Origin (a non-browser client) is refused; browsers always send one.
    with pytest.raises(WebSocketDisconnect) as exc:
        with _bare().websocket_connect(WS_OK, headers={}) as ws:
            ws.receive_json()
    assert exc.value.code == 1008


@pytest.mark.parametrize("headers", [{}, {"Origin": TERMINAL_ORIGIN}, {"Origin": EVIL}],
                         ids=["no_origin", "loopback_origin", "rebinding_origin"])
def test_ws_hostile_host_refused(env, one_tick, headers):
    # With no Origin, or a loopback Origin on the Host's port, the PK-5
    # Origin check alone would let this through; the Host check must not.
    with pytest.raises(WebSocketDisconnect) as exc:
        with _bare().websocket_connect(WS_EVIL, headers=headers) as ws:
            ws.receive_json()
    assert exc.value.code == 1008


def test_ws_hostile_forwarded_host_refused(env, one_tick):
    with pytest.raises(WebSocketDisconnect) as exc:
        with _bare().websocket_connect(WS_EVIL, headers={"X-Forwarded-Host": "127.0.0.1:8000"}) as ws:
            ws.receive_json()
    assert exc.value.code == 1008


@pytest.mark.parametrize("origin", ["https://evil.example", "http://localhost:3000", "null"])
def test_ws_hostile_origin_refused(env, one_tick, origin):
    with pytest.raises(WebSocketDisconnect) as exc:
        with _bare().websocket_connect(WS_OK, headers={"Origin": origin}) as ws:
            ws.receive_json()
    assert exc.value.code == 1008


def test_ws_url_carries_no_credentials():
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    sockets = re.findall(r"new WebSocket\(([^)]*)\)", js)
    assert sockets == ["`${protocol}//${location.host}/ws/ticks`"], sockets


# ==================================================================== PK-18B runtime

def _resolve(env=None, argv=()):
    from backend.server import resolve_runtime
    return resolve_runtime(env or {}, list(argv))


def test_runtime_defaults_are_local_and_safe():
    config = _resolve()
    assert config.host == "127.0.0.1" and config.port == 8000
    assert config.reload is False and config.workers == 1
    assert config.trusted_proxy_ips == () and config.remote_bind is False


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2"])
def test_loopback_binds_need_no_opt_in(host):
    assert _resolve({"KITE_TERMINAL_HOST": host}).remote_bind is False


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.128.0.5", "terminal-vm", "34.93.1.2"])
def test_remote_bind_requires_explicit_opt_in(host):
    from backend.server import RuntimeConfigError
    with pytest.raises(RuntimeConfigError, match="KITE_TERMINAL_ALLOW_REMOTE_BIND"):
        _resolve({"KITE_TERMINAL_HOST": host})
    with pytest.raises(RuntimeConfigError):
        _resolve(argv=["--host", host])
    config = _resolve({"KITE_TERMINAL_HOST": host, "KITE_TERMINAL_ALLOW_REMOTE_BIND": "1"})
    assert config.remote_bind is True and config.host == host


def test_reload_is_explicit_dev_opt_in_only():
    assert _resolve().reload is False
    assert _resolve({"KITE_TERMINAL_DEV_RELOAD": "0"}).reload is False
    assert _resolve({"KITE_TERMINAL_DEV_RELOAD": "1"}).reload is True
    assert _resolve(argv=["--dev-reload"]).reload is True


def test_reload_with_remote_bind_is_refused():
    from backend.server import RuntimeConfigError
    with pytest.raises(RuntimeConfigError, match="reload"):
        _resolve({"KITE_TERMINAL_HOST": "0.0.0.0", "KITE_TERMINAL_ALLOW_REMOTE_BIND": "1",
                  "KITE_TERMINAL_DEV_RELOAD": "1"})


@pytest.mark.parametrize("workers", ["2", "4", "0"])
def test_unsafe_worker_count_is_refused(workers):
    from backend.server import RuntimeConfigError
    with pytest.raises(RuntimeConfigError, match="one application worker"):
        _resolve({"KITE_TERMINAL_WORKERS": workers})


@pytest.mark.parametrize("env,match", [
    ({"KITE_TERMINAL_PORT": "abc"}, "port"),
    ({"KITE_TERMINAL_PORT": "70000"}, "port"),
    ({"KITE_TERMINAL_TRUSTED_PROXY_IPS": "*"}, "proxy"),
    ({"KITE_TERMINAL_TRUSTED_PROXY_IPS": "proxy.example"}, "IP address"),
])
def test_bad_runtime_values_are_refused(env, match):
    from backend.server import RuntimeConfigError
    with pytest.raises(RuntimeConfigError, match=match):
        _resolve(env)


def test_trusted_proxy_is_explicit():
    assert _resolve({"KITE_TERMINAL_TRUSTED_PROXY_IPS": "127.0.0.1, 10.0.0.0/8"}).trusted_proxy_ips == (
        "127.0.0.1", "10.0.0.0/8")


@pytest.fixture
def launcher(monkeypatch):
    """Run backend.server.main() without starting a server or reading .env."""
    import os
    import dotenv
    import uvicorn
    from backend import server

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    for name in ("HOST", "PORT", "ALLOW_REMOTE_BIND", "DEV_RELOAD", "WORKERS", "TRUSTED_PROXY_IPS"):
        monkeypatch.delenv(f"KITE_TERMINAL_{name}", raising=False)
    for name in ("HOST", "PORT", "RELOAD", "WORKERS", "TRUSTED_PROXY_IPS"):
        monkeypatch.setenv(server.RUNTIME_PREFIX + name, "placeholder")  # restored after the test
    # PK-22E: the launcher sets a default log file; restored after the test.
    monkeypatch.setenv("KITE_TERMINAL_LOG_FILE", "placeholder")
    monkeypatch.delenv("KITE_TERMINAL_LOG_FILE")
    return server, calls, os.environ


def test_launcher_default_invocation(launcher):
    server, calls, environ = launcher
    assert server.main([]) == 0
    [(app, kw)] = calls
    assert app == "backend.main:app"
    assert kw["host"] == "127.0.0.1" and kw["port"] == 8000
    assert kw["reload"] is False and kw["workers"] == 1
    assert kw["proxy_headers"] is False and "forwarded_allow_ips" not in kw
    assert kw["server_header"] is False
    assert environ[server.RUNTIME_PREFIX + "HOST"] == "127.0.0.1"


def test_launcher_refuses_unsafe_start(launcher, monkeypatch, capsys):
    server, calls, _ = launcher
    monkeypatch.setenv("KITE_TERMINAL_HOST", "0.0.0.0")
    assert server.main([]) == 2
    assert calls == []
    assert "Refusing to start" in capsys.readouterr().err


def test_launcher_honours_trusted_proxy(launcher, monkeypatch):
    server, calls, _ = launcher
    monkeypatch.setenv("KITE_TERMINAL_TRUSTED_PROXY_IPS", "127.0.0.1")
    assert server.main([]) == 0
    kw = calls[0][1]
    assert kw["proxy_headers"] is True and kw["forwarded_allow_ips"] == "127.0.0.1"


@pytest.mark.parametrize("argv,remote_opt_in", [
    (["uvicorn", "backend.main:app", "--host", "0.0.0.0"], False),
    (["uvicorn", "backend.main:app", "--host=192.168.1.5"], False),
    (["uvicorn", "backend.main:app", "--workers", "2"], True),
    (["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--reload"], True),
])
def test_in_app_policy_refuses_unsafe_bare_uvicorn(argv, remote_opt_in):
    from backend.server import RuntimeConfigError, enforce_runtime_policy
    env = {"KITE_TERMINAL_ALLOW_REMOTE_BIND": "1"} if remote_opt_in else {}
    with pytest.raises(RuntimeConfigError):
        enforce_runtime_policy(env=env, argv=argv, allowed_hosts={"127.0.0.1"})


def test_in_app_policy_reads_web_concurrency():
    from backend.server import RuntimeConfigError, enforce_runtime_policy
    with pytest.raises(RuntimeConfigError):
        enforce_runtime_policy(env={"WEB_CONCURRENCY": "3"}, argv=["uvicorn", "backend.main:app"],
                               allowed_hosts={"127.0.0.1"})


def test_in_app_policy_summary_for_bare_uvicorn_reload(caplog, monkeypatch):
    from backend.server import enforce_runtime_policy
    monkeypatch.setenv("KITE_API_SECRET", "summarySECRETvalue123")
    with caplog.at_level(logging.INFO, logger="backend.server"):
        config = enforce_runtime_policy(env={}, argv=["uvicorn", "backend.main:app", "--reload", "--port", "8123"],
                                        allowed_hosts={"127.0.0.1", "localhost", "::1"})
    assert config.reload is True and config.port == 8123 and config.source == "unverified"
    text = caplog.text
    assert "Security summary: bind=127.0.0.1 port=8123" in text
    assert "remote_exposure=no" in text and "reload=yes" in text and "workers=1" in text and "mode=local" in text
    assert "Auto-reload is ON" in text and "outside `python -m backend.server`" in text
    assert "summarySECRETvalue123" not in text


def test_in_app_policy_trusts_launcher_values(caplog):
    from backend.server import enforce_runtime_policy
    env = {"KITE_TERMINAL_RUNTIME_HOST": "10.8.0.2", "KITE_TERMINAL_RUNTIME_PORT": "8000",
           "KITE_TERMINAL_RUNTIME_RELOAD": "0", "KITE_TERMINAL_RUNTIME_WORKERS": "1",
           "KITE_TERMINAL_RUNTIME_TRUSTED_PROXY_IPS": "", "KITE_TERMINAL_ALLOW_REMOTE_BIND": "1"}
    with caplog.at_level(logging.INFO, logger="backend.server"):
        config = enforce_runtime_policy(env=env, argv=["python"], allowed_hosts={"127.0.0.1", "localhost", "::1"})
    assert config.source == "launcher" and config.remote_bind is True
    assert "remote_exposure=yes" in caplog.text and "mode=private-remote" in caplog.text
    assert "REMOTE BIND" in caplog.text and "INVALID_HOST" in caplog.text


# ==================================================================== PK-18C secrets

SENTINEL_SECRET = "APIsecretSENTINEL7777777777777777"
SENTINEL_TOKEN = "ACCESStokenSENTINEL88888888888888"
SENTINEL_LEGACY = "LEGACYplaintextSENTINEL9999999999"


def _get_routes():
    from backend.main import app
    return sorted({route.path for route in app.routes
                   if isinstance(route, APIRoute) and "GET" in route.methods and route.path.startswith("/api/")})


def test_no_get_endpoint_returns_a_secret(env):
    """Every GET /api route in the live route table, with sentinel values in
    every stored credential. No response may contain any of them."""
    from backend import crypto
    secret_enc, token_enc = crypto.encrypt(SENTINEL_SECRET), crypto.encrypt(SENTINEL_TOKEN)
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE accounts SET api_secret_enc = ?, access_token_enc = ?", (secret_enc, token_enc))
        conn.execute("UPDATE sessions SET access_token = ?", (SENTINEL_LEGACY,))
        conn.commit()
    finally:
        conn.close()
    forbidden = (SENTINEL_SECRET, SENTINEL_TOKEN, SENTINEL_LEGACY, secret_enc, token_enc,
                 "api_secret", "access_token")
    client = _bare(raise_server_exceptions=False)
    routes = _get_routes()
    assert len(routes) > 30  # the crawl really covers the API
    for path in routes:
        r = client.get(re.sub(r"\{[^}]+\}", "1", path))
        leaked = [f for f in forbidden if f in r.text]
        assert leaked == [], (path, leaked)


def test_login_stores_token_only_encrypted(env, fake_login):
    from backend import crypto
    issued, _ = fake_login
    r = _bare().get("/callback?request_token=reqTOKENsentinel42&status=success", follow_redirects=False)
    assert r.headers["location"] == "/"
    conn = env.db.get_connection()
    try:
        session = conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        account = conn.execute("SELECT * FROM accounts WHERE is_default = 1").fetchone()
    finally:
        conn.close()
    assert session["access_token"] == "" and session["is_active"] == 1
    assert crypto.decrypt(account["access_token_enc"]) == issued["access_token"]
    assert env.client.get("/api/session/status").json()["connected"] is True


def test_get_kite_uses_the_encrypted_token(env, fake_login):
    issued, seen = fake_login
    _bare().get("/callback?request_token=reqTOKENsentinel42&status=success", follow_redirects=False)
    REAL_GET_KITE()
    assert seen["set_access_token"] == [issued["access_token"]]


def test_get_kite_without_encrypted_token_needs_login(env, fake_login):
    from backend.kite_client import NoActiveSessionError
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE accounts SET access_token_enc = NULL")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(NoActiveSessionError):
        REAL_GET_KITE()


def test_get_kite_with_undecryptable_token_needs_login(env, fake_login):
    from backend.kite_client import NoActiveSessionError
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE accounts SET access_token_enc = 'not-a-fernet-token'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(NoActiveSessionError):
        REAL_GET_KITE()


def test_logout_clears_the_encrypted_token(env):
    assert env.client.post("/api/logout").status_code == 200
    conn = env.db.get_connection()
    try:
        assert conn.execute("SELECT access_token_enc FROM accounts WHERE is_default = 1").fetchone()[0] is None
    finally:
        conn.close()


def test_scrub_migration_backfills_then_blanks_and_is_idempotent(env):
    from backend import crypto
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE accounts SET access_token_enc = NULL")
        conn.execute("UPDATE sessions SET is_active = 0")
        aid = conn.execute("SELECT id FROM accounts WHERE is_default = 1").fetchone()["id"]
        conn.execute("INSERT INTO sessions (access_token, login_time, account_id, is_active) "
                     "VALUES ('old-inactive-token', '2020-01-01T09:00:00+05:30', ?, 0)", (aid,))
        conn.execute("INSERT INTO sessions (access_token, login_time, account_id, is_active) "
                     "VALUES (?, '2026-01-01T09:00:00+05:30', ?, 1)", (SENTINEL_TOKEN, aid))
        conn.commit()
    finally:
        conn.close()

    env.db.init_db()
    conn = env.db.get_connection()
    try:
        tokens = {r[0] for r in conn.execute("SELECT access_token FROM sessions")}
        enc = conn.execute("SELECT access_token_enc FROM accounts WHERE id = ?", (aid,)).fetchone()[0]
    finally:
        conn.close()
    assert tokens == {""}
    assert crypto.decrypt(enc) == SENTINEL_TOKEN  # the active session is not logged out

    env.db.init_db()  # second run changes nothing
    conn = env.db.get_connection()
    try:
        assert {r[0] for r in conn.execute("SELECT access_token FROM sessions")} == {""}
        assert conn.execute("SELECT access_token_enc FROM accounts WHERE id = ?", (aid,)).fetchone()[0] == enc
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_scrub_migration_keeps_an_existing_encrypted_token(env):
    from backend import crypto
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE accounts SET access_token_enc = ?", (crypto.encrypt("the-real-current-token"),))
        conn.execute("UPDATE sessions SET access_token = 'stale-plaintext-copy'")
        conn.commit()
    finally:
        conn.close()
    env.db.init_db()
    conn = env.db.get_connection()
    try:
        enc = conn.execute("SELECT access_token_enc FROM accounts WHERE is_default = 1").fetchone()[0]
    finally:
        conn.close()
    assert crypto.decrypt(enc) == "the-real-current-token"


@pytest.mark.parametrize("text,secret,kept", [
    ("GET /callback?request_token=reqABC123xyz&status=success", "reqABC123xyz", "status=success"),
    ('{"access_token": "tokABCdef123456", "instrument_token": 408065}', "tokABCdef123456", "408065"),
    ("{'access_token': 'tokABCdef123456'}", "tokABCdef123456", "{'access_token'"),
    ("api_secret=s3cr3tValue&x=1", "s3cr3tValue", "x=1"),
    ("KITE_API_SECRET=s3cr3tValue", "s3cr3tValue", "KITE_API_SECRET="),
    ("Authorization: token myapikey:myACCESStoken99", "myACCESStoken99", "Authorization"),
    ("sent header token myapikey:myACCESStoken99 to kite", "myACCESStoken99", "to kite"),
    ("Cookie: session=abcdef0123456789", "abcdef0123456789", "Cookie"),
    ("wss://ws.kite.trade?api_key=k&access_token=wsTOKEN12345678", "wsTOKEN12345678", "api_key=k"),
    ("checksum=0f1e2d3c4b5a69788796a5b4c3d2e1f0", "0f1e2d3c4b5a69788796a5b4c3d2e1f0", "checksum="),
])
def test_redact_text(text, secret, kept):
    from backend.redaction import redact_text
    out = redact_text(text)
    assert secret not in out and "[REDACTED]" in out and kept in out, out


@pytest.mark.parametrize("text", [
    "GET /callback?request_token=reqABC123xyz&status=success HTTP/1.1",
    '{"access_token": "tok", "api_secret": "s"}',
    "Authorization: token key:tok123\nCookie: a=b",
    "KITE_TERMINAL_ENCRYPTION_KEY=abcdefgh",
])
def test_redaction_is_idempotent(text):
    # A record is filtered by its logger and again by the handler; the
    # server smoke test once showed "request_token=[REDACTED]]" from this.
    from backend.redaction import redact_text
    once = redact_text(text)
    assert redact_text(once) == once and "[REDACTED]]" not in once


@pytest.mark.parametrize("text", [
    "instrument_token=408065 tokens=3",
    '{"instrument_token": 2953217, "last_price": 3000.0}',
    "Starting KiteTicker for 12 instruments",
    "GTT trigger_id=9001 for TCS",
])
def test_redact_text_leaves_innocent_fields(text):
    from backend.redaction import redact_text
    assert redact_text(text) == text


def test_registered_and_env_secrets_are_redacted_by_value(monkeypatch):
    from backend.redaction import redact_text, register_secret
    register_secret("RegisteredValue0123456789")
    monkeypatch.setenv("KITE_API_SECRET", "EnvSecretValue987654")
    out = redact_text("x RegisteredValue0123456789 y EnvSecretValue987654 z")
    assert out == "x [REDACTED] y [REDACTED] z"
    register_secret("short")  # too short to redact safely: ignored
    assert redact_text("a short word") == "a short word"


def test_uvicorn_access_log_hides_request_token():
    from uvicorn.logging import AccessFormatter
    from backend.redaction import SecretRedactingFilter
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:50000", "GET", "/callback?request_token=reqLOGGEDtoken77&status=success", "1.1", 307), None)
    assert SecretRedactingFilter().filter(record) is True
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False).format(record)
    assert "reqLOGGEDtoken77" not in line
    assert "/callback?request_token=[REDACTED]&status=success" in line and "307" in line


def test_installed_redaction_covers_uvicorn_and_app_loggers(caplog):
    from backend.redaction import install_log_redaction
    install_log_redaction()  # also attaches to caplog's handler, as it does to real handlers at startup
    with caplog.at_level(logging.INFO):
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET",
                                                 "/callback?request_token=reqVIAlogger55&status=success", "1.1", 307)
        logging.getLogger("backend.routes.auth").warning("exchange failed for access_token=tokVIAlogger66")
    assert "reqVIAlogger55" not in caplog.text and "tokVIAlogger66" not in caplog.text
    assert caplog.text.count("[REDACTED]") == 2 and "[REDACTED]]" not in caplog.text


def test_exception_traceback_is_redacted(caplog):
    from backend.redaction import install_log_redaction, register_secret
    install_log_redaction()
    register_secret("TracebackToken1234567")
    with caplog.at_level(logging.ERROR):
        try:
            raise RuntimeError("Kite said: access_token=TracebackToken1234567 invalid")
        except RuntimeError as e:
            logging.getLogger("backend.kite_client").exception("login failed: %s", e)
    assert "TracebackToken1234567" not in caplog.text
    assert "Traceback" in caplog.text and "RuntimeError" in caplog.text


def test_unhandled_exception_leaks_nothing(env, monkeypatch):
    from backend.main import app
    from backend.routes import auth
    assert app.debug is False

    def boom():
        raise RuntimeError(r"C:\Users\someone\secret\kite_terminal.db access_token=tokLEAK123456789")
    monkeypatch.setattr(auth, "get_default_account", boom)
    r = _bare(raise_server_exceptions=False).get("/api/session/status")
    assert r.status_code == 500
    assert r.text == "Internal Server Error"


def test_error_detail_is_redacted(env, monkeypatch):
    from kiteconnect.exceptions import GeneralException
    from backend.redaction import register_secret
    register_secret("DetailToken987654321")

    def failing_holdings():
        raise GeneralException("Upstream rejected access_token=DetailToken987654321 (token apikey:DetailToken987654321)")
    monkeypatch.setattr(env.kite, "holdings", failing_holdings)
    r = env.client.get("/api/holdings")
    assert r.status_code == 502 and r.json()["detail"]["error_code"] == "KITE_API_ERROR"
    assert "DetailToken987654321" not in r.text and "[REDACTED]" in r.text


def test_env_example_has_placeholders_only():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    values = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    assert values == {"KITE_API_KEY": "your_kite_api_key", "KITE_API_SECRET": "your_kite_api_secret"}
    assert not _FERNET_KEY.search(text)


def _git(*args):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        pytest.skip(f"git {args[0]} unavailable: {result.stderr[:200]}")
    return result


_FERNET_KEY = re.compile(r"(?<![A-Za-z0-9_+/=-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_+/=-])")
_SECRET_ASSIGN = re.compile(r"(?m)^\s*(KITE_API_SECRET|KITE_TERMINAL_ENCRYPTION_KEY)\s*=\s*(\S*)")
_KITE_TOKEN_ASSIGN = re.compile(r"(?i)access_token[\"']?\s*[:=]\s*[\"']([A-Za-z0-9]{32})[\"']")
_PLACEHOLDER = re.compile(r"^(|your_\w*|<[^>]*>|\$\{?\w+\}?)$")


def test_repository_contains_no_live_secrets():
    listed = _git("ls-files", "--cached", "--others", "--exclude-standard").stdout.splitlines()
    assert listed, "git listed no files"
    found = []
    for rel in listed:
        path = ROOT / rel
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if _FERNET_KEY.search(text):
            found.append((rel, "fernet-key-shaped value"))
        for name, value in _SECRET_ASSIGN.findall(text):
            if not _PLACEHOLDER.match(value):
                found.append((rel, f"{name} assignment"))
        if _KITE_TOKEN_ASSIGN.search(text):
            found.append((rel, "access_token literal"))
    assert found == []


@pytest.mark.parametrize("path,ignored", [
    (".env", True), (".env.local", True), (".env.example", False),
    ("backend/kite_terminal.db", True), ("backend/kite_terminal.db-wal", True),
    ("backend/kite_terminal.db.pre_pk18_bak", True), ("backend/anything.bak", True),
    ("scratch.sqlite3", True), ("captures/holdings.json", True), ("secrets/kite.key", True),
    ("backend/security.py", False),
])
def test_gitignore_covers_secret_and_runtime_files(path, ignored):
    result = _git("check-ignore", "-q", "--no-index", path)
    assert (result.returncode == 0) is ignored, path


# ==================================================================== PK-18F headers

def test_security_headers_on_the_ui_page(env):
    r = _bare().get("/")
    assert r.status_code == 200
    h = r.headers
    csp = h["content-security-policy"]
    directives = dict(d.strip().split(" ", 1) for d in csp.split(";"))
    assert directives["frame-ancestors"] == "'none'"
    assert directives["script-src"] == "'self'"
    assert directives["default-src"] == "'self'" and directives["object-src"] == "'none'"
    assert directives["base-uri"] == "'none'"
    assert "ws://127.0.0.1:8000" in directives["connect-src"]
    assert "unsafe-eval" not in csp
    assert h["x-frame-options"] == "DENY"
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "same-origin"
    assert "camera=()" in h["permissions-policy"]
    assert h["cross-origin-opener-policy"] == "same-origin"
    assert h["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", ["/api/holdings", "/api/positions", "/api/tickets", "/api/journal/stats?mode=live",
                                  "/api/risk/settings", "/api/session/status", "/api/execution/mode",
                                  "/api/account-safety/status"])
def test_sensitive_api_responses_are_no_store(env, path):
    r = env.client.get(path)
    assert r.status_code == 200, (path, r.text[:200])
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_auth_redirects_are_no_store(env, fake_login):
    assert _bare().get("/login", follow_redirects=False).headers["cache-control"] == "no-store"
    r = _bare().get("/callback?status=cancelled", follow_redirects=False)
    assert r.headers["cache-control"] == "no-store"


def test_static_assets_keep_their_caching(env):
    r = _bare().get("/static/app.js")
    assert r.status_code == 200
    assert "no-store" not in r.headers.get("cache-control", "")
    assert "etag" in r.headers
    assert r.headers["x-content-type-options"] == "nosniff"


def test_rejections_carry_security_headers(env):
    host = _bare(EVIL).get("/api/holdings")
    csrf = _bare().post("/api/ui-prefs", json={}, headers={"Origin": "https://evil.example"})
    for r in (host, csrf):
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["cache-control"] == "no-store"
    # A rejected Host is never echoed into the policy.
    assert "evil.example" not in host.headers["content-security-policy"]


def test_swagger_ui_is_not_served(env):
    # FastAPI's docs pages load JavaScript from a public CDN.
    assert _bare().get("/docs").status_code == 404
    assert _bare().get("/redoc").status_code == 404


# ==================================================================== PK-18D dependencies

INDEX = FRONTEND / "index.html"
_ATTR = re.compile(r'([a-zA-Z-]+)\s*=\s*"([^"]*)"')


def _tags():
    html = INDEX.read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r"<(script|link)\b([^>]*)>", html, re.I):
        out.append((m.group(1).lower(), dict((k.lower(), v) for k, v in _ATTR.findall(m.group(2)))))
    return html, out


def test_no_external_or_inline_scripts():
    html, tags = _tags()
    scripts = [attrs for kind, attrs in tags if kind == "script"]
    assert scripts, "no script tags found"
    for attrs in scripts:
        assert attrs.get("src", "").startswith("/static/"), attrs
    # Every <script> has a src and an empty body: CSP script-src 'self' needs no inline code.
    assert re.findall(r"<script\b[^>]*>\s*(?!</script>)\S", html, re.I) == []
    for kind, attrs in tags:
        if kind == "link":
            href = attrs.get("href", "")
            assert href.startswith("/static/") or href.startswith("data:"), attrs


def test_no_inline_event_handlers_or_javascript_urls():
    for path in (INDEX, FRONTEND / "app.js"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"""<[^>]*\son[a-z]+\s*=\s*["']""", text, re.I), path
        assert "javascript:" not in text.lower(), path


def test_every_static_reference_resolves():
    _, tags = _tags()
    refs = [attrs.get("src") or attrs.get("href") for _, attrs in tags]
    refs = [r for r in refs if r and r.startswith("/static/")]
    assert len(refs) >= 5
    for ref in refs:
        assert (FRONTEND / ref.removeprefix("/static/")).is_file(), ref


def test_vendored_scripts_are_pinned_and_integrity_checked():
    _, tags = _tags()
    vendored = [attrs for kind, attrs in tags if kind == "script" and attrs["src"].startswith("/static/vendor/")]
    assert {Path(a["src"]).name for a in vendored} == {
        "Sortable-1.15.2.min.js", "lightweight-charts-5.2.1.standalone.production.js"}
    for attrs in vendored:
        assert re.search(r"-\d+\.\d+\.\d+[.-]", Path(attrs["src"]).name), attrs["src"]  # exact version in the name
        data = (FRONTEND / attrs["src"].removeprefix("/static/")).read_bytes()
        expected = "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()
        assert attrs.get("integrity") == expected, attrs["src"]


def test_frontend_code_references_no_external_origin():
    for name in ("app.js", "theme-init.js", "style.css"):
        text = (FRONTEND / name).read_text(encoding="utf-8")
        assert not re.search(r"https?://", text), name
        assert "@import" not in text and "import(" not in text, name
