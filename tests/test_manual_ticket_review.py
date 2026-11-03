"""SEC-1 P1-D: manual / Quick-Action BUY ticket review, and the strategy
stale-signal hardening.

A risk-adding manual or Quick-Action BUY entry may be sent only while the
human review it rests on is current — same NSE session window, same Kite
login, exactly the content stored — and only by a view that showed that
review. Every refusal is asserted to have made no broker call and written
nothing to the ticket; exits are asserted never to be held back.

The gate's clock (account_safety.now_ist) is pinned by the `clock` fixture;
the review window is read from the trading calendar, never from
market.is_market_open (which the market_open fixture pins).
"""

import json
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from kiteconnect.exceptions import NetworkException

from conftest import IST, account_id, approval_body, displayed_mode, reauthorize, set_mode, ticket_row
from test_account_safety import (  # noqa: F401 — live is a fixture
    _assert_untouched_pending, _calls_since, _code, _codes, _confirm, _lock, _re_review, _risk, _sql,
    _strategy_entry, live,
)
from test_account_safety_integration import _block_every_entry_control
from test_baseline_api import _insert_ticket, _manual_ticket
from test_live_arming import _fake_login
from test_live_safety import _call, _live_position, _pending_exit

FRI = date(2026, 9, 25)
MON = date(2026, 9, 28)
TUE = date(2026, 9, 29)
BROKER_CALLS = ("place_order", "place_gtt", "modify_gtt", "delete_gtt", "ltp")


def at(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=IST)


@pytest.fixture
def clock(monkeypatch):
    """The account-safety clock, pinned and movable: state["now"]."""
    from backend import account_safety
    state = {"now": at(MON, 10, 0)}
    monkeypatch.setattr(account_safety, "now_ist", lambda: state["now"])
    return state


def _create(env, path="/api/tickets", **overrides) -> dict:
    r = env.client.post(path, json=_manual_ticket(**overrides))
    assert r.status_code == 201, r.text
    t = r.json()["data"]
    assert t["status"] == "pending_confirmation", t
    return t


def _broker_calls_since(env, mark):
    return [name for name in _calls_since(env, mark) if name in BROKER_CALLS]


def _events(env, tid):
    return _sql(env, "SELECT event_type, detail FROM ticket_events WHERE ticket_id = ? ORDER BY id", tid)


def _new_kite_session(env) -> int:
    """A new Kite login appears (the backstop case of test_live_arming)."""
    _sql(env, "UPDATE sessions SET is_active = 0")
    _sql(env, "INSERT INTO sessions (access_token, user_id, user_name, login_time, account_id, is_active) "
              "VALUES ('', 'DT0001', 'Demo Trader', datetime('now'), ?, 1)", account_id(env.db))
    return _sql(env, "SELECT id FROM sessions WHERE is_active = 1")[0]["id"]


# ============================================================ unit: the window

@pytest.mark.parametrize("reviewed,now,current", [
    (at(MON, 10, 0), at(MON, 14, 0), True),        # same session
    (at(MON, 10, 0), at(MON, 15, 29, 59), True),
    (at(MON, 10, 0), at(MON, 15, 30), False),      # the close ends the window
    (at(MON, 20, 0), at(TUE, 9, 14, 59), True),    # evening -> before the open: one window (AMO)
    (at(MON, 20, 0), at(TUE, 9, 15), False),       # the open ends it
    (at(FRI, 20, 0), at(FRI + timedelta(days=1), 11, 0), True),   # Saturday
    (at(FRI, 20, 0), at(FRI + timedelta(days=2), 20, 0), True),   # Sunday
    (at(FRI, 20, 0), at(MON, 9, 14), True),
    (at(FRI, 20, 0), at(MON, 9, 15), False),
    (at(MON, 9, 14), at(MON, 9, 15), False),       # pre-open review, then the open
], ids=["same_session", "last_second", "close", "evening_to_preopen", "open", "saturday", "sunday",
        "monday_preopen", "monday_open", "preopen_to_open"])
def test_review_window_boundaries(reviewed, now, current):
    from backend import account_safety
    assert (account_safety.review_window(reviewed) == account_safety.review_window(now)) is current


def test_review_window_knows_nse_holidays():
    # Monday 2026-09-14 is Ganesh Chaturthi (NSE/CMTR/71775): Friday evening's
    # window runs until Tuesday's open.
    from backend import account_safety
    fri, holiday, tue = date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15)
    window = account_safety.review_window(at(fri, 20, 0))
    assert window == ("CLOSED", tue.isoformat())
    assert account_safety.review_window(at(holiday, 11, 0)) == window
    assert account_safety.review_window(at(tue, 9, 14)) == window
    assert account_safety.review_window(at(tue, 9, 15)) != window


def test_review_window_reads_the_calendar_not_the_pinned_market_flag(market_open):
    # market_open pins market.is_market_open to True; the window must not care.
    from backend import account_safety
    assert account_safety.review_window(at(MON, 20, 0))[0] == "CLOSED"


# ============================================================ unit: digest and token

def test_the_stored_digest_matches_the_digest_of_the_stored_row(env, clock):
    from backend import account_safety
    t = _create(env)
    row = ticket_row(env.db, t["id"])
    assert row["review_digest"] == account_safety.ticket_review_digest(row)
    assert row["reviewed_at"] == "2026-09-28 04:30:00"              # 10:00 IST on the gate clock
    assert row["reviewed_session_id"] == _sql(env, "SELECT id FROM sessions WHERE is_active = 1")[0]["id"]


