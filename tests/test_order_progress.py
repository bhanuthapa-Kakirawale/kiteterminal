"""SEC-1 P1-E: broker acceptance versus actual fill.

A ticket's stored status 'executed' means the broker ACCEPTED the order; it
never meant the order filled. The API states the two separately
(lifecycle.order_progress: `order_progress` on every ticket, `entry_progress`
on every app position), derived only from what is persisted — the submission
bookkeeping, the broker order id, and the fill the order monitor recorded
from the broker's own order history. Everything below runs on FakeKite and
the real order monitor; the order count is asserted so nothing is ever
resubmitted.
"""

import itertools

from kiteconnect.exceptions import NetworkException

from conftest import approval_body, reauthorize, set_mode, ticket_row
from test_baseline_api import _manual_ticket
from test_live_safety import _confirm_unknown_entry, _monitor, market_open  # noqa: F401
from test_partial_fills import _fill, _place_entry

ACCEPTANCE = {"NOT_SUBMITTED", "SUBMITTING", "UNKNOWN", "NEEDS_REVIEW", "ACCEPTED", "REJECTED", "NOT_FOUND",
              "NOT_RECORDED"}
FILL = {"NONE", "UNKNOWN", "AWAITING_FILL", "PARTIAL", "FILLED", "CANCELLED", "REJECTED", "UNCONFIRMED",
        "NOT_RECORDED"}


def _progress(env, tid) -> dict:
    return env.client.get(f"/api/tickets/{tid}").json()["data"]["order_progress"]


def _orders(env) -> int:
    return len(env.kite.calls_to("place_order"))


def _summary(p) -> tuple:
    return p["acceptance"], p["fill"], p["filled_quantity"], p["working"]


# ============================================================ the order life cycle (live, FakeKite)

def test_an_acknowledged_order_with_no_fill_is_accepted_and_awaiting(env, market_open):
    tid, order_id, _ = _place_entry(env, quantity=100)
    assert ticket_row(env.db, tid)["status"] == "executed"                    # the stored, historical meaning
    p = _progress(env, tid)
    assert _summary(p) == ("ACCEPTED", "AWAITING_FILL", 0, True) and p["fill_evidence"] == "BROKER"
    assert p["requested_quantity"] == 100 and p["simulated"] is False


def test_a_partial_fill_is_partial_and_still_working(env, market_open):
    tid, order_id, _ = _place_entry(env, quantity=100)
    _fill(env, order_id, 40, "OPEN")
    _monitor(env)
    assert _summary(_progress(env, tid)) == ("ACCEPTED", "PARTIAL", 40, True)


def test_a_full_fill_is_filled(env, market_open):
    tid, order_id, _ = _place_entry(env, quantity=100)
    _fill(env, order_id, 100, "COMPLETE")
    _monitor(env)
    p = _progress(env, tid)
    assert _summary(p) == ("ACCEPTED", "FILLED", 100, False) and p["fill_evidence"] == "BROKER"


def test_a_rejection_after_acceptance_is_accepted_then_rejected(env, market_open):
    tid, order_id, _ = _place_entry(env, quantity=100)
    _fill(env, order_id, 0, "REJECTED")
    _monitor(env)
    assert _summary(_progress(env, tid)) == ("ACCEPTED", "REJECTED", 0, False)


def test_a_cancellation_with_nothing_filled_is_cancelled(env, market_open):
    tid, order_id, _ = _place_entry(env, quantity=100)
    _fill(env, order_id, 0, "CANCELLED")
    _monitor(env)
    assert _summary(_progress(env, tid)) == ("ACCEPTED", "CANCELLED", 0, False)


def test_a_cancellation_after_a_partial_fill_keeps_the_filled_shares(env, market_open):
    tid, order_id, _ = _place_entry(env, quantity=100)
    _fill(env, order_id, 30, "CANCELLED")
    _monitor(env)
    assert _summary(_progress(env, tid)) == ("ACCEPTED", "PARTIAL", 30, False)


def test_a_broker_rejection_at_submission_is_rejected_with_nothing_accepted(env, market_open):
    from kiteconnect.exceptions import InputException
    set_mode(env.db, "live")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    env.kite.raise_on_next("place_order", InputException("Insufficient funds"))
    env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"]))
    assert _summary(_progress(env, t["id"])) == ("REJECTED", "NONE", 0, False)


def test_an_order_awaiting_approval_is_not_submitted(env):
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    assert _summary(_progress(env, t["id"])) == ("NOT_SUBMITTED", "NONE", 0, False)


# ============================================================ unknown submissions

