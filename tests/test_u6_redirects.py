"""U6 mitigation M1/M2: no HTTP redirect can resend a Kite broker request.

docs/U6_PROXY_REPLAY_INVESTIGATION.md found one automatic resend path: kiteconnect hard-codes
allow_redirects=True, and requests then follows a 307/308 by resending the same method and body (an
order, a GTT) to the Location URL, and a 301/302/303 by sending another request, without the caller
ever seeing the 3xx. M1 (kite_client.create_kite_connect) sets reqsession.max_redirects = 0 on every
client the factory builds, so the first 3xx raises requests.TooManyRedirects before anything is resent.
docs/U6_MITIGATION_REPORT.md has the analysis and the results.

A. The factory's transport policy: no redirects, zero retries (read retries off), no custom adapter,
   hook or SDK pool, on every client variant; a source audit that nothing in backend/ changes that.
B. The real stack on real sockets: a real factory client (fenced, proxied, trust_env off) talks to a
   local HTTP server that is its configured proxy, so the server's log is everything the client sent.
   Every write method answered by every redirect status: one request, TooManyRedirects, nothing sent to
   the redirect destination, no body resent. A positive control restores requests' default (30) and
   shows the same server sees the resend, so the harness can detect one.
C. Broker writes end to end: FakeKite, and the real client over a recorded transport, under the real
   order path. A redirect on a write is UNKNOWN (orders) / protection_failed (the protective GTT) /
   read back (GTT cancel); nothing is resubmitted; the Phase 2 fence and the narrowed clients still
   refuse before anything is sent.

Loopback only (127.0.0.1) and *.invalid host names that never resolve: nothing leaves this machine,
and no_direct_sockets refuses any connect beyond loopback.
"""

import ast
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests
from kiteconnect import KiteConnect as SDK
from kiteconnect.exceptions import KiteException

from backend import execution, gtt_cancellation, instance_lock, kite_client
from conftest import approval_body, set_mode, ticket_row
from test_baseline_api import _manual_ticket
from test_kite_rest_proxy import (  # noqa: F401 - fixtures
    PROXY_MAP, REAL_GET_KITE, _with_token, constructed, no_direct_sockets, proxy_env, real_kite, wire)
from test_live_safety import (  # noqa: F401 - fixtures
    _assert_unknown_response, _live_position, _monitor, _pending_exit, unsafe_client)
from test_order_path_assurance import _kite_replies
from test_partial_fills import PRICE, STOP, TARGET, _fill, _reconcile

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

REDIRECT_STATUSES = (301, 302, 303, 307, 308)
ORDER_ID = "250000000001"
GTT_ID = 9001
_GTT_ORDERS = [{"transaction_type": "SELL", "quantity": 1, "order_type": "LIMIT", "product": "CNC", "price": 2890.0}]

# (SDK method, HTTP method, path, kwargs): every broker write the application sends, plus the login's
# token exchange (a POST that creates the session).
WRITES = [
    ("place_order", "POST", "/orders/regular",
     dict(variety="regular", exchange="NSE", tradingsymbol="TCS", transaction_type="BUY", quantity=1,
          product="CNC", order_type="MARKET", tag="KTU6")),
    ("place_gtt", "POST", "/gtt/triggers",
     dict(trigger_type="single", tradingsymbol="TCS", exchange="NSE", trigger_values=[2900.0], last_price=3000.0,
          orders=_GTT_ORDERS)),
    ("generate_session", "POST", "/session/token", dict(request_token="req-token", api_secret="secret")),
    ("modify_order", "PUT", f"/orders/regular/{ORDER_ID}", dict(variety="regular", order_id=ORDER_ID, price=2990.0)),
    ("modify_gtt", "PUT", f"/gtt/triggers/{GTT_ID}",
     dict(trigger_id=GTT_ID, trigger_type="single", tradingsymbol="TCS", exchange="NSE", trigger_values=[2850.0],
          last_price=3000.0, orders=_GTT_ORDERS)),
    ("cancel_order", "DELETE", f"/orders/regular/{ORDER_ID}", dict(variety="regular", order_id=ORDER_ID)),
    ("delete_gtt", "DELETE", f"/gtt/triggers/{GTT_ID}", dict(trigger_id=GTT_ID)),
]
_WRITE_IDS = [w[0] for w in WRITES]
MATRIX = [(status, *write) for write in WRITES for status in REDIRECT_STATUSES]
_MATRIX_IDS = [f"{w[0]}-{s}" for w in WRITES for s in REDIRECT_STATUSES]

