"""Phase 3 S8: runtime continuity, clocks and recovery (instance_lock M-6/R2-7 primitives, protection_runtime DD-9 and
recovery, the G3 journal-aware Stage 1 inventory, the G4 consumer-driven reset). In-process with injected clocks on
env's tmp database. Real sleep/hibernate and wake ordering can only be validated on a physical Windows machine
(WIN-INT; see the S8 report)."""

import pathlib
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from backend import config, instance_lock, protection, protection_runtime, protection_send, reconciliation, startup_reconciliation
from protection_helpers import account_id, claim, conn_of, force_state, gtt, journal, make_position, op, read, request

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
GAP = 3 * config.HEARTBEAT_SECONDS


@pytest.fixture
def jenv(env):
    journal(env)
    return env


def _base():
    with instance_lock._state.cv:
        return instance_lock._state.last_hb_wall, instance_lock._state.last_hb_mono


def _check(wall_shift=0.0, mono_shift=0.0):
    wall, mono = _base()
    return instance_lock.clock_gap_check(now=lambda: wall + timedelta(seconds=wall_shift),
                                         monotonic=lambda: mono + mono_shift)


# ---------------------------------------------------------------- detection on the shared baseline
@pytest.mark.parametrize("wall,mono", [(GAP + 1, 1), (1, GAP + 1), (GAP + 1, GAP + 1)], ids=["wall", "monotonic", "both"])
def test_S8_C01_a_gap_on_either_clock_is_one_resume(jenv, wall, mono):
    out = _check(wall, mono)
    assert out["gap"] and out["resume_seq"] == 1
    assert instance_lock.RESUMED_AFTER_SUSPEND in instance_lock.process_reasons()


def test_S8_C02_a_gap_below_the_threshold_does_nothing(jenv):
    assert not _check(GAP - 1, GAP - 1)["gap"] and instance_lock.resume_seq() == 0


def test_S8_C03_cas_first_then_heartbeat_one_resume_seq_and_exactly_one_note_resume(jenv, monkeypatch):
    noted = []
    monkeypatch.setattr(startup_reconciliation, "note_resume", lambda: noted.append(1))
    assert _check(GAP + 5, GAP + 5)["gap"]                      # the engine CAS saw the gap first
    wall, mono = _base()
    hb = instance_lock.heartbeat_once(now=lambda: wall + timedelta(seconds=1), monotonic=lambda: mono + 1)
    assert hb["ok"] and not hb["resumed"]                       # the heartbeat sees a refreshed baseline
    assert instance_lock.consume_resume() is True and instance_lock.consume_resume() is False
    assert noted == [1] and instance_lock.resume_seq() == 1     # (today's loop would never have noted it)


