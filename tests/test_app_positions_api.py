"""PK-19: the app-managed Positions API.

GET /api/app-positions?view=active|closed — UI-ready positions described by
the server-derived lifecycle; POST /api/app-positions/{id}/exit — creates
(never confirms) one linked exit ticket for the open quantity. Reading is
broker-free and costs a constant number of queries.
"""

import pytest
import requests

from conftest import TERMINAL_ORIGIN, account_id, set_mode, ticket_row
from test_baseline_api import _insert_ticket
from test_live_safety import _closed_trades_for, _monitor, _rows, _sells, market_open  # noqa: F401
from test_partial_fills import _exit_ticket, _fill, _held_position, _place_entry, _place_exit


def _get(env, view="active"):
    r = env.client.get(f"/api/app-positions?view={view}")
    assert r.status_code == 200, r.text
    return r.json()


def _by_id(body):
    return {p["position_id"]: p for p in body["data"]}


def _exit(env, position_id):
    return env.client.post(f"/api/app-positions/{position_id}/exit")


# ================================================================ reading

def test_active_excludes_closed_and_history_includes_it(env):
    open_id, _ = _held_position(env, 40)
    closed_id, _ = _held_position(env, 20, closed_at="2026-09-10 05:00:00", kite_gtt_id=None)
    pending_id = _insert_ticket(env.db, status="pending_confirmation", mode=None)
    rejected_id = _insert_ticket(env.db, status="rejected", mode=None)

    active, closed = _by_id(_get(env)), _by_id(_get(env, "closed"))
    assert set(active) == {open_id}
    assert set(closed) == {closed_id}
    assert {pending_id, rejected_id}.isdisjoint(set(active) | set(closed))  # never positions
    assert closed[closed_id]["lifecycle_state"] == "CLOSED"


def test_quantities_and_active_exit_are_served(env, market_open):
    buy_id, _ = _held_position(env, 40)
    sell_id, order_id = _place_exit(env, buy_id, 40)
    _fill(env, order_id, 15, price=3100.0)
    _monitor(env)

    p = _by_id(_get(env))[buy_id]
    assert (p["requested_quantity"], p["filled_quantity"], p["open_quantity"], p["exited_quantity"]) == (40, 40, 25, 15)
    assert p["active_exit"]["ticket_id"] == sell_id
    assert (p["exit_requested_quantity"], p["exit_filled_quantity"], p["remaining_quantity"]) == (40, 15, 25)
    assert p["lifecycle_state"] == "EXIT_PARTIAL"


def test_partial_entry_quantities(env, market_open):
    buy_id, order_id, _ = _place_entry(env, 100)
    _fill(env, order_id, 40, status="CANCELLED")
    _monitor(env)
    p = _by_id(_get(env))[buy_id]
    assert (p["lifecycle_state"], p["requested_quantity"], p["filled_quantity"], p["open_quantity"]) == (
        "OPEN", 100, 40, 40)
    assert p["entry_partial"] is True


@pytest.mark.parametrize("state,expected", [
    ("open", {"EXIT", "EMERGENCY_EXIT", "VIEW_DETAIL"}),
    ("exiting", {"CANCEL_ORDER", "VIEW_DETAIL"}),
    ("closed", {"VIEW_JOURNAL", "VIEW_DETAIL"}),
])
def test_allowed_actions_per_state(env, market_open, state, expected):
    buy_id, _ = _held_position(env, 40)
    view = "active"
    if state in ("exiting", "closed"):
        _, order_id = _place_exit(env, buy_id, 40)
        if state == "closed":
            _fill(env, order_id, 40, status="COMPLETE", price=3100.0)
            _monitor(env)
            view = "closed"
    p = _by_id(_get(env, view))[buy_id]
    assert set(p["allowed_actions"]) == expected


def test_active_summary_counts(env, market_open):
    _place_entry(env, 100)                                     # waiting to fill (first: TCS exposure limit)
    _held_position(env, 40)                                    # open
    _held_position(env, 10, kite_gtt_id=None)                  # open, unprotected
    exiting_id, _ = _held_position(env, 5)
    _place_exit(env, exiting_id, 5)                            # exiting
    s = _get(env)["summary"]
    assert (s["open"], s["waiting_to_fill"], s["exiting"], s["unprotected"], s["total"]) == (2, 1, 1, 1, 4)
    assert s["needs_attention"] >= 1


def test_dry_run_positions_are_clearly_identified(env):
    dry_id = _insert_ticket(env.db, mode="dry_run", entry_fill_status="filled", filled_quantity=5,
                            filled_price=3000.0)
    set_mode(env.db, "live")
    p = _by_id(_get(env))[dry_id]
    assert p["mode"] == "dry_run" and p["protection_status"] == "NOT_APPLICABLE"
    assert "EMERGENCY_EXIT" not in p["allowed_actions"]  # Emergency Exit never touches dry-run positions


