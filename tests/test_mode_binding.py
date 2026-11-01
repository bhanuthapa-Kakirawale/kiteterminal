"""PK-11 (P1-7): approval is bound to the execution mode the user saw.

A NEW entry's confirm carries expected_mode — the mode the approving view
displayed. The server compares it with the global mode BEFORE the claim; a
missing or different mode is refused (409) with nothing written and nothing
sent. The compared value is the one the claim stamps and execution runs on,
so a switch after the check cannot change what executes. Exits ignore
expected_mode and keep their position's own mode (PK-3). A mode switch is
broadcast as `mode_changed`, but the server check is the protection.
No sleeps; nothing reaches the network.
"""

import pytest

from conftest import set_mode, ticket_row
from test_baseline_api import _manual_ticket
from test_live_safety import (  # noqa: F401 — market_open is a fixture
    _broker_calls, _dry_run_position, _event_types, _live_position, _pending_exit, _rows, _sells, _unknown_ticket,
    market_open,
)


def _entry(env) -> int:
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    assert t["status"] == "pending_confirmation", t
    return t["id"]


def _confirm(client, ticket_id, expected_mode="__omit__"):
    if expected_mode == "__omit__":
        return client.post(f"/api/tickets/{ticket_id}/confirm")
    # P1-D: the view also echoes the review token it showed — read, like the
    # UI does, from the ticket payload. These tests are about the mode only.
    token = client.get(f"/api/tickets/{ticket_id}").json()["data"]["review"]["token"]
    return client.post(f"/api/tickets/{ticket_id}/confirm",
                       json={"expected_mode": expected_mode, "expected_review_token": token})


def _mode_broadcasts(env):
    return [b for b in env.broadcasts if b.get("type") == "mode_changed"]


def _assert_untouched(env, ticket_id, before: dict, events_before: list):
    assert ticket_row(env.db, ticket_id) == before
    assert _event_types(env, ticket_id) == events_before
    assert env.kite.calls_to("place_order") == [] and env.kite.calls_to("place_gtt") == []


# ---------------------------------------------------------------- 1 / 2. mismatch refused

@pytest.mark.parametrize("shown, server", [("dry_run", "live"), ("live", "dry_run")])
def test_mismatched_mode_is_refused_without_any_broker_call(env, market_open, shown, server):
    set_mode(env.db, server)
    tid = _entry(env)
    before, events = ticket_row(env.db, tid), _event_types(env, tid)

    r = _confirm(env.client, tid, shown)

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error_code"] == "EXECUTION_MODE_CHANGED"
    assert (detail["expected_mode"], detail["current_mode"]) == (shown, server)
    assert "Review the ticket again before approving" in detail["message"]
    _assert_untouched(env, tid, before, events)


@pytest.mark.parametrize("body", [None, {}, {"expected_mode": None}], ids=["no_body", "empty", "null"])
def test_missing_expected_mode_on_entry_is_refused(env, market_open, body):
    set_mode(env.db, "live")
    tid = _entry(env)
    before, events = ticket_row(env.db, tid), _event_types(env, tid)

    r = env.client.post(f"/api/tickets/{tid}/confirm", json=body) if body is not None else _confirm(env.client, tid)

    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "EXECUTION_MODE_REQUIRED"
    _assert_untouched(env, tid, before, events)


def test_invalid_expected_mode_is_rejected_before_anything(env, market_open):
    set_mode(env.db, "live")
    tid = _entry(env)
    before = ticket_row(env.db, tid)

    r = _confirm(env.client, tid, "LIVE")

    assert r.status_code == 422
    assert ticket_row(env.db, tid) == before and env.kite.calls_to("place_order") == []


# ---------------------------------------------------------------- 3 / 4. matching mode proceeds

def test_matching_live_follows_the_normal_live_path(env, market_open):
    set_mode(env.db, "live")
    tid = _entry(env)

    r = _confirm(env.client, tid, "live")

    assert r.status_code == 200, r.text
    row = ticket_row(env.db, tid)
    assert (row["status"], row["mode"], row["submission_state"]) == ("executed", "live", "acknowledged")
    assert row["order_tag"] and row["kite_order_id"] and row["kite_gtt_id"]
    assert len(env.kite.calls_to("place_order")) == 1 and len(env.kite.calls_to("place_gtt")) == 1