def test_an_unknown_submission_has_an_unknown_fill_and_is_never_resubmitted(env, market_open):
    tid, r = _confirm_unknown_entry(env, env.client, "accept_then_raise", NetworkException("response lost"))
    assert r.status_code == 200
    p = _progress(env, tid)
    assert (p["acceptance"], p["fill"], p["filled_quantity"]) == ("UNKNOWN", "UNKNOWN", None)
    orders = _orders(env)
    _monitor(env)                                       # finds the order by its tag: adopted
    p = _progress(env, tid)
    assert p["acceptance"] == "ACCEPTED" and p["fill"] in ("AWAITING_FILL", "FILLED")
    assert _orders(env) == orders                       # adopted, never placed again


def test_an_unknown_submission_that_never_reached_the_broker_becomes_not_found(env, market_open, monkeypatch):
    import requests
    from test_live_safety import _pin_clock, _set_submitted_at
    tid, _ = _confirm_unknown_entry(env, env.client, "raise_on_next", requests.ReadTimeout("timed out"))
    _set_submitted_at(env, tid)
    _pin_clock(monkeypatch, "2026-01-05 04:10:00")     # inside the search window: still unknown
    _monitor(env)
    p = _progress(env, tid)
    assert (p["acceptance"], p["fill"], p["filled_quantity"]) == ("UNKNOWN", "UNKNOWN", None)
    _pin_clock(monkeypatch, "2026-01-05 04:15:00")     # window over, no order with its tag: not found
    _monitor(env)
    assert _summary(_progress(env, tid)) == ("NOT_FOUND", "NONE", 0, False)
    assert _orders(env) == 1                           # the one attempt that never reached the broker


# ============================================================ delayed / repeated / out-of-order broker data

def test_delayed_broker_history_keeps_the_order_awaiting_its_fill(env, market_open):
    tid, order_id, _ = _place_entry(env, quantity=100)
    for _ in range(5):                                 # the broker keeps saying OPEN, nothing filled
        _monitor(env)
        assert _summary(_progress(env, tid)) == ("ACCEPTED", "AWAITING_FILL", 0, True)
    assert _orders(env) == 1


def test_an_unreachable_broker_history_never_turns_into_a_fill(env, market_open):
    tid, order_id, _ = _place_entry(env, quantity=100)
    env.kite.raise_on_next("orders", NetworkException("timeout"))
    env.kite.raise_on_next("order_history", NetworkException("timeout"))
    _monitor(env)
    assert _summary(_progress(env, tid)) == ("ACCEPTED", "AWAITING_FILL", 0, True)


def test_repeated_polling_after_the_fill_changes_nothing(env, market_open):
    tid, order_id, _ = _place_entry(env, quantity=100)
    _fill(env, order_id, 100, "COMPLETE")
    _monitor(env)
    before, events = _progress(env, tid), env.client.get(f"/api/tickets/{tid}").json()["data"]["events"]
    for _ in range(4):
        _monitor(env)
    assert _progress(env, tid) == before
    assert env.client.get(f"/api/tickets/{tid}").json()["data"]["events"] == events
    assert _orders(env) == 1


def test_a_duplicate_approval_neither_resubmits_nor_changes_the_progress(env, market_open):
    tid, order_id, _ = _place_entry(env, quantity=100)
    before = _progress(env, tid)
    r = env.client.post(f"/api/tickets/{tid}/confirm", json=approval_body(env.db, tid))
    assert r.status_code == 409
    assert _progress(env, tid) == before and _orders(env) == 1


def test_an_out_of_order_broker_status_never_regresses_a_recorded_fill(env, market_open):
    tid, order_id, _ = _place_entry(env, quantity=100)
    _fill(env, order_id, 100, "COMPLETE")
    _monitor(env)
    _fill(env, order_id, 20, "OPEN")                   # a stale/out-of-order report
    _monitor(env)
    assert _summary(_progress(env, tid)) == ("ACCEPTED", "FILLED", 100, False)


def test_a_restarted_process_resumes_from_the_persisted_state(env, market_open):
    from backend import reconciliation
    tid, order_id, _ = _place_entry(env, quantity=100)
    _fill(env, order_id, 60, "OPEN")
    _monitor(env)
    reconciliation.reset_cycle_memory()               # what a new process starts with
    reauthorize(env.client)                           # SEC-1 P1-F: the restarted terminal needs a login
    assert _summary(_progress(env, tid)) == ("ACCEPTED", "PARTIAL", 60, True)
    _fill(env, order_id, 100, "COMPLETE")
    _monitor(env)
    assert _summary(_progress(env, tid)) == ("ACCEPTED", "FILLED", 100, False)
    assert _orders(env) == 1


# ============================================================ exits, dry run, positions payload

def test_an_exit_order_reports_its_own_acceptance_and_fill(env, market_open):
    from test_partial_fills import _held_position, _place_exit
    buy_id, _ = _held_position(env, quantity=40)
    sell_id, order_id = _place_exit(env, buy_id, 40)
    assert _summary(_progress(env, sell_id)) == ("ACCEPTED", "AWAITING_FILL", 0, True)
    _fill(env, order_id, 40, "COMPLETE")
    _monitor(env)
    assert _summary(_progress(env, sell_id)) == ("ACCEPTED", "FILLED", 40, False)