# The six cases the U6 task names explicitly are all in MATRIX: POST 307/308, PUT 301/307, DELETE 301/308.
REQUIRED = {("POST", 307), ("POST", 308), ("PUT", 301), ("PUT", 307), ("DELETE", 301), ("DELETE", 308)}
assert REQUIRED <= {(m, s) for s, _, m, _, _ in MATRIX}


def _follow_up(status: int, method: str) -> tuple[str, bool]:
    """What requests' resolve_redirects resends WITHOUT M1 (requests 2.x sessions.rebuild_method and the
    body purge): (method, body kept)."""
    if status in (302, 303) or (status == 301 and method == "POST"):
        return "GET", False
    return method, status in (307, 308)


# ---------------------------------------------------------------- A. the factory's transport policy

def _variants(monkeypatch):
    """Every kind of client the factory builds: (label, client)."""
    built = []
    for label, proxy, writes in (("proxied", "http://10.77.0.1:3128", None), ("direct", None, None),
                                 ("proxied-readonly", "http://10.77.0.1:3128", "disabled"),
                                 ("direct-readonly", None, "disabled")):
        if proxy:
            monkeypatch.setenv("KITE_REST_PROXY", proxy)
        else:
            monkeypatch.delenv("KITE_REST_PROXY", raising=False)
        if writes:
            monkeypatch.setenv(kite_client.BROKER_WRITES_ENV, writes)
        else:
            monkeypatch.delenv(kite_client.BROKER_WRITES_ENV, raising=False)
        built.append((label, kite_client.create_kite_connect("test_api_key")))
    return built


def test_a1_every_factory_client_follows_no_redirect(monkeypatch):
    kinds = {}
    for label, kite in _variants(monkeypatch):
        assert kite.reqsession.max_redirects == 0, label
        kinds[label] = type(kite)
    # M1 changes nothing else about the client: the same classes, the same fence, the same route.
    assert kinds == {"proxied": SDK, "direct": kite_client.DirectRouteKiteConnect,
                     "proxied-readonly": kite_client.ReadOnlyKiteConnect,
                     "direct-readonly": kite_client.ReadOnlyKiteConnect}


def test_a2_every_mounted_adapter_has_zero_retries_and_no_read_retry(monkeypatch):
    for label, kite in _variants(monkeypatch):
        session = kite.reqsession
        # Only the two adapters requests.Session mounts itself: no SDK pool, no custom adapter.
        assert set(session.adapters) == {"https://", "http://"}, label
        for prefix, adapter in session.adapters.items():
            assert type(adapter) is requests.adapters.HTTPAdapter, (label, prefix)
            retry = adapter.max_retries
            assert retry.total == 0, (label, prefix, retry)
            assert retry.read is False, (label, prefix, retry)
            assert not retry.status_forcelist and retry.connect in (None, 0) and retry.redirect in (None, 0), retry
        # Nothing else could act on a 3xx before resolve_redirects: no response hooks.
        assert session.hooks == {"response": []}, label


def test_a3_the_proxied_client_keeps_its_net1_settings(monkeypatch):
    monkeypatch.setenv("KITE_REST_PROXY", "http://10.77.0.1:3128")
    monkeypatch.delenv(kite_client.BROKER_WRITES_ENV, raising=False)
    kite = kite_client.create_kite_connect("test_api_key")
    assert kite.proxies == PROXY_MAP and kite.reqsession.trust_env is False
    assert vars(kite).get("_kt_write_fence") is True and "_request" in vars(kite)  # the Phase 2 fence
    assert (kite.root, kite.timeout, kite.disable_ssl) == ("https://api.kite.trade", 7, False)


