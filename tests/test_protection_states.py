"""Honest protective-GTT lifecycle states (final order-path remediation, finding 2).

The frozen order path (backend/execution.py) places the protective sell GTT for the FULL ticket
quantity as soon as Kite ACCEPTS the entry. Acceptance is not a fill, and a GTT acts on the
account's holdings, not on the entry. So, everywhere the terminal reports protection:

- a position is PROTECTED only when shares are confirmed held and an active GTT covers exactly
  them; never an accepted entry that has filled nothing (PENDING_FILL), never a GTT
  that could sell more than is confirmed held (QUANTITY_EXCEEDS_POSITION);
- the GTT's actual status and quantity (broker-observed only; the app's record is shown as the
  record, never as protection) are shown next to the confirmed filled quantity;
- every mismatch or missing protection is alerted (reconciliation finding and/or broadcast);
- nothing ever creates a GTT except the frozen order path itself: no retry, no re-creation after a
  failure, never as a side effect of an exit or Emergency Exit;
- an ambiguous broker answer is never resubmitted.

FakeKite and scratch databases only.
"""

from kiteconnect.exceptions import NetworkException

from backend import gtt_observations
from conftest import approval_body, set_mode, ticket_row
from test_live_safety import _monitor
from test_partial_fills import _fill, _held_position, _legs, _place_entry, _reconcile


def _lc(env, ticket_id) -> dict:
    r = env.client.get(f"/api/tickets/{ticket_id}")
    assert r.status_code == 200, r.text
    return r.json()["data"]["lifecycle"]


def _findings(env, ticket_id) -> list[dict]:
    conn = env.db.get_connection()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT issue_code, severity, expected_quantity, broker_quantity FROM reconciliation_log "
            "WHERE ticket_id = ? AND resolved = 0 ORDER BY id", (ticket_id,)).fetchall()]
    finally:
        conn.close()


def _events(env, ticket_id) -> list[str]:
    conn = env.db.get_connection()
    try:
        return [r["event_type"] for r in conn.execute(
            "SELECT event_type FROM ticket_events WHERE ticket_id = ? ORDER BY id", (ticket_id,)).fetchall()]
    finally:
        conn.close()


def _entry_with_failed_gtt(env, quantity=100) -> tuple[int, str]:
    """Kite accepts the entry; the protective GTT placement then fails (protection_failed)."""
    from test_partial_fills import PRICE, STOP, TARGET
    from test_baseline_api import _manual_ticket
    set_mode(env.db, "live")
    env.kite.ltp_map["NSE:TCS"] = PRICE
    env.kite.auto_fill = False
    env.kite.raise_on_next("place_gtt", NetworkException("GTT service unavailable"))
    t = env.client.post("/api/tickets", json=_manual_ticket(quantity=quantity, entry_price=PRICE, stop_loss=STOP,
                                                            target_1=TARGET)).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"]))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "executed" and data["kite_gtt_id"] is None
    assert "UNPROTECTED POSITION" in data["execution_message"]
    return t["id"], data["kite_order_id"]


# ---------------------------------------------------------------- accepted, not filled

def test_01_an_accepted_unfilled_entry_is_never_protected(env, market_open):
    buy_id, _, gtt_id = _place_entry(env, 100)
    lc = _lc(env, buy_id)
    assert lc["lifecycle_state"] == "ENTRY_OPEN"
    # S2/S3: PENDING_FILL, never PROTECTED, and never reported as an unprotected open position.
    assert (lc["protection_status"], lc["protection_reason"]) == ("PENDING_FILL", "GTT_BEFORE_FILL")
    assert lc["protection_label"] == "PENDING FILL"
    assert (lc["kite_gtt_id"], lc["gtt_recorded_quantity"], lc["confirmed_filled_quantity"], lc["open_quantity"]) == (
        gtt_id, 100, 0, 0)
    # The app's record is never presented as a broker observation.
    assert (lc["gtt_source"], lc["gtt_quantity"]) == (None, None) and lc["protection_alert"] is True
    assert "PROTECTION_PENDING_FILL" in lc["attention_reasons"]


def test_02_the_broker_observed_gtt_is_shown_once_a_loop_has_read_the_book(env, market_open):
    buy_id, _, gtt_id = _place_entry(env, 100)
    _reconcile()
    lc = _lc(env, buy_id)
    assert (lc["gtt_source"], lc["gtt_status"], lc["gtt_quantity"]) == ("broker", "active", 100)
    assert lc["gtt_observed_at"] is not None
    assert lc["protection_status"] == "PENDING_FILL"


