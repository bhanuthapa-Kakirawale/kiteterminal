"""DD-6 in production: what happens when the broker returns a GTT id another ticket already holds
(docs/PHASE_3_V12_REGISTRATION_REMEDIATION_REPORT.md §7).

Under migration 12 idx_trade_tickets_unique_gtt makes a duplicate GTT id a sqlite3.IntegrityError. The two places
the approval route writes a broker GTT id (routes/tickets.py: _record_execution_result, after the broker accepted
the order, and _reconcile_late_result, the late attach) must never turn that into an unhandled 500: the order is
still tracked, the GTT is NOT linked to a second ticket, and an urgent AMBIGUOUS_GTT_OWNERSHIP finding names it.
Each case runs with the unique index (the journal on env's database: the dark schema on today's build, v12 on a
build that registers it) and on a plain v11 database without it (same outcome through the explicit checks).
Dry-run only (the paper broker); FakeKite records no broker call. Every database is a temporary file."""

import threading

import pytest

from conftest import approval_body, set_mode, ticket_row
from protection_helpers import journal
from test_baseline_api import _insert_ticket, _manual_ticket


def _has_unique_index(env) -> bool:
    conn = env.db.get_connection()
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_trade_tickets_unique_gtt'").fetchone() is not None
    finally:
        conn.close()


@pytest.fixture(params=["unique_index", "v11_no_index"])
def gtt_env(request, market_open):   # an entry approved outside market hours would need an AMO confirmation
    if request.param == "v11_no_index":
        request.node.add_marker("v11_database")
    env = request.getfixturevalue("env")
    if request.param == "unique_index":
        journal(env)
    assert _has_unique_index(env) is (request.param == "unique_index")
    return env


def _rows(env, sql, *args):
    conn = env.db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _findings(env, gtt_id):
    return _rows(env, "SELECT ticket_id, issue_code, severity, suggested_action FROM reconciliation_log "
                      "WHERE gtt_id = ? ORDER BY id", gtt_id)


def _approve_dry_run(env):
    set_mode(env.db, "dry_run")
    t = env.client.post("/api/tickets", json=_manual_ticket(quantity=5, entry_order_type="MARKET")).json()["data"]
    assert t["status"] == "pending_confirmation", t
    return t["id"], env.client.post(f"/api/tickets/{t['id']}/confirm",
                                    json=approval_body(env.db, t["id"], expected_mode="dry_run"))


# ================================================================ the main write (_record_execution_result)

def test_g01_a_duplicate_gtt_returned_for_an_approved_order_is_tracked_not_linked_and_flagged(gtt_env):
    env = gtt_env
    holder = _insert_ticket(env.db, mode="dry_run", kite_gtt_id="PAPER-G-1", kite_order_id="PAPER-O-99")
    mark = len(env.kite.calls)
    ticket_id, response = _approve_dry_run(env)                        # the paper broker's first GTT: PAPER-G-1
    assert response.status_code == 200, response.text                  # never a 500
    row = ticket_row(env.db, ticket_id)
    assert row["status"] == "executed" and row["kite_order_id"].startswith("PAPER-O-")   # the order is tracked
    assert row["kite_gtt_id"] is None                                  # the GTT is not linked to a second ticket
    assert ticket_row(env.db, holder)["kite_gtt_id"] == "PAPER-G-1"    # the holder keeps its own
    events = _rows(env, "SELECT detail FROM ticket_events WHERE ticket_id = ? AND event_type = 'modified'", ticket_id)
    assert any("PAPER-G-1" in e["detail"] and f"ticket {holder}" in e["detail"] for e in events)
    assert _findings(env, "PAPER-G-1") == [{"ticket_id": ticket_id, "issue_code": "AMBIGUOUS_GTT_OWNERSHIP",
                                           "severity": "urgent", "suggested_action": "CHECK KITE"}]
    writes = {"place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt"}
    assert [c for c in env.kite.calls[mark:] if c[0] in writes] == []  # dry run: no order or GTT reaches Kite


def test_g02_a_unique_gtt_is_linked_exactly_as_before(gtt_env):
    env = gtt_env
    ticket_id, response = _approve_dry_run(env)
    assert response.status_code == 200, response.text
    assert ticket_row(env.db, ticket_id)["kite_gtt_id"] == "PAPER-G-1"
    assert _findings(env, "PAPER-G-1") == []


