"""Phase 0 FakeKite scenarios (docs/PROPOSAL_GTT_AFTER_FILL_LIFECYCLE_REVIEW.md §9.2).

Each one is a CONFIGURABLE mock of broker behaviour, for testing how the terminal copes with it.
None of them is verified Zerodha behaviour; the scenarios marked UNVERIFIED in tests/fake_kite.py
model the open questions of the review's §10 under every assumed outcome. All are off by default.
"""

from datetime import datetime, timedelta

import pytest
import requests
from kiteconnect.exceptions import InputException, TokenException

from fake_kite import IST, FakeKite, SimulatedCrash

LEGS = [{"transaction_type": "SELL", "quantity": 10, "order_type": "LIMIT", "product": "CNC", "price": 95.0}]


def _gtt(fake, symbol="TCS", qty=10):
    legs = [dict(LEGS[0], quantity=qty)]
    return fake.place_gtt("single", symbol, "NSE", [96.0], 100.0, legs)["trigger_id"]


def test_defaults_leave_existing_behaviour_unchanged():
    fake = FakeKite()
    assert fake.fail_all is None and fake.gtt_list_lag_calls == 0 and fake.gtt_timestamps is False
    assert fake.order_history_previous_days == "ok"
    gid = _gtt(fake)
    listing = fake.get_gtts()
    assert [g["id"] for g in listing] == [gid]
    assert "created_at" not in listing[0] and "updated_at" not in listing[0]
    old = fake.add_broker_order("TCS", "BUY", 5, order_timestamp="2020-01-02 10:00:00")
    assert fake.order_history(old)[0]["order_id"] == old
    assert [o["order_id"] for o in fake.orders()] == [old]
    order_id = fake.trigger_gtt(gid)
    assert fake.order_book[order_id]["status_message"] is None


# ------------------------------------------------------------------ crash points
def test_simulated_crash_is_not_absorbed_by_except_exception():
    fake = FakeKite()
    fake.raise_on_next("place_gtt", SimulatedCrash("before send"))
    with pytest.raises(SimulatedCrash):
        try:
            _gtt(fake)
        except Exception:  # noqa: BLE001 - the point: a crash is not an Exception
            pytest.fail("SimulatedCrash must pass through `except Exception`")
    assert fake.gtt_book == {}


def test_simulated_crash_after_the_broker_took_effect():
    fake = FakeKite()
    fake.accept_then_raise("place_gtt", SimulatedCrash("after send"))
    with pytest.raises(SimulatedCrash):
        _gtt(fake)
    assert len(fake.gtt_book) == 1                     # a live GTT the crashed process never learned about


# ------------------------------------------------------------------ eventual consistency (UNVERIFIED)
def test_a_new_gtt_is_missing_from_the_next_n_listings():
    fake = FakeKite()
    fake.gtt_list_lag_calls = 2
    gid = _gtt(fake)
    assert fake.get_gtts() == []
    assert fake.get_gtts() == []
    assert [g["id"] for g in fake.get_gtts()] == [gid]
    fake.gtt_list_lag_calls = 0
    later = _gtt(fake, "INFY")
    assert {g["id"] for g in fake.get_gtts()} == {gid, later}


# ------------------------------------------------------------------ GTT timestamps (UNVERIFIED shape)
def test_gtt_timestamps_when_enabled():
    fake = FakeKite()
    fake.gtt_timestamps = True
    gid = _gtt(fake)
    g = fake.gtt_book[gid]
    assert g["created_at"] == g["updated_at"]
    g["updated_at"] = "2000-01-01 00:00:00"
    fake.modify_gtt(gid, "single", "TCS", "NSE", [97.0], 100.0, LEGS)
    assert g["updated_at"] != "2000-01-01 00:00:00"
    g["updated_at"] = "2000-01-01 00:00:00"
    fake.delete_gtt(gid)
    assert g["updated_at"] != "2000-01-01 00:00:00"


