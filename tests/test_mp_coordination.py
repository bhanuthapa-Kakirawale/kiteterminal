"""Phase 3 S3: application processes coordinating through one database and one shared FakeKite.

Two (or more) real application processes (tests/mp_app_child.py) run the Phase 2 startup sequence, fencing,
heartbeat and Stage 1 against env's tmp database, and send broker requests to ONE shared FakeKite
(tests/shared_fake_kite.py). Every test asserts both the database and the shared broker's call log, which
records, for every request that reached the "broker", the sending pid and its in-memory epoch.

What these tests do NOT prove:
- Anything about Kite. The shared FakeKite is a model.
- Engine behaviour: Phase 3 has no protection engine yet (S4+). MP-C1 races the dark journal's unique index
  directly; MP-F2L uses the existing live-entry claim, exactly as Phase 2 C1 does.
- Absence of races: barriers and repetition give statistical evidence only.

Tests named test_residual_* PIN today's disclosed residual behaviour (docs/PHASE_2_DESIGN_REVIEW.md §7.3).
They describe what the code does now, not a safety property. A later stage that closes the residual must
update them deliberately.

Timing is scaled IN THE CHILDREN ONLY: HEARTBEAT_SECONDS = 0.2 (resume threshold 0.6 s), busy timeout 1 s,
lease 15 s (the schema minimum). A successor that must not wait out a predecessor's lease shifts its own
wall clock forward (set_clock), as Phase 2 E2/P1 did with clock_shift.
"""

import os
import time
import uuid

import pytest

from mp_harness import descendants, mp, pid_alive, prepare_app_db, rows  # noqa: F401  (mp is a fixture)

LEG = [{"transaction_type": "SELL", "quantity": 5, "order_type": "LIMIT", "product": "CNC", "price": 2900.0}]
GTT = {"trigger_type": "single", "tradingsymbol": "TCS", "exchange": "NSE", "trigger_values": [2910.0],
       "last_price": 3000.0, "orders": LEG}
ORDER = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "TCS", "transaction_type": "SELL", "quantity": 1,
         "product": "CNC", "order_type": "MARKET"}
SKIP_NOT_WINDOWS = pytest.mark.skipif(os.name != "nt", reason="Windows-specific (W-tests)")


@pytest.fixture
def shared(mp, env):
    """env's tmp database handed to the children, and a shared FakeKite."""
    prepare_app_db(env)
    mp.start_server()
    return mp, env


def _writes(server, pid=None, method=None):
    return server.control.writes(method=method, pid=pid)


