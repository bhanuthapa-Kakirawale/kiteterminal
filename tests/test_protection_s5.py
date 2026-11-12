"""Phase 3 S5: sending protection ops, fencing, ambiguous outcomes and late responses (backend/protection_send.py),
in-process on env's tmp database with the dark schema and FakeKite. Multi-process races are in
test_protection_mp.py (S9). Nothing here says anything about Kite."""

import logging
import sqlite3
from datetime import timedelta

import pytest
from kiteconnect.exceptions import InputException, NetworkException, TokenException

from backend import config, instance_lock, protection, protection_send
from backend.protection_send import Outcome, SendContext
from protection_helpers import (
    claim, conn_of, events, force_state, gtt, journal, make_position, op, read, request,
)


@pytest.fixture
def jenv(env):
    journal(env)
    return env


def _send(env, op_id, evidence=None, **kw):
    conn = conn_of(env)
    try:
        return protection_send.send(conn, op_id, env.kite, evidence or read(env), **kw)
    finally:
        conn.close()


def _retry(env, op_id, evidence=None, **kw):
    conn = conn_of(env)
    try:
        return protection_send.retry(conn, op_id, env.kite, evidence or read(env), **kw)
    finally:
        conn.close()


def _foreign_takeover(env):
    """Another process's committed takeover (the bypass simulation): epoch + 1, another identity."""
    raw = sqlite3.connect(env.db.DB_PATH, timeout=0.2)
    try:
        raw.execute("UPDATE app_instance SET epoch = epoch + 1, instance_uuid = 'someone-else' WHERE id = 1")
        raw.commit()
    finally:
        raw.close()


def _places(env):
    return len(env.kite.calls_to("place_gtt"))


