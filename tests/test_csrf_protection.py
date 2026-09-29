"""PK-5 (audit P0-8): cross-origin write protection.

Policy under test (backend/security.py):
- every non-GET/HEAD/OPTIONS request needs ``X-Kite-Terminal: 1``;
- a present Origin (else a present Referer) must be a terminal origin: the
  request's scheme, a loopback hostname, and the Host header's port;
- no Origin and no Referer is accepted from a header-bearing (non-browser)
  client, because browsers always send Origin on cross-origin writes;
- the /ws/ticks handshake gets the same Origin check, header not required;
- GET routes, including /login and the Kite /callback, are never checked.

The ordinary ``env.client`` sends the header and a same-origin Origin (see
conftest.terminal_client). Tests here build bare clients so they can omit or
alter exactly one of them.
"""

import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import displayed_mode, TERMINAL_ORIGIN, set_mode, ticket_row
from test_baseline_api import _insert_ticket, _manual_ticket
from test_review_changes import _breakeven_proposal

APP_HEADER = {"X-Kite-Terminal": "1"}
FOREIGN = "https://evil.example"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
BROKER_WRITES = ("place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt")


@pytest.fixture
def market_open(monkeypatch):
    from backend import market
    monkeypatch.setattr(market, "is_market_open", lambda at=None: True)


def _bare(base_url=TERMINAL_ORIGIN, **kwargs):
    from backend.main import app
    return TestClient(app, base_url=base_url, **kwargs)


def _is_csrf_reject(r):
    return r.status_code == 403 and r.json()["detail"]["error_code"] == "CSRF_PROTECTION_FAILED"


def _pending_ticket(env):
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    assert t["status"] == "pending_confirmation", t
    return t["id"]


def _rows(env, sql, *args):
    conn = env.db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------- inventory

def _mutating_routes():
    from backend.main import app
    return sorted((method, route.path) for route in app.routes if isinstance(route, APIRoute)
                  for method in route.methods & MUTATING)


def test_inventory_covers_post_put_delete():
    methods = {m for m, _ in _mutating_routes()}
    assert {"POST", "PUT", "DELETE"} <= methods


@pytest.mark.parametrize("method,path", _mutating_routes())
@pytest.mark.parametrize("attack", ["foreign_origin_no_header", "foreign_origin_with_header"])
def test_every_mutating_route_rejects_cross_origin(env, method, path, attack):
    # Path parameters are filled with a plausible value; the middleware
    # answers before routing, so the route never gets to 404 or run.
    url = re.sub(r"\{[^}]+\}", "1", path)
    headers = {"Origin": FOREIGN, "Content-Type": "text/plain"}
    if attack == "foreign_origin_with_header":
        headers.update(APP_HEADER)
    r = _bare().request(method, url, content=b"{}", headers=headers)
    assert _is_csrf_reject(r), (method, url, r.status_code, r.text[:200])
    assert [name for name, _ in env.kite.calls if name in BROKER_WRITES] == []


def test_unknown_mutating_method_is_rejected(env):
    # Safe methods are an allow-list; PATCH (no route uses it) is still guarded.
    r = _bare().request("PATCH", "/api/tickets/1", headers={"Origin": FOREIGN})
    assert _is_csrf_reject(r)


# ---------------------------------------------------------------- A / B / F: high-risk endpoints

def test_a_cross_origin_confirm_places_nothing(env, market_open):
    set_mode(env.db, "live")
    tid = _pending_ticket(env)
    r = _bare().post(f"/api/tickets/{tid}/confirm", content=b"",
                     headers={"Origin": FOREIGN, "Content-Type": "text/plain"})
    assert _is_csrf_reject(r)
    assert env.kite.calls_to("place_order") == []
    row = ticket_row(env.db, tid)
    assert row["status"] == "pending_confirmation" and row["kite_order_id"] is None


def test_b_cross_origin_gtt_cancel_leaves_protection(env):
    set_mode(env.db, "live")
    gtt_id = env.kite.place_gtt("single", "TCS", "NSE", [2900.0], 3000.0, [])["trigger_id"]
    _insert_ticket(env.db, kite_gtt_id=gtt_id)
    r = _bare().post(f"/api/gtts/{gtt_id}/cancel", headers={"Origin": FOREIGN})
    assert _is_csrf_reject(r)
    assert env.kite.calls_to("delete_gtt") == []
    assert env.kite.gtt_book[gtt_id]["status"] == "active"


