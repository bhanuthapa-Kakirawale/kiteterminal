"""Phase 3 S3: ONE FakeKite shared by several processes (docs/PHASE_3_S3_IMPLEMENTATION_REPORT.md §3).

Run as a script, this is the shared-broker SERVER, a child process of the test:

    python tests/shared_fake_kite.py <config.json>

It installs the Phase 0 guard first (child_bootstrap), binds 127.0.0.1 on a free port (any other
host is refused), prints "ready <port>" and serves one in-memory FakeKite until its stdin closes.

Imported, it provides the CLIENT, RemoteFakeKite: the same method surface as FakeKite, every call
forwarded to the server. An application child installs it, fenced (kite_client.fence_broker), as its
broker, so every process sees one order book and one GTT book. Importing this module installs nothing
and touches no file.

The server:
- applies one call at a time, under one lock, so FakeKite (which is not thread-safe) sees serial calls;
- logs every call with a global sequence number, the caller's pid, tag and in-memory epoch, whether the
  effect was applied, and what happened to the response;
- replaces FakeKite's in-process hooks (Python callables cannot cross a process boundary) with
  declarative, one-shot FAULTS per method:
    raise_before           the request never takes effect; the client gets `exc`
    accept_then_raise      the effect is applied; the client gets `exc` (e.g. SimulatedCrash, ReadTimeout)
    accept_then_drop       the effect is applied; the connection closes with no response
    accept_then_hold       the effect is applied; the response waits for the named gate
    accept_then_malformed  the effect is applied; the response is not JSON
    hold_before            the request waits for the named gate, THEN takes effect (late processing)
  A gate is released by the parent (/control "release"); an unreleased gate releases itself after
  max_hold_seconds and is logged "timeout", so nothing can hang forever. Waiting on a gate never holds
  the server lock, so other processes and the parent keep working while a response is held.

Exceptions cross the boundary by class name, from an allow-list only (EXCEPTIONS). An exception of any
other class reaches the client as HarnessError, never silently as something else.

Every result is a MODEL: nothing here is verified Kite behaviour.
"""

if __name__ == "__main__":
    import child_bootstrap  # noqa: F401  (first: the Phase 0 guard in this process before anything else)

import copy
import itertools
import json
import os
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path

import requests
from kiteconnect import exceptions as kite_exceptions

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from fake_kite import FakeKite, SimulatedCrash  # noqa: E402

# FakeKite's broker writes (each one is in backend.kite_client.BROKER_WRITE_METHODS, so fence_broker wraps it
# on the client; tests/test_mp_harness_selftest.py checks that) and its reads.
WRITE_METHODS = ("place_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt", "order_margins")
READ_METHODS = ("profile", "holdings", "positions", "mf_holdings", "margins", "quote", "ltp", "orders",
                "order_history", "trades", "get_gtts", "historical_data", "instruments")
RPC_METHODS = WRITE_METHODS + READ_METHODS
# Test-side helpers of FakeKite the parent may call through /control (never logged as broker calls).
HELPERS = ("set_holding", "set_position", "set_order_state", "set_gtt_status", "trigger_gtt",
           "trigger_gtt_against_holdings", "add_broker_order", "rollover", "expire_session", "fail_every_call",
           "clear_fail_every_call", "snapshot_orders", "serve_orders_snapshot")
# FakeKite attributes the parent may set through /control.
SETTABLE = ("auto_fill", "positions_from_fills", "margins_net", "ltp_map", "gtt_list_lag_calls", "gtt_timestamps",
            "order_history_previous_days", "repeat_delete_semantics", "repeat_modify_semantics",
            "place_gtt_refuse_until_holding_visible", "place_gtt_creates_duplicate", "hide_triggered_order_calls",
            "deleted_gtt_listing", "gtt_listing_limit", "gtt_listing_omit_ids", "gtt_expiry_days")
FAULT_KINDS = ("raise_before", "accept_then_raise", "accept_then_drop", "accept_then_hold", "accept_then_malformed",
               "hold_before")
GATED_KINDS = ("accept_then_hold", "hold_before")