# ------------------------------------------------------------------ stale / duplicate orders() snapshots
def test_an_older_orders_snapshot_is_served_once():
    fake = FakeKite()
    fake.auto_fill = False
    oid = fake.place_order("regular", "NSE", "TCS", "BUY", 100, "CNC", "LIMIT", price=100.0)
    fake.set_order_state(oid, "OPEN", filled_quantity=40)
    stale = fake.snapshot_orders()
    fake.set_order_state(oid, "OPEN", filled_quantity=70)
    fake.serve_orders_snapshot(stale)
    fake.serve_orders_snapshot(stale)                   # a duplicate
    assert fake.orders()[0]["filled_quantity"] == 40
    assert fake.orders()[0]["filled_quantity"] == 40
    assert fake.orders()[0]["filled_quantity"] == 70    # back to the live book
    stale[0]["filled_quantity"] = 1                     # the queued copy is independent
    assert fake.order_book[oid]["filled_quantity"] == 70


# ------------------------------------------------------------------ session expiry / outage
def test_session_expiry_fails_every_recorded_call_until_cleared():
    fake = FakeKite()
    fake.expire_session()
    for call in (fake.orders, fake.get_gtts, fake.holdings, fake.positions, lambda: fake.margins("equity"),
                 lambda: _gtt(fake), lambda: fake.ltp(["NSE:TCS"])):
        with pytest.raises(TokenException):
            call()
    assert fake.gtt_book == {}                          # nothing took effect
    assert fake.profile()["user_id"] == "DT0001"         # documented: unrecorded calls are unaffected
    fake.clear_fail_every_call()
    assert fake.orders() == []


def test_proxy_outage_as_a_connection_error():
    fake = FakeKite()
    fake.fail_every_call(requests.ConnectionError("proxy unreachable (simulated)"))
    with pytest.raises(requests.ConnectionError):
        fake.get_gtts()
    with pytest.raises(requests.ConnectionError):
        fake.delete_gtt(1)


# ------------------------------------------------------------------ earlier-day order_history (UNVERIFIED)
def test_order_history_for_earlier_day_orders_can_be_made_to_fail():
    fake = FakeKite()
    fake.order_history_previous_days = "raise"
    yesterday = (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d 10:00:00")
    old = fake.add_broker_order("TCS", "BUY", 5, order_timestamp=yesterday)
    today = fake.add_broker_order("TCS", "BUY", 5)
    with pytest.raises(InputException):
        fake.order_history(old)
    assert fake.order_history(today)[0]["order_id"] == today
    assert old in {o["order_id"] for o in fake.orders()}


# ------------------------------------------------------------------ trigger vs sellable quantity (UNVERIFIED)
def test_trigger_status_message():
    fake = FakeKite()
    gid = _gtt(fake)
    oid = fake.trigger_gtt(gid, status="REJECTED", filled_quantity=0, status_message="RMS: simulated")
    assert fake.order_book[oid]["status"] == "REJECTED"
    assert fake.order_book[oid]["status_message"] == "RMS: simulated"


@pytest.mark.parametrize("policy, status, filled", [
    ("reject", "REJECTED", 0), ("partial", "CANCELLED", 6), ("oversell", "COMPLETE", 10),
])
def test_a_trigger_beyond_the_holding_follows_the_chosen_policy(policy, status, filled):
    fake = FakeKite()
    fake.set_holding("TCS", 4, t1_quantity=2)          # 6 sellable against a 10-share leg
    gid = _gtt(fake, qty=10)
    oid = fake.trigger_gtt_against_holdings(gid, policy=policy)
    order = fake.order_book[oid]
    assert (order["status"], order["filled_quantity"]) == (status, filled)
    assert fake.gtt_book[gid]["status"] == "triggered"
    assert fake._holdings[0]["quantity"] == 4           # holdings are left to the test


def test_a_trigger_within_the_holding_fills_completely():
    fake = FakeKite()
    fake.set_holding("TCS", 10)
    gid = _gtt(fake, qty=10)
    oid = fake.trigger_gtt_against_holdings(gid, policy="reject")
    assert (fake.order_book[oid]["status"], fake.order_book[oid]["filled_quantity"]) == ("COMPLETE", 10)


def test_an_unknown_trigger_policy_is_refused():
    fake = FakeKite()
    gid = _gtt(fake)
    with pytest.raises(ValueError):
        fake.trigger_gtt_against_holdings(gid, policy="guess")
    assert fake.gtt_book[gid]["status"] == "active"