def test_a4_every_client_the_application_obtains_comes_from_the_policy(env, proxy_env, wire, monkeypatch):
    """get_kite (every route and loop), the login URL and the token exchange: each client has M1."""
    _with_token(env)
    monkeypatch.setattr(kite_client, "get_kite", REAL_GET_KITE)
    seen = []
    factory = kite_client.create_kite_connect

    def recording(api_key):
        kite = factory(api_key)
        seen.append(kite)
        return kite
    monkeypatch.setattr(kite_client, "create_kite_connect", recording)
    kite_client.get_kite()
    kite_client.require_kite()
    kite_client.build_login_url()
    kite_client.exchange_request_token("req-token-abc")
    assert len(seen) == 4
    assert all(k.reqsession.max_redirects == 0 and k.reqsession.trust_env is False for k in seen)


def test_a5_a_stand_in_client_is_left_alone(monkeypatch):
    """The demo and some tests replace kite_client.KiteConnect with a stand-in without a requests
    session (test_proxy_mandatory test_17); M1 must not break them."""
    monkeypatch.delenv("KITE_REST_PROXY", raising=False)

    class StandIn:
        def __init__(self, api_key, **kwargs):
            self.api_key = api_key
    monkeypatch.setattr(kite_client, "KiteConnect", StandIn)
    assert type(kite_client.create_kite_connect("k")) is StandIn


def test_a6_source_audit_nothing_in_backend_bypasses_the_policy():
    texts = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in BACKEND.rglob("*.py")}
    # (`app.mount(` is FastAPI's static-files mount in main.py, not a requests adapter.)
    retry_config = re.compile(r"(?<!\bapp)\.mount\(|HTTPAdapter|\bRetry\(|max_retries"
                              r"|^\s*(?:import|from)\s+(?:tenacity|backoff)\b", re.M)
    assert [path for path, text in texts.items() if retry_config.search(text)] == []
    # Redirect policy is set in one place, and nothing in backend/ turns redirects back on.
    assert [path for path, text in texts.items() if "max_redirects" in text] == ["backend/kite_client.py"]
    assert [path for path, text in texts.items() if re.search(r"allow_redirects\s*=\s*True", text)] == []
    assert [path for path, text in texts.items() if re.search(r"reqsession\.\w+\s*=", text)] == [
        "backend/kite_client.py"]
    # The factory passes no SDK option that would replace the session's adapters or retries.
    factory = next(n for n in ast.walk(ast.parse(texts["backend/kite_client.py"]))
                   if isinstance(n, ast.FunctionDef) and n.name == "create_kite_connect")
    calls = [n for n in ast.walk(factory) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "KiteConnect"]
    assert len(calls) == 2
    assert all({k.arg for k in c.keywords} <= {"api_key", "proxies"} for c in calls)
    # And M1 itself: one literal setting on the proxied client, and the helper for the direct one.
    assert re.search(r"^\s*kite\.reqsession\.max_redirects = 0\b", texts["backend/kite_client.py"], re.M)


# ---------------------------------------------------------------- B. real requests on real sockets

ORIGIN = "http://kite-origin.invalid"            # never resolves; only the local proxy ever sees it
REDIRECT_PATH = "/redirected/landing"
LOCATION = ORIGIN + REDIRECT_PATH                 # same host: the worst case (Authorization is kept)
FOLLOW_UP_DATA = {"order_id": "SECOND-ORDER", "trigger_id": 2, "access_token": "tok2", "public_token": "pub",
                  "user_id": "DT0001", "user_name": "Demo Trader", "login_time": None}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        server = self.server
        with server.log_lock:
            server.log.append({"method": self.command, "target": self.path, "body": body,
                               "headers": {k.lower(): v for k, v in self.headers.items()}})
        if REDIRECT_PATH in self.path or server.status == 200:
            status, payload = 200, {"status": "success", "data": FOLLOW_UP_DATA}
        else:
            status, payload = server.status, {"status": "error", "error_type": "GeneralException",
                                              "message": "redirect body; never seen by the caller"}
        raw = json.dumps(payload).encode()
        self.send_response(status)
        if status != 200:
            self.send_header("Location", LOCATION)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PUT = do_DELETE = _serve


class LocalKiteProxy(ThreadingHTTPServer):
    """A loopback HTTP server used as the client's KITE_REST_PROXY. The client's root is the
    unresolvable ORIGIN, so every request it makes - the original and any redirect it follows -
    arrives here as an absolute-URI proxy request, and `log` is the complete record of what was sent.
    It answers the original with `status` (and Location: LOCATION), and LOCATION with 200."""
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.log: list[dict] = []
        self.log_lock = threading.Lock()
        self.status = 200
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def stop(self):
        self.shutdown()
        self.server_close()


