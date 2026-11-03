"""P2-G security audit regressions (docs/SECURITY_P2G_AUDIT.md).

F1 (fixed): login CSRF - a forced /callback never exchanges a request token or issues a terminal
credential unless THIS browser started the login (GET /login) within 10 minutes; single use.
The residual window and the documented, unfixed findings are pinned here as well, so a change
to them is a deliberate decision rather than an accident. FakeKite, throwaway database.
"""
import re

import pytest
from fastapi.testclient import TestClient

from conftest import TERMINAL_ORIGIN, start_kite_login, terminal_credential

KITE_REFERER = {"Referer": "https://kite.zerodha.com/connect/finish"}
CALLBACK = "/callback?request_token=reqATTACKER123&status=success"


@pytest.fixture
def exchanges(env, monkeypatch):
    from backend import kite_client
    from backend.routes import auth
    seen = []
    monkeypatch.setattr(kite_client, "exchange_request_token", lambda token: seen.append(token))
    monkeypatch.setattr(kite_client, "build_login_url",
                        lambda account_id=None: "https://kite.zerodha.com/connect/login?v=3&api_key=test_api_key")

    async def _no_after_login():
        return None
    monkeypatch.setattr(auth, "_after_login", _no_after_login)
    return seen


def _browser(**kw):
    from backend.main import app
    return TestClient(app, base_url=TERMINAL_ORIGIN, **kw)


def _set_cookies(r) -> str:
    return "; ".join(v for k, v in r.headers.multi_items() if k.lower() == "set-cookie")


# ============================================================ F1 login CSRF (fixed)
def test_a_forced_callback_without_a_started_login_exchanges_nothing(exchanges):
    r = _browser().get(CALLBACK, headers=KITE_REFERER, follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/?login_error=1"
    assert exchanges == [] and "kt_session=" not in _set_cookies(r)


def test_a_login_started_in_this_browser_completes_once(exchanges):
    browser = _browser()
    login = browser.get("/login", follow_redirects=False)
    assert login.headers["location"].startswith("https://kite.zerodha.com/connect/login")
    header = _set_cookies(login).lower()
    assert "kt_login_state=" in header and "httponly" in header and "samesite=lax" in header
    assert "path=/callback" in header and "max-age=600" in header
    state = re.search(r"kt_login_state=([A-Za-z0-9_\-]+)", _set_cookies(login)).group(1)
    assert state not in login.headers["location"]
    r = browser.get(CALLBACK, headers=KITE_REFERER, follow_redirects=False)
    assert r.headers["location"] == "/" and exchanges == ["reqATTACKER123"]
    assert "kt_session=" in _set_cookies(r)
    again = _browser()
    again.cookies.set("kt_login_state", state)                       # replaying the used state
    r = again.get(CALLBACK, follow_redirects=False)
    assert r.headers["location"] == "/?login_error=1" and exchanges == ["reqATTACKER123"]


def test_a_wrong_or_foreign_state_is_refused(exchanges):
    start_kite_login(_browser())                                     # someone else's pending login
    attacker = _browser()
    attacker.cookies.set("kt_login_state", "x" * 43)
    assert attacker.get(CALLBACK, follow_redirects=False).headers["location"] == "/?login_error=1"
    assert exchanges == []


def test_an_expired_state_is_refused(exchanges):
    from backend import security
    browser = _browser()
    token = security.begin_login(now=0.0)                            # started long ago
    browser.cookies.set("kt_login_state", token)
    assert browser.get(CALLBACK, follow_redirects=False).headers["location"] == "/?login_error=1"
    assert exchanges == []


def test_a_cancelled_login_consumes_the_state(exchanges):
    browser = _browser()
    start_kite_login(browser)
    assert browser.get("/callback?status=cancelled", follow_redirects=False).headers["location"] == "/?login_error=1"
    assert browser.get(CALLBACK, follow_redirects=False).headers["location"] == "/?login_error=1"
    assert exchanges == []


def test_a_forced_callback_never_rotates_the_existing_credential(exchanges):
    from backend import security
    current = terminal_credential()
    _browser().get(CALLBACK, follow_redirects=False)
    assert security.terminal_credential_valid(current)


def test_pending_logins_are_bounded(env):
    from backend import security
    tokens = [security.begin_login(now=1000.0 + i) for i in range(20)]
    assert sum(security.consume_login_state(t, now=1001.0 + 20) for t in tokens) == security._MAX_PENDING_LOGINS


def test_residual_a_callback_inside_a_login_this_browser_started_passes(exchanges):
    """Documented residual (R-F1): the state binds the callback to a login THIS browser started,
    not to the request token Kite issued for it. A forced callback that lands while such a login
    is pending (at most 10 minutes, single use) is accepted."""
    browser = _browser()
    start_kite_login(browser)
    assert browser.get(CALLBACK, follow_redirects=False).headers["location"] == "/"


# ============================================================ pinned residual findings
def test_residual_reads_are_host_checked_only(env):
    """R-F2 (documented, owner decision P1-F): any local process can GET account data with an
    allowed Host. A foreign page cannot read it: no CORS headers, and DNS rebinding is refused."""
    anonymous = _browser()
    assert anonymous.get("/api/session/status").status_code == 200
    r = anonymous.get("/api/dmb-inbox/items", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200 and "access-control-allow-origin" not in {k.lower() for k in r.headers}
    rebound = _browser(headers={"Host": "evil.example:8000"})
    assert rebound.get("/api/dmb-inbox/items").status_code == 400


def test_the_credential_cookie_is_host_scoped_not_port_scoped():
    """R-F3 (documented, open): cookies carry no port. The kt_session cookie therefore has no Port
    or Domain attribute that could isolate it from another server on the same host."""
    from starlette.responses import Response
    from backend import security
    r = Response()
    security.set_credential_cookie(r, "token-value", secure=False)
    header = r.headers["set-cookie"].lower()
    assert "domain=" not in header and "port" not in header and "samesite=strict" in header


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
