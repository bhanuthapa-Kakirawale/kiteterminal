"""C4: a GTT cancellation is reported only as the broker confirms it.

backend/gtt_cancellation.cancel() sends one delete, then always reads the GTT book back:

  CANCEL_CONFIRMED         the read-back lists the GTT deleted / cancelled
  CANCEL_FAILED            it is still ACTIVE, or the delete was definitively refused and the
                           book could not be read back
  CANCEL_OUTCOME_UNKNOWN   no read-back after an acknowledged / ambiguous delete, the GTT is
                           absent, or it is in another state (e.g. triggered)

Anything but CONFIRMED keeps the GTT id on the ticket, writes a persistent reconciliation
finding (GTT_CANCEL_FAILED / GTT_CANCEL_OUTCOME_UNKNOWN, both critical) and an urgent alert, at
all three places the app cancels a GTT. Nothing is retried. FakeKite and scratch databases only.
"""

import pytest
from kiteconnect.exceptions import InputException, NetworkException, TokenException

from backend import gtt_cancellation, gtt_observations, lifecycle, position_quantity
from conftest import ticket_row
from test_live_safety import _monitor
from test_partial_fills import _fill, _held_position, _place_entry


def _gtt(env, quantity=5):
    legs = [{"transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT", "product": "CNC",
             "price": 2885.5}]
    return env.kite.place_gtt("single", "TCS", "NSE", [2900.0], 3000.0, legs)["trigger_id"]


def _fail_readback(env):
    """The next get_gtts — the read-back right after the delete — raises."""
    env.kite.raise_on_next("get_gtts", NetworkException("read-back timeout"))


def _findings(env, ticket_id=None) -> list[dict]:
    conn = env.db.get_connection()
    try:
        sql = "SELECT issue_code, severity, actual, detail, suggested_action FROM reconciliation_log WHERE resolved = 0"
        args = ()
        if ticket_id is not None:
            sql += " AND ticket_id = ?"
            args = (ticket_id,)
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", args)]
    finally:
        conn.close()


# ---------------------------------------------------------------- the outcome table

def test_01_acknowledged_and_listed_deleted_is_confirmed(env):
    gtt_id = _gtt(env)
    out = gtt_cancellation.cancel(env.kite, gtt_id)
    assert (out.state, out.broker_status, out.confirmed) == ("CANCEL_CONFIRMED", "deleted", True)
    assert gtt_observations.get(gtt_id)["status"] == "deleted"          # the read-back is an observation


def test_02_response_lost_but_listed_deleted_is_confirmed(env):
    gtt_id = _gtt(env)
    env.kite.accept_then_raise("delete_gtt", NetworkException("Gateway timeout"))
    out = gtt_cancellation.cancel(env.kite, gtt_id)
    assert out.state == "CANCEL_CONFIRMED" and isinstance(out.error, NetworkException)


def test_03_refused_and_still_active_is_failed(env):
    gtt_id = _gtt(env)
    env.kite.raise_on_next("delete_gtt", InputException("GTT cannot be deleted"))
    out = gtt_cancellation.cancel(env.kite, gtt_id)
    assert (out.state, out.broker_status) == ("CANCEL_FAILED", "active")
    assert "still lists it ACTIVE" in out.detail


def test_04_ambiguous_error_but_still_active_is_failed(env):
    gtt_id = _gtt(env)
    env.kite.raise_on_next("delete_gtt", NetworkException("timeout before reaching Kite"))
    assert gtt_cancellation.cancel(env.kite, gtt_id).state == "CANCEL_FAILED"


def test_05_a_definite_refusal_without_a_read_back_is_failed(env):
    gtt_id = _gtt(env)
    env.kite.raise_on_next("delete_gtt", InputException("refused"))
    _fail_readback(env)
    out = gtt_cancellation.cancel(env.kite, gtt_id)
    assert (out.state, out.broker_status) == ("CANCEL_FAILED", None)


def test_06_acknowledged_without_a_read_back_is_unknown_never_cancelled(env):
    gtt_id = _gtt(env)
    _fail_readback(env)
    out = gtt_cancellation.cancel(env.kite, gtt_id)
    assert (out.state, out.confirmed) == ("CANCEL_OUTCOME_UNKNOWN", False)
    assert "could not be read back" in out.detail


def test_07_ambiguous_error_without_a_read_back_is_unknown(env):
    gtt_id = _gtt(env)
    env.kite.accept_then_raise("delete_gtt", NetworkException("Gateway timeout"))
    _fail_readback(env)
    assert gtt_cancellation.cancel(env.kite, gtt_id).state == "CANCEL_OUTCOME_UNKNOWN"