def test_S8_C04_heartbeat_first_then_the_cas_is_refused_by_the_shared_flag(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    wall, mono = _base()
    assert instance_lock.heartbeat_once(now=lambda: wall,                                 # a monotonic-only gap:
                                        monotonic=lambda: mono + GAP + 5)["resumed"]      # resumed_at precedes the read
    conn = conn_of(jenv)
    try:
        res = protection_send.send(conn, row["id"], jenv.kite, read(jenv), may_increase_exposure=True)
    finally:
        conn.close()
    assert res.code == instance_lock.RESUMED_AFTER_SUSPEND and not res.sent   # no gap seen, refused by the flag


def test_S8_C05_concurrent_detectors_produce_exactly_one_resume_seq(jenv):
    wall, mono = _base()
    barrier, results = threading.Barrier(8), []

    def detect(i):
        barrier.wait()
        if i % 2:
            results.append(instance_lock.clock_gap_check(now=lambda: wall + timedelta(seconds=GAP + 9),
                                                         monotonic=lambda: mono + GAP + 9)["gap"])
        else:
            results.append(instance_lock.heartbeat_once(now=lambda: wall + timedelta(seconds=GAP + 9),
                                                        monotonic=lambda: mono + GAP + 9)["resumed"])

    threads = [threading.Thread(target=detect, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and instance_lock.resume_seq() == 1


def test_S8_C06_a_second_gap_during_stage1_is_not_cleared_by_that_stage1(jenv):
    _check(GAP + 1, GAP + 1)
    stage1_started = instance_lock.resumed_at() + timedelta(seconds=1)
    wall, mono = _base()
    instance_lock.clock_gap_check(now=lambda: stage1_started + timedelta(seconds=GAP + 30),
                                  monotonic=lambda: mono + GAP + 30)                # a second suspend mid-run
    instance_lock.clear_resume(stage1_started)
    assert instance_lock.RESUMED_AFTER_SUSPEND in instance_lock.process_reasons()


def test_S8_C07_clock_moving_backward_refuses_sends_through_freshness(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    ev = read(jenv)
    conn = conn_of(jenv)
    try:
        back = lambda: datetime.now(timezone.utc) - timedelta(hours=1)  # noqa: E731
        res = protection_send.send(conn, row["id"], jenv.kite, ev, may_increase_exposure=True, now=back)
    finally:
        conn.close()
    assert res.code in ("READ_STALE", "READ_BEFORE_TAKEOVER") and not res.sent


def test_S8_C08_clock_moving_forward_is_a_resume(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    ev = read(jenv)
    conn = conn_of(jenv)
    try:
        fwd = lambda: datetime.now(timezone.utc) + timedelta(minutes=10)  # noqa: E731
        res = protection_send.send(conn, row["id"], jenv.kite, ev, may_increase_exposure=True, now=fwd)
    finally:
        conn.close()
    assert res.code == instance_lock.RESUMED_AFTER_SUSPEND and instance_lock.resume_seq() == 1


def test_S8_C09_hibernate_reads_taken_before_the_wake_are_refused_for_every_class(jenv):
    flat = make_position(jenv, closed=True)
    target = gtt(jenv)
    none = protection.classify(kind="cancel", reason="position_closed", q0=10, q1=0, open_qty=0, loosen=False)
    row = claim(jenv, flat, "cancel", "position_closed", target=target, automatic=True, dx=none)
    before = read(jenv)
    _check(8 * 3600, 8 * 3600)                                  # 8 hours asleep
    conn = conn_of(jenv)
    try:
        res = protection_send.send(conn, row["id"], jenv.kite, before)
    finally:
        conn.close()
    assert res.code == "READ_BEFORE_RESUME" and jenv.kite.calls_to("delete_gtt") == []


def test_S8_C10_a_delayed_response_after_a_resume_is_still_associated(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    force_state(jenv, row["id"], "unknown")
    _check(GAP + 1, GAP + 1)
    ctx = protection_send.SendContext(row["id"], row["op_uuid"], 1, instance_lock.owner_epoch(), "create",
                                      row["request_fingerprint"])
    conn = conn_of(jenv)
    try:
        res = protection_send.record_late_response(conn, ctx, protection_send.Outcome("acknowledged", "4242"))
    finally:
        conn.close()
    assert res.op["state"] == "acknowledged"                     # same epoch: M-4 association holds


def test_S8_C11_a_takeover_during_sleep_raises_before_any_clock_logic(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    raw = sqlite3.connect(jenv.db.DB_PATH, timeout=0.5)
    raw.execute("UPDATE app_instance SET epoch = epoch + 1, instance_uuid = 'successor' WHERE id = 1")
    raw.commit()
    raw.close()
    wall, mono = _base()
    conn = conn_of(jenv)
    try:
        with pytest.raises(instance_lock.FencingError):
            protection_send.send(conn, row["id"], jenv.kite, read_unfenced(jenv), may_increase_exposure=True,
                                 now=lambda: wall + timedelta(hours=2), monotonic=lambda: mono + 7200)
    finally:
        conn.close()
    assert instance_lock.resume_seq() == 0 and instance_lock.status()["role"] == "lost"


def read_unfenced(env):
    from backend.protection_evidence import from_read
    from backend import startup_reconciliation as sr
    now = datetime.now(timezone.utc)
    return from_read(sr.read_broker(env.kite), account_id=1, purpose="prewrite", epoch=instance_lock.owner_epoch(),
                     started_at=now, finished_at=now, read_id=1)


def test_S8_C12_authority_is_never_regained_in_the_same_process(jenv):
    instance_lock.lose_authority(instance_lock.INSTANCE_LOCK_LOST, "test")
    conn = conn_of(jenv)
    try:
        with pytest.raises(instance_lock.FencingError):
            with protection.journal_transaction(conn):
                pass
    finally:
        conn.close()
    assert not instance_lock.authority_ok()


# ---------------------------------------------------------------- recovery after a restart (rows 3, 10)
def test_S8_C13_recovery_after_a_restart(jenv):
    pid = make_position(jenv)
    older = claim(jenv, pid)
    pid2 = make_position(jenv, symbol="INFY", token=408065)
    sent = claim(jenv, pid2, req=request(symbol="INFY"))
    pid3 = make_position(jenv, symbol="RELIANCE", token=738561)
    mine = claim(jenv, pid3, req=request(symbol="RELIANCE"))
    epoch = instance_lock.owner_epoch()
    force_state(jenv, older["id"], "intent", instance_epoch=epoch - 1)
    force_state(jenv, sent["id"], "sending", instance_epoch=epoch - 1, evidence='[{"send": 1, "epoch": %d}]' % (epoch - 1))
    conn = conn_of(jenv)
    try:
        conn.execute("INSERT INTO trade_tickets (account_id, symbol, exchange, instrument_token, transaction_type, quantity, "
                     "entry_price, entry_order_type, stop_loss, target_1, product_type, rationale, status, mode, "
                     "closes_ticket_id, risk_check_results) SELECT account_id, symbol, exchange, instrument_token, 'SELL', "
                     "10, 3000, 'MARKET', 2900, 3300, 'CNC', 'Emergency Exit (engine)', 'confirmed', mode, id, ? FROM "
                     "trade_tickets WHERE id = ?", ('{"ee_engine": {"epoch": %d}}' % (epoch - 1), pid))
        conn.execute("INSERT INTO trade_tickets (account_id, symbol, exchange, instrument_token, transaction_type, quantity, "
                     "entry_price, entry_order_type, stop_loss, target_1, product_type, rationale, status, mode, "
                     "closes_ticket_id, risk_check_results, submission_state) SELECT account_id, symbol, exchange, "
                     "instrument_token, 'SELL', 10, 3000, 'MARKET', 2900, 3300, 'CNC', 'Emergency Exit (engine)', "
                     "'confirmed', mode, id, ?, 'submitting' FROM trade_tickets WHERE id = ?",
                     ('{"ee_engine": {"epoch": %d}}' % (epoch - 1), pid2))   # its SELL may have gone out
        conn.commit()
        out = protection_runtime.recover_journal(conn, account_id(conn), now=protection.utc_now())
        again = protection_runtime.recover_journal(conn, account_id(conn), now=protection.utc_now())
        assert not protection.exit_in_flight(conn, pid)
        assert protection.exit_in_flight(conn, pid2)               # a submitted SELL is never released
    finally:
        conn.close()
    assert out["intent_to_not_sent"] == [older["id"]] and out["sending_to_unknown"] == [sent["id"]]
    assert len(out["ee_tickets_released"]) == 1
    assert op(jenv, mine["id"])["state"] == "intent"              # this epoch's own claim is untouched
    assert again == {"intent_to_not_sent": [], "sending_to_unknown": [], "ee_tickets_released": []}   # idempotent
    assert op(jenv, sent["id"])["attempts"] == 1                   # never re-sent


def test_S8_C14_recovery_with_unknown_broker_state_concludes_nothing(jenv):
    pid = make_position(jenv)
    row = claim(jenv, pid)
    force_state(jenv, row["id"], "unknown")
    jenv.kite.fail_every_call(RuntimeError("broker down"))
    conn = conn_of(jenv)
    try:
        from backend.protection_evidence import capture
        ev = capture(conn, jenv.kite, account_id(conn), "reconciliation")
        from backend import protection_reconcile
        assert protection_reconcile.reconcile_op(conn, row["id"], ev)["action"] == "READ_INCOMPLETE"
        assert protection.create_claim_problem(conn, protection.get_position(conn, pid), "fill", ev,
                                               now=protection.utc_now()) is not None
    finally:
        conn.close()
    assert op(jenv, row["id"])["state"] == "unknown"


# ---------------------------------------------------------------- DD-9
def _dd9(env, pid, ev=None, **kw):
    conn = conn_of(env)
    try:
        return protection_runtime.may_increase_exposure(conn, pid, ev or read(env), now=protection.utc_now(),
                                                        attribution=kw.pop("attribution", lambda e: "exclusive"), **kw)
    finally:
        conn.close()


def test_S8_C15_dd9_passes_on_a_clean_account_and_ignores_unresolved_ops_elsewhere(jenv):
    pid = make_position(jenv)
    other = make_position(jenv, symbol="INFY", token=408065)
    row = claim(jenv, other, req=request(symbol="INFY"))
    force_state(jenv, row["id"], "unknown")                       # UNRESOLVED_OPERATION on another position
    ok, failed = _dd9(jenv, pid)
    assert ok, failed


@pytest.mark.parametrize("breaker", ["1_owner", "2_stage1", "3_read", "4_reason", "5_attribution", "6_clock", "7_m5"])
def test_S8_C16_each_dd9_condition_failing_alone_refuses(jenv, breaker, monkeypatch):
    pid = make_position(jenv)
    ev = read(jenv)
    kw = {}
    if breaker == "1_owner":
        monkeypatch.setattr(instance_lock, "authority_ok", lambda: False)
    elif breaker == "2_stage1":
        conn = conn_of(jenv)
        conn.execute("UPDATE startup_reconciliation_runs SET stage1_outcome = 'failed'")
        conn.commit()
        conn.close()
    elif breaker == "3_read":
        ev.complete = False
    elif breaker == "4_reason":
        with instance_lock._state.cv:
            instance_lock._state.process_reasons.add(instance_lock.HEARTBEAT_STALE)
    elif breaker == "5_attribution":
        kw["attribution"] = lambda e: "broken"
    elif breaker == "6_clock":
        wall, mono = _base()
        kw["monotonic"] = lambda: mono + GAP + 1
    else:
        gtt(jenv)                                                 # an unlinked SELL GTT on the instrument
        ev = read(jenv)
    ok, failed = _dd9(jenv, pid, ev, **kw)
    assert not ok and any(f.startswith(breaker[0]) for f in failed), failed


# ---------------------------------------------------------------- G3 and G4
def test_S8_C17_journal_aware_stage1_inventory(jenv):
    pid = make_position(jenv)
    claim(jenv, pid)
    conn = conn_of(jenv)
    try:
        inv = startup_reconciliation.local_inventory(conn, account_id(conn))
        assert inv["open_protection_operations"] and \
            startup_reconciliation.UNRESOLVED_OPERATION in startup_reconciliation.local_reasons(inv)
    finally:
        conn.close()


def test_S8_C18_the_g4_reset_is_consumer_driven_after_a_cas_detected_resume(jenv):
    with reconciliation.CLASSIFICATION_LOCK:
        reconciliation.reset_memory_if_resumed()
        reconciliation._price_seen[1] = ("sighting",)
        assert reconciliation.reset_memory_if_resumed() is False and reconciliation._price_seen
    _check(GAP + 1, GAP + 1)                                      # detected by an engine CAS, not the heartbeat
    with reconciliation.CLASSIFICATION_LOCK:
        assert reconciliation.reset_memory_if_resumed() is True and not reconciliation._price_seen
    src = (BACKEND / "reconciliation.py").read_text(encoding="utf-8")
    assert re.search(r"def _run_cycle\(\) -> None:\s+reset_memory_if_resumed\(\)", src)
    assert "reconciliation.reset_memory_if_resumed()" in (BACKEND / "startup_reconciliation.py").read_text(encoding="utf-8")


def test_S8_C19_the_engine_never_takes_classification_lock_or_a_second_connection(jenv, monkeypatch):
    for path in BACKEND.glob("protection*.py"):
        text = path.read_text(encoding="utf-8")
        assert "CLASSIFICATION_LOCK" not in text and "get_connection" not in text, path.name
    pid = make_position(jenv)
    row = claim(jenv, pid)
    ev = read(jenv)
    from backend import db
    opened = []
    real = db.get_connection
    monkeypatch.setattr(db, "get_connection", lambda: (opened.append(1), real())[1])
    conn = real()
    try:
        protection_send.send(conn, row["id"], jenv.kite, ev, may_increase_exposure=True)
    finally:
        conn.close()
    assert opened == []
