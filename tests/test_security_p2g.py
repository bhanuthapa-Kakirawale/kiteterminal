"""P2-G security audit regressions (docs/SECURITY_P2G_AUDIT.md).

F1 (fixed in the readiness sprint): login CSRF - a forced /callback never exchanges a request token
or issues a terminal credential unless THIS browser started the pending login.
R-F1 (closed): the login state also travels through Kite (`redirect_params`) and must come back
unchanged; one pending login at a time, single use, 10 minutes.
R-F2 (closed): /api/ reads are default-deny - every route but three public ones needs both factors
of this launch's credential; cross-site / other-port browser reads are refused before routing.
R-F3 (mitigated): the host-scoped cookie is no longer sufficient alone - a port-isolated terminal
token (localStorage, sent as a header / WebSocket subprotocol) is required with it; the Host
authority must name this terminal's exact port.
R-F4 (closed): a logout or a new login closes every open /ws/ticks socket.
FakeKite, throwaway database; no real Kite login.
"""
import logging
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import TERMINAL_HEADERS, TERMINAL_ORIGIN, authorize_client, reauthorize, start_kite_login, \
    terminal_credential, terminal_token

KITE_REFERER = {"Referer": "https://kite.zerodha.com/connect/finish"}
CALLBACK = "/callback?request_token=reqATTACKER123&status=success"
WS_URL = TERMINAL_ORIGIN.replace("http://", "ws://") + "/ws/ticks"


@pytest.fixture
def exchanges(env, monkeypatch):
    from backend import kite_client
    from backend.routes import auth
    seen = []
    monkeypatch.setattr(kite_client, "exchange_request_token", lambda token: seen.append(token))
    real = kite_client.build_login_url

    class _FakeKiteConnect:                      # the real builder, on a fake KiteConnect
        def __init__(self, api_key, **kwargs):
            self.api_key = api_key

        def login_url(self):
            return f"https://kite.zerodha.com/connect/login?api_key={self.api_key}&v=3"
    monkeypatch.setattr(kite_client, "create_kite_connect", lambda api_key: _FakeKiteConnect(api_key))
    monkeypatch.setattr(kite_client, "build_login_url", real)

    async def _no_after_login():
        return None
    monkeypatch.setattr(auth, "_after_login", _no_after_login)
    return seen


def _browser(**kw):
    from backend.main import app
    return TestClient(app, base_url=TERMINAL_ORIGIN, **kw)


def _set_cookies(r) -> str:
    return "; ".join(v for k, v in r.headers.multi_items() if k.lower() == "set-cookie")


def _state(login) -> str:
    return re.search(r"kt_login_state=([A-Za-z0-9_\-]+)", _set_cookies(login)).group(1)


def _echoed(state: str) -> str:
    """The callback Kite sends after a login that carried redirect_params=kt_state%3D<state>."""
    return f"{CALLBACK}&kt_state={state}"


def _code(r):
    return r.json()["detail"]["error_code"]