@pytest.fixture
def local_proxy(monkeypatch, no_direct_sockets, owner_process):
    server = LocalKiteProxy()
    monkeypatch.setenv("KITE_REST_PROXY", server.url)
    monkeypatch.delenv(kite_client.BROKER_WRITES_ENV, raising=False)
    yield server
    server.stop()
    assert no_direct_sockets == []


def _client():
    kite = kite_client.create_kite_connect("test_api_key")
    kite.set_access_token("tok")
    kite.root = ORIGIN   # TLS-free and unresolvable; everything still goes through the configured proxy
    return kite


@pytest.mark.parametrize("status,name,method,path,kwargs", MATRIX, ids=_MATRIX_IDS)
def test_b1_a_redirected_write_is_sent_once_and_never_resent(local_proxy, status, name, method, path, kwargs):
    local_proxy.status = status
    kite = _client()
    assert type(kite) is SDK and kite.reqsession.max_redirects == 0
    with pytest.raises(requests.exceptions.TooManyRedirects) as raised:
        getattr(kite, name)(**kwargs)
    # The 3xx itself is in the exception, for the caller's log; nothing followed it.
    assert raised.value.response.status_code == status
    assert raised.value.response.headers["Location"] == LOCATION
    # A RequestException, not a Kite refusal: the order paths record it as UNKNOWN, never "rejected".
    assert isinstance(raised.value, requests.exceptions.RequestException)
    assert not isinstance(raised.value, KiteException)
    assert not isinstance(raised.value, execution.DEFINITE_REJECTIONS)
    assert not isinstance(raised.value, gtt_cancellation._DEFINITE_REFUSALS)
    # Exactly one request, the original, and nothing to the redirect destination.
    [only] = local_proxy.log
    assert (only["method"], only["target"].split("?")[0]) == (method, ORIGIN + path), local_proxy.log
    assert not any(REDIRECT_PATH in r["target"] for r in local_proxy.log)
    # The body went out once, with the original request, and was never resent.
    bodies = [r["body"] for r in local_proxy.log if r["body"]]
    assert bodies == ([only["body"]] if method in ("POST", "PUT") else []), bodies
    # The fence counted the write and released it.
    assert instance_lock._state.in_flight == 0


@pytest.mark.parametrize("status", REDIRECT_STATUSES)
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_b2_302_303_and_301_never_turn_a_write_into_a_get(local_proxy, method, status):
    """302/303 (any method) and 301 on POST would become a body-less GET without M1."""
    name, _, path, kwargs = next(w for w in WRITES if w[1] == method)
    local_proxy.status = status
    with pytest.raises(requests.exceptions.TooManyRedirects):
        getattr(_client(), name)(**kwargs)
    assert [r["method"] for r in local_proxy.log] == [method]
    assert not any(r["method"] == "GET" for r in local_proxy.log)


@pytest.mark.parametrize("name,method,path,kwargs", WRITES, ids=_WRITE_IDS)
def test_b3_a_normal_response_is_unchanged(local_proxy, name, method, path, kwargs):
    local_proxy.status = 200
    result = getattr(_client(), name)(**kwargs)
    assert result is not None
    assert [(r["method"], r["target"].split("?")[0]) for r in local_proxy.log] == [(method, ORIGIN + path)]


@pytest.mark.parametrize("status", REDIRECT_STATUSES)
def test_b4_a_redirected_read_fails_instead_of_following(local_proxy, status):
    """The GET implication of M1: a read answered with a 3xx fails (TooManyRedirects, a transport
    failure) instead of being followed. Kite's REST API is not known to redirect any read."""
    local_proxy.status = status
    with pytest.raises(requests.exceptions.TooManyRedirects):
        _client().holdings()
    assert [(r["method"], r["target"]) for r in local_proxy.log] == [("GET", ORIGIN + "/portfolio/holdings")]