def test_digest_normalises_types_and_covers_identity():
    from backend import account_safety
    base = dict(id=7, account_id=1, closes_ticket_id=None, instrument_token=2953217, quantity=5, trailing_enabled=0,
                use_gtt=1, entry_price=3000.0, stop_loss=2900.0, target_1=3300.0, target_2=None,
                trailing_distance=None, source="manual", symbol="TCS", exchange="NSE", transaction_type="BUY",
                entry_order_type="LIMIT", trailing_trigger=None, product_type="CNC")
    digest = account_safety.ticket_review_digest(base)
    assert account_safety.ticket_review_digest({**base, "trailing_enabled": False, "use_gtt": True,
                                                "entry_price": 3000, "quantity": 5.0}) == digest
    for changed in ({"id": 8}, {"account_id": 2}, {"entry_price": 3000.05}, {"quantity": 6}, {"stop_loss": 2899.95},
                    {"target_2": 3400.0}, {"source": "alert_action"}, {"closes_ticket_id": 3}, {"symbol": "INFY"}):
        assert account_safety.ticket_review_digest({**base, **changed}) != digest, changed


def test_two_identical_tickets_never_share_a_digest_or_a_token(env, clock):
    from backend import account_safety
    a, b = _create(env), _create(env)
    ra, rb = ticket_row(env.db, a["id"]), ticket_row(env.db, b["id"])
    assert ra["review_digest"] != rb["review_digest"]
    assert account_safety.review_token(ra) != account_safety.review_token(rb)


def test_the_token_changes_with_every_review(env, clock):
    t = _create(env)
    clock["now"] = at(MON, 10, 5)
    again = _re_review(env, t["id"])
    assert again["review"]["token"] != t["review"]["token"]
    assert again["review_digest"] == t["review_digest"]               # the content was not re-stamped


# ============================================================ unit: fail closed

def _subject(**fields) -> dict:
    from backend import account_safety
    row = dict(id=1, account_id=1, closes_ticket_id=None, instrument_token=2953217, quantity=5, trailing_enabled=0,
               use_gtt=1, entry_price=3000.0, stop_loss=2900.0, target_1=3300.0, target_2=None,
               trailing_distance=None, source="manual", symbol="TCS", exchange="NSE", transaction_type="BUY",
               entry_order_type="LIMIT", trailing_trigger=None, product_type="CNC",
               reviewed_at="2026-09-28 04:30:00", reviewed_session_id=1, review_generation="g" * 32)
    row["review_digest"] = account_safety.ticket_review_digest(row)
    row.update(fields)
    return row


@pytest.mark.parametrize("fields,session,reason", [
    ({}, 1, None),
    ({"review_digest": None}, 1, "no review record"),
    ({"reviewed_at": None}, 1, "cannot be read"),
    ({"reviewed_at": "28/09/2026 10:00"}, 1, "cannot be read"),
    ({"reviewed_at": "2026-09-28 04:32:01"}, 1, "in the future"),       # 121 s ahead of the clock
    ({"reviewed_session_id": 1}, 2, "earlier Kite login"),
    ({}, None, "no active Kite session"),
], ids=["current", "no_digest", "no_time", "unparseable_time", "future", "other_session", "no_session"])
def test_review_problems_fail_closed(fields, session, reason):
    from backend import account_safety
    problem = account_safety.review_problem(None, 1, _subject(**fields), at(MON, 10, 0),
                                            session_id=session, session_known=True)
    if reason is None:
        assert problem is None
    else:
        assert problem.code == "TICKET_REVIEW_REQUIRED" and reason in problem.reason and not problem.permanent


def test_up_to_120_seconds_of_clock_skew_is_tolerated():
    from backend import account_safety
    ticket = _subject(reviewed_at="2026-09-28 04:32:00")
    assert account_safety.review_problem(None, 1, ticket, at(MON, 10, 0), session_id=1, session_known=True) is None


def test_changed_content_is_permanent_tamper_evidence():
    from backend import account_safety
    problem = account_safety.review_problem(None, 1, _subject(entry_price=2990.0), at(MON, 10, 0),
                                            session_id=1, session_known=True)
    assert problem.code == "TICKET_EVIDENCE_MISMATCH" and problem.permanent


def test_a_review_that_cannot_be_evaluated_fails_closed(env, monkeypatch):
    from backend import account_safety, trading_calendar

    def broken(at=None):
        raise trading_calendar.CalendarDataError("no trading day within 31 days")
    monkeypatch.setattr(trading_calendar, "next_session_date", broken)
    failures = account_safety.check_ticket_review(None, 1, _subject(), at(MON, 20, 0), "x")
    assert [(f.code, f.http_status, f.permanent) for f in failures] == [("ACCOUNT_SAFETY_UNAVAILABLE", 503, False)]


@pytest.mark.parametrize("ticket", [
    {"source": "strategy", "transaction_type": "BUY", "closes_ticket_id": None},
    {"source": "manual", "transaction_type": "SELL", "closes_ticket_id": 4},           # an exit of a manual position
    {"source": "alert_action", "transaction_type": "SELL", "closes_ticket_id": None},  # Quick-Action sell
], ids=["strategy_entry", "linked_exit", "quick_action_sell"])
def test_strategy_entries_and_closing_sells_are_never_review_subjects(ticket):
    from backend import account_safety
    assert not account_safety.is_review_subject(ticket)
    assert account_safety.check_ticket_review(None, 1, ticket, at(MON, 10, 0)) == []


# ============================================================ payloads carry the review