# ============================================================ F1 + R-F1 login binding
def test_a_forced_callback_without_a_started_login_exchanges_nothing(exchanges):
    r = _browser().get(CALLBACK, headers=KITE_REFERER, follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/?login_error=1"
    assert exchanges == [] and "kt_session=" not in _set_cookies(r)


def test_login_sends_the_state_through_kites_documented_redirect_params(exchanges):
    login = _browser().get("/login", follow_redirects=False)
    state = _state(login)
    location = login.headers["location"]
    assert location.startswith("https://kite.zerodha.com/connect/login?api_key=")
    # URL-encoded query string, as Kite Connect documents it: redirect_params=some%3DX%26more%3DY
    assert location.endswith(f"&redirect_params=kt_state%3D{state}")
    header = _set_cookies(login).lower()
    assert "kt_login_state=" in header and "httponly" in header and "samesite=lax" in header
    assert "path=/callback" in header and "max-age=600" in header


def test_a_login_started_in_this_browser_completes_once(exchanges):
    browser = _browser()
    state = _state(browser.get("/login", follow_redirects=False))
    r = browser.get(_echoed(state), headers=KITE_REFERER, follow_redirects=False)
    assert r.headers["location"].startswith("/#kt_token=") and exchanges == ["reqATTACKER123"]
    assert "kt_session=" in _set_cookies(r)
    again = _browser()
    again.cookies.set("kt_login_state", state)                       # replaying the used state
    r = again.get(_echoed(state), follow_redirects=False)
    assert r.headers["location"] == "/?login_error=1" and exchanges == ["reqATTACKER123"]
    r = browser.get(_echoed(state), follow_redirects=False)          # the same browser, replayed
    assert r.headers["location"] == "/?login_error=1" and exchanges == ["reqATTACKER123"]


def test_r_f1_a_forced_callback_inside_the_users_pending_login_is_refused(exchanges):
    """Was the documented R-F1 residual: the user has started a login (cookie set); an attacker's
    page navigates the browser to /callback with the attacker's request token. Kite echoes the state
    only on the user's own redirect, so the forced callback carries none (or a guess) and is
    refused - and it consumes the pending login, so it cannot be retried."""
    for forced in (CALLBACK, CALLBACK + "&kt_state=" + "g" * 43):
        browser = _browser()
        start_kite_login(browser)
        assert browser.get(forced, follow_redirects=False).headers["location"] == "/?login_error=1"
    assert exchanges == []


def test_r_f1_the_echo_without_the_browsers_cookie_is_refused(exchanges):
    state = _state(_browser().get("/login", follow_redirects=False))
    stranger = _browser()                                            # holds no login-state cookie
    assert stranger.get(_echoed(state), follow_redirects=False).headers["location"] == "/?login_error=1"
    assert exchanges == []


def test_r_f1_a_new_login_supersedes_a_stale_one(exchanges):
    """Two Kite tabs: the older attempt can never complete once a newer one was started."""
    old_tab, new_tab = _browser(), _browser()
    old_state = _state(old_tab.get("/login", follow_redirects=False))
    new_state = _state(new_tab.get("/login", follow_redirects=False))
    assert old_tab.get(_echoed(old_state), follow_redirects=False).headers["location"] == "/?login_error=1"
    assert exchanges == []
    assert new_tab.get(_echoed(new_state), follow_redirects=False).headers["location"].startswith("/#kt_token=")
    assert exchanges == ["reqATTACKER123"]


def test_r_f1_a_state_echoed_into_another_browsers_flow_is_refused(exchanges):
    """The echoed state must equal THIS browser's cookie, not merely some pending state."""
    victim = _browser()
    state = _state(victim.get("/login", follow_redirects=False))
    victim.cookies.set("kt_login_state", state)
    assert victim.get(_echoed("x" * 43), follow_redirects=False).headers["location"] == "/?login_error=1"
    assert exchanges == []


def test_a_wrong_or_foreign_state_is_refused(exchanges):
    start_kite_login(_browser())                                     # someone else's pending login
    attacker = _browser()
    attacker.cookies.set("kt_login_state", "x" * 43)
    assert attacker.get(_echoed("x" * 43), follow_redirects=False).headers["location"] == "/?login_error=1"
    assert exchanges == []


def test_an_expired_state_is_refused(exchanges):
    from backend import security
    browser = _browser()
    token = security.begin_login(now=0.0)                            # started long ago
    browser.cookies.set("kt_login_state", token)
    assert browser.get(_echoed(token), follow_redirects=False).headers["location"] == "/?login_error=1"
    assert exchanges == []


def test_a_cancelled_login_consumes_the_state(exchanges):
    browser = _browser()
    token = start_kite_login(browser)
    assert browser.get(f"/callback?status=cancelled&kt_state={token}",
                       follow_redirects=False).headers["location"] == "/?login_error=1"
    assert browser.get(_echoed(token), follow_redirects=False).headers["location"] == "/?login_error=1"
    assert exchanges == []


def test_a_forced_callback_never_rotates_the_existing_credential(exchanges):
    from backend import security
    current = terminal_credential()
    _browser().get(CALLBACK, follow_redirects=False)
    assert security.terminal_credential_valid(current)


def test_only_one_login_is_pending_at_a_time(env):
    from backend import security
    tokens = [security.begin_login(now=1000.0 + i) for i in range(5)]
    assert [security.consume_login_state(t, t, now=1010.0) for t in tokens] == [False] * 4 + [True]


def test_the_cookie_only_downgrade_is_explicit_and_logged(exchanges, monkeypatch, caplog):
    """If a real Kite login shows the parameter is not echoed, the operator may fall back to the
    earlier cookie-only binding - explicitly, never silently."""
    from backend import security
    monkeypatch.setenv(security.LOGIN_STATE_ECHO_ENV, "cookie-only")
    browser = _browser()
    start_kite_login(browser)
    with caplog.at_level(logging.WARNING, logger="backend.security"):
        assert browser.get(CALLBACK, follow_redirects=False).headers["location"].startswith("/#kt_token=")
    assert "R-F1 downgrade" in caplog.text
    assert _browser().get(CALLBACK, follow_redirects=False).headers["location"] == "/?login_error=1"


# ============================================================ R-F2 sensitive reads
def _get_routes():
    from backend.main import app
    return sorted(route.path for route in app.routes if isinstance(route, APIRoute) and "GET" in route.methods)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def test_r_f2_the_read_inventory_is_classified():
    """Every GET route is either public by design or under the default-deny /api/ rule. A new
    route is protected without being listed; a new PUBLIC one needs this test (and the audit) changed."""
    from backend import security
    public = {p for p in _get_routes() if not security.read_requires_credential(p)}
    assert public == {"/", "/login", "/callback", "/api/session/status", "/api/health", "/api/config"}
    assert security.PUBLIC_API_READS == {"/api/session/status", "/api/health", "/api/config"}


def test_r_f2_every_sensitive_read_refuses_an_anonymous_caller(env):
    from backend import security
    anonymous = _browser()
    sensitive = [p for p in _get_routes() if security.read_requires_credential(p)]
    assert len(sensitive) >= 50
    for path in sensitive:
        r = anonymous.get(_concrete(path))
        assert r.status_code == 401 and _code(r) == "TERMINAL_SESSION_REQUIRED", path


@pytest.mark.parametrize("factors", ["cookie_only", "token_only", "swapped", "wrong_token"])
def test_r_f2_one_factor_alone_never_reads(env, factors):
    cookie, token = terminal_credential(), terminal_token()
    c = _browser()
    if factors == "cookie_only":
        c.cookies.set("kt_session", cookie)
    elif factors == "token_only":
        c.headers["X-Kite-Terminal-Token"] = token
    elif factors == "swapped":
        authorize_client(c, token, cookie)
    else:
        authorize_client(c, cookie, "t" * 43)
    for path in ("/api/holdings", "/api/positions", "/api/orders", "/api/trades", "/api/tickets",
                 "/api/journal/stats", "/api/dmb-inbox/items", "/api/app-positions", "/api/ready"):
        r = c.get(path)
        assert r.status_code == 401 and _code(r) == "TERMINAL_SESSION_REQUIRED", path


def test_r_f2_a_revoked_or_restarted_session_no_longer_reads(env):
    from backend import security
    c = _browser()
    authorize_client(c, terminal_credential(), terminal_token())
    assert c.get("/api/dmb-inbox/items").status_code == 200
    security.revoke_terminal_credential()                            # logout / process restart
    assert c.get("/api/dmb-inbox/items").status_code == 401
    reauthorize(c)                                                   # the fresh login
    assert c.get("/api/dmb-inbox/items").status_code == 200


def test_r_f2_the_legitimate_ui_reads_work(env):
    for path in ("/api/session/status", "/api/tickets", "/api/holdings", "/api/positions", "/api/dmb-inbox/items",
                 "/api/account-safety/status", "/api/live-readiness", "/api/research/volume-scan.csv"):
        assert env.client.get(path).status_code in (200, 503), path


def test_r_f2_the_public_reads_disclose_nothing_account_specific(env):
    anonymous = _browser()
    status = anonymous.get("/api/session/status").json()
    assert status["terminal_authorized"] is False
    assert status["user_name"] is None and status["login_time"] is None
    assert set(anonymous.get("/api/health").json()["data"]) == {"status", "database", "version", "commit", "time_utc"}
    assert anonymous.get("/api/config").status_code == 200
    authorized = env.client.get("/api/session/status").json()
    assert authorized["terminal_authorized"] is True


@pytest.mark.parametrize("site", ["same-site", "cross-site"])
def test_r_f2_a_browser_read_from_another_origin_is_refused_even_with_credentials(env, site):
    """`same-site` is exactly what another port on 127.0.0.1 is; `cross-site` a foreign page."""
    r = env.client.get("/api/holdings", headers={"Sec-Fetch-Site": site})
    assert r.status_code == 403 and _code(r) == "CSRF_PROTECTION_FAILED"
    assert env.client.get("/api/holdings", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "http://127.0.0.1:9999", "http://localhost:8001", "null"])
