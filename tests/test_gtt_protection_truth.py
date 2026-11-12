"""S2/S3: a position is PROTECTED only on a confirmed, fresh broker observation.

The app's own record of its protective GTT (kite_gtt_id, the quantity it placed or resized it
to) is never proof of protection. PROTECTED needs a broker READ of the GTT book, no older than
config.GTT_OBSERVATION_MAX_AGE_SECONDS, of an active GTT on the position's own instrument
whose every leg is a SELL of exactly the confirmed filled quantity held:

- never observed (a restart, or no loop has read the book yet)    -> UNVERIFIED
- observation older than the maximum age                           -> STALE
- incorrect or missing for a filled open position                  -> NOT_PROTECTED (+ reason)
- an accepted entry that has filled nothing                        -> PENDING_FILL (not "unprotected")

FakeKite and scratch databases only.
"""

from datetime import datetime, timedelta, timezone

from kiteconnect.exceptions import NetworkException

from backend import config, gtt_observations
from conftest import ticket_row
from test_live_safety import _monitor
from test_partial_fills import _fill, _held_position, _place_entry, _reconcile


def _lc(env, ticket_id) -> dict:
    r = env.client.get(f"/api/tickets/{ticket_id}")
    assert r.status_code == 200, r.text
    return r.json()["data"]["lifecycle"]


def _observe(env):
    gtt_observations.record_book(env.kite.get_gtts())


def _summary(env) -> dict:
    r = env.client.get("/api/app-positions?view=active")
    assert r.status_code == 200, r.text
    return r.json()["summary"]


# ---------------------------------------------------------------- never from the database alone

def test_01_a_database_record_alone_is_unverified_never_protected(env):
    buy_id, gtt_id = _held_position(env, 40)
    row = ticket_row(env.db, buy_id)
    assert row["kite_gtt_id"] == gtt_id and row["filled_quantity"] == 40   # the app's record is complete
    lc = _lc(env, buy_id)
    assert lc["protection_status"] == "UNVERIFIED" and lc["protection_label"] == "PROTECTION UNVERIFIED"
    assert (lc["gtt_source"], lc["gtt_status"], lc["gtt_quantity"]) == (None, None, None)
    assert lc["gtt_recorded_quantity"] == 40          # the record is shown, labelled as the record
    assert lc["protection_alert"] is True and "PROTECTION_UNVERIFIED" in lc["attention_reasons"]


def test_02_a_fresh_correct_broker_observation_is_protected(env):
    buy_id, gtt_id = _held_position(env, 40)
    _observe(env)
    lc = _lc(env, buy_id)
    assert (lc["protection_status"], lc["gtt_source"], lc["gtt_status"], lc["gtt_quantity"]) == (
        "PROTECTED", "broker", "active", 40)
    assert lc["protection_reason"] is None and lc["protection_alert"] is False
    assert lc["gtt_observation_age_seconds"] <= 5


def test_03_restart_forgets_every_observation(env):
    buy_id, _ = _held_position(env, 40)
    _observe(env)
    assert _lc(env, buy_id)["protection_status"] == "PROTECTED"
    gtt_observations.reset()                         # a new process: process memory is empty
    assert _lc(env, buy_id)["protection_status"] == "UNVERIFIED"
    _reconcile()                                     # the first reconciliation cycle reads the book
    assert _lc(env, buy_id)["protection_status"] == "PROTECTED"


def test_04_an_observation_of_another_gtt_does_not_count(env):
    buy_id, gtt_id = _held_position(env, 40)
    gtt_observations.record_book([g for g in env.kite.get_gtts() if g["id"] != gtt_id])
    assert _lc(env, buy_id)["protection_status"] == "UNVERIFIED"


# ---------------------------------------------------------------- age

def test_05_an_expired_observation_is_stale(env):
    buy_id, gtt_id = _held_position(env, 40)
    old = (datetime.now(timezone.utc) - timedelta(seconds=config.GTT_OBSERVATION_MAX_AGE_SECONDS + 5))
    gtt_observations.record_book(env.kite.get_gtts(), observed_at=old.strftime("%Y-%m-%d %H:%M:%S"))
    lc = _lc(env, buy_id)
    assert lc["protection_status"] == "STALE" and lc["protection_label"] == "PROTECTION STALE"
    assert lc["gtt_observation_age_seconds"] > config.GTT_OBSERVATION_MAX_AGE_SECONDS
    assert lc["protection_alert"] is True


def test_06_an_observation_just_inside_the_age_limit_still_counts(env):
    buy_id, _ = _held_position(env, 40)
    recent = datetime.now(timezone.utc) - timedelta(seconds=config.GTT_OBSERVATION_MAX_AGE_SECONDS - 30)
    gtt_observations.record_book(env.kite.get_gtts(), observed_at=recent.strftime("%Y-%m-%d %H:%M:%S"))
    assert _lc(env, buy_id)["protection_status"] == "PROTECTED"