EXCEPTIONS = {
    **{name: getattr(kite_exceptions, name) for name in (
        "KiteException", "GeneralException", "TokenException", "PermissionException", "OrderException",
        "InputException", "DataException", "NetworkException")},
    "ReadTimeout": requests.exceptions.ReadTimeout,
    "ConnectTimeout": requests.exceptions.ConnectTimeout,
    "Timeout": requests.exceptions.Timeout,
    "ConnectionError": requests.exceptions.ConnectionError,
    "SimulatedCrash": SimulatedCrash,
}


class HarnessError(RuntimeError):
    """The harness itself was misused or failed (an unmapped exception, an unknown op): never a broker answer."""


# ---------------------------------------------------------------- wire format
def _default(value):
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, (set, frozenset)):
        return {"__set__": sorted(value)}
    raise TypeError(f"not serialisable: {type(value).__name__}")


def _hook(obj):
    if len(obj) == 1:
        if "__datetime__" in obj:
            return datetime.fromisoformat(obj["__datetime__"])
        if "__date__" in obj:
            return date.fromisoformat(obj["__date__"])
        if "__set__" in obj:
            return set(obj["__set__"])
    return obj


def encode(obj) -> bytes:
    return json.dumps(obj, default=_default).encode("utf-8")


def decode(data):
    return json.loads(data, object_hook=_hook)


def exception_spec(exc: BaseException) -> dict:
    return {"type": type(exc).__name__, "message": str(exc), "code": getattr(exc, "code", None)}


def build_exception(spec: dict) -> BaseException:
    """The allow-listed exception a spec names; HarnessError for anything else."""
    cls = EXCEPTIONS.get(spec.get("type"))
    message = spec.get("message") or ""
    if cls is None:
        return HarnessError(f"unmapped exception from the shared FakeKite: {spec.get('type')}: {message}")
    if issubclass(cls, kite_exceptions.KiteException):
        return cls(message, code=spec["code"]) if spec.get("code") is not None else cls(message)
    return cls(message)


# ---------------------------------------------------------------- client
def _caller_epoch():
    module = sys.modules.get("backend.instance_lock")
    return getattr(getattr(module, "_state", None), "epoch", None) if module else None


class RemoteFakeKite:
    """FakeKite's method surface, every call answered by the shared server. Only these methods exist (no
    __getattr__ fallback), so a broker write can never slip past kite_client.fence_broker unwrapped."""

    VARIETY_REGULAR = FakeKite.VARIETY_REGULAR
    VARIETY_AMO = FakeKite.VARIETY_AMO
    GTT_TYPE_SINGLE = FakeKite.GTT_TYPE_SINGLE
    GTT_TYPE_OCO = FakeKite.GTT_TYPE_OCO

    def __init__(self, url: str, *, tag: str = "", timeout: float = 10.0, epoch=_caller_epoch):
        self.url = url.rstrip("/")
        self.tag = tag
        self.timeout = timeout
        self._epoch = epoch
        self._local = threading.local()
        self.access_token = "fake-access-token"
        self.sent: list[tuple[str, str]] = []   # (method, outcome) as this client saw it

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.trust_env = False           # never an environment proxy, even for loopback
            self._local.session = session
        return session

    def _call(self, method, *args, **kwargs):
        body = encode({"method": method, "args": list(args), "kwargs": kwargs,
                       "caller": {"pid": os.getpid(), "tag": self.tag, "epoch": self._epoch()}})
        try:
            response = self._session().post(self.url + "/rpc", data=body, timeout=(5.0, self.timeout),
                                            headers={"Content-Type": "application/json"})
        except requests.RequestException as e:
            self.sent.append((method, type(e).__name__))
            raise
        try:
            payload = decode(response.text)
        except ValueError:
            self.sent.append((method, "malformed"))
            # What the SDK raises on a body it cannot parse.
            raise kite_exceptions.DataException(
                f"Couldn't parse the JSON response received from the server: {response.text[:80]!r}") from None
        if payload.get("ok"):
            self.sent.append((method, "ok"))
            return payload["result"]
        self.sent.append((method, payload["error"]["type"]))
        raise build_exception(payload["error"])

    # -- broker writes (fence_broker wraps every one of these) --
    def place_order(self, *args, **kwargs):
        return self._call("place_order", *args, **kwargs)

    def cancel_order(self, *args, **kwargs):
        return self._call("cancel_order", *args, **kwargs)

    def place_gtt(self, *args, **kwargs):
        return self._call("place_gtt", *args, **kwargs)

    def modify_gtt(self, *args, **kwargs):
        return self._call("modify_gtt", *args, **kwargs)

    def delete_gtt(self, *args, **kwargs):
        return self._call("delete_gtt", *args, **kwargs)

    def order_margins(self, *args, **kwargs):
        return self._call("order_margins", *args, **kwargs)

    # -- reads --
    def profile(self):
        return self._call("profile")

    def holdings(self):
        return self._call("holdings")

    def positions(self):
        return self._call("positions")

    def mf_holdings(self):
        return self._call("mf_holdings")

    def margins(self, *args, **kwargs):
        return self._call("margins", *args, **kwargs)

    def quote(self, *args, **kwargs):
        return self._call("quote", *args, **kwargs)

    def ltp(self, *args, **kwargs):
        return self._call("ltp", *args, **kwargs)

    def orders(self):
        return self._call("orders")

    def order_history(self, *args, **kwargs):
        return self._call("order_history", *args, **kwargs)

    def trades(self):
        return self._call("trades")

    def get_gtts(self):
        return self._call("get_gtts")

    def historical_data(self, *args, **kwargs):
        return self._call("historical_data", *args, **kwargs)

    def instruments(self, *args, **kwargs):
        return self._call("instruments", *args, **kwargs)