def test_create_list_get_and_quick_action_buy_carry_the_review(env, clock):
    t = _create(env)
    assert t["review"]["subject"] is True and t["review"]["current"] is True and len(t["review"]["token"]) == 64
    qa = _create(env, "/api/tickets/alert-action/buy")
    assert qa["source"] == "alert_action" and qa["review"]["current"] is True and qa["review"]["token"]
    listed = {x["id"]: x["review"] for x in env.client.get("/api/tickets").json()["data"]}
    assert listed[t["id"]] == t["review"] and listed[qa["id"]] == qa["review"]
    got = env.client.get(f"/api/tickets/{t['id']}").json()["data"]["review"]
    assert got == t["review"]


def test_the_payload_review_turns_stale_exactly_when_the_gate_would(env, clock):
    t = _create(env)
    clock["now"] = at(MON, 15, 30)
    review = env.client.get(f"/api/tickets/{t['id']}").json()["data"]["review"]
    assert review["current"] is False and review["code"] == "TICKET_REVIEW_REQUIRED"
    assert "2026-09-28 session" in review["reason"]


def test_an_edit_re_stamps_the_review_over_the_new_content(env, clock):
    from backend import account_safety
    tid = _insert_ticket(env.db, status="draft", mode=None)
    clock["now"] = at(MON, 11, 0)
    r = env.client.put(f"/api/tickets/{tid}", json=_manual_ticket(quantity=4))
    assert r.status_code == 200, r.text
    data, row = r.json()["data"], ticket_row(env.db, tid)
    assert data["status"] == "pending_confirmation" and data["review"]["current"] is True
    assert row["quantity"] == 4 and row["review_digest"] == account_safety.ticket_review_digest(row)
    assert row["reviewed_at"] == "2026-09-28 05:30:00"


def test_strategy_tickets_and_exits_carry_no_token(env, clock):
    tid = _strategy_entry(env, MON.isoformat())
    review = env.client.get(f"/api/tickets/{tid}").json()["data"]["review"]
    assert review["subject"] is False and review["token"] is None
    assert "expected_review_token" not in approval_body(env.db, tid)


# ============================================================ the gate at confirmation

@pytest.mark.parametrize("mode", ["live", "dry_run"])
@pytest.mark.parametrize("later", [at(MON, 15, 30), at(TUE, 9, 15), at(TUE, 10, 0)],
                         ids=["after_the_close", "after_the_next_open", "next_day"])
def test_a_review_from_an_earlier_window_is_refused_without_any_broker_call(env, market_open, clock, mode, later):
    set_mode(env.db, mode)
    for path in ("/api/tickets", "/api/tickets/alert-action/buy"):
        clock["now"] = at(MON, 10, 0)
        t = _create(env, path)
        body = approval_body(env.db, t["id"])
        clock["now"] = later
        mark = len(env.kite.calls)
        r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=body)
        assert r.status_code == 409 and _code(r) == "TICKET_REVIEW_REQUIRED", r.text
        assert _calls_since(env, mark) == []
        _assert_untouched_pending(env, t["id"])


def test_a_current_review_executes(live, clock):
    t = _create(live)
    clock["now"] = at(MON, 15, 29)
    r = _confirm(live, t["id"])
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_an_evening_ticket_can_be_sent_as_an_amo_until_the_open(env, clock, monkeypatch):
    from backend import market
    monkeypatch.setattr(market, "is_market_open", lambda at=None: False)
    set_mode(env.db, "live")
    clock["now"] = at(MON, 20, 0)
    t = _create(env)
    clock["now"] = at(TUE, 9, 10)
    r = _confirm(env, t["id"], allow_amo=True)
    assert r.status_code == 200 and r.json()["data"]["order_variety"] == "amo", r.text


@pytest.mark.parametrize("mode", ["live", "dry_run"])
def test_a_new_kite_login_ends_every_manual_review(env, market_open, clock, monkeypatch, mode):
    from backend import kite_client
    _fake_login(monkeypatch)
    set_mode(env.db, mode)
    t = _create(env)
    kite_client.exchange_request_token("req-token-1")          # disarms LIVE (P0-C)
    set_mode(env.db, mode)                                      # LIVE re-armed for the new session
    mark = len(env.kite.calls)
    r = _confirm(env, t["id"])
    assert r.status_code == 409 and "TICKET_REVIEW_REQUIRED" in _codes(r), r.text
    assert "earlier Kite login" in r.text
    assert _calls_since(env, mark) == []
    _assert_untouched_pending(env, t["id"])
    _re_review(env, t["id"])
    r = _confirm(env, t["id"])
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed", r.text


def test_a_logout_and_login_ends_the_review_too(env, market_open, clock, monkeypatch):
    from backend import kite_client
    _fake_login(monkeypatch)
    t = _create(env)
    assert env.client.post("/api/logout").status_code == 200
    kite_client.exchange_request_token("req-token-2")
    reauthorize(env.client)          # SEC-1 P1-F: the new login also authorizes this browser again
    r = _confirm(env, t["id"])
    assert r.status_code == 409 and _code(r) == "TICKET_REVIEW_REQUIRED"


def test_tampered_content_is_rejected_for_good(live, clock):
    t = _create(live)
    body = approval_body(live.db, t["id"])
    _sql(live, "UPDATE trade_tickets SET entry_price = 2950.0, quantity = 50 WHERE id = ?", t["id"])
    mark = len(live.kite.calls)
    r = live.client.post(f"/api/tickets/{t['id']}/confirm", json=body)
    assert r.status_code == 409 and _code(r) == "TICKET_EVIDENCE_MISMATCH", r.text
    assert _calls_since(live, mark) == []
    row = ticket_row(live.db, t["id"])
    assert row["status"] == "rejected" and row["submission_state"] is None and row["kite_order_id"] is None
    assert "TICKET_EVIDENCE_MISMATCH" in _events(live, t["id"])[-1]["detail"]


