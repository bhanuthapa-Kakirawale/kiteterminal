"""Phase 3 S4: the protection journal and its state machine (backend/protection.py), on env's tmp database with
the dark schema (never the account database). FakeKite only. These tests say nothing about Kite."""

import ast
import json
import pathlib
import re
import sqlite3
import threading

import pytest

from backend import config, db, instance_lock, protection
from backend.protection import (
    ALLOWED_TRANSITIONS, CANCEL, CREATE, MODIFY, OPEN_STATES, STATES, TERMINAL_STATES, ClaimRefused,
    ProtectionRefused, TransitionRefused,
)
from protection_helpers import (
    claim, conn_of, events, force_state, gtt, journal, make_position, op, read, request,
)

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"


@pytest.fixture
def jenv(env):
    journal(env)
    return env


# ---------------------------------------------------------------- the transition table
def _setup_for(state, kind):
    cols = {}
    if kind == CREATE and state in ("acknowledged", "confirmed", "adopted"):
        cols["result_gtt_id"] = 777
    if state == "absent_after_settle":
        cols["complete_reads_after"] = 2
    return cols


def _sets_for(to, kind, env, op_row):
    sets = {}
    if to == "sending":
        sets.update(sent_at="2026-11-11T10:00:00+00:00", attempts=(op_row["attempts"] or 0) + 1)
    if to in ("acknowledged", "confirmed", "adopted") and kind == CREATE:
        sets["result_gtt_id"] = 778
    if to == "absent_after_settle":
        sets["complete_reads_after"] = 2
    if to == "resolved_manual":
        conn = conn_of(env)
        try:
            rid = conn.execute("INSERT INTO broker_reads (account_id, purpose, instance_epoch, started_at, complete, "
                               "gtts_malformed) VALUES (1, 'resolution', 0, '2026-11-11T10:00:00+00:00', 1, 0)").lastrowid
            res = conn.execute("INSERT INTO protection_operation_resolutions (op_id, outcome, reason, evidence_read_id, "
                               "resolver, instance_epoch) VALUES (?, 'no_app_gtt', 'r', ?, 'u', 0)",
                               (op_row["id"], rid)).lastrowid
            conn.commit()
        finally:
            conn.close()
        sets["resolution_id"] = res
    return sets


def _kind_for(frm, to):
    if to == "absent_after_settle" or to == "adopted":
        return CREATE
    if frm == "unknown" and to in ("confirmed", "sending"):   # a create may never be sent twice (retry CHECK)
        return CANCEL
    return CREATE


def _fresh_op(env, kind):
    pid = make_position(env)
    if kind == CANCEL:
        return claim(env, pid, CANCEL, "user_cancel", target=999)
    return claim(env, pid)


PAIRS = [(f, t) for f in STATES for t in STATES if f != t]


@pytest.mark.parametrize("frm,to", PAIRS, ids=[f"{f}->{t}" for f, t in PAIRS])
def test_S4_T01_every_pair_outside_the_table_is_refused_and_every_pair_inside_succeeds(jenv, frm, to):
    kind = _kind_for(frm, to)
    row = _fresh_op(jenv, kind)
    if frm != "intent":
        cols = _setup_for(frm, kind)
        if frm == "resolved_manual":
            cols.update(_sets_for("resolved_manual", kind, jenv, row))
        force_state(jenv, row["id"], frm, **cols)
    row = op(jenv, row["id"])
    before_events = len(events(jenv, row["position_id"]))
    conn = conn_of(jenv)
    try:
        if (frm, to) in ALLOWED_TRANSITIONS:
            sets = _sets_for(to, kind, jenv, row)
            with protection.journal_transaction(conn):
                after = protection.transition(conn, row, to, from_states=(frm,), sets=sets)
            assert after["state"] == to
            assert len(events(jenv, row["position_id"])) == before_events + 1      # exactly one event (DD-7)
            assert (after["resolved_at"] is None) == (to in OPEN_STATES)
        else:
            with pytest.raises(TransitionRefused) as e:
                with protection.journal_transaction(conn):
                    protection.transition(conn, row, to, from_states=(frm,))
            assert e.value.code == "ILLEGAL_TRANSITION"
            assert op(jenv, row["id"])["state"] == frm                          # nothing written
            assert len(events(jenv, row["position_id"])) == before_events
    finally:
        conn.close()