@pytest.mark.parametrize("status,name,method,path,kwargs", MATRIX, ids=_MATRIX_IDS)
def test_b5_positive_control_without_m1_the_same_harness_sees_the_resend(local_proxy, status, name, method, path,
                                                                         kwargs):
    """requests' default (30) restored on one client: the server receives the follow-up that M1
    prevents - for 307/308 the same method with the same body and the Authorization header - and the
    SDK call returns the FOLLOW-UP's answer as if it were the original's. This proves the harness above
    would see a resend, and documents exactly what M1 closes."""
    local_proxy.status = status
    kite = _client()
    kite.reqsession.max_redirects = 30
    try:
        getattr(kite, name)(**kwargs)
    except Exception:  # noqa: BLE001 - the SDK's parsing of the follow-up's answer is not the point
        pass
    assert len(local_proxy.log) == 2, local_proxy.log
    first, second = local_proxy.log
    want_method, body_kept = _follow_up(status, method)
    assert second["target"].startswith(LOCATION) and second["method"] == want_method
    assert second["body"] == (first["body"] if body_kept else b"")
    if body_kept:
        assert second["headers"].get("authorization") == first["headers"].get("authorization") == "token test_api_key:tok"


def test_b6_positive_control_a_307_order_returns_the_follow_ups_order_id(local_proxy):
    local_proxy.status = 307
    kite = _client()
    kite.reqsession.max_redirects = 30
    assert kite.place_order(**WRITES[0][3]) == "SECOND-ORDER"   # the caller never saw the 307
    assert [r["method"] for r in local_proxy.log] == ["POST", "POST"]


# ---------------------------------------------------------------- C. broker writes end to end

def _redirect(status=307) -> requests.exceptions.TooManyRedirects:
    """What the SDK raises for a redirected write under M1 (as in B)."""
    response = requests.Response()
    response.status_code = status
    response.headers["Location"] = LOCATION
    return requests.exceptions.TooManyRedirects("Exceeded 0 redirects.", response=response)


_OUTCOMES = pytest.mark.parametrize("inject", ["raise_on_next", "accept_then_raise"],
                                    ids=["kite-did-not-process", "kite-processed"])


def _live_entry(env, inject_name, call, exc):
    set_mode(env.db, "live")
    env.kite.ltp_map["NSE:TCS"] = PRICE
    env.kite.auto_fill = False
    getattr(env.kite, inject_name)(call, exc)
    t = env.client.post("/api/tickets", json=_manual_ticket(entry_price=PRICE, stop_loss=STOP,
                                                            target_1=TARGET)).json()["data"]
    return t, env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"]))


@_OUTCOMES
def test_c1_a_redirected_entry_order_is_unknown_and_never_resubmitted(env, market_open, inject):
    t, r = _live_entry(env, inject, "place_order", _redirect(307))
    _assert_unknown_response(r)
    assert "TooManyRedirects" in r.json()["data"]["execution_message"]
    again = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"]))
    assert again.status_code == 409
    for _ in range(2):
        _monitor(env)
        _reconcile()
    row = ticket_row(env.db, t["id"])
    # Not processed: it stays UNKNOWN (the monitor keeps looking for the tag). Processed: adopted from the
    # book by its tag. Either way exactly one order, and no protective GTT created by itself.
    assert row["submission_state"] == ("unknown" if inject == "raise_on_next" else "adopted"), dict(row)
    assert len(env.kite.calls_to("place_order")) == 1
    assert env.kite.calls_to("place_gtt") == []


@_OUTCOMES
def test_c2_a_redirected_exit_order_is_unknown_and_never_resubmitted(env, unsafe_client, market_open, inject):
    set_mode(env.db, "live")
    buy_id, gtt_id = _live_position(env)
    sell_id = _pending_exit(env, buy_id)
    env.kite.auto_fill = False
    getattr(env.kite, inject)("place_order", _redirect(308))
    _assert_unknown_response(unsafe_client.post(f"/api/tickets/{sell_id}/confirm"))
    assert unsafe_client.post(f"/api/tickets/{sell_id}/confirm").status_code == 409
    for _ in range(2):
        _monitor(env)
        _reconcile()
    sells = [c for c in env.kite.calls_to("place_order") if c["transaction_type"] == "SELL"]
    assert len(sells) == 1
    assert ticket_row(env.db, buy_id)["closed_at"] is None   # nothing filled: the position is still open


