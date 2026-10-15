"""NET-1: Kite REST through the static-IP proxy (KITE_REST_PROXY).

Routing is proven on the wire boundary: a REAL KiteConnect built by
kite_client.create_kite_connect, with requests' HTTPAdapter.send replaced by
a recorder that captures (method, url, proxies) and returns a canned Kite
reply — or raises. That exercises KiteConnect._request and requests' own
proxy merging, so the recorded `proxies` are what the socket layer would
use. Nothing reaches the network; no real order or GTT is ever sent.

The rest uses the ordinary FakeKite fixtures: fail-closed order handling
(PK-2 UNKNOWN), Live Readiness (LIVE-0) and the DRY RUN path.
"""

import ast
import json
import re
from datetime import date
from pathlib import Path

import pytest
import requests
from fastapi import HTTPException
from kiteconnect.exceptions import TokenException

from backend import kite_client, kite_rest_proxy
from conftest import TEST_STATIC_IP, displayed_mode, set_mode, ticket_row
from test_baseline_api import _manual_ticket
from test_live_preflight import _check, _evaluate, ready  # noqa: F401 — fixture

# conftest's `env` fixture replaces kite_client.get_kite with a FakeKite; the
# routing tests need the real one on top of that throwaway database.
REAL_GET_KITE = kite_client.get_kite

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

PROXY_URL = "http://10.77.0.1:3128"
PROXY_MAP = {"http": PROXY_URL, "https": PROXY_URL}
SNAPSHOT_READS = ("profile", "holdings", "positions", "margins", "orders", "get_gtts", "ltp")
MUTATING_BROKER_CALLS = ("place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt")


# ---------------------------------------------------------------- wire recorder

class Wire:
    """Stands in for requests.adapters.HTTPAdapter.send."""

    def __init__(self):
        self.sends: list[dict] = []
        self.raise_exc: Exception | None = None

    def reply_for(self, method: str, path: str):
        if path == "/session/token":
            return {"access_token": "tok-net1-xyz", "public_token": "pub", "user_id": "DT0001",
                    "user_name": "Demo Trader", "login_time": "2026-10-15 09:00:00"}
        if path.startswith("/orders/") and method in ("POST", "PUT", "DELETE"):
            return {"order_id": "250000000099"}
        if path.startswith("/gtt/triggers") and method in ("POST", "PUT", "DELETE"):
            return {"trigger_id": 9001}
        if path.startswith("/instruments/historical/"):
            return {"candles": []}
        if path in ("/portfolio/holdings", "/orders", "/gtt/triggers"):
            return []
        if path == "/portfolio/positions":
            return {"net": [], "day": []}
        return {}

    def send(self, adapter, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        url = requests.utils.urlparse(request.url)
        self.sends.append({"method": request.method, "host": url.hostname, "path": url.path,
                           "proxies": dict(proxies or {})})
        if self.raise_exc is not None:
            raise self.raise_exc
        response = requests.Response()
        response.status_code = 200
        response.headers["content-type"] = "application/json"
        response._content = json.dumps({"status": "success",
                                        "data": self.reply_for(request.method, url.path)}).encode()
        response.url = request.url
        response.request = request
        return response


@pytest.fixture
def wire(monkeypatch):
    w = Wire()
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send",
                        lambda adapter, request, **kw: w.send(adapter, request, **kw))
    return w


@pytest.fixture
def constructed(monkeypatch):
    """Every KiteConnect kite_client builds: its constructor kwargs."""
    made: list[dict] = []
    real = kite_client.KiteConnect

    class Recording(real):
        def __init__(self, *args, **kwargs):
            made.append(dict(kwargs))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(kite_client, "KiteConnect", Recording)
    return made


@pytest.fixture
def proxy_env(monkeypatch):
    monkeypatch.setenv("KITE_REST_PROXY", PROXY_URL)


def _with_token(env):
    from backend import crypto
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE accounts SET access_token_enc = ?", (crypto.encrypt("fake-access-token"),))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def real_kite(env, monkeypatch, proxy_env, wire):
    """The real get_kite on the test database, proxy configured."""
    _with_token(env)
    monkeypatch.setattr(kite_client, "get_kite", REAL_GET_KITE)
    return env