@pytest.mark.parametrize("token", [None, "0" * 64], ids=["missing", "different"])
def test_a_view_that_did_not_show_this_review_is_refused(live, clock, token):
    t = _create(live)
    body = displayed_mode(live.db) if token is None else approval_body(live.db, t["id"], expected_review_token=token)
    mark = len(live.kite.calls)
    r = live.client.post(f"/api/tickets/{t['id']}/confirm", json=body)
    assert r.status_code == 409 and _code(r) == "TICKET_REVIEW_CHANGED", r.text
    assert _calls_since(live, mark) == []
    _assert_untouched_pending(live, t["id"])


def test_a_dialog_left_open_across_a_re_review_cannot_approve(live, clock):
    t = _create(live)
    shown = approval_body(live.db, t["id"])                  # the dialog opened now
    clock["now"] = at(MON, 11, 0)
    _re_review(live, t["id"])                                  # another tab re-reviews
    r = live.client.post(f"/api/tickets/{t['id']}/confirm", json=shown)
    assert r.status_code == 409 and _code(r) == "TICKET_REVIEW_CHANGED"
    assert _confirm(live, t["id"]).status_code == 200          # the fresh view can


def test_a_stale_review_is_reported_with_every_other_entry_control(live, clock):
    t = _create(live)
    _lock(live)
    clock["now"] = at(TUE, 10, 0)
    before = ticket_row(live.db, t["id"])
    r = _confirm(live, t["id"])
    assert r.status_code == 409 and _codes(r) == ["TRADING_LOCKED", "TICKET_REVIEW_REQUIRED"], r.text
    assert ticket_row(live.db, t["id"]) == before


def test_locking_and_unlocking_does_not_invalidate_a_review(live, clock):
    # FIRST_LIVE_TRADE: prepare under the lock, unlock, approve.
    _lock(live)
    t = _create(live)
    _lock(live, locked=False)
    r = _confirm(live, t["id"])
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


# ============================================================ the final gate

@pytest.mark.parametrize("mode", ["live", "dry_run"])
def test_a_close_during_revalidation_withdraws_the_claim_before_the_broker(env, market_open, clock, mode):
    set_mode(env.db, mode)
    t = _create(env)
    clock["now"] = at(MON, 15, 29, 50)

    def the_bell_rings(_kwargs):
        clock["now"] = at(MON, 15, 30, 1)
    env.kite.on_call("margins", the_bell_rings)               # revalidation reads the portfolio
    mark = len(env.kite.calls)
    r = _confirm(env, t["id"])
    assert r.status_code == 409 and _code(r) == "TICKET_REVIEW_REQUIRED", r.text
    assert _broker_calls_since(env, mark) == []
    row = ticket_row(env.db, t["id"])
    assert row["status"] == "pending_confirmation" and row["submission_state"] is None
    assert "before the broker call" in _events(env, t["id"])[-1]["detail"]


def test_a_new_login_during_revalidation_withdraws_the_claim(live, clock):
    t = _create(live)
    live.kite.on_call("margins", lambda _kwargs: _new_kite_session(live))
    mark = len(live.kite.calls)
    r = _confirm(live, t["id"])
    assert r.status_code == 409 and "TICKET_REVIEW_REQUIRED" in _codes(r), r.text
    assert _broker_calls_since(live, mark) == []
    assert ticket_row(live.db, t["id"])["status"] == "pending_confirmation"


def test_tampering_after_the_claim_cannot_change_what_is_sent(live, clock):
    # R6: the final gate verifies the row read right after the claim, and the
    # broker is sent exactly that row. A write that lands later, outside the
    # API (a claimed ticket is not editable), changes nothing that is sent:
    # the order and its protective stop are the reviewed ones.
    t = _create(live)
    live.kite.on_call("margins", lambda _kwargs: _sql(
        live, "UPDATE trade_tickets SET stop_loss = 2000.0, quantity = 500 WHERE id = ?", t["id"]))
    r = _confirm(live, t["id"])
    assert r.status_code == 200, r.text
    [order] = live.kite.calls_to("place_order")
    [gtt] = live.kite.calls_to("place_gtt")
    assert (order["quantity"], order["price"]) == (5, 3000.0)
    assert 2900.0 in gtt["trigger_values"] and 2000.0 not in gtt["trigger_values"]


def test_tampering_before_the_claim_is_rejected_before_any_broker_call(live, clock):
    t = _create(live)
    body = approval_body(live.db, t["id"])
    _sql(live, "UPDATE trade_tickets SET stop_loss = 2000.0 WHERE id = ?", t["id"])
    mark = len(live.kite.calls)
    r = live.client.post(f"/api/tickets/{t['id']}/confirm", json=body)
    assert r.status_code == 409 and _code(r) == "TICKET_EVIDENCE_MISMATCH"
    assert _calls_since(live, mark) == []


# ============================================================ Re-review

