"""The GCP Kite REST proxy is mandatory for LIVE (final order-path remediation, finding 1).

- Missing proxy configuration blocks LIVE arming (Live Readiness KITE_REST_ROUTE is a blocker).
- Without the proxy the REST client refuses EVERY broker write locally (DirectRouteKiteConnect):
  orders, GTTs, cancels, order-margin calculations, from any caller, Emergency Exit included. Reads,
  login and dry run keep working on the direct route.
- A malformed proxy refuses every broker route (unchanged NET-1 behaviour, re-asserted).
- An unavailable proxy blocks live broker writes: an entry is refused at the final gate's proxied
  LTP read; a write that fails in transport is never retried, and never through another route.
- The configured proxy must be proven to be the intended endpoint before LIVE can be armed: a recent
  outbound-IP observation taken THROUGH the configured proxy, by this process, equal to the static
  IP whitelisted in Kite (live_preflight.egress_proof).

The KiteTicker WebSocket stays direct by design (it carries no orders); see test_kite_rest_proxy
test_15/16. Wire recorder and mock network transports only: nothing leaves this machine.
"""

import pytest
import requests
from kiteconnect.exceptions import PermissionException

from backend import account_safety, kite_client, kite_rest_proxy, live_preflight, risk_engine
from conftest import TEST_STATIC_IP, account_id, approval_body, make_live_ready, set_mode, ticket_row
from test_baseline_api import _insert_ticket, _manual_ticket
from test_kite_rest_proxy import (  # noqa: F401 - fixtures
    PROXY_MAP, PROXY_URL, REAL_GET_KITE, _MockConnectProxy, _with_token, no_direct_sockets, proxy_env, real_kite,
    wire)
from test_live_preflight import _check, _evaluate, ready  # noqa: F401 - fixture

WRITE_CALLS = [
    ("place_order", dict(variety="regular", exchange="NSE", tradingsymbol="TCS", transaction_type="BUY",
                         quantity=1, product="CNC", order_type="MARKET")),
    ("cancel_order", dict(variety="regular", order_id="250000000001")),
    ("place_gtt", dict(trigger_type="single", tradingsymbol="TCS", exchange="NSE", trigger_values=[2900.0],
                       last_price=3000.0, orders=[{"transaction_type": "SELL", "quantity": 1, "order_type": "LIMIT",
                                                   "product": "CNC", "price": 2890.0}])),
    ("modify_gtt", dict(trigger_id=9001, trigger_type="single", tradingsymbol="TCS", exchange="NSE",
                        trigger_values=[2900.0], last_price=3000.0,
                        orders=[{"transaction_type": "SELL", "quantity": 1, "order_type": "LIMIT", "product": "CNC",
                                 "price": 2890.0}])),
    ("delete_gtt", dict(trigger_id=9001)),
    ("order_margins", dict(params=[{"exchange": "NSE", "tradingsymbol": "TCS", "transaction_type": "BUY",
                                    "variety": "regular", "product": "CNC", "order_type": "MARKET", "quantity": 1}])),
]


@pytest.fixture
def direct(env, wire, monkeypatch):
    """The real factory on the test database, with NO proxy configured."""
    _with_token(env)
    monkeypatch.setattr(kite_client, "get_kite", REAL_GET_KITE)
    monkeypatch.delenv("KITE_REST_PROXY", raising=False)
    return env


# ---------------------------------------------------------------- 1. missing configuration

@pytest.mark.parametrize("name,kwargs", WRITE_CALLS, ids=[c[0] for c in WRITE_CALLS])
def test_01_without_a_proxy_every_broker_write_is_refused_locally(direct, wire, name, kwargs):
    kite = kite_client.get_kite()
    assert isinstance(kite, kite_client.DirectRouteKiteConnect)
    with pytest.raises(kite_client.DirectRouteWritesRefused) as refused:
        getattr(kite, name)(**kwargs)
    assert isinstance(refused.value, PermissionException)  # a definite refusal, never UNKNOWN
    assert "never sent to Kite" in str(refused.value)
    assert wire.sends == []


def test_02_without_a_proxy_reads_still_work_directly(direct, wire):
    kite = kite_client.get_kite()
    kite.holdings()
    assert [(s["method"], s["host"], s["proxies"]) for s in wire.sends] == [("GET", "api.kite.trade", {})]


def test_03_the_writes_disabled_switch_still_wins_on_the_direct_route(direct, monkeypatch):
    monkeypatch.setenv(kite_client.BROKER_WRITES_ENV, "disabled")
    kite = kite_client.get_kite()
    assert type(kite) is kite_client.ReadOnlyKiteConnect
    with pytest.raises(kite_client.BrokerWritesDisabled):
        kite.place_order(**WRITE_CALLS[0][1])