@_OUTCOMES
def test_c3_a_redirected_protective_gtt_is_never_placed_again(env, market_open, inject):
    t, r = _live_entry(env, inject, "place_gtt", _redirect(307))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "executed" and data["kite_gtt_id"] is None
    assert "UNPROTECTED POSITION" in data["execution_message"]
    _fill(env, data["kite_order_id"], 5, status="COMPLETE")
    for _ in range(2):
        _monitor(env)
        _reconcile()
    # One attempt, never retried or re-created, whether or not Kite created it (F9: a GTT Kite did create
    # stays at the broker untracked and is a Phase 3 design input; it is not duplicated here).
    assert len(env.kite.calls_to("place_gtt")) == 1
    assert len(env.kite.gtt_book) == (0 if inject == "raise_on_next" else 1)
    assert ticket_row(env.db, t["id"])["kite_gtt_id"] is None


@pytest.mark.parametrize("inject,state", [("raise_on_next", gtt_cancellation.FAILED),
                                          ("accept_then_raise", gtt_cancellation.CONFIRMED)],
                         ids=["kite-did-not-process", "kite-processed"])
def test_c4_a_redirected_gtt_delete_is_read_back_never_resent(env, inject, state):
    gtt_id = env.kite.place_gtt(trigger_type="single", tradingsymbol="TCS", exchange="NSE", trigger_values=[2900.0],
                                last_price=3000.0, orders=_GTT_ORDERS)["trigger_id"]
    getattr(env.kite, inject)("delete_gtt", _redirect(301))
    outcome = gtt_cancellation.cancel(env.kite, gtt_id)
    # Only the broker's read-back decides: still ACTIVE is FAILED, listed deleted is CONFIRMED.
    assert outcome.state == state and isinstance(outcome.error, requests.exceptions.TooManyRedirects)
    assert len(env.kite.calls_to("delete_gtt")) == 1


class RedirectingWire:
    """requests.adapters.HTTPAdapter.send under the REAL Session.send / resolve_redirects: answers the
    chosen (method, path) with a 3xx and a Location, everything else with a canned Kite reply."""

    def __init__(self, reply_for):
        self.sends: list[dict] = []
        self.redirects: dict[tuple[str, str], int] = {}
        self.reply_for = reply_for

    def send(self, adapter, request, **kwargs):
        url = requests.utils.urlparse(request.url)
        self.sends.append({"method": request.method, "host": url.hostname, "path": url.path,
                           "body": request.body, "proxies": dict(kwargs.get("proxies") or {})})
        response = requests.Response()
        response.url, response.request = request.url, request
        response.headers["content-type"] = "application/json"
        status = self.redirects.get((request.method, url.path))
        if status is None or REDIRECT_PATH in url.path:
            response.status_code = 200
            response._content = json.dumps({"status": "success",
                                            "data": self.reply_for(request.method, url.path)}).encode()
        else:
            response.status_code = status
            response.headers["Location"] = "https://api.kite.trade" + REDIRECT_PATH
            response._content = b'{"status": "error", "error_type": "GeneralException", "message": "moved"}'
        response._content_consumed = True
        return response


def _order_path_replies(method, path):
    if method == "POST" and path.startswith("/orders/"):
        return {"order_id": "250000000555"}
    if method == "POST" and path == "/gtt/triggers":
        return {"trigger_id": 9555}
    if method == "GET" and path == "/gtt/triggers":
        return [{"id": GTT_ID, "status": "active"}]
    return _kite_replies(method, path)


@pytest.fixture
def redirecting(real_kite, monkeypatch):  # noqa: F811
    """The real get_kite (real factory client, M1, fence) on the test database, proxy configured, with
    the transport below requests replaced by a RedirectingWire."""
    from backend import margin_preflight
    monkeypatch.setattr(margin_preflight, "check", lambda kite, ticket, variety, ltp: ([], {}))
    w = RedirectingWire(_order_path_replies)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", lambda adapter, request, **kw: w.send(adapter, request, **kw))
    return w


def _writes(w):
    return [(s["method"], s["path"]) for s in w.sends if s["method"] != "GET"]