def test_08_absent_from_the_book_is_unknown(env):
    gtt_id = _gtt(env)
    real_delete = env.kite.delete_gtt

    def delete_and_vanish(trigger_id):
        real_delete(trigger_id)
        del env.kite.gtt_book[trigger_id]
    env.kite.delete_gtt = delete_and_vanish
    out = gtt_cancellation.cancel(env.kite, gtt_id)
    assert (out.state, out.broker_status) == ("CANCEL_OUTCOME_UNKNOWN", "missing")


def test_09_a_gtt_that_fired_meanwhile_is_not_cancelled(env):
    gtt_id = _gtt(env)
    env.kite.set_gtt_status(gtt_id, "triggered")
    env.kite.raise_on_next("delete_gtt", InputException("GTT is not active"))
    out = gtt_cancellation.cancel(env.kite, gtt_id)
    assert (out.state, out.broker_status) == ("CANCEL_OUTCOME_UNKNOWN", "triggered")


def test_10_a_token_error_is_a_definite_refusal(env):
    gtt_id = _gtt(env)
    env.kite.raise_on_next("delete_gtt", TokenException("session expired"))
    env.kite.raise_on_next("get_gtts", TokenException("session expired"))
    assert gtt_cancellation.cancel(env.kite, gtt_id).state == "CANCEL_FAILED"


def test_11_one_delete_never_a_retry(env):
    gtt_id = _gtt(env)
    env.kite.raise_on_next("delete_gtt", NetworkException("timeout"))
    gtt_cancellation.cancel(env.kite, gtt_id)
    assert len(env.kite.calls_to("delete_gtt")) == 1


def test_12_both_codes_are_critical():
    assert {"GTT_CANCEL_FAILED", "GTT_CANCEL_OUTCOME_UNKNOWN"} <= lifecycle.CRITICAL_ISSUES


# ---------------------------------------------------------------- site 1: a cancelled / rejected entry

@pytest.mark.parametrize("setup,code", [
    (lambda env: env.kite.raise_on_next("delete_gtt", InputException("refused")), "GTT_CANCEL_FAILED"),
    (lambda env: (env.kite.on_call("delete_gtt", lambda kw: env.kite.raise_on_next("get_gtts",
                                                                                   NetworkException("x")))),
     "GTT_CANCEL_OUTCOME_UNKNOWN"),
], ids=["failed", "unknown"])
def test_13_order_monitor_keeps_and_flags_an_unconfirmed_cancellation(env, market_open, setup, code):
    buy_id, order_id, gtt_id = _place_entry(env, 100)
    _fill(env, order_id, 0, status="REJECTED")
    setup(env)
    _monitor(env)
    env.kite.on_call("delete_gtt", None)
    row = ticket_row(env.db, buy_id)
    assert (row["status"], row["kite_gtt_id"]) == ("cancelled", gtt_id)       # the id is kept
    [finding] = [f for f in _findings(env, buy_id) if f["issue_code"] == code]
    assert finding["severity"] == "urgent" and "Nothing was retried" in finding["detail"]
    [alert] = [b for b in env.broadcasts if b.get("type") == "order_update" and b.get("ticket_id") == buy_id]
    assert alert["severity"] == "urgent" and "NOT confirmed cancelled" in alert["message"]
    lc = env.client.get(f"/api/tickets/{buy_id}").json()["data"]["lifecycle"]
    assert code in {i["code"] for i in lc["reconciliation_issues"]}


def test_14_order_monitor_clears_the_gtt_only_when_confirmed(env, market_open):
    buy_id, order_id, gtt_id = _place_entry(env, 100)
    _fill(env, order_id, 0, status="CANCELLED")
    _monitor(env)
    row = ticket_row(env.db, buy_id)
    assert row["kite_gtt_id"] is None and env.kite.gtt_book[gtt_id]["status"] == "deleted"
    assert not [f for f in _findings(env, buy_id) if (f["issue_code"] or "").startswith("GTT_CANCEL")]


# ---------------------------------------------------------------- site 2: after an exit sold everything