def _all_proxied(wire):
    assert wire.sends, "no request reached the wire"
    for s in wire.sends:
        assert s["host"] == "api.kite.trade", s
        assert s["proxies"] == PROXY_MAP, s


# ---------------------------------------------------------------- 1-8 configuration

def test_01_unset_builds_a_direct_client(constructed):
    kite = kite_client.create_kite_connect("key")
    assert constructed == [{"api_key": "key"}]
    assert kite.proxies == {}
    assert kite_rest_proxy.route_status() == {"mode": "DIRECT", "endpoint": None, "error": None}


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_02_blank_is_direct(monkeypatch, constructed, blank):
    monkeypatch.setenv("KITE_REST_PROXY", blank)
    kite = kite_client.create_kite_connect("key")
    assert constructed == [{"api_key": "key"}] and kite.proxies == {}
    assert kite_rest_proxy.route_status()["mode"] == "DIRECT"


def test_03_http_proxy_gives_kiteconnect_the_exact_mapping(proxy_env, constructed):
    kite = kite_client.create_kite_connect("key")
    assert constructed == [{"api_key": "key", "proxies": PROXY_MAP}]
    assert kite.proxies == PROXY_MAP
    assert kite.reqsession.trust_env is False
    assert kite_rest_proxy.route_status() == {"mode": "PROXY", "endpoint": "10.77.0.1:3128", "error": None}


@pytest.mark.parametrize("value, expected", [
    ("https://proxy.internal:8443", {"http": "https://proxy.internal:8443", "https": "https://proxy.internal:8443"}),
    ("HTTP://10.77.0.1:3128/", PROXY_MAP),
    ("  http://10.77.0.1:3128  ", PROXY_MAP),
    ("http://[fd00::1]:3128", {"http": "http://[fd00::1]:3128", "https": "http://[fd00::1]:3128"}),
])
def test_04_https_and_normalised_forms_are_accepted(value, expected):
    assert kite_rest_proxy.parse(value).requests_proxies == expected


@pytest.mark.parametrize("value", [
    "10.77.0.1:3128",            # no scheme
    "10.77.0.1",
    "//10.77.0.1:3128",
    "http://",                   # empty hostname
    "http://:3128",
    "http://10.77.0.1",          # no port
    "http://10.77.0.1:",
    "http://10.77.0.1:3128/path",
    "http://10.77.0.1:3128?x=1",
    "http://10.77.0.1:3128#frag",
    "http://10.77.0.1 :3128",
])
def test_05_malformed_proxy_is_rejected(value):
    with pytest.raises(kite_rest_proxy.KiteRestProxyError):
        kite_rest_proxy.parse(value)


@pytest.mark.parametrize("value", ["ftp://10.77.0.1:3128", "socks5://10.77.0.1:1080", "file://10.77.0.1:3128",
                                   "ws://10.77.0.1:3128"])
def test_06_unsupported_scheme_is_rejected(value):
    with pytest.raises(kite_rest_proxy.KiteRestProxyError, match="only the http and https schemes"):
        kite_rest_proxy.parse(value)


@pytest.mark.parametrize("value", ["http://10.77.0.1:0", "http://10.77.0.1:65536", "http://10.77.0.1:70000",
                                   "http://10.77.0.1:abc", "http://10.77.0.1:-1"])
def test_07_invalid_port_is_rejected(value):
    with pytest.raises(kite_rest_proxy.KiteRestProxyError, match="port"):
        kite_rest_proxy.parse(value)


CRED_URL = "http://netuser:S3cretPw@10.77.0.1:3128"


def test_08_credentials_are_refused_and_never_echoed(monkeypatch, caplog, constructed):
    with pytest.raises(kite_rest_proxy.KiteRestProxyError) as raised:
        kite_rest_proxy.parse(CRED_URL)
    assert "credentials" in str(raised.value)
    monkeypatch.setenv("KITE_REST_PROXY", CRED_URL)
    status = kite_rest_proxy.route_status()
    assert status["mode"] == "INVALID" and status["endpoint"] is None
    with pytest.raises(kite_rest_proxy.KiteRestProxyError):
        kite_client.create_kite_connect("key")
    assert constructed == []
    from backend.server import RuntimeConfigError, enforce_runtime_policy
    with caplog.at_level("DEBUG"), pytest.raises(RuntimeConfigError):
        enforce_runtime_policy(env={"KITE_REST_PROXY": CRED_URL}, argv=["python"], allowed_hosts={"127.0.0.1"})
    for text in (str(raised.value), json.dumps(status), caplog.text):
        assert "S3cretPw" not in text and "netuser" not in text and CRED_URL not in text