def test_f_foreign_origin_with_header_is_still_rejected(env, market_open):
    set_mode(env.db, "live")
    tid = _pending_ticket(env)
    r = _bare().post(f"/api/tickets/{tid}/confirm", json=displayed_mode(env.db), headers={**APP_HEADER, "Origin": FOREIGN})
    assert _is_csrf_reject(r)
    assert env.kite.calls_to("place_order") == []
    assert ticket_row(env.db, tid)["status"] == "pending_confirmation"


def test_cross_origin_order_cancel_leaves_order(env):
    order_id = env.kite.add_broker_order("TCS", "BUY", 5)
    r = _bare().post(f"/api/orders/{order_id}/cancel?variety=regular", headers={"Origin": FOREIGN})
    assert _is_csrf_reject(r)
    assert env.kite.calls_to("cancel_order") == []
    assert env.kite.order_book[order_id]["status"] == "OPEN"


def test_cross_origin_stop_adjustment_confirm_leaves_stop(env):
    set_mode(env.db, "live")
    gtt_id = env.kite.place_gtt("single", "TCS", "NSE", [2900.0], 3000.0, [])["trigger_id"]
    tid, pid = _breakeven_proposal(env, gtt_id)
    r = _bare().post(f"/api/strategies/stop-adjustments/{pid}/confirm", headers={"Origin": FOREIGN})
    assert _is_csrf_reject(r)
    assert env.kite.calls_to("modify_gtt") == []
    assert ticket_row(env.db, tid)["current_stop_loss"] == 2900.0
    assert _rows(env, "SELECT status FROM stop_adjustment_proposals WHERE id = ?", pid)[0]["status"] == "pending"


def test_cross_origin_cancel_all_pending_leaves_tickets(env):
    tid = _pending_ticket(env)
    r = _bare().post("/api/tickets/cancel-all-pending", headers={"Origin": FOREIGN})
    assert _is_csrf_reject(r)
    assert ticket_row(env.db, tid)["status"] == "pending_confirmation"


def test_cross_origin_logout_keeps_session(env):
    r = _bare().post("/api/logout", headers={"Origin": FOREIGN})
    assert _is_csrf_reject(r)
    assert env.client.get("/api/session/status").json()["connected"] is True


@pytest.mark.parametrize("url,target", [
    ("/api/strategies/scan-now", "backend.strategy_engine.run_daily_scans"),
    ("/api/automation/run-now", "backend.automation.run_pipeline"),
    ("/api/instruments/refresh", "backend.instruments.refresh_instrument_cache"),
])
def test_cross_origin_triggers_do_not_run(env, monkeypatch, url, target):
    module_path, attr = target.rsplit(".", 1)
    import importlib
    called = []
    monkeypatch.setattr(importlib.import_module(module_path), attr, lambda *a, **k: called.append(1))
    r = _bare().post(url, headers={"Origin": FOREIGN})
    assert _is_csrf_reject(r)
    assert called == []


# ---------------------------------------------------------------- C / D: header required, same origin works

def test_c_same_origin_without_header_is_rejected(env):
    tid = _pending_ticket(env)
    r = _bare().post(f"/api/tickets/{tid}/confirm", json=displayed_mode(env.db), headers={"Origin": TERMINAL_ORIGIN})
    assert _is_csrf_reject(r)
    assert ticket_row(env.db, tid)["status"] == "pending_confirmation"


@pytest.mark.parametrize("value", ["0", "true", "", "1 "])
def test_c_wrong_header_value_is_rejected(env, value):
    r = _bare().post("/api/ui-prefs", json={"x": 1},
                     headers={"X-Kite-Terminal": value, "Origin": TERMINAL_ORIGIN})
    assert _is_csrf_reject(r)


def test_d_same_origin_with_header_behaves_normally(env, market_open):
    set_mode(env.db, "dry_run")
    tid = _pending_ticket(env)
    r = _bare().post(f"/api/tickets/{tid}/confirm", json=displayed_mode(env.db), headers={**APP_HEADER, "Origin": TERMINAL_ORIGIN})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "executed" and r.json()["data"]["mode"] == "dry_run"


def test_d_same_origin_put_and_delete_behave_normally(env):
    c = _bare(headers={**APP_HEADER, "Origin": TERMINAL_ORIGIN})
    draft = _insert_ticket(env.db, status="draft", mode=None)
    r = c.put(f"/api/tickets/{draft}", json=_manual_ticket(quantity=4))
    assert r.status_code == 200, r.text
    tid = _pending_ticket(env)
    assert c.post(f"/api/tickets/{tid}/reject", json={"reason": "no"}).status_code == 200
    r = c.delete(f"/api/tickets/{tid}")
    assert r.status_code == 200, r.text