def test_r_f2_a_read_with_a_foreign_origin_is_refused(env, origin):
    r = env.client.get("/api/dmb-inbox/items", headers={"Origin": origin})
    assert r.status_code == 403
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_r_f2_no_cors_is_ever_granted(env):
    pre = env.client.options("/api/holdings", headers={"Origin": "https://evil.example",
                                                       "Access-Control-Request-Method": "GET",
                                                       "Access-Control-Request-Headers": "x-kite-terminal-token"})
    assert not any(k.lower().startswith("access-control-") for k in pre.headers)
    r = env.client.get("/api/holdings")
    assert not any(k.lower().startswith("access-control-") for k in r.headers)


def test_r_f2_dns_rebinding_is_still_refused(env):
    rebound = _browser(headers={"Host": "evil.example:8000"})
    authorize_client(rebound, terminal_credential(), terminal_token())
    assert rebound.get("/api/dmb-inbox/items").status_code == 400


# ============================================================ R-F3 cookie exposure across ports
def test_r_f3_the_cookie_alone_replayed_from_another_port_cannot_read_write_or_stream(env):
    """The attack R-F3 described: a server on 127.0.0.1:9999 collects kt_session (cookies have no
    port) and replays it with forged Host and Origin from a non-browser client. Without the
    terminal token - which lives in the terminal origin's storage, unreadable from :9999 - every
    read, write and WebSocket handshake is refused."""
    stolen = terminal_credential()
    replay = _browser(headers=TERMINAL_HEADERS)
    replay.cookies.set("kt_session", stolen)
    assert replay.get("/api/holdings").status_code == 401
    r = replay.post("/api/kill-switch/activate", json={})
    assert r.status_code == 401 and _code(r) == "TERMINAL_SESSION_REQUIRED"
    with pytest.raises(WebSocketDisconnect) as closed:
        with replay.websocket_connect(WS_URL, headers={"Origin": TERMINAL_ORIGIN}) as ws:
            ws.receive_json()
    assert closed.value.code == 1008


