"""Phase 3 S3: one APPLICATION process of the multi-process harness. Not a test module.

    python tests/mp_app_child.py <config.json>

It installs the Phase 0 guard first (child_bootstrap), refuses any configured path outside the test's
tmp root or protected, runs on sentinel credentials, and uses the SHARED FakeKite (RemoteFakeKite, fenced
by kite_client.fence_broker exactly like the env fixture's FakeKite) as its only broker. No SDK client
can be built in this process. The parent drives it with JSON-line commands on stdin; each command gets
exactly one JSON reply on stdout, {"id": n, "ok": true|false, ...}. Commands that block on a barrier
first print {"event": "parked"}.

Timing constants are scaled down IN THIS PROCESS ONLY (config["scale"]; e.g. HEARTBEAT_SECONDS=0.2,
so a resume threshold of 0.6 s), so suspend, lease and detection tests run in seconds. Production
values are untouched.

A lock bypass (config["bypass_lock_path"]) makes this process take a DIFFERENT lock file for the same
database: the simulated network-folder / older-build case of Phase 2 E2/P1. It is the only way two
processes on one machine can both reach the takeover; the OS lock otherwise prevents it.
"""

import child_bootstrap  # noqa: F401  (first: the Phase 0 guard in this process before anything else)

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
child_bootstrap.require_safe_paths(CONFIG["root"], sys.argv[1], CONFIG["db"], CONFIG["env_path"],
                                   CONFIG.get("bypass_lock_path"), CONFIG.get("barrier_dir"),
                                   os.environ.get(child_bootstrap.REPORT_ENV))

# The env fixture's sentinels, set by the child itself (inherited KITE_* were scrubbed by the bootstrap).
os.environ["KITE_TERMINAL_ENCRYPTION_KEY"] = CONFIG["key"]
os.environ["KITE_API_KEY"] = "test_api_key"
os.environ["KITE_API_SECRET"] = "test_api_secret"
os.environ["KITE_REST_PROXY"] = "http://127.0.0.1:9"   # loopback discard port: never contacted (no SDK client)

from backend import config, crypto, db, instance_lock, kite_client, startup_reconciliation, ui_prefs  # noqa: E402
from fake_kite import SimulatedCrash  # noqa: E402
from shared_fake_kite import RemoteFakeKite  # noqa: E402

crypto.ENV_PATH = Path(CONFIG["env_path"])
db.DB_PATH = Path(CONFIG["db"])
ui_prefs.PREFS_PATH = Path(CONFIG["root"]) / f"ui_prefs.{os.getpid()}.json"
for _name, _value in (CONFIG.get("scale") or {}).items():
    assert hasattr(config, _name), _name
    setattr(config, _name, _value)

BROKER = kite_client.fence_broker(RemoteFakeKite(CONFIG["server_url"], tag=CONFIG.get("tag", ""),
                                                 timeout=float(CONFIG.get("client_timeout", 10.0))))
kite_client.get_kite = lambda account_id=None: BROKER


def _no_sdk_client(*args, **kwargs):
    raise AssertionError("mp_app_child: no Kite SDK client may be built in a harness process")


kite_client.create_kite_connect = _no_sdk_client

STAGE1_REQUESTS: list[str] = []
startup_reconciliation.request_run = lambda trigger: STAGE1_REQUESTS.append(trigger)

if CONFIG.get("bypass_lock_path"):
    _bypass = Path(CONFIG["bypass_lock_path"])
    instance_lock.lock_path_for = lambda _db: _bypass

BARRIERS = Path(CONFIG["barrier_dir"])
CLOCK = {"wall": 0.0, "mono": 0.0}
_OUT = threading.Lock()


def now():
    return datetime.now(timezone.utc) + timedelta(seconds=CLOCK["wall"])


def mono():
    return time.monotonic() + CLOCK["mono"]


def _say(obj: dict) -> None:
    with _OUT:
        sys.stdout.write(json.dumps(obj, default=str) + "\n")
        sys.stdout.flush()