def test_re_review_re_runs_the_checks_and_records_the_review(live, clock):
    t = _create(live)
    clock["now"] = at(TUE, 9, 20)
    session = _new_kite_session(live)
    set_mode(live.db, "live")
    mark = len(live.kite.calls)
    data = _re_review(live, t["id"])
    assert {"holdings", "margins"} <= set(_calls_since(live, mark))
    assert _broker_calls_since(live, mark) == []
    row = ticket_row(live.db, t["id"])
    assert (row["status"], row["reviewed_at"], row["reviewed_session_id"]) == \
        ("pending_confirmation", "2026-09-29 03:50:00", session)
    assert row["review_digest"] == t["review_digest"]
    assert data["re_review"] == {"passed": True, "failed_checks": []}
    event = _events(live, t["id"])[-1]
    assert event["event_type"] == "modified" and f"Kite session #{session}" in event["detail"]
    r = _confirm(live, t["id"])
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_re_review_that_now_fails_a_check_is_risk_rejected(live, clock):
    t = _create(live)
    _risk(live, min_risk_reward=100.0)
    data = _re_review_raw(live, t["id"]).json()["data"]
    assert data["status"] == "risk_rejected" and data["re_review"]["passed"] is False
    assert "min_risk_reward" in data["re_review"]["failed_checks"]
    assert _events(live, t["id"])[-1]["event_type"] == "risk_rejected"
    assert _code(_confirm(live, t["id"])) == "CONFLICT_INVALID_STATE"


