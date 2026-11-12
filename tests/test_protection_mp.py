"""Phase 3 S9: the protection engine across real processes (tests/mp_app_child.py engine commands) on env's tmp
database and ONE shared FakeKite (tests/shared_fake_kite.py), with real OS locks, real fencing and real crashes.

What these tests do NOT prove: anything about Kite (the shared FakeKite is a model); the absence of races
(repetition gives statistical evidence only); real suspend/hibernate wake ordering (WIN-INT)."""

import time
import uuid

import pytest

from mp_harness import mp, prepare_app_db, rows  # noqa: F401  (mp is a fixture)
from protection_helpers import journal, make_position, request

ENGINE_SCALE = {"HEARTBEAT_SECONDS": 15, "PROTECTION_INFLIGHT_GRACE_SECONDS": 0.3}
REQ = request(10)


@pytest.fixture
def eng(mp, env):
    prepare_app_db(env)
    journal(env)
    mp.start_server()
    return mp, env


def _app(mp, env, name, **kw):
    return mp.app(name, env, scale=ENGINE_SCALE, **kw)


def _successor(mp, env, name, shift=3600, **kw):
    child = _app(mp, env, name, **kw)
    child.cmd("set_clock", wall=shift)
    return child


def _writes(mp, pid=None, method=None):
    return mp.server.control.writes(method=method, pid=pid)


def _op(env, op_id):
    return rows(env, "SELECT * FROM protection_operations WHERE id = ?", op_id)[0]


def test_MP_PE1_a_stale_owner_can_neither_claim_nor_send_after_a_bypass_takeover(eng):
    mp, env = eng
    pid = make_position(env)
    a = _app(mp, env, "a")
    epoch_a = a.cmd("start")["epoch"]
    claimed = a.cmd("engine_claim", position_id=pid, request=REQ)
    assert claimed["claimed"] is True
    b = _successor(mp, env, "b", bypass=True)
    assert b.cmd("start")["epoch"] == epoch_a + 1
    sent = a.cmd("engine_send", op_id=claimed["op_id"])
    assert sent["sent"] is False and sent["code"] == "FencingError"
    assert a.cmd("engine_claim", position_id=pid, request=REQ)["error"] == "FencingError"
    assert _writes(mp, pid=a.pid) == [] and _op(env, claimed["op_id"])["state"] == "intent"


def test_MP_PE2_no_takeover_can_commit_while_a_send_is_in_progress(eng):
    """Authorisation is atomic with respect to ownership: the successor's takeover cannot commit while the owner
    holds its fenced send transaction, and the send that was authorised completes under the owner's epoch."""
    mp, env = eng
    pid = make_position(env)
    a = _app(mp, env, "a")
    epoch_a = a.cmd("start")["epoch"]
    op_id = a.cmd("engine_claim", position_id=pid, request=REQ)["op_id"]
    send = a.start("engine_send", op_id=op_id, park_in_call="in_call")
    a.wait_event("parked")
    b = _successor(mp, env, "b", bypass=True)
    refused = b.cmd("start", check=False)
    assert refused["ok"] is False and "database is locked" in refused["error"]
    mp.release("in_call")
    done = a.result(send)
    assert done["sent"] is True and done["code"] == "ACKNOWLEDGED"
    [write] = _writes(mp, method="place_gtt")
    assert write["pid"] == a.pid and write["epoch"] == epoch_a
    b.cmd("reset")
    b.cmd("set_clock", wall=7200)
    assert b.cmd("start")["epoch"] == epoch_a + 1                  # after the send, the takeover commits
    assert a.cmd("engine_claim", position_id=make_position(env, symbol="INFY", token=408065),
                 request=request(symbol="INFY"))["error"] == "FencingError"
    assert len(_writes(mp)) == 1


