"""Post-claim tampering auditability (backend/ticket_integrity.py, migration v10).

A claimed ticket's economic content is snapshotted at the claim and compared with the persisted
row at every trusted validation point afterwards (post_claim, pre_broker, post_execution,
trailing). A change is recorded as an append-only, hash-chained TAMPER_DETECTED event and the
path is refused; the broker only ever receives verified values. FakeKite + throwaway database.
"""
import asyncio
import json
import sqlite3

import pytest

from conftest import ticket_row
from test_account_safety import _code, _confirm, _sql, _strategy_entry, live  # noqa: F401 - live is a fixture
from test_baseline_api import _insert_ticket, _manual_ticket

BROKER_WRITES = ("place_order", "place_gtt", "modify_gtt", "delete_gtt", "cancel_order", "modify_order")


def _create(env, **overrides) -> int:
    r = env.client.post("/api/tickets", json=_manual_ticket(**overrides))
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _events(env, tid=None):
    sql = "SELECT * FROM ticket_integrity_events" + (" WHERE ticket_id = ?" if tid is not None else "") + " ORDER BY id"
    return _sql(env, sql, *([tid] if tid is not None else []))


def _broker_writes(env):
    return [n for n, _ in env.kite.calls if n in BROKER_WRITES]


def _chain(env):
    from backend import ticket_integrity
    conn = env.db.get_connection()
    try:
        return ticket_integrity.verify_chain(conn)
    finally:
        conn.close()


# ============================================================ what is bound
def test_the_categories_cover_exactly_the_p1d_digest_fields():
    from backend import account_safety, ticket_integrity
    digest_fields = set(account_safety._DIGEST_INT_FIELDS + account_safety._DIGEST_FLOAT_FIELDS
                        + account_safety._DIGEST_TEXT_FIELDS)
    grouped = [f for fields in ticket_integrity.CATEGORIES.values() for f in fields]
    assert sorted(grouped) == sorted(digest_fields) and len(grouped) == len(set(grouped))


def test_migration_v10_creates_the_append_only_audit_table(env):
    assert env.db.LATEST_SCHEMA_VERSION == 10
    conn = env.db.get_connection()
    try:
        triggers = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' "
                                                     "AND tbl_name = 'ticket_integrity_events'")}
    finally:
        conn.close()
    assert triggers == {"ticket_integrity_append_only_u", "ticket_integrity_append_only_d"}


# ============================================================ legitimate tickets
def test_an_unchanged_ticket_executes_with_one_claim_snapshot_equal_to_its_review(live):
    tid = _create(live)
    r = _confirm(live, tid)
    assert r.status_code == 200, r.text
    assert "integrity_warning" not in r.json()["data"]
    [snap] = _events(live, tid)
    assert snap["event_type"] == "CLAIM_SNAPSHOT" and snap["stage"] == "claim"
    assert snap["claimed_digest"] == ticket_row(live.db, tid)["review_digest"]
    assert set(json.loads(snap["categories"])) == {"identity", "side", "quantity", "prices", "order_type", "protection"}
    assert _chain(live) == {"ok": True, "events": 1, "first_bad_id": None, "problem": None}


def test_a_strategy_entry_and_a_dry_run_entry_are_snapshotted_too(env, market_open):
    from conftest import set_mode
    set_mode(env.db, "dry_run")
    from test_account_safety import _session
    sid = _strategy_entry(env, _session().isoformat())
    assert _confirm(env, sid).status_code == 200
    assert [e["event_type"] for e in _events(env, sid)] == ["CLAIM_SNAPSHOT"]


# ============================================================ post-claim changes
def test_a_change_right_after_the_claim_is_recorded_and_refused(live, monkeypatch):
    from backend import ticket_integrity
    tid = _create(live)
    original = ticket_integrity.record_claim

    def claim_then_tamper(conn, account_id, row):
        out = original(conn, account_id, row)
        conn.execute("UPDATE trade_tickets SET quantity = quantity + 7 WHERE id = ?", (row["id"],))
        return out
    monkeypatch.setattr(ticket_integrity, "record_claim", claim_then_tamper)
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "TICKET_TAMPERED_AFTER_CLAIM", r.text
    assert r.json()["detail"]["categories"] == ["quantity"]
    assert _broker_writes(live) == [] and live.kite.calls_to("ltp") == []
    snap, hit = _events(live, tid)
    assert (hit["event_type"], hit["stage"]) == ("TAMPER_DETECTED", "post_claim")
    assert hit["claimed_digest"] == snap["claimed_digest"] != hit["current_digest"]
    assert json.loads(hit["categories"]) == ["quantity"]
    row = ticket_row(live.db, tid)
    assert row["status"] == "rejected" and row["submission_state"] is None