# ---------------------------------------------------------------- the normal outcomes
def test_S5_T01_a_create_is_written_ahead_sent_once_and_linked(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    res = _send(jenv, row["id"], may_increase_exposure=True)
    assert res.sent and res.code == "ACKNOWLEDGED"
    after = op(jenv, row["id"])
    assert after["state"] == "acknowledged" and after["attempts"] == 1 and after["sent_at"]
    assert str(after["result_gtt_id"]) == res.op["result_gtt_id"].__str__()
    conn = conn_of(jenv)
    try:
        assert str(protection.get_position(conn, pid)["kite_gtt_id"]) == str(after["result_gtt_id"])
    finally:
        conn.close()
    assert _places(jenv) == 1
    assert [e[0] for e in events(jenv, pid)] == ["protection_pending", "protection_submitted", "protection_submitted"]


def test_S5_T02_a_definite_refusal_is_rejected_and_nothing_is_linked(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    jenv.kite.raise_on_next("place_gtt", InputException("bad trigger"))
    res = _send(jenv, row["id"], may_increase_exposure=True)
    assert res.code == "REJECTED" and op(jenv, row["id"])["state"] == "rejected"


@pytest.mark.parametrize("exc", [NetworkException("timeout"), RuntimeError("socket reset"),
                                 TokenException("session")], ids=["5xx", "transport", "token_on_create"])
def test_S5_T03_an_ambiguous_create_is_unknown_and_never_resent(jenv, exc):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    jenv.kite.accept_then_raise("place_gtt", exc)            # the broker took effect; the response was lost
    res = _send(jenv, row["id"], may_increase_exposure=True)
    assert res.code == "UNKNOWN"
    after = op(jenv, row["id"])
    assert after["state"] == "unknown" and after["complete_reads_after"] == 0 and after["result_gtt_id"] is None
    assert len(jenv.kite.gtt_book) == 1                       # the GTT exists at the broker
    assert _send(jenv, row["id"], may_increase_exposure=True).code == "NOT_INTENT"
    force_state(jenv, row["id"], "unknown", sent_at=(protection.utc_now() - timedelta(minutes=10)).isoformat())
    assert _retry(jenv, row["id"]).code == "INCREASE_NEVER_RETRIED"
    assert _places(jenv) == 1                                 # never re-sent (M-3, F1)


def test_S5_T04_a_malformed_create_response_is_ambiguous(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    jenv.kite.on_call("place_gtt", lambda kw: None)
    original = jenv.kite.place_gtt
    jenv.kite.place_gtt = lambda *a, **k: (original(*a, **k), {"trigger_id": None})[1]
    assert _send(jenv, row["id"], may_increase_exposure=True).code == "UNKNOWN"


def test_S5_T05_session_expiry_on_delete_is_a_definite_refusal(jenv):
    pid = make_position(jenv, closed=True)
    target = gtt(jenv)
    none = protection.classify(kind="cancel", reason="position_closed", q0=10, q1=0, open_qty=0, loosen=False)
    row = claim(jenv, pid, "cancel", "position_closed", target=target, automatic=True, dx=none)
    jenv.kite.raise_on_next("delete_gtt", TokenException("expired"))
    assert _send(jenv, row["id"]).code == "REJECTED"


# ---------------------------------------------------------------- send predicates (row 5 / 5a)
def test_S5_T06_superseded_before_the_send_is_not_sent_with_no_call(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    conn = conn_of(jenv)
    try:
        with protection.journal_transaction(conn):
            protection.supersede_open_changes(conn, pid, "emergency_exit", "1")
    finally:
        conn.close()
    res = _send(jenv, row["id"], may_increase_exposure=True)
    assert not res.sent and op(jenv, row["id"])["state"] == "not_sent" and _places(jenv) == 0


def test_S5_T07_superseded_after_the_sending_commit_keeps_the_op_and_applies_section_4_3(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    conn = conn_of(jenv)
    try:
        _op, ctx, code = protection_send.begin_send(conn, row["id"], read(jenv), may_increase_exposure=True)
        assert code == "SENDING"
        with protection.journal_transaction(conn):
            protection.supersede_open_changes(conn, pid, "exit", "5")
        res = protection_send.perform_send(conn, ctx, jenv.kite)
    finally:
        conn.close()
    after = op(jenv, row["id"])
    assert res.code == "ACKNOWLEDGED" and after["state"] == "acknowledged" and after["superseded_by_kind"] == "exit"
    resolution = [e for e in protection.evidence_entries(after) if "superseded_resolution" in e]
    assert resolution and resolution[0]["superseded_resolution"]["action"] == "linked_overlay_lapsed"


def test_S5_T08_exit_in_flight_refuses_a_create_send(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    conn = conn_of(jenv)
    try:
        conn.execute("INSERT INTO trade_tickets (account_id, symbol, exchange, instrument_token, transaction_type, "
                     "quantity, entry_price, entry_order_type, stop_loss, target_1, product_type, rationale, status, "
                     "mode, closes_ticket_id) SELECT account_id, symbol, exchange, instrument_token, 'SELL', 10, 3000, "
                     "'MARKET', 2900, 3300, 'CNC', 'exit', 'confirmed', mode, id FROM trade_tickets WHERE id = ?", (pid,))
        conn.commit()
    finally:
        conn.close()
    res = _send(jenv, row["id"], may_increase_exposure=True)
    assert res.code == "EXIT_IN_FLIGHT" and op(jenv, row["id"])["state"] == "not_sent" and _places(jenv) == 0


def test_S5_T09_dd8_freshness(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    stale = read(jenv)
    stale.started_at -= timedelta(seconds=config.PREWRITE_MAX_AGE_SECONDS + 1)
    assert _send(jenv, row["id"], stale, may_increase_exposure=True).code in ("READ_STALE", "READ_BEFORE_TAKEOVER")
    assert op(jenv, row["id"])["state"] == "not_sent" and _places(jenv) == 0
    row2 = claim(jenv, pid)
    partial = read(jenv)
    partial.complete = False
    assert _send(jenv, row2["id"], partial, may_increase_exposure=True).code == "READ_INCOMPLETE"


def test_S5_T10_dd9_refuses_an_exposure_increasing_send(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    assert _send(jenv, row["id"], may_increase_exposure=False).code == "DD9_REFUSED"
    assert op(jenv, row["id"])["state"] == "not_sent" and _places(jenv) == 0


def test_S5_T11_the_effect_class_is_re_evaluated_at_the_send(jenv):
    pid = make_position(jenv, gtt_id=None)
    survivor, extra = gtt(jenv), gtt(jenv)
    m2 = {k: True for k in protection.M2_CONDITIONS}
    redundant = protection.classify(kind="cancel", reason="duplicate_cleanup", q0=10, q1=0, open_qty=10,
                                    loosen=False, m2_conditions=m2)
    row = claim(jenv, pid, "cancel", "duplicate_cleanup", target=extra, automatic=True, dx=redundant)
    res = _send(jenv, row["id"])                     # no M-2 proof at the send: reduces_protection
    assert res.code == "AUTOMATIC_CLASS_REFUSED" and op(jenv, row["id"])["state"] == "not_sent"
    assert jenv.kite.calls_to("delete_gtt") == [] and survivor


def test_S5_T12_resume_flag_refuses_every_class_but_none_with_a_post_resume_read(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    flat = make_position(jenv, symbol="INFY", token=408065, closed=True)
    target = gtt(jenv, request(symbol="INFY"))
    none = protection.classify(kind="cancel", reason="position_closed", q0=10, q1=0, open_qty=0, loosen=False)
    cancel = claim(jenv, flat, "cancel", "position_closed", target=target, automatic=True, dx=none)
    pre_resume = read(jenv)
    with instance_lock._state.cv:
        instance_lock._state.process_reasons.add(instance_lock.RESUMED_AFTER_SUSPEND)
        instance_lock._state.resumed_at = protection.utc_now()
    assert _send(jenv, row["id"], read(jenv), may_increase_exposure=True).code == "RESUMED_AFTER_SUSPEND"
    assert _send(jenv, cancel["id"], pre_resume).code == "READ_BEFORE_RESUME"
    cancel2 = claim(jenv, flat, "cancel", "position_closed", target=target, automatic=True, dx=none)
    assert _send(jenv, cancel2["id"], read(jenv)).code == "ACKNOWLEDGED"      # none + post-resume read
    assert _places(jenv) == 0


# ---------------------------------------------------------------- fencing and ownership
def test_S5_T13_old_owner_cannot_claim_or_send_after_a_takeover(jenv):
    pid = make_position(jenv)
    evidence = read(jenv)
    _foreign_takeover(jenv)
    conn = conn_of(jenv)
    try:
        with pytest.raises(instance_lock.FencingError):
            with protection.journal_transaction(conn):
                protection.claim(conn, position_id=pid, kind="create", reason="fill", request=request(),
                                 evidence=evidence, may_increase_exposure=True)
    finally:
        conn.close()
    assert instance_lock.status()["role"] == "lost" and _places(jenv) == 0


def test_S5_T14_ownership_change_between_claim_and_send(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    evidence = read(jenv)
    _foreign_takeover(jenv)
    with pytest.raises(instance_lock.FencingError):
        _send(jenv, row["id"], evidence, may_increase_exposure=True)
    assert op(jenv, row["id"])["state"] == "intent" and _places(jenv) == 0


def test_S5_T15_ownership_change_after_the_sending_commit_before_the_call(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    conn = conn_of(jenv)
    try:
        _op, ctx, _ = protection_send.begin_send(conn, row["id"], read(jenv), may_increase_exposure=True)
        _foreign_takeover(jenv)
        res = protection_send.perform_send(conn, ctx, jenv.kite)
    finally:
        conn.close()
    assert not res.sent and res.code == "AUTHORITY_LOST"
    assert op(jenv, row["id"])["state"] == "sending" and _places(jenv) == 0   # the successor resolves it


def test_S5_T16_no_takeover_can_commit_while_a_send_is_in_progress(jenv):
    """The held fenced transaction: authorisation is atomic with respect to an ownership change."""
    pid = make_position(jenv)
    row = claim(jenv, pid)
    attempts = []

    def try_takeover(_ctx):
        try:
            _foreign_takeover(jenv)
            attempts.append("committed")
        except sqlite3.OperationalError as e:
            attempts.append(str(e))

    res = _send(jenv, row["id"], may_increase_exposure=True, on_call=try_takeover)
    assert attempts == ["database is locked"]
    assert res.code == "ACKNOWLEDGED" and _places(jenv) == 1
    _foreign_takeover(jenv)                                    # after the send, the takeover commits
    pid2 = make_position(jenv, symbol="INFY", token=408065)
    with pytest.raises(instance_lock.FencingError):
        claim(jenv, pid2, req=request(symbol="INFY"))


def test_S5_T17_a_clock_gap_at_the_pre_call_check_makes_no_call(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    conn = conn_of(jenv)
    try:
        _op, ctx, _ = protection_send.begin_send(conn, row["id"], read(jenv), may_increase_exposure=True)
        res = protection_send.perform_send(conn, ctx, jenv.kite,
                                           monotonic=lambda: instance_lock._state.last_hb_mono + 3600)
    finally:
        conn.close()
    assert not res.sent and res.code == instance_lock.RESUMED_AFTER_SUSPEND
    assert op(jenv, row["id"])["state"] == "sending" and _places(jenv) == 0
    assert instance_lock.resume_seq() == 1


def test_S5_T18_g2_a_response_that_cannot_be_committed_is_logged_never_written(jenv, monkeypatch, caplog):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    conn = conn_of(jenv)
    try:
        _op, ctx, _ = protection_send.begin_send(conn, row["id"], read(jenv), may_increase_exposure=True)
        real = protection.journal_transaction
        import contextlib

        @contextlib.contextmanager
        def losing(c):           # authority found lost at commit: the transaction rolls back
            with pytest.raises(RuntimeError):
                with real(c):
                    yield c
                    raise RuntimeError("rollback")
            raise instance_lock.FencingError("INSTANCE_LOCK_LOST: lost at commit")

        monkeypatch.setattr(protection, "journal_transaction", losing)
        with caplog.at_level(logging.CRITICAL):
            res = protection_send.perform_send(conn, ctx, jenv.kite)
    finally:
        conn.close()
    assert res.code == "G2_NOT_RECORDED" and _places(jenv) == 1
    assert "event=protection.response_after_authority_loss" in caplog.text
    assert op(jenv, row["id"])["state"] == "sending"          # no database write


def test_S5_T19_late_response_after_authority_loss_is_g2(jenv, caplog):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    force_state(jenv, row["id"], "unknown")
    ctx = SendContext(row["id"], row["op_uuid"], 1, instance_lock.owner_epoch(), "create", row["request_fingerprint"])
    _foreign_takeover(jenv)
    conn = conn_of(jenv)
    try:
        with caplog.at_level(logging.CRITICAL):
            res = protection_send.record_late_response(conn, ctx, Outcome("acknowledged", "55"))
    finally:
        conn.close()
    assert res.code == "G2_NOT_RECORDED" and "response_after_authority_loss" in caplog.text
    assert op(jenv, row["id"])["state"] == "unknown"


# ---------------------------------------------------------------- M-4 association
def _late(env, row, attempt, outcome):
    ctx = SendContext(row["id"], row["op_uuid"], attempt, instance_lock.owner_epoch(), row["kind"],
                      row["request_fingerprint"])
    conn = conn_of(env)
    try:
        return protection_send.record_late_response(conn, ctx, outcome)
    finally:
        conn.close()


def test_S5_T20_late_ack_on_unknown_same_attempt_acknowledges_and_links(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    force_state(jenv, row["id"], "unknown")
    res = _late(jenv, row, 1, Outcome("acknowledged", "321"))
    assert res.op["state"] == "acknowledged" and str(res.op["result_gtt_id"]) == "321"
    conn = conn_of(jenv)
    try:
        assert str(protection.get_position(conn, pid)["kite_gtt_id"]) == "321"
    finally:
        conn.close()


def test_S5_T21_late_rejection_on_unknown(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    force_state(jenv, row["id"], "unknown")
    assert _late(jenv, row, 1, Outcome("rejected", None, "InputException")).op["state"] == "rejected"


def test_S5_T22_a_response_for_a_non_current_attempt_is_evidence_only(jenv):
    pid = make_position(jenv, closed=True)
    row = claim(jenv, pid, "cancel", "user_cancel", target=77)
    force_state(jenv, row["id"], "sending", attempts=2)
    res = _late(jenv, row, 1, Outcome("acknowledged", "77"))
    assert res.op["state"] == "sending"
    assert any(e.get("stale_attempt") for e in protection.evidence_entries(res.op))


def test_S5_T23_duplicate_and_reordered_responses(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    force_state(jenv, row["id"], "unknown")
    first = _late(jenv, row, 1, Outcome("acknowledged", "400"))
    second = _late(jenv, row, 1, Outcome("acknowledged", "400"))       # the same response again
    assert first.op["state"] == second.op["state"] == "acknowledged"
    assert any("ignored_in_state" in e for e in protection.evidence_entries(second.op))
    pid2 = make_position(jenv, symbol="INFY", token=408065, closed=True)
    c = claim(jenv, pid2, "cancel", "user_cancel", target=88)
    force_state(jenv, c["id"], "sending", attempts=2)
    newer = _late(jenv, c, 2, Outcome("acknowledged", "88"))           # attempt 2 answers first
    older = _late(jenv, c, 1, Outcome("rejected", None, "InputException"))
    assert newer.op["state"] == "acknowledged" and older.op["state"] == "acknowledged"


def test_S5_T24_terminal_consistent_late_response_is_evidence_only(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    force_state(jenv, row["id"], "confirmed", result_gtt_id=500)
    res = _late(jenv, row, 1, Outcome("acknowledged", "500"))
    assert res.op["state"] == "confirmed"
    assert any(e.get("late") == "consistent" for e in protection.evidence_entries(res.op))


@pytest.mark.parametrize("state,code", [("absent_after_settle", "LATE_GTT_APPEARED"),
                                        ("rejected", "LATE_RESPONSE_CONTRADICTS"),
                                        ("not_sent", "LATE_RESPONSE_CONTRADICTS")])
def test_S5_T25_terminal_contradictory_late_response_raises_a_finding_op_unchanged(jenv, state, code):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    extra = {"complete_reads_after": 2} if state == "absent_after_settle" else {}
    if state == "not_sent":
        extra["sent_at"] = None
        extra["attempts"] = 0
    force_state(jenv, row["id"], state, **extra)
    res = _late(jenv, row, row["attempts"] if state == "not_sent" else 1, Outcome("acknowledged", "600"))
    assert res.op["state"] == state
    conn = conn_of(jenv)
    try:
        assert protection.get_position(conn, pid)["protection_state"] == "PROTECTION_NEEDS_REVIEW"
        assert conn.execute("SELECT COUNT(*) FROM reconciliation_log WHERE issue_code = ?", (code,)).fetchone()[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------- M-3 retries (row 16)
def _unknown_cancel(env, pid, target, reason="position_closed", automatic=True, sent_ago=60):
    q = 10
    dx = protection.classify(kind="cancel", reason=reason, q0=q, q1=0, open_qty=0, loosen=False)
    row = claim(env, pid, "cancel", reason, target=target, automatic=automatic and reason == "position_closed",
                dx=dx if automatic and reason == "position_closed" else None)
    force_state(env, row["id"], "unknown",
                sent_at=(protection.utc_now() - timedelta(seconds=sent_ago)).isoformat())
    return op(env, row["id"])


def test_S5_T26_a_cancel_is_retried_only_after_a_post_grace_read_shows_the_prestate(jenv):
    pid = make_position(jenv, closed=True)
    target = gtt(jenv)
    row = _unknown_cancel(jenv, pid, target, sent_ago=1)
    assert _retry(jenv, row["id"]).code in ("READ_NOT_AFTER_GRACE", "BACKOFF")
    force_state(jenv, row["id"], "unknown", sent_at=(protection.utc_now() - timedelta(seconds=60)).isoformat())
    res = _retry(jenv, row["id"])
    assert res.code == "ACKNOWLEDGED" and op(jenv, row["id"])["attempts"] == 2
    assert len(jenv.kite.calls_to("delete_gtt")) == 1


def test_S5_T27_no_retry_when_the_target_is_missing_or_not_active(jenv):
    pid = make_position(jenv, closed=True)
    target = gtt(jenv)
    row = _unknown_cancel(jenv, pid, target)
    del jenv.kite.gtt_book[target]
    assert _retry(jenv, row["id"]).code == "TARGET_NOT_LISTED"
    assert jenv.kite.calls_to("delete_gtt") == []


def test_S5_T28_an_exit_prepare_cancel_on_an_open_position_is_never_auto_retried(jenv):
    pid = make_position(jenv)
    target = gtt(jenv)
    row = claim(jenv, pid, "cancel", "exit_prepare", target=target)
    force_state(jenv, row["id"], "unknown", sent_at=(protection.utc_now() - timedelta(seconds=60)).isoformat())
    assert _retry(jenv, row["id"]).code == "RETRY_CLASS_REFUSED"
    assert jenv.kite.calls_to("delete_gtt") == []


def test_S5_T29_the_retry_cap(jenv):
    pid = make_position(jenv, closed=True)
    target = gtt(jenv)
    row = _unknown_cancel(jenv, pid, target)
    force_state(jenv, row["id"], "unknown", attempts=config.PROTECTION_RETRY_MAX_ATTEMPTS,
                sent_at=(protection.utc_now() - timedelta(seconds=600)).isoformat())
    assert _retry(jenv, row["id"]).code == "RETRY_CAP_REACHED"


# ---------------------------------------------------------------- crash after the broker effect
def test_S5_T30_a_crash_after_the_broker_effect_leaves_sending_and_one_gtt(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)

    class SimulatedCrash(BaseException):
        pass

    jenv.kite.accept_then_raise("place_gtt", SimulatedCrash())
    with pytest.raises(SimulatedCrash):
        _send(jenv, row["id"], may_increase_exposure=True)
    assert op(jenv, row["id"])["state"] == "sending"       # write-ahead committed; nothing after the call
    assert len(jenv.kite.gtt_book) == 1 and _places(jenv) == 1


def test_S5_T32_an_op_that_changed_state_between_the_two_transactions_is_not_sent(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    conn = conn_of(jenv)
    try:
        _op, ctx, _ = protection_send.begin_send(conn, row["id"], read(jenv), may_increase_exposure=True)
        force_state(jenv, row["id"], "unknown")                  # e.g. recovery past the grace
        res = protection_send.perform_send(conn, ctx, jenv.kite)
    finally:
        conn.close()
    assert not res.sent and res.code == "STATE_CHANGED" and _places(jenv) == 0


# ---------------------------------------------------------------- paper parity (DD-11)
def test_S5_T31_a_dry_run_op_uses_the_paper_broker_on_the_same_connection(jenv):
    pid = make_position(jenv, mode="dry_run")
    row = claim(jenv, pid)
    res = _send(jenv, row["id"], may_increase_exposure=True)
    assert res.code == "ACKNOWLEDGED" and str(res.op["result_gtt_id"]).startswith("PAPER-G-")
    assert _places(jenv) == 0                               # never Kite
    conn = conn_of(jenv)
    try:
        assert conn.execute("SELECT COUNT(*) FROM paper_gtts").fetchone()[0] == 1
    finally:
        conn.close()
