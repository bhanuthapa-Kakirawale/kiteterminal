"""Phase 3 S3 harness self-tests (H1-H16): the exit gate of RR §13 S3.

They prove the multi-process harness itself: one shared broker for several processes, serial and
complete call logging, faithful exception and ambiguity modelling, gates that cannot deadlock, the
Phase 0 guard installed and reporting in every child, and cleanup that leaves no process behind.
Every database, lock and state file is under tmp_path; the only network is loopback; no broker is
contacted. FakeKite results are models, not evidence of Kite behaviour.
"""

import ast
import json
import os
import subprocess
import sys
import threading
import time

import pytest
import requests
from kiteconnect.exceptions import DataException, InputException

import mp_harness
import real_data_guard
from mp_harness import mp, pid_alive, prepare_app_db  # noqa: F401  (mp is a fixture)
from shared_fake_kite import EXCEPTIONS, HarnessError, RemoteFakeKite, WRITE_METHODS

LEG = [{"transaction_type": "SELL", "quantity": 10, "order_type": "LIMIT", "product": "CNC", "price": 95.0}]
GTT = {"trigger_type": "single", "tradingsymbol": "TCS", "exchange": "NSE", "trigger_values": [96.0],
       "last_price": 100.0, "orders": LEG}


def _owner_app(mp, env, name):
    """An application child with a simulated owner identity: broker writes pass its fence; no DB write."""
    child = mp.app(name, env)
    child.cmd("simulate_owner", epoch=7, uuid=f"selftest-{name}")
    return child


# ---------------------------------------------------------------- H1, H14: the server's lifetime
def test_H1_the_server_binds_loopback_only(mp):
    server = mp.start_server()
    assert server.host == "127.0.0.1"
    assert server.control("ping")["pid"] == server.pid


def test_H1_a_non_loopback_bind_is_refused(mp):
    config = mp.root / "bad_server.json"
    config.write_text(json.dumps({"root": str(mp.root), "host": "0.0.0.0"}), encoding="utf-8")
    bad = mp.child("bad_server", [sys.executable, str(mp_harness.SERVER_SCRIPT), str(config)], "server")
    assert bad.wait_exit(30) == 2
    assert any("binds loopback only" in line for line in bad.stderr_tail), bad.diag()


def test_H14_the_server_and_an_app_child_exit_when_their_pipe_closes(mp, env):
    prepare_app_db(env)
    server = mp.start_server()
    app = mp.app("a", env)
    app.proc.stdin.close()
    assert app.wait_exit(20) == 0
    server.proc.stdin.close()
    assert server.wait_exit(20) == 0


# ---------------------------------------------------------------- H2, H3: one book, a complete serial log
def test_H2_two_processes_see_one_book(mp, env):
    prepare_app_db(env)
    server = mp.start_server()
    a = _owner_app(mp, env, "a")
    placed = a.cmd("broker", method="place_gtt", kwargs=GTT)
    assert placed["sent"] is True
    gid = placed["result"]["trigger_id"]
    here = RemoteFakeKite(server.url, tag="parent")           # a second process: the test itself
    assert [g["id"] for g in here.get_gtts()] == [gid]
    log = server.control.log()
    assert [e["pid"] for e in log] == [a.pid, os.getpid()]
    assert [e["seq"] for e in log] == [1, 2] and log[0]["write"] and log[0]["effect_applied"]
    assert log[0]["epoch"] == 7 and log[0]["tag"] == "a"


def test_H3_concurrent_writes_from_three_processes_are_serialised_and_all_logged(mp, env):
    prepare_app_db(env)
    server = mp.start_server()
    apps = [_owner_app(mp, env, f"p{i}") for i in range(3)]
    started = [a.start("broker_burst", method="place_gtt", kwargs=GTT, threads=4, count=25) for a in apps]
    results = [a.result(cid, timeout=120) for a, cid in zip(apps, started)]
    ids = [r["trigger_id"] for res in results for r in res["results"]]
    assert all(res["errors"] == [] for res in results), results
    assert len(ids) == 300 and len(set(ids)) == 300
    log = server.control.log()
    assert len(log) == 300 and [e["seq"] for e in log] == list(range(1, 301))
    assert {e["pid"] for e in log} == {a.pid for a in apps}
    # The log is serial: its order is the order of the effects (FakeKite allocates GTT ids in effect order).
    created = [e["result"]["trigger_id"] for e in sorted(log, key=lambda e: e["seq"])]
    assert created == sorted(created) and len(set(created)) == 300
    effect_times = [e["t_effect"] for e in sorted(log, key=lambda e: e["seq"])]
    assert effect_times == sorted(effect_times)
    assert len(server.control.state()["gtt_book"]) == 300