def test_r_f3_the_terminal_token_travels_only_in_the_landing_fragment(exchanges):
    browser = _browser()
    state = _state(browser.get("/login", follow_redirects=False))
    r = browser.get(_echoed(state), follow_redirects=False)
    token = r.headers["location"].removeprefix("/#kt_token=")
    from backend import security
    assert security.terminal_token_valid(token)
    assert token not in _set_cookies(r) and token not in r.text
    cookie = re.search(r"kt_session=([A-Za-z0-9_\-]+)", _set_cookies(r)).group(1)
    assert cookie != token and security.request_authorized(cookie, token)
    assert r.headers["cache-control"] == "no-store"


def test_r_f3_the_cookie_keeps_its_attributes(env):
    from starlette.responses import Response
    from backend import security
    r = Response()
    security.set_credential_cookie(r, "token-value", secure=False)
    header = r.headers["set-cookie"].lower()
    assert "httponly" in header and "samesite=strict" in header and "path=/" in header
    # Host-only: no Domain attribute. (Cookies have no port attribute at all - which is why the
    # port-isolated token exists.)
    assert "domain=" not in header


def test_r_f3_the_host_must_name_this_terminals_exact_port(env, monkeypatch):
    from backend import security
    monkeypatch.setenv(security.RUNTIME_PORT_ENV, "8000")
    assert env.client.get("/api/holdings").status_code == 200
    for host in ("127.0.0.1:9999", "127.0.0.1", "localhost:8001"):
        r = _browser(headers={**TERMINAL_HEADERS, "Host": host}).get("/api/health")
        assert r.status_code == 400, host


def test_r_f3_a_dedicated_hostname_is_exact_not_wildcard(env, monkeypatch):
    """The optional dedicated application hostname (a host-only cookie no other local hostname
    receives) is an exact entry; other local names and wildcards stay refused."""
    from backend import security
    monkeypatch.setenv(security.ALLOWED_HOSTS_ENV, "kite-terminal.localhost,*.localhost")
    assert security.allowed_hostnames() == security.LOOPBACK_HOSTS | {"kite-terminal.localhost"}
    ok = _browser(headers={"Host": "kite-terminal.localhost:8000"})
    assert ok.get("/api/health").status_code == 200
    for host in ("other.localhost:8000", "kite-terminal.localhost.evil.example:8000"):
        assert _browser(headers={"Host": host}).get("/api/health").status_code == 400, host


