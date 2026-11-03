"""SEC-1 P1-F: the per-launch terminal credential (two factors since P2-G R-F3).

Host and Origin checks stop browser-borne attacks; any local program can
forge them. Every write and the /ws/ticks handshake must therefore also carry
the terminal credential, which the app issues only when a Kite login
completes (/callback) — as an HttpOnly, SameSite=Strict cookie, never in a
URL, page, asset, body or log. A restart (process memory) or a logout ends
it. These tests cover the guard on every mutating route, the WebSocket, the
lifecycle (issue, rotate, restart, logout) and leakage.

P2-G (docs/SECURITY_P2G_AUDIT.md) changed two things deliberately: the cookie must now be
accompanied by the port-isolated terminal token (R-F3), and reads other than three public routes
need the credential too (R-F2) - P1-F's "reads are unchanged" decision is reversed. Positive paths
below therefore present both factors, as the browser does; no negative assertion was relaxed.
"""

import logging
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import TERMINAL_HEADERS, TERMINAL_ORIGIN, approval_body, reauthorize, set_mode, terminal_credential, \
    start_kite_login, terminal_token
from test_baseline_api import _manual_ticket

ROOT = Path(__file__).resolve().parents[1]
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
BROKER_WRITES = ("place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt")
WS_URL = TERMINAL_ORIGIN.replace("http://", "ws://") + "/ws/ticks"
COOKIE = "kt_session"

def _logged_in(client):
    """P2-G: the browser first went through GET /login (a pending, cookie-bound login state), and
    Kite echoes that state on the callback (R-F1): every request of this client carries it."""
    state, get = start_kite_login(client), client.get

    def _get(url, **kw):
        if url.startswith("/callback"):
            url += ("&" if "?" in url else "?") + f"kt_state={state}"
        return get(url, **kw)
    client.get = _get
    return client


def _landing_token(r) -> str:
    """The terminal token a completed login hands the page in the landing URL's fragment."""
    location = r.headers["location"]
    assert location.startswith("/#kt_token="), location
    return location.removeprefix("/#kt_token=")



def _client(token: str | None = None, headers=TERMINAL_HEADERS, base_url=TERMINAL_ORIGIN, terminal: str | None = None):
    """A browser-like client: terminal Origin + header; the credential cookie only when given, and
    the terminal token (P2-G R-F3, the page's X-Kite-Terminal-Token) only when given."""
    from backend.main import app
    client = TestClient(app, base_url=base_url, headers=headers)
    if token is not None:
        client.cookies.set(COOKIE, token)
    if terminal is not None:
        client.headers["X-Kite-Terminal-Token"] = terminal
    return client


def _browser():
    """The browser holding both factors of this test's current credential."""
    return _client(terminal_credential(), terminal=terminal_token())


def _code(r):
    return r.json()["detail"]["error_code"]


def _mutating_routes():
    from backend.main import app
    return sorted((method, route.path) for route in app.routes if isinstance(route, APIRoute)
                  for method in route.methods & MUTATING)


@pytest.fixture
def one_tick(monkeypatch):
    from backend.ticker import ticker_manager
    monkeypatch.setattr(ticker_manager, "add_listener", lambda q: q.put_nowait({"type": "tick", "n": 1}))
    monkeypatch.setattr(ticker_manager, "remove_listener", lambda q: None)


@pytest.fixture
def no_after_login(monkeypatch):
    from backend import kite_client
    from backend.routes import auth
    exchanged = []
    monkeypatch.setattr(kite_client, "exchange_request_token", lambda token: exchanged.append(token))

    async def _nothing():
        return None
    monkeypatch.setattr(auth, "_after_login", _nothing)
    return exchanged


# ============================================================ every mutating route