def test_matching_dry_run_is_simulated(env, market_open):
    set_mode(env.db, "dry_run")
    tid = _entry(env)

    r = _confirm(env.client, tid, "dry_run")

    assert r.status_code == 200
    assert (r.json()["data"]["status"], r.json()["data"]["mode"]) == ("executed", "dry_run")
    assert _broker_calls(env) == []


def test_stale_stamped_mode_on_a_reverted_entry_does_not_decide(env, market_open):
    # An earlier, refused claim stamped dry_run; the ticket was reverted to
    # approval. The server claims a new entry in the GLOBAL mode, so that is
    # the mode the user must have been shown.
    set_mode(env.db, "live")
    tid = _entry(env)
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE trade_tickets SET mode = 'dry_run' WHERE id = ?", (tid,))
        conn.commit()
    finally:
        conn.close()

    assert _confirm(env.client, tid, "dry_run").json()["detail"]["error_code"] == "EXECUTION_MODE_CHANGED"
    r = _confirm(env.client, tid, "live")
    assert r.status_code == 200 and ticket_row(env.db, tid)["mode"] == "live"
    assert len(env.kite.calls_to("place_order")) == 1


# ---------------------------------------------------------------- 5. exits follow the position

@pytest.mark.parametrize("sent", ["dry_run", "live", "__omit__"])
def test_live_position_exit_ignores_expected_mode_and_global_mode(env, market_open, sent):
    buy_id, _ = _live_position(env)
    set_mode(env.db, "dry_run")  # global says dry run; the position is live
    sell_id = _pending_exit(env, buy_id)

    r = _confirm(env.client, sell_id, sent)

    assert r.status_code == 200, r.text
    assert (r.json()["data"]["mode"], r.json()["data"]["status"]) == ("live", "executed")
    assert [s["tradingsymbol"] for s in _sells(env)] == ["TCS"]


@pytest.mark.parametrize("sent", ["live", "dry_run", "__omit__"])
def test_dry_run_position_exit_ignores_expected_mode_and_global_mode(env, market_open, sent):
    buy_id = _dry_run_position(env)
    set_mode(env.db, "live")  # global says live; the position is a simulation
    sell_id = _pending_exit(env, buy_id)

    r = _confirm(env.client, sell_id, sent)

    assert r.status_code == 200, r.text
    assert (r.json()["data"]["mode"], r.json()["data"]["status"]) == ("dry_run", "executed")
    assert _broker_calls(env) == []


# ---------------------------------------------------------------- 6. broadcast

def test_mode_switch_broadcasts_mode_changed(env):
    from conftest import make_live_ready
    make_live_ready(env)   # SEC-1: arming LIVE needs a passing, broker-checked Live Readiness
    set_mode(env.db, "dry_run")

    assert env.client.post("/api/execution/mode", json={"mode": "live", "confirm_real_money": True}).status_code == 200
    assert _mode_broadcasts(env) == [{"type": "mode_changed", "mode": "live", "previous_mode": "dry_run"}]

    env.client.post("/api/execution/mode", json={"mode": "live", "confirm_real_money": True})  # no change
    assert len(_mode_broadcasts(env)) == 1

    env.client.post("/api/execution/mode", json={"mode": "dry_run"})
    assert _mode_broadcasts(env)[-1] == {"type": "mode_changed", "mode": "dry_run", "previous_mode": "live"}


def test_refused_live_switch_broadcasts_nothing(env):
    set_mode(env.db, "dry_run")
    r = env.client.post("/api/execution/mode", json={"mode": "live", "confirm_real_money": False})
    assert r.status_code == 400 and _mode_broadcasts(env) == []


# ---------------------------------------------------------------- 7. stale tab

