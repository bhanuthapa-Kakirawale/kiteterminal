"""Phase 3 S3 FakeKite models (docs/PHASE_3_S3_IMPLEMENTATION_REPORT.md §4).

Every model is OPT-IN and off by default, and each one is a CONFIGURABLE mock of broker behaviour that is
not known. None of them is verified Zerodha behaviour, and a passing test here proves nothing about Kite.
The B-nn labels refer to the broker-assumption register (docs/PHASE_3_DESIGN_DECISIONS.md §5). The same
models are reachable across processes through the shared FakeKite server (tests/shared_fake_kite.py).
"""

from datetime import datetime, timedelta

import pytest
from kiteconnect.exceptions import InputException

from fake_kite import IST, FakeKite

LEG = {"transaction_type": "SELL", "quantity": 10, "order_type": "LIMIT", "product": "CNC", "price": 95.0}


def _gtt(fake, symbol="TCS", qty=10, legs=None):
    legs = legs or [dict(LEG, quantity=qty)]
    return fake.place_gtt("single" if len(legs) == 1 else "two-leg", symbol, "NSE", [96.0] * len(legs), 100.0,
                          legs)["trigger_id"]


def test_defaults_leave_every_s3_model_off_and_existing_behaviour_unchanged():
    fake = FakeKite()
    assert fake.day_offset == 0 and fake.repeat_delete_semantics == "noop" and fake.repeat_modify_semantics == "noop"
    assert fake.place_gtt_refuse_until_holding_visible is False and fake.place_gtt_creates_duplicate is False
    assert fake.hide_triggered_order_calls == 0 and fake.deleted_gtt_listing == "kept"
    assert fake.gtt_listing_limit is None and fake.gtt_listing_omit_ids == set() and fake.gtt_expiry_days is None
    # The pre-S3 behaviours the models make switchable, at their defaults:
    gid = _gtt(fake)                                      # a GTT without any holding: accepted
    assert fake.delete_gtt(gid) == {"trigger_id": gid}
    assert fake.delete_gtt(gid) == {"trigger_id": gid}    # a repeated delete succeeds again
    assert [g["status"] for g in fake.get_gtts()] == ["deleted"]   # and stays listed
    other = _gtt(fake)
    legs = [dict(LEG)]
    fake.modify_gtt(other, "single", "TCS", "NSE", [96.0], 100.0, legs)
    fake.modify_gtt(other, "single", "TCS", "NSE", [96.0], 100.0, legs)    # a repeated modify succeeds
    assert len(fake.gtt_book) == 2                        # one request, one GTT
    order_id = fake.trigger_gtt(other)
    assert order_id in [o["order_id"] for o in fake.orders()]   # a triggered order is listed at once
    assert fake._now().date() == datetime.now(IST).date()


# ------------------------------------------------------------------ B-27: repeated delete / modify (G5)
@pytest.mark.parametrize("semantics", ["noop", "error"])
def test_repeated_delete_follows_the_selected_semantics(semantics):
    fake = FakeKite()
    fake.repeat_delete_semantics = semantics
    gid = _gtt(fake)
    fake.delete_gtt(gid)
    if semantics == "noop":
        assert fake.delete_gtt(gid) == {"trigger_id": gid}
    else:
        with pytest.raises(InputException, match="already deleted"):
            fake.delete_gtt(gid)
    assert fake.gtt_book[gid]["status"] == "deleted"
    assert len(fake.calls_to("delete_gtt")) == 2          # the repeat reached the broker either way


@pytest.mark.parametrize("semantics", ["noop", "error"])
def test_repeated_modify_follows_the_selected_semantics(semantics):
    fake = FakeKite()
    fake.repeat_modify_semantics = semantics
    gid = _gtt(fake)
    legs = [dict(LEG, quantity=8)]
    fake.modify_gtt(gid, "single", "TCS", "NSE", [94.0], 100.0, legs)        # a real change: always applied
    if semantics == "noop":
        assert fake.modify_gtt(gid, "single", "TCS", "NSE", [94.0], 100.0, legs) == {"trigger_id": gid}
    else:
        with pytest.raises(InputException, match="unchanged"):
            fake.modify_gtt(gid, "single", "TCS", "NSE", [94.0], 100.0, legs)
    assert fake.gtt_book[gid]["orders"][0]["quantity"] == 8 and fake.gtt_book[gid]["status"] == "active"