def test_S4_T02_terminal_states_have_no_outgoing_transition():
    for (frm, _to) in ALLOWED_TRANSITIONS:
        assert frm not in TERMINAL_STATES
    assert set(TERMINAL_STATES) == {"confirmed", "adopted", "rejected", "not_sent", "absent_after_settle",
                                    "resolved_manual"}
    # never allowed (§4.2): unknown->sending only for cancel and modify down/tighten is enforced at the send
    # (protection_send); here the pair itself exists, and create/up/loosen retries are refused by the CHECK.
    assert ("unknown", "sending") in ALLOWED_TRANSITIONS


def test_S4_T03_a_lost_race_is_refused_with_nothing_written(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    stale = dict(row)
    force_state(jenv, row["id"], "not_sent")
    conn = conn_of(jenv)
    try:
        with pytest.raises(TransitionRefused) as e:
            with protection.journal_transaction(conn):
                protection.transition(conn, stale, "sending", from_states=("intent",),
                                      sets={"sent_at": "x", "attempts": 1})
        assert e.value.code == "CAS_FAILED"
    finally:
        conn.close()
    assert op(jenv, row["id"])["state"] == "not_sent"


def test_S4_T04_the_attempt_key_refuses_a_stale_attempt(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid, CANCEL, "user_cancel", target=55)
    force_state(jenv, row["id"], "sending", attempts=2)
    conn = conn_of(jenv)
    try:
        with pytest.raises(TransitionRefused):
            with protection.journal_transaction(conn):
                protection.transition(conn, op(jenv, row["id"]), "unknown", from_states=("sending",), attempt=1)
        with protection.journal_transaction(conn):
            assert protection.transition(conn, op(jenv, row["id"]), "unknown", from_states=("sending",),
                                         attempt=2)["state"] == "unknown"
    finally:
        conn.close()


# ---------------------------------------------------------------- claims
def test_S4_T05_claim_copies_account_and_mode_verbatim(jenv):
    for mode in ("live", "dry_run"):
        pid = make_position(jenv, mode=mode)
        row = claim(jenv, pid)
        assert row["mode"] == mode and row["state"] == "intent" and row["attempts"] == 0
        assert row["instance_epoch"] == instance_lock.owner_epoch()
        assert events(jenv, pid)[-1][0] == "protection_pending"


def test_S4_T06_a_null_mode_ticket_is_never_journaled(jenv):
    pid = make_position(jenv)
    conn = conn_of(jenv)
    try:
        conn.execute("UPDATE trade_tickets SET mode = NULL WHERE id = ?", (pid,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ClaimRefused) as e:
        claim(jenv, pid, CANCEL, "user_cancel", target=5)
    assert e.value.code == "MODE_NULL"
    conn = conn_of(jenv)
    try:   # the DDL cannot even store 'LIVE' or 'paper'
        for bad in ("LIVE", "paper"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE trade_tickets SET mode = ? WHERE id = ?", (bad, pid))
    finally:
        conn.close()


def test_S4_T07_duplicate_change_claims_are_refused_cancels_partition_by_target(jenv):
    pid = make_position(jenv)
    claim(jenv, pid)
    with pytest.raises(ClaimRefused):            # M-5: an open op exists
        claim(jenv, pid)
    with pytest.raises(sqlite3.IntegrityError):  # the partition index refuses a second open change
        claim(jenv, pid, MODIFY, "user_modify", target=5, direction="tighten", req=request(10, trigger=2920.0))
    a = claim(jenv, pid, CANCEL, "user_cancel", target=5)       # a cancel coexists with the unresolved create...
    b = claim(jenv, pid, CANCEL, "user_cancel", target=6)       # ...and with a cancel of another target
    assert a["target_gtt_id"] == 5 and b["target_gtt_id"] == 6
    with pytest.raises(sqlite3.IntegrityError):                 # two open cancels of the SAME target
        claim(jenv, pid, CANCEL, "user_cancel", target=5)


def test_S4_T08_needs_review_blocks_every_new_change(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    force_state(jenv, row["id"], "needs_review")
    with pytest.raises((ClaimRefused, sqlite3.IntegrityError)):
        claim(jenv, pid, MODIFY, "user_modify", target=5, direction="tighten", req=request(10, trigger=2920.0))
    conn = conn_of(jenv)
    try:
        assert protection.derive_protection(conn, pid)["state"] == "PROTECTION_NEEDS_REVIEW"
    finally:
        conn.close()


def test_S4_T09_concurrent_claims_from_many_threads_produce_exactly_one_op(jenv):
    pid = make_position(jenv)
    evidence = read(jenv)
    results, barrier = [], threading.Barrier(8)

    def worker():
        conn = conn_of(jenv)
        try:
            barrier.wait()
            with protection.journal_transaction(conn):
                protection.claim(conn, position_id=pid, kind=CREATE, reason="fill", request=request(),
                                 evidence=evidence, may_increase_exposure=True)
            results.append("ok")
        except (ClaimRefused, sqlite3.IntegrityError, sqlite3.OperationalError) as e:
            results.append(type(e).__name__)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1, results
    conn = conn_of(jenv)
    try:
        assert conn.execute("SELECT COUNT(*) FROM protection_operations WHERE position_id = ?", (pid,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_S4_T10_an_exception_inside_the_transaction_rolls_back_the_claim_and_its_event(jenv):
    pid = make_position(jenv)
    evidence = read(jenv)
    conn = conn_of(jenv)
    try:
        with pytest.raises(RuntimeError):
            with protection.journal_transaction(conn):
                protection.claim(conn, position_id=pid, kind=CREATE, reason="fill", request=request(),
                                 evidence=evidence, may_increase_exposure=True)
                raise RuntimeError("crash after the claim, before commit")
        assert conn.execute("SELECT COUNT(*) FROM protection_operations").fetchone()[0] == 0
    finally:
        conn.close()
    assert events(jenv, pid) == []


def test_S4_T11_a_check_violation_mid_transition_writes_nothing(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    conn = conn_of(jenv)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with protection.journal_transaction(conn):   # sending without sent_at: the sent-state CHECK
                protection.transition(conn, row, "sending", from_states=("intent",), sets={"attempts": 1})
    finally:
        conn.close()
    assert op(jenv, row["id"])["state"] == "intent"
    assert [e[0] for e in events(jenv, pid)] == ["protection_pending"]


def test_S4_T12_missing_or_incomplete_broker_evidence_refuses_a_create(jenv):
    pid = make_position(jenv)
    conn = conn_of(jenv)
    try:
        with pytest.raises(ClaimRefused) as e:
            with protection.journal_transaction(conn):
                protection.claim(conn, position_id=pid, kind=CREATE, reason="fill", request=request(), evidence=None,
                                 may_increase_exposure=True)
        assert e.value.code == "M5_NO_COMPLETE_READ"
        incomplete = read(jenv)
        incomplete.complete = False
        with pytest.raises(ClaimRefused) as e:
            with protection.journal_transaction(conn):
                protection.claim(conn, position_id=pid, kind=CREATE, reason="fill", request=request(),
                                 evidence=incomplete, may_increase_exposure=True)
        assert e.value.code == "M5_NO_COMPLETE_READ"
    finally:
        conn.close()


def test_S4_T13_an_unlinked_sell_gtt_on_the_instrument_refuses_a_create(jenv):
    pid = make_position(jenv)
    gtt(jenv)                                             # someone's SELL GTT on TCS, linked to nothing
    with pytest.raises(ClaimRefused) as e:
        claim(jenv, pid)
    assert e.value.code == "M5_UNLINKED_SELL_GTT_ON_INSTRUMENT"


@pytest.mark.parametrize("last_state,reason,code", [
    ("rejected", "fill", "M5_USER_RECREATE_REQUIRED"),
    ("absent_after_settle", "fill", "M5_ABSENT_AFTER_SETTLE_WATCH"),
    ("unknown", "fill", "M5_OPEN_OP_EXISTS"),
])
def test_S4_T14_m5_last_create_rules(jenv, last_state, reason, code):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    extra = {"complete_reads_after": 2} if last_state == "absent_after_settle" else {}
    force_state(jenv, row["id"], last_state, **extra)
    with pytest.raises(ClaimRefused) as e:
        claim(jenv, pid, reason=reason)
    assert e.value.code == code


def test_S4_T15_a_not_sent_create_allows_an_automatic_new_create_unless_superseded(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    force_state(jenv, row["id"], "not_sent")
    assert claim(jenv, pid)["state"] == "intent"
    pid2 = make_position(jenv, symbol="INFY", token=408065)
    row2 = claim(jenv, pid2, req=request(symbol="INFY"))
    force_state(jenv, row2["id"], "not_sent", superseded_at="t", superseded_by_kind="exit", superseded_by_ref="9")
    with pytest.raises(ClaimRefused) as e:
        claim(jenv, pid2, req=request(symbol="INFY"))
    assert e.value.code == "M5_LAST_CREATE_SUPERSEDED"


def test_S4_T16_an_exposure_increasing_claim_needs_dd9(jenv):
    pid = make_position(jenv)
    dx = protection.classify(kind=CREATE, reason="fill", q0=0, q1=10, open_qty=10, loosen=False)
    assert dx.cls == protection.DX_INCREASES
    with pytest.raises(ClaimRefused) as e:
        claim(jenv, pid, dx=dx, may_increase=False)
    assert e.value.code == "DD9_REFUSED"


def test_S4_T17_automatic_claims_follow_the_effect_class(jenv):
    pid = make_position(jenv)
    reduces = protection.classify(kind=CANCEL, reason="exit_prepare", q0=10, q1=0, open_qty=10, loosen=False)
    assert reduces.cls == protection.DX_REDUCES
    with pytest.raises(ClaimRefused) as e:
        claim(jenv, pid, CANCEL, "exit_prepare", target=5, automatic=True, dx=reduces)
    assert e.value.code == "CANCEL_NOT_AUTOMATIC"
    with pytest.raises(ClaimRefused) as e:
        claim(jenv, pid, CANCEL, "user_cancel", target=5, automatic=True, dx=reduces)
    assert e.value.code == "USER_REASON_NOT_AUTOMATIC"
    flat = make_position(jenv, symbol="INFY", token=408065, closed=True)
    none = protection.classify(kind=CANCEL, reason="position_closed", q0=10, q1=0, open_qty=0, loosen=False)
    assert none.cls == protection.DX_NONE
    assert claim(jenv, flat, CANCEL, "position_closed", target=5, automatic=True, dx=none)["state"] == "intent"


def test_S4_T18_kind_reason_direction_matrix(jenv):
    pid = make_position(jenv)
    for kind, reasons in protection.REASONS.items():
        for reason in [r for rs in protection.REASONS.values() for r in rs]:
            for direction in (None, "up", "down", "tighten", "loosen"):
                ok = reason in reasons and (kind == MODIFY) == (direction is not None) and \
                    protection.FIXED_DIRECTION.get(reason, direction) == direction
                if not ok:
                    with pytest.raises(ClaimRefused):
                        protection._validate_shape(kind, reason, direction)
                else:
                    protection._validate_shape(kind, reason, direction)
    conn = conn_of(jenv)   # the DDL refuses a mismatch even if the Python check were bypassed
    try:
        row = claim(jenv, pid, CANCEL, "user_cancel", target=5)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE protection_operations SET reason = 'fill' WHERE id = ?", (row["id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE protection_operations SET attempts = 2, kind = 'create', reason = 'fill', "
                         "target_gtt_id = NULL WHERE id = ?", (row["id"],))
    finally:
        conn.close()


def test_S4_T19_a_target_linked_to_another_ticket_is_refused(jenv):
    a = make_position(jenv, gtt_id=41)
    b = make_position(jenv, symbol="INFY", token=408065)
    with pytest.raises(ClaimRefused) as e:
        claim(jenv, b, CANCEL, "user_cancel", target=41)
    assert e.value.code == "TARGET_LINKED_ELSEWHERE"
    assert claim(jenv, a, CANCEL, "user_cancel", target=41)["target_gtt_id"] == 41


def test_S4_T20_link_guard_is_independent_of_the_unique_index(jenv):
    a = make_position(jenv, gtt_id=50)
    b = make_position(jenv, symbol="INFY", token=408065)
    conn = conn_of(jenv)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_trade_tickets_unique_gtt")
        conn.commit()
        with protection.journal_transaction(conn):
            assert protection.link_gtt(conn, b, 50) is False     # DD-6 engine guard: linked elsewhere
            assert protection.link_gtt(conn, b, 51) is True
    finally:
        conn.close()
    assert a


# ---------------------------------------------------------------- fencing of the journal
def test_S4_T21_no_journal_write_outside_a_fenced_transaction(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    conn = conn_of(jenv)
    try:
        with pytest.raises(ProtectionRefused) as e:
            protection.transition(conn, row, "not_sent", from_states=("intent",))
        assert e.value.code == "UNFENCED_JOURNAL_WRITE"
        instance_lock.reset_for_tests()                     # no authority at all
        with pytest.raises(instance_lock.FencingError):
            with protection.journal_transaction(conn):
                pass
    finally:
        conn.close()


@pytest.mark.v11_database   # a v11 database without the journal (on a v12 database migration 12 created it)
def test_S4_T22_journal_refused_without_the_dark_schema(env):
    conn = conn_of(env)
    try:
        with pytest.raises(ProtectionRefused) as e:
            with protection.journal_transaction(conn):
                pass
        assert e.value.code == "JOURNAL_ABSENT"
    finally:
        conn.close()


def test_S4_T23_the_journal_registration_state_is_the_pinned_one():
    import schema_builds   # the one pin: unregistered (v11) until the separately authorized registration diff
    assert db.MIGRATIONS[-1][0] == schema_builds.expected_latest()
    assert db.LATEST_SCHEMA_VERSION == schema_builds.expected_latest()


# ---------------------------------------------------------------- DX (rev. 3 decision table)
ALL7 = {c: True for c in protection.CAP_CONDITIONS}


@pytest.mark.parametrize("n,q0,q_cap,cls", [
    (100, 120, 100, protection.DX_CAPS),
    (100, 120, 80, protection.DX_CAPS),
    (100, 100, 100, protection.DX_NONE),
    (100, 80, 100, protection.DX_INCREASES),
])
def test_S4_T24_dx_rev3_mandatory_table(n, q0, q_cap, cls):
    reason = "attribution_cap"
    got = protection.classify(kind=MODIFY, reason=reason, q0=q0, q1=q_cap, open_qty=n, loosen=False,
                              cap_conditions=ALL7)
    assert got.cls == cls


@pytest.mark.parametrize("missing", protection.CAP_CONDITIONS)
def test_S4_T25_each_cap_condition_failing_alone_makes_it_reduces_protection(missing):
    conds = dict(ALL7, **{missing: False})
    got = protection.classify(kind=MODIFY, reason="attribution_cap", q0=120, q1=100, open_qty=100, loosen=False,
                              cap_conditions=conds)
    assert got.cls == protection.DX_REDUCES   # never `none`, even though coverage is kept (R3-1)


def test_S4_T26_dx_other_rows():
    c = protection.classify
    assert c(kind=CANCEL, reason="position_closed", q0=10, q1=0, open_qty=0, loosen=False).cls == protection.DX_NONE
    assert c(kind=CANCEL, reason="exit_prepare", q0=10, q1=0, open_qty=10, loosen=False).cls == protection.DX_REDUCES
    assert c(kind=MODIFY, reason="exit_fill", q0=120, q1=100, open_qty=100, loosen=False).cls == protection.DX_NONE
    assert c(kind=MODIFY, reason="trailing", q0=100, q1=100, open_qty=100, loosen=False).cls == protection.DX_NONE
    assert c(kind=MODIFY, reason="user_modify", q0=100, q1=100, open_qty=100, loosen=True).cls == protection.DX_REDUCES
    assert c(kind=MODIFY, reason="exit_fill", q0=100, q1=80, open_qty=100, loosen=False).cls == protection.DX_REDUCES
    m2 = {k: True for k in protection.M2_CONDITIONS}
    assert c(kind=CANCEL, reason="duplicate_cleanup", q0=10, q1=0, open_qty=10, loosen=False,
             m2_conditions=m2).cls == protection.DX_REDUNDANT
    for k in protection.M2_CONDITIONS:
        assert c(kind=CANCEL, reason="duplicate_cleanup", q0=10, q1=0, open_qty=10, loosen=False,
                 m2_conditions=dict(m2, **{k: False})).cls == protection.DX_REDUCES


def test_S4_T27_direction_is_the_riskiest_component():
    base = request(10, trigger=2910.0)
    assert protection.direction_of(base, request(12, trigger=2920.0)) == "up"      # up beats tighten
    assert protection.direction_of(base, request(8, trigger=2900.0)) == "loosen"   # loosen beats down
    assert protection.direction_of(base, request(8, trigger=2910.0)) == "down"
    assert protection.direction_of(base, request(10, trigger=2920.0, price=2910.0)) == "tighten"


# ---------------------------------------------------------------- derived protection state
def _confirm_create(env, pid, req):
    row = claim(env, pid, req=req)
    gid = gtt(env, req)                                      # placed after the claim's read
    force_state(env, row["id"], "confirmed", result_gtt_id=gid)
    conn = conn_of(env)
    try:
        conn.execute("UPDATE trade_tickets SET kite_gtt_id = ? WHERE id = ?", (gid, pid))
        conn.commit()
    finally:
        conn.close()
    return gid


def test_S4_T28_confirmed_only_with_the_full_shape_in_a_fresh_complete_read(jenv):
    pid = make_position(jenv)
    req = request(10)
    gid = _confirm_create(jenv, pid, req)
    conn = conn_of(jenv)
    try:
        ev = read(jenv)
        assert protection.derive_protection(conn, pid, ev)["state"] == "PROTECTION_CONFIRMED"
        assert protection.derive_protection(conn, pid, None)["state"] == "PROTECTION_STALE"
        jenv.kite.gtt_book[gid]["orders"][0]["quantity"] = 8
        assert protection.derive_protection(conn, pid, read(jenv))["state"] == "PROTECTION_RESIZE_PENDING"
        jenv.kite.gtt_book[gid]["orders"][0]["quantity"] = 10
        jenv.kite.gtt_book[gid]["condition"]["trigger_values"] = [2800.0]
        assert protection.derive_protection(conn, pid, read(jenv))["state"] == "PROTECTION_MISMATCH"
        jenv.kite.gtt_book[gid]["status"] = "disabled"
        assert protection.derive_protection(conn, pid, read(jenv))["state"] == "PROTECTION_NEEDS_REVIEW"
        del jenv.kite.gtt_book[gid]
        assert protection.derive_protection(conn, pid, read(jenv))["state"] == "PROTECTION_UNKNOWN"   # never unlinked
        assert protection.get_position(conn, pid)["kite_gtt_id"] == gid
    finally:
        conn.close()


def test_S4_T29_closed_cleanup_required_is_derived_never_stored(jenv):
    flat = make_position(jenv, closed=True, gtt_id=61)
    conn = conn_of(jenv)
    try:
        d = protection.derive_protection(conn, flat, read(jenv))
        assert d["cleanup_required"] is True and d["reason"] == "CLOSED_CLEANUP_REQUIRED"
        assert "CLOSED_CLEANUP_REQUIRED" not in db.PROTECTION_STATES
    finally:
        conn.close()
    row = claim(jenv, flat, CANCEL, "position_closed", target=61)
    force_state(jenv, row["id"], "confirmed")
    conn = conn_of(jenv)
    try:
        d = protection.derive_protection(conn, flat, read(jenv))
        assert d["cleanup_required"] is False and d["state"] == "PROTECTION_NOT_REQUIRED"
    finally:
        conn.close()


# ---------------------------------------------------------------- manual resolution (Q3, R2-6)
def _review_op(env, pid, kind=CREATE, **kw):
    row = claim(env, pid, kind, **kw)
    force_state(env, row["id"], "needs_review")
    return op(env, row["id"])


def _resolve(env, op_id, outcome, linked=None, purpose="resolution", evidence=None):
    conn = conn_of(env)
    try:
        ev = evidence if evidence is not None else read(env, purpose)
        with protection.journal_transaction(conn):
            return protection.resolve_manual(conn, op_id, outcome=outcome, reason="checked in Kite",
                                             resolver="operator", evidence=ev, linked_gtt_id=linked)
    finally:
        conn.close()


def test_S4_T30_resolution_refusals(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    force_state(jenv, row["id"], "unknown")
    with pytest.raises(TransitionRefused):                       # only from needs_review
        _resolve(jenv, row["id"], "no_app_gtt")
    force_state(jenv, row["id"], "needs_review")
    with pytest.raises(ProtectionRefused) as e:                  # needs a read taken for the resolution
        _resolve(jenv, row["id"], "no_app_gtt", purpose="prewrite")
    assert e.value.code == "RESOLUTION_READ_REQUIRED"
    with pytest.raises(ProtectionRefused) as e:
        _resolve(jenv, row["id"], "linked_gtt")                  # linked_gtt without an id
    assert e.value.code == "RESOLUTION_LINK_SHAPE"
    with pytest.raises(ProtectionRefused) as e:
        _resolve(jenv, row["id"], "no_app_gtt", linked=5)        # an id with another outcome
    assert e.value.code == "RESOLUTION_LINK_SHAPE"
    with pytest.raises(ProtectionRefused) as e:
        _resolve(jenv, row["id"], "linked_gtt", linked=12345)    # not active in the read
    assert e.value.code == "RESOLUTION_LINK_NOT_ACTIVE"
    stale = read(jenv, "resolution")
    from datetime import timedelta
    stale.started_at = stale.started_at - timedelta(seconds=config.PREWRITE_MAX_AGE_SECONDS + 5)
    with pytest.raises(ProtectionRefused) as e:
        _resolve(jenv, row["id"], "no_app_gtt", evidence=stale)
    assert e.value.code == "RESOLUTION_READ_UNUSABLE"
    _resolve(jenv, row["id"], "no_app_gtt")
    with pytest.raises(TransitionRefused):                       # a second resolution of the same op
        _resolve(jenv, row["id"], "no_app_gtt")


def test_S4_T31_no_outcome_yields_confirmed_on_a_mismatch_and_no_gtt_is_failed(jenv):
    pid = make_position(jenv)
    row = _review_op(jenv, pid)
    wrong = gtt(jenv, request(8))                               # active, right instrument, wrong quantity
    out = _resolve(jenv, row["id"], "linked_gtt", linked=wrong)
    assert out["op"]["state"] == "resolved_manual"
    assert out["protection"]["state"] == "PROTECTION_RESIZE_PENDING"      # never CONFIRMED by the outcome
    pid2 = make_position(jenv, symbol="INFY", token=408065)
    row2 = _review_op(jenv, pid2, req=request(symbol="INFY"))
    out2 = _resolve(jenv, row2["id"], "no_app_gtt")
    assert out2["protection"]["state"] == "PROTECTION_FAILED"
    conn = conn_of(jenv)
    try:
        assert protection.create_claim_problem(conn, protection.get_position(conn, pid2), "fill", read(jenv),
                                               now=protection.utc_now()) == "M5_USER_RECREATE_REQUIRED"
    finally:
        conn.close()


def test_S4_T32_flat_position_linked_resolution_claims_an_automatic_cleanup_cancel(jenv):
    flat = make_position(jenv, closed=True)
    row = _review_op(jenv, flat, CANCEL, reason="position_closed", target=901)
    live = gtt(jenv)
    out = _resolve(jenv, row["id"], "linked_gtt", linked=live)
    assert out["cleanup_op"]["kind"] == "cancel" and out["cleanup_op"]["reason"] == "position_closed"
    assert out["cleanup_op"]["target_gtt_id"] == live
    conn = conn_of(jenv)
    try:
        assert protection.derive_protection(conn, flat, read(jenv))["cleanup_required"] is True
    finally:
        conn.close()


def test_S4_T33_deleted_in_kite_verified_needs_an_explicit_status(jenv):
    pid = make_position(jenv)
    g = gtt(jenv)
    row = _review_op(jenv, pid, CANCEL, reason="user_cancel", target=g)
    with pytest.raises(ProtectionRefused) as e:
        _resolve(jenv, row["id"], "deleted_in_kite_verified")       # still active
    assert e.value.code == "RESOLUTION_DELETE_NOT_EXPLICIT"
    jenv.kite.gtt_book[g]["status"] = "deleted"
    assert _resolve(jenv, row["id"], "deleted_in_kite_verified")["op"]["state"] == "resolved_manual"


# ---------------------------------------------------------------- supersede overlay
def test_S4_T34_supersede_moves_intent_to_not_sent_and_overlays_the_rest_never_cancels(jenv):
    pid = make_position(jenv)
    create = claim(jenv, pid)
    cancel = claim(jenv, pid, CANCEL, "user_cancel", target=8)
    conn = conn_of(jenv)
    try:
        with protection.journal_transaction(conn):
            out = protection.supersede_open_changes(conn, pid, "emergency_exit", "77")
    finally:
        conn.close()
    assert [o["id"] for o in out] == [create["id"]]
    c = op(jenv, create["id"])
    assert c["state"] == "not_sent" and c["superseded_by_kind"] == "emergency_exit" and c["superseded_by_ref"] == "77"
    assert op(jenv, cancel["id"])["superseded_at"] is None
    pid2 = make_position(jenv, symbol="INFY", token=408065)
    sent = claim(jenv, pid2, req=request(symbol="INFY"))
    force_state(jenv, sent["id"], "sending")
    conn = conn_of(jenv)
    try:
        with protection.journal_transaction(conn):
            protection.supersede_open_changes(conn, pid2, "exit", "9")
    finally:
        conn.close()
    s = op(jenv, sent["id"])
    assert s["state"] == "sending" and s["superseded_by_kind"] == "exit"     # state unchanged (row 19)
    assert [e[0] for e in events(jenv, pid2)][-1] == "protection_superseded"


# ---------------------------------------------------------------- static checks (DD-3, DD-5)
_WRITE_RE = re.compile(r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(protection_operations|protection_operation_resolutions)\b",
                       re.IGNORECASE)


def test_S4_T35_only_protection_py_writes_the_journal():
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if path.name in ("protection.py", "db.py"):
            continue
        if _WRITE_RE.search(path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    assert offenders == []


ENGINE_MODULES = ("protection_", "emergency_exit_engine")


def test_S4_T36_only_protection_py_names_the_reason_literals():
    """DD-3: engine code names a reason only through protection.R_* constants. Scoped to the engine modules:
    legacy modules use words such as 'trailing' (a worker name) with unrelated meanings."""
    literals = {r for rs in protection.REASONS.values() for r in rs}
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if path.name == "protection.py" or not path.name.startswith(ENGINE_MODULES):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in literals:
                offenders.append((path.name, node.value))
    assert offenders == []


def test_S4_T37_event_types_match_the_dd7_suffix():
    used = set(protection.EVENT_FOR_STATE.values()) | {"protection_confirmed", "protection_cancelled",
                                                        protection.EV_SUPERSEDED, protection.EV_LATE_GTT}
    assert used == set(db.PROTECTION_EVENT_TYPES)