def test_stale_tab_cannot_approve_live_while_showing_dry_run(env, market_open):
    from conftest import make_live_ready
    make_live_ready(env)   # SEC-1: arming LIVE needs a passing, broker-checked Live Readiness
    set_mode(env.db, "dry_run")
    tid = _entry(env)
    tab_a_shows = env.client.get("/api/execution/mode").json()["data"]["mode"]  # tab A loads: DRY RUN
    assert tab_a_shows == "dry_run"
    # Tab B switches to LIVE. Tab A misses the broadcast and still shows DRY RUN.
    env.client.post("/api/execution/mode", json={"mode": "live", "confirm_real_money": True})

    r = _confirm(env.client, tid, tab_a_shows)

    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "EXECUTION_MODE_CHANGED"
    assert env.kite.calls_to("place_order") == []
    assert ticket_row(env.db, tid)["status"] == "pending_confirmation"
    # Tab A refreshes, now shows LIVE, and the user approves knowingly.
    tab_a_shows = env.client.get("/api/execution/mode").json()["data"]["mode"]
    r = _confirm(env.client, tid, tab_a_shows)
    assert r.status_code == 200 and r.json()["data"]["mode"] == "live"
    assert len(env.kite.calls_to("place_order")) == 1


# ---------------------------------------------------------------- 8 / 9. before the claim

def test_mismatch_is_refused_before_the_atomic_claim(env, market_open, monkeypatch):
    from backend.routes import tickets
    set_mode(env.db, "live")
    tid = _entry(env)
    before, events = ticket_row(env.db, tid), _event_types(env, tid)
    transitions = []
    real = tickets._transition

    def spy(conn, ticket_id, account_id, from_statuses, to_status, **columns):
        transitions.append((ticket_id, to_status))
        return real(conn, ticket_id, account_id, from_statuses, to_status, **columns)
    monkeypatch.setattr(tickets, "_transition", spy)

    r = _confirm(env.client, tid, "dry_run")

    assert r.status_code == 409 and transitions == []
    _assert_untouched(env, tid, before, events)
    after = ticket_row(env.db, tid)
    assert (after["status"], after["mode"], after["order_tag"], after["submission_state"], after["submitted_at"]) == (
        "pending_confirmation", None, None, None, None)
    assert "confirmed" not in _event_types(env, tid)


def test_ticket_stays_approvable_after_a_mismatch(env, market_open):
    set_mode(env.db, "live")
    tid = _entry(env)
    for _ in range(3):
        assert _confirm(env.client, tid, "dry_run").status_code == 409
    assert ticket_row(env.db, tid)["status"] == "pending_confirmation"

    r = _confirm(env.client, tid, "live")

    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"
    assert _event_types(env, tid).count("confirmed") == 1 and len(env.kite.calls_to("place_order")) == 1


def test_mode_switch_after_the_check_cannot_change_what_executes(env, market_open, monkeypatch):
    # The value checked is the value the claim stamps and execution runs on.
    from backend.routes import tickets
    set_mode(env.db, "dry_run")
    tid = _entry(env)
    real = tickets._require_displayed_mode

    def check_then_switch(ticket, mode, expected_mode):
        real(ticket, mode, expected_mode)
        set_mode(env.db, "live")  # another session flips the switch right after the check
    monkeypatch.setattr(tickets, "_require_displayed_mode", check_then_switch)

    r = _confirm(env.client, tid, "dry_run")

    assert r.status_code == 200 and r.json()["data"]["mode"] == "dry_run"
    assert _broker_calls(env) == []


# ---------------------------------------------------------------- 10. UNKNOWN / already-confirmed tickets

@pytest.mark.parametrize("sent", ["live", "dry_run", "__omit__"])
def test_unknown_and_executed_tickets_are_unaffected(env, market_open, sent):
    set_mode(env.db, "live")
    unknown = _unknown_ticket(env)
    executed = _live_position(env)[0]
    rows_before = {t: ticket_row(env.db, t) for t in (unknown, executed)}
    calls_before = len(env.kite.calls)

    for ticket_id in (unknown, executed):
        r = _confirm(env.client, ticket_id, sent)
        assert r.status_code == 409 and r.json()["detail"]["error_code"] == "CONFLICT_INVALID_STATE"
        assert ticket_row(env.db, ticket_id) == rows_before[ticket_id]
    assert _broker_calls(env, calls_before) == []