def test_a_modify_of_a_deleted_gtt_still_raises_under_both_semantics():
    for semantics in ("noop", "error"):
        fake = FakeKite()
        fake.repeat_modify_semantics = semantics
        gid = _gtt(fake)
        fake.delete_gtt(gid)
        with pytest.raises(InputException, match="not active"):
            fake.modify_gtt(gid, "single", "TCS", "NSE", [96.0], 100.0, [dict(LEG)])


# ------------------------------------------------------------------ B-02: placement refused before visibility
def test_placement_is_refused_until_the_holding_is_visible_and_nothing_is_created():
    fake = FakeKite()
    fake.place_gtt_refuse_until_holding_visible = True
    with pytest.raises(InputException, match="Insufficient holdings"):
        _gtt(fake, qty=10)
    assert fake.gtt_book == {} and len(fake.calls_to("place_gtt")) == 1   # recorded, then refused
    fake.set_position("TCS", 6)                                           # the day's fill becomes visible...
    with pytest.raises(InputException):
        _gtt(fake, qty=10)                                                # ...but not all of it yet
    fake.set_holding("TCS", 4)
    gid = _gtt(fake, qty=10)                                              # 4 held + 6 bought today
    assert fake.gtt_book[gid]["status"] == "active"


def test_placement_visibility_counts_t1_and_fills_derived_from_the_order_book():
    fake = FakeKite()
    fake.place_gtt_refuse_until_holding_visible = True
    fake.positions_from_fills = True
    fake.place_order("regular", "NSE", "TCS", "BUY", 7, "CNC", "MARKET")
    fake.set_holding("TCS", 0, t1_quantity=3)
    assert fake._visible_quantity("NSE", "TCS") == 10
    _gtt(fake, qty=10)


# ------------------------------------------------------------------ B-05 / U1: DDPI-rejected trigger
def test_a_ddpi_rejected_trigger_leaves_a_rejected_order_and_nothing_filled():
    fake = FakeKite()
    gid = _gtt(fake)
    order_id = fake.trigger_gtt(gid, ddpi_rejected=True)
    order = fake.order_book[order_id]
    assert fake.gtt_book[gid]["status"] == "triggered"
    assert order["status"] == "REJECTED" and order["filled_quantity"] == 0
    assert "DDPI" in order["status_message"] and "simulated" in order["status_message"]
    assert fake.gtt_book[gid]["orders"][0]["result"]["order_result"]["order_id"] == order_id


# ------------------------------------------------------------------ B-24: one request, two GTTs
def test_one_place_gtt_request_can_create_two_gtts_and_return_one_id():
    fake = FakeKite()
    fake.place_gtt_creates_duplicate = True
    returned = _gtt(fake, legs=[dict(LEG, quantity=100), dict(LEG, quantity=100)])
    listed = fake.get_gtts()
    assert len(listed) == 2 and returned in [g["id"] for g in listed]
    sibling = next(g for g in listed if g["id"] != returned)
    assert sibling["orders"] == fake.gtt_book[returned]["orders"]   # identical shape: never merge by fingerprint
    assert len(fake.calls_to("place_gtt")) == 1


# ------------------------------------------------------------------ B-13: triggered order not yet listed
def test_a_triggered_order_can_stay_out_of_orders_for_n_listings():
    fake = FakeKite()
    fake.hide_triggered_order_calls = 2
    gid = _gtt(fake, legs=[dict(LEG, quantity=100), dict(LEG, quantity=100)])
    order_id = fake.trigger_gtt(gid, leg=1)
    assert fake.gtt_book[gid]["status"] == "triggered"
    assert fake.gtt_book[gid]["orders"][1]["result"]["order_id"] == order_id   # the GTT says it exists...
    assert order_id not in [o["order_id"] for o in fake.orders()]              # ...the listing does not
    assert order_id not in [o["order_id"] for o in fake.orders()]
    assert order_id in [o["order_id"] for o in fake.orders()]                  # third listing: visible
    assert fake.order_history(order_id)[0]["order_id"] == order_id