def test_07_a_stale_terminal_status_is_still_not_protected(env):
    buy_id, gtt_id = _held_position(env, 40)
    env.kite.set_gtt_status(gtt_id, "triggered")
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    gtt_observations.record_book(env.kite.get_gtts(), observed_at=old.strftime("%Y-%m-%d %H:%M:%S"))
    lc = _lc(env, buy_id)
    assert (lc["protection_status"], lc["protection_reason"]) == ("NOT_PROTECTED", "GTT_NOT_ACTIVE")


def test_08_the_max_age_is_two_reconciliation_cycles_plus_slack():
    assert config.GTT_OBSERVATION_MAX_AGE_SECONDS == 2 * config.RECONCILIATION_INTERVAL_SECONDS + 60


# ---------------------------------------------------------------- incorrect protection

def test_09_a_gtt_covering_more_than_is_held_is_not_protected(env):
    buy_id, _ = _held_position(env, 40, gtt_quantity=100)
    _observe(env)
    lc = _lc(env, buy_id)
    assert (lc["protection_status"], lc["protection_reason"]) == ("NOT_PROTECTED", "QUANTITY_EXCEEDS_POSITION")
    assert lc["severity"] == "critical" and (lc["gtt_quantity"], lc["open_quantity"]) == (100, 40)


def test_10_a_gtt_covering_fewer_than_are_held_is_not_protected(env):
    buy_id, _ = _held_position(env, 40, gtt_quantity=25)
    _observe(env)
    assert (_lc(env, buy_id)["protection_status"], _lc(env, buy_id)["protection_reason"]) == (
        "NOT_PROTECTED", "QUANTITY_BELOW_POSITION")


def test_11_one_wrong_leg_of_an_oco_is_not_protected(env):
    buy_id, gtt_id = _held_position(env, 40)
    env.kite.gtt_book[gtt_id]["orders"][1]["quantity"] = 39
    _observe(env)
    assert _lc(env, buy_id)["protection_reason"] == "QUANTITY_BELOW_POSITION"


def test_12_a_gtt_for_another_instrument_is_not_protected(env):
    buy_id, gtt_id = _held_position(env, 40)
    env.kite.gtt_book[gtt_id]["condition"]["tradingsymbol"] = "INFY"
    _observe(env)
    assert (_lc(env, buy_id)["protection_status"], _lc(env, buy_id)["protection_reason"]) == (
        "NOT_PROTECTED", "INSTRUMENT_MISMATCH")


def test_13_a_gtt_on_another_exchange_is_not_protected(env):
    buy_id, gtt_id = _held_position(env, 40)
    env.kite.gtt_book[gtt_id]["condition"]["exchange"] = "BSE"
    _observe(env)
    assert _lc(env, buy_id)["protection_reason"] == "INSTRUMENT_MISMATCH"


def test_14_a_gtt_with_another_instrument_token_is_not_protected(env):
    buy_id, gtt_id = _held_position(env, 40)
    env.kite.gtt_book[gtt_id]["condition"]["instrument_token"] = 408065     # INFY's token, ticket is TCS's
    _observe(env)
    assert _lc(env, buy_id)["protection_reason"] == "INSTRUMENT_MISMATCH"


def test_15_a_buy_leg_is_not_protection(env):
    buy_id, gtt_id = _held_position(env, 40)
    env.kite.gtt_book[gtt_id]["orders"][0]["transaction_type"] = "BUY"
    _observe(env)
    assert _lc(env, buy_id)["protection_reason"] == "LEGS_INVALID"


def test_16_a_gtt_absent_from_a_complete_book_read_is_missing(env):
    buy_id, gtt_id = _held_position(env, 40)
    _observe(env)
    assert _lc(env, buy_id)["protection_status"] == "PROTECTED"
    del env.kite.gtt_book[gtt_id]                 # gone from Kite's book
    _reconcile()
    lc = _lc(env, buy_id)
    assert lc["protection_status"] == "NOT_PROTECTED" and lc["gtt_status"] == "missing"


def test_17_no_gtt_for_a_filled_position_is_not_protected(env):
    buy_id, _ = _held_position(env, 40, kite_gtt_id=None)
    lc = _lc(env, buy_id)
    assert (lc["protection_status"], lc["protection_reason"]) == ("NOT_PROTECTED", "NO_GTT")
    assert _summary(env)["unprotected"] == 1


# ---------------------------------------------------------------- accepted, not filled