def _park(name: str, timeout: float = 120.0) -> None:
    _say({"event": "parked", "barrier": name})
    deadline = time.monotonic() + timeout
    target = BARRIERS / name
    while not target.is_dir():
        if time.monotonic() > deadline:
            raise TimeoutError(f"barrier {name} was never released")
        time.sleep(0.002)


def _account_id(conn) -> int:
    return conn.execute("SELECT id FROM accounts WHERE is_default = 1").fetchone()["id"]


# ---------------------------------------------------------------- the heartbeat thread
HB = {"thread": None, "stop": threading.Event(), "events": []}


def _heartbeat_loop() -> None:
    """run_heartbeat_loop's body, on a thread, with this process's (scaled) interval and clocks."""
    while not HB["stop"].wait(config.HEARTBEAT_SECONDS):
        if not instance_lock.loop_may_run("instance_heartbeat"):
            HB["events"].append({"t": time.time(), "stopped": instance_lock.refusal_code()})
            return
        result = instance_lock.heartbeat_once(now=now, monotonic=mono)
        if result.get("resumed"):
            startup_reconciliation.note_resume()
        HB["events"].append({"t": time.time(), **result})   # recorded once the loop has handled it
        if result.get("lost"):
            return


# ---------------------------------------------------------------- commands
def c_acquire(m):
    return {"role": instance_lock.acquire(db.DB_PATH)}


def c_takeover(m):
    took = instance_lock.takeover(now=now, monotonic=mono, poll_seconds=0.01)
    return {k: took.get(k) for k in ("epoch", "already_owner", "previous_unclean", "previous_end", "waited_seconds",
                                     "conflict", "reasons", "detail")}


def c_start(m):
    """The startup sequence: lock, init_db (only as the lock holder), fenced takeover."""
    role = instance_lock.acquire(db.DB_PATH)
    if role != instance_lock.LOCKED:
        return {"role": role, "epoch": None}
    if m.get("init_db", True):
        db.init_db()
    took = c_takeover(m)
    return {"role": instance_lock.status()["role"], **took}


def c_race_start(m):
    """MP-L1: park on a barrier, then the startup sequence, then one broker write through the fence."""
    _park(m["barrier"])
    started = c_start({"init_db": False})
    write = _broker("place_order", kwargs=m["order"])
    return {**started, "write": write}


def c_reset(m):
    """Forget this process's ownership state (instance_lock.reset_for_tests): releases a held lock."""
    instance_lock.reset_for_tests()
    return {"role": instance_lock.status()["role"]}


def c_status(m):
    s = instance_lock.status()
    return {"role": s["role"], "epoch": s["epoch"], "refusal_code": s["refusal_code"], "authority": s["authority"],
            "process_reasons": s["process_reasons"], "heartbeat_seq": s["heartbeat_seq"],
            "in_flight": s["broker_writes_in_flight"], "stage1_requests": list(STAGE1_REQUESTS),
            "heartbeats": len(HB["events"]), "last_heartbeat": HB["events"][-1] if HB["events"] else None,
            "lost_at": next((e["t"] for e in HB["events"] if e.get("lost")), None),
            "resumed_events": sum(1 for e in HB["events"] if e.get("resumed"))}


def c_heartbeat(m):
    result = instance_lock.heartbeat_once(now=now, monotonic=mono)
    if result.get("resumed"):
        startup_reconciliation.note_resume()
    return {"heartbeat": result, "role": instance_lock.status()["role"]}


def c_heartbeats_start(m):
    HB["stop"].clear()
    HB["thread"] = threading.Thread(target=_heartbeat_loop, daemon=True)
    HB["thread"].start()
    return {"started": True, "interval": config.HEARTBEAT_SECONDS}


def c_heartbeats_stop(m):
    HB["stop"].set()
    if HB["thread"] is not None:
        HB["thread"].join(timeout=5)
    return {"stopped": True, "events": len(HB["events"])}