# ------------------------------------------------------------------ B-28 / B-08: listing completeness
def test_deleted_gtts_can_be_pruned_from_the_listing():
    fake = FakeKite()
    fake.deleted_gtt_listing = "pruned"
    kept, gone = _gtt(fake), _gtt(fake)
    fake.delete_gtt(gone)
    assert [g["id"] for g in fake.get_gtts()] == [kept]
    assert fake.gtt_book[gone]["status"] == "deleted"     # it exists at the "broker"; only the listing omits it


def test_a_partial_or_single_page_listing_omits_gtts_that_exist():
    fake = FakeKite()
    ids = [_gtt(fake) for _ in range(5)]
    fake.gtt_listing_omit_ids = {ids[1]}
    assert [g["id"] for g in fake.get_gtts()] == [ids[0]] + ids[2:]
    fake.gtt_listing_omit_ids = set()
    fake.gtt_listing_limit = 2
    assert [g["id"] for g in fake.get_gtts()] == ids[:2]
    assert len(fake.gtt_book) == 5


def test_listing_lag_and_completeness_models_compose():
    fake = FakeKite()
    fake.gtt_list_lag_calls = 1
    fake.gtt_listing_limit = 1
    first = _gtt(fake)
    assert fake.get_gtts() == []                          # lagging
    assert [g["id"] for g in fake.get_gtts()] == [first]


# ------------------------------------------------------------------ day rollover (proposal §9.2 item 6)
def test_rollover_moves_the_day_net_to_t1_then_settles_it():
    fake = FakeKite()
    fake.positions_from_fills = True
    fake.place_order("regular", "NSE", "TCS", "BUY", 5, "CNC", "MARKET")
    fake.rollover()
    [h] = [h for h in fake.holdings() if h["tradingsymbol"] == "TCS"]
    assert (h["quantity"], h["t1_quantity"]) == (0, 5)
    assert fake.positions()["net"] == [] and fake.orders() == []
    fake.rollover()
    [h] = [h for h in fake.holdings() if h["tradingsymbol"] == "TCS"]
    assert (h["quantity"], h["t1_quantity"]) == (5, 0)
    assert fake.day_offset == 2


def test_rollover_applies_a_net_sale_to_the_settled_quantity_and_keeps_gtts():
    fake = FakeKite()
    fake.set_holding("TCS", 10)
    gid = _gtt(fake)
    fake.set_position("TCS", -4)
    fake.rollover()
    [h] = [h for h in fake.holdings() if h["tradingsymbol"] == "TCS"]
    assert h["quantity"] == 6 and fake.gtt_book[gid]["status"] == "active"
    assert [g["id"] for g in fake.get_gtts()] == [gid]


def test_rollover_moves_the_clock_and_earlier_orders_stay_in_order_history():
    fake = FakeKite()
    order_id = fake.place_order("regular", "NSE", "TCS", "BUY", 1, "CNC", "MARKET")
    today = fake._now().date()
    fake.rollover(days=3)
    assert fake._now().date() == today + timedelta(days=3)
    assert fake.orders() == []
    assert fake.order_history(order_id)[0]["order_id"] == order_id
    fake.order_history_previous_days = "raise"           # the Phase 0 model now sees an earlier-day order
    with pytest.raises(InputException, match="earlier-day"):
        fake.order_history(order_id)
    new_id = fake.place_order("regular", "NSE", "TCS", "BUY", 1, "CNC", "MARKET")
    assert fake.order_book[new_id]["order_timestamp"][:10] == fake._now().strftime("%Y-%m-%d")


def test_rollover_expires_old_active_gtts_only_when_configured():
    fake = FakeKite()
    old = _gtt(fake)
    fake.rollover(days=5)
    assert fake.gtt_book[old]["status"] == "active"       # no expiry by default
    fake.gtt_expiry_days = 7
    newer = _gtt(fake)                                    # placed on day 5
    deleted = _gtt(fake)
    fake.delete_gtt(deleted)
    fake.rollover(days=2)                                 # day 7: the first GTT is 7 days old
    assert fake.gtt_book[old]["status"] == "expired"
    assert fake.gtt_book[newer]["status"] == "active" and fake.gtt_book[deleted]["status"] == "deleted"