def test_g03_a_duplicate_appearing_between_the_check_and_the_write_hits_the_index_backstop(env, monkeypatch,
                                                                                            market_open):
    """The race: the holder commits after _record_execution_result's check. With the unique index the UPDATE
    raises IntegrityError; the route catches it and writes the result without the GTT."""
    from backend.routes import tickets
    journal(env)
    _insert_ticket(env.db, mode="dry_run", kite_gtt_id="PAPER-G-1", kite_order_id="PAPER-O-99")
    real = tickets._gtt_holder
    calls = []

    def missed_first(conn, gtt_id, ticket_id):
        calls.append(gtt_id)
        return None if len(calls) == 1 else real(conn, gtt_id, ticket_id)

    monkeypatch.setattr(tickets, "_gtt_holder", missed_first)
    ticket_id, response = _approve_dry_run(env)
    assert response.status_code == 200, response.text
    assert len(calls) >= 2                                              # the backstop path ran
    row = ticket_row(env.db, ticket_id)
    assert row["status"] == "executed" and row["kite_gtt_id"] is None
    assert [f["issue_code"] for f in _findings(env, "PAPER-G-1")] == ["AMBIGUOUS_GTT_OWNERSHIP"]


def test_g04_an_integrity_error_that_is_not_the_gtt_index_is_not_swallowed(env, monkeypatch):
    from backend import execution
    from backend.routes import tickets
    import sqlite3

    def other_constraint(*args, **kwargs):
        raise sqlite3.IntegrityError("simulated: another constraint")

    conn = env.db.get_connection()
    try:
        ticket = conn.execute("SELECT * FROM trade_tickets LIMIT 1").fetchone() or {"id": 1, "stop_loss": 1.0}
        monkeypatch.setattr(execution, "update_ticket_if", other_constraint)
        result = execution.ExecutionResult(success=True, kite_order_id="PAPER-O-1", kite_gtt_id="PAPER-G-777",
                                           status="executed", event_type="executed", message="m")
        with pytest.raises(sqlite3.IntegrityError, match="another constraint"):
            tickets._record_execution_result(conn, ticket, result)
    finally:
        conn.close()


# ================================================================ the late attach (_reconcile_late_result)

def _late(env, ticket_id, order_id, gtt_id):
    from backend import execution
    from backend.routes import tickets
    conn = env.db.get_connection()
    try:
        claimed = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (ticket_id,)).fetchone()
        tickets._reconcile_late_result(conn, claimed, execution.ExecutionResult(
            success=True, kite_order_id=order_id, kite_gtt_id=gtt_id, status="executed", event_type="executed",
            message="late"))
    finally:
        conn.close()


def test_g05_a_late_result_with_a_duplicate_gtt_is_not_attached_and_is_flagged(gtt_env):
    env = gtt_env
    holder = _insert_ticket(env.db, kite_gtt_id=9001, kite_order_id="O-HOLDER")
    late = _insert_ticket(env.db, kite_gtt_id=None, kite_order_id="O-LATE")
    _late(env, late, "O-LATE", 9001)                                       # no exception
    assert ticket_row(env.db, late)["kite_gtt_id"] is None
    assert ticket_row(env.db, holder)["kite_gtt_id"] == 9001
    assert [f["ticket_id"] for f in _findings(env, 9001)] == [late]
    events = _rows(env, "SELECT detail FROM ticket_events WHERE ticket_id = ?", late)
    assert any("9001" in e["detail"] and f"ticket {holder}" in e["detail"] for e in events)


def test_g06_a_late_result_with_a_unique_gtt_is_attached_as_before(gtt_env):
    env = gtt_env
    late = _insert_ticket(env.db, kite_gtt_id=None, kite_order_id="O-LATE")
    _late(env, late, "O-LATE", 9002)
    assert ticket_row(env.db, late)["kite_gtt_id"] == 9002
    assert _findings(env, 9002) == []


def test_g07_two_late_results_racing_for_one_gtt_id_link_it_exactly_once(gtt_env):
    env = gtt_env
    first = _insert_ticket(env.db, kite_gtt_id=None, kite_order_id="O-A")
    second = _insert_ticket(env.db, kite_gtt_id=None, kite_order_id="O-B")
    barrier = threading.Barrier(2)
    errors = []

    def attach(ticket_id, order_id):
        try:
            barrier.wait(timeout=10)
            _late(env, ticket_id, order_id, 9003)
        except BaseException as e:  # noqa: BLE001 — reported below
            errors.append(e)

    threads = [threading.Thread(target=attach, args=a) for a in ((first, "O-A"), (second, "O-B"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    linked = [tid for tid in (first, second) if ticket_row(env.db, tid)["kite_gtt_id"] == 9003]
    assert len(linked) == 1                                               # exactly one ticket holds it
    loser = ({first, second} - set(linked)).pop()
    assert [f["ticket_id"] for f in _findings(env, 9003)] == [loser]