@pytest.mark.parametrize("value", ["10.77.0.1:3128", "ftp://10.77.0.1:3128", "http://10.77.0.1:99999"])
def test_08b_errors_never_echo_the_raw_value(value):
    with pytest.raises(kite_rest_proxy.KiteRestProxyError) as raised:
        kite_rest_proxy.parse(value)
    assert value not in str(raised.value) and "10.77.0.1" not in str(raised.value)


def test_08c_startup_logs_only_the_sanitized_endpoint(caplog):
    from backend.server import enforce_runtime_policy
    with caplog.at_level("INFO"):
        enforce_runtime_policy(env={"KITE_REST_PROXY": PROXY_URL}, argv=["python"], allowed_hosts={"127.0.0.1"})
    assert "Kite REST proxy enabled: 10.77.0.1:3128" in caplog.text
    with caplog.at_level("INFO"):
        enforce_runtime_policy(env={}, argv=["python"], allowed_hosts={"127.0.0.1"})
    assert "Kite REST: direct (KITE_REST_PROXY not set)" in caplog.text


def test_08d_launcher_refuses_an_invalid_proxy(monkeypatch, capsys):
    import dotenv
    import uvicorn
    from backend import server
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("the launcher started despite an invalid proxy"))
    monkeypatch.setenv("KITE_REST_PROXY", "10.77.0.1:3128")
    assert server.main([]) == 2
    err = capsys.readouterr().err
    assert "Refusing to start" in err and "KITE_REST_PROXY is invalid" in err and "10.77.0.1" not in err


# ---------------------------------------------------------------- 9-14 every REST call uses the proxy

def test_09_profile_goes_through_the_proxy(real_kite, wire):
    kite_client.get_kite().profile()
    _all_proxied(wire)
    assert wire.sends[-1]["path"] == "/user/profile"


def test_10_holdings_use_the_same_proxied_client(real_kite, wire):
    kite = kite_client.get_kite()
    kite.holdings()
    kite.positions()
    kite.margins()
    kite.orders()
    kite.get_gtts()
    kite.ltp("NSE:TCS")
    kite.quote("NSE:TCS")
    _all_proxied(wire)
    assert [s["path"] for s in wire.sends] == ["/portfolio/holdings", "/portfolio/positions", "/user/margins",
                                               "/orders", "/gtt/triggers", "/quote/ltp", "/quote"]


def test_10b_rest_route_endpoints_use_the_proxy(real_kite, wire):
    r = real_kite.client.get("/api/holdings")
    assert r.status_code == 200, r.text
    _all_proxied(wire)
    assert wire.sends[-1]["path"] == "/portfolio/holdings"


def test_11_order_placement_uses_the_same_proxied_client(real_kite, wire):
    kite = kite_client.require_trading_account(kite_client.get_kite())
    assert kite.proxies == PROXY_MAP and kite.reqsession.trust_env is False
    kite.place_order(variety="regular", exchange="NSE", tradingsymbol="TCS", transaction_type="BUY", quantity=1,
                     product="CNC", order_type="LIMIT", price=3000.0, tag="KT1")
    kite.modify_order(variety="regular", order_id="250000000099", price=2990.0)
    kite.cancel_order(variety="regular", order_id="250000000099")
    _all_proxied(wire)
    assert [(s["method"], s["path"]) for s in wire.sends] == [
        ("POST", "/orders/regular"), ("PUT", "/orders/regular/250000000099"), ("DELETE", "/orders/regular/250000000099")]


def test_11b_trading_dependency_returns_the_proxied_client_itself(real_kite):
    kite = kite_client.require_kite()
    assert kite_client.require_trading_account(kite) is kite