def test_route_errors_pass_through_unchanged(env):
    # An allowed request reaches the route, whose own 404 is not masked.
    r = env.client.post("/api/tickets/999999/confirm")
    assert r.status_code == 404 and r.json()["detail"]["error_code"] == "NOT_FOUND"


# ---------------------------------------------------------------- E: loopback forms and ports

@pytest.mark.parametrize("base,origin", [
    ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
    ("http://localhost:8000", "http://localhost:8000"),
    ("http://127.0.0.1:8000", "http://localhost:8000"),   # the same server under its other loopback name
    ("http://localhost:8000", "http://127.0.0.1:8000"),
    ("http://127.0.0.1:8765", "http://127.0.0.1:8765"),   # any --port, not a hard-coded 8000
    ("http://127.0.0.1", "http://127.0.0.1"),             # default port both sides
    ("http://127.0.0.1", "http://127.0.0.1:80"),
    ("http://LOCALHOST:8000", "http://Localhost:8000"),
])
def test_e_loopback_origins_are_accepted(env, base, origin):
    r = _bare(base).post("/api/ui-prefs", json={"theme": "dark"}, headers={**APP_HEADER, "Origin": origin})
    assert r.status_code == 200, (base, origin, r.status_code)


@pytest.mark.parametrize("base,origin", [
    ("http://127.0.0.1:8000", "http://localhost:3000"),      # another local app, different port
    ("http://127.0.0.1:8000", "http://127.0.0.1:8001"),
    ("http://127.0.0.1:8000", "https://127.0.0.1:8000"),     # scheme mismatch
    ("http://127.0.0.1:8000", "http://127.0.0.1"),           # port 80 is not 8000
    ("http://127.0.0.1:8000", "null"),                       # sandboxed iframe / file://
    ("http://127.0.0.1:8000", ""),
    ("http://127.0.0.1:8000", "not a url"),
    ("http://127.0.0.1:8000", "http://192.168.1.10:8000"),   # LAN address, not loopback
    ("http://127.0.0.1:8000", "http://127.0.0.1.evil.example:8000"),
    # The DNS-rebinding request (Host and Origin both evil.example:8000) is
    # now refused earlier, with INVALID_HOST, by the PK-18 Host guard; see
    # test_security_hardening.py. The Origin layer's own verdict on it stays
    # covered by test_e_is_terminal_origin below.
])
def test_e_non_terminal_origins_are_rejected(env, base, origin):
    r = _bare(base).post("/api/ui-prefs", json={"theme": "dark"}, headers={**APP_HEADER, "Origin": origin})
    assert _is_csrf_reject(r), (base, origin, r.status_code)


@pytest.mark.parametrize("origin,scheme,host,expected", [
    ("http://[::1]:8000", "http", "[::1]:8000", True),        # TestClient cannot address IPv6 hosts
    ("http://localhost:8000", "http", "[::1]:8000", True),
    ("http://[::1]:8001", "http", "[::1]:8000", False),
    ("http://127.0.0.1:8000", "ws", "127.0.0.1:8000", True),  # WebSocket scheme maps to http
    ("https://127.0.0.1:8443", "wss", "127.0.0.1:8443", True),
    ("http://127.0.0.1:8443", "wss", "127.0.0.1:8443", False),
    ("http://127.0.0.1:8000", "http", "", False),              # no Host header: nothing to match
    ("http://127.0.0.1:99999", "http", "127.0.0.1:8000", False),
    ("http://evil.example:8000", "http", "evil.example:8000", False),  # DNS rebinding: Origin == Host but foreign
])
def test_e_is_terminal_origin(origin, scheme, host, expected):
    from backend.security import is_terminal_origin
    assert is_terminal_origin(origin, scheme, host) is expected


# ---------------------------------------------------------------- G: non-browser clients, Referer fallback

def test_g_no_origin_with_header_is_accepted(env, market_open):
    set_mode(env.db, "dry_run")
    tid = _pending_ticket(env)
    r = _bare().post(f"/api/tickets/{tid}/confirm", json=displayed_mode(env.db), headers=APP_HEADER)
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_g_no_origin_without_header_is_rejected(env):
    tid = _pending_ticket(env)
    r = _bare().post(f"/api/tickets/{tid}/confirm")
    assert _is_csrf_reject(r)
    assert ticket_row(env.db, tid)["status"] == "pending_confirmation"