@pytest.mark.parametrize("status", [307, 308])
def test_c5_real_stack_a_redirected_entry_is_unknown_one_post_no_follow_up(real_kite, redirecting, market_open,  # noqa: F811
                                                                           status):
    env, w = real_kite, redirecting
    w.redirects[("POST", "/orders/regular")] = status
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"]))
    _assert_unknown_response(r)
    assert "TooManyRedirects" in r.json()["data"]["execution_message"]
    assert env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"])).status_code == 409
    assert _writes(w) == [("POST", "/orders/regular")]
    assert not any(REDIRECT_PATH in s["path"] for s in w.sends)
    assert all(s["host"] == "api.kite.trade" and s["proxies"] == PROXY_MAP for s in w.sends)
    assert ticket_row(env.db, t["id"])["submission_state"] == "unknown"


def test_c6_real_stack_a_redirected_protective_gtt_is_one_post_no_follow_up(real_kite, redirecting, market_open):  # noqa: F811
    env, w = real_kite, redirecting
    w.redirects[("POST", "/gtt/triggers")] = 307
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"]))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "executed" and data["kite_gtt_id"] is None
    assert "UNPROTECTED POSITION" in data["execution_message"]
    assert _writes(w) == [("POST", "/orders/regular"), ("POST", "/gtt/triggers")]
    assert not any(REDIRECT_PATH in s["path"] for s in w.sends)


def test_c7_real_stack_a_redirected_gtt_delete_is_one_delete_then_a_read_back(real_kite, redirecting):  # noqa: F811
    w = redirecting
    w.redirects[("DELETE", f"/gtt/triggers/{GTT_ID}")] = 308
    outcome = gtt_cancellation.cancel(kite_client.get_kite(), GTT_ID)
    assert outcome.state == gtt_cancellation.FAILED and outcome.broker_status == "active"
    assert isinstance(outcome.error, requests.exceptions.TooManyRedirects)
    assert [(s["method"], s["path"]) for s in w.sends] == [("DELETE", f"/gtt/triggers/{GTT_ID}"),
                                                           ("GET", "/gtt/triggers")]


@pytest.mark.parametrize("name,method,path,kwargs", WRITES, ids=_WRITE_IDS)
def test_c8_the_phase2_fence_still_refuses_before_anything_is_sent(real_kite, redirecting, name, method, path,  # noqa: F811
                                                                   kwargs):
    w = redirecting
    w.redirects[(method, path)] = 307
    kite = kite_client.get_kite()
    instance_lock.reset_for_tests()                 # this process is no longer the owner
    with pytest.raises(instance_lock.InstanceNotOwner):
        getattr(kite, name)(**kwargs)
    assert w.sends == []


@pytest.mark.parametrize("route", ["direct", "writes-disabled"])
@pytest.mark.parametrize("name,method,path,kwargs", [w for w in WRITES if w[0] != "generate_session"],
                         ids=[w[0] for w in WRITES if w[0] != "generate_session"])
def test_c9_narrowed_clients_still_refuse_writes_locally(real_kite, redirecting, monkeypatch, route,  # noqa: F811
                                                         name, method, path, kwargs):
    w = redirecting
    w.redirects[(method, path)] = 307
    if route == "direct":
        monkeypatch.delenv("KITE_REST_PROXY", raising=False)
        refused = kite_client.DirectRouteWritesRefused
    else:
        monkeypatch.setenv(kite_client.BROKER_WRITES_ENV, "disabled")
        refused = kite_client.BrokerWritesDisabled
    kite = kite_client.get_kite()
    assert kite.reqsession.max_redirects == 0
    with pytest.raises(refused):
        getattr(kite, name)(**kwargs)
    assert w.sends == []


def test_c10_phase2_restrictions_are_unchanged():
    """M1 touches only the transport: the LIVE-entry gate is still closed in the source, and the
    factory still fences every client it builds."""
    gate = (BACKEND / "live_entry_gate.py").read_text(encoding="utf-8")
    assert re.search(r"^LIVE_ENTRIES_ENABLED = False\b", gate, re.M)
    source = (BACKEND / "kite_client.py").read_text(encoding="utf-8")
    factory = source[source.index("def create_kite_connect"):source.index("def _resolve_account")]
    assert factory.count("return fence_broker(") == 2