def test_12_gtt_mutations_use_the_same_proxied_client(real_kite, wire):
    kite = kite_client.require_trading_account(kite_client.get_kite())
    orders = [{"transaction_type": "SELL", "quantity": 1, "price": 2800.0, "order_type": "LIMIT", "product": "CNC"}]
    kite.place_gtt(trigger_type="single", tradingsymbol="TCS", exchange="NSE", trigger_values=[2800.0],
                   last_price=3000.0, orders=orders)
    kite.modify_gtt(trigger_id=9001, trigger_type="single", tradingsymbol="TCS", exchange="NSE",
                    trigger_values=[2850.0], last_price=3000.0, orders=orders)
    kite.delete_gtt(9001)
    _all_proxied(wire)
    assert [(s["method"], s["path"]) for s in wire.sends] == [
        ("POST", "/gtt/triggers"), ("PUT", "/gtt/triggers/9001"), ("DELETE", "/gtt/triggers/9001")]


def test_13_historical_data_uses_the_same_proxied_client(real_kite, wire):
    kite_client.get_kite().historical_data(408065, date(2026, 1, 1), date(2026, 3, 1), "day")
    _all_proxied(wire)
    assert wire.sends[-1]["path"] == "/instruments/historical/408065/day"


def test_14_login_token_exchange_goes_through_the_proxy(env, proxy_env, wire, constructed):
    data = kite_client.exchange_request_token("req-token-abc")
    assert data["access_token"] == "tok-net1-xyz"
    _all_proxied(wire)
    assert [(s["method"], s["path"]) for s in wire.sends] == [("POST", "/session/token")]
    assert constructed == [{"api_key": "test_api_key", "proxies": PROXY_MAP}]


def test_14b_login_url_client_is_built_with_the_proxy(env, proxy_env, constructed):
    assert kite_client.build_login_url().startswith("https://kite.zerodha.com/connect/login")
    assert constructed == [{"api_key": "test_api_key", "proxies": PROXY_MAP}]


def test_14c_environment_proxies_cannot_redirect_or_bypass_the_route(real_kite, wire, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://198.51.100.7:9999")
    monkeypatch.setenv("HTTP_PROXY", "http://198.51.100.7:9999")
    monkeypatch.setenv("ALL_PROXY", "http://198.51.100.7:9999")
    monkeypatch.setenv("NO_PROXY", "api.kite.trade,*")
    kite = kite_client.get_kite()
    kite.profile()
    kite.place_order(variety="regular", exchange="NSE", tradingsymbol="TCS", transaction_type="BUY", quantity=1,
                     product="CNC", order_type="MARKET")
    _all_proxied(wire)


def test_14d_only_the_factory_constructs_kiteconnect():
    """Every Kite REST client in the backend comes from create_kite_connect."""
    offenders = []
    for path in BACKEND.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "KiteConnect":
                offenders.append((path.relative_to(ROOT).as_posix(), node.lineno))
    factory = next(n for n in ast.walk(ast.parse((BACKEND / "kite_client.py").read_text(encoding="utf-8")))
                   if isinstance(n, ast.FunctionDef) and n.name == "create_kite_connect")
    allowed = {("backend/kite_client.py", n.lineno) for n in ast.walk(factory) if isinstance(n, ast.Call)
               and getattr(n.func, "id", None) == "KiteConnect"}
    assert len(allowed) == 2
    assert set(offenders) == allowed, offenders


def test_14e_backend_never_sets_process_wide_proxy_variables():
    pattern = re.compile(r"""(environ\[\s*['"]|environ\.setdefault\(\s*['"]|putenv\(\s*['"]|setenv\(\s*['"])"""
                         r"""(?i:https?_proxy|all_proxy|no_proxy)""")
    hits = [p.relative_to(ROOT).as_posix() for p in BACKEND.rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
    assert hits == []


# ---------------------------------------------------------------- 15-16 KiteTicker stays direct

class RecordingTicker:
    instances: list = []

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs, self.connect_kwargs = args, kwargs, None
        RecordingTicker.instances.append(self)

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs

    def is_connected(self):
        return False

    def close(self):
        pass


@pytest.mark.parametrize("value", [None, PROXY_URL, "https://proxy.internal:8443"])
def test_15_ticker_construction_is_unchanged_by_the_rest_proxy(monkeypatch, value):
    from backend import ticker
    if value is not None:
        monkeypatch.setenv("KITE_REST_PROXY", value)
    RecordingTicker.instances = []
    monkeypatch.setattr(ticker, "KiteTicker", RecordingTicker)
    manager = ticker.TickerManager()
    manager.start("key", "tok", [408065])
    manager.restart_with_new_token("tok2")
    assert [(t.args, t.kwargs, t.connect_kwargs) for t in RecordingTicker.instances] == [
        (("key", "tok"), {}, {"threaded": True}), (("key", "tok2"), {}, {"threaded": True})]
    manager.stop()


def test_16_ticker_never_receives_or_references_the_rest_proxy():
    source = (BACKEND / "ticker.py").read_text(encoding="utf-8")
    assert "proxy" not in source.lower()
    assert "create_kite_connect" not in source and "kite_rest_proxy" not in source
    calls = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "KiteTicker"]
    assert len(calls) == 1 and len(calls[0].args) == 2 and calls[0].keywords == []
    connects = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)
                and getattr(n.func, "attr", None) == "connect"]
    assert all({k.arg for k in c.keywords} <= {"threaded"} for c in connects)
    other_users = [p.relative_to(ROOT).as_posix() for p in BACKEND.rglob("*.py")
                   if "KiteTicker(" in p.read_text(encoding="utf-8") and p.name != "ticker.py"]
    assert other_users == []