def test_03_reconciliation_flags_a_gtt_live_before_the_fill_as_a_warning_not_critical(env, market_open):
    buy_id, _, gtt_id = _place_entry(env, 100)
    _reconcile()
    staged = [f for f in _findings(env, buy_id) if f["issue_code"] == "GTT_STAGED_BEFORE_FILL"]
    assert staged == [{"issue_code": "GTT_STAGED_BEFORE_FILL", "severity": "warning", "expected_quantity": 0,
                       "broker_quantity": 100}]
    # Detection only: it never blocks an exit, and it is not one of the critical codes.
    from backend import lifecycle
    assert "GTT_STAGED_BEFORE_FILL" not in lifecycle.CRITICAL_ISSUES


# ---------------------------------------------------------------- partial and full fills

def test_04_a_partial_fill_resizes_the_gtt_and_only_then_is_it_protected(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env, 100)
    _fill(env, order_id, 40)                      # still OPEN, 40 filled
    _monitor(env)
    assert _legs(env, gtt_id) == {40}
    lc = _lc(env, buy_id)
    assert (lc["lifecycle_state"], lc["confirmed_filled_quantity"], lc["open_quantity"]) == ("ENTRY_PARTIAL", 40, 40)
    assert (lc["gtt_quantity"], lc["protection_status"]) == (40, "PROTECTED")


def test_05_a_partial_fill_whose_resize_failed_is_never_protected(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env, 100)
    _reconcile()                                  # the broker GTT (100) observed
    env.kite.raise_on_next("modify_gtt", NetworkException("timeout"))
    _fill(env, order_id, 40)
    _monitor(env)
    assert _legs(env, gtt_id) == {100}
    lc = _lc(env, buy_id)
    assert (lc["protection_status"], lc["protection_reason"]) == ("NOT_PROTECTED", "QUANTITY_EXCEEDS_POSITION")
    assert lc["severity"] == "critical"
    assert (lc["gtt_quantity"], lc["confirmed_filled_quantity"]) == (100, 40)
    assert "PROTECTION_QUANTITY_MISMATCH" in {f["issue_code"] for f in _findings(env, buy_id)}


def test_05b_without_a_broker_observation_the_failed_resize_is_still_a_mismatch(env, market_open):
    buy_id, order_id, _ = _place_entry(env, 100)
    env.kite.raise_on_next("modify_gtt", NetworkException("timeout"))
    _fill(env, order_id, 40)
    _monitor(env)
    # The resize's own read of the book is an observation: 100 covered, 40 held.
    lc = _lc(env, buy_id)
    assert (lc["protection_status"], lc["protection_reason"]) == ("NOT_PROTECTED", "QUANTITY_EXCEEDS_POSITION")


def test_06_a_full_fill_is_protected_and_verified_at_the_broker(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env, 100)
    reads = len(env.kite.calls_to("get_gtts"))
    _fill(env, order_id, 100, status="COMPLETE")
    _monitor(env)
    assert len(env.kite.calls_to("get_gtts")) == reads + 1   # verified, not assumed
    lc = _lc(env, buy_id)
    assert (lc["lifecycle_state"], lc["protection_status"], lc["gtt_quantity"]) == ("OPEN", "PROTECTED", 100)
    assert len(env.kite.calls_to("place_gtt")) == 1


def test_07_a_triggered_gtt_seen_at_the_broker_is_not_protection(env, market_open):
    buy_id, _ = _held_position(env, 40)
    gtt_id = ticket_row(env.db, buy_id)["kite_gtt_id"]
    env.kite.set_gtt_status(gtt_id, "triggered")
    gtt_observations.record_book(env.kite.get_gtts())
    lc = _lc(env, buy_id)
    assert (lc["protection_status"], lc["protection_reason"]) == ("NOT_PROTECTED", "GTT_NOT_ACTIVE")


# ---------------------------------------------------------------- GTT creation failed after acceptance

