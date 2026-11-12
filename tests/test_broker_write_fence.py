"""Phase 2 broker-write fence (design §7.3, E5/E6): only the process that owns the database may send a
broker write. The fence sits at the SDK's single request method (KiteConnect._request), so it covers every
SDK write - including those of the frozen execution.py and strategy_engine.py - without editing them; a
non-SDK broker (FakeKite, the demo double) and the paper broker get the same check around their write
methods. A refusal is InstanceNotOwner, a PermissionException subclass: a definite rejection, nothing sent.

No network: the SDK's HTTP session is replaced by a recorder (the test_broker_writes_switch pattern)."""

import pytest
import requests
from kiteconnect import KiteConnect
from kiteconnect.exceptions import PermissionException

from backend import instance_lock, kite_client
from test_broker_writes_switch import WRITES, _Response


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def _request(self, method, url, **kwargs):
        calls.append((method.upper(), url, instance_lock.status()["broker_writes_in_flight"]))
        return _Response()
    monkeypatch.setattr(requests.Session, "request", _request)
    return calls


@pytest.fixture
def no_owner():
    instance_lock.reset_for_tests()
    yield
    instance_lock.reset_for_tests()


def _client(monkeypatch, proxy=True, writes="enabled"):
    if proxy:
        monkeypatch.setenv("KITE_REST_PROXY", "http://10.77.0.1:3128")
    else:
        monkeypatch.delenv("KITE_REST_PROXY", raising=False)
    monkeypatch.setenv(kite_client.BROKER_WRITES_ENV, writes)
    kite = kite_client.create_kite_connect("test_api_key")
    kite.set_access_token("test_access_token")
    return kite


@pytest.mark.parametrize("method", sorted(WRITES))
def test_E5_every_sdk_write_is_refused_without_authority_before_any_request(monkeypatch, sent, no_owner, method):
    kite = _client(monkeypatch)
    with pytest.raises(instance_lock.InstanceNotOwner) as refused:
        getattr(kite, method)(**WRITES[method])
    assert sent == []
    assert isinstance(refused.value, PermissionException)          # execution.DEFINITE_REJECTIONS: nothing sent
    assert refused.value.reason == "INSTANCE_NOT_OWNER" and "never sent to Kite" in str(refused.value)


def test_E5_reads_pass_the_fence_without_authority(monkeypatch, sent, no_owner):
    kite = _client(monkeypatch)
    kite.profile()
    kite.orders()
    kite.get_gtts()
    kite.holdings()
    assert [m for m, _, _ in sent] == ["GET"] * 4


def test_E5_the_login_token_exchange_is_refused_for_a_non_owner(monkeypatch, sent, no_owner):
    kite = _client(monkeypatch)
    with pytest.raises(instance_lock.InstanceNotOwner):
        kite.generate_session("request_token", api_secret="secret")   # POST api.token
    assert sent == []


@pytest.mark.parametrize("method", sorted(WRITES))
def test_E5_with_authority_writes_go_out_once_and_are_counted_in_flight(monkeypatch, sent, owner_process, method):
    kite = _client(monkeypatch)
    try:
        getattr(kite, method)(**WRITES[method])
    except Exception:
        pass  # the recorder's canned body may not parse as this method's result; the request is what counts
    assert len(sent) == 1 and sent[0][0] != "GET"
    assert sent[0][2] == 1                                           # counted while inside the SDK
    assert instance_lock.status()["broker_writes_in_flight"] == 0     # and released after


def test_E5_the_fence_composes_with_the_existing_narrowing_and_keeps_the_class(monkeypatch, sent, owner_process):
    direct = _client(monkeypatch, proxy=False)
    assert type(direct) is kite_client.DirectRouteKiteConnect          # the class is unchanged by the fence
    with pytest.raises(kite_client.DirectRouteWritesRefused):
        direct.place_order(**WRITES["place_order"])
    read_only = _client(monkeypatch, writes="disabled")
    assert type(read_only) is kite_client.ReadOnlyKiteConnect
    with pytest.raises(kite_client.BrokerWritesDisabled):
        read_only.place_gtt(**WRITES["place_gtt"])
    proxied = _client(monkeypatch)
    assert type(proxied) is KiteConnect
    assert sent == []


def test_E5_the_fence_is_ahead_of_the_narrowing(monkeypatch, sent, no_owner):
    """Without authority the ownership refusal comes first, whatever the route."""
    with pytest.raises(instance_lock.InstanceNotOwner):
        _client(monkeypatch, proxy=False).place_order(**WRITES["place_order"])
    with pytest.raises(instance_lock.InstanceNotOwner):
        _client(monkeypatch, writes="disabled").place_order(**WRITES["place_order"])


