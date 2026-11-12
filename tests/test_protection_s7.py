"""Phase 3 S7: the Emergency Exit engine API (backend/emergency_exit_engine.py) on env's tmp database with FakeKite.
Not wired into routes/kill_switch. A FakeKite result says nothing about Kite (B-04, B-28, B-31 stay unverified)."""

import pathlib
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from kiteconnect.exceptions import InputException, NetworkException

from backend import config, emergency_exit_engine as ee, instance_lock, protection, protection_send
from backend.protection_send import Outcome, SendContext
from protection_helpers import claim, conn_of, force_state, gtt, journal, make_position, op, request

GRACE = config.PROTECTION_INFLIGHT_GRACE_SECONDS
EXCLUSIVE = lambda ev: "exclusive"  # noqa: E731 - attribution is Phase 7; the default hook is tested in E30


class Clock:
    def __init__(self):
        self.offset = 0.0

    def mono(self):
        return time.monotonic() + self.offset

    def sleep(self, s):
        self.offset += max(0.0, s)

    def wall(self):
        return datetime.now(timezone.utc) + timedelta(seconds=self.offset)


@pytest.fixture
def jenv(env):
    journal(env)
    ee._single_flight.clear()
    return env


def _ee(env, pid, clock=None, **kw):
    clock = clock or Clock()
    kw.setdefault("attribution", EXCLUSIVE)
    conn = conn_of(env)
    try:
        return ee.emergency_exit(conn, pid, env.kite, clock=clock.wall, monotonic=clock.mono, sleep=clock.sleep, **kw)
    finally:
        conn.close()


def _linked(env, qty=10, legs=1, symbol="TCS", token=2953217):
    pid = make_position(env, qty=qty, symbol=symbol, token=token)
    g = gtt(env, request(qty, legs=legs, symbol=symbol))
    conn = conn_of(env)
    try:
        conn.execute("UPDATE trade_tickets SET kite_gtt_id = ? WHERE id = ?", (g, pid))
        conn.commit()
    finally:
        conn.close()
    return pid, g


def _sells(env):
    return [c for c in env.kite.calls_to("place_order") if c["transaction_type"] == "SELL"]


def _exit_row(env, exit_id):
    conn = conn_of(env)
    try:
        return dict(conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (exit_id,)).fetchone())
    finally:
        conn.close()


# ---------------------------------------------------------------- the safe path
def test_S7_E01_fully_safe_exit_cancels_waits_reads_once_then_sells(jenv):
    pid, g = _linked(jenv)
    res = _ee(jenv, pid)
    assert res.outcome == ee.SOLD and res.order_id
    assert [c["target"] for c in res.cancels] == [g] and jenv.kite.gtt_book[g]["status"] == "deleted"
    assert res.waited_seconds >= GRACE                                 # acknowledged is not definite: full grace
    assert len(_sells(jenv)) == 1 and _sells(jenv)[0]["quantity"] == 10
    row = _exit_row(jenv, res.exit_ticket_id)
    assert row["submission_state"] == "acknowledged" and row["kite_order_id"] == res.order_id
    conn = conn_of(jenv)
    try:
        assert conn.execute("SELECT COUNT(*) FROM reconciliation_log WHERE issue_code = ?",
                            (ee.POSSIBLE_DOUBLE_SELL_WATCH,)).fetchone()[0] == 1
        cancel = protection.ops_for_position(conn, pid)[0]
        assert cancel["reason"] == "exit_prepare" and cancel["state"] == "confirmed"
        assert conn.execute("SELECT COUNT(*) FROM broker_reads WHERE purpose = 'emergency_exit'").fetchone()[0] == 1
    finally:
        conn.close()


def test_S7_E02_no_cancel_sent_means_no_wait(jenv):
    pid = make_position(jenv)
    res = _ee(jenv, pid)
    assert res.outcome == ee.SOLD and res.cancels == [] and res.waited_seconds == 0.0