def _fenced_insert(marker: str) -> dict:
    conn = db.get_connection()
    try:
        with instance_lock.fenced_transaction(conn):
            conn.execute("INSERT INTO mp_markers (marker, pid, epoch, kind) VALUES (?, ?, ?, 'fenced')",
                         (marker, os.getpid(), instance_lock._state.epoch))
        return {"written": True}
    except instance_lock.FencingError as e:
        return {"written": False, "error": type(e).__name__, "code": e.error_code}
    except sqlite3.OperationalError as e:
        return {"written": False, "error": "OperationalError", "message": str(e)}
    finally:
        conn.close()


def c_fenced_write(m):
    return _fenced_insert(m["marker"])


def _legacy_insert(marker: str) -> dict:
    """A pre-Phase-3 style write: no fence, just a commit (the D§7.3 residual)."""
    conn = db.get_connection()
    try:
        conn.execute("INSERT INTO mp_markers (marker, pid, epoch, kind) VALUES (?, ?, ?, 'legacy')",
                     (marker, os.getpid(), instance_lock._state.epoch))
        conn.commit()
        return {"written": True}
    except sqlite3.OperationalError as e:
        return {"written": False, "error": "OperationalError", "message": str(e)}
    finally:
        conn.close()


def c_legacy_write(m):
    return _legacy_insert(m["marker"])


def _broker(method: str, args=(), kwargs=None) -> dict:
    try:
        result = getattr(BROKER, method)(*args, **(kwargs or {}))
        return {"sent": True, "result": result}
    except SimulatedCrash:
        # The broker took the request and then this process "died": exactly that, with no cleanup.
        child_bootstrap.write_report()
        os._exit(137)
    except instance_lock.InstanceNotOwner as e:
        return {"sent": False, "refused": e.reason}
    except Exception as e:  # an answer from the broker, or an ambiguous outcome
        return {"sent": None, "error": type(e).__name__, "message": str(e)[:300]}


def c_broker(m):
    return _broker(m["method"], m.get("args") or (), m.get("kwargs"))


def c_broker_burst(m):
    """`threads` threads, each making `count` calls of one method, concurrently (H3)."""
    results, errors, lock = [], [], threading.Lock()

    def work():
        for _ in range(int(m["count"])):
            out = _broker(m["method"], m.get("args") or (), m.get("kwargs"))
            with lock:
                (results if out["sent"] else errors).append(out.get("result") if out["sent"] else out)

    workers = [threading.Thread(target=work) for _ in range(int(m["threads"]))]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    return {"results": results, "errors": errors}


def c_probe_cas_then_send(m):
    """The engine's pattern (M-6 R2-7): a fenced claim transaction first, the broker call only after it
    committed. An optional park sits between the commit and the call (the residual window)."""
    claim = _fenced_insert(m["marker"])
    if not claim["written"]:
        return {"claim": claim, "sent": False}
    if m.get("park"):
        _park(m["park"])
    return {"claim": claim, **_broker(m["method"], m.get("args") or (), m.get("kwargs"))}


def c_cycle(m):
    """A legacy background-loop cycle: the authority check at its start (loop_may_run), an optional park
    (the cycle is past its check), then its write(s) and an optional broker call."""
    if not instance_lock.loop_may_run("mp_cycle"):
        return {"ran": False, "refusal": instance_lock.refusal_code()}
    if m.get("park"):
        _park(m["park"])
    out = {"ran": True}
    if m.get("fenced_marker"):
        out["fenced"] = _fenced_insert(m["fenced_marker"])
    if m.get("legacy_marker"):
        out["legacy"] = _legacy_insert(m["legacy_marker"])
    if m.get("method"):
        out["broker"] = _broker(m["method"], m.get("args") or (), m.get("kwargs"))
    return out


def c_park(m):
    _park(m["barrier"], float(m.get("timeout", 120)))
    return {"released": True}


TX: dict = {}


