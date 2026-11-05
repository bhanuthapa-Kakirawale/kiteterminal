"""The LIVE-entry hard gate (backend/live_entry_gate.py) at its PRODUCTION value.

The frozen order path (backend/execution.py) places the protective sell GTT for the full ticket
quantity as soon as Kite accepts an entry, before it fills. Until a separately authorised change
fixes that, every LIVE new entry is refused server-side, whatever route reached the confirm path,
and LIVE cannot even be armed. Dry run, exits and Emergency Exit are unaffected.

The conftest `env` fixture lifts the gate so the rest of the suite can test the order path behind
it; every test here first puts it back to the production value (`gate_closed`).

FakeKite and scratch databases only: nothing here can reach a broker.
"""

import ast
from pathlib import Path

import pytest

from backend import account_safety, execution, live_entry_gate
from conftest import approval_body, make_live_ready, restore_interim_guards, set_mode, ticket_row
from test_baseline_api import _insert_ticket, _manual_ticket

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def gate_closed(monkeypatch):
    """The production build: what the source says, not what conftest lifted."""
    monkeypatch.setattr(live_entry_gate, "LIVE_ENTRIES_ENABLED", _source_constant())
    restore_interim_guards(monkeypatch)


def _source_constant():
    tree = ast.parse((ROOT / "backend" / "live_entry_gate.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "LIVE_ENTRIES_ENABLED" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("LIVE_ENTRIES_ENABLED is not assigned in backend/live_entry_gate.py")


def _nothing_sent(env) -> None:
    for name in ("place_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt", "order_margins"):
        assert env.kite.calls_to(name) == [], name


def _confirm(env, ticket_id: int, **body):
    payload = approval_body(env.db, ticket_id)
    payload.update(body)
    return env.client.post(f"/api/tickets/{ticket_id}/confirm", json=payload)


def _codes(r) -> set[str]:
    detail = r.json()["detail"]
    return {f["code"] for f in detail.get("failures", [])} | {detail["error_code"]}


# ---------------------------------------------------------------- the constant itself

def test_01_the_gate_is_closed_in_the_source():
    assert _source_constant() is False


def test_02_no_environment_variable_or_setting_can_lift_it():
    text = (ROOT / "backend" / "live_entry_gate.py").read_text(encoding="utf-8")
    assert "os.environ" not in text and "getenv" not in text and "import os" not in text
    # Nothing else in the backend assigns it.
    for path in (ROOT / "backend").rglob("*.py"):
        if path.name == "live_entry_gate.py":
            continue
        assert "LIVE_ENTRIES_ENABLED" not in path.read_text(encoding="utf-8"), path


def test_03_frozen_execution_file_is_unchanged_while_the_gate_is_closed():
    """The gate exists because of execution.py's frozen GTT-before-fill behaviour. If that file
    changes, lifting the gate is a separate, deliberate decision: this test pins both together."""
    import hashlib
    import json
    manifest = json.loads((ROOT / "research" / "pk25" / "validation_manifest.json").read_text(encoding="utf-8"))
    pinned = manifest["model_freeze"]["production_file_sha256"]["backend/execution.py"]
    data = (ROOT / "backend" / "execution.py").read_bytes()
    actual = {hashlib.sha256(data).hexdigest(), hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()}
    assert pinned in actual
    assert _source_constant() is False


# ---------------------------------------------------------------- every entry path is refused

def test_04_manual_live_entry_is_refused_and_nothing_is_sent(env, market_open, gate_closed):
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket(entry_order_type="MARKET")).json()["data"]
    r = _confirm(env, t["id"])
    assert r.status_code == 409, r.text
    assert "LIVE_ENTRIES_DISABLED" in _codes(r)
    assert "execution.py" in r.json()["detail"]["message"]
    row = ticket_row(env.db, t["id"])
    assert row["status"] == "pending_confirmation" and row["submission_state"] is None
    _nothing_sent(env)


def test_05_quick_action_buy_is_refused(env, market_open, gate_closed):
    set_mode(env.db, "live")
    r = env.client.post("/api/tickets/alert-action/buy", json={
        "symbol": "TCS", "transaction_type": "BUY", "quantity": 1, "entry_price": 3000.0,
        "entry_order_type": "MARKET", "stop_loss": 2900.0, "target_1": 3300.0, "product_type": "CNC",
        "rationale": "quick action"})
    assert r.status_code == 201, r.text
    t = r.json()["data"]
    assert t["status"] == "pending_confirmation", t
    r = _confirm(env, t["id"])
    assert r.status_code == 409 and "LIVE_ENTRIES_DISABLED" in _codes(r), r.text
    _nothing_sent(env)


def test_06_strategy_entry_is_refused(env, market_open, gate_closed):
    set_mode(env.db, "live")
    tid = _insert_ticket(env.db, source="strategy", strategy_name="momentum_breakout", status="pending_confirmation",
                         mode=None, entry_order_type="MARKET", signal_date=None)
    r = _confirm(env, tid)
    assert r.status_code == 409 and "LIVE_ENTRIES_DISABLED" in _codes(r), r.text
    _nothing_sent(env)


def test_07_dry_run_entry_still_works(env, market_open, gate_closed):
    set_mode(env.db, "dry_run")
    t = env.client.post("/api/tickets", json=_manual_ticket(entry_order_type="MARKET")).json()["data"]
    r = _confirm(env, t["id"])
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "executed"
    assert r.json()["data"]["kite_order_id"].startswith("PAPER-")
    _nothing_sent(env)  # the paper broker, never Kite


def test_08_a_live_exit_is_not_refused(env, market_open, gate_closed):
    set_mode(env.db, "live")
    pos = _insert_ticket(env.db, entry_fill_status="filled", filled_quantity=5, kite_order_id="250000000900",
                         kite_gtt_id=None, use_gtt=0)
    exit_id = _insert_ticket(env.db, transaction_type="SELL", closes_ticket_id=pos, status="pending_confirmation",
                             entry_order_type="MARKET", use_gtt=0)
    r = _confirm(env, exit_id)
    assert r.status_code == 200, r.text
    assert len(env.kite.calls_to("place_order")) == 1
    assert env.kite.calls_to("place_gtt") == []


def test_09_emergency_exit_is_not_refused(env, market_open, gate_closed):
    pos = _insert_ticket(env.db, entry_fill_status="filled", filled_quantity=5, kite_order_id="250000000901",
                         kite_gtt_id=None, use_gtt=0)
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    assert r.status_code == 200, r.text
    results = r.json()["data"]["results"]
    assert [x["status"] for x in results] == ["exit_submitted"], results
    assert ticket_row(env.db, results[0]["ticket_id"])["closes_ticket_id"] == pos
    assert env.kite.calls_to("place_gtt") == []


# ---------------------------------------------------------------- the three enforcement layers

def test_10_final_gate_refuses_before_any_broker_read(env, gate_closed):
    tid = _insert_ticket(env.db, status="confirmed", mode="live", entry_order_type="MARKET", source="strategy",
                         strategy_name="momentum_breakout")
    conn = env.db.get_connection()
    try:
        row = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (tid,)).fetchone()
        account_id = row["account_id"]
        env.kite.calls.clear()
        decision = account_safety.final_gate(conn, account_id, row, env.kite, None)
    finally:
        conn.close()
    assert [f.code for f in decision.failures] == ["LIVE_ENTRIES_DISABLED"]
    assert env.kite.calls == []  # not even the LTP read