def _acked_result(env, pid, gid):
    """Test setup: an earlier create of this position whose response returned `gid` (app-proven, M-7 step 3)."""
    row = claim(env, pid, "cancel", "user_cancel", target=99999)
    conn = conn_of(env)
    try:
        conn.execute("UPDATE protection_operations SET kind = 'create', reason = 'fill', target_gtt_id = NULL, "
                     "result_gtt_id = ?, state = 'confirmed', sent_at = datetime('now'), attempts = 1, "
                     "resolved_at = datetime('now'), evidence = ? WHERE id = ?",
                     (gid, '[{"response": "acknowledged", "result_gtt_id": "%s"}]' % gid, row["id"]))
        conn.commit()
    finally:
        conn.close()


def test_S7_E03_multiple_and_multi_leg_gtts_are_cancelled_together(jenv):
    pid, g1 = _linked(jenv, legs=2)
    g2 = gtt(jenv, request(10))
    _acked_result(jenv, pid, g2)
    res = _ee(jenv, pid)
    assert sorted(c["target"] for c in res.cancels) == sorted([g1, g2])  # both claimed in one transaction
    assert res.outcome == ee.SOLD


# ---------------------------------------------------------------- refusals
def test_S7_E04_a_gtt_still_active_after_the_cancel_refuses(jenv):
    pid, g = _linked(jenv)
    jenv.kite.raise_on_next("delete_gtt", InputException("cannot delete"))
    res = _ee(jenv, pid)
    assert res.outcome == ee.REFUSED and res.reason == "GATE_MEMBERS_REMAIN" and _sells(jenv) == []
    assert str(g) in res.instruction and _exit_row(jenv, res.exit_ticket_id)["status"] == "cancelled"


def test_S7_E05_an_unknown_unlinked_gtt_is_listed_never_cancelled(jenv):
    pid = make_position(jenv)
    users = gtt(jenv, request(10, trigger=2700.0))
    res = _ee(jenv, pid)
    assert res.outcome == ee.REFUSED and res.cancels == [] and jenv.kite.calls_to("delete_gtt") == []
    member = res.exposure["members"][0]
    assert member["member"] == str(users) and 6 in member["sources"] and not member["cancellable"]


def test_S7_E06_an_incomplete_r_ee_refuses_g22(jenv):
    pid, _g = _linked(jenv)
    calls = {"n": 0}
    real = jenv.kite.orders

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:                      # the prewrite read succeeds, R_EE's orders() fails
            raise NetworkException("timeout")
        return real()

    jenv.kite.orders = flaky
    res = _ee(jenv, pid)
    assert res.outcome == ee.REFUSED and res.reason == "G22_R_EE_UNUSABLE" and _sells(jenv) == []


def test_S7_E07_an_acknowledgement_without_deletion_proof_refuses(jenv):
    pid, g = _linked(jenv)
    jenv.kite.delete_gtt = lambda trigger_id: {"trigger_id": trigger_id}      # acknowledged, nothing happened
    res = _ee(jenv, pid)
    assert res.outcome == ee.REFUSED and jenv.kite.gtt_book[g]["status"] == "active" and _sells(jenv) == []


def test_S7_E08_late_cancellation_refuses_then_a_re_click_sends_no_new_cancel(jenv):
    pid, g = _linked(jenv)
    jenv.kite.delete_gtt = lambda trigger_id: {"trigger_id": trigger_id}      # processed later (B-31)
    first = _ee(jenv, pid)
    assert first.outcome == ee.REFUSED
    jenv.kite.gtt_book[g]["status"] = "deleted"                                # the broker processed it after the grace
    second = _ee(jenv, pid)
    assert second.cancels == [] and second.waited_seconds == 0.0               # no new cancel, no new grace
    assert second.outcome == ee.SOLD and len(_sells(jenv)) == 1


@pytest.mark.parametrize("status,filled", [("OPEN", 0), ("OPEN", 4)], ids=["outstanding", "partial_fill"])
def test_S7_E09_a_triggered_order_still_outstanding_refuses(jenv, status, filled):
    pid, g = _linked(jenv)
    jenv.kite.trigger_gtt(g, leg=0, status=status, filled_quantity=filled)
    res = _ee(jenv, pid)
    assert res.outcome == ee.REFUSED and _sells(jenv) == []
    assert res.exposure["members"][0]["outstanding"]