@pytest.mark.parametrize("method,path", _mutating_routes())
@pytest.mark.parametrize("credential", ["missing", "wrong", "previous_launch", "cookie_without_token"])
def test_every_mutating_route_needs_the_current_credential(env, method, path, credential):
    from backend import security
    current = terminal_credential()                      # a current one exists...
    token = {"missing": None, "wrong": "x" * 43}.get(credential)
    terminal = None
    if credential == "previous_launch":
        token, terminal = current, terminal_token()
        security._credential = security._TerminalCredential()   # ...but this is a new process
    if credential == "cookie_without_token":             # P2-G R-F3: e.g. collected by another port
        token = current
    url = re.sub(r"\{[^}]+\}", "1", path)
    r = _client(token, terminal=terminal).request(method, url, json={})
    assert r.status_code == 401 and _code(r) == "TERMINAL_SESSION_REQUIRED", (method, url, r.status_code, r.text)
    assert [n for n, _ in env.kite.calls if n in BROKER_WRITES] == []


def test_the_inventory_covers_the_safety_critical_writes():
    routes = {path for _, path in _mutating_routes()}
    for path in ("/api/tickets/{ticket_id}/confirm", "/api/kill-switch/emergency-exit-confirm", "/api/execution/mode",
                 "/api/account-safety/trading-lock", "/api/logout", "/api/tickets/{ticket_id}/re-review"):
        assert path in routes


def test_the_current_credential_is_accepted(env):
    r = _browser().post("/api/ui-prefs", json={"theme": "dark"})
    assert r.status_code == 200, r.text


# ============================================================ alongside the Origin checks

def test_a_forged_origin_without_the_credential_is_refused(env, market_open):
    set_mode(env.db, "dry_run")
    tid = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]["id"]
    r = _client(None).post(f"/api/tickets/{tid}/confirm", json=approval_body(env.db, tid))
    assert r.status_code == 401 and _code(r) == "TERMINAL_SESSION_REQUIRED"
    assert env.kite.calls_to("place_order") == []


def test_a_valid_credential_never_overrides_a_foreign_origin(env):
    token = terminal_credential()
    r = _client(token, headers={"X-Kite-Terminal": "1", "Origin": "https://evil.example"}).post(
        "/api/ui-prefs", json={"theme": "dark"})
    assert r.status_code == 403 and _code(r) == "CSRF_PROTECTION_FAILED"


def test_a_valid_credential_never_replaces_the_application_header(env):
    r = _client(terminal_credential(), headers={"Origin": TERMINAL_ORIGIN}).post("/api/ui-prefs", json={"x": 1})
    assert r.status_code == 403 and _code(r) == "CSRF_PROTECTION_FAILED"


def test_reads_need_the_credential_except_the_public_ones(env):
    """P2-G R-F2 reverses P1-F's "reads are unchanged": account data needs both factors."""
    anonymous, browser = _client(None), _browser()
    for path in ("/api/session/status", "/api/health", "/api/config", "/"):
        assert anonymous.get(path).status_code == 200, path
    for path in ("/api/tickets", "/api/holdings"):
        r = anonymous.get(path)
        assert r.status_code == 401 and _code(r) == "TERMINAL_SESSION_REQUIRED", path
        assert _client(terminal_credential()).get(path).status_code == 401, path     # cookie alone
        assert browser.get(path).status_code == 200, path


# ============================================================ WebSocket handshake

@pytest.mark.parametrize("cookie", [None, "kt_session=" + "y" * 43], ids=["missing", "wrong"])
def test_the_websocket_handshake_needs_the_credential(env, one_tick, cookie):
    terminal_credential()
    headers = {"Origin": TERMINAL_ORIGIN, **({"Cookie": cookie} if cookie else {})}
    with pytest.raises(WebSocketDisconnect) as exc:
        with _client(None).websocket_connect(WS_URL, headers=headers) as ws:
            ws.receive_json()
    assert exc.value.code == 1008


def test_the_websocket_handshake_with_the_credential_is_accepted(env, one_tick):
    headers = {"Origin": TERMINAL_ORIGIN, "Cookie": f"{COOKIE}={terminal_credential()}"}
    with _client(None).websocket_connect(WS_URL, headers=headers,
                                         subprotocols=["kt.v1", f"kt-token.{terminal_token()}"]) as ws:
        assert ws.receive_json() == {"type": "tick", "n": 1}


def test_a_credential_never_rescues_a_foreign_websocket_origin(env, one_tick):
    headers = {"Origin": "https://evil.example", "Cookie": f"{COOKIE}={terminal_credential()}"}
    with pytest.raises(WebSocketDisconnect) as exc:
        with _client(None).websocket_connect(WS_URL, headers=headers) as ws:
            ws.receive_json()
    assert exc.value.code == 1008