# ---------------------------------------------------------------- 17-19 fail closed

@pytest.mark.parametrize("exc", [requests.exceptions.ProxyError("Unable to connect to proxy"),
                                 requests.exceptions.ConnectTimeout("connect timed out"),
                                 requests.exceptions.ReadTimeout("read timed out")],
                         ids=["ProxyError", "ConnectTimeout", "ReadTimeout"])
def test_17_proxy_failure_is_raised_never_retried_directly(real_kite, wire, constructed, exc):
    wire.raise_exc = exc
    kite = kite_client.get_kite()
    with pytest.raises(type(exc)):
        kite.profile()
    assert len(wire.sends) == 1 and wire.sends[0]["proxies"] == PROXY_MAP
    assert len(constructed) == 1


def test_17b_invalid_proxy_builds_no_client_and_sends_nothing(env, wire, constructed, monkeypatch):
    _with_token(env)
    monkeypatch.setattr(kite_client, "get_kite", REAL_GET_KITE)
    monkeypatch.setenv("KITE_REST_PROXY", "10.77.0.1:3128")
    with pytest.raises(kite_rest_proxy.KiteRestProxyError):
        kite_client.get_kite()
    with pytest.raises(HTTPException) as raised:
        kite_client.require_kite()
    assert raised.value.status_code == 503 and raised.value.detail["error_code"] == "KITE_REST_PROXY_INVALID"
    r = env.client.get("/api/holdings")
    assert r.status_code == 503 and r.json()["detail"]["error_code"] == "KITE_REST_PROXY_INVALID"
    assert constructed == [] and wire.sends == []


@pytest.mark.parametrize("exc", [requests.exceptions.ProxyError("Unable to connect to proxy"),
                                 requests.exceptions.ConnectTimeout("connect timed out"),
                                 requests.exceptions.ReadTimeout("read timed out")],
                         ids=["ProxyError", "ConnectTimeout", "ReadTimeout"])
def test_18_proxy_timeout_on_place_order_reaches_unknown_handling(env, proxy_env, market_open, exc):
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    env.kite.raise_on_next("place_order", exc)
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["success"] is False and "UNKNOWN" in data["execution_message"]
    assert ticket_row(env.db, t["id"])["submission_state"] == "unknown"
    assert len(env.kite.calls_to("place_order")) == 1


def test_19_proxy_failure_after_acceptance_cannot_cause_a_duplicate_order(env, proxy_env, market_open):
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    env.kite.accept_then_raise("place_order", requests.exceptions.ReadTimeout("read timed out via proxy"))
    first = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))
    assert "UNKNOWN" in first.json()["data"]["execution_message"]
    again = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))
    assert again.status_code == 409
    assert len(env.kite.calls_to("place_order")) == 1
    assert ticket_row(env.db, t["id"])["submission_state"] == "unknown"


@pytest.mark.parametrize("exc", [requests.exceptions.ProxyError("Unable to connect to proxy"),
                                 requests.exceptions.ConnectTimeout("connect timed out")],
                         ids=["ProxyError", "ConnectTimeout"])