def test_H3b_the_seq_and_effect_section_admits_one_request_at_a_time_even_when_slow(mp, env):
    """A deterministic check of the server lock (H3 alone cannot catch its removal: with fast calls the GIL
    rarely interleaves them). A 20 ms sleep inside the section makes overlap all but certain without it."""
    prepare_app_db(env)
    server = mp.start_server()
    server.control("probe", delay=0.02)
    apps = [_owner_app(mp, env, f"p{i}") for i in range(3)]
    started = [a.start("broker_burst", method="place_gtt", kwargs=GTT, threads=4, count=3) for a in apps]
    results = [a.result(cid, timeout=120) for a, cid in zip(apps, started)]
    assert all(res["errors"] == [] for res in results), results
    assert server.control("probe")["max_inside"] == 1          # never two requests inside at once
    log = sorted(server.control.log(), key=lambda e: e["seq"])
    created = [e["result"]["trigger_id"] for e in log]
    assert len(created) == 36 and created == sorted(created)
    assert [e["t_effect"] for e in log] == sorted(e["t_effect"] for e in log)


# ---------------------------------------------------------------- H4: exceptions cross the boundary faithfully
@pytest.mark.parametrize("name", sorted(n for n in EXCEPTIONS if n != "SimulatedCrash"))
def test_H4_every_allow_listed_exception_round_trips(mp, name):
    server = mp.start_server()
    server.control.fault("get_gtts", "raise_before", exc={"type": name, "message": f"simulated {name}"})
    client = RemoteFakeKite(server.url)
    with pytest.raises(EXCEPTIONS[name]) as raised:
        client.get_gtts()
    assert type(raised.value) is EXCEPTIONS[name] and f"simulated {name}" in str(raised.value)


def test_H4_a_broker_refusal_keeps_its_type_and_an_unmapped_exception_is_a_harness_error(mp):
    server = mp.start_server()
    client = RemoteFakeKite(server.url)
    with pytest.raises(InputException, match="not found"):
        client.delete_gtt(12345)                              # FakeKite's own refusal
    with pytest.raises(HarnessError, match="unmapped exception.*TypeError"):
        client.place_gtt()                                    # a TypeError on the server: never disguised
    with pytest.raises(HarnessError, match="allow-listed"):
        server.control.fault("get_gtts", "raise_before", exc={"type": "ValueError", "message": "x"})


# ---------------------------------------------------------------- H5-H8: ambiguity and gates
def test_H5_accept_then_drop_applies_the_effect_and_returns_no_response(mp):
    server = mp.start_server()
    server.control.fault("place_gtt", "accept_then_drop")
    with pytest.raises(requests.ConnectionError):
        RemoteFakeKite(server.url).place_gtt(**GTT)
    [entry] = server.control.log()
    assert entry["effect_applied"] is True and entry["response"] == "dropped"
    assert len(server.control.state()["gtt_book"]) == 1


def test_H5_accept_then_malformed_is_a_data_exception_after_the_effect(mp):
    server = mp.start_server()
    server.control.fault("place_gtt", "accept_then_malformed")
    with pytest.raises(DataException, match="parse"):
        RemoteFakeKite(server.url).place_gtt(**GTT)
    assert server.control.log()[0]["effect_applied"] is True


def test_H6_a_held_response_times_out_the_client_and_is_logged(mp):
    server = mp.start_server()
    server.control.fault("place_gtt", "accept_then_hold", gate="g1")
    with pytest.raises(requests.ReadTimeout):
        RemoteFakeKite(server.url, timeout=0.5).place_gtt(**GTT)
    assert server.control.log()[0]["effect_applied"] is True    # applied although the client gave up
    server.control.release("g1")
    deadline = time.monotonic() + 10
    while server.control.log()[0]["response"] == "pending" and time.monotonic() < deadline:
        time.sleep(0.02)
    entry = server.control.log()[0]
    assert entry["gate_release"] == "released" and entry["response"] in ("written", "write_failed")


def test_H6_a_held_response_is_delivered_on_release(mp):
    server = mp.start_server()
    server.control.fault("place_gtt", "accept_then_hold", gate="g2")
    threading.Timer(0.3, lambda: server.control.release("g2")).start()
    started = time.monotonic()
    result = RemoteFakeKite(server.url, timeout=10).place_gtt(**GTT)
    assert result["trigger_id"] and time.monotonic() - started >= 0.25
    assert server.control.log()[0]["response"] == "written"