def test_reading_positions_makes_no_broker_call(env, market_open):
    _held_position(env, 40)
    buy_id, _ = _held_position(env, 30)
    _place_exit(env, buy_id, 30)
    calls = len(env.kite.calls)
    _get(env)
    _get(env, "closed")
    env.client.get("/api/tickets")
    assert env.kite.calls[calls:] == []


def _count_selects(env, monkeypatch, path) -> int:
    """SELECTs one request makes through the route modules' connections."""
    from backend import db
    from backend.routes import positions, tickets
    statements: list[str] = []

    def traced():
        conn = db.get_connection()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(positions, "get_connection", traced)
    monkeypatch.setattr(tickets, "get_connection", traced)
    assert env.client.get(path).status_code == 200
    return sum(s.lstrip().upper().startswith("SELECT") for s in statements)


@pytest.mark.parametrize("path", ["/api/app-positions", "/api/app-positions?view=closed", "/api/tickets"])
def test_query_count_does_not_grow_with_positions(env, monkeypatch, path):
    buy_id, _ = _held_position(env, 40)
    _exit_ticket(env, buy_id, 40)
    _held_position(env, 5, closed_at="2026-09-10 05:00:00", kite_gtt_id=None)
    one = _count_selects(env, monkeypatch, path)
    for n in range(8):
        pid, _ = _held_position(env, 5 + n)
        _exit_ticket(env, pid, 5 + n)
        _held_position(env, 5, closed_at="2026-09-10 05:00:00", kite_gtt_id=None)
    many = _count_selects(env, monkeypatch, path)
    assert many == one, (one, many)
    # PK-19's four, plus PK-20's one batched read of the paper order book.
    assert one <= 5


def test_reconciliation_issues_are_attached_structurally(env):
    from backend import reconciliation
    buy_id, gtt_id = _held_position(env, 40, gtt_quantity=100)
    reconciliation._run_cycle()
    p = _by_id(_get(env))[buy_id]
    codes = {i["code"] for i in p["reconciliation_issues"]}
    assert "GTT_QUANTITY_TOO_HIGH" in codes
    assert p["protection_status"] == "QUANTITY_MISMATCH" and p["review_required"] is True
    assert all({"id", "code", "label", "severity", "detail"} <= set(i) for i in p["reconciliation_issues"])


# ============================================================== the exit action

def test_exit_creates_one_pending_exit_ticket_and_sends_nothing(env, market_open):
    set_mode(env.db, "live")
    buy_id, _ = _held_position(env, 40)
    before_orders = len(env.kite.calls_to("place_order"))

    r = _exit(env, buy_id)
    assert r.status_code == 201, r.text
    t = r.json()["data"]
    assert (t["status"], t["transaction_type"], t["entry_order_type"], t["quantity"]) == (
        "pending_confirmation", "SELL", "MARKET", 40)
    assert t["closes_ticket_id"] == buy_id and t["mode"] == "live"
    assert t["lifecycle"]["lifecycle_state"] == "OPEN"  # nothing happened to the position yet
    assert len(env.kite.calls_to("place_order")) == before_orders

    p = _by_id(_get(env))[buy_id]
    assert p["pending_exit_ticket_id"] == t["id"]
    assert "REVIEW_EXIT" in p["allowed_actions"] and "EXIT" not in p["allowed_actions"]

    again = _exit(env, buy_id)
    assert again.status_code == 409 and again.json()["detail"]["error_code"] == "EXIT_ALREADY_PENDING"
    assert again.json()["detail"]["ticket_id"] == t["id"]
    assert len(_rows(env, "SELECT id FROM trade_tickets WHERE closes_ticket_id = ?", buy_id)) == 1


def test_live_exit_through_the_normal_approval_is_exiting_then_closed(env, market_open):
    set_mode(env.db, "live")
    env.kite.auto_fill = False
    buy_id, _ = _held_position(env, 40)
    t = _exit(env, buy_id).json()["data"]
    done = env.client.post(f"/api/tickets/{t['id']}/confirm").json()["data"]
    assert done["status"] == "executed"
    assert done["lifecycle"]["lifecycle_state"] == "EXITING"  # not CLOSED on acceptance (PK-8)
    assert [s["quantity"] for s in _sells(env)] == [40]

    _fill(env, done["kite_order_id"], 40, status="COMPLETE", price=3120.0)
    _monitor(env)
    assert buy_id in _by_id(_get(env, "closed"))
    assert _closed_trades_for(env, buy_id)[0]["exit_price"] == 3120.0