@pytest.mark.parametrize("fault", ["accept_then_raise", "accept_then_drop"], ids=["crash", "dropped"])
def test_MP_PE3_crash_or_lost_response_after_the_broker_effect_the_successor_adopts_never_resends(eng, fault):
    mp, env = eng
    pid = make_position(env)
    a = _app(mp, env, "a", timeout=2.0)
    a.cmd("start")
    op_id = a.cmd("engine_claim", position_id=pid, request=REQ)["op_id"]
    if fault == "accept_then_raise":
        mp.server.control.fault("place_gtt", "accept_then_raise", exc={"type": "SimulatedCrash", "message": "died"})
        a.start("engine_send", op_id=op_id)
        assert a.wait_exit() == 137                                # died after the broker took effect
        assert _op(env, op_id)["state"] == "sending"
    else:
        mp.server.control.fault("place_gtt", "accept_then_drop")
        assert a.cmd("engine_send", op_id=op_id)["code"] == "UNKNOWN"   # ambiguous: never re-sent
        assert a.kill_hard() == 137
    b = _successor(mp, env, "b")
    assert b.cmd("start")["role"] == "owner"
    out = b.cmd("engine_recover")
    assert any(a_["action"] == "ADOPTED" for a_ in out["actions"]), out
    assert len(_writes(mp, method="place_gtt")) == 1 and _writes(mp, pid=b.pid) == []
    row = _op(env, op_id)
    assert row["state"] == "adopted"
    assert rows(env, "SELECT kite_gtt_id FROM trade_tickets WHERE id = ?", pid)[0]["kite_gtt_id"] == row["result_gtt_id"]


def test_MP_PE4_a_held_response_released_to_a_dead_owner_is_resolved_by_reads(eng):
    mp, env = eng
    pid = make_position(env)
    a = _app(mp, env, "a")
    a.cmd("start")
    op_id = a.cmd("engine_claim", position_id=pid, request=REQ)["op_id"]
    mp.server.control.fault("place_gtt", "accept_then_hold", gate="hold")
    a.start("engine_send", op_id=op_id)
    deadline = time.monotonic() + 20
    while not any(e["effect_applied"] for e in mp.server.control.log()) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert a.kill_hard() == 137                                     # dies holding its send transaction
    mp.server.control.release("hold")                               # the late response reaches nobody
    b = _successor(mp, env, "b")
    assert b.cmd("start")["role"] == "owner"
    out = b.cmd("engine_recover")
    assert out["recovered"]["sending_to_unknown"] == [op_id]
    assert any(x["action"] == "ADOPTED" for x in out["actions"])
    assert len(_writes(mp, method="place_gtt")) == 1 and _writes(mp, pid=b.pid) == []


def test_MP_PE5_repeated_claim_races_between_processes_yield_one_op_per_position(eng):
    mp, env = eng
    a, b = _app(mp, env, "a"), _app(mp, env, "b")
    ident = {"epoch": 1, "uuid": str(uuid.uuid4())}
    for c in (a, b):
        c.cmd("simulate_owner", **ident)
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE app_instance SET epoch = ?, instance_uuid = ?, started_at = '2000-01-01T00:00:00+00:00' "
                     "WHERE id = 1", (ident["epoch"], ident["uuid"]))
        conn.commit()
    finally:
        conn.close()
    for k in range(10):
        pid = make_position(env, symbol=f"S{k}", token=900000 + k)
        req = request(10, symbol=f"S{k}")
        ids = {c: c.start("engine_claim", position_id=pid, request=req) for c in (a, b)}
        results = [c.result(i) for c, i in ids.items()]
        assert sum(r["claimed"] for r in results) == 1, results
    assert rows(env, "SELECT COUNT(*) AS n FROM protection_operations")[0]["n"] == 10


def test_MP_PE6_concurrent_emergency_exit_clicks_in_two_processes_sell_at_most_once(eng):
    mp, env = eng
    pid = make_position(env)
    a, b = _app(mp, env, "a"), _app(mp, env, "b")
    ident = {"epoch": 1, "uuid": str(uuid.uuid4())}
    for c in (a, b):
        c.cmd("simulate_owner", **ident)
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE app_instance SET epoch = ?, instance_uuid = ?, started_at = '2000-01-01T00:00:00+00:00' "
                     "WHERE id = 1", (ident["epoch"], ident["uuid"]))
        conn.commit()
    finally:
        conn.close()
    ids = {c: c.start("engine_ee", position_id=pid, park="go") for c in (a, b)}
    for c in (a, b):
        c.wait_event("parked")
    mp.release("go")
    results = [c.result(i) for c, i in ids.items()]
    assert sum(r["outcome"] == "sell_submitted" for r in results) == 1, results
    assert len([w for w in _writes(mp, method="place_order")]) == 1