def test_08_failed_gtt_on_an_unfilled_entry_is_unprotected_and_flagged(env, market_open):
    buy_id, _ = _entry_with_failed_gtt(env)
    assert "protection_failed" in _events(env, buy_id)
    # S2/S3: nothing has filled, so there is no open position to call unprotected: PENDING_FILL,
    # with the missing GTT stated as the reason (and its finding alerted below).
    lc = _lc(env, buy_id)
    assert (lc["protection_status"], lc["protection_reason"]) == ("PENDING_FILL", "NO_GTT")
    _reconcile()
    [missing] = [f for f in _findings(env, buy_id) if f["issue_code"] == "PROTECTION_MISSING"]
    assert missing["severity"] == "urgent"
    assert len(env.kite.calls_to("place_gtt")) == 1         # the one failed attempt; never retried


def test_09_failed_gtt_then_a_fill_straight_to_complete_raises_an_urgent_alert(env, market_open):
    """Previously silent: an entry whose GTT failed and that then filled straight to COMPLETE got a
    'success' monitor result. Now its protection is verified at the broker on every fill."""
    buy_id, order_id = _entry_with_failed_gtt(env)
    _fill(env, order_id, 100, status="COMPLETE")
    _monitor(env)
    alerts = [b for b in env.broadcasts if "UNPROTECTED POSITION" in (b.get("message") or "")]
    assert alerts and alerts[-1].get("severity") == "urgent", env.broadcasts
    assert _events(env, buy_id).count("protection_failed") >= 2
    assert any(f["issue_code"] == "PROTECTION_MISSING" and f["severity"] == "urgent" for f in _findings(env, buy_id))
    lc = _lc(env, buy_id)
    assert (lc["protection_status"], lc["protection_reason"]) == ("NOT_PROTECTED", "NO_GTT")
    assert lc["open_quantity"] == 100
    assert len(env.kite.calls_to("place_gtt")) == 1         # nothing re-created it


def test_10_a_cancelled_or_rejected_entry_removes_its_gtt_and_claims_nothing(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env, 100)
    _fill(env, order_id, 0, status="REJECTED")
    _monitor(env)
    row = ticket_row(env.db, buy_id)
    assert (row["status"], row["entry_fill_status"], row["kite_gtt_id"]) == ("cancelled", "rejected", None)
    assert env.kite.gtt_book[gtt_id]["status"] == "deleted"
    assert _lc(env, buy_id)["protection_status"] == "NOT_APPLICABLE"


def test_11_cancelled_after_a_partial_fill_protects_exactly_what_was_bought(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env, 100)
    _fill(env, order_id, 30, status="CANCELLED")
    _monitor(env)
    assert _legs(env, gtt_id) == {30}
    lc = _lc(env, buy_id)
    assert (lc["open_quantity"], lc["gtt_quantity"], lc["protection_status"]) == (30, 30, "PROTECTED")


# ---------------------------------------------------------------- ambiguity and duplicates

def test_12_an_ambiguous_entry_is_never_resubmitted_and_never_gets_a_gtt_by_itself(env, market_open):
    from test_partial_fills import PRICE, STOP, TARGET
    from test_baseline_api import _manual_ticket
    set_mode(env.db, "live")
    env.kite.ltp_map["NSE:TCS"] = PRICE
    env.kite.auto_fill = False
    env.kite.accept_then_raise("place_order", NetworkException("Gateway timeout"))
    t = env.client.post("/api/tickets", json=_manual_ticket(entry_price=PRICE, stop_loss=STOP,
                                                            target_1=TARGET)).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"]))
    assert r.status_code == 200 and "DO NOT RESUBMIT" in r.json()["data"]["execution_message"]
    again = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"]))
    assert again.status_code == 409
    _monitor(env)                                 # adopts the tagged order from the book
    row = ticket_row(env.db, t["id"])
    assert row["submission_state"] == "adopted" and row["kite_gtt_id"] is None
    assert len(env.kite.calls_to("place_order")) == 1 and env.kite.calls_to("place_gtt") == []
    order_id = row["kite_order_id"]
    _fill(env, order_id, 5, status="COMPLETE")
    _monitor(env)
    assert any(f["issue_code"] == "PROTECTION_MISSING" for f in _findings(env, t["id"]))
    assert (_lc(env, t["id"])["protection_status"], _lc(env, t["id"])["protection_reason"]) == (
        "NOT_PROTECTED", "NO_GTT")
    assert env.kite.calls_to("place_gtt") == []


def test_13_summary_counts_every_protection_alert(env, market_open):
    _place_entry(env, 100)                        # PENDING_FILL
    r = env.client.get("/api/app-positions?view=active")
    assert r.status_code == 200, r.text
    summary = r.json()["summary"]
    assert summary["protection_attention"] == 1 and summary["unprotected"] == 0