def test_g_referer_is_checked_when_origin_is_absent(env):
    ok = _bare().post("/api/ui-prefs", json={"a": 1},
                      headers={**APP_HEADER, "Referer": f"{TERMINAL_ORIGIN}/?tab=approvals"})
    assert ok.status_code == 200
    bad = _bare().post("/api/ui-prefs", json={"a": 1},
                       headers={**APP_HEADER, "Referer": f"{FOREIGN}/page.html"})
    assert _is_csrf_reject(bad)


def test_g_origin_takes_precedence_over_referer(env):
    r = _bare().post("/api/ui-prefs", json={"a": 1},
                     headers={**APP_HEADER, "Origin": FOREIGN, "Referer": f"{TERMINAL_ORIGIN}/"})
    assert _is_csrf_reject(r)


# ---------------------------------------------------------------- safe methods, preflight

def test_reads_are_not_blocked_by_the_guard(env):
    # Reads are left to the browser same-origin policy (no CORS headers are
    # ever sent, so a foreign page cannot read the response).
    r = _bare().get("/api/tickets", headers={"Origin": FOREIGN})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


def test_preflight_gets_no_cors_grant(env):
    r = _bare().options("/api/tickets/1/confirm", headers={
        "Origin": FOREIGN, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-kite-terminal"})
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-headers" not in r.headers


# ---------------------------------------------------------------- H / I: WebSocket

@pytest.fixture
def one_tick(monkeypatch):
    from backend.ticker import ticker_manager
    monkeypatch.setattr(ticker_manager, "add_listener", lambda q: q.put_nowait({"type": "tick", "n": 1}))
    monkeypatch.setattr(ticker_manager, "remove_listener", lambda q: None)


# websocket_connect ignores base_url, so the terminal's address is given in full.
WS_URL = TERMINAL_ORIGIN.replace("http://", "ws://") + "/ws/ticks"


@pytest.mark.parametrize("origin", [FOREIGN, "http://localhost:3000", "null", "http://evil.example:8000"])
def test_h_websocket_foreign_origin_is_refused(env, one_tick, origin):
    with pytest.raises(WebSocketDisconnect) as exc:
        with _bare().websocket_connect(WS_URL, headers={"Origin": origin}) as ws:
            ws.receive_json()
    assert exc.value.code == 1008


@pytest.mark.parametrize("headers", [{"Origin": TERMINAL_ORIGIN}, {"Origin": "http://localhost:8000"}, {}],
                         ids=["same_origin", "localhost_alias", "no_origin"])
def test_i_websocket_terminal_origin_is_accepted(env, one_tick, headers):
    with _bare().websocket_connect(WS_URL, headers=headers) as ws:
        assert ws.receive_json() == {"type": "tick", "n": 1}


# ---------------------------------------------------------------- J: login / callback / static

def test_j_login_redirects_without_header(env):
    r = _bare().get("/login", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "kite.zerodha.com" in r.headers["location"]


def test_j_kite_callback_navigation_is_not_blocked(env, monkeypatch):
    from backend import kite_client
    from backend.routes import auth
    exchanged = []
    monkeypatch.setattr(kite_client, "exchange_request_token", lambda token: exchanged.append(token))

    async def _no_after_login():
        return None
    monkeypatch.setattr(auth, "_after_login", _no_after_login)
    # A top-level redirect from Zerodha: cross-site Referer, no app header.
    r = _bare().get("/callback?request_token=abc&status=success", follow_redirects=False,
                    headers={"Referer": "https://kite.zerodha.com/connect/finish"})
    assert r.status_code in (302, 307) and r.headers["location"] == "/"
    assert exchanged == ["abc"]


def test_j_callback_failure_path_is_unchanged(env):
    r = _bare().get("/callback?status=cancelled", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/?login_error=1"


def test_j_frontend_is_served_without_header(env):
    c = _bare()
    assert c.get("/").status_code == 200
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/api/session/status").status_code == 200


# ---------------------------------------------------------------- frontend contract

def test_frontend_mutations_send_the_application_header():
    js = (Path(__file__).resolve().parents[1] / "frontend" / "app.js").read_text(encoding="utf-8")
    helper = re.search(r"async function fetchJsonMutate\(.*?\n}\n", js, re.S)
    assert helper and '"X-Kite-Terminal": "1"' in helper.group(0)
    # Every raw fetch() outside that helper must be a plain GET: fetch(url)
    # with no options object, so it can never carry a method.
    rest = js.replace(helper.group(0), "")
    raw = []
    for m in re.finditer(r"\bfetch\(", rest):
        depth, i = 1, m.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(rest[i], 0)
            i += 1
        raw.append(rest[m.start():i])
    assert raw and all("," not in call and "{" not in call for call in raw), raw
    assert "XMLHttpRequest" not in js and "sendBeacon" not in js