def c_begin_tx(m):
    """Opens a write transaction (BEGIN IMMEDIATE; fenced or plain), writes a marker, and keeps it open."""
    conn = db.get_connection()
    if m.get("fenced", True):
        cm = instance_lock.fenced_transaction(conn)
        cm.__enter__()
    else:
        cm = None
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO mp_markers (marker, pid, epoch, kind) VALUES (?, ?, ?, 'tx')",
                 (m["marker"], os.getpid(), instance_lock._state.epoch))
    TX.update(conn=conn, cm=cm)
    return {"in_tx": True}


def c_end_tx(m):
    conn, cm = TX.pop("conn"), TX.pop("cm")
    try:
        if cm is not None:
            if m.get("commit", True):
                cm.__exit__(None, None, None)
            else:
                error = RuntimeError("rollback requested")
                cm.__exit__(RuntimeError, error, None)
        else:
            conn.execute("COMMIT" if m.get("commit", True) else "ROLLBACK")
    finally:
        conn.close()
    return {"ended": True}


def c_crash(m):
    child_bootstrap.write_report()
    os._exit(int(m.get("code", 137)))


def c_spawn_grandchild(m):
    env = dict(os.environ)
    env[child_bootstrap.REPORT_ENV] = os.environ[child_bootstrap.REPORT_ENV] + ".grandchild.json"
    env[child_bootstrap.ROLE_ENV] = "grandchild"
    proc = subprocess.Popen([sys.executable, str(Path(child_bootstrap.__file__)), "--sleep", str(m.get("seconds", 600))],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    return {"grandchild_pid": proc.pid}


def c_stage1(m):
    result = startup_reconciliation.run_stage1(m.get("trigger", "startup"))
    findings = result.get("findings") or {}
    return {"outcome": result.get("outcome"), "skipped": result.get("skipped"),
            "reasons_after": result.get("reasons_after"), "state": result.get("state"),
            "read": findings.get("read"), "error": findings.get("error")}


def c_claim_entry(m):
    """A live entry claimed exactly as routes/tickets claims it (submission_state 'submitting', committed
    before the broker call), like Phase 2 C1. Not the route itself."""
    conn = db.get_connection()
    try:
        account = _account_id(conn)
        tid = conn.execute(
            "INSERT INTO trade_tickets (account_id, symbol, exchange, instrument_token, transaction_type, quantity, "
            "entry_price, entry_order_type, stop_loss, target_1, product_type, rationale, status, mode, source, "
            "submission_state, submitted_at, order_tag) VALUES (?, 'TCS', 'NSE', 2953217, 'BUY', 5, 3000, 'LIMIT', "
            "2900, 3300, 'CNC', 'mp harness', 'confirmed', 'live', 'manual', 'submitting', "
            "strftime('%Y-%m-%d %H:%M:%S', 'now'), ?)", (account, m["tag"])).lastrowid
        conn.commit()
        return {"ticket_id": tid}
    finally:
        conn.close()


def c_derive(m):
    conn = db.get_connection()
    try:
        derived = startup_reconciliation.derive_restriction(conn, _account_id(conn))
        return {"state": derived["state"], "reasons": sorted(derived["reasons"])}
    finally:
        conn.close()


def c_set_clock(m):
    CLOCK["wall"], CLOCK["mono"] = float(m.get("wall", 0.0)), float(m.get("mono", 0.0))
    return dict(CLOCK)


def c_simulate_owner(m):
    """A fixed, shared (simulated) owner identity, as Phase 2 S1's `contend`: the OS lock and the epoch
    are taken out of the picture so a test can race the database alone."""
    state = instance_lock._state
    with state.cv:
        state.role, state.epoch, state.instance_uuid = instance_lock.OWNER, int(m["epoch"]), m["uuid"]
        state.db_path = Path(os.path.realpath(db.DB_PATH))
    return {"role": instance_lock.status()["role"]}


def c_insert_intent(m):
    """The dark journal's at-most-one-open-change index (db.py idx_protection_ops_one_open_change), raced by
    processes: an `intent` create claimed in a fenced transaction; the broker call only after the commit."""
    if m.get("park"):
        _park(m["park"])
    conn = db.get_connection()
    try:
        try:
            with instance_lock.fenced_transaction(conn):
                conn.execute(
                    "INSERT INTO protection_operations (op_uuid, account_id, position_id, mode, kind, reason, state, "
                    "basis_filled_quantity, basis_exits_filled, requested_quantity, request_fingerprint, "
                    "request_payload, instance_epoch) VALUES (?, ?, ?, 'live', 'create', 'fill', 'intent', ?, 0, ?, "
                    "'{}', '{}', ?)",
                    (m["op_uuid"], _account_id(conn), int(m["position_id"]), int(m["quantity"]), int(m["quantity"]),
                     instance_lock._state.epoch))
        except sqlite3.IntegrityError as e:
            return {"inserted": False, "error": "IntegrityError", "message": str(e), "sent": False}
        except sqlite3.OperationalError as e:
            return {"inserted": False, "error": "OperationalError", "message": str(e), "sent": False}
    finally:
        conn.close()
    out = {"inserted": True}
    if m.get("method"):
        out.update(_broker(m["method"], m.get("args") or (), m.get("kwargs")))
    return out


def c_shutdown(m):
    instance_lock.begin_shutdown()
    return {"shutdown": instance_lock.finish_shutdown(timeout=float(m.get("timeout", 1.0)))}


def c_guard_probe(m):
    """Proves the guard works in this child: a decoy protected path and a non-loopback connect are both
    refused, as expected refusals (H10)."""
    import real_data_guard
    out = {}
    decoy = Path(m["decoy"])
    child_bootstrap.require_safe_paths(CONFIG["root"], decoy)
    with real_data_guard.protect_also(decoy), real_data_guard.expected():
        try:
            decoy.read_bytes()
            out["decoy"] = "opened"
        except real_data_guard.RealAccountDataAccess:
            out["decoy"] = "refused"
    with real_data_guard.expected():
        sock = socket.socket()
        sock.settimeout(1)
        try:
            sock.connect(("192.0.2.1", 9))       # TEST-NET-1: never routable, and refused before the connect
            out["network"] = "connected"
        except real_data_guard.ExternalNetworkAccess:
            out["network"] = "refused"
        except OSError as e:
            out["network"] = f"oserror:{e}"
        finally:
            sock.close()
    return out


def c_report(m):
    data = child_bootstrap.write_report()
    return {"report": {k: data[k] for k in ("guard_installed", "dotenv_neutralised", "unexpected", "refusals",
                                            "protected_opens", "opens_total")}}


def c_sleep(m):
    time.sleep(float(m["seconds"]))
    return {"slept": m["seconds"]}


def c_ignore_exit(m):
    """For the cleanup self-test: from now on, ignore stdin EOF and 'exit' (only a kill stops this)."""
    IGNORE_EXIT["on"] = True
    return {"ignoring": True}


# ---------------------------------------------------------------- Phase 3 S9: the protection engine across processes
def _engine_read(conn, purpose):
    from backend.protection_evidence import capture
    return capture(conn, BROKER, _account_id(conn), purpose, clock=now, monotonic=mono)


def _crash_guard(fn):
    """A SimulatedCrash from the shared broker means "this process died here": exit at once, no cleanup."""
    try:
        return fn()
    except SimulatedCrash:
        child_bootstrap.write_report()
        os._exit(137)


def c_engine_claim(m):
    from backend import protection
    conn = db.get_connection()
    try:
        try:
            evidence = _engine_read(conn, "prewrite")
            with protection.journal_transaction(conn):
                op = protection.claim(conn, position_id=int(m["position_id"]), kind="create", reason="fill",
                                      request=m["request"], evidence=evidence, may_increase_exposure=True, now=now())
            return {"claimed": True, "op_id": op["id"]}
        except instance_lock.FencingError as e:
            return {"claimed": False, "error": "FencingError", "code": getattr(e, "error_code", None)}
        except (protection.ClaimRefused, sqlite3.IntegrityError, sqlite3.OperationalError) as e:
            return {"claimed": False, "error": type(e).__name__, "message": str(e)}
    finally:
        conn.close()


def c_engine_send(m):
    """protection_send.send for one op. park_in_call: park on that barrier INSIDE the held fenced transaction,
    immediately before the broker call (the write lock is held while parked)."""
    from backend import protection_send
    conn = db.get_connection()
    try:
        hook = (lambda ctx: _park(m["park_in_call"])) if m.get("park_in_call") else None
        try:
            evidence = _engine_read(conn, "prewrite")
            res = _crash_guard(lambda: protection_send.send(conn, int(m["op_id"]), BROKER, evidence,
                                                            may_increase_exposure=True, now=now, monotonic=mono,
                                                            on_call=hook))
        except instance_lock.FencingError as e:
            return {"sent": False, "code": "FencingError", "detail": str(e)}
        return {"sent": res.sent, "code": res.code, "state": res.op["state"] if res.op else None}
    finally:
        conn.close()


def c_engine_recover(m):
    """Journal-aware recovery, then one reconciliation read applied to every open op."""
    from backend import protection_reconcile, protection_runtime
    conn = db.get_connection()
    try:
        recovered = protection_runtime.recover_journal(conn, _account_id(conn), now=now())
        evidence = _engine_read(conn, "reconciliation")
        actions = protection_reconcile.reconcile_all(conn, evidence, now=now())
        return {"recovered": recovered, "actions": [{"op_id": a["op_id"], "action": a["action"]} for a in actions]}
    finally:
        conn.close()


def c_engine_ee(m):
    from backend import emergency_exit_engine
    if m.get("park"):
        _park(m["park"])
    conn = db.get_connection()
    try:
        try:
            res = _crash_guard(lambda: emergency_exit_engine.emergency_exit(
                conn, int(m["position_id"]), BROKER, attribution=lambda ev: "exclusive", clock=now, monotonic=mono))
        except instance_lock.FencingError as e:
            return {"outcome": "refused", "reason": f"FencingError: {e}"}
        return {"outcome": res.outcome, "reason": res.reason, "order_id": res.order_id}
    finally:
        conn.close()


def c_engine_op(m):
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT id, state, attempts, result_gtt_id, instance_epoch FROM protection_operations "
                           "WHERE id = ?", (int(m["op_id"]),)).fetchone()
        return {"op": dict(row) if row else None}
    finally:
        conn.close()


IGNORE_EXIT = {"on": False}
COMMANDS = {name[2:]: fn for name, fn in globals().items() if name.startswith("c_") and callable(fn)}


def main() -> None:
    child_bootstrap.write_report()            # a report exists from the moment the parent knows this process
    _say({"event": "ready", "pid": os.getpid(), "db": str(db.DB_PATH), "server": CONFIG["server_url"],
          "bypass": bool(CONFIG.get("bypass_lock_path"))})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        op, command_id = message.get("op"), message.get("id")
        if op == "exit":
            if IGNORE_EXIT["on"]:
                continue
            _say({"id": command_id, "ok": True, "exiting": True})
            break
        handler = COMMANDS.get(op)
        if handler is None:
            reply = {"id": command_id, "ok": False, "error": f"unknown op {op!r}"}
        else:
            try:
                reply = {"id": command_id, "ok": True, **handler(message)}
            except Exception:
                reply = {"id": command_id, "ok": False, "error": traceback.format_exc()[-3000:]}
        child_bootstrap.write_report()
        _say(reply)
    while IGNORE_EXIT["on"]:
        time.sleep(0.1)
    HB["stop"].set()


if __name__ == "__main__":
    main()
    sys.exit(0)