def test_04_live_confirm_without_a_proxy_is_refused_and_nothing_is_sent(env, market_open, monkeypatch):
    monkeypatch.delenv("KITE_REST_PROXY", raising=False)
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket(entry_order_type="MARKET")).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"]))
    assert r.status_code == 409, r.text
    codes = {f["code"] for f in r.json()["detail"]["failures"]}
    assert "KITE_REST_PROXY_REQUIRED" in codes
    assert env.kite.calls_to("place_order") == [] and env.kite.calls_to("place_gtt") == []
    assert ticket_row(env.db, t["id"])["status"] == "pending_confirmation"


def test_05_live_readiness_blocks_and_arming_is_refused_without_a_proxy(env, monkeypatch):
    make_live_ready(env)
    monkeypatch.delenv("KITE_REST_PROXY", raising=False)
    data = env.client.get("/api/live-readiness", params={"broker": "true", "refresh": "true"}).json()["data"]
    route = next(c for c in data["checks"] if c["code"] == "KITE_REST_ROUTE")
    assert route["status"] == "FAIL" and route["blocking"] is True
    assert "REST proxy required for LIVE" in route["message"]
    r = env.client.post("/api/execution/mode", json={"mode": "live", "confirm_real_money": True})
    assert r.status_code == 409 and "KITE_REST_ROUTE" in r.json()["detail"]["blocking_codes"], r.text


def test_06_emergency_exit_without_a_proxy_sends_nothing_and_says_where_to_exit(direct, wire, market_open):
    """No direct fallback, Emergency Exit included: the SELL is refused locally (a definite
    failure, nothing sent) and the message points the user to Kite itself."""
    pos = _insert_ticket(direct.db, entry_fill_status="filled", filled_quantity=5, kite_order_id="250000000901",
                         kite_gtt_id=None, use_gtt=0)
    # E3: the (real-client) broker holds what the app's position says.
    _reply = wire.reply_for
    wire.reply_for = lambda method, path: ([{"tradingsymbol": "TCS", "exchange": "NSE", "instrument_token": 2953217, "quantity": 5, "t1_quantity": 0}] if path == "/portfolio/holdings" else _reply(method, path))
    r = direct.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    assert r.status_code == 200, r.text
    [result] = r.json()["data"]["results"]
    assert result["status"] == "failed", result
    assert "never sent to Kite" in result["message"] and "Kite web or the Kite app" in result["message"]
    assert all(s["method"] == "GET" for s in wire.sends)
    assert ticket_row(direct.db, pos)["closed_at"] is None


def test_07_dry_run_is_unaffected_without_a_proxy(env, market_open, monkeypatch):
    monkeypatch.delenv("KITE_REST_PROXY", raising=False)
    set_mode(env.db, "dry_run")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"]))
    assert r.status_code == 200 and r.json()["data"]["kite_order_id"].startswith("PAPER-"), r.text


# ---------------------------------------------------------------- 2. malformed configuration

@pytest.mark.parametrize("value", ["10.77.0.1:3128", "socks5://10.77.0.1:1080", "http://user:pw@10.77.0.1:3128",
                                   "http://10.77.0.1", "http://10.77.0.1:3128/path", "http://10.77.0.1:99999"])
def test_08_a_malformed_proxy_refuses_every_route_and_blocks_live(env, monkeypatch, value):
    monkeypatch.setenv("KITE_REST_PROXY", value)
    with pytest.raises(kite_rest_proxy.KiteRestProxyError):
        kite_client.create_kite_connect("test_api_key")
    failure = account_safety.check_rest_route()
    assert failure is not None and failure.code == "KITE_REST_PROXY_REQUIRED"
    assert value not in failure.message


# ---------------------------------------------------------------- 3. unavailable proxy

def test_09_unreachable_proxy_refuses_a_live_entry_before_any_write(real_kite, wire, market_open):
    """Every read before the order (risk revalidation, the final gate's LTP, holdings, margins)
    goes through the proxy; with the tunnel down the first of them fails and the entry is refused
    (503) — no write is attempted, through the proxy or around it, and nothing is executed."""
    from test_order_path_assurance import _kite_replies
    env = real_kite
    wire.reply_for = _kite_replies
    set_mode(env.db, "live")
    r = env.client.post("/api/tickets", json=_manual_ticket(entry_order_type="MARKET"))
    assert r.status_code == 201, r.text
    t = r.json()["data"]
    wire.sends.clear()
    wire.raise_exc = requests.exceptions.ProxyError("Unable to connect to proxy")
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"]))
    assert r.status_code == 503, r.text
    assert wire.sends and all(s["method"] == "GET" and s["proxies"] == PROXY_MAP for s in wire.sends), wire.sends
    row = ticket_row(env.db, t["id"])
    assert row["status"] in ("pending_confirmation", "rejected") and row["kite_order_id"] is None, row