def _wait(predicate, timeout=20.0, step=0.02, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError(f"timed out waiting for {what}")


def _successor(mp, env, name, shift=3600, **kwargs):
    """A process whose wall clock is `shift` s ahead: it does not wait out a predecessor's lease."""
    child = mp.app(name, env, **kwargs)
    child.cmd("set_clock", wall=shift)
    return child


def _app_instance(env):
    return rows(env, "SELECT * FROM app_instance WHERE id = 1")[0]


# ================================================================ L: the lock across processes
def test_MP_L1_two_processes_released_together_exactly_one_owner_and_only_it_writes(shared):
    mp, env = shared
    server = mp.server
    a, b = mp.app("a", env), mp.app("b", env)
    epoch_before = _app_instance(env)["epoch"]
    for k in range(50):
        barrier = f"go_{k}"
        ids = {c: c.start("race_start", barrier=barrier, order=ORDER) for c in (a, b)}
        for c in (a, b):
            c.wait_event("parked")
        mp.release(barrier)
        results = {c: c.result(ids[c]) for c in (a, b)}
        owners = [c for c, r in results.items() if r["role"] == "owner"]
        assert len(owners) == 1, (k, results)
        owner = owners[0]
        loser = b if owner is a else a
        assert results[loser]["role"] == "non_owner"
        assert results[loser]["write"] == {"sent": False, "refused": "ANOTHER_INSTANCE_RUNNING"}
        assert results[owner]["write"]["sent"] is True
        assert results[owner]["epoch"] == epoch_before + k + 1
        owner.cmd("shutdown")
        for c in (a, b):
            c.cmd("reset")
    log = server.control.writes()
    assert len(log) == 50 and all(e["method"] == "place_order" for e in log)
    # Every write that reached the broker came from that round's owner, with that round's epoch.
    assert [e["epoch"] for e in log] == list(range(epoch_before + 1, epoch_before + 51))


def test_MP_L2_after_a_clean_shutdown_the_next_process_takes_over_at_once(shared):
    mp, env = shared
    a = mp.app("a", env)
    took_a = a.cmd("start")
    assert took_a["role"] == "owner"
    assert a.cmd("shutdown")["shutdown"] == {"recorded": True, "in_flight": 0}
    row = _app_instance(env)
    assert row["state"] == "stopped_clean" and row["clean_shutdown"] == 1
    a.stop()
    b = mp.app("b", env)
    took_b = b.cmd("start")
    assert took_b["role"] == "owner" and took_b["epoch"] == took_a["epoch"] + 1
    assert took_b["previous_end"] == "stopped_clean" and took_b["previous_unclean"] is False
    assert took_b["waited_seconds"] < 1 and took_b["reasons"] == ["STARTUP_RECONCILIATION_INCOMPLETE"]
    history = rows(env, "SELECT end_state FROM app_instance_history WHERE epoch = ?", took_a["epoch"])
    assert history == [{"end_state": "stopped_clean"}]


def test_MP_L3_W1_a_terminated_owner_releases_the_lock_and_the_successor_waits_out_its_lease(shared):
    mp, env = shared
    a = mp.app("a", env)
    took_a = a.cmd("start")
    a.cmd("heartbeat")
    assert a.kill_hard() == 137                               # TerminateProcess: no cleanup, no shutdown record
    b = mp.app("b", env)
    started = time.monotonic()
    role = b.cmd("acquire")["role"]
    while role != "locked" and time.monotonic() - started < 10:
        b.cmd("reset")
        role = b.cmd("acquire")["role"]
    released_after = time.monotonic() - started
    print(f"MP-L3 lock acquired {released_after * 1000:.1f} ms after TerminateProcess returned")
    assert role == "locked"
    # The predecessor's lease is 15 s from its last heartbeat. B's clock is shifted so that ~2 s of it remain,
    # measured from the heartbeat actually recorded (B's own start-up has already used part of the lease).
    from datetime import datetime, timezone
    heartbeat = datetime.fromisoformat(_app_instance(env)["heartbeat_at"])
    age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
    b.cmd("set_clock", wall=max(0.0, 13.0 - age))
    took_b = b.cmd("takeover", timeout=60)
    assert took_b["previous_end"] == "crashed" and took_b["previous_unclean"] is True and not took_b["conflict"]
    print(f"MP-L3 successor waited {took_b['waited_seconds']:.2f} s of the predecessor's lease (target ~2 s)")
    assert 1.0 <= took_b["waited_seconds"] <= 10, took_b
    assert took_b["epoch"] == took_a["epoch"] + 1
    assert "PREVIOUS_INSTANCE_UNCLEAN" in took_b["reasons"]
    assert rows(env, "SELECT end_state FROM app_instance_history WHERE epoch = ?", took_a["epoch"]) == [
        {"end_state": "crashed_detected"}]


@pytest.mark.parametrize("death", ["os_exit", "terminate"])
def test_MP_L4_W7_a_death_inside_a_write_transaction_leaves_nothing_behind(shared, death):
    mp, env = shared
    a = mp.app("a", env)
    a.cmd("start")
    a.cmd("begin_tx", marker="uncommitted", fenced=True)     # BEGIN IMMEDIATE held: the write lock is A's
    if death == "os_exit":
        a.start("crash", code=137)
        assert a.wait_exit() == 137
    else:
        assert a.kill_hard() == 137
    b = _successor(mp, env, "b")
    took = b.cmd("start")
    assert took["role"] == "owner" and took["previous_unclean"] is True
    assert b.cmd("fenced_write", marker="after")["written"] is True
    markers = [r["marker"] for r in rows(env, "SELECT marker FROM mp_markers ORDER BY id")]
    assert markers == ["after"]                               # the uncommitted WAL frames are gone
    assert rows(env, "PRAGMA integrity_check") == [{"integrity_check": "ok"}]


@pytest.mark.parametrize("release_before_successor", [False, True], ids=["held", "released_late"])
def test_MP_L5_D1_a_death_while_a_response_is_held_the_successor_sends_nothing(shared, release_before_successor):
    """MP-L5 (held for good) and MP-D1 (the response released to a dead process)."""
    mp, env = shared
    server = mp.server
    a = mp.app("a", env)
    a.cmd("start")
    server.control.fault("place_gtt", "accept_then_hold", gate="hold")
    a.start("broker", method="place_gtt", kwargs=GTT)
    _wait(lambda: any(e["effect_applied"] for e in server.control.log()), what="the GTT to be accepted")
    assert a.kill_hard() == 137
    if release_before_successor:
        server.control.release("hold")
        _wait(lambda: server.control.log()[-1]["response"] != "pending", what="the late response")
    b = _successor(mp, env, "b")
    assert b.cmd("start")["role"] == "owner"
    b.cmd("stage1")
    listed = b.cmd("broker", method="get_gtts")["result"]
    assert [g["status"] for g in listed] == ["active"]        # the successor sees the predecessor's GTT
    assert len(_writes(server)) == 1 and _writes(server, pid=b.pid) == []   # exactly one write, none by B
    [write] = _writes(server)
    assert write["pid"] == a.pid and write["effect_applied"] is True


# ================================================================ E: the fencing epoch across processes
def test_MP_E1_a_bypass_takeover_fences_out_the_old_owner(shared):
    mp, env = shared
    server = mp.server
    a = mp.app("a", env)
    epoch_a = a.cmd("start")["epoch"]
    b = _successor(mp, env, "b", bypass=True)                 # a different lock file: the simulated bypass
    took_b = b.cmd("start")
    assert took_b["role"] == "owner" and took_b["epoch"] == epoch_a + 1
    fenced = a.cmd("fenced_write", marker="from_a")
    assert fenced == {"id": fenced["id"], "ok": True, "written": False, "error": "FencingError",
                      "code": "INSTANCE_LOCK_LOST"}
    status = a.cmd("status")
    assert status["role"] == "lost" and status["refusal_code"] == "INSTANCE_LOCK_LOST"
    assert a.cmd("broker", method="place_order", kwargs=ORDER)["refused"] == "INSTANCE_LOCK_LOST"
    assert _writes(server, pid=a.pid) == []
    assert rows(env, "SELECT marker FROM mp_markers WHERE marker = 'from_a'") == []
    assert b.cmd("fenced_write", marker="from_b")["written"] is True


def test_residual_MP_E2_a_cycle_past_its_authority_check_still_commits_its_unfenced_write(shared):
    """D§7.3 residual, pinned: a legacy cycle that passed loop_may_run before the takeover finishes its local
    writes. Its fenced write is refused (and it loses authority); its UNFENCED write commits anyway."""
    mp, env = shared
    a = mp.app("a", env)
    epoch_a = a.cmd("start")["epoch"]
    cycle = a.start("cycle", park="past_check", fenced_marker="a_fenced", legacy_marker="a_legacy")
    a.wait_event("parked")
    b = _successor(mp, env, "b", bypass=True)
    assert b.cmd("start")["epoch"] == epoch_a + 1
    mp.release("past_check")
    result = a.result(cycle)
    assert result["ran"] is True
    assert result["fenced"]["written"] is False and result["fenced"]["error"] == "FencingError"
    assert result["legacy"] == {"written": True}             # THE RESIDUAL: no fence, no refusal
    assert rows(env, "SELECT marker, epoch, kind FROM mp_markers") == [
        {"marker": "a_legacy", "epoch": epoch_a, "kind": "legacy"}]
    assert a.cmd("status")["role"] == "lost"


def test_residual_MP_E3_a_stale_owner_can_send_until_it_notices_and_a_fenced_claim_first_stops_it(shared):
    """Pinned: broker_write checks the IN-MEMORY role only. Between a bypass takeover and the old owner's
    next fenced transaction or heartbeat, its broker write goes out (stale epoch in the broker log). The
    engine's pattern, a fenced claim committed BEFORE the call (M-6 R2-7 points 6 and 8), refuses it."""
    mp, env = shared
    server = mp.server
    a = mp.app("a", env)
    epoch_a = a.cmd("start")["epoch"]
    b = _successor(mp, env, "b", bypass=True)
    assert b.cmd("start")["epoch"] == epoch_a + 1
    stale = a.cmd("broker", method="place_order", kwargs=ORDER)
    assert stale["sent"] is True                              # THE RESIDUAL
    [write] = _writes(server, pid=a.pid)
    assert write["epoch"] == epoch_a                          # attributable: the old epoch, after the takeover
    probe = a.cmd("probe_cas_then_send", marker="claim", method="place_order", kwargs=ORDER)
    assert probe["claim"]["written"] is False and probe["claim"]["error"] == "FencingError"
    assert probe["sent"] is False
    assert len(_writes(server, pid=a.pid)) == 1               # the fenced claim stopped the second send
    assert a.cmd("broker", method="place_order", kwargs=ORDER)["refused"] == "INSTANCE_LOCK_LOST"


def test_MP_E4_the_old_owners_heartbeat_detects_the_takeover_within_two_intervals(shared):
    mp, env = shared
    a = mp.app("a", env)
    epoch_a = a.cmd("start")["epoch"]
    b = _successor(mp, env, "b", bypass=True)
    assert b.cmd("start")["epoch"] == epoch_a + 1
    restarted = time.time()
    a.cmd("heartbeats_start")
    status = _wait(lambda: (lambda s: s if s["lost_at"] else None)(a.cmd("status")), what="A to lose authority")
    latency = status["lost_at"] - restarted
    print(f"MP-E4 takeover detected {latency:.3f} s after A's heartbeat loop started (interval 0.2 s)")
    assert status["role"] == "lost" and status["refusal_code"] == "INSTANCE_LOCK_LOST"
    assert latency <= 2 * 0.2 + 1.0                           # two intervals plus a scheduling margin


def test_MP_E5_epochs_stay_strictly_sequential_across_twenty_crashes(shared):
    mp, env = shared
    first = _app_instance(env)["epoch"]
    epochs = []
    for k in range(20):
        # Each generation's clock is an hour past its crashed predecessor's (whose heartbeat it reads), so no
        # successor waits out the 15 s lease; MP-L3 covers the wait itself.
        child = _successor(mp, env, f"c{k}", shift=3600 * (k + 1))
        took = child.cmd("start")
        assert took["role"] == "owner", took
        epochs.append(took["epoch"])
        assert took["previous_unclean"] is (k > 0)
        child.start("crash", code=137)
        assert child.wait_exit() == 137
    assert epochs == list(range(first + 1, first + 21))
    closing = _successor(mp, env, "closing", shift=3600 * 21)
    assert closing.cmd("start")["epoch"] == first + 21
    history = rows(env, "SELECT epoch, end_state FROM app_instance_history WHERE epoch > ? ORDER BY epoch", first)
    assert [h["epoch"] for h in history] == list(range(first + 1, first + 22))
    assert [h["end_state"] for h in history] == ["crashed_detected"] * 20 + ["running"]


# ================================================================ S: suspend, resume, loss of authority
@SKIP_NOT_WINDOWS
def test_MP_S1_W3_a_suspended_owner_keeps_the_lock(shared):
    mp, env = shared
    a = mp.app("a", env)
    a.cmd("start")
    a.suspend()
    try:
        b = mp.app("b", env)
        assert b.cmd("acquire")["role"] == "non_owner"       # frozen, not dead: the OS lock is still held
    finally:
        a.resume()
    status = a.cmd("status")
    assert status["role"] == "owner" and status["authority"] is True


@SKIP_NOT_WINDOWS
def test_MP_S2_a_resume_gap_restricts_until_stage1_passes_again(shared):
    mp, env = shared
    a = mp.app("a", env)
    a.cmd("start")
    a.cmd("stage1")                                           # NORMAL before the suspend
    assert a.cmd("derive")["state"] == "NORMAL", a.cmd("derive")
    a.cmd("heartbeats_start")
    _wait(lambda: a.cmd("status")["heartbeats"] >= 2, what="heartbeats")
    a.suspend()
    time.sleep(1.5)                                           # > 3 x 0.2 s: a real freeze of every thread
    a.resume()
    status = _wait(lambda: (lambda s: s if s["resumed_events"] and s["stage1_requests"] else None)(a.cmd("status")),
                   what="the resume and its Stage 1 request")
    assert status["role"] == "owner" and "RESUMED_AFTER_SUSPEND" in status["process_reasons"]
    assert "resume" in status["stage1_requests"]
    derived = a.cmd("derive")
    assert derived["state"] == "RESTRICTED" and "RESUMED_AFTER_SUSPEND" in derived["reasons"]
    a.cmd("heartbeats_stop")
    assert a.cmd("stage1", trigger="resume")["outcome"] == "passed"
    assert a.cmd("derive")["state"] == "NORMAL"


@SKIP_NOT_WINDOWS
def test_MP_S3_a_takeover_during_a_suspend_is_found_by_the_first_heartbeat_after_it(shared):
    mp, env = shared
    server = mp.server
    a = mp.app("a", env)
    epoch_a = a.cmd("start")["epoch"]
    a.cmd("heartbeats_start")
    _wait(lambda: a.cmd("status")["heartbeats"] >= 1, what="a heartbeat")
    a.suspend()
    try:
        b = _successor(mp, env, "b", bypass=True)
        assert b.cmd("start")["epoch"] == epoch_a + 1
    finally:
        a.resume()
    status = _wait(lambda: (lambda s: s if s["lost_at"] else None)(a.cmd("status")), what="A to lose authority")
    assert status["role"] == "lost"
    first_after = status["last_heartbeat"]
    assert first_after["lost"] is True
    assert a.cmd("broker", method="place_order", kwargs=ORDER)["refused"] == "INSTANCE_LOCK_LOST"
    assert _writes(server, pid=a.pid) == []


@SKIP_NOT_WINDOWS
def test_MP_S4_W3_a_frozen_writer_blocks_a_takeover_which_fails_closed(shared):
    mp, env = shared
    server = mp.server
    a = mp.app("a", env)
    epoch_a = a.cmd("start")["epoch"]
    a.cmd("begin_tx", marker="held", fenced=True)
    a.suspend()
    try:
        b = _successor(mp, env, "b", bypass=True)
        failed = b.cmd("start", check=False, timeout=30)
        assert failed["ok"] is False and "database is locked" in failed["error"]
        assert b.cmd("status")["role"] == "locked"            # never became owner
        assert b.cmd("broker", method="place_order", kwargs=ORDER)["refused"] == "INSTANCE_NOT_OWNER"
        assert _app_instance(env)["epoch"] == epoch_a
    finally:
        a.resume()
    a.cmd("end_tx", commit=True)
    assert [r["marker"] for r in rows(env, "SELECT marker FROM mp_markers")] == ["held"]
    took = b.cmd("takeover")
    assert took["epoch"] == epoch_a + 1
    assert server.control.writes() == []


# ================================================================ C: concurrent transactions
def test_MP_C1_the_dark_journal_index_admits_one_open_create_per_position_across_processes(shared):
    """Schema-level P§9.3 S3/D2, NOT the engine: two processes with a shared simulated identity (the OS lock
    and epoch out of the picture, as Phase 2 S1) race to claim the same position, 100 rounds."""
    mp, env = shared
    server = mp.server
    from protection_helpers import journal
    journal(env)   # the dark schema on today's build; on a build registering v12, env is v12 and has it already
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE app_instance SET epoch = 9, instance_uuid = 'c1'")
        account = conn.execute("SELECT id FROM accounts WHERE is_default = 1").fetchone()["id"]
        positions = [conn.execute(
            "INSERT INTO trade_tickets (account_id, symbol, exchange, instrument_token, transaction_type, quantity, "
            "entry_price, entry_order_type, stop_loss, target_1, product_type, rationale, status, mode, source) "
            "VALUES (?, 'TCS', 'NSE', 2953217, 'BUY', 5, 3000, 'LIMIT', 2900, 3300, 'CNC', 'mp c1', 'confirmed', "
            "'live', 'manual')", (account,)).lastrowid for _ in range(100)]
        conn.commit()
    finally:
        conn.close()
    a, b = mp.app("a", env), mp.app("b", env)
    for c in (a, b):
        c.cmd("simulate_owner", epoch=9, uuid="c1")
    for k, position in enumerate(positions):
        ids = {c: c.start("insert_intent", park=f"r{k}", position_id=position, quantity=5,
                          op_uuid=str(uuid.uuid4()), method="place_gtt", kwargs=GTT) for c in (a, b)}
        for c in (a, b):
            c.wait_event("parked")
        mp.release(f"r{k}")
        results = [c.result(ids[c]) for c in (a, b)]
        winners = [r for r in results if r["inserted"]]
        assert len(winners) == 1, (k, results)
        [loser] = [r for r in results if not r["inserted"]]
        assert loser["error"] == "IntegrityError" and loser["sent"] is False, loser
        assert winners[0]["sent"] is True
    ops = rows(env, "SELECT position_id, COUNT(*) AS n FROM protection_operations GROUP BY position_id")
    assert len(ops) == 100 and all(o["n"] == 1 for o in ops)
    assert len(server.control.writes(method="place_gtt")) == 100   # one broker call per position, never two


# ================================================================ F2 on the existing live-entry path
@pytest.mark.parametrize("ambiguity", ["crash_after_accept", "dropped_response"])
def test_MP_F2L_an_ambiguous_entry_is_never_resent_by_the_successor(shared, ambiguity):
    """D§10.2 F2 for the EXISTING entry path (the protection-op F2 needs the S5 engine): the predecessor's
    claim is committed, the shared broker accepts the order, and the outcome is lost (the process dies, or
    the response never arrives). The successor's Stage 1 restricts and sends nothing."""
    mp, env = shared
    server = mp.server
    a = mp.app("a", env)
    a.cmd("start")
    a.cmd("claim_entry", tag="ktmpf2")
    if ambiguity == "crash_after_accept":
        server.control.fault("place_order", "accept_then_raise", exc={"type": "SimulatedCrash", "message": "died"})
        a.start("broker", method="place_order", kwargs={**ORDER, "transaction_type": "BUY", "tag": "ktmpf2"})
        assert a.wait_exit() == 137
        b = _successor(mp, env, "b")
    else:
        server.control.fault("place_order", "accept_then_drop")
        out = a.cmd("broker", method="place_order", kwargs={**ORDER, "transaction_type": "BUY", "tag": "ktmpf2"})
        assert out["sent"] is None and out["error"] == "ConnectionError"
        a.cmd("shutdown")
        a.stop()
        b = mp.app("b", env)
    assert b.cmd("start")["role"] == "owner"
    stage1 = b.cmd("stage1")
    assert "UNRESOLVED_OPERATION" in stage1["reasons_after"], stage1
    assert [w["pid"] for w in _writes(server, method="place_order")] == [a.pid]   # exactly one, A's
    assert _writes(server, pid=b.pid) == []
    assert rows(env, "SELECT submission_state FROM trade_tickets WHERE order_tag = 'ktmpf2'") == [
        {"submission_state": "submitting"}]


# ================================================================ D: delayed and reordered responses
def test_MP_D2_a_late_response_reaches_a_stale_owner_whose_record_is_then_refused(shared):
    """The G2 primitive (DD-10): the response arrives after the takeover; recording it needs a fenced
    transaction, which is refused. (The CRITICAL log line and the successor's adoption belong to S5/S6.)"""
    mp, env = shared
    server = mp.server
    a = mp.app("a", env)
    epoch_a = a.cmd("start")["epoch"]
    server.control.fault("place_gtt", "accept_then_hold", gate="late")
    call = a.start("broker", method="place_gtt", kwargs=GTT)
    _wait(lambda: any(e["effect_applied"] for e in server.control.log()), what="the GTT to be accepted")
    b = _successor(mp, env, "b", bypass=True)
    assert b.cmd("start")["epoch"] == epoch_a + 1
    server.control.release("late")
    late = a.result(call)
    assert late["sent"] is True and late["result"]["trigger_id"]
    record = a.cmd("fenced_write", marker=f"gtt_{late['result']['trigger_id']}")
    assert record["written"] is False and record["error"] == "FencingError"
    assert rows(env, "SELECT marker FROM mp_markers") == []
    assert a.cmd("status")["role"] == "lost"


def test_MP_D3_effects_follow_arrival_order_while_responses_can_be_delivered_out_of_order(shared):
    mp, env = shared
    server = mp.server
    a = mp.app("a", env)
    a.cmd("simulate_owner", epoch=5, uuid="d3")
    server.control.fault("place_gtt", "accept_then_hold", gate="first")
    first = a.start("broker", method="place_gtt", kwargs=GTT)
    _wait(lambda: server.control.log(), what="the first call")
    from shared_fake_kite import RemoteFakeKite
    second = RemoteFakeKite(server.url, tag="parent").place_gtt(**GTT)
    server.control.release("first")
    first_result = a.result(first)
    log = server.control.log()
    assert [e["pid"] for e in log] == [a.pid, os.getpid()]
    assert log[0]["t_effect"] < log[1]["t_effect"]            # effects in arrival order
    assert log[1]["t_response"] < log[0]["t_response"]        # responses in the other order
    assert first_result["result"]["trigger_id"] < second["trigger_id"]


# ================================================================ W: Windows process and handle semantics
@SKIP_NOT_WINDOWS
def test_W2_the_lock_is_not_inherited_by_a_grandchild_that_outlives_its_parent(shared):
    mp, env = shared
    a = mp.app("a", env)
    a.cmd("start")
    grandchild = a.cmd("spawn_grandchild", seconds=600)["grandchild_pid"]
    mp.track(grandchild)
    assert grandchild in descendants({a.pid})
    a.kill_hard()
    assert pid_alive(grandchild)                              # alive, and yet...
    b = _successor(mp, env, "b")
    assert b.cmd("start")["role"] == "owner"                  # ...the lock was free (O_NOINHERIT)


@SKIP_NOT_WINDOWS
def test_W6_after_teardown_no_process_holds_a_handle_on_the_database_or_the_lock(shared):
    mp, env = shared
    a = mp.app("a", env)
    a.cmd("start")
    a.cmd("fenced_write", marker="w6")
    b = mp.app("b", env)
    assert b.cmd("acquire")["role"] == "non_owner"
    mp.teardown()
    db_path = env.db.DB_PATH
    family = [p for p in (db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm"),
                          db_path.with_name(db_path.name + ".instance.lock")) if p.exists()]
    assert db_path in family and family[-1].name.endswith(".instance.lock")
    for path in family:                                       # Windows refuses to rename a file someone has open
        moved = path.with_name(path.name + ".moved")
        os.replace(path, moved)
        os.replace(moved, path)