def test_19c_proxy_failure_during_risk_revalidation_sends_no_order(env, proxy_env, market_open, monkeypatch, exc):
    """The proxy dies before the order: the pre-order broker reads fail, so
    nothing is placed, and the failure counts toward the broker circuit
    breaker (PK-12G) like any other transport failure."""
    from backend import account_safety
    recorded = []
    monkeypatch.setattr(account_safety, "record_broker_failure",
                        lambda account_id, source, detail: recorded.append(source))
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    env.kite.raise_on_next("holdings", exc)
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))
    assert r.status_code == 503 and r.json()["detail"]["error_code"] == "RISK_REVALIDATION_UNAVAILABLE"
    assert "risk_revalidation" in recorded
    assert env.kite.calls_to("place_order") == [] and env.kite.calls_to("place_gtt") == []


def test_19b_wire_level_place_order_failure_sends_exactly_once(real_kite, wire):
    wire.raise_exc = requests.exceptions.ProxyError("Tunnel connection failed: 503")
    kite = kite_client.require_trading_account(kite_client.get_kite())
    with pytest.raises(requests.exceptions.ProxyError):
        kite.place_order(variety="regular", exchange="NSE", tradingsymbol="TCS", transaction_type="BUY", quantity=1,
                         product="CNC", order_type="MARKET")
    assert [(s["method"], s["path"], s["proxies"]) for s in wire.sends] == [("POST", "/orders/regular", PROXY_MAP)]


# ---------------------------------------------------------------- 20-23 Live Readiness

def _fail_every_read(env, monkeypatch, make_exc):
    """Every snapshot read raises. FakeKite.profile ignores raise_on_next,
    so it is replaced for the test."""
    for name in SNAPSHOT_READS:
        env.kite.raise_on_next(name, make_exc())

    def profile():
        raise make_exc()

    monkeypatch.setattr(env.kite, "profile", profile)


def _unreachable(env, monkeypatch):
    _fail_every_read(env, monkeypatch, lambda: requests.exceptions.ProxyError("Unable to connect to proxy"))


def test_20_unreachable_proxy_makes_readiness_not_ready_despite_static_ip(ready, proxy_env, monkeypatch):
    _unreachable(ready, monkeypatch)
    result = _evaluate()
    route = _check(result, "KITE_REST_ROUTE")
    assert route["status"] == "FAIL" and route["blocking"] is True
    assert "unreachable" in route["message"] and "never retried directly" in route["message"]
    assert result["overall_status"] == "NOT_READY" and "KITE_REST_ROUTE" in result["blocking_codes"]
    assert _check(result, "STATIC_IP")["status"] == "PASS"   # the user's attestation alone is not enough


def test_20b_invalid_proxy_fails_readiness_without_any_broker_call(ready, monkeypatch):
    monkeypatch.setattr(kite_client, "get_kite", REAL_GET_KITE)
    monkeypatch.setenv("KITE_REST_PROXY", CRED_URL)
    result = _evaluate()
    assert _check(result, "KITE_REST_ROUTE")["status"] == "FAIL"
    assert _check(result, "BROKER_SESSION")["status"] == "FAIL"
    assert result["overall_status"] == "NOT_READY" and result["rest_route"] == {"mode": "INVALID", "endpoint": None}
    assert ready.kite.calls == []
    assert "S3cretPw" not in json.dumps(result) and "netuser" not in json.dumps(result)


def test_20c_proxy_configured_but_not_yet_proven_needs_the_broker_check(ready, proxy_env):
    result = _evaluate(broker=False)
    route = _check(result, "KITE_REST_ROUTE")
    assert route["status"] == "USER_ACTION" and route["blocking"] is True
    assert "Run the broker check" in route["message"]


def test_20d_kite_api_error_through_the_proxy_proves_the_route_not_the_session(ready, proxy_env, monkeypatch):
    _fail_every_read(ready, monkeypatch, lambda: TokenException("Incorrect `api_key` or `access_token`."))
    result = _evaluate()
    assert _check(result, "KITE_REST_ROUTE")["status"] == "PASS"
    assert _check(result, "BROKER_SESSION")["status"] == "FAIL"
    assert result["overall_status"] == "NOT_READY"


