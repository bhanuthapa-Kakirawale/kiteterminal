"""G7: the trading mode fails closed, at the broker boundary.

Exactly "dry_run" routes to the paper broker and exactly "live" to Kite (where every live gate
still applies). NULL, missing, malformed or unrecognised modes refuse every broker write before
any call, wherever they come from: the global execution setting, a ticket row, a position row.
The rule lives in backend/paper_broker.py (check_mode / broker_for), which
backend/execution.py (frozen, unchanged) already calls before any broker call; the confirm
route also refuses such a mode before the claim. No environment variable, request field or
ordinary setting is consulted.

FakeKite and scratch databases only.
"""

import ast
from pathlib import Path

import pytest

from backend import execution, paper_broker, position_quantity
from conftest import account_id, approval_body, set_mode, ticket_row
from test_baseline_api import _insert_ticket, _manual_ticket

ROOT = Path(__file__).resolve().parents[1]


class _StrMode(str):
    pass


BAD_MODES = [None, "", "LIVE", "Live", " live", "live ", "DRY_RUN", "dry-run", "paper", "real", 1, 0, True,
             b"live", _StrMode("live"), ["live"]]
BAD_IDS = ["None", "empty", "LIVE", "Live", "lead-space", "trail-space", "DRY_RUN", "dry-dash", "paper", "real",
           "int1", "int0", "bool", "bytes", "str-subclass", "list"]


def _no_broker_calls(kite) -> None:
    writes = ("place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt")
    assert [name for name, _ in kite.calls if name in writes] == [], kite.calls


# ---------------------------------------------------------------- the boundary itself

@pytest.mark.parametrize("mode", BAD_MODES, ids=BAD_IDS)
def test_01_check_mode_refuses_every_unrecognised_mode(env, mode):
    with pytest.raises(paper_broker.InvalidTradingMode):
        paper_broker.check_mode(env.kite, mode)
    conn = env.db.get_connection()
    try:
        with pytest.raises(paper_broker.InvalidTradingMode):
            paper_broker.check_mode(paper_broker.PaperBroker(conn, account_id(env.db)), mode)
    finally:
        conn.close()


@pytest.mark.parametrize("mode", BAD_MODES, ids=BAD_IDS)
def test_02_broker_for_routes_no_unrecognised_mode_anywhere(env, mode):
    conn = env.db.get_connection()
    try:
        with pytest.raises(paper_broker.InvalidTradingMode):
            paper_broker.broker_for(mode, env.kite, conn, account_id(env.db))
    finally:
        conn.close()


def test_03_only_the_exact_modes_route(env):
    conn = env.db.get_connection()
    try:
        assert paper_broker.broker_for("live", env.kite, conn, 1) is env.kite
        assert paper_broker.is_paper_broker(paper_broker.broker_for("dry_run", env.kite, conn, 1))
    finally:
        conn.close()
    paper_broker.check_mode(env.kite, "live")                       # no raise
    with pytest.raises(paper_broker.ModeIsolationError):
        paper_broker.check_mode(env.kite, "dry_run")                # dry run never reaches Kite


def test_04_an_invalid_mode_is_a_mode_isolation_refusal():
    # execution.execute_ticket (frozen) turns ModeIsolationError into a definite failure.
    assert issubclass(paper_broker.InvalidTradingMode, paper_broker.ModeIsolationError)


@pytest.mark.parametrize("mode", [None, "LIVE", " live", "paper"], ids=["None", "LIVE", "space", "paper"])
def test_05_execute_ticket_with_an_invalid_mode_sends_nothing(env, mode):
    row = dict(ticket_row(env.db, _insert_ticket(env.db, status="confirmed")))
    row["mode"] = mode
    result = execution.execute_ticket(env.kite, row)
    assert (result.success, result.status, result.event_type) == (False, "failed", "execution_failed")
    assert "Refused before any broker call" in result.message
    assert env.kite.calls == []


# ---------------------------------------------------------------- the confirm route

def _pending_entry(env) -> int:
    r = env.client.post("/api/tickets", json=_manual_ticket())
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


@pytest.mark.parametrize("mode", [None, "LIVE", " live", "paper", 1], ids=["None", "LIVE", "space", "paper", "int"])
def test_06_a_bad_global_mode_is_refused_before_the_claim(env, market_open, monkeypatch, mode):
    set_mode(env.db, "live")
    ticket_id = _pending_entry(env)
    body = approval_body(env.db, ticket_id)
    monkeypatch.setattr(execution, "get_execution_mode", lambda: mode)
    r = env.client.post(f"/api/tickets/{ticket_id}/confirm", json=body)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error_code"] == "INVALID_EXECUTION_MODE"
    row = ticket_row(env.db, ticket_id)
    assert (row["status"], row["submission_state"], row["mode"]) == ("pending_confirmation", None, None)
    _no_broker_calls(env.kite)


def test_07_a_malformed_mode_in_the_database_is_refused(env, market_open):
    """A database whose CHECK constraint was bypassed (an old schema, a manual edit)."""
    set_mode(env.db, "live")
    ticket_id = _pending_entry(env)
    body = approval_body(env.db, ticket_id)
    conn = env.db.get_connection()
    try:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE execution_settings SET mode = 'LIVE' WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
    r = env.client.post(f"/api/tickets/{ticket_id}/confirm", json=dict(body, expected_mode=None))
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "INVALID_EXECUTION_MODE", r.text
    assert ticket_row(env.db, ticket_id)["status"] == "pending_confirmation"
    _no_broker_calls(env.kite)