def test_E5_a_lost_or_shutting_down_owner_is_refused(monkeypatch, sent, owner_process):
    kite = _client(monkeypatch)
    monkeypatch.setattr(instance_lock._state, "shutting_down", True)
    with pytest.raises(instance_lock.InstanceNotOwner) as refused:
        kite.cancel_order(**WRITES["cancel_order"])
    assert refused.value.reason == "INSTANCE_NOT_OWNER"
    monkeypatch.setattr(instance_lock._state, "shutting_down", False)
    monkeypatch.setattr(instance_lock._state, "role", instance_lock.LOST)
    monkeypatch.setattr(instance_lock._state, "reason", instance_lock.INSTANCE_LOCK_LOST)
    with pytest.raises(instance_lock.InstanceNotOwner) as refused:
        kite.cancel_order(**WRITES["cancel_order"])
    assert refused.value.reason == "INSTANCE_LOCK_LOST" and sent == []


def test_E5_frozen_call_paths_go_through_the_fence(monkeypatch, sent, no_owner):
    """execution.py (place_order / place_gtt) and strategy_engine.py (modify_gtt) call SDK methods on the
    client the factory built; every one funnels into the fenced _request."""
    from backend import execution, strategy_engine
    import inspect
    assert "place_order(" in inspect.getsource(execution) and "place_gtt(" in inspect.getsource(execution)
    assert "modify_gtt(" in inspect.getsource(strategy_engine)
    kite = _client(monkeypatch)
    for method in ("place_order", "place_gtt", "modify_gtt", "delete_gtt", "cancel_order"):
        with pytest.raises(instance_lock.InstanceNotOwner):
            getattr(kite, method)(**WRITES[method])
    assert sent == []


def test_E5_fence_broker_is_idempotent_and_marks_the_instance(monkeypatch, sent, no_owner):
    kite = _client(monkeypatch)
    first = kite._request
    assert kite_client.fence_broker(kite) is kite and kite._request is first


def test_E6_a_fenced_fake_kite_refuses_every_write_without_authority(no_owner):
    from fake_kite import FakeKite
    fake = kite_client.fence_broker(FakeKite())
    for name in ("place_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt"):
        with pytest.raises(instance_lock.InstanceNotOwner):
            getattr(fake, name)()
    fake.holdings()
    fake.get_gtts()
    assert [n for n, _ in fake.calls] == ["holdings", "get_gtts"]


def test_E6_the_env_fake_kite_is_fenced_and_works_for_the_owner(env):
    assert vars(env.kite).get("_kt_write_fence") is True
    env.kite.place_gtt(trigger_type="single", tradingsymbol="TCS", exchange="NSE", trigger_values=[1.0],
                       last_price=2.0, orders=[{"transaction_type": "SELL", "quantity": 1, "price": 1.0,
                                                "order_type": "LIMIT", "product": "CNC"}])
    assert len(env.kite.calls_to("place_gtt")) == 1


def test_E6_the_paper_broker_refuses_writes_without_authority(env):
    from backend import paper_broker
    conn = env.db.get_connection()
    try:
        broker = paper_broker.PaperBroker(conn, 1)
        instance_lock.reset_for_tests()
        for name, kwargs in (("place_order", dict(variety="regular", exchange="NSE", tradingsymbol="TCS",
                                                   transaction_type="BUY", quantity=1, product="CNC",
                                                   order_type="LIMIT", price=1.0)),
                             ("cancel_order", dict(variety="regular", order_id="PAPER-1")),
                             ("place_gtt", dict(trigger_type="single", tradingsymbol="TCS", exchange="NSE",
                                                trigger_values=[1.0], last_price=2.0, orders=[])),
                             ("modify_gtt", dict(trigger_id="PAPER-G-1", trigger_type="single", tradingsymbol="TCS",
                                                 exchange="NSE", trigger_values=[1.0], last_price=2.0, orders=[])),
                             ("delete_gtt", dict(trigger_id="PAPER-G-1"))):
            with pytest.raises(instance_lock.InstanceNotOwner):
                getattr(broker, name)(**kwargs)
        assert conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM paper_gtts").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_refused_write_is_a_definite_rejection_for_the_order_paths():
    from backend import execution
    assert issubclass(instance_lock.InstanceNotOwner, execution.DEFINITE_REJECTIONS)