def test_a_strategy_entry_changed_during_revalidation_is_refused_before_the_broker(env, market_open):
    # A strategy entry has no P1-D review digest: before the audit, a write during
    # revalidation went unnoticed (the in-memory row was sent). Now it is refused.
    from conftest import set_mode
    from test_account_safety import _session
    set_mode(env.db, "live")
    sid = _strategy_entry(env, _session().isoformat())
    env.kite.on_call("margins", lambda _kw: _sql(env, "UPDATE trade_tickets SET target_1 = 9999.0 WHERE id = ?", sid))
    r = _confirm(env, sid)
    assert r.status_code == 409 and _code(r) == "TICKET_TAMPERED_AFTER_CLAIM", r.text
    assert _broker_writes(env) == []
    assert [e["stage"] for e in _events(env, sid)] == ["claim", "pre_broker"]


def test_a_change_during_the_broker_call_is_recorded_and_the_broker_got_verified_values(live):
    tid = _create(live)
    live.kite.on_call("place_order", lambda _kw: _sql(
        live, "UPDATE trade_tickets SET stop_loss = 1.0 WHERE id = ?", tid))
    r = _confirm(live, tid)
    assert r.status_code == 200, r.text
    warning = r.json()["data"]["integrity_warning"]
    assert warning["error_code"] == "TICKET_TAMPERED_AFTER_CLAIM" and warning["categories"] == ["prices"]
    [gtt] = live.kite.calls_to("place_gtt")
    assert 2900.0 in gtt["trigger_values"] and 1.0 not in gtt["trigger_values"]
    assert [e["stage"] for e in _events(live, tid)] == ["claim", "post_execution"]


def test_repeated_detection_is_recorded_once_per_stage_and_trailing_never_resends(env):
    from backend import ticket_integrity, trailing_engine
    gtt_id = env.kite.place_gtt("single", "TCS", "NSE", [2900.0], 3000.0, [])["trigger_id"]
    tid = _insert_ticket(env.db, trailing_enabled=1, trailing_trigger="after_target_1", trailing_distance=50.0,
                         trailing_active=1, current_stop_loss=2900.0, kite_gtt_id=gtt_id)
    conn = env.db.get_connection()
    try:
        ticket_integrity.record_claim(conn, 1, conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (tid,)).fetchone())
        conn.commit()
    finally:
        conn.close()
    _sql(env, "UPDATE trade_tickets SET trailing_distance = 1.0 WHERE id = ?", tid)   # tighter trail, outside the app
    env.kite.ltp_map["NSE:TCS"] = 3100.0
    for _ in range(3):
        asyncio.run(trailing_engine._tick_once())
    assert env.kite.calls_to("modify_gtt") == []                         # never rebuilt from tampered content
    assert ticket_row(env.db, tid)["current_stop_loss"] == 2900.0
    assert [e["stage"] for e in _events(env, tid)] == ["claim", "trailing"]   # once, not three times
    conn = env.db.get_connection()
    try:
        row = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (tid,)).fetchone()
        assert ticket_integrity.check(conn, 1, row, "post_execution") == ["protection"]   # another stage: recorded
        assert ticket_integrity.check(conn, 1, row, "post_execution") == ["protection"]   # repeat: not again
        conn.commit()
    finally:
        conn.close()
    assert [e["stage"] for e in _events(env, tid)] == ["claim", "trailing", "post_execution"]


def test_a_tampered_ticket_is_never_confirmed_or_reverted_again(live, monkeypatch):
    from backend import ticket_integrity
    tid = _create(live)
    original = ticket_integrity.record_claim

    def claim_then_tamper(conn, account_id, row):
        out = original(conn, account_id, row)
        conn.execute("UPDATE trade_tickets SET entry_price = 2990.0 WHERE id = ?", (row["id"],))
        return out
    monkeypatch.setattr(ticket_integrity, "record_claim", claim_then_tamper)
    assert _code(_confirm(live, tid)) == "TICKET_TAMPERED_AFTER_CLAIM"
    monkeypatch.setattr(ticket_integrity, "record_claim", original)
    r = live.client.post(f"/api/tickets/{tid}/revert-rejection")
    assert r.status_code == 409 and _code(r) == "TICKET_TAMPERED_AFTER_CLAIM"
    # Even pushed back to pending behind the app's back, it is refused for good - and no
    # fresh review token is issued for it.
    _sql(live, "UPDATE trade_tickets SET status = 'pending_confirmation' WHERE id = ?", tid)
    r = _confirm(live, tid)
    assert r.status_code == 409 and _code(r) == "TICKET_TAMPERED_AFTER_CLAIM"
    assert ticket_row(live.db, tid)["status"] == "rejected"
    rr = live.client.post(f"/api/tickets/{tid}/re-review")
    assert rr.status_code == 409
    assert _broker_writes(live) == []