@pytest.mark.parametrize("failure", ["broker", "unexpected"])
def test_re_review_that_cannot_run_changes_nothing(live, clock, monkeypatch, failure):
    from backend import risk_engine
    t = _create(live)
    clock["now"] = at(TUE, 10, 0)
    before = ticket_row(live.db, t["id"])
    events_before = _events(live, t["id"])
    if failure == "broker":
        live.kite.raise_on_next("holdings", NetworkException("timed out"))
    else:
        monkeypatch.setattr(risk_engine, "run_checks", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = _re_review_raw(live, t["id"])
    assert r.status_code == 503 and _code(r) == "RISK_REVALIDATION_UNAVAILABLE", r.text
    assert ticket_row(live.db, t["id"]) == before and _events(live, t["id"]) == events_before
    assert _code(_confirm(live, t["id"])) == "TICKET_REVIEW_REQUIRED"


def test_re_review_never_blesses_tampered_content(live, clock):
    t = _create(live)
    _sql(live, "UPDATE trade_tickets SET quantity = 4 WHERE id = ?", t["id"])   # still passes every risk check
    r = _re_review_raw(live, t["id"])
    assert r.status_code == 409 and _code(r) == "TICKET_EVIDENCE_MISMATCH"
    row = ticket_row(live.db, t["id"])
    assert row["status"] == "rejected" and row["review_digest"] == t["review_digest"]
    # Revert does not stamp either: back to pending, the tampered content stays refused.
    reverted = live.client.post(f"/api/tickets/{t['id']}/revert-rejection").json()["data"]
    assert reverted["status"] == "pending_confirmation" and reverted["review"]["code"] == "TICKET_EVIDENCE_MISMATCH"
    assert ticket_row(live.db, t["id"])["review_digest"] == t["review_digest"]
    assert _code(_confirm(live, t["id"])) == "TICKET_EVIDENCE_MISMATCH"
    live.client.post(f"/api/tickets/{t['id']}/revert-rejection")
    assert _code(_re_review_raw(live, t["id"])) == "TICKET_EVIDENCE_MISMATCH"
    assert live.kite.calls_to("place_order") == []


def test_a_legacy_row_gets_its_first_digest_at_re_review(live, clock):
    from backend import account_safety
    tid = _insert_ticket(live.db, status="pending_confirmation", mode=None, risk_check_results="[]",
                         reviewed_at=None)
    assert ticket_row(live.db, tid)["review_digest"] is None
    _re_review(live, tid)
    row = ticket_row(live.db, tid)
    assert row["review_digest"] == account_safety.ticket_review_digest(row)
    assert _confirm(live, tid).status_code == 200


def test_re_review_is_allowed_while_trading_is_locked(live, clock):
    t = _create(live)
    _lock(live)
    clock["now"] = at(TUE, 10, 0)
    assert _re_review_raw(live, t["id"]).status_code == 200


def test_re_review_refuses_tickets_it_does_not_apply_to(live, clock):
    strategy = _strategy_entry(live, MON.isoformat())
    assert _code(_re_review_raw(live, strategy)) == "TICKET_REVIEW_NOT_APPLICABLE"
    buy_id, _ = _live_position(live)
    exit_id = _pending_exit(live, buy_id)
    assert _code(_re_review_raw(live, exit_id)) == "TICKET_REVIEW_NOT_APPLICABLE"
    executed = _insert_ticket(live.db, status="executed", symbol="INFY", instrument_token=408065)
    assert _code(_re_review_raw(live, executed)) == "CONFLICT_INVALID_STATE"
    assert live.kite.calls_to("place_order") == []


def _re_review_raw(env, tid):
    return env.client.post(f"/api/tickets/{tid}/re-review")


# ============================================================ duplicate confirmation

def _race(env, tid, body, monkeypatch):
    from backend import account_safety
    from backend.models import TicketConfirmRequest
    from backend.routes import tickets
    barrier = threading.Barrier(2, timeout=5)
    real = account_safety.precheck

    def synchronised(*args, **kwargs):
        decision = real(*args, **kwargs)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return decision
    monkeypatch.setattr(account_safety, "precheck", synchronised)

    def confirm():
        return _call(tickets.confirm_ticket, tid, req=TicketConfirmRequest(**body), kite=env.kite)
    with ThreadPoolExecutor(max_workers=2) as pool:
        return [f.result(timeout=30) for f in [pool.submit(confirm) for _ in range(2)]]


def test_two_concurrent_confirms_of_a_current_ticket_place_one_order(live, clock, monkeypatch):
    t = _create(live)
    results = _race(live, t["id"], approval_body(live.db, t["id"]), monkeypatch)
    assert sorted(code for code, _ in results) == [200, 409]
    assert len(live.kite.calls_to("place_order")) == 1


def test_two_concurrent_confirms_of_a_stale_ticket_are_both_refused(live, clock, monkeypatch):
    t = _create(live)
    body = approval_body(live.db, t["id"])
    clock["now"] = at(TUE, 10, 0)
    results = _race(live, t["id"], body, monkeypatch)
    assert [(code, d["error_code"]) for code, d in results] == [(409, "TICKET_REVIEW_REQUIRED")] * 2
    assert live.kite.calls_to("place_order") == []
    _assert_untouched_pending(live, t["id"])


def test_a_confirm_racing_a_re_review_places_at_most_one_order(live, clock):
    t = _create(live)
    body = approval_body(live.db, t["id"])
    outcome = {}

    def confirm_meanwhile(_kwargs):
        live.kite.on_call("margins", None)                     # once
        outcome["confirm"] = live.client.post(f"/api/tickets/{t['id']}/confirm", json=body)
    live.kite.on_call("margins", confirm_meanwhile)            # the re-review reads the portfolio
    r = _re_review_raw(live, t["id"])
    assert outcome["confirm"].status_code == 200 and outcome["confirm"].json()["data"]["status"] == "executed"
    assert r.status_code == 409 and _code(r) == "CONFLICT_INVALID_STATE"
    assert len(live.kite.calls_to("place_order")) == 1
    row = ticket_row(live.db, t["id"])
    assert row["status"] == "executed" and row["reviewed_at"] == "2026-09-28 04:30:00"


# ============================================================ exits are never held back

def _stale_manual_entry(env) -> int:
    return _insert_ticket(env.db, status="pending_confirmation", mode=None, risk_check_results="[]",
                          symbol="INFY", instrument_token=408065, entry_price=1500.0, stop_loss=1450.0,
                          target_1=1650.0, reviewed_at=None)


def test_s9b_exit_remains_possible_while_every_entry_control_and_a_stale_review_block(live, monkeypatch):
    buy_id, _ = _live_position(live)
    sell_id = _pending_exit(live, buy_id)
    _block_every_entry_control(live, monkeypatch)
    stale = _stale_manual_entry(live)
    r = _confirm(live, stale)
    assert _codes(r) == ["TRADING_LOCKED", "LIVE_PREREQUISITE_MISSING", "DAILY_LOSS_LIMIT", "DAILY_ENTRY_LIMIT",
                         "MAX_NOTIONAL_EXCEEDED", "TICKET_REVIEW_REQUIRED", "BROKER_CIRCUIT_OPEN",
                         "AMO_CONFIRMATION_REQUIRED"]
    _new_kite_session(live)
    r = live.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed", r.text
    assert [c["transaction_type"] for c in live.kite.calls_to("place_order")] == ["SELL"]


def test_s10b_emergency_exit_remains_possible_while_reviews_are_stale(live, monkeypatch):
    _live_position(live)
    _block_every_entry_control(live, monkeypatch)
    _stale_manual_entry(live)
    _new_kite_session(live)
    r = live.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    results = {x["symbol"]: x["status"] for x in r.json()["data"]["results"]}
    assert results["TCS"] == "exit_submitted"
    assert [c["transaction_type"] for c in live.kite.calls_to("place_order")] == ["SELL"]


def test_exits_of_manual_positions_and_quick_action_sells_are_not_review_subjects(live, clock):
    # A manual position's exit inherits source='manual'; it is a closing sell.
    buy_id, _ = _live_position(live, source="manual", strategy_name=None)
    sell_id = _pending_exit(live, buy_id, source="manual", strategy_name=None)
    _lock(live)
    _new_kite_session(live)
    clock["now"] = at(TUE, 10, 0)
    r = live.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed", r.text
    qa = live.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "RELIANCE", "exchange": "NSE", "quantity": 2, "entry_order_type": "MARKET", "entry_price": 1300.0,
        "rationale": "reduce", "alert_type": "breach_concentration"}).json()["data"]
    assert qa["review"]["subject"] is False and qa["review"]["token"] is None
    r = live.client.post(f"/api/tickets/{qa['id']}/confirm", json=displayed_mode(live.db))
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed", r.text
    assert sorted(c["tradingsymbol"] for c in live.kite.calls_to("place_order")) == ["RELIANCE", "TCS"]


def test_switching_to_dry_run_is_never_held_back(live, clock):
    _stale_manual_entry(live)
    _new_kite_session(live)
    r = live.client.post("/api/execution/mode", json={"mode": "dry_run"})
    assert r.status_code == 200, r.text


# ============================================================ strategy stale-signal hardening

def test_a_strategy_signal_dated_after_today_is_rejected(live):
    from backend import account_safety
    future = (account_safety.now_ist().date() + timedelta(days=1)).isoformat()
    tid = _strategy_entry(live, future)
    mark = len(live.kite.calls)
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "STALE_SIGNAL" and "after today" in r.text
    assert _calls_since(live, mark) == []
    assert ticket_row(live.db, tid)["status"] == "rejected"


def _session():
    from backend import market
    return market.last_complete_session_date()


