"""Phase 3 S6: GTT observation, reconciliation and exposure aggregation (backend/protection_reconcile.py,
backend/protection_exposure.py), on env's tmp database with FakeKite. The FakeKite models used here (listing lag,
partial or single-page listing, pruned deletes, hidden triggered orders, duplicate create) are UNVERIFIED models:
these tests prove the engine's handling of each modelled behaviour, not that Kite behaves that way."""

from datetime import timedelta

import pytest

from backend import config, instance_lock, protection, protection_exposure, protection_reconcile
from protection_helpers import (
    account_id, claim, conn_of, force_state, gtt, journal, make_position, op, read, request,
)


@pytest.fixture
def jenv(env):
    journal(env)
    protection_reconcile._missing.clear()
    return env


def _rec(env, op_id, ev=None, now=None):
    conn = conn_of(env)
    try:
        return protection_reconcile.reconcile_op(conn, op_id, ev or read(env, "reconciliation"), now=now)
    finally:
        conn.close()


def _ago(seconds):
    return (protection.utc_now() - timedelta(seconds=seconds)).isoformat()


def _exposure(env, ev=None, symbol="TCS", mode="live"):
    conn = conn_of(env)
    try:
        return protection_exposure.evaluate(conn, account_id=account_id(conn), mode=mode, exchange="NSE",
                                            tradingsymbol=symbol, evidence=ev or read(env, "emergency_exit"))
    finally:
        conn.close()


def _link(env, pid, gid):
    conn = conn_of(env)
    try:
        conn.execute("UPDATE trade_tickets SET kite_gtt_id = ? WHERE id = ?", (gid, pid))
        conn.commit()
    finally:
        conn.close()


def _exit_fill(env, pid, order_id, filled):
    conn = conn_of(env)
    try:
        conn.execute("INSERT INTO trade_tickets (account_id, symbol, exchange, instrument_token, transaction_type, quantity, "
                     "entry_price, entry_order_type, stop_loss, target_1, product_type, rationale, status, mode, "
                     "closes_ticket_id, kite_order_id, filled_quantity, entry_fill_status) SELECT account_id, symbol, "
                     "exchange, instrument_token, 'SELL', ?, 3000, 'LIMIT', 2900, 3300, 'CNC', 'gtt exit', 'executed', mode, "
                     "id, ?, ?, 'filled' FROM trade_tickets WHERE id = ?", (filled, order_id, filled, pid))
        conn.commit()
    finally:
        conn.close()