# ============================================================ audit-event integrity
def test_audit_events_cannot_be_updated_or_deleted(live):
    tid = _create(live)
    assert _confirm(live, tid).status_code == 200
    conn = live.db.get_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE ticket_integrity_events SET stage = 'claim'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM ticket_integrity_events")
    finally:
        conn.close()


def test_the_hash_chain_detects_an_edited_or_deleted_entry(live):
    for _ in range(3):
        assert _confirm(live, _create(live)).status_code == 200
    assert _chain(live)["ok"] and _chain(live)["events"] == 3
    conn = live.db.get_connection()
    try:      # a direct writer that drops the triggers - possible, and detected
        conn.execute("DROP TRIGGER ticket_integrity_append_only_u")
        conn.execute("UPDATE ticket_integrity_events SET claimed_digest = ? WHERE id = 2", ("0" * 64,))
        conn.commit()
    finally:
        conn.close()
    assert _chain(live) == {"ok": False, "events": 2, "first_bad_id": 2, "problem": "entry content changed"}
    conn = live.db.get_connection()
    try:
        conn.execute("DROP TRIGGER ticket_integrity_append_only_d")
        conn.execute("DELETE FROM ticket_integrity_events WHERE id = 2")
        conn.commit()
    finally:
        conn.close()
    assert _chain(live)["first_bad_id"] == 3 and "chain broken" in _chain(live)["problem"]


def test_boundary_a_full_consistent_rewrite_of_the_file_is_not_detectable(live):
    """Documented limitation (docs/POST_CLAIM_TAMPERING_AUDIT.md): whoever controls the database
    file can rewrite the whole chain consistently. This test pins that the guarantee is NOT
    claimed to be stronger than it is."""
    from backend import ticket_integrity
    assert _confirm(live, _create(live)).status_code == 200
    conn = live.db.get_connection()
    try:
        conn.execute("DROP TRIGGER ticket_integrity_append_only_u")
        row = dict(conn.execute("SELECT * FROM ticket_integrity_events WHERE id = 1").fetchone())
        row["claimed_digest"] = "f" * 64
        new_hash = ticket_integrity._entry_hash(ticket_integrity.GENESIS, {k: row[k] for k in ticket_integrity._HASHED})
        conn.execute("UPDATE ticket_integrity_events SET claimed_digest = ?, entry_hash = ? WHERE id = 1",
                     (row["claimed_digest"], new_hash))
        conn.commit()
    finally:
        conn.close()
    assert _chain(live)["ok"] is True


def test_events_hold_digests_and_category_names_only(live):
    tid = _create(live, quantity=5, entry_price=3000.0)
    live.kite.on_call("place_order", lambda _kw: _sql(live, "UPDATE trade_tickets SET quantity = 77 WHERE id = ?", tid))
    assert _confirm(live, tid).status_code == 200
    import re
    from backend import ticket_integrity
    hex64 = re.compile(r"^[0-9a-f]{64}$")
    events = _events(live, tid)
    assert len(events) == 2
    for e in events:
        assert hex64.match(e["claimed_digest"]) and hex64.match(e["current_digest"])
        cats = json.loads(e["categories"])
        if e["event_type"] == "CLAIM_SNAPSHOT":     # category -> digest, nothing else
            assert set(cats) == set(ticket_integrity.CATEGORIES) and all(hex64.match(v) for v in cats.values())
        else:                                       # category names only
            assert cats == ["quantity"]
        assert set(e) == {"id", "account_id", "ticket_id", "event_type", "stage", "claimed_digest", "current_digest",
                          "categories", "detected_at", "prev_hash", "entry_hash"}


def test_the_integrity_route_reports_events_and_the_chain(live):
    tid = _create(live)
    live.kite.on_call("place_order", lambda _kw: _sql(live, "UPDATE trade_tickets SET quantity = 6 WHERE id = ?", tid))
    assert _confirm(live, tid).status_code == 200
    data = live.client.get(f"/api/tickets/{tid}/integrity").json()["data"]
    assert data["tampered"] is True and data["chain"]["ok"] is True
    assert [e["event_type"] for e in data["events"]] == ["TAMPER_DETECTED", "CLAIM_SNAPSHOT"]
    assert data["events"][0]["categories"] == ["quantity"]