def test_10_a_write_through_a_refusing_proxy_is_attempted_once_and_never_directly(monkeypatch, no_direct_sockets,
                                                                                     owner_process):
    """A real local proxy that refuses the CONNECT tunnel: exactly one attempt reaches it, no
    socket ever goes to Kite directly, and the failure propagates (the order path records it as
    UNKNOWN, never resubmitted: test_kite_rest_proxy test_17/test_19b)."""
    proxy = _MockConnectProxy()
    try:
        monkeypatch.setenv("KITE_REST_PROXY", f"http://127.0.0.1:{proxy.port}")
        monkeypatch.delenv(kite_client.BROKER_WRITES_ENV, raising=False)
        kite = kite_client.create_kite_connect("test_api_key")
        kite.set_access_token("tok")
        with pytest.raises(requests.exceptions.RequestException):
            kite.place_order(**WRITE_CALLS[0][1])
        assert len(proxy.lines) == 1 and proxy.lines[0].startswith("CONNECT api.kite.trade:443"), proxy.lines
        assert no_direct_sockets == []
    finally:
        proxy.close()


# ---------------------------------------------------------------- 4. the intended endpoint

def _observe(env, monkeypatch, ip=TEST_STATIC_IP):
    monkeypatch.setattr(live_preflight, "_fetch_egress_ip", lambda: ip)
    return env.client.post("/api/live-readiness/egress-check").json()["data"]


def test_11_a_proxied_observation_matching_the_static_ip_proves_the_endpoint(env, proxy_env, monkeypatch):
    assert _observe(env, monkeypatch)["status"] == "VERIFIED"
    conn = env.db.get_connection()
    try:
        proof = live_preflight.egress_proof(conn, account_id(env.db))
    finally:
        conn.close()
    assert proof["proven"] is True and proof["endpoint"] == "10.77.0.1:3128"


def test_12_an_observation_through_another_proxy_does_not_prove_the_current_one(env, proxy_env, monkeypatch):
    assert _observe(env, monkeypatch)["status"] == "VERIFIED"
    monkeypatch.setenv("KITE_REST_PROXY", "http://10.77.0.9:3128")  # reconfigured since the observation
    conn = env.db.get_connection()
    try:
        proof = live_preflight.egress_proof(conn, account_id(env.db))
        settings = risk_engine.get_settings(conn, account_id(env.db))
        failure = account_safety.check_live_prerequisites(conn, account_id(env.db), settings,
                                                          account_safety.now_ist())
    finally:
        conn.close()
    assert proof["proven"] is False and "not taken through the configured proxy 10.77.0.9:3128" in proof["message"]
    assert failure is not None and failure.code == "LIVE_PREREQUISITE_MISSING"


def test_13_a_restart_forgets_the_route_of_the_stored_observation(env, proxy_env, monkeypatch):
    assert _observe(env, monkeypatch)["status"] == "VERIFIED"
    live_preflight.reset_egress_routes()  # what a new process starts with
    check = _check(_evaluate(), "STATIC_IP")
    assert check["status"] == "USER_ACTION" and check["detail"]["egress_proven_via_proxy"] is False


def test_14_a_proxied_observation_of_another_ip_fails(env, proxy_env, monkeypatch):
    assert _observe(env, monkeypatch, ip="198.51.100.7")["status"] == "FAILED"
    assert _check(_evaluate(), "STATIC_IP")["status"] == "FAIL"


def test_15_a_failed_observation_clears_the_proof(env, proxy_env, monkeypatch):
    assert _observe(env, monkeypatch)["status"] == "VERIFIED"

    def down():
        raise requests.exceptions.ProxyError("tunnel down")
    monkeypatch.setattr(live_preflight, "_fetch_egress_ip", down)
    env.client.post("/api/live-readiness/egress-check")
    assert account_id(env.db) not in live_preflight._egress_routes


def test_16_a_valid_proven_proxy_lets_readiness_pass_the_route_checks(ready, proxy_env):
    result = _evaluate()
    assert _check(result, "KITE_REST_ROUTE")["status"] == "PASS"
    assert _check(result, "STATIC_IP")["status"] == "PASS"


def test_17_only_a_real_sdk_client_is_narrowed(monkeypatch):
    """The demo and some tests replace kite_client.KiteConnect with a stand-in that is not an SDK
    client. Narrowing it to DirectRouteKiteConnect would give it the SDK's request path (found by
    the P1-D browser runner, 2026-11-05). A real client is always narrowed, even while the module
    name points at a stand-in."""
    from kiteconnect import KiteConnect as SDK
    monkeypatch.delenv("KITE_REST_PROXY", raising=False)

    class StandIn:
        def __init__(self, api_key, **kwargs):
            self.api_key = api_key

    monkeypatch.setattr(kite_client, "KiteConnect", StandIn)
    assert type(kite_client.create_kite_connect("k")) is StandIn
    real = SDK(api_key="k")
    assert type(kite_client._no_writes_on_direct_route(real)) is kite_client.DirectRouteKiteConnect
