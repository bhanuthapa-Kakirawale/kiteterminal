"""KITE_TERMINAL_BROKER_WRITES: the read-only switch for the user-operated real-login verification
(docs/DMB_KITE_OPERATIONAL_RUNBOOK.md section 1.6). With writes disabled, a REST client can log in
and read; every other broker write is refused in this process before any HTTP request is made, and
the order paths see it as a definite rejection (nothing sent), never an UNKNOWN outcome.
No network: the SDK's HTTP session is replaced by a recorder."""
import pytest
from kiteconnect import KiteConnect

from backend import kite_client

WRITES = {
    "place_order": dict(variety="regular", exchange="NSE", tradingsymbol="INFY", transaction_type="BUY",
                        quantity=1, product="CNC", order_type="LIMIT", price=1.0),
    "modify_order": dict(variety="regular", order_id="1", price=2.0),
    "cancel_order": dict(variety="regular", order_id="1"),
    "exit_order": dict(variety="regular", order_id="1"),
    "place_gtt": dict(trigger_type="single", tradingsymbol="INFY", exchange="NSE", trigger_values=[1.0],
                      last_price=2.0, orders=[{"transaction_type": "SELL", "quantity": 1, "price": 1.0,
                                               "order_type": "LIMIT", "product": "CNC"}]),
    "modify_gtt": dict(trigger_id=1, trigger_type="single", tradingsymbol="INFY", exchange="NSE",
                       trigger_values=[1.0], last_price=2.0, orders=[{"transaction_type": "SELL", "quantity": 1,
                                                                      "price": 1.0, "order_type": "LIMIT",
                                                                      "product": "CNC"}]),
    "delete_gtt": dict(trigger_id=1),
    "convert_position": dict(exchange="NSE", tradingsymbol="INFY", transaction_type="BUY", position_type="day",
                             quantity=1, old_product="MIS", new_product="CNC"),
    "invalidate_access_token": dict(),
    "order_margins": dict(params=[]),
}


class _Response:
    status_code = 200
    headers = {"content-type": "application/json"}
    content = b'{"status": "success", "data": {"user_id": "AB1234", "access_token": "x", "public_token": "y", ' \
              b'"refresh_token": "", "user_name": "U", "login_time": "2026-11-04 10:00:00"}}'

    def json(self):
        import json
        return json.loads(self.content)


@pytest.fixture
def sent(monkeypatch):
    """Every HTTP request any client would make, instead of making it."""
    calls = []

    def _request(self, method, url, **kwargs):
        calls.append((method.upper(), url))
        return _Response()
    import requests
    monkeypatch.setattr(requests.Session, "request", _request)
    monkeypatch.delenv("KITE_REST_PROXY", raising=False)
    return calls


def _client(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(kite_client.BROKER_WRITES_ENV, raising=False)
    else:
        monkeypatch.setenv(kite_client.BROKER_WRITES_ENV, value)
    kite = kite_client.create_kite_connect("test_api_key")
    kite.set_access_token("test_access_token")
    return kite


@pytest.mark.parametrize("value", [None, "", "enabled", " Enabled "])
def test_writes_are_enabled_by_default(monkeypatch, sent, value):
    kite = _client(monkeypatch, value)
    assert type(kite) is KiteConnect and kite_client.broker_writes_enabled()


@pytest.mark.parametrize("value", ["disabled", "off", "DISABLED", "disabeld", "0", "no"])
def test_any_other_value_disables_writes(monkeypatch, sent, value):
    kite = _client(monkeypatch, value)
    assert isinstance(kite, kite_client.ReadOnlyKiteConnect) and not kite_client.broker_writes_enabled()


@pytest.mark.parametrize("method", sorted(WRITES))
def test_every_broker_write_is_refused_before_any_request(monkeypatch, sent, method):
    kite = _client(monkeypatch, "disabled")
    with pytest.raises(kite_client.BrokerWritesDisabled) as refused:
        getattr(kite, method)(**WRITES[method])
    assert sent == [] and "never sent to Kite" in str(refused.value)


def test_reads_and_the_login_exchange_still_work(monkeypatch, sent):
    kite = _client(monkeypatch, "disabled")
    kite.profile()
    kite.orders()
    kite.get_gtts()
    kite.generate_session("request_token", api_secret="secret")         # the login: POST api.token
    assert [m for m, _ in sent] == ["GET", "GET", "GET", "POST"]
    assert sent[-1][1].endswith("/session/token")


def test_a_refused_write_is_a_definite_rejection_for_the_order_paths():
    """execution.py (frozen) treats DEFINITE_REJECTIONS as "Kite refused, nothing filled" and every
    other exception as UNKNOWN. A locally refused write was never sent, so it must be definite."""
    from backend import execution
    assert issubclass(kite_client.BrokerWritesDisabled, execution.DEFINITE_REJECTIONS)


def test_the_security_summary_names_the_switch(monkeypatch):
    from backend import server
    config = server.RuntimeConfig(host="127.0.0.1", port=8000, reload=False, workers=1, allow_remote=False,
                                  trusted_proxy_ips=(), source="launcher")
    monkeypatch.setenv(kite_client.BROKER_WRITES_ENV, "disabled")
    assert server.security_summary(config, {"127.0.0.1"}).endswith("broker_writes=DISABLED")
    monkeypatch.delenv(kite_client.BROKER_WRITES_ENV)
    assert server.security_summary(config, {"127.0.0.1"}).endswith("broker_writes=enabled")


def test_the_switch_keeps_the_net1_proxy_route(monkeypatch, sent):
    """With KITE_REST_PROXY set the read-only client is still the factory's proxied client: reads
    go through the proxy, writes are refused before any request."""
    monkeypatch.setenv("KITE_REST_PROXY", "http://10.77.0.1:3128")
    kite = _client(monkeypatch, "disabled")
    assert isinstance(kite, kite_client.ReadOnlyKiteConnect)
    assert kite.proxies == {"http": "http://10.77.0.1:3128", "https": "http://10.77.0.1:3128"}
    assert kite.reqsession.trust_env is False
    with pytest.raises(kite_client.BrokerWritesDisabled):
        kite.place_gtt(**WRITES["place_gtt"])
    assert sent == []