def test_dry_run_position_can_be_exited_and_closes_on_the_paper_fill(env, market_open):
    # PK-20 (was: "closes on approval"): approval makes it EXITING; only the
    # paper broker's fill closes and journals it — the PK-8 rule, in dry run.
    set_mode(env.db, "live")  # the global switch never decides an exit's mode (PK-3)
    dry_id = _insert_ticket(env.db, mode="dry_run", entry_fill_status="filled", filled_quantity=5,
                            filled_price=3000.0, kite_gtt_id=None)
    t = _exit(env, dry_id).json()["data"]
    assert t["mode"] == "dry_run"
    done = env.client.post(f"/api/tickets/{t['id']}/confirm").json()["data"]
    assert done["status"] == "executed" and done["lifecycle"]["lifecycle_state"] == "EXITING"
    assert _closed_trades_for(env, dry_id) == []

    from backend import order_monitor
    order_monitor.check_paper_orders(env.kite)
    closed = {p["position_id"]: p for p in env.client.get("/api/app-positions?view=closed").json()["data"]}
    assert closed[dry_id]["lifecycle_state"] == "CLOSED"
    assert env.kite.calls_to("place_order") == []
    assert len(_closed_trades_for(env, dry_id)) == 1


@pytest.mark.parametrize("state,code", [
    ("exiting", "EXIT_NOT_ALLOWED"),
    ("unknown_exit", "EXIT_NOT_ALLOWED"),
    ("closed", "EXIT_NOT_ALLOWED"),
    ("entry_working", "EXIT_NOT_ALLOWED"),
    ("pending_exit", "EXIT_ALREADY_PENDING"),
])
def test_exit_is_refused_where_the_lifecycle_does_not_allow_it(env, market_open, state, code):
    set_mode(env.db, "live")
    env.kite.auto_fill = False
    if state == "entry_working":
        buy_id = _place_entry(env, 100)[0]
    else:
        buy_id, _ = _held_position(env, 40, **({"closed_at": "2026-09-10 05:00:00", "kite_gtt_id": None}
                                               if state == "closed" else {}))
    if state == "exiting":
        _place_exit(env, buy_id, 40)
    elif state == "unknown_exit":
        sell_id = _exit_ticket(env, buy_id, 40)
        env.kite.raise_on_next("place_order", requests.ReadTimeout("Read timed out."))
        env.client.post(f"/api/tickets/{sell_id}/confirm")
    elif state == "pending_exit":
        _exit_ticket(env, buy_id, 40)
    tickets_before = _rows(env, "SELECT COUNT(*) AS n FROM trade_tickets")[0]["n"]
    orders_before = len(env.kite.calls_to("place_order"))

    r = _exit(env, buy_id)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error_code"] == code
    assert _rows(env, "SELECT COUNT(*) AS n FROM trade_tickets")[0]["n"] == tickets_before
    assert len(env.kite.calls_to("place_order")) == orders_before


def test_exit_of_an_unknown_id_or_an_exit_ticket_is_404(env, market_open):
    buy_id, _ = _held_position(env, 40)
    sell_id = _exit_ticket(env, buy_id, 40)
    assert _exit(env, 99999).status_code == 404
    assert _exit(env, sell_id).status_code == 404


def test_exit_without_the_terminal_headers_is_refused(env):
    from fastapi.testclient import TestClient
    from backend.main import app
    buy_id, _ = _held_position(env, 40)
    bare = TestClient(app, base_url=TERMINAL_ORIGIN)
    r = bare.post(f"/api/app-positions/{buy_id}/exit", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json()["detail"]["error_code"] == "CSRF_PROTECTION_FAILED"
    assert _rows(env, "SELECT id FROM trade_tickets WHERE closes_ticket_id = ?", buy_id) == []
    assert ticket_row(env.db, buy_id)["closed_at"] is None


def test_emergency_exit_is_unchanged_by_the_shared_builder(env, market_open):
    set_mode(env.db, "live")
    env.kite.auto_fill = False
    buy_id, _ = _held_position(env, 40)
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    [result] = r.json()["data"]["results"]
    assert result["status"] == "exit_submitted"
    [sell] = _rows(env, "SELECT * FROM trade_tickets WHERE closes_ticket_id = ?", buy_id)
    assert sell["rationale"] == "Emergency exit — all positions" and sell["rule_checklist"] == (
        '{"emergency_exit_all": true}')
    preview = env.client.get("/api/kill-switch/emergency-exit-preview").json()["data"]
    assert preview[0]["lifecycle_state"] == "EXITING"
    assert account_id(env.db)