class Controller:
    """The parent's handle on the server's /control endpoint."""

    def __init__(self, url: str, timeout: float = 30.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.trust_env = False

    def __call__(self, op: str, **kwargs):
        response = self._session.post(self.url + "/control", data=encode({"op": op, **kwargs}),
                                      timeout=(5.0, self.timeout))
        payload = decode(response.text)
        if not payload.get("ok"):
            raise HarnessError(f"control {op} failed: {payload.get('error')}")
        return payload.get("result")

    def fault(self, method, kind, exc=None, gate=None):
        spec = None
        if exc is not None:
            spec = exc if isinstance(exc, dict) else exception_spec(exc)
        return self("fault", method=method, kind=kind, exc=spec, gate=gate)

    def release(self, gate):
        return self("release", gate=gate)

    def set(self, attr, value):
        return self("set", attr=attr, value=value)

    def call(self, helper, *args, **kwargs):
        return self("call", helper=helper, args=list(args), kwargs=kwargs)

    def log(self) -> list[dict]:
        return self("log")

    def writes(self, method=None, pid=None) -> list[dict]:
        return [e for e in self.log() if e["write"] and (method is None or e["method"] == method)
                and (pid is None or e["pid"] == pid)]

    def state(self) -> dict:
        return self("state")


# ---------------------------------------------------------------- server
class Broker:
    def __init__(self, fake: FakeKite, max_hold: float):
        self.fake = fake
        self.max_hold = max_hold
        self.lock = threading.RLock()
        self.log: list[dict] = []
        self._seq = itertools.count(1)
        self._faults: dict[str, list[dict]] = {}
        self._gates: dict[str, threading.Event] = {}
        self._gates_lock = threading.Lock()
        self.stopping = threading.Event()
        # Self-test probe (H3b): a sleep inside the seq-and-effect section, and the most requests ever seen
        # inside it at once. With the lock that maximum is 1 whatever the delay; without it, it is not.
        self.probe_delay = 0.0
        self._inside = 0
        self.max_inside = 0

    def _gate(self, name: str) -> threading.Event:
        with self._gates_lock:
            return self._gates.setdefault(name, threading.Event())

    def _wait_gate(self, entry: dict, name: str) -> None:
        entry["gate"] = name
        released = self._gate(name).wait(timeout=self.max_hold)
        entry["gate_release"] = "released" if released else "timeout"

    def release_all(self) -> None:
        with self._gates_lock:
            for event in self._gates.values():
                event.set()

    # -- /rpc --
    def _apply(self, entry: dict, fault, method, args, kwargs):
        """Under self.lock: the request's effect at the FakeKite. Returns a finished (mode, payload, entry) for
        an error answer, or None with the value stored in entry['_value']."""
        if fault and fault["kind"] == "raise_before":
            entry["error"] = fault["exc"]
            return "json", {"ok": False, "error": fault["exc"]}, entry
        try:
            result = getattr(self.fake, method)(*args, **kwargs)
        except Exception as e:   # a broker answer (InputException, TokenException...) or a misuse
            entry["error"] = exception_spec(e)
            return "json", {"ok": False, "error": entry["error"]}, entry
        entry["effect_applied"] = entry["write"]
        entry["t_effect"] = time.monotonic()
        entry["result"] = copy.deepcopy(result) if entry["write"] else None
        entry["_value"] = result
        return None

    def rpc(self, request: dict):
        """Returns (mode, payload, entry): mode 'json' | 'drop' | 'malformed'."""
        method = request.get("method")
        if method not in RPC_METHODS:
            return "json", {"ok": False, "error": exception_spec(HarnessError(f"unknown method {method!r}"))}, None
        caller = request.get("caller") or {}
        args, kwargs = request.get("args") or [], request.get("kwargs") or {}
        # The sequence number and the effect are taken in ONE locked section, so the log's order is the
        # order in which effects happened. Only hold_before separates them, on purpose: its entry is logged
        # on arrival and takes effect later (late processing, B-31).
        with self.lock:
            entry = {"seq": next(self._seq), "pid": caller.get("pid"), "tag": caller.get("tag"),
                     "epoch": caller.get("epoch"), "method": method, "args": copy.deepcopy(args),
                     "kwargs": copy.deepcopy(kwargs), "write": method in WRITE_METHODS, "fault": None,
                     "effect_applied": False, "error": None, "result": None, "response": "pending",
                     "t_arrival": time.monotonic(), "t_effect": None, "t_response": None}
            self.log.append(entry)
            queue = self._faults.get(method) or []
            fault = queue.pop(0) if queue else None
            entry["fault"] = fault["kind"] if fault else None
            if not (fault and fault["kind"] == "hold_before"):
                self._inside += 1
                self.max_inside = max(self.max_inside, self._inside)
                try:
                    if self.probe_delay:
                        time.sleep(self.probe_delay)
                    outcome = self._apply(entry, fault, method, args, kwargs)
                finally:
                    self._inside -= 1
        if fault and fault["kind"] == "hold_before":
            self._wait_gate(entry, fault["gate"])
            with self.lock:
                outcome = self._apply(entry, fault, method, args, kwargs)
        if outcome is not None:
            return outcome
        result = entry.pop("_value")
        kind = fault["kind"] if fault else None
        if kind == "accept_then_raise":
            entry["error"] = fault["exc"]
            return "json", {"ok": False, "error": fault["exc"]}, entry
        if kind == "accept_then_drop":
            return "drop", None, entry
        if kind == "accept_then_malformed":
            return "malformed", None, entry
        if kind == "accept_then_hold":
            self._wait_gate(entry, fault["gate"])
        return "json", {"ok": True, "result": result}, entry

    # -- /control --
    def control(self, request: dict):
        op = request.get("op")
        if op == "release":                     # never takes the server lock
            self._gate(request["gate"]).set()
            return None
        if op == "release_all":
            self.release_all()
            return None
        if op == "ping":
            return {"pid": os.getpid()}
        if op == "probe":                       # H3b: set the probe delay; report the concurrency maximum
            if "delay" in request:
                self.probe_delay = float(request["delay"])
            return {"max_inside": self.max_inside, "delay": self.probe_delay}
        if op == "report":
            import child_bootstrap
            return child_bootstrap.write_report()
        if op == "shutdown":
            self.stopping.set()
            return None
        with self.lock:
            if op == "fault":
                method, kind = request.get("method"), request.get("kind")
                if method not in RPC_METHODS or kind not in FAULT_KINDS:
                    raise HarnessError(f"bad fault {method!r} {kind!r}")
                if kind in GATED_KINDS and not request.get("gate"):
                    raise HarnessError(f"{kind} needs a gate")
                if kind in ("raise_before", "accept_then_raise"):
                    spec = request.get("exc") or {}
                    if spec.get("type") not in EXCEPTIONS:
                        raise HarnessError(f"{kind} needs an allow-listed exception, got {spec.get('type')!r}")
                if request.get("gate"):
                    self._gate(request["gate"])
                self._faults.setdefault(method, []).append(
                    {"kind": kind, "exc": request.get("exc"), "gate": request.get("gate")})
                return None
            if op == "set":
                attr, value = request.get("attr"), request.get("value")
                if attr not in SETTABLE:
                    raise HarnessError(f"attribute {attr!r} is not settable")
                setattr(self.fake, attr, set(value) if attr == "gtt_listing_omit_ids" else value)
                return None
            if op == "call":
                helper = request.get("helper")
                if helper not in HELPERS:
                    raise HarnessError(f"helper {helper!r} is not allowed")
                args = list(request.get("args") or [])
                if helper == "fail_every_call":
                    args = [build_exception(args[0])]
                return getattr(self.fake, helper)(*args, **(request.get("kwargs") or {}))
            if op == "state":
                return {"gtt_book": copy.deepcopy(list(self.fake.gtt_book.values())),
                        "order_book": copy.deepcopy(list(self.fake.order_book.values())),
                        "holdings": copy.deepcopy(self.fake._holdings), "positions": self.fake._net_rows(),
                        "day_offset": self.fake.day_offset}
            if op == "log":
                return copy.deepcopy(self.log)
        raise HarnessError(f"unknown control op {op!r}")


def _make_handler(broker: Broker):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"         # one request per connection: a dropped response is a closed socket

        def log_message(self, *args):         # quiet
            pass

        def _send(self, body: bytes, entry=None, content_type="application/json"):
            try:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                outcome = "written"
            except OSError:
                outcome = "write_failed"
            if entry is not None:
                entry["response"] = outcome if content_type == "application/json" else "malformed"
                entry["t_response"] = time.monotonic()

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                request = decode(body)
            except ValueError:
                self._send(encode({"ok": False, "error": {"type": "HarnessError", "message": "bad request"}}))
                return
            if self.path == "/rpc":
                mode, payload, entry = broker.rpc(request)
                if mode == "drop":
                    entry["response"] = "dropped"
                    entry["t_response"] = time.monotonic()
                    self.close_connection = True
                    return                    # no status line, no body: the client sees a closed connection
                if mode == "malformed":
                    self._send(b"<html>not json", entry, content_type="text/html")
                    return
                self._send(encode(payload), entry)
            elif self.path == "/control":
                try:
                    result = broker.control(request)
                    self._send(encode({"ok": True, "result": result}))
                except Exception as e:
                    self._send(encode({"ok": False, "error": exception_spec(e)}))
            else:
                self.send_error(404)

    return Handler


def serve(config_path: str) -> int:
    import ipaddress
    from http.server import ThreadingHTTPServer

    import child_bootstrap
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    host = config.get("host", "127.0.0.1")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        sys.stderr.write(f"refused: the shared FakeKite binds loopback only, not {host!r}\n")
        sys.stderr.flush()
        return 2
    child_bootstrap.require_safe_paths(config["root"], config_path, os.environ.get(child_bootstrap.REPORT_ENV))
    broker = Broker(FakeKite(**(config.get("fake") or {})), max_hold=float(config.get("max_hold_seconds", 60)))
    server = ThreadingHTTPServer((host, 0), _make_handler(broker))
    server.daemon_threads = True

    def watchdog():                           # the parent's pipe closed (or it asked to stop): exit
        try:
            sys.stdin.read()
        except Exception:
            pass
        broker.stopping.set()

    def stopper():
        broker.stopping.wait()
        broker.release_all()
        server.shutdown()

    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=stopper, daemon=True).start()
    child_bootstrap.write_report()            # a report exists from the moment the parent knows this process
    sys.stdout.write(f"ready {server.server_address[0]} {server.server_address[1]} {os.getpid()}\n")
    sys.stdout.flush()
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        server.server_close()
        child_bootstrap.write_report()
    return 0


if __name__ == "__main__":
    sys.exit(serve(sys.argv[1]))