# ================================================================ reconciliation: rows 9-13
def test_S6_R01_sending_within_the_grace_is_left_and_past_it_or_older_epoch_becomes_unknown(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    force_state(jenv, row["id"], "sending", sent_at=_ago(1),
                evidence='[{"send": 1, "epoch": %d}]' % instance_lock.owner_epoch())
    assert _rec(jenv, row["id"])["action"] == "IN_FLIGHT_GRACE"
    force_state(jenv, row["id"], "sending", sent_at=_ago(config.PROTECTION_INFLIGHT_GRACE_SECONDS + 1))
    out = _rec(jenv, row["id"])
    assert out["action"] == "SENDING_TO_UNKNOWN" and out["op"]["complete_reads_after"] == 0
    row2 = claim(jenv, make_position(jenv, symbol="INFY", token=408065), req=request(symbol="INFY"))
    force_state(jenv, row2["id"], "sending", sent_at=_ago(1), evidence='[{"send": 1, "epoch": -5}]')
    assert _rec(jenv, row2["id"])["action"] == "SENDING_TO_UNKNOWN"     # another epoch: row 10 at once


def test_S6_R02_acknowledged_create_read_back(jenv):
    pid = make_position(jenv)
    req = request(10)
    row = claim(jenv, pid, req=req)
    gid = gtt(jenv, req)
    force_state(jenv, row["id"], "acknowledged", result_gtt_id=gid)
    _link(jenv, pid, gid)
    incomplete = read(jenv, "reconciliation")
    incomplete.complete = False
    assert _rec(jenv, row["id"], incomplete)["action"] == "READ_INCOMPLETE"
    out = _rec(jenv, row["id"])
    assert out["action"] == "CONFIRMED"
    conn = conn_of(jenv)
    try:
        assert protection.get_position(conn, pid)["protection_state"] == "PROTECTION_CONFIRMED"
    finally:
        conn.close()


@pytest.mark.parametrize("fault,why", [("instrument", "WRONG_INSTRUMENT"), ("buy", "BUY_LEG"), ("mis", "NON_CNC")])
def test_S6_R03_acknowledged_create_contradicted_goes_to_needs_review(jenv, fault, why):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    req = request(10, symbol="INFY") if fault == "instrument" else request(10)
    gid = gtt(jenv, req)
    if fault == "buy":
        jenv.kite.gtt_book[gid]["orders"][0]["transaction_type"] = "BUY"
    if fault == "mis":
        jenv.kite.gtt_book[gid]["orders"][0]["product"] = "MIS"
    force_state(jenv, row["id"], "acknowledged", result_gtt_id=gid)
    out = _rec(jenv, row["id"])
    assert out["action"] == f"NEEDS_REVIEW:{why}"


def test_S6_R04_acknowledged_create_not_listed_is_never_absent(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    force_state(jenv, row["id"], "acknowledged", result_gtt_id=9999)
    assert _rec(jenv, row["id"])["action"] == "READBACK_PENDING"


# ================================================================ unknown create: adopt / review / settle
def test_S6_R05_exactly_one_fingerprint_match_is_adopted_and_linked(jenv):
    pid = make_position(jenv)
    req = request(10)
    row = claim(jenv, pid, req=req)
    force_state(jenv, row["id"], "unknown")
    gid = gtt(jenv, req)
    out = _rec(jenv, row["id"])
    assert out["action"] == "ADOPTED" and str(out["op"]["result_gtt_id"]) == str(gid)
    conn = conn_of(jenv)
    try:
        assert protection.get_position(conn, pid)["kite_gtt_id"] == gid
    finally:
        conn.close()


def test_S6_R06_a_duplicate_create_b24_goes_to_needs_review_never_adopted(jenv):
    pid = make_position(jenv)
    req = request(10)
    row = claim(jenv, pid, req=req)
    force_state(jenv, row["id"], "unknown")
    gtt(jenv, req)
    gtt(jenv, req)                                        # one request, two GTTs (the B-24 model)
    out = _rec(jenv, row["id"])
    assert out["action"] == "NEEDS_REVIEW:MULTIPLE_FINGERPRINT_MATCHES" and len(out["candidates"]) == 2


def test_S6_R07_another_unlinked_sell_gtt_goes_to_needs_review(jenv):
    pid = make_position(jenv)
    req = request(10)
    row = claim(jenv, pid, req=req)
    force_state(jenv, row["id"], "unknown")
    gtt(jenv, req)
    gtt(jenv, request(10, trigger=2800.0))
    assert _rec(jenv, row["id"])["action"] == "NEEDS_REVIEW:UNLINKED_SELL_GTT_ON_INSTRUMENT"


def _settle_op(env):
    pid = make_position(env)
    row = claim(env, pid)
    sent = protection.utc_now() - timedelta(seconds=config.PROTECTION_SETTLE_SECONDS + 300)
    force_state(env, row["id"], "unknown", sent_at=sent.isoformat(), evidence="[]")
    return row, sent


def _ev_at(env, started, gtts_at=None, mono=None):
    ev = read(env, "reconciliation")
    ev.started_at = started
    ev.gtts_at = gtts_at or started + timedelta(seconds=1)
    if mono is not None:
        ev.started_mono, ev.gtts_mono = mono, mono + 1
    return ev


def test_S6_R08_settle_counts_only_reads_after_t0_and_spaced(jenv, monkeypatch):
    row, sent = _settle_op(jenv)
    monkeypatch.setattr(protection_reconcile, "takeover_at", lambda conn: sent - timedelta(hours=1))
    t0 = sent + timedelta(seconds=config.PROTECTION_SETTLE_SECONDS)
    early = _ev_at(jenv, t0 - timedelta(seconds=1), mono=1000.0)
    assert _rec(jenv, row["id"], early)["action"] == "SETTLE_NOT_COUNTED:BEFORE_T0"
    r1 = _ev_at(jenv, t0, mono=1000.0)                                     # exactly at the bound: counts
    assert _rec(jenv, row["id"], r1)["action"] == "SETTLE_COUNTED:1"
    short = _ev_at(jenv, r1.gtts_at + timedelta(seconds=config.SETTLE_READ_SPACING_SECONDS - 1), mono=1060.0)
    assert _rec(jenv, row["id"], short)["action"] == "SETTLE_NOT_COUNTED:SPACING_WALL"
    forward_step = _ev_at(jenv, r1.gtts_at + timedelta(seconds=config.SETTLE_READ_SPACING_SECONDS), mono=1030.0)
    assert _rec(jenv, row["id"], forward_step)["action"] == "SETTLE_NOT_COUNTED:SPACING_MONOTONIC"
    r2 = _ev_at(jenv, r1.gtts_at + timedelta(seconds=config.SETTLE_READ_SPACING_SECONDS), mono=1062.0)
    out = _rec(jenv, row["id"], r2)
    assert out["action"] == "ABSENT_AFTER_SETTLE" and out["op"]["complete_reads_after"] == 2
    entries = [e for e in protection.evidence_entries(out["op"]) if "settle_read" in e]
    assert [e["settle_read"] for e in entries] == [r1.read_id, r2.read_id]


def test_S6_R09_a_resume_between_the_settle_reads_restarts_the_count(jenv, monkeypatch):
    row, sent = _settle_op(jenv)
    monkeypatch.setattr(protection_reconcile, "takeover_at", lambda conn: sent - timedelta(hours=1))
    t0 = sent + timedelta(seconds=config.PROTECTION_SETTLE_SECONDS)
    r1 = _ev_at(jenv, t0, mono=1000.0)
    assert _rec(jenv, row["id"], r1)["action"] == "SETTLE_COUNTED:1"
    with instance_lock._state.cv:
        instance_lock._state.resume_seq += 1                           # a resume was detected
    r2 = _ev_at(jenv, r1.gtts_at + timedelta(seconds=config.SETTLE_READ_SPACING_SECONDS), mono=1062.0)
    assert _rec(jenv, row["id"], r2)["action"] == "SETTLE_COUNTED:1"    # the count restarted


def test_S6_R10_failed_or_partial_reads_count_nothing_and_listing_lag_never_concludes_absent(jenv):
    row, sent = _settle_op(jenv)
    bad = read(jenv, "reconciliation")
    bad.complete = False
    assert _rec(jenv, row["id"], bad)["action"] == "READ_INCOMPLETE"
    assert op(jenv, row["id"])["complete_reads_after"] == 0
    jenv.kite.gtt_list_lag_calls = 2                                    # the GTT lands but lags in the listing
    gid = gtt(jenv, request(10))
    assert _rec(jenv, row["id"])["action"].startswith("SETTLE_")        # not visible yet: never "absent"
    _rec(jenv, row["id"])
    assert _rec(jenv, row["id"])["action"] == "ADOPTED" and gid


# ================================================================ unknown cancel / modify: rows 15, 15a, 15b
def _unknown_cancel(env, target, *, flat=True):
    pid = make_position(env, closed=flat)
    row = claim(env, pid, "cancel", "user_cancel", target=target)
    force_state(env, row["id"], "unknown", sent_at=_ago(config.PROTECTION_SETTLE_SECONDS + 300), evidence="[]")
    return row


def test_S6_R11_unknown_cancel_outcomes(jenv):
    deleted = gtt(jenv, status="deleted")
    assert _rec(jenv, _unknown_cancel(jenv, deleted)["id"])["action"] == "CONFIRMED"
    active = gtt(jenv)
    assert _rec(jenv, _unknown_cancel(jenv, active)["id"])["action"] == "STILL_ACTIVE"
    trig = gtt(jenv, status="triggered")
    assert _rec(jenv, _unknown_cancel(jenv, trig)["id"])["action"] == "NEEDS_REVIEW:CANCEL_TARGET_TRIGGERED"
    weird = gtt(jenv, status="paused")
    assert _rec(jenv, _unknown_cancel(jenv, weird)["id"])["action"] == "NEEDS_REVIEW:TARGET_STATUS_UNKNOWN"


def test_S6_R12_a_pruned_deleted_gtt_is_never_confirmed_settle_then_needs_review(jenv, monkeypatch):
    target = gtt(jenv, status="deleted")
    jenv.kite.deleted_gtt_listing = "pruned"                           # deleted GTTs vanish from the listing
    row = _unknown_cancel(jenv, target)
    sent = protection.utc_now() - timedelta(seconds=config.PROTECTION_SETTLE_SECONDS + 300)
    monkeypatch.setattr(protection_reconcile, "takeover_at", lambda conn: sent - timedelta(hours=1))
    t = protection.utc_now()
    a = _rec(jenv, row["id"], _ev_at(jenv, t, mono=1.0))
    assert a["action"] == "MISSING_SETTLE_COUNTED:1"
    b = _rec(jenv, row["id"], _ev_at(jenv, t + timedelta(seconds=config.SETTLE_READ_SPACING_SECONDS + 2), mono=70.0))
    assert b["action"] == "NEEDS_REVIEW:CANCEL_TARGET_MISSING_LISTING_UNVERIFIED"
    assert b["op"]["state"] == "needs_review"                          # never confirmed by absence (B-28)


def test_S6_R13_unknown_modify(jenv):
    pid = make_position(jenv)
    old = request(10, trigger=2910.0)
    target = gtt(jenv, old)
    _link(jenv, pid, target)
    new = request(10, trigger=2950.0, price=2940.0)
    row = claim(jenv, pid, "modify", "trailing", target=target, direction="tighten", req=new)
    force_state(jenv, row["id"], "unknown")
    assert _rec(jenv, row["id"])["action"] == "PRESTATE_OR_OTHER"
    jenv.kite.gtt_book[target]["condition"]["trigger_values"] = [2950.0]
    jenv.kite.gtt_book[target]["orders"][0]["price"] = 2940.0
    assert _rec(jenv, row["id"])["action"] == "CONFIRMED"
    row2 = claim(jenv, pid, "modify", "trailing", target=target, direction="tighten", req=request(10, trigger=2960.0))
    force_state(jenv, row2["id"], "unknown")
    del jenv.kite.gtt_book[target]
    assert _rec(jenv, row2["id"])["action"] == "NEEDS_REVIEW:MODIFY_TARGET_MISSING"


# ================================================================ late GTT watch, M-2, M-5
def test_S6_R14_a_late_gtt_after_absent_after_settle_is_reported_never_deleted(jenv):
    pid = make_position(jenv)
    req = request(10)
    row = claim(jenv, pid, req=req)
    force_state(jenv, row["id"], "absent_after_settle", complete_reads_after=2)
    gtt(jenv, req)
    conn = conn_of(jenv)
    try:
        found = protection_reconcile.late_match_watch(conn, read(jenv, "reconciliation"))
        assert found and found[0]["op_id"] == row["id"]
        assert protection.get_position(conn, pid)["protection_state"] == "PROTECTION_NEEDS_REVIEW"
        assert conn.execute("SELECT COUNT(*) FROM reconciliation_log WHERE issue_code = 'LATE_GTT_APPEARED'").fetchone()[0] == 1
    finally:
        conn.close()
    assert op(jenv, row["id"])["state"] == "absent_after_settle" and jenv.kite.calls_to("delete_gtt") == []


def test_S6_R15_m2_conditions(jenv):
    pid = make_position(jenv)
    req = request(10)
    survivor_op = claim(jenv, pid, req=req)
    survivor = gtt(jenv, req)
    force_state(jenv, survivor_op["id"], "confirmed", result_gtt_id=survivor)
    _link(jenv, pid, survivor)
    dup_op = claim(jenv, pid, "modify", "user_modify", target=survivor, direction="tighten", req=request(10, trigger=2915.0))
    dup = gtt(jenv, req)
    force_state(jenv, dup_op["id"], "rejected", evidence='[{"response": "acknowledged", "result_gtt_id": "%s"}]' % dup,
                result_gtt_id=None)
    conn = conn_of(jenv)
    try:
        with protection.journal_transaction(conn):
            conn.execute("UPDATE protection_operations SET result_gtt_id = ? WHERE id = ?", (dup, dup_op["id"]))
        cancel = {"position_id": pid, "target_gtt_id": dup}
        c = protection_reconcile.m2_conditions(conn, cancel, read(jenv))
    finally:
        conn.close()
    assert c == {"target_app_proven": True, "different_linked_gtt": True, "survivor_protects_full_quantity": True,
                 "no_exit_or_change_open": True}


def test_S6_R16_a_missing_linked_gtt_is_never_unlinked_external_delete_is_failed(jenv):
    pid = make_position(jenv, gtt_id=7777)
    conn = conn_of(jenv)
    try:
        ev1 = read(jenv, "reconciliation")
        assert protection_reconcile.linked_gtt_watch(conn, ev1) == []
        ev2 = read(jenv, "reconciliation")
        ev2.started_at = ev1.started_at + timedelta(seconds=config.SETTLE_READ_SPACING_SECONDS + 1)
        ev2.gtts_at = ev2.started_at
        assert protection_reconcile.linked_gtt_watch(conn, ev2)[0]["finding"] == "LINKED_GTT_MISSING"
        assert protection.get_position(conn, pid)["kite_gtt_id"] == 7777
    finally:
        conn.close()
    pid2 = make_position(jenv, symbol="INFY", token=408065)
    g = gtt(jenv, request(symbol="INFY"), status="deleted")
    _link(jenv, pid2, g)
    conn = conn_of(jenv)
    try:
        out = protection_reconcile.linked_gtt_watch(conn, read(jenv, "reconciliation"))
        assert {"position_id": pid2, "finding": "GTT_REMOVED_EXTERNALLY"} in out
        assert protection.get_position(conn, pid2)["protection_state"] == "PROTECTION_FAILED"
    finally:
        conn.close()


# ================================================================ exposure: membership and deduplication
def test_S6_X01_one_id_through_three_sources_counts_once(jenv):
    pid = make_position(jenv)
    req = request(10)
    row = claim(jenv, pid, req=req)
    gid = gtt(jenv, req)
    force_state(jenv, row["id"], "acknowledged", result_gtt_id=gid)      # source 2 (and 5 below)
    _link(jenv, pid, gid)                                                 # source 1
    x = _exposure(jenv)                                                   # source 6 (listed)
    assert x.established and x.g_max == 1 and x.s_agg == 10
    assert x.members[0].sources >= {1, 2, 6}


def test_S6_X02_different_ids_with_identical_payloads_are_two_members(jenv):
    make_position(jenv)
    gtt(jenv, request(10))
    gtt(jenv, request(10))
    x = _exposure(jenv)
    assert x.g_max == 2 and x.s_agg == 20


def test_S6_X03_r5_1_an_id_less_create_is_unknown_and_never_merged(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid, req=request(10, legs=2))
    force_state(jenv, row["id"], "unknown")
    gtt(jenv, request(10, legs=2))                                        # a listed GTT that may or may not be it
    x = _exposure(jenv)
    keys = sorted(m.key for m in x.members)
    assert x.g_max == 2 and f"op:{row['id']}" in keys
    assert x.s_agg is None and not x.gate_open()                          # not 20, not "at least 20"


def test_S6_X04_r5_1_positive_control_identified_results_are_separate_members(jenv):
    pid = make_position(jenv)
    req = request(10)
    row = claim(jenv, pid, req=req)
    x_id = gtt(jenv, req)
    y_id = gtt(jenv, req)
    force_state(jenv, row["id"], "confirmed", result_gtt_id=x_id)
    _link(jenv, pid, x_id)
    x = _exposure(jenv)
    assert sorted(m.key for m in x.members) == sorted([str(x_id), str(y_id)]) and x.s_agg == 20


def test_S6_X05_t1_two_sell_legs_are_summed_never_capped(jenv):
    pid = make_position(jenv, qty=100)
    g = gtt(jenv, request(100, legs=2))
    _link(jenv, pid, g)
    x = _exposure(jenv)
    assert x.s_agg == 200                                                 # not 100 (OCO not assumed; no cap)


@pytest.mark.parametrize("old_legs,new_legs,expected", [((100, 100), (150, 30), 200), ((100, 50), (120, 120), 240)])
def test_S6_X06_t2_unresolved_modify_takes_the_greater_version(jenv, old_legs, new_legs, expected):
    pid = make_position(jenv, qty=100)
    old = request(old_legs[0], legs=2)
    old["orders"][1]["quantity"] = old_legs[1]
    g = gtt(jenv, old)
    _link(jenv, pid, g)
    new = request(new_legs[0], legs=2, trigger=2920.0)
    new["orders"][1]["quantity"] = new_legs[1]
    row = claim(jenv, pid, "modify", "user_modify", target=g, direction="up", req=new)
    force_state(jenv, row["id"], "unknown", evidence='[{"send": 1, "prestate": %s}]' % __import__("json").dumps(old))
    x = _exposure(jenv)
    assert x.s_agg == expected                                            # never 380/390, never one leg
    force_state(jenv, row["id"], "unknown", evidence="[]")                # the old version cannot be established
    assert _exposure(jenv).s_agg is None


def test_S6_X06b_a_landed_but_unresolved_modify_still_counts_the_old_version(jenv):
    """The listing already shows the requested version (L = 180), the op is still unknown: the old version (200)
    remains a possible version of the GTT, so w = 200."""
    pid = make_position(jenv, qty=100)
    old = request(100, legs=2)
    new = request(150, legs=2, trigger=2920.0)
    new["orders"][1]["quantity"] = 30
    g = gtt(jenv, new)
    _link(jenv, pid, g)
    row = claim(jenv, pid, "modify", "user_modify", target=g, direction="up", req=new)
    force_state(jenv, row["id"], "unknown", evidence='[{"send": 1, "prestate": %s}]' % __import__("json").dumps(old))
    assert _exposure(jenv).s_agg == 200


def test_S6_X07_t3_two_outstanding_triggered_orders_are_summed_by_remaining(jenv):
    pid = make_position(jenv, qty=100)
    req = request(100, legs=2)
    req["orders"][0]["quantity"], req["orders"][1]["quantity"] = 40, 60
    g = gtt(jenv, req)
    _link(jenv, pid, g)
    jenv.kite.trigger_gtt(g, leg=0, status="OPEN", filled_quantity=0)
    o2 = jenv.kite.trigger_gtt(g, leg=1, status="OPEN", filled_quantity=20)   # partially filled: remaining 40
    x = _exposure(jenv)
    assert x.members[0].outstanding and x.s_agg == 40 + 40
    assert o2 in x.members[0].outstanding


def test_S6_X08_terminal_orders_with_reconciled_fills_remove_the_member_under_e(jenv):
    pid = make_position(jenv, qty=100)
    g = gtt(jenv, request(50, legs=2))
    _link(jenv, pid, g)
    o1 = jenv.kite.trigger_gtt(g, leg=0, status="COMPLETE")
    o2 = jenv.kite.trigger_gtt(g, leg=1, status="COMPLETE")
    x = _exposure(jenv)
    assert x.g_max == 1                                                   # fills not reconciled yet: kept
    _exit_fill(jenv, pid, o1, 50)
    _exit_fill(jenv, pid, o2, 50)
    x = _exposure(jenv)
    assert x.g_max == 0 and x.removed[0]["by"] == "e" and x.gate_open()


def test_S6_X09_t5_one_terminal_leg_never_removes_a_multi_leg_gtt(jenv):
    pid = make_position(jenv, qty=100)
    g = gtt(jenv, request(100, legs=2))
    _link(jenv, pid, g)
    o1 = jenv.kite.trigger_gtt(g, leg=0, status="COMPLETE")
    _exit_fill(jenv, pid, o1, 100)
    x = _exposure(jenv)
    assert x.g_max == 1 and x.s_agg == 100 and not x.gate_open()          # the second leg can still execute
    jenv.kite.trigger_gtt(g, leg=1, status="OPEN")
    oid = [o for o in jenv.kite.order_book if o != o1][0]
    jenv.kite.order_book[oid]["status"] = "SOMETHING NEW"                 # B-32: an unknown order status
    assert _exposure(jenv).s_agg is None


def test_S6_X10_t6_an_expected_triggered_order_not_yet_visible_keeps_the_member(jenv):
    pid = make_position(jenv, qty=100)
    g = gtt(jenv, request(50, legs=2))
    _link(jenv, pid, g)
    o1 = jenv.kite.trigger_gtt(g, leg=0, status="COMPLETE")
    _exit_fill(jenv, pid, o1, 50)
    jenv.kite.hide_triggered_order_calls = 5
    jenv.kite.trigger_gtt(g, leg=1, status="COMPLETE")                    # placed, but not yet in orders()
    x = _exposure(jenv)
    m = x.members[0]
    assert x.g_max == 1 and m.t_complete is False and m.w == 50           # counted once at its leg quantity
    assert not m.outstanding.get(o1)                                      # the terminal order is not outstanding


def test_S6_X11_missing_remaining_quantity_is_unknown(jenv):
    pid = make_position(jenv, qty=100)
    g = gtt(jenv, request(100))
    _link(jenv, pid, g)
    oid = jenv.kite.trigger_gtt(g, leg=0, status="OPEN")
    jenv.kite.order_book[oid]["filled_quantity"] = None
    assert _exposure(jenv).s_agg is None


def test_S6_X12_an_omitted_or_paginated_listing_cannot_hide_a_local_member(jenv):
    pid = make_position(jenv)
    req = request(10)
    row = claim(jenv, pid, req=req)
    g = gtt(jenv, req)
    force_state(jenv, row["id"], "confirmed", result_gtt_id=g)
    _link(jenv, pid, g)
    jenv.kite.gtt_listing_omit_ids = {g}                                  # B-08 partial listing (unknowable to us)
    x = _exposure(jenv)
    assert x.g_max == 1 and x.s_agg == 10 and not x.gate_open()           # w from the journal's payload version
    jenv.kite.gtt_listing_omit_ids = set()
    jenv.kite.gtt_listing_limit = 0                                       # a "single page" that shows nothing
    assert not _exposure(jenv).gate_open()


def test_S6_X13_an_unresolved_cancel_removes_nothing(jenv):
    pid = make_position(jenv)
    g = gtt(jenv)
    _link(jenv, pid, g)
    row = claim(jenv, pid, "cancel", "exit_prepare", target=g)
    force_state(jenv, row["id"], "acknowledged")
    x = _exposure(jenv)
    assert x.g_max == 1 and x.s_agg == 10 and 4 in x.members[0].sources
    jenv.kite.gtt_book[g]["status"] = "deleted"
    x = _exposure(jenv)
    assert x.g_max == 0 and x.removed[0]["by"] == "a"


def test_S6_X14_a_confirmed_cancel_contradicted_by_an_active_listing_stays_and_is_reported(jenv):
    pid = make_position(jenv)
    g = gtt(jenv)
    _link(jenv, pid, g)
    row = claim(jenv, pid, "cancel", "exit_prepare", target=g)
    force_state(jenv, row["id"], "confirmed")
    x = _exposure(jenv)
    assert x.g_max == 1 and x.contradictions[0]["code"] == "CONFIRMED_CANCEL_CONTRADICTED"
    del jenv.kite.gtt_book[g]
    assert _exposure(jenv).g_max == 0                                     # (b): confirmed and not listed otherwise


def test_S6_X15_ambiguous_identity_fails_closed(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    force_state(jenv, row["id"], "acknowledged", result_gtt_id=111,
                evidence='[{"response": "acknowledged", "result_gtt_id": "222"}]')
    x = _exposure(jenv)
    assert not x.established and any(r.startswith("AMBIGUOUS_CONFLICTING_IDS") for r in x.reasons)
    assert {"111", "222"} <= {m.key for m in x.members}


def test_S6_X16_no_or_incomplete_read_is_never_established(jenv):
    make_position(jenv)
    conn = conn_of(jenv)
    try:
        x = protection_exposure.evaluate(conn, account_id=account_id(conn), mode="live", exchange="NSE",
                                         tradingsymbol="TCS", evidence=None)
    finally:
        conn.close()
    assert not x.established and not x.gate_open()
    ev = read(jenv, "emergency_exit")
    ev.complete = False
    assert not _exposure(jenv, ev).gate_open()


def test_S6_X17_an_unlinked_gtt_on_the_instrument_blocks_and_holdings_never_cap(jenv):
    make_position(jenv, qty=10)
    gtt(jenv, request(10))                                                 # the user's own GTT
    gtt(jenv, request(10, trigger=2800.0))
    x = _exposure(jenv)
    assert x.g_max == 2 and x.s_agg == 20 and not x.gate_open()


def test_S6_X19_an_unfilled_remainder_is_never_inferred_cancelled(jenv):
    pid = make_position(jenv, qty=50)
    g = gtt(jenv, request(50))
    _link(jenv, pid, g)
    oid = jenv.kite.trigger_gtt(g, leg=0, status="COMPLETE", filled_quantity=30)   # 20 unaccounted for
    _exit_fill(jenv, pid, oid, 30)
    x = _exposure(jenv)
    assert x.g_max == 1 and not x.gate_open()                             # COMPLETE with 20 missing is not proof
    jenv.kite.order_book[oid]["status"] = "CANCELLED"                     # explicitly reported by the broker
    assert _exposure(jenv).gate_open()


def test_S6_X20_triggered_with_no_leg_result_keeps_the_member_though_no_order_is_outstanding(jenv):
    """O(m) is empty (no order is visible as outstanding) but which leg fired cannot be established, so T(m) is not
    complete: every SELL leg counts and the member stays (R6: an empty O(m) is never proof)."""
    pid = make_position(jenv, qty=50)
    g = gtt(jenv, request(50, legs=2))
    _link(jenv, pid, g)
    oid = jenv.kite.trigger_gtt(g, leg=0, status="COMPLETE", expose_result=False)
    _exit_fill(jenv, pid, oid, 50)
    x = _exposure(jenv)
    assert x.g_max == 1 and x.members[0].outstanding == {} and x.members[0].w == 100 and not x.gate_open()


def test_S6_X21_a_gtt_deleted_after_it_triggered_keeps_its_outstanding_order(jenv):
    """Deleting a GTT does not cancel an order it already placed: (a) removes the member only when every order
    it triggered is terminal with nothing left and reconciled ((e) item 4: items 1-3, 5, 6 still apply)."""
    pid = make_position(jenv, qty=50)
    g = gtt(jenv, request(50, legs=2))
    _link(jenv, pid, g)
    oid = jenv.kite.trigger_gtt(g, leg=0, status="OPEN", filled_quantity=10)
    jenv.kite.gtt_book[g]["status"] = "deleted"
    x = _exposure(jenv)
    assert x.g_max == 1 and x.members[0].w == 40 and not x.gate_open()      # the untriggered leg is dead; 40 left
    jenv.kite.order_book[oid].update(status="COMPLETE", filled_quantity=50)
    _exit_fill(jenv, pid, oid, 50)
    x = _exposure(jenv)
    assert x.g_max == 0 and x.removed[0]["by"] == "a"


def test_S6_X18_a_flat_safe_instrument_opens_the_gate(jenv):
    make_position(jenv, qty=10)
    x = _exposure(jenv)
    assert x.established and x.g_max == 0 and x.s_agg == 0 and x.gate_open()