def test_18_an_accepted_unfilled_entry_is_pending_fill_not_unprotected(env, market_open):
    buy_id, _, gtt_id = _place_entry(env, 100)
    lc = _lc(env, buy_id)
    assert (lc["lifecycle_state"], lc["protection_status"], lc["protection_reason"]) == (
        "ENTRY_OPEN", "PENDING_FILL", "GTT_BEFORE_FILL")
    assert lc["open_quantity"] == 0 and lc["severity"] != "critical"
    summary = _summary(env)
    assert summary["unprotected"] == 0 and summary["protection_attention"] == 1


def test_19_pending_fill_even_when_its_gtt_failed(env, market_open):
    from test_protection_states import _entry_with_failed_gtt
    buy_id, _ = _entry_with_failed_gtt(env)
    lc = _lc(env, buy_id)
    assert (lc["protection_status"], lc["protection_reason"]) == ("PENDING_FILL", "NO_GTT")
    assert _summary(env)["unprotected"] == 0


# ---------------------------------------------------------------- the resize is verified, never assumed

def test_20_a_partial_fill_resize_is_verified_by_a_read_back(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env, 100)
    reads = len(env.kite.calls_to("get_gtts"))
    _fill(env, order_id, 40)
    _monitor(env)
    assert len(env.kite.calls_to("get_gtts")) >= reads + 2     # the read before the resize, and the read-back
    lc = _lc(env, buy_id)
    assert (lc["protection_status"], lc["gtt_quantity"], lc["gtt_source"]) == ("PROTECTED", 40, "broker")


def test_21_a_resize_whose_read_back_fails_is_unverified(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env, 100)
    calls = {"n": 0}

    def fail_second_read(kwargs):
        calls["n"] += 1
        if calls["n"] == 2:               # the read-back right after the accepted modify
            raise NetworkException("timeout")
    env.kite.on_call("get_gtts", fail_second_read)
    _fill(env, order_id, 40)
    _monitor(env)
    env.kite.on_call("get_gtts", None)
    assert {leg["quantity"] for leg in env.kite.gtt_book[gtt_id]["orders"]} == {40}   # it did resize
    lc = _lc(env, buy_id)
    assert lc["protection_status"] == "UNVERIFIED" and lc["gtt_quantity"] is None


def test_22_a_failed_resize_with_an_observation_is_not_protected(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env, 100)
    env.kite.raise_on_next("modify_gtt", NetworkException("timeout"))
    _fill(env, order_id, 40)
    _monitor(env)
    lc = _lc(env, buy_id)
    assert (lc["protection_status"], lc["protection_reason"]) == ("NOT_PROTECTED", "QUANTITY_EXCEEDS_POSITION")


# ---------------------------------------------------------------- paper positions

def test_23_a_paper_position_is_judged_from_the_paper_brokers_own_book(env):
    from backend import paper_broker
    conn = env.db.get_connection()
    try:
        from conftest import account_id
        broker = paper_broker.PaperBroker(conn, account_id(env.db))
        legs = [{"transaction_type": "SELL", "quantity": 10, "order_type": "LIMIT", "product": "CNC",
                 "price": 2880.0}]
        gtt_id = broker.place_gtt("single", "TCS", "NSE", [2900.0], 3000.0, legs)["trigger_id"]
        conn.commit()
    finally:
        conn.close()
    from test_baseline_api import _insert_ticket
    buy_id = _insert_ticket(env.db, mode="dry_run", kite_order_id="PAPER-O-1", kite_gtt_id=gtt_id, quantity=10,
                            entry_fill_status="filled", filled_quantity=10, filled_price=3000.0,
                            executed_at="2026-09-01 04:00:00")
    lc = _lc(env, buy_id)
    assert (lc["protection_status"], lc["gtt_source"], lc["gtt_quantity"]) == ("PROTECTED", "paper_broker", 10)
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE paper_gtts SET status = 'triggered' WHERE paper_gtt_id = ?", (gtt_id,))
        conn.commit()
    finally:
        conn.close()
    assert _lc(env, buy_id)["protection_reason"] == "GTT_NOT_ACTIVE"


# ---------------------------------------------------------------- the observation store itself

def test_24_an_accepted_modify_only_invalidates_what_was_seen():
    gtt_observations.reset()
    gtt_observations.record_book([{"id": 7, "status": "active", "condition": {"exchange": "NSE",
                                   "tradingsymbol": "TCS"}, "orders": [{"transaction_type": "SELL", "quantity": 5}]}])
    assert gtt_observations.get(7)["quantity"] == 5
    gtt_observations.invalidate(7)
    assert gtt_observations.get(7) is None
    assert not hasattr(gtt_observations, "record_resize")


def test_25_expected_ids_absent_from_the_book_are_recorded_missing():
    gtt_observations.reset()
    gtt_observations.record_book([], expected=[42])
    assert gtt_observations.get(42)["status"] == "missing"
