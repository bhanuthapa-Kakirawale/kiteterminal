"""Child process for the Phase 2 cross-process tests (tests/test_instance_lock.py). Not a test module.

Like tests/demo_server.py, it installs the Phase 0 isolation guard BEFORE any backend import: a child
is outside the parent's audit hook. Every database and lock file it touches is a path the parent
passes in (under the parent's tmp_path). No broker: FakeKite only. No real credentials: sentinels.

    python tests/instance_lock_child.py <mode> <json args>
"""

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import real_data_guard  # noqa: E402

real_data_guard.install()
real_data_guard.neutralise_dotenv()
real_data_guard.scrub_kite_environment()


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _wait_for(path: Path, timeout: float = 600.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{path} never appeared")
        time.sleep(0.002)


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _app_env(args: dict) -> None:
    """The env-fixture equivalent for a child: sentinels, tmp .env, tmp database."""
    os.environ["KITE_TERMINAL_ENCRYPTION_KEY"] = args["key"]
    os.environ["KITE_API_KEY"] = "test_api_key"
    os.environ["KITE_API_SECRET"] = "test_api_secret"
    from backend import crypto, db
    crypto.ENV_PATH = Path(args["env_path"])
    db.DB_PATH = Path(args["db"])


def hold(args: dict) -> None:
    """Take the instance lock of args['db'] (or report BUSY), then hold it until stdin closes."""
    from backend import instance_lock
    fd = instance_lock.try_lock_file(instance_lock.lock_path_for(args["db"]))
    _say("LOCKED" if fd is not None else "BUSY")
    if fd is None:
        return
    if args.get("exit_code") is not None:   # crash while holding it
        os._exit(int(args["exit_code"]))
    sys.stdin.read()


def race_pipe(args: dict) -> None:
    """The L1 racer, coordinated through pipes (no file contents to read: on Windows a file being created
    or replaced can be briefly unreadable). Per round the parent writes 'round <k> <n>'; a participant
    (index < n) waits for the directory go_<k> (an atomic mkdir: the simultaneous release), tries the lock
    and answers 'r <k> 1|0'; the parent then writes 'end <k>' and the racer releases and answers."""
    from backend import instance_lock
    lock, barrier, index = instance_lock.lock_path_for(args["db"]), Path(args["dir"]), int(args["index"])
    _say("ready")
    for line in sys.stdin:
        word, k, *rest = line.split()
        if word == "round":
            fd = None
            if index < int(rest[0]):
                go = barrier / f"go_{k}"
                while not go.is_dir():
                    pass                                   # spin: release as close together as possible
                fd = instance_lock.try_lock_file(lock)
                _say(f"r {k} {1 if fd is not None else 0}")
            else:
                _say(f"r {k} -")
        elif word == "end":
            if fd is not None:
                instance_lock.release_lock_file(fd)
                fd = None
            _say(f"released {k}")


def startup(args: dict) -> None:
    """The app's startup sequence, crashing (os._exit) at a named boundary (design §9, C1)."""
    _app_env(args)
    from backend import db, instance_lock
    boundary = args["boundary"]
    if boundary == "before_lock":
        os._exit(137)
    assert instance_lock.acquire(db.DB_PATH) == instance_lock.LOCKED
    if boundary == "after_lock":
        os._exit(137)
    db.init_db()
    result = instance_lock.takeover(poll_seconds=0.01, now=lambda: _shifted(args))
    _say(json.dumps({"epoch": result["epoch"]}))
    if boundary == "after_takeover":
        os._exit(137)
    conn = db.get_connection()
    if boundary == "mid_fenced_transaction":
        with instance_lock.fenced_transaction(conn):
            conn.execute("UPDATE account_restriction SET restriction_reasons = '[\"FROM_THE_CRASHED_PROCESS\"]'")
            os._exit(137)
    if boundary in ("after_claim_before_broker", "after_claim_after_broker"):
        # A live entry claimed (submission_state 'submitting', committed before the broker call, exactly
        # as routes/tickets claims it) and then the process dies before or after the call: indistinguishable
        # locally (design §9 D4/D5).
        account = conn.execute("SELECT id FROM accounts WHERE is_default = 1").fetchone()["id"]
        conn.execute(
            "INSERT INTO trade_tickets (account_id, symbol, exchange, instrument_token, transaction_type, quantity, "
            "entry_price, entry_order_type, stop_loss, target_1, product_type, rationale, status, mode, source, "
            "submission_state, submitted_at, order_tag) VALUES (?, 'TCS', 'NSE', 2953217, 'BUY', 5, 3000, 'LIMIT', "
            "2900, 3300, 'CNC', 'crash harness', 'confirmed', 'live', 'manual', 'submitting', "
            "strftime('%Y-%m-%d %H:%M:%S', 'now'), 'ktcrash1')", (account,))
        conn.commit()
        if boundary == "after_claim_after_broker":
            Path(args["broker_log"]).write_text(json.dumps({"place_order": 1}))   # the "sent" request
        os._exit(137)
    raise SystemExit(f"unknown boundary {boundary}")


def _shifted(args: dict):
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) + timedelta(seconds=float(args.get("clock_shift", 0)))