def test_11_backstop_before_the_broker_call_holds_if_both_orchestrators_were_skipped(env, market_open, gate_closed,
                                                                                    monkeypatch):
    """A path that skipped precheck and the final gate (simulated) still cannot send a LIVE entry:
    the copy next to the only execute_ticket call refuses it and withdraws the claim."""
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket(entry_order_type="MARKET")).json()["data"]
    passed = account_safety.SafetyDecision(variety="regular")
    monkeypatch.setattr(account_safety, "precheck", lambda *a, **k: passed)
    monkeypatch.setattr(account_safety, "final_gate", lambda *a, **k: account_safety.SafetyDecision())
    r = _confirm(env, t["id"])
    assert r.status_code == 409 and _codes(r) == {"LIVE_ENTRIES_DISABLED"}, r.text
    row = ticket_row(env.db, t["id"])
    assert row["status"] == "pending_confirmation", row
    assert env.kite.calls_to("place_order") == [] and env.kite.calls_to("place_gtt") == []


def test_12_live_readiness_blocks_and_live_cannot_be_armed(env, gate_closed):
    make_live_ready(env)
    r = env.client.get("/api/live-readiness", params={"broker": "true", "refresh": "true"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    gate = next(c for c in data["checks"] if c["code"] == "LIVE_ENTRY_GATE")
    assert gate["status"] == "FAIL" and gate["blocking"] is True
    assert "LIVE_ENTRY_GATE" in data["blocking_codes"] and data["overall_status"] != "READY"
    r = env.client.post("/api/execution/mode", json={"mode": "live", "confirm_real_money": True})
    assert r.status_code == 409, r.text
    assert "LIVE_ENTRY_GATE" in r.json()["detail"]["blocking_codes"]
    assert execution.get_execution_mode() == "dry_run"


def test_13_with_the_gate_lifted_in_test_readiness_passes_the_gate_check(env):
    make_live_ready(env)
    data = env.client.get("/api/live-readiness", params={"broker": "true", "refresh": "true"}).json()["data"]
    gate = next(c for c in data["checks"] if c["code"] == "LIVE_ENTRY_GATE")
    assert gate["status"] == "PASS"


# ---------------------------------------------------------------- structure: one way to the broker

def _calls(path: Path, attr: str) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(func):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr:
                    found.append((func.name, node.lineno))
    return found


def test_14_execute_ticket_has_exactly_one_caller():
    callers = []
    for path in (ROOT / "backend").rglob("*.py"):
        for name, line in _calls(path, "execute_ticket"):
            callers.append((path.relative_to(ROOT).as_posix(), name))
    assert sorted(set(callers)) == [("backend/routes/tickets.py", "confirm_and_execute_ticket")], callers


def test_15_only_the_frozen_order_path_places_orders_or_gtts():
    for attr in ("place_order", "place_gtt", "modify_order"):
        offenders = []
        for path in (ROOT / "backend").rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel == "backend/execution.py":
                continue
            offenders += [(rel, name, line) for name, line in _calls(path, attr)]
        assert offenders == [], (attr, offenders)


def test_16_the_backstop_sits_immediately_before_the_broker_call():
    source = (ROOT / "backend" / "routes" / "tickets.py").read_text(encoding="utf-8")
    body = source[source.index("def confirm_and_execute_ticket("):]
    backstop = body.index("_refuse_disabled_live_entry(conn, account, ticket)")
    call = body.index("execution.execute_ticket(broker, ticket")
    assert backstop < call
    assert "_account_safety_" not in body[backstop:call] and "_verify_claimed_content" not in body[backstop:call]