def test_S7_E10_an_expected_but_invisible_triggered_order_refuses(jenv):
    pid, g = _linked(jenv)
    jenv.kite.hide_triggered_order_calls = 10
    jenv.kite.trigger_gtt(g, leg=0, status="COMPLETE")
    res = _ee(jenv, pid)
    assert res.outcome == ee.REFUSED and res.exposure["members"][0]["t_complete"] is False


def test_S7_E11_an_unknown_id_less_create_refuses_g21(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    force_state(jenv, row["id"], "unknown")
    res = _ee(jenv, pid)
    assert res.outcome == ee.REFUSED and f"op:{row['id']}" in res.instruction
    assert res.exposure["s_agg"] is None and jenv.kite.calls_to("delete_gtt") == []
    assert op(jenv, row["id"])["superseded_by_kind"] == "emergency_exit"      # overlay set; state unchanged
    assert op(jenv, row["id"])["state"] == "unknown"


def test_S7_E12_a_confirmed_cancel_contradicted_by_an_active_listing_refuses_with_a_finding(jenv):
    pid, g = _linked(jenv)
    row = claim(jenv, pid, "cancel", "exit_prepare", target=g)
    force_state(jenv, row["id"], "confirmed")
    res = _ee(jenv, pid)
    assert res.outcome == ee.REFUSED and res.findings[0]["code"] == "CONFIRMED_CANCEL_CONTRADICTED"
    conn = conn_of(jenv)
    try:
        assert protection.get_position(conn, pid)["protection_state"] == "PROTECTION_NEEDS_REVIEW"
    finally:
        conn.close()
    assert op(jenv, row["id"])["state"] == "confirmed"                         # terminal op unchanged


def test_S7_E13_preconditions_refuse_before_anything_is_claimed(jenv):
    flat = make_position(jenv, closed=True)
    res = _ee(jenv, flat)
    assert res.outcome == ee.REFUSED and res.exit_ticket_id is None
    conn = conn_of(jenv)
    try:
        assert conn.execute("SELECT COUNT(*) FROM protection_operations").fetchone()[0] == 0
    finally:
        conn.close()


def test_S7_E14_default_attribution_refuses_when_holdings_do_not_show_the_shares(jenv):
    pid = make_position(jenv)
    res = _ee(jenv, pid, attribution=None)
    assert res.outcome == ee.REFUSED and res.reason.startswith("ATTRIBUTION_") and _sells(jenv) == []


# ---------------------------------------------------------------- the grace window
def test_S7_E15_the_grace_starts_at_the_last_cancel_sent_not_at_initiation(jenv):
    pid, _g1 = _linked(jenv)
    _acked_result(jenv, pid, gtt(jenv, request(10)))
    clock = Clock()
    seen = []

    def slow_first(kwargs):
        if not seen:
            clock.offset += 6.0                     # the first delete takes 6 s: the second is sent 6 s later
        seen.append(kwargs["trigger_id"])

    jenv.kite.on_call("delete_gtt", slow_first)
    start = clock.mono()
    res = _ee(jenv, pid, clock)
    assert len(seen) == 2
    assert res.waited_seconds >= GRACE                       # the full grace from the LAST send
    assert clock.mono() - start >= 6.0 + GRACE               # a window from initiation would have been 6 s shorter


def test_S7_E16_a_definite_result_ends_the_wait_early(jenv):
    pid, g = _linked(jenv)
    jenv.kite.raise_on_next("delete_gtt", InputException("refused"))         # rejected: definite
    res = _ee(jenv, pid)
    assert res.waited_seconds < 1.0 and res.outcome == ee.REFUSED


# ---------------------------------------------------------------- concurrency and crashes
def test_S7_E17_concurrent_clicks_produce_at_most_one_sell(jenv):
    pid, _g = _linked(jenv)
    results, barrier = [], threading.Barrier(4)

    def click():
        barrier.wait()
        results.append(_ee(jenv, pid))

    threads = [threading.Thread(target=click) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_sells(jenv)) == 1
    assert sum(r.outcome == ee.SOLD for r in results) == 1
    assert all(r.outcome in (ee.SOLD, ee.REFUSED) for r in results)


def test_S7_E18_another_process_already_owning_the_exit_refuses(jenv):
    pid, _g = _linked(jenv)
    conn = conn_of(jenv)
    try:
        conn.execute("INSERT INTO trade_tickets (account_id, symbol, exchange, instrument_token, transaction_type, quantity, "
                     "entry_price, entry_order_type, stop_loss, target_1, product_type, rationale, status, mode, "
                     "closes_ticket_id) SELECT account_id, symbol, exchange, instrument_token, 'SELL', 10, 3000, 'MARKET', "
                     "2900, 3300, 'CNC', 'other exit', 'confirmed', mode, id FROM trade_tickets WHERE id = ?", (pid,))
        conn.commit()
    finally:
        conn.close()
    res = _ee(jenv, pid)
    assert res.outcome == ee.REFUSED and res.reason == "EXIT_IN_FLIGHT" and _sells(jenv) == []


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize("boundary", ["during_cancel", "after_cancels", "after_read", "during_sell"])
def test_S7_E19_a_crash_at_each_boundary_never_sells_twice(jenv, boundary):
    pid, _g = _linked(jenv)
    kw = {}
    if boundary == "during_cancel":
        jenv.kite.accept_then_raise("delete_gtt", SimulatedCrash())
    elif boundary == "after_cancels":
        kw["after_cancels"] = lambda: (_ for _ in ()).throw(SimulatedCrash())
    elif boundary == "after_read":
        kw["after_read"] = lambda: (_ for _ in ()).throw(SimulatedCrash())
    else:
        jenv.kite.accept_then_raise("place_order", SimulatedCrash())
    with pytest.raises(SimulatedCrash):
        _ee(jenv, pid, **kw)
    assert len(_sells(jenv)) == (1 if boundary == "during_sell" else 0)
    second = _ee(jenv, pid)                                       # a re-click in the same (surviving) process
    assert second.outcome == ee.REFUSED and len(_sells(jenv)) == (1 if boundary == "during_sell" else 0)


def test_S7_E20_ownership_takeover_during_the_sequence_refuses(jenv):
    pid, _g = _linked(jenv)

    def takeover():
        raw = sqlite3.connect(jenv.db.DB_PATH, timeout=0.5)
        raw.execute("UPDATE app_instance SET epoch = epoch + 1, instance_uuid = 'successor' WHERE id = 1")
        raw.commit()
        raw.close()

    res = _ee(jenv, pid, after_cancels=takeover)
    assert res.outcome == ee.REFUSED and res.reason.startswith("AUTHORITY_LOST") and _sells(jenv) == []


def test_S7_E21_broker_timeout_after_accepting_a_cancellation(jenv):
    pid, g = _linked(jenv)
    jenv.kite.accept_then_raise("delete_gtt", NetworkException("timeout"))  # deleted, response lost
    res = _ee(jenv, pid)
    assert res.outcome == ee.SOLD                                         # G10: unknown + explicit deleted
    pid2, g2 = _linked(jenv, symbol="INFY", token=408065)
    jenv.kite.deleted_gtt_listing = "pruned"                              # ... but if deleted GTTs vanish: G12
    jenv.kite.accept_then_raise("delete_gtt", NetworkException("timeout"))
    res2 = _ee(jenv, pid2)
    assert res2.outcome == ee.REFUSED and len(_sells(jenv)) == 1


def test_S7_E22_broker_timeout_after_accepting_a_sell_is_never_resent(jenv):
    pid, _g = _linked(jenv)
    jenv.kite.accept_then_raise("place_order", NetworkException("timeout"))
    res = _ee(jenv, pid)
    assert res.outcome == ee.SELL_UNKNOWN
    assert _exit_row(jenv, res.exit_ticket_id)["submission_state"] == "unknown"
    again = _ee(jenv, pid)
    assert again.outcome == ee.REFUSED and again.reason == "EXIT_IN_FLIGHT" and len(_sells(jenv)) == 1


# ---------------------------------------------------------------- §4.3 late results after EE
def test_S7_E23_late_ack_after_a_refused_exit_links_on_an_open_position(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    force_state(jenv, row["id"], "sending")
    res = _ee(jenv, pid)
    assert res.outcome == ee.REFUSED                                       # sending create: G21
    ctx = SendContext(row["id"], row["op_uuid"], 1, instance_lock.owner_epoch(), "create", row["request_fingerprint"])
    force_state(jenv, row["id"], "unknown")
    conn = conn_of(jenv)
    try:
        protection_send.record_late_response(conn, ctx, Outcome("acknowledged", "9001"))
        assert str(protection.get_position(conn, pid)["kite_gtt_id"]) == "9001"
    finally:
        conn.close()


def test_S7_E24_late_ack_on_a_flat_position_claims_an_automatic_cleanup_cancel(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    force_state(jenv, row["id"], "unknown", superseded_at="t", superseded_by_kind="emergency_exit", superseded_by_ref="1")
    conn = conn_of(jenv)
    try:   # the position became broker-flat by another route
        conn.execute("UPDATE trade_tickets SET closed_at = datetime('now') WHERE id = ?", (pid,))
        conn.execute("INSERT INTO trade_tickets (account_id, symbol, exchange, instrument_token, transaction_type, quantity, "
                     "entry_price, entry_order_type, stop_loss, target_1, product_type, rationale, status, mode, "
                     "closes_ticket_id, filled_quantity, entry_fill_status) SELECT account_id, symbol, exchange, "
                     "instrument_token, 'SELL', 10, 3000, 'MARKET', 2900, 3300, 'CNC', 'x', 'executed', mode, id, 10, "
                     "'filled' FROM trade_tickets WHERE id = ?", (pid,))
        conn.commit()
        ctx = SendContext(row["id"], row["op_uuid"], 1, instance_lock.owner_epoch(), "create", row["request_fingerprint"])
        protection_send.record_late_response(conn, ctx, Outcome("acknowledged", "9002"))
        cancels = [o for o in protection.ops_for_position(conn, pid) if o["kind"] == "cancel"]
    finally:
        conn.close()
    assert len(cancels) == 1 and cancels[0]["reason"] == "supersede_cleanup" and str(cancels[0]["target_gtt_id"]) == "9002"


def test_S7_E26_a_gtt_deleted_after_trigger_with_an_open_order_refuses(jenv):
    pid, g = _linked(jenv)
    jenv.kite.trigger_gtt(g, leg=0, status="OPEN")
    jenv.kite.gtt_book[g]["status"] = "deleted"
    res = _ee(jenv, pid)
    assert res.outcome == ee.REFUSED and _sells(jenv) == []


def test_S7_E27_g5_acknowledged_cancel_with_the_gtt_missing_from_the_listing_refuses(jenv):
    pid, g = _linked(jenv)
    jenv.kite.deleted_gtt_listing = "pruned"            # deleted GTTs vanish: "missing" is never "gone" (B-28)
    res = _ee(jenv, pid)
    assert res.outcome == ee.REFUSED and jenv.kite.gtt_book[g]["status"] == "deleted" and _sells(jenv) == []
    conn = conn_of(jenv)
    try:
        assert protection.ops_for_position(conn, pid)[0]["state"] == "acknowledged"
    finally:
        conn.close()


def test_S7_E28_a_foreign_open_sell_order_on_the_instrument_refuses(jenv):
    pid = make_position(jenv)
    jenv.kite._new_order(jenv.kite.VARIETY_REGULAR, "NSE", "TCS", "SELL", 5, "CNC", "LIMIT", 3100.0, "OPEN", 0, 0)
    res = _ee(jenv, pid)
    assert res.outcome == ee.REFUSED and res.reason.startswith("E3:OUTSTANDING_SELL_ORDER") and _sells(jenv) == []


# ---------------------------------------------------------------- static: no override, no deferred sale
def test_S7_E25_no_sell_anyway_override_and_no_deferred_sale_path_exists():
    src = pathlib.Path(ee.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", src, flags=re.S)                       # docstrings may name what is absent
    for forbidden in ("override", "sell_anyway", "force", "create_task", "Timer(", "schedule", "defer", "retry("):
        assert forbidden not in code, forbidden
    import inspect
    params = set(inspect.signature(ee.emergency_exit).parameters)
    assert not params & {"force", "override", "sell_anyway", "defer", "when_clear"}