def test_a_signal_date_that_contradicts_its_evaluation_record_is_rejected(live):
    signal = _session().isoformat()
    tid = _strategy_entry(live, signal, entry_evaluation=json.dumps({"signal_date": "2026-01-02", "conditions": []}))
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "STALE_SIGNAL" and "evaluation record" in r.text
    assert ticket_row(live.db, tid)["status"] == "rejected" and live.kite.calls_to("place_order") == []


@pytest.mark.parametrize("evaluation", [None, "not json {", json.dumps(["a list"]), json.dumps({"conditions": []})],
                         ids=["none", "malformed", "not_a_dict", "no_signal_date"])
def test_an_unreadable_or_silent_evaluation_record_never_rejects_by_itself(live, evaluation):
    signal = _session().isoformat()
    tid = _strategy_entry(live, signal, entry_evaluation=evaluation)
    r = _confirm(live, tid)
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed", r.text


def test_a_matching_evaluation_record_executes(live):
    signal = _session().isoformat()
    tid = _strategy_entry(live, signal, entry_evaluation=json.dumps({"signal_date": signal}))
    assert _confirm(live, tid).status_code == 200


def test_hardening_works_on_plain_dict_tickets():
    from backend import account_safety
    now = at(TUE, 10, 0)
    plain = {"source": "strategy", "signal_date": MON.isoformat(), "created_at": "2026-09-28 10:40:00"}
    assert account_safety.check_stale_signal(plain, now) is None
    assert account_safety.check_stale_signal({**plain, "signal_date": "2026-09-30"}, now).permanent
    assert account_safety.check_stale_signal({**plain, "entry_evaluation": json.dumps({"signal_date": "2026-09-25"})},
                                             now).code == "STALE_SIGNAL"


# ============================================================ review generations (v8)
#
# reviewed_at has one-second resolution. Without a per-review nonce, two
# reviews of one ticket in the same second produced the same token and a
# stale approval dialog stayed valid. The clock fixture is pinned, so every
# review below happens in the SAME second.

def test_every_review_gets_its_own_generation_even_in_the_same_second(live, clock):
    t = _create(live)
    first = ticket_row(live.db, t["id"])["review_generation"]
    _re_review(live, t["id"])
    second = ticket_row(live.db, t["id"])["review_generation"]
    _re_review(live, t["id"])
    third = ticket_row(live.db, t["id"])["review_generation"]
    assert len({first, second, third}) == 3 and all(len(g) == 32 for g in (first, second, third))
    assert ticket_row(live.db, t["id"])["reviewed_at"] == "2026-09-28 04:30:00"   # one and the same second


def test_a_same_second_re_review_invalidates_the_previous_token(live, clock):
    t = _create(live)
    created = approval_body(live.db, t["id"])
    _re_review(live, t["id"])
    re_reviewed = approval_body(live.db, t["id"])
    _re_review(live, t["id"])                                  # another tab, the same second
    current = approval_body(live.db, t["id"])
    tokens = {b["expected_review_token"] for b in (created, re_reviewed, current)}
    assert len(tokens) == 3
    for stale in (created, re_reviewed):
        r = live.client.post(f"/api/tickets/{t['id']}/confirm", json=stale)
        assert r.status_code == 409 and _code(r) == "TICKET_REVIEW_CHANGED", r.text
    assert live.kite.calls_to("place_order") == []
    r = live.client.post(f"/api/tickets/{t['id']}/confirm", json=current)
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed"


def test_tokens_of_two_identical_tickets_are_not_interchangeable(live, clock):
    a, b = _create(live), _create(live)
    r = live.client.post(f"/api/tickets/{b['id']}/confirm",
                         json=approval_body(live.db, b["id"], expected_review_token=a["review"]["token"]))
    assert r.status_code == 409 and _code(r) == "TICKET_REVIEW_CHANGED"
    assert live.kite.calls_to("place_order") == []


def test_a_kite_session_change_invalidates_the_old_token(live, clock):
    t = _create(live)
    old = approval_body(live.db, t["id"])
    _new_kite_session(live)
    set_mode(live.db, "live")
    r = live.client.post(f"/api/tickets/{t['id']}/confirm", json=old)
    assert r.status_code == 409 and _code(r) == "TICKET_REVIEW_REQUIRED"
    _re_review(live, t["id"])                                   # current again, under the new login
    r = live.client.post(f"/api/tickets/{t['id']}/confirm", json=old)
    assert r.status_code == 409 and _code(r) == "TICKET_REVIEW_CHANGED"
    assert live.kite.calls_to("place_order") == []


def test_concurrent_re_reviews_commit_exactly_one_new_generation(live, clock, monkeypatch):
    from backend import risk_engine
    from backend.routes import tickets
    t = _create(live)
    before = ticket_row(live.db, t["id"])["review_generation"]
    barrier = threading.Barrier(2, timeout=5)
    real = risk_engine.build_portfolio_context

    def synchronised(kite):
        try:
            barrier.wait()                                     # both have read the same generation
        except threading.BrokenBarrierError:
            pass
        return real(kite)
    monkeypatch.setattr(risk_engine, "build_portfolio_context", synchronised)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=30) for f in
                   [pool.submit(_call, tickets.re_review_ticket, t["id"], kite=live.kite) for _ in range(2)]]
    assert not barrier.broken
    assert sorted(code for code, _ in results) == [200, 409], results
    winner = next(body for code, body in results if code == 200)
    loser = next(body for code, body in results if code == 409)
    assert loser["error_code"] == "CONFLICT_INVALID_STATE"
    row = ticket_row(live.db, t["id"])
    assert row["review_generation"] not in (None, before)
    assert winner["review"]["token"] == approval_body(live.db, t["id"])["expected_review_token"]
    assert _confirm(live, t["id"]).status_code == 200