def test_r_f3_an_unexpected_origin_never_writes_even_with_both_factors(env):
    for origin in ("http://127.0.0.1:9999", "http://kite-terminal.localhost:8000", "null"):
        r = env.client.post("/api/kill-switch/activate", json={}, headers={"Origin": origin})
        assert r.status_code == 403, origin


# ============================================================ R-F4 WebSocket revocation
@pytest.fixture
def quiet_ticker(monkeypatch):
    from backend.ticker import ticker_manager
    monkeypatch.setattr(ticker_manager, "add_listener", lambda q: None)
    monkeypatch.setattr(ticker_manager, "remove_listener", lambda q: None)


def _ws(client, token: str | None, **kw):
    protocols = ["kt.v1"] + ([f"kt-token.{token}"] if token else [])
    headers = {"Origin": TERMINAL_ORIGIN, "Cookie": f"kt_session={terminal_credential()}"}
    return client.websocket_connect(WS_URL, subprotocols=protocols, headers=headers, **kw)


def test_r_f4_the_browser_handshake_uses_the_token_subprotocol(env, quiet_ticker):
    bare = _browser()
    with _ws(bare, terminal_token()) as ws:
        assert ws.accepted_subprotocol == "kt.v1"
    with pytest.raises(WebSocketDisconnect) as closed:
        with _ws(bare, None) as ws:
            ws.receive_json()
    assert closed.value.code == 1008


def test_r_f4_logout_closes_an_open_socket(env, quiet_ticker):
    with _ws(_browser(), terminal_token()) as ws:
        assert env.client.post("/api/logout").status_code == 200
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4401


def test_r_f4_a_new_login_closes_the_old_socket_and_old_factors_never_reconnect(env, quiet_ticker):
    old_cookie, old_token = terminal_credential(), terminal_token()
    with _ws(_browser(), old_token) as ws:
        reauthorize(env.client)                                      # a new Kite login (elsewhere)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4401
    stale = _browser()
    with pytest.raises(WebSocketDisconnect) as refused:
        with stale.websocket_connect(WS_URL, subprotocols=["kt.v1", f"kt-token.{old_token}"],
                                     headers={"Origin": TERMINAL_ORIGIN, "Cookie": f"kt_session={old_cookie}"}) as ws:
            ws.receive_json()
    assert refused.value.code == 1008
    with _ws(_browser(), terminal_token()) as ws:                    # the new login's socket works
        assert ws.accepted_subprotocol == "kt.v1"


def test_r_f4_a_message_sent_after_revocation_gets_nothing_back(env, monkeypatch):
    """Events keep flowing to a live socket; after a revoke the socket is closed instead - an event
    queued afterwards is never delivered, and what the client sends is never answered."""
    import asyncio
    from backend.ticker import ticker_manager
    queues = []

    def _listen(queue):          # on the event loop, as the handler registers it
        queues.append((asyncio.get_running_loop(), queue))
    monkeypatch.setattr(ticker_manager, "add_listener", _listen)
    monkeypatch.setattr(ticker_manager, "remove_listener", lambda q: None)

    def _push(message):          # as TickerManager hands messages over: on the loop
        loop, queue = queues[0]
        loop.call_soon_threadsafe(queue.put_nowait, message)
    from backend import security
    with _ws(_browser(), terminal_token()) as ws:
        _push({"type": "tickets_changed"})
        assert ws.receive_json() == {"type": "tickets_changed"}
        security.revoke_terminal_credential()
        ws.send_text("hello")
        _push({"type": "tickets_changed", "after": "revoke"})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4401


def test_r_f4_the_revocation_listener_is_released_with_the_socket(env, quiet_ticker):
    from backend import security
    before = len(security._credential._listeners)
    with _ws(_browser(), terminal_token()):
        assert len(security._credential._listeners) == before + 1
    assert len(security._credential._listeners) == before


# ============================================================ audit sweep
def test_no_get_route_issues_a_credential_or_writes_trading_state():
    """Audit sweep: the only GET routes with side effects are /callback (guarded above) and the
    ticket chart's cached chart-URL write (not an economic field)."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "backend" / "routes"
    flagged = {}
    for f in sorted(root.glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    if isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "get":
                        body = ast.get_source_segment(src, node)
                        if any(w in body for w in ("commit(", "issue_terminal_credential", "place_order")):
                            flagged[d.args[0].value] = f.name
    assert flagged == {"/callback": "auth.py", "/api/tickets/{ticket_id}/chart": "tickets.py"}
