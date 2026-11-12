"""Phase 2: single-instance ownership (backend/instance_lock.py; docs/PHASE_2_DESIGN_REVIEW.md §5, §7, §10).

IDs follow the design's test list (§10.2). Child processes run tests/instance_lock_child.py, which
installs the Phase 0 guard before any backend import. Every database and lock file is under tmp_path;
no broker is contacted (FakeKite only). WIN-INT items that cannot run here are skipped with an explicit
"WIN-INT PENDING" reason and reported as PENDING, never as passed."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import grant_write_authority, release_write_authority

HERE = Path(__file__).resolve().parent
CHILD = HERE / "instance_lock_child.py"


@pytest.fixture
def clean_state():
    from backend import instance_lock
    instance_lock.reset_for_tests()
    yield instance_lock
    instance_lock.reset_for_tests()


def _child_args(env, **extra) -> dict:
    return {"db": str(env.db.DB_PATH), "env_path": str(env.tmp_path / ".env"),
            "key": os.environ["KITE_TERMINAL_ENCRYPTION_KEY"], **extra}


def _spawn(mode: str, args: dict, **kwargs) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(CHILD), mode, json.dumps(args)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)


def _run(mode: str, args: dict, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHILD), mode, json.dumps(args)], capture_output=True, text=True,
                          timeout=timeout)


def _line(proc: subprocess.Popen) -> str:
    return proc.stdout.readline().strip()


def _rows(env, sql, *args):
    conn = env.db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _future(seconds=600):
    return lambda: datetime.now(timezone.utc) + timedelta(seconds=seconds)


# ---------------------------------------------------------------- L: the OS lock

def test_lock_path_is_derived_from_the_database_file(tmp_path):
    from backend import instance_lock
    assert instance_lock.lock_path_for(tmp_path / "x.db") == Path(os.path.realpath(tmp_path)) / "x.db.instance.lock"


def test_one_handle_wins_and_a_second_is_refused_in_process(tmp_path, clean_state):
    il = clean_state
    lock = tmp_path / "a.db.instance.lock"
    first = il.try_lock_file(lock)
    assert first is not None and il.try_lock_file(lock) is None   # even from the same process (per handle)
    il.release_lock_file(first)
    again = il.try_lock_file(lock)
    assert again is not None
    il.release_lock_file(again)


def test_L1_startup_race_exactly_one_owner_in_every_round(tmp_path):
    """N = 2..8 processes released together by a barrier, 200 rounds: exactly one takes the lock.
    The release is an atomic mkdir (racers spin on its existence); everything else goes through pipes, so
    no racer ever reads a file another process is creating (Windows sharing violations)."""
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    db = tmp_path / "race.db"
    db.write_bytes(b"")
    rounds, racers = 200, 8
    procs = [subprocess.Popen([sys.executable, str(CHILD), "race_pipe",
                               json.dumps({"db": str(db), "dir": str(barrier), "index": i})],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              bufsize=1) for i in range(racers)]

    def send(p, text):
        p.stdin.write(text + "\n")
        p.stdin.flush()

    try:
        assert [_line(p) for p in procs] == ["ready"] * racers, [p.stderr.read()[-1500:] for p in procs]
        winners_per_round = []
        for k in range(rounds):
            n = 2 + k % (racers - 1)
            for p in procs:
                send(p, f"round {k} {n}")
            os.mkdir(barrier / f"go_{k}")                       # the simultaneous release
            answers = [_line(p) for p in procs]
            assert all(a.startswith(f"r {k} ") for a in answers), (k, answers)
            winners_per_round.append(sum(a == f"r {k} 1" for a in answers[:n]))
            for p in procs:
                send(p, f"end {k}")
            assert [_line(p) for p in procs] == [f"released {k}"] * racers
        assert winners_per_round == [1] * rounds
    finally:
        for p in procs:
            try:
                p.stdin.close()
                p.wait(timeout=60)
            except (OSError, subprocess.TimeoutExpired):   # after a failure above: never leave racers behind
                p.kill()
                p.wait(timeout=30)
    assert all(p.returncode == 0 for p in procs), [p.stderr.read()[-1500:] for p in procs]


def test_L2_the_lock_is_released_when_the_holder_is_killed(tmp_path, clean_state):
    """os._exit(137) while holding the lock: a new process (here, this one) acquires it within the bound.
    The release timing measured on this Windows machine is recorded in the Phase 2 report."""
    il = clean_state
    db = tmp_path / "crash.db"
    db.write_bytes(b"")
    timings = []
    for _ in range(5):
        out = _run("hold", {"db": str(db), "exit_code": 137})
        assert out.returncode == 137 and out.stdout.strip() == "LOCKED", out.stderr[-1500:]
        started = time.monotonic()
        fd = None
        while fd is None and time.monotonic() - started < 10:
            fd = il.try_lock_file(il.lock_path_for(db))
            if fd is None:
                time.sleep(0.005)
        assert fd is not None, "the lock was not released within 10 s of the holder's death"
        timings.append(time.monotonic() - started)
        il.release_lock_file(fd)
    print(f"L2 release after os._exit(137): max {max(timings) * 1000:.1f} ms over {len(timings)} runs")
    assert max(timings) < 10


def test_L2b_a_second_live_process_is_a_non_owner(tmp_path, clean_state):
    il = clean_state
    db = tmp_path / "held.db"
    db.write_bytes(b"")
    holder = _spawn("hold", {"db": str(db)})
    try:
        assert _line(holder) == "LOCKED"
        assert il.acquire(db) == il.NON_OWNER
        assert il.refusal_code() == il.ANOTHER_INSTANCE_RUNNING and not il.authority_ok()
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
    il.reset_for_tests()
    assert il.acquire(db) == il.LOCKED   # released on the holder's normal exit


@pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")
def test_L3_path_aliases_collide_on_the_same_lock(tmp_path, clean_state):
    """A different-case spelling and a directory junction name the same database, so the same lock file.
    (The subst-drive alias is WIN-INT and PENDING: it needs a system-wide drive mapping.)"""
    il = clean_state
    real = tmp_path / "Real"
    real.mkdir()
    db = real / "alias.db"
    db.write_bytes(b"")
    junction = tmp_path / "Junction"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(real)], capture_output=True, text=True)
    assert made.returncode == 0, made.stderr
    try:
        holder = _spawn("hold", {"db": str(db)})
        try:
            assert _line(holder) == "LOCKED"
            for alias in (Path(str(db).upper()), junction / "alias.db"):
                assert il.lock_path_for(alias) == il.lock_path_for(db)
                il.reset_for_tests()
                assert il.acquire(alias) == il.NON_OWNER, alias
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)
    finally:
        os.rmdir(junction)


@pytest.mark.skip(reason="WIN-INT PENDING: L3 subst-drive alias needs a system-wide drive mapping on a "
                         "controlled Windows machine (docs/PHASE_2_IMPLEMENTATION_REPORT.md, WIN-INT runbook)")
def test_L3_subst_drive_alias_winint():
    pass


def test_L5_the_real_lock_file_is_guarded():
    import real_data_guard
    from backend import db, instance_lock
    real_lock = instance_lock.lock_path_for(db._ACCOUNT_DB_FILE)
    assert real_lock.name == "kite_terminal.db.instance.lock"
    assert real_data_guard.is_protected(real_lock)
    before = len(real_data_guard.refusals())
    with real_data_guard.expected():
        with pytest.raises(real_data_guard.RealAccountDataAccess):
            instance_lock.try_lock_file(real_lock)
    # The audit hook stopped os.open at its "open" event, before the syscall, so O_CREAT never reached the OS.
    # Nothing was created, opened, changed or deleted, whether or not the lock file already existed.
    # No exists()/stat on the protected path: the lock file is meant to persist after release.
    assert real_data_guard.refusals()[before:] == [("open", str(real_lock), False, True)]


@pytest.mark.skipif(os.name != "nt", reason="GetDriveTypeW")
@pytest.mark.parametrize("kind", [0, 1, 2, 4, 5, 6])
def test_L6_a_database_not_on_a_fixed_drive_is_refused(tmp_path, clean_state, monkeypatch, kind):
    il = clean_state
    monkeypatch.setattr(il, "_drive_type", lambda root: kind)
    db = tmp_path / "remote.db"
    assert il.acquire(db) == il.DRIVE_REFUSED
    assert il.refusal_code() == il.DATABASE_NOT_ON_LOCAL_DRIVE and not il.authority_ok()
    assert not il.lock_path_for(db).exists()          # not even the lock file is created


def test_L6_unc_paths_are_refused(clean_state):
    if os.name != "nt":
        pytest.skip("UNC")
    assert clean_state.local_drive_problem(r"\\server\share\kite.db").startswith("the database path is a network")


def test_L6_the_fixed_local_tmp_drive_is_accepted(tmp_path, clean_state):
    assert clean_state.local_drive_problem(tmp_path / "x.db") is None


@pytest.mark.skip(reason="WIN-INT PENDING: L6 against a real network share needs a controlled Windows machine "
                         "with a share (docs/PHASE_2_IMPLEMENTATION_REPORT.md, WIN-INT runbook)")
def test_L6_real_network_share_winint():
    pass


def test_onedrive_folder_is_a_warning_not_a_refusal(tmp_path, clean_state, monkeypatch):
    il = clean_state
    root = tmp_path / "OneDrive"
    root.mkdir()
    monkeypatch.setenv("OneDrive", str(root))
    assert il.sync_root_warning(root / "kite.db")
    assert il.acquire(root / "kite.db") == il.LOCKED and il.status()["warnings"]


# ---------------------------------------------------------------- L4: non-owner mode

def test_L4_a_non_owner_never_runs_init_db_loops_or_the_ticker(env, monkeypatch):
    from backend import instance_lock, main, server, workers
    release_write_authority()
    holder = _spawn("hold", {"db": str(env.db.DB_PATH)})
    try:
        assert _line(holder) == "LOCKED"
        calls = []
        monkeypatch.setattr(server, "enforce_runtime_policy", lambda *a, **k: None)
        monkeypatch.setattr(main, "init_db", lambda: calls.append("init_db"))
        monkeypatch.setattr(workers.supervisor, "start", lambda loops: calls.append(("start", sorted(loops))))
        monkeypatch.setattr(instance_lock, "takeover", lambda **k: calls.append("takeover"))
        mtime = os.stat(env.db.DB_PATH).st_mtime_ns
        asyncio.run(main.on_startup())
        assert calls == [] and instance_lock.status()["role"] == instance_lock.NON_OWNER
        assert os.stat(env.db.DB_PATH).st_mtime_ns == mtime
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)


def test_L4_a_non_owner_serves_a_status_page_and_refuses_everything_else(env, monkeypatch):
    from backend import instance_lock
    release_write_authority()
    holder = _spawn("hold", {"db": str(env.db.DB_PATH)})
    try:
        assert _line(holder) == "LOCKED"
        assert instance_lock.acquire(env.db.DB_PATH) == instance_lock.NON_OWNER
        page = env.client.get("/")
        assert page.status_code == 503 and "read-only" in page.text and "ANOTHER_INSTANCE_RUNNING" in page.text
        health = env.client.get("/api/health")
        assert health.status_code == 503 and health.json()["role"] == "non_owner"
        mark = len(env.kite.calls)
        for method, path in [("get", "/login"), ("get", "/callback?request_token=x&status=success"),
                             ("post", "/api/logout"), ("get", "/api/tickets"), ("get", "/api/diagnostics"),
                             ("post", "/api/kill-switch/emergency-exit-confirm"), ("post", "/api/tickets")]:
            r = getattr(env.client, method)(path, json={}) if method == "post" else env.client.get(path)
            assert r.status_code == 503, (path, r.status_code, r.text[:200])
            assert r.json()["detail"]["error_code"] == "ANOTHER_INSTANCE_RUNNING"
        assert env.kite.calls[mark:] == []
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)


def test_startup_takes_the_lock_before_init_db_and_persists_restricted_before_loops(env, monkeypatch):
    """Mutation targets: 'lock not taken before init_db', 'RESTRICTED not persisted before the loops start'."""
    from backend import instance_lock, main, server, workers
    release_write_authority()                    # a crash: app_instance still says 'owner'...
    conn = env.db.get_connection()
    conn.execute("UPDATE app_instance SET heartbeat_at = '2000-01-01T00:00:00+00:00'")   # ...with an old heartbeat
    conn.commit()
    conn.close()
    order = []
    real_acquire, real_takeover = instance_lock.acquire, instance_lock.takeover
    monkeypatch.setattr(server, "enforce_runtime_policy", lambda *a, **k: None)
    monkeypatch.setattr(instance_lock, "acquire", lambda p: order.append("acquire") or real_acquire(p))
    monkeypatch.setattr(main, "init_db", lambda: order.append("init_db") or env.db.init_db())

    def takeover(**k):
        result = real_takeover(**k)
        restriction = _rows(env, "SELECT restriction_state, restriction_reasons FROM account_restriction")[0]
        order.append(("takeover", restriction["restriction_state"],
                      json.loads(restriction["restriction_reasons"])))
        return result

    monkeypatch.setattr(instance_lock, "takeover", takeover)
    monkeypatch.setattr(workers.supervisor, "start", lambda loops: order.append(("loops", sorted(loops))))
    monkeypatch.setattr(main.automation, "is_due", lambda at=None: True)
    asyncio.run(main.on_startup())
    assert order[:3] == ["acquire", "init_db",
                         ("takeover", "RESTRICTED", ["STARTUP_RECONCILIATION_INCOMPLETE", "PREVIOUS_INSTANCE_UNCLEAN"])]
    assert order[3][0] == "loops" and ("loops", ["instance_heartbeat"]) in order
    assert env.stage1_requests == ["startup"]
    instance_lock.finish_shutdown(timeout=0)


# ---------------------------------------------------------------- E: the fencing epoch

def test_E1_the_epoch_increments_by_one_per_takeover_and_is_never_reused(env):
    from backend import instance_lock
    seen = []
    for _ in range(100):
        instance_lock.finish_shutdown(timeout=0)      # a clean stop, then the next start
        grant_write_authority(env.db)
        seen.append(instance_lock.owner_epoch())
    assert seen == list(range(2, 102))
    history = _rows(env, "SELECT epoch, instance_uuid, end_state FROM app_instance_history ORDER BY epoch")
    assert [h["epoch"] for h in history] == list(range(1, 102))
    assert len({h["instance_uuid"] for h in history}) == 101
    assert all(h["end_state"] == "stopped_clean" for h in history[:-1]) and history[-1]["end_state"] == "running"


def test_E2_a_stale_owner_loses_authority_and_its_broker_writes_are_refused(env):
    """In-process: another epoch appears (a takeover through a lock bypass). The next heartbeat drops
    authority BEFORE anything else: routes 503, the fence refuses (FakeKite sees nothing), loops stop,
    fenced transactions are refused, and no shutdown record is written."""
    from backend import instance_lock, startup_reconciliation
    conn = env.db.get_connection()
    conn.execute("UPDATE app_instance SET epoch = epoch + 1, instance_uuid = 'someone-else'")
    conn.commit()
    conn.close()
    assert instance_lock.heartbeat_once()["lost"] is True
    assert instance_lock.refusal_code() == instance_lock.INSTANCE_LOCK_LOST
    mark = len(env.kite.calls)
    with pytest.raises(instance_lock.InstanceNotOwner) as refused:
        env.kite.place_gtt(trigger_type="single", tradingsymbol="TCS", exchange="NSE", trigger_values=[1.0],
                           last_price=2.0, orders=[])
    assert refused.value.reason == "INSTANCE_LOCK_LOST" and env.kite.calls[mark:] == []
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    assert r.status_code == 503 and r.json()["detail"]["error_code"] == "INSTANCE_LOCK_LOST"
    assert env.client.get("/api/tickets").status_code == 200          # reads still work
    assert not instance_lock.loop_may_run("order_monitor")
    assert startup_reconciliation.run_stage1("startup")["skipped"] == "INSTANCE_LOCK_LOST"
    conn = env.db.get_connection()
    try:
        with pytest.raises(instance_lock.FencingError):
            with instance_lock.fenced_transaction(conn):
                pass
    finally:
        conn.close()
    assert instance_lock.finish_shutdown(timeout=0)["recorded"] is False
    assert _rows(env, "SELECT instance_uuid FROM app_instance")[0]["instance_uuid"] == "someone-else"


def test_E2_P1_two_processes_through_a_bypassed_lock_at_most_one_writes(env, tmp_path):
    """Subprocesses: A owns the database; B becomes owner through a DIFFERENT lock file (a simulated
    bypass: network folder, older build). The epochs serialise; A's next heartbeat finds B's epoch and
    A's broker write is refused (its FakeKite records nothing); B's write is allowed. At most one
    process can send a broker write. (The legacy, unfenced DB writes of a cycle already past its
    authority check are the disclosed residual, design §7.3.)"""
    release_write_authority()
    status_a, stop_a, go_a = tmp_path / "a.json", tmp_path / "a.stop", tmp_path / "a.go"
    a = _spawn("owner", _child_args(env, status=str(status_a), stop=str(stop_a), seconds=60, wait_file=str(go_a),
                                    clock_shift=3600))
    try:
        assert json.loads(_line(a))["epoch"] == 2
        b = _run("owner", _child_args(env, status=str(tmp_path / "b.json"), stop=str(tmp_path / "b.stop"),
                                      seconds=0.3, bypass_lock_path=str(tmp_path / "bypass.lock"),
                                      clock_shift=3600), timeout=120)
        lines = [json.loads(x) for x in b.stdout.splitlines() if x.startswith("{")]
        assert lines[0]["epoch"] == 3, b.stderr[-2000:]
        assert lines[-1]["result"] == "sent" and lines[-1]["fake_place_order_calls"] == 1
        go_a.write_text("1")                      # A resumes heartbeating: it finds B's epoch
        a_write = json.loads(_line(a))
        assert a_write["result"] == "refused:INSTANCE_LOCK_LOST" and a_write["fake_place_order_calls"] == 0
        assert a_write["role"] == "lost"
    finally:
        stop_a.write_text("1")
        a.wait(timeout=60)


def test_E3_the_successor_waits_out_the_lease_of_an_unclean_predecessor(env, monkeypatch):
    from backend import instance_lock
    instance_lock.finish_shutdown(timeout=0)
    conn = env.db.get_connection()
    now = datetime.now(timezone.utc)
    conn.execute("UPDATE app_instance SET state = 'owner', clean_shutdown = 0, heartbeat_at = ?, heartbeat_seq = 7, "
                 "lease_seconds = 15", (now.isoformat(),))
    conn.execute("UPDATE app_instance_history SET end_state = 'running', ended_at = NULL WHERE epoch = 1")  # it crashed
    conn.commit()
    conn.close()
    clock = {"t": 1000.0}
    slept = []

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    assert instance_lock.acquire(env.db.DB_PATH) == instance_lock.LOCKED
    result = instance_lock.takeover(now=lambda: now, sleep=sleep, monotonic=lambda: clock["t"], poll_seconds=2)
    assert not result["conflict"] and result["previous_unclean"] is True and result["previous_end"] == "crashed"
    assert result["waited_seconds"] >= 15 and sum(slept) >= 15
    reasons = json.loads(_rows(env, "SELECT restriction_reasons FROM account_restriction")[0]["restriction_reasons"])
    assert reasons == ["STARTUP_RECONCILIATION_INCOMPLETE", "PREVIOUS_INSTANCE_UNCLEAN"]
    assert _rows(env, "SELECT end_state FROM app_instance_history WHERE epoch = 1")[0]["end_state"] == "crashed_detected"


def test_E3_an_advancing_foreign_heartbeat_is_an_instance_conflict(env):
    from backend import instance_lock
    instance_lock.finish_shutdown(timeout=0)
    conn = env.db.get_connection()
    now = datetime.now(timezone.utc)
    conn.execute("UPDATE app_instance SET state = 'owner', clean_shutdown = 0, heartbeat_at = ?, heartbeat_seq = 7",
                 (now.isoformat(),))
    conn.commit()
    clock = {"t": 0.0}

    def sleep(s):  # while we wait, a foreign writer heartbeats: the OS lock does not see it
        clock["t"] += s
        conn.execute("UPDATE app_instance SET heartbeat_seq = heartbeat_seq + 1")
        conn.commit()

    epoch_before = _rows(env, "SELECT epoch FROM app_instance")[0]["epoch"]
    assert instance_lock.acquire(env.db.DB_PATH) == instance_lock.LOCKED
    result = instance_lock.takeover(now=lambda: now, sleep=sleep, monotonic=lambda: clock["t"], poll_seconds=1)
    conn.close()
    assert result["conflict"] is True
    assert instance_lock.status()["role"] == instance_lock.CONFLICT
    assert instance_lock.refusal_code() == instance_lock.INSTANCE_CONFLICT and not instance_lock.authority_ok()
    assert _rows(env, "SELECT epoch FROM app_instance")[0]["epoch"] == epoch_before   # nothing written
    assert env.client.get("/").status_code == 503                                      # read-only status page


def test_E3_a_clean_predecessor_is_taken_over_without_waiting(env):
    from backend import instance_lock
    instance_lock.finish_shutdown(timeout=0)
    started = time.monotonic()
    grant_write_authority(env.db)
    assert time.monotonic() - started < 5
    assert instance_lock.status()["previous_instance_unclean"] is False


def test_E4_pid_reuse_is_reported_as_a_different_process_and_never_decides_liveness(env):
    from backend import instance_lock
    me = os.getpid()
    assert instance_lock.describe_process(me, instance_lock.process_created_at()) == "the same process is still running"
    assert instance_lock.describe_process(me, "1999-01-01T00:00:00+00:00").startswith("a different process")
    # A crashed predecessor recorded with OUR pid (reused): liveness is the OS lock, so we take over.
    instance_lock.finish_shutdown(timeout=0)
    conn = env.db.get_connection()
    conn.execute("UPDATE app_instance SET state = 'owner', clean_shutdown = 0, pid = ?, process_created_at = "
                 "'1999-01-01T00:00:00+00:00'", (me,))
    conn.commit()
    conn.close()
    assert instance_lock.acquire(env.db.DB_PATH) == instance_lock.LOCKED
    assert instance_lock.takeover(now=_future(), poll_seconds=0.01)["previous_unclean"] is True


# ---------------------------------------------------------------- S: SQLite contention

def test_S1_fenced_transactions_under_contention_never_lose_an_update(env, tmp_path):
    release_write_authority()
    conn = env.db.get_connection()
    conn.execute("CREATE TABLE s1_counter (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)")
    conn.execute("INSERT INTO s1_counter VALUES (1, 0)")
    conn.execute("UPDATE app_instance SET epoch = 9, instance_uuid = 's1'")
    conn.commit()
    conn.close()
    procs = [_spawn("contend", _child_args(env, epoch=9, uuid="s1", threads=4, iterations=25)) for _ in range(3)]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=300)
        assert p.returncode == 0, err[-2000:]
        results.append(json.loads(out.strip().splitlines()[-1]))
    done = sum(r["done"] for r in results)
    assert done + sum(r["busy"] for r in results) == 3 * 4 * 25
    assert _rows(env, "SELECT value FROM s1_counter")[0]["value"] == done   # every committed claim counted once


def test_S2_a_deferred_upgrade_fails_without_waiting_and_begin_immediate_does_not(env):
    path = str(env.db.DB_PATH)
    a, b = sqlite3.connect(path, timeout=5, isolation_level=None), sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        a.execute("BEGIN")                                     # deferred: a read snapshot
        a.execute("SELECT COUNT(*) FROM accounts").fetchone()
        b.execute("UPDATE app_instance SET heartbeat_seq = heartbeat_seq + 1")   # another connection commits
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            a.execute("UPDATE app_instance SET heartbeat_seq = heartbeat_seq + 100")   # SQLITE_BUSY_SNAPSHOT
        assert time.monotonic() - started < 2                  # no busy wait helps a stale snapshot
        a.execute("ROLLBACK")
        a.execute("BEGIN IMMEDIATE")                           # the write lock up front
        a.execute("SELECT COUNT(*) FROM accounts").fetchone()
        a.execute("UPDATE app_instance SET heartbeat_seq = heartbeat_seq + 100")
        a.execute("COMMIT")
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------- C: crashes at named boundaries, shutdown

@pytest.mark.parametrize("boundary", ["before_lock", "after_lock", "after_takeover", "mid_fenced_transaction",
                                      "after_claim_before_broker", "after_claim_after_broker"])
def test_C1_crash_at_a_boundary_then_restart(env, tmp_path, boundary):
    from backend import instance_lock, startup_reconciliation
    instance_lock.finish_shutdown(timeout=0)             # env's process stops cleanly first
    broker_log = tmp_path / "broker.json"
    out = _run("startup", _child_args(env, boundary=boundary, broker_log=str(broker_log)))
    assert out.returncode == 137, out.stderr[-2000:]
    epoch_after_child = _rows(env, "SELECT epoch FROM app_instance")[0]["epoch"]
    assert instance_lock.acquire(env.db.DB_PATH) == instance_lock.LOCKED        # the OS released it
    env.db.init_db()
    result = instance_lock.takeover(now=_future(), poll_seconds=0.01)
    assert result["epoch"] == epoch_after_child + 1
    reasons = json.loads(_rows(env, "SELECT restriction_reasons FROM account_restriction")[0]["restriction_reasons"])
    crashed_as_owner = boundary not in ("before_lock", "after_lock")
    assert result["previous_unclean"] is crashed_as_owner
    assert reasons == ["STARTUP_RECONCILIATION_INCOMPLETE"] + (["PREVIOUS_INSTANCE_UNCLEAN"] if crashed_as_owner else [])
    if crashed_as_owner:
        history = _rows(env, "SELECT end_state FROM app_instance_history WHERE epoch = ?", epoch_after_child)
        assert history[0]["end_state"] == "crashed_detected"
    conn = env.db.get_connection()
    try:
        derived = startup_reconciliation.derive_restriction(conn, 1)
    finally:
        conn.close()
    assert derived["state"] == "RESTRICTED"                                     # never inherits NORMAL
    if boundary == "mid_fenced_transaction":
        assert "FROM_THE_CRASHED_PROCESS" not in _rows(env, "SELECT restriction_reasons FROM account_restriction")[0][
            "restriction_reasons"]                                              # rolled back
    if boundary.startswith("after_claim"):
        # The predecessor's claim is unresolved, even though it is recent; Stage 1 never re-sends it.
        assert "UNRESOLVED_OPERATION" in derived["reasons"]
        mark = len(env.kite.calls_to("place_order"))
        startup_reconciliation.run_stage1("startup")
        assert len(env.kite.calls_to("place_order")) == mark
        assert _rows(env, "SELECT submission_state FROM trade_tickets WHERE order_tag = 'ktcrash1'")[0][
            "submission_state"] == "submitting"


def test_C2_a_clean_shutdown_is_recorded_as_clean(env):
    from backend import instance_lock
    epoch = instance_lock.owner_epoch()
    instance_lock.begin_shutdown()
    with pytest.raises(instance_lock.InstanceNotOwner):              # refused from the first step
        env.kite.cancel_order(variety="regular", order_id="1")
    assert env.client.post("/api/logout").status_code == 503
    assert instance_lock.finish_shutdown(timeout=1) == {"recorded": True, "in_flight": 0}
    row = _rows(env, "SELECT state, clean_shutdown, shutdown_at, shutdown_note FROM app_instance")[0]
    assert row["state"] == "stopped_clean" and row["clean_shutdown"] == 1 and row["shutdown_note"] is None
    assert _rows(env, "SELECT end_state FROM app_instance_history WHERE epoch = ?", epoch)[0]["end_state"] == \
        "stopped_clean"


def test_C2_a_write_in_flight_at_shutdown_is_recorded_as_unclean(env):
    from backend import instance_lock
    inside, release = threading.Event(), threading.Event()

    def slow(kwargs):
        inside.set()
        release.wait(10)

    env.kite.on_call("place_order", slow)
    writer = threading.Thread(target=lambda: env.kite.place_order(
        variety="regular", exchange="NSE", tradingsymbol="TCS", transaction_type="SELL", quantity=1, product="CNC",
        order_type="MARKET"))
    writer.start()
    assert inside.wait(10)
    assert instance_lock.status()["broker_writes_in_flight"] == 1
    instance_lock.begin_shutdown()
    result = instance_lock.finish_shutdown(timeout=0.2)
    release.set()
    writer.join(10)
    assert result == {"recorded": True, "in_flight": 1}
    row = _rows(env, "SELECT state, clean_shutdown, shutdown_note FROM app_instance")[0]
    assert row["clean_shutdown"] == 0 and "1 broker write(s) still in flight" in row["shutdown_note"]
    # The successor treats it as unclean (and would wait out the lease).
    assert instance_lock.acquire(env.db.DB_PATH) == instance_lock.LOCKED
    took = instance_lock.takeover(now=_future(), poll_seconds=0.01)
    assert took["previous_unclean"] is True and took["previous_end"] == "stopped_with_write_in_flight"


# ---------------------------------------------------------------- heartbeat, suspend, authority loss

def test_heartbeat_advances_the_sequence_under_the_fence(env):
    from backend import instance_lock
    for expected in (1, 2, 3):
        assert instance_lock.heartbeat_once()["ok"] is True
        assert _rows(env, "SELECT heartbeat_seq FROM app_instance")[0]["heartbeat_seq"] == expected


def test_heartbeat_with_our_identity_but_a_foreign_sequence_is_a_conflict(env):
    from backend import instance_lock
    conn = env.db.get_connection()
    conn.execute("UPDATE app_instance SET heartbeat_seq = 99")
    conn.commit()
    conn.close()
    assert instance_lock.heartbeat_once()["lost"] is True
    assert instance_lock.refusal_code() == instance_lock.INSTANCE_CONFLICT


def test_heartbeat_failure_becomes_heartbeat_stale_after_the_lease_and_clears(env, monkeypatch):
    from backend import config, instance_lock, startup_reconciliation
    real_connect = instance_lock._connect
    clock = {"t": time.monotonic()}
    monkeypatch.setattr(instance_lock, "_connect", lambda p: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    wall = datetime.now(timezone.utc)
    for step in range(6):   # every 15 s, past the 60 s lease
        clock["t"] += 15
        result = instance_lock.heartbeat_once(now=lambda: wall + timedelta(seconds=15 * (step + 1)),
                                              monotonic=lambda: clock["t"])
        assert result["ok"] is False and result["lost"] is False     # a missed heartbeat never loses ownership
    assert instance_lock.HEARTBEAT_STALE in instance_lock.process_reasons() and instance_lock.authority_ok()
    conn = env.db.get_connection()
    try:
        assert "HEARTBEAT_STALE" in startup_reconciliation.derive_restriction(conn, 1)["reasons"]
    finally:
        conn.close()
    monkeypatch.setattr(instance_lock, "_connect", real_connect)
    clock["t"] += 15
    assert instance_lock.heartbeat_once(now=lambda: wall + timedelta(seconds=105), monotonic=lambda: clock["t"])["ok"]
    assert instance_lock.HEARTBEAT_STALE not in instance_lock.process_reasons()
    assert config.INSTANCE_LEASE_SECONDS == 60


@pytest.mark.parametrize("jump", ["wall", "monotonic"])
def test_R8_a_clock_jump_is_a_resume_that_restricts_until_stage1_passes_again(env, monkeypatch, jump):
    from backend import gtt_observations, instance_lock, startup_reconciliation
    gtt_observations.record_book([{"id": 5, "status": "active"}])
    wall, mono = datetime.now(timezone.utc), time.monotonic()
    # The previous heartbeat, 300 s ago on one clock (15 s on the other): the process was suspended.
    instance_lock._state.last_hb_wall = wall - timedelta(seconds=300 if jump == "wall" else 15)
    instance_lock._state.last_hb_mono = mono - (300 if jump == "monotonic" else 15)
    result = instance_lock.heartbeat_once(now=lambda: wall, monotonic=lambda: mono)
    assert result["resumed"] is True and result["ok"] is True
    assert gtt_observations.get(5) is None                                  # observations are stale now
    startup_reconciliation.note_resume()
    assert env.stage1_requests == ["resume"]
    conn = env.db.get_connection()
    try:
        derived = startup_reconciliation.derive_restriction(conn, 1)
    finally:
        conn.close()
    assert derived["state"] == "RESTRICTED" and "RESUMED_AFTER_SUSPEND" in derived["reasons"]
    assert startup_reconciliation.run_stage1("resume")["outcome"] == "passed"
    conn = env.db.get_connection()
    try:
        derived = startup_reconciliation.derive_restriction(conn, 1)
    finally:
        conn.close()
    assert derived["state"] == "NORMAL", derived


@pytest.mark.skip(reason="WIN-INT PENDING: R8 with a real sleep/hibernate needs a controlled Windows machine "
                         "(docs/PHASE_2_IMPLEMENTATION_REPORT.md, WIN-INT runbook)")
def test_R8_real_sleep_and_hibernate_winint():
    pass


def test_loops_stop_at_the_start_of_a_cycle_without_authority(clean_state, monkeypatch):
    from backend import automation, data_jobs, order_monitor, reconciliation, trailing_engine
    ran = []
    monkeypatch.setattr(order_monitor, "check_pending_orders", lambda *a, **k: ran.append("om"))
    monkeypatch.setattr(trailing_engine, "_tick_sync", lambda *a, **k: ran.append("tr"))
    monkeypatch.setattr(reconciliation, "_run_cycle", lambda *a, **k: ran.append("rc"))
    monkeypatch.setattr(automation, "_mark_interrupted_runs", lambda *a, **k: ran.append("au"))

    async def scenario():
        for loop in (order_monitor.run_order_monitor_loop(), trailing_engine.run_trailing_loop(),
                     reconciliation.run_reconciliation_loop(), automation.run_automation_loop()):
            await asyncio.wait_for(loop, timeout=5)      # each returns at once: no authority
        queue = data_jobs._queue()
        queue.put_nowait(("refresh", 1, "TCS"))
        await asyncio.wait_for(data_jobs.run_job_worker(), timeout=5)

    asyncio.run(scenario())
    assert ran == []


def test_the_heartbeat_loop_stops_without_authority(clean_state, monkeypatch):
    from backend import config, instance_lock
    monkeypatch.setattr(config, "HEARTBEAT_SECONDS", 0)
    asyncio.run(asyncio.wait_for(instance_lock.run_heartbeat_loop(), timeout=5))