def owner(args: dict) -> None:
    """E2 / P1: become the owner (optionally through a DIFFERENT lock file: a simulated lock bypass, as
    a network folder or an older build would be), heartbeat until authority is lost (or a deadline),
    then try one broker write through the fence and report what happened."""
    _app_env(args)
    from backend import db, instance_lock, kite_client
    from fake_kite import FakeKite
    if args.get("bypass_lock_path"):
        instance_lock.lock_path_for = lambda _db: Path(args["bypass_lock_path"])
    assert instance_lock.acquire(db.DB_PATH) == instance_lock.LOCKED
    took = instance_lock.takeover(poll_seconds=0.01, now=lambda: _shifted(args))
    _say(json.dumps({"event": "owner", "epoch": took["epoch"]}))
    fake = kite_client.fence_broker(FakeKite())
    if args.get("wait_file"):   # stay idle (no heartbeat) until the parent says go
        _wait_for(Path(args["wait_file"]), timeout=120)
    deadline = time.monotonic() + float(args.get("seconds", 20))
    status_file = Path(args["status"])
    while time.monotonic() < deadline:
        result = instance_lock.heartbeat_once()
        _write(status_file, json.dumps({"role": instance_lock.status()["role"], "lost": result["lost"],
                                        "epoch": took["epoch"]}))
        if result["lost"]:
            break
        if Path(args["stop"]).exists():
            break
        time.sleep(0.05)
    try:
        fake.place_order(variety="regular", exchange="NSE", tradingsymbol="TCS", transaction_type="SELL",
                         quantity=1, product="CNC", order_type="MARKET")
        write = "sent"
    except instance_lock.InstanceNotOwner as e:
        write = f"refused:{e.reason}"
    _say(json.dumps({"event": "write", "result": write, "fake_place_order_calls": len(fake.calls_to("place_order")),
                     "role": instance_lock.status()["role"], "refusal": instance_lock.refusal_code()}))


def contend(args: dict) -> None:
    """S1: K threads doing fenced read-modify-write increments on one database, with a fixed (shared,
    simulated) identity - SQLite contention only; the OS lock is out of the picture here."""
    import threading
    _app_env(args)
    from backend import db, instance_lock
    state = instance_lock._state
    state.role, state.epoch, state.instance_uuid = instance_lock.OWNER, int(args["epoch"]), args["uuid"]
    state.db_path = Path(os.path.realpath(db.DB_PATH))
    done, busy = [0], [0]
    lock = threading.Lock()

    def work():
        for _ in range(int(args["iterations"])):
            conn = db.get_connection()
            try:
                with instance_lock.fenced_transaction(conn):
                    value = conn.execute("SELECT value FROM s1_counter WHERE id = 1").fetchone()[0]
                    conn.execute("UPDATE s1_counter SET value = ? WHERE id = 1", (value + 1,))
                with lock:
                    done[0] += 1
            except Exception as e:  # a busy timeout fails closed: counted, nothing written
                if "locked" in str(e) or "busy" in str(e):
                    with lock:
                        busy[0] += 1
                else:
                    raise
            finally:
                conn.close()

    threads = [threading.Thread(target=work) for _ in range(int(args["threads"]))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    _say(json.dumps({"done": done[0], "busy": busy[0]}))


if __name__ == "__main__":
    {"hold": hold, "race_pipe": race_pipe, "startup": startup, "owner": owner,
     "contend": contend}[sys.argv[1]](
        json.loads(sys.argv[2]))