# ============================================================ lifecycle

def _cookie_header(response) -> str:
    return "; ".join(v for k, v in response.headers.multi_items() if k.lower() == "set-cookie")


def test_the_kite_callback_issues_an_httponly_strict_cookie(env, no_after_login):
    r = _logged_in(_client(None)).get("/callback?request_token=abc&status=success", follow_redirects=False,
                          headers={"Referer": "https://kite.zerodha.com/connect/finish"})
    assert r.status_code in (302, 307) and r.headers["location"].startswith("/#kt_token=")
    header = _cookie_header(r)
    m = re.search(r"kt_session=([A-Za-z0-9_\-]+)", header)
    assert m and no_after_login == ["abc"]
    lowered = header.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "path=/" in lowered
    assert "secure" not in lowered                                   # plain-http loopback
    from backend import security
    assert security.terminal_credential_valid(m.group(1))
    assert m.group(1) not in r.headers["location"]                    # never in the URL


def test_over_https_the_cookie_is_also_secure(env, no_after_login):
    r = _logged_in(_client(None, base_url="https://127.0.0.1:8000")).get("/callback?request_token=abc&status=success",
                                                            follow_redirects=False)
    assert "secure" in _cookie_header(r).lower()


@pytest.mark.parametrize("query", ["status=cancelled", "request_token=abc&status=failure", "status=success"],
                         ids=["cancelled", "failed", "no_token"])
def test_a_failed_or_cancelled_login_issues_nothing(env, no_after_login, query):
    r = _logged_in(_client(None)).get(f"/callback?{query}", follow_redirects=False)
    assert r.headers["location"] == "/?login_error=1" and "kt_session" not in _cookie_header(r)


def test_a_token_exchange_failure_issues_nothing(env, monkeypatch):
    from backend import kite_client

    def boom(token):
        raise RuntimeError("exchange failed")
    monkeypatch.setattr(kite_client, "exchange_request_token", boom)
    r = _logged_in(_client(None)).get("/callback?request_token=abc&status=success", follow_redirects=False)
    assert r.headers["location"] == "/?login_error=1" and "kt_session" not in _cookie_header(r)


def test_a_new_login_rotates_the_credential(env, no_after_login):
    old, old_terminal = terminal_credential(), terminal_token()
    r = _logged_in(_client(None)).get("/callback?request_token=abc&status=success", follow_redirects=False)
    new = re.search(r"kt_session=([A-Za-z0-9_\-]+)", _cookie_header(r)).group(1)
    new_terminal = _landing_token(r)
    assert new != old and new_terminal != old_terminal
    assert _client(old, terminal=old_terminal).post("/api/ui-prefs", json={"x": 1}).status_code == 401
    assert _client(old, terminal=new_terminal).post("/api/ui-prefs", json={"x": 1}).status_code == 401
    assert _client(new, terminal=new_terminal).post("/api/ui-prefs", json={"x": 1}).status_code == 200


def test_an_application_restart_invalidates_the_credential(env):
    from backend import security
    token, terminal = terminal_credential(), terminal_token()
    assert _client(token, terminal=terminal).post("/api/ui-prefs", json={"x": 1}).status_code == 200
    security._credential = security._TerminalCredential()           # what a new process starts with
    r = _client(token, terminal=terminal).post("/api/ui-prefs", json={"x": 1})
    assert r.status_code == 401 and _code(r) == "TERMINAL_SESSION_REQUIRED"
    status = _client(token, terminal=terminal).get("/api/session/status").json()
    assert status["connected"] is True and status["terminal_authorized"] is False


def test_logout_revokes_the_credential_and_clears_the_cookie(env):
    token, terminal = terminal_credential(), terminal_token()
    c = _client(token, terminal=terminal)
    r = c.post("/api/logout")
    assert r.status_code == 200
    assert re.search(r'kt_session=(""|);', _cookie_header(r)) and "max-age=0" in _cookie_header(r).lower()
    assert _client(token, terminal=terminal).post("/api/ui-prefs", json={"x": 1}).status_code == 401