@pytest.mark.parametrize("setup,code,urgent_text", [
    (lambda env: env.kite.raise_on_next("delete_gtt", RuntimeError("Kite down")), "GTT_CANCEL_FAILED",
     "ORPHANED GTT"),
    (lambda env: env.kite.on_call("delete_gtt", lambda kw: env.kite.raise_on_next("get_gtts", NetworkException("x"))),
     "GTT_CANCEL_OUTCOME_UNKNOWN", "GTT CANCELLATION UNKNOWN"),
], ids=["failed", "unknown"])
def test_15_remove_protective_gtt_keeps_and_flags_an_unconfirmed_cancellation(env, setup, code, urgent_text):
    buy_id, gtt_id = _held_position(env, 40)
    setup(env)
    conn = env.db.get_connection()
    try:
        note, urgent = position_quantity.remove_protective_gtt(conn, env.kite, None, buy_id)
        conn.commit()
    finally:
        conn.close()
    env.kite.on_call("delete_gtt", None)
    assert urgent is True and urgent_text in note and "NOT confirmed cancelled" in note
    assert ticket_row(env.db, buy_id)["kite_gtt_id"] == gtt_id
    assert [f["issue_code"] for f in _findings(env, buy_id)] == [code]


def test_16_remove_protective_gtt_confirmed(env):
    buy_id, gtt_id = _held_position(env, 40)
    conn = env.db.get_connection()
    try:
        note, urgent = position_quantity.remove_protective_gtt(conn, env.kite, None, buy_id)
        conn.commit()
    finally:
        conn.close()
    assert urgent is False and "confirmed by the broker" in note
    assert ticket_row(env.db, buy_id)["kite_gtt_id"] is None and _findings(env, buy_id) == []


# ---------------------------------------------------------------- site 3: the manual cancel route

def test_17_manual_cancel_confirmed(env):
    buy_id, gtt_id = _held_position(env, 40)
    r = env.client.post(f"/api/gtts/{gtt_id}/cancel")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert (data["cancelled"], data["outcome"], data["broker_status"]) == (True, "CANCEL_CONFIRMED", "deleted")
    assert ticket_row(env.db, buy_id)["kite_gtt_id"] is None


def test_18_manual_cancel_failed_is_a_502_with_a_finding(env):
    buy_id, gtt_id = _held_position(env, 40)
    env.kite.raise_on_next("delete_gtt", InputException("GTT cannot be modified"))
    r = env.client.post(f"/api/gtts/{gtt_id}/cancel")
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert (detail["error_code"], detail["outcome"]) == ("GTT_CANCEL_FAILED", "CANCEL_FAILED")
    assert "Delete it in Kite now" in detail["message"]
    assert ticket_row(env.db, buy_id)["kite_gtt_id"] == gtt_id
    [finding] = _findings(env, buy_id)
    assert (finding["issue_code"], finding["suggested_action"]) == ("GTT_CANCEL_FAILED", "CANCEL IN KITE")


def test_19_manual_cancel_unknown_is_a_409_with_a_finding(env):
    buy_id, gtt_id = _held_position(env, 40)
    env.kite.on_call("delete_gtt", lambda kw: env.kite.raise_on_next("get_gtts", NetworkException("x")))
    r = env.client.post(f"/api/gtts/{gtt_id}/cancel")
    env.kite.on_call("delete_gtt", None)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error_code"] == "GTT_CANCEL_OUTCOME_UNKNOWN"
    assert ticket_row(env.db, buy_id)["kite_gtt_id"] == gtt_id
    assert [f["issue_code"] for f in _findings(env, buy_id)] == ["GTT_CANCEL_OUTCOME_UNKNOWN"]


def test_20_manual_cancel_with_an_expired_session_is_still_a_401(env):
    buy_id, gtt_id = _held_position(env, 40)
    env.kite.raise_on_next("delete_gtt", TokenException("session expired"))
    env.kite.raise_on_next("get_gtts", TokenException("session expired"))
    r = env.client.post(f"/api/gtts/{gtt_id}/cancel")
    assert r.status_code == 401 and r.json()["detail"]["error_code"] == "SESSION_EXPIRED"
    assert ticket_row(env.db, buy_id)["kite_gtt_id"] == gtt_id
    assert [f["issue_code"] for f in _findings(env, buy_id)] == ["GTT_CANCEL_FAILED"]


def test_21_an_unlinked_gtt_failure_is_still_recorded(env):
    gtt_id = _gtt(env)
    env.kite.raise_on_next("delete_gtt", InputException("refused"))
    r = env.client.post(f"/api/gtts/{gtt_id}/cancel")
    assert r.status_code == 502
    assert [f["issue_code"] for f in _findings(env)] == ["GTT_CANCEL_FAILED"]