def test_H6_an_unreleased_gate_releases_itself_after_max_hold(mp):
    server = mp.start_server(max_hold=0.5)
    server.control.fault("get_gtts", "hold_before", gate="never")
    assert RemoteFakeKite(server.url, timeout=10).get_gtts() == []
    assert server.control.log()[0]["gate_release"] == "timeout"


def test_H7_raise_before_changes_nothing(mp):
    server = mp.start_server()
    server.control.fault("place_gtt", "raise_before", exc={"type": "NetworkException", "message": "down"})
    with pytest.raises(EXCEPTIONS["NetworkException"]):
        RemoteFakeKite(server.url).place_gtt(**GTT)
    assert server.control.state()["gtt_book"] == [] and server.control.log()[0]["effect_applied"] is False


def test_H7_hold_before_takes_effect_only_after_the_gate(mp):
    server = mp.start_server()
    server.control.fault("place_gtt", "hold_before", gate="late")
    with pytest.raises(requests.ReadTimeout):
        RemoteFakeKite(server.url, timeout=0.3).place_gtt(**GTT)
    assert server.control.state()["gtt_book"] == []              # not processed yet (B-31 model)
    server.control.release("late")
    deadline = time.monotonic() + 10
    while not server.control.state()["gtt_book"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(server.control.state()["gtt_book"]) == 1           # processed after the client gave up


def test_H8_control_and_other_clients_work_while_a_response_is_held(mp):
    server = mp.start_server()
    server.control.fault("place_gtt", "accept_then_hold", gate="g")
    holder = threading.Thread(target=lambda: RemoteFakeKite(server.url, timeout=30).place_gtt(**GTT), daemon=True)
    holder.start()
    deadline = time.monotonic() + 10
    while not server.control.log() and time.monotonic() < deadline:
        time.sleep(0.01)
    started = time.monotonic()
    assert len(server.control.state()["gtt_book"]) == 1
    assert len(RemoteFakeKite(server.url).get_gtts()) == 1        # another client is not blocked
    assert time.monotonic() - started < 2
    server.control.release("g")
    holder.join(10)
    assert not holder.is_alive()


# ---------------------------------------------------------------- H9, H10, H15: the guard in every child
def test_H9_every_child_reports_an_installed_guard_and_no_unexpected_refusal(mp, env):
    prepare_app_db(env)
    mp.start_server()
    app = mp.app("a", env)
    live = app.cmd("report")["report"]
    assert live["guard_installed"] and live["dotenv_neutralised"] and live["unexpected"] == []
    mp.teardown()
    assert {r["child"] for r in mp.reports} == {"server", "a"}
    for report in mp.reports:
        assert report["guard_installed"] and report["dotenv_neutralised"], report["child"]
        assert report["unexpected"] == [] and report["protected_opens"] == [], report["child"]
        assert report["role"] in ("server", "app:a")
    assert mp.guard_problems() == []


def test_H10_a_child_refuses_a_decoy_protected_path_and_an_outbound_connect(mp, env):
    prepare_app_db(env)
    mp.start_server()
    decoy = env.tmp_path / "decoy_kite_terminal.db"
    decoy.write_bytes(b"decoy")
    app = mp.app("a", env)
    assert app.cmd("guard_probe", decoy=str(decoy)) | {} == {"id": 1, "ok": True, "decoy": "refused",
                                                           "network": "refused"} | {"id": 1}
    report = app.cmd("report")["report"]
    kinds = sorted((r[0], r[2], r[3]) for r in report["refusals"])
    assert ("network", False, True) in kinds and ("open", True, True) in kinds
    assert report["unexpected"] == []


def test_H15_children_open_data_files_only_under_tmp_path(mp, env):
    prepare_app_db(env)
    mp.start_server()
    app = mp.app("a", env)
    app.cmd("start")
    app.cmd("fenced_write", marker="h15")
    mp.teardown()
    tmp = os.path.normcase(os.path.realpath(env.tmp_path))
    # Read-only repository files the application legitimately reads: the committed NSE calendar, and the
    # git metadata backend/version.commit_short() reads for app_instance.app_commit at takeover.
    # V12 final readiness, Phase C: the installation registry, redirected for the whole session to a temp dir
    # (conftest, KT_INSTALLATION_REGISTRY_DIR, inherited by children); the real one is protected by the guard.
    from backend import installation_state
    allowed_outside = (os.path.normcase(str(mp_harness.REPO / "backend" / "calendar_data")),
                       os.path.normcase(str(mp_harness.REPO / ".git")),
                       os.path.normcase(os.path.realpath(installation_state.registry_dir())))
    for report in mp.reports:
        assert report["protected_opens"] == []
        for path in report["data_opens"]:
            target = path
            if target.startswith("file:"):   # a SQLite URI (the read-only startup precheck): its file part, as the guard reads it
                target = real_data_guard._URI_DRIVE.sub("", target[len("file:"):].split("?", 1)[0])
            norm = os.path.normcase(os.path.realpath(target))
            assert "kite_terminal.db" not in norm and "ui_prefs.json" not in norm, (report["child"], path)
            if norm.startswith(tmp):
                continue
            assert not norm.endswith((".db", ".db-wal", ".db-shm", ".lock", ".tmp", ".env")), (report["child"], path)
            assert norm.startswith(allowed_outside), (report["child"], path)


# ---------------------------------------------------------------- H11, H16: static and fence checks
def test_H11_every_s3_child_entry_script_imports_child_bootstrap_first():
    for script in mp_harness.CHILD_ENTRY_SCRIPTS:
        tree = ast.parse(script.read_text(encoding="utf-8"))
        body = [n for n in tree.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        if script.name == "child_bootstrap.py":
            # It IS the guard installer: before `import real_data_guard` and its install() it may import only
            # these standard-library modules (which open no data file), and touch nothing else.
            names = []
            for node in body:
                if isinstance(node, ast.Import):
                    names += [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names.append(node.module)
                if "real_data_guard" in names:
                    break
            assert names[-1] == "real_data_guard", names
            assert set(names[:-1]) <= {"atexit", "json", "os", "sys", "time", "pathlib"}, names
            source = script.read_text(encoding="utf-8")
            assert source.index("real_data_guard.install()") < source.index("def ")
            continue
        first = body[0]
        if isinstance(first, ast.If):                         # `if __name__ == "__main__": import child_bootstrap`
            assert "__main__" in ast.unparse(first.test), script.name
            first = first.body[0]
        assert isinstance(first, ast.Import) and first.names[0].name == "child_bootstrap", script.name


def test_H16_every_remote_write_is_fenced_and_refused_without_authority(mp):
    from backend import instance_lock, kite_client
    assert set(WRITE_METHODS) <= set(kite_client.BROKER_WRITE_METHODS)
    instance_lock.reset_for_tests()                          # this process: no authority
    server = mp.start_server()
    client = kite_client.fence_broker(RemoteFakeKite(server.url))
    for name in WRITE_METHODS:
        assert getattr(getattr(client, name), "__wrapped_broker_write__", False), name
        with pytest.raises(instance_lock.InstanceNotOwner):
            getattr(client, name)()
    assert not hasattr(client, "modify_order")               # no generic fallback that could bypass the fence
    assert server.control.log() == []                        # nothing reached the "broker"


# ---------------------------------------------------------------- H12, H13: cleanup and diagnostics
def test_H12_teardown_kills_a_child_that_ignores_exit_and_reaps_its_grandchild(mp, env):
    prepare_app_db(env)
    mp.start_server()
    app = mp.app("a", env)
    grandchild = app.cmd("spawn_grandchild", seconds=600)["grandchild_pid"]
    app.cmd("ignore_exit")
    assert pid_alive(grandchild)
    assert {app.pid, grandchild} <= mp_harness.descendants({app.launcher_pid})   # what teardown will reap
    started = time.monotonic()
    mp.teardown()
    assert time.monotonic() - started < 3 * mp_harness.GRACE + 10
    assert not pid_alive(app.pid) and not pid_alive(grandchild)


def test_H13_a_hung_command_fails_with_the_childs_diagnostics(mp, env):
    prepare_app_db(env)
    mp.start_server()
    app = mp.app("a", env)
    with pytest.raises(mp_harness.HarnessTimeout) as timed_out:
        app.cmd("sleep", seconds=5, timeout=0.5)
    assert "stderr tail" in str(timed_out.value) and f"pid {app.pid}" in str(timed_out.value)


def test_H13_a_dead_child_is_reported_not_waited_for(mp, env):
    prepare_app_db(env)
    mp.start_server()
    app = mp.app("a", env)
    started = time.monotonic()
    app.start("crash", code=3)
    with pytest.raises(mp_harness.ChildDied):
        app.wait(lambda m: m.get("id") == 99, timeout=30)
    assert time.monotonic() - started < 15 and app.proc.returncode == 3