def test_a_review_recorded_without_a_generation_needs_a_re_review(live, clock):
    from backend import account_safety
    tid = _insert_ticket(live.db, status="pending_confirmation", mode=None, risk_check_results="[]",
                         review_generation=None)
    row = ticket_row(live.db, tid)
    _sql(live, "UPDATE trade_tickets SET reviewed_at = ?, reviewed_session_id = ?, review_digest = ? WHERE id = ?",
         "2026-09-28 04:30:00", _sql(live, "SELECT id FROM sessions WHERE is_active = 1")[0]["id"],
         account_safety.ticket_review_digest(row), tid)            # a v7-era review: no generation
    review = live.client.get(f"/api/tickets/{tid}").json()["data"]["review"]
    assert review["current"] is False and "review generations" in review["reason"] and review["token"] is None
    assert _code(_confirm(live, tid)) == "TICKET_REVIEW_REQUIRED"
    _re_review(live, tid)
    assert _confirm(live, tid).status_code == 200


# ============================================================ frontend contract

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _js_function(name: str) -> str:
    m = re.search(rf"^function {name}\(.*?^}}$", APP_JS, re.M | re.S)
    assert m, f"function {name} not found in app.js"
    return m.group(0)


def test_every_approval_path_sends_the_review_token_its_view_showed():
    confirm = re.search(r"confirmTicket: \(id, expectedMode, allowAmo = false, reviewToken = undefined\).*?"
                        r": undefined\),\n", APP_JS, re.S)
    assert confirm and "expected_review_token: reviewToken" in confirm.group(0)
    calls = [m.group(0) for m in re.finditer(r"await approveTicket\([^)]*\)", APP_JS)]
    assert sorted(calls) == sorted([
        "await approveTicket(id, state.activeConfirmShownMode, allowAmo, state.activeConfirmShownReview)",
        "await approveTicket(id, shownMode, false, shownReview)",      # Approvals card
        "await approveTicket(t.id, shownMode, false, sd.shownReview)",  # Signal Detail
    ]), calls
    # Captured when each view is rendered or opened, never re-read at send time.
    assert "state.activeConfirmShownReview = shownReviewFor(ticket);" in _js_function("openConfirmDialog")
    assert "sd.shownReview = shownReviewFor(ticket);" in re.search(
        r"async function openSignalDetail\(.*?\n}\n", APP_JS, re.S).group(0)
    assert "sd.shownReview" not in re.search(r"function sdOnTicketsChanged\(.*?\n}\n", APP_JS, re.S).group(0)


def test_review_refusals_are_named_and_reopen_the_dialog():
    titles = re.search(r"const ACCOUNT_SAFETY_TITLES = \{.*?\n};", APP_JS, re.S).group(0)
    for code in ("TICKET_REVIEW_REQUIRED", "TICKET_REVIEW_CHANGED", "TICKET_EVIDENCE_MISMATCH"):
        assert f"{code}:" in titles
    approve = re.search(r"async function approveTicket\(.*?\n}\n", APP_JS, re.S).group(0)
    assert "failureCodes(e)" in approve       # every listed reason, not only failures[0]
    assert 'reReviewTicket: (id) => fetchJsonMutate(`/api/tickets/${id}/re-review`, "POST")' in APP_JS
    assert 'id="ticket-rereview-btn"' in INDEX_HTML and 'id="ticket-confirm-review"' in INDEX_HTML


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_shown_review_and_failure_codes_behave():
    script = "\n".join([_js_function("shownReviewFor"), _js_function("failureCodes"), """
        const out = [
          shownReviewFor({review: {subject: true, token: "abc"}}),
          shownReviewFor({review: {subject: true, token: null}}),
          shownReviewFor({review: {subject: false, token: null}}),
          shownReviewFor({}),
          failureCodes({errorCode: "TRADING_LOCKED", failures: [{code: "TRADING_LOCKED"},
                                                              {code: "TICKET_REVIEW_REQUIRED"}]}),
        ];
        process.stdout.write(JSON.stringify(out.map((v) => v === undefined ? "<undefined>" : v)));
    """])
    r = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == ["abc", "<undefined>", "<undefined>", "<undefined>",
                                    ["TRADING_LOCKED", "TRADING_LOCKED", "TICKET_REVIEW_REQUIRED"]]


# ============================================================ migration v7

def test_migration_v7_is_idempotent(env):
    from backend import db
    conn = env.db.get_connection()
    try:
        db._m007_p1d_ticket_review(conn)
        db._m007_p1d_ticket_review(conn)
        columns = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(trade_tickets)")}
    finally:
        conn.close()
    assert (columns["reviewed_at"], columns["reviewed_session_id"], columns["review_digest"]) == \
        ("TEXT", "INTEGER", "TEXT")
    assert {v: n for v, n, _ in db.MIGRATIONS}[7] == "p1d_ticket_review"


def test_a_pending_manual_ticket_from_before_v7_needs_a_re_review(live):
    tid = _insert_ticket(live.db, status="pending_confirmation", mode=None, risk_check_results="[]",
                         reviewed_at=None, reviewed_session_id=None, review_digest=None)
    review = live.client.get(f"/api/tickets/{tid}").json()["data"]["review"]
    assert review["current"] is False and "no review record" in review["reason"]
    assert _code(_confirm(live, tid)) == "TICKET_REVIEW_REQUIRED"