def test_08_an_exit_of_a_position_with_a_malformed_mode_is_refused(env, market_open):
    pos = _insert_ticket(env.db, entry_fill_status="filled", filled_quantity=5, kite_order_id="250000000901")
    conn = env.db.get_connection()
    try:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE trade_tickets SET mode = 'Live' WHERE id = ?", (pos,))
        conn.commit()
    finally:
        conn.close()
    exit_id = _insert_ticket(env.db, transaction_type="SELL", closes_ticket_id=pos, status="pending_confirmation",
                             entry_order_type="MARKET", mode=None)
    r = env.client.post(f"/api/tickets/{exit_id}/confirm")
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "INVALID_EXECUTION_MODE", r.text
    _no_broker_calls(env.kite)


def test_09_a_row_changed_after_the_claim_is_a_definite_failure_not_unknown(env, market_open, monkeypatch):
    """Defence in depth: if the claimed row's mode is somehow invalid at broker_for, the result
    is 'failed, nothing sent' — never an UNKNOWN the monitor would chase."""
    set_mode(env.db, "live")
    ticket_id = _pending_entry(env)
    real = paper_broker.broker_for

    def poisoned(mode, kite, conn, acct):
        return real("LIVE", kite, conn, acct)
    monkeypatch.setattr(paper_broker, "broker_for", poisoned)
    r = env.client.post(f"/api/tickets/{ticket_id}/confirm", json=approval_body(env.db, ticket_id))
    assert r.status_code == 200, r.text
    row = ticket_row(env.db, ticket_id)
    assert row["status"] == "failed" and row["kite_order_id"] is None
    _no_broker_calls(env.kite)


# ---------------------------------------------------------------- other broker-write paths

def test_10_protection_upkeep_refuses_a_position_with_an_invalid_mode(env):
    pos = _insert_ticket(env.db, entry_fill_status="filled", filled_quantity=5, kite_gtt_id=9999)
    conn = env.db.get_connection()
    try:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE trade_tickets SET mode = 'LIVE' WHERE id = ?", (pos,))
        conn.commit()
        # Not a broker-tracked position (only exactly 'live', or a paper id, is): nothing to resize.
        assert position_quantity.sync_protection(conn, env.kite, pos, "test").status == "no_position"
        with pytest.raises(paper_broker.InvalidTradingMode):
            position_quantity.remove_protective_gtt(conn, env.kite, None, pos)
    finally:
        conn.close()
    assert env.kite.calls == []


def test_11_the_trailing_watcher_refuses_an_invalid_mode_before_any_call(env):
    from backend import trailing_engine
    conn = env.db.get_connection()
    try:
        with pytest.raises(paper_broker.InvalidTradingMode):
            trailing_engine._tick_mode(conn, env.kite, [{"id": 1, "mode": "LIVE", "kite_gtt_id": None}])
    finally:
        conn.close()
    assert env.kite.calls == []


def test_12_emergency_exit_cancel_checks_the_mode(env):
    from backend.routes import kill_switch
    conn = env.db.get_connection()
    try:
        with pytest.raises(paper_broker.InvalidTradingMode):
            kill_switch._cancel_unfilled_entry(env.kite, conn, {"id": 1, "mode": None, "symbol": "TCS",
                                                                "order_variety": "regular", "kite_order_id": "1"})
    finally:
        conn.close()
    assert env.kite.calls_to("cancel_order") == []


# ---------------------------------------------------------------- no bypass

@pytest.mark.parametrize("mode", ["LIVE", "Live", "paper", ""])
def test_13_the_mode_api_accepts_only_the_exact_modes(env, mode):
    r = env.client.post("/api/execution/mode", json={"mode": mode, "confirm_real_money": True})
    assert r.status_code == 422, r.text


def test_14_the_confirm_body_accepts_only_the_exact_modes(env):
    ticket_id = _pending_entry(env)
    r = env.client.post(f"/api/tickets/{ticket_id}/confirm", json={"expected_mode": "LIVE"})
    assert r.status_code == 422, r.text
    assert ticket_row(env.db, ticket_id)["status"] == "pending_confirmation"


def test_15_no_environment_variable_selects_or_overrides_the_mode():
    """The mode path reads no environment variable: paper_broker's mode rule, the confirm
    route's claim and the execution-mode setter."""
    for rel in ("backend/paper_broker.py", "backend/live_arming.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
                raise AssertionError(f"{rel}:{node.lineno} reads the environment")
    source = (ROOT / "backend/routes/tickets.py").read_text(encoding="utf-8")
    claim = source[source.index("def _claim_mode"):source.index("def _require_displayed_mode")]
    assert "environ" not in claim and "getenv" not in claim


def test_16_execution_py_is_byte_identical_to_the_pk25_freeze():
    import json
    from backend import smart_money_validation as smv
    text = (ROOT / "research/pk25/validation_manifest.json").read_text(encoding="utf-8")
    hashes = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "backend/execution.py" and isinstance(v, str):
                    hashes.append(v)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(json.loads(text))
    assert hashes and set(hashes) == {smv.normalised_sha256(ROOT / "backend/execution.py")}