def test_session_status_reports_authorization_without_the_value(env):
    token, terminal = terminal_credential(), terminal_token()
    authorized = _client(token, terminal=terminal).get("/api/session/status")
    assert authorized.json()["terminal_authorized"] is True
    assert token not in authorized.text and terminal not in authorized.text
    assert _client(token).get("/api/session/status").json()["terminal_authorized"] is False    # cookie alone
    assert _client(None).get("/api/session/status").json()["terminal_authorized"] is False


# ============================================================ the operator workflows

def test_the_safety_workflows_work_with_the_credential_and_only_with_it(env, market_open):
    c, anonymous = _browser(), _client(None)
    cookie_only = _client(terminal_credential())
    lock = {"locked": True, "reason": "drill"}
    assert cookie_only.post("/api/account-safety/trading-lock", json=lock).status_code == 401
    assert anonymous.post("/api/account-safety/trading-lock", json=lock).status_code == 401
    assert c.post("/api/account-safety/trading-lock", json=lock).status_code == 200
    mode = {"mode": "dry_run"}
    assert anonymous.post("/api/execution/mode", json=mode).status_code == 401
    assert c.post("/api/execution/mode", json=mode).status_code == 200
    phrase = {"confirmation_phrase": "EXIT ALL"}
    assert anonymous.post("/api/kill-switch/emergency-exit-confirm", json=phrase).status_code == 401
    assert cookie_only.post("/api/kill-switch/emergency-exit-confirm", json=phrase).status_code == 401
    assert c.post("/api/kill-switch/emergency-exit-confirm", json=phrase).status_code == 200
    assert anonymous.post("/api/logout").status_code == 401
    assert c.post("/api/logout").status_code == 200


def test_reauthorizing_after_a_logout_restores_writes(env):
    env.client.post("/api/logout")
    assert env.client.post("/api/ui-prefs", json={"x": 1}).status_code == 401
    reauthorize(env.client)
    assert env.client.post("/api/ui-prefs", json={"x": 1}).status_code == 200


# ============================================================ leakage

def test_the_credential_never_leaks_into_logs_bodies_pages_or_assets(env, no_after_login, caplog):
    caplog.set_level(logging.DEBUG)
    r = _logged_in(_client(None)).get("/callback?request_token=abc&status=success", follow_redirects=False)
    token = re.search(r"kt_session=([A-Za-z0-9_\-]+)", _cookie_header(r)).group(1)
    terminal = _landing_token(r)            # P2-G R-F3: by design in the landing fragment only
    c, anonymous = _client(token, terminal=terminal), _client(None)
    bodies = [
        c.get("/api/session/status").text, c.get("/").text, c.get("/static/app.js").text,
        c.get("/static/terminal_auth.js").text,
        c.post("/api/ui-prefs", json={"x": 1}).text, anonymous.post("/api/ui-prefs", json={"x": 1}).text,
        _client("z" * 43).post("/api/ui-prefs", json={"x": 1}).text,
    ]
    assert terminal not in _cookie_header(r)
    with pytest.raises(WebSocketDisconnect):
        with _client(None).websocket_connect(WS_URL, headers={"Origin": TERMINAL_ORIGIN, "Cookie": "kt_session=bad"}):
            pass
    for body in bodies:
        assert token not in body and terminal not in body
    assert token not in r.headers["location"]
    assert token not in caplog.text and terminal not in caplog.text
    from backend import redaction
    assert token not in redaction.redact_text(f"cookie kt_session={token}")      # registered for redaction
    assert terminal not in redaction.redact_text(f"X-Kite-Terminal-Token: {terminal}")


def test_no_static_asset_or_source_carries_a_credential():
    for path in [*(ROOT / "frontend").rglob("*.js"), *(ROOT / "frontend").rglob("*.html")]:
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "kt_session" not in text, path          # the UI never reads or writes the cookie itself


def test_the_comparison_is_constant_time():
    import inspect
    from backend import security
    source = inspect.getsource(security._TerminalCredential._matches)
    assert "hmac.compare_digest(candidate, expected)" in source and " == expected" not in source
    for method in (security._TerminalCredential.valid, security._TerminalCredential.token_valid):
        assert "self._matches(" in inspect.getsource(method)