def test_a_dry_run_order_is_marked_simulated(env, market_open):
    set_mode(env.db, "dry_run")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"]))
    p = _progress(env, t["id"])
    assert p["simulated"] is True and p["acceptance"] == "ACCEPTED"


def test_app_positions_carry_the_entry_progress(env, market_open):
    tid, order_id, _ = _place_entry(env, quantity=100)
    _fill(env, order_id, 40, "OPEN")
    _monitor(env)
    positions = env.client.get("/api/app-positions").json()["data"]
    entry = next(p for p in positions if p["position_id"] == tid)["entry_progress"]
    assert _summary(entry) == ("ACCEPTED", "PARTIAL", 40, True)


# ============================================================ the mapping itself

def test_a_legacy_row_is_never_reported_as_broker_verified():
    from backend import lifecycle
    base = dict(status="executed", submission_state=None, kite_order_id="250000000001", quantity=10, mode="live",
                submitted_at=None)
    backfilled = lifecycle.order_progress({**base, "entry_fill_status": None, "filled_quantity": 10})
    assert (backfilled["fill"], backfilled["fill_evidence"]) == ("FILLED", "LEGACY_RECORD")
    unrecorded = lifecycle.order_progress({**base, "entry_fill_status": None, "filled_quantity": None})
    assert (unrecorded["fill"], unrecorded["filled_quantity"]) == ("NOT_RECORDED", None)
    legacy_failure = lifecycle.order_progress({**base, "status": "failed", "kite_order_id": None,
                                               "entry_fill_status": None, "filled_quantity": None})
    assert legacy_failure["acceptance"] == "NOT_RECORDED"


def test_the_mapping_is_total_and_never_infers_a_fill_from_an_acknowledgement():
    from backend import lifecycle
    statuses = ("draft", "pending_confirmation", "confirmed", "executed", "rejected", "cancelled", "failed",
                "risk_rejected")
    subs = (None, "submitting", "acknowledged", "unknown", "adopted", "needs_review", "rejected", "not_found")
    fills = (None, "pending", "partial", "filled", "unconfirmed", "cancelled", "rejected")
    for status, sub, efs, order_id, filled in itertools.product(statuses, subs, fills, (None, "O-1"), (None, 0, 4)):
        p = lifecycle.order_progress({"status": status, "submission_state": sub, "entry_fill_status": efs,
                                      "kite_order_id": order_id, "filled_quantity": filled, "quantity": 10,
                                      "mode": "live", "submitted_at": None})
        assert p["acceptance"] in ACCEPTANCE and p["fill"] in FILL, (status, sub, efs, order_id, filled, p)
        if p["fill"] == "FILLED":
            assert efs == "filled" or (efs is None and p["fill_evidence"] == "LEGACY_RECORD")
        if p["acceptance"] != "ACCEPTED":
            assert p["fill"] in ("NONE", "UNKNOWN", "NOT_RECORDED") and not p["working"]


# ============================================================ the UI words what the server decided

def test_every_acceptance_and_fill_value_has_a_ui_label():
    import json
    import re
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    app_js = (Path(__file__).resolve().parents[1] / "frontend" / "app.js").read_text(encoding="utf-8")
    parts = [re.search(rf"^const {name} = .*?;$", app_js, re.M | re.S).group(0)
             for name in ("ACCEPTANCE_LABELS", "FILL_LABELS", "_ESC")]
    parts += [re.search(rf"^function {name}\(.*?^}}$", app_js, re.M | re.S).group(0)
              for name in ("esc", "fmt", "orderProgressRowHtml")]
    cases = [{"acceptance": a, "fill": f, "filled_quantity": 3, "requested_quantity": 10, "working": False,
              "simulated": False, "fill_evidence": "BROKER"}
             for a in sorted(ACCEPTANCE - {"NOT_SUBMITTED"}) for f in sorted(FILL)]
    script = "\n".join(parts) + f"""
        const cases = {json.dumps(cases)};
        const out = cases.map((c) => orderProgressRowHtml(c));
        out.push(orderProgressRowHtml({{acceptance: "NOT_SUBMITTED", fill: "NONE"}}));
        process.stdout.write(JSON.stringify(out));"""
    r = subprocess.run([node, "-"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    rendered = json.loads(r.stdout)
    assert rendered[-1] == ""                                     # nothing before the order is sent
    for case, html in zip(cases, rendered):
        shown = html.split("<strong>")[1]
        assert "3/10" in html and "undefined" not in html, html
        # every server value is worded, never echoed raw (UNKNOWN/REJECTED words contain their own names)
        assert case["fill"] not in shown or case["fill"] in ("UNKNOWN", "REJECTED"), (case, html)
    assert any("accepted · awaiting fill · 3/10" in html for html in rendered)