def test_21_healthy_proxy_passes_and_readiness_can_be_ready(ready, proxy_env):
    result = _evaluate()
    route = _check(result, "KITE_REST_ROUTE")
    assert route["status"] == "PASS" and route["section"] == "network"
    assert route["message"].startswith("Kite REST via proxy 10.77.0.1:3128")
    assert route["detail"] == {"mode": "PROXY", "endpoint": "10.77.0.1:3128"}
    assert result["overall_status"] == "READY", result["blocking_codes"]


def test_21b_no_proxy_is_an_advisory_warning_and_readiness_is_unchanged(ready):
    result = _evaluate()
    route = _check(result, "KITE_REST_ROUTE")
    assert route["status"] == "WARN" and route["blocking"] is False
    assert result["overall_status"] == "READY" and result["rest_route"] == {"mode": "DIRECT", "endpoint": None}


def test_22_broker_check_reports_proxy_routing(ready, proxy_env):
    r = ready.client.get("/api/live-readiness?broker=true&refresh=true")
    data = r.json()["data"]
    assert data["broker_checked"] is True
    assert data["rest_route"] == {"mode": "PROXY", "endpoint": "10.77.0.1:3128"}
    assert {s["key"]: s["title"] for s in data["sections"]}["network"] == "Kite REST Route"
    for name in MUTATING_BROKER_CALLS:
        assert ready.kite.calls_to(name) == []


@pytest.mark.parametrize("value", [PROXY_URL, CRED_URL])
def test_23_readiness_endpoint_exposes_no_secret(ready, monkeypatch, value):
    monkeypatch.setenv("KITE_REST_PROXY", value)
    body = ready.client.get("/api/live-readiness?broker=true&refresh=true").text
    for secret in ("fake-access-token", "test_api_secret", "S3cretPw", "netuser", "Authorization"):
        assert secret not in body


class _Reply:
    def __init__(self, text):
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        return self.text.encode()[:n]


def test_23b_outbound_ip_check_goes_through_the_proxy(ready, proxy_env, monkeypatch):
    import urllib.request
    handlers = []

    class Opener:
        def open(self, request, timeout=None):
            return _Reply(TEST_STATIC_IP)

    def build_opener(*hs):
        handlers.extend(hs)
        return Opener()

    def direct(*a, **k):
        raise AssertionError("the outbound-IP check went direct while a REST proxy is configured")

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(urllib.request, "urlopen", direct)
    r = ready.client.post("/api/live-readiness/egress-check")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "VERIFIED"
    assert [h.proxies for h in handlers if isinstance(h, urllib.request.ProxyHandler)] == [PROXY_MAP]
    detail = _check(_evaluate(broker=False), "STATIC_IP")["detail"]
    assert (detail["egress_route"], detail["egress_proxy"]) == ("PROXY", "10.77.0.1:3128")


def test_23c_outbound_ip_check_without_proxy_is_unchanged(ready, monkeypatch):
    import urllib.request
    used = []
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: used.append(h))
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _Reply(TEST_STATIC_IP))
    r = ready.client.post("/api/live-readiness/egress-check")
    assert r.json()["data"]["status"] == "VERIFIED" and used == []


# ---------------------------------------------------------------- 24-25 DRY RUN / backward compatibility

def test_24_dry_run_works_with_a_proxy_and_never_mutates_the_broker(env, proxy_env, market_open):
    set_mode(env.db, "dry_run")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=displayed_mode(env.db))
    assert r.status_code == 200, r.text
    row = ticket_row(env.db, t["id"])
    assert row["mode"] == "dry_run" and str(row["kite_order_id"]).startswith("PAPER-")
    for name in MUTATING_BROKER_CALLS:
        assert env.kite.calls_to(name) == []


def test_25_direct_mode_requests_carry_no_configured_proxy(env, wire, monkeypatch):
    _with_token(env)
    monkeypatch.setattr(kite_client, "get_kite", REAL_GET_KITE)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    kite = kite_client.get_kite()
    assert kite.proxies == {} and kite.reqsession.trust_env is True
    kite.profile()
    assert wire.sends[0]["host"] == "api.kite.trade"
    assert not {"http", "https"} & set(wire.sends[0]["proxies"])
