"""Phase 2 startup reconciliation and the entry restriction (design §8, R1-R8, F1/F2).

RESTRICTED adds refusals of NEW live entries only: exits, cancellations, Emergency Exit and protection
upkeep are asserted to work unchanged while it holds (R7). Stage 1 never calls a broker write and never
treats a failed, partial or malformed read as "absent". Stage 2 is an explicit no-op.

The `env` fixture owns its tmp database with a passed Stage 1 on record (NORMAL). `_restart` simulates a
clean process restart: a new epoch, persisted RESTRICTED(STARTUP_RECONCILIATION_INCOMPLETE)."""

import json
import time

import pytest
from kiteconnect.exceptions import NetworkException, TokenException

from conftest import approval_body, grant_write_authority, set_mode, ticket_row
from test_app_positions_api import _held_position
from test_live_safety import _live_position, _monitor, _pending_exit

LIVE_GATE_FALSE = "LIVE_ENTRIES_ENABLED = False"


@pytest.fixture
def live(env, market_open):
    set_mode(env.db, "live")
    return env


def _restart(env):
    from backend import instance_lock
    instance_lock.finish_shutdown(timeout=0)
    grant_write_authority(env.db)


def _derived(env):
    from backend import startup_reconciliation
    conn = env.db.get_connection()
    try:
        return startup_reconciliation.derive_restriction(conn, 1)
    finally:
        conn.close()


def _rows(env, sql, *args):
    conn = env.db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _stage1(trigger="startup"):
    from backend import startup_reconciliation
    return startup_reconciliation.run_stage1(trigger)


def _entry(env):
    from test_live_preflight import _live_entry
    return _live_entry(env)


def _confirm(env, tid):
    return env.client.post(f"/api/tickets/{tid}/confirm", json=approval_body(env.db, tid))


# ---------------------------------------------------------------- persisted state and derivation

def test_the_takeover_persists_restricted_and_never_inherits_normal(env):
    assert _derived(env)["state"] == "NORMAL"
    _restart(env)
    row = _rows(env, "SELECT * FROM account_restriction")[0]
    assert row["restriction_state"] == "RESTRICTED"
    assert json.loads(row["restriction_reasons"]) == ["STARTUP_RECONCILIATION_INCOMPLETE"]
    derived = _derived(env)
    assert derived["state"] == "RESTRICTED" and "STARTUP_RECONCILIATION_INCOMPLETE" in derived["reasons"]


def test_R1_a_complete_read_passes_and_the_account_derives_normal(env):
    _restart(env)
    result = _stage1()
    assert result["outcome"] == "passed" and result["state"] == "NORMAL" and result["reasons_after"] == []
    [read] = _rows(env, "SELECT * FROM broker_reads WHERE id = ?", result["broker_read_id"])
    assert read["purpose"] == "startup" and read["complete"] == 1 and read["session_expired"] == 0
    for endpoint in ("orders", "gtts", "holdings", "positions"):
        assert read[f"{endpoint}_at"] and read[f"{endpoint}_error"] is None
    assert read["instance_epoch"] == 2
    [run] = _rows(env, "SELECT * FROM startup_reconciliation_runs WHERE id = ?", result["run_id"])
    assert run["stage1_outcome"] == "passed" and run["trigger"] == "startup" and run["instance_epoch"] == 2
    findings = json.loads(run["findings"])
    assert findings["stage2"]["writes"] == 0 and "no recovery writes" in findings["stage2"]["note"]
    assert json.loads(run["inventory"])["unresolved_submissions"] == []
    assert _rows(env, "SELECT restriction_state FROM account_restriction")[0]["restriction_state"] == "NORMAL"
    assert _derived(env)["state"] == "NORMAL"


def test_R1_stage1_makes_no_broker_write(env):
    _live_position(env)
    _restart(env)
    mark = len(env.kite.calls)
    _stage1()
    assert [name for name, _ in env.kite.calls[mark:]] == ["orders", "get_gtts", "holdings", "positions"]


@pytest.mark.parametrize("endpoint,method", [("orders", "orders"), ("gtts", "get_gtts"), ("holdings", "holdings"),
                                             ("positions", "positions")])
def test_R2_one_failing_endpoint_is_named_and_nothing_is_concluded_from_it(env, endpoint, method):
    _live_position(env)
    _restart(env)
    findings_before = _rows(env, "SELECT COUNT(*) AS n FROM reconciliation_log")[0]["n"]
    env.kite.raise_on_next(method, NetworkException("connection reset"))
    result = _stage1()
    assert result["outcome"] == "incomplete" and "BROKER_READ_FAILED" in result["reasons_after"]
    [read] = _rows(env, "SELECT * FROM broker_reads WHERE id = ?", result["broker_read_id"])
    assert read["complete"] == 0 and "connection reset" in read[f"{endpoint}_error"]
    others = [e for e in ("orders", "gtts", "holdings", "positions") if e != endpoint]
    assert all(read[f"{e}_error"] is None for e in others)                       # the others were still read
    # Nothing is "absent" because one read failed: no GTT_MISSING or shortfall finding from this run.
    assert _rows(env, "SELECT COUNT(*) AS n FROM reconciliation_log")[0]["n"] == findings_before
    assert _derived(env)["state"] == "RESTRICTED"


def test_R2_retries_with_backoff_until_it_passes(env, monkeypatch):
    import asyncio
    from backend import startup_reconciliation
    _restart(env)
    env.kite.raise_on_next("get_gtts", NetworkException("down"))
    triggers = []
    real = startup_reconciliation.run_stage1
    monkeypatch.setattr(startup_reconciliation, "run_stage1", lambda trigger: triggers.append(trigger) or real(trigger))
    result = asyncio.run(startup_reconciliation.run_supervised("startup", delays=(0.01, 0.02)))
    assert result["outcome"] == "passed" and triggers == ["startup", "retry"]
    outcomes = [r["stage1_outcome"] for r in _rows(env, "SELECT stage1_outcome FROM startup_reconciliation_runs "
                                                        "WHERE instance_epoch = 2 ORDER BY id")]
    assert outcomes == ["incomplete", "passed"]


def test_the_retry_schedule_is_the_designs():
    from backend import config
    assert config.STARTUP_RECONCILIATION_RETRY_SECONDS == (30, 60, 120, 300)


def test_R3_a_session_expiring_mid_read_is_no_session_and_the_rest_is_not_attempted(env):
    _restart(env)
    env.kite.raise_on_next("get_gtts", TokenException("Incorrect `api_key` or `access_token`."))
    result = _stage1()
    assert result["outcome"] == "incomplete"
    assert {"NO_SESSION", "BROKER_READ_FAILED"} <= set(result["reasons_after"])
    [read] = _rows(env, "SELECT * FROM broker_reads WHERE id = ?", result["broker_read_id"])
    assert read["session_expired"] == 1 and read["orders_error"] is None
    assert read["holdings_error"].startswith("not attempted") and read["positions_error"].startswith("not attempted")


def test_R3_no_session_blocks_stage1_without_any_read(env, monkeypatch):
    from backend import kite_client
    _restart(env)

    def no_session(account_id=None):
        raise kite_client.NoActiveSessionError()

    monkeypatch.setattr(kite_client, "get_kite", no_session)
    result = _stage1()
    assert result["outcome"] == "blocked" and result["broker_read_id"] is None
    assert "NO_SESSION" in result["reasons_after"]


def test_R3_a_missing_rest_proxy_blocks_stage1(env, monkeypatch):
    _restart(env)
    monkeypatch.delenv("KITE_REST_PROXY", raising=False)
    mark = len(env.kite.calls)
    result = _stage1()
    assert result["outcome"] == "blocked" and "PROXY_UNAVAILABLE" in result["reasons_after"]
    assert env.kite.calls[mark:] == []


def test_R3_a_login_requires_a_new_passing_stage1(env):
    from datetime import datetime
    from conftest import IST
    assert _derived(env)["state"] == "NORMAL"
    conn = env.db.get_connection()
    conn.execute("UPDATE sessions SET login_time = ?", (datetime.now(IST).isoformat(),))   # a new login
    conn.commit()
    conn.close()
    assert "STARTUP_RECONCILIATION_INCOMPLETE" in _derived(env)["reasons"]
    assert _stage1("login")["outcome"] == "passed"
    assert _derived(env)["state"] == "NORMAL"


@pytest.mark.parametrize("malformed", ["gtt_row", "holdings_not_a_list", "positions_without_net"])
def test_R4_malformed_broker_data_is_an_incomplete_read(env, monkeypatch, malformed):
    _restart(env)
    if malformed == "gtt_row":
        real = env.kite.get_gtts
        monkeypatch.setattr(env.kite, "get_gtts", lambda: real() + [{"id": None, "status": "active"}])
    elif malformed == "holdings_not_a_list":
        monkeypatch.setattr(env.kite, "holdings", lambda: {"oops": 1})
    else:
        monkeypatch.setattr(env.kite, "positions", lambda: {"day": []})
    result = _stage1()
    assert result["outcome"] == "incomplete" and "BROKER_READ_FAILED" in result["reasons_after"]
    [read] = _rows(env, "SELECT * FROM broker_reads WHERE id = ?", result["broker_read_id"])
    assert read["complete"] == 0
    if malformed == "gtt_row":
        assert read["gtts_malformed"] == 1


def test_R4_cross_endpoint_inconsistency_fails_stage1(live):
    """An app position whose instrument the broker does not hold at all, with an entry order filled long
    ago: the endpoints cannot all be right."""
    buy_id, _ = _live_position(live)
    live.kite._holdings[:] = [h for h in live.kite._holdings if h["tradingsymbol"] != "TCS"]
    _restart(live)
    result = _stage1()
    assert result["outcome"] == "failed" and "BROKER_DATA_INCONSISTENT" in result["reasons_after"]
    [code] = [i["code"] for i in result["findings"]["inconsistencies"]]
    assert code == "APP_POSITION_ABSENT_AT_BROKER"


def test_R5_a_read_spanning_too_long_is_incomplete(env, monkeypatch):
    from backend import config
    _restart(env)
    monkeypatch.setattr(config, "STARTUP_READ_SPAN_SECONDS", 0.05)
    env.kite.on_call("positions", lambda kwargs: time.sleep(0.15))
    result = _stage1()
    assert result["outcome"] == "incomplete" and result["findings"]["read"]["span_seconds"] > 0.05
    [read] = _rows(env, "SELECT complete FROM broker_reads WHERE id = ?", result["broker_read_id"])
    assert read["complete"] == 0


def test_R6_a_crash_after_the_takeover_restarts_restricted(env):
    """The takeover writes RESTRICTED in its own transaction: a crash right after it (no Stage 1) leaves
    RESTRICTED for the next start (the subprocess form is test_instance_lock C1)."""
    from backend import instance_lock
    instance_lock.reset_for_tests()     # "crash": no shutdown record
    assert instance_lock.acquire(env.db.DB_PATH) == instance_lock.LOCKED
    from datetime import datetime, timedelta, timezone
    instance_lock.takeover(now=lambda: datetime.now(timezone.utc) + timedelta(hours=1), poll_seconds=0.01)
    derived = _derived(env)
    assert derived["state"] == "RESTRICTED"
    assert {"STARTUP_RECONCILIATION_INCOMPLETE", "PREVIOUS_INSTANCE_UNCLEAN"} <= set(derived["reasons"])
    assert _stage1()["outcome"] == "passed"
    assert _derived(env)["state"] == "NORMAL"                     # PREVIOUS_INSTANCE_UNCLEAN cleared by Stage 1


def test_stale_broker_read_restricts(env, monkeypatch):
    from backend import config, reconciliation
    monkeypatch.setattr(reconciliation, "last_cycle", {})
    conn = env.db.get_connection()
    conn.execute("UPDATE broker_reads SET finished_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()
    assert "BROKER_READ_STALE" in _derived(env)["reasons"]
    assert config.GTT_OBSERVATION_MAX_AGE_SECONDS == 420


def test_local_state_reasons_clear_when_the_cause_clears(env):
    from test_live_safety import _unknown_ticket
    from backend import execution
    tid = _unknown_ticket(env, submitted_at=execution.utc_now_sql())
    assert "UNRESOLVED_OPERATION" in _derived(env)["reasons"]
    conn = env.db.get_connection()
    conn.execute("UPDATE trade_tickets SET submission_state = 'adopted', status = 'executed' WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    assert "UNRESOLVED_OPERATION" not in _derived(env)["reasons"]


def test_a_cleanup_finding_restricts_until_reviewed(env):
    conn = env.db.get_connection()
    conn.execute("INSERT INTO reconciliation_log (check_type, expected, actual, detail, issue_code, severity) "
                 "VALUES ('order_status', 'x', 'y', 'z', 'GTT_CANCEL_OUTCOME_UNKNOWN', 'urgent')")
    conn.commit()
    conn.close()
    assert "CLEANUP_REQUIRED" in _derived(env)["reasons"]
    conn = env.db.get_connection()
    conn.execute("UPDATE reconciliation_log SET resolved = 1")
    conn.commit()
    conn.close()
    assert "CLEANUP_REQUIRED" not in _derived(env)["reasons"]


# ---------------------------------------------------------------- the entry gate: refuse, then release

def test_restricted_refuses_a_live_entry_and_a_passing_stage1_releases_it(live):
    _restart(live)
    tid = _entry(live)
    mark = len(live.kite.calls)
    r = _confirm(live, tid)
    assert r.status_code == 409 and "ACCOUNT_RESTRICTED" in [f["code"] for f in r.json()["detail"]["failures"]]
    assert "STARTUP_RECONCILIATION_INCOMPLETE" in r.json()["detail"]["message"]
    assert [n for n, _ in live.kite.calls[mark:] if n in ("place_order", "place_gtt")] == []
    assert ticket_row(live.db, tid)["status"] == "pending_confirmation"      # never permanent: still approvable
    assert _stage1()["outcome"] == "passed"
    r = _confirm(live, tid)
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed", r.text


def test_restricted_does_not_touch_dry_run_entries(env, market_open):
    _restart(env)
    set_mode(env.db, "dry_run")
    tid = _entry(env)
    assert _confirm(env, tid).status_code == 200


def test_the_live_entry_gate_constant_is_still_false_in_the_source():
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "backend" / "live_entry_gate.py").read_text(encoding="utf-8")
    assert LIVE_GATE_FALSE in source.splitlines()


def test_readiness_shows_restricted_as_a_warning_only(env):
    from backend import diagnostics, live_preflight
    _restart(env)
    check = [c for c in live_preflight.evaluate()["checks"] if c["code"] == "ACCOUNT_RESTRICTED"][0]
    assert check["status"] == "WARN" and check["blocking"] is False
    ready = diagnostics.readiness()
    assert "ACCOUNT_RESTRICTED" in [w["code"] for w in ready["warnings"]]
    assert "ACCOUNT_RESTRICTED" not in [b["code"] for b in ready["blockers"]]
    report = env.client.get("/api/diagnostics").json()["data"]
    assert report["instance"]["role"] == "owner" and report["instance"]["epoch"] == 2
    assert report["instance"]["restriction"]["state"] == "RESTRICTED"


# ---------------------------------------------------------------- R7: RESTRICTED never blocks these

def test_R7_cancelling_a_gtt_and_an_order_works_while_restricted(live):
    tid = _entry(live)
    done = _confirm(live, tid).json()["data"]                       # executed while NORMAL
    _restart(live)
    assert _derived(live)["state"] == "RESTRICTED"
    r = live.client.post(f"/api/gtts/{done['kite_gtt_id']}/cancel")
    assert r.status_code == 200 and r.json()["data"]["cancelled"] is True, r.text
    r = live.client.post(f"/api/orders/{done['kite_order_id']}/cancel")
    assert r.status_code == 200 and r.json()["data"]["cancel_requested"] is True, r.text


def test_R7_an_explicit_exit_works_while_restricted(live):
    buy_id, _ = _live_position(live)
    sell_id = _pending_exit(live, buy_id)
    _restart(live)
    assert _derived(live)["state"] == "RESTRICTED"
    r = live.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed", r.text
    assert [c["transaction_type"] for c in live.kite.calls_to("place_order")] == ["SELL"]


def test_R7_the_exit_action_works_while_restricted(live):
    buy_id, _ = _held_position(live, 40)
    _restart(live)
    r = live.client.post(f"/api/app-positions/{buy_id}/exit")
    assert r.status_code == 201, r.text


def test_R7_emergency_exit_works_while_restricted(live):
    _live_position(live)
    _restart(live)
    assert _derived(live)["state"] == "RESTRICTED"
    r = live.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    assert r.status_code == 200, r.text
    assert [x["status"] for x in r.json()["data"]["results"]] == ["exit_submitted"]


def test_R7_protection_upkeep_runs_while_restricted(live):
    """The order monitor follows a partial fill and resizes the protective GTT (a legacy modify_gtt) while
    the account is RESTRICTED: upkeep is never gated."""
    live.kite.auto_fill = False
    tid = _entry(live)
    done = _confirm(live, tid).json()["data"]
    _restart(live)
    assert _derived(live)["state"] == "RESTRICTED"
    live.kite.set_order_state(done["kite_order_id"], "COMPLETE", filled_quantity=ticket_row(live.db, tid)["quantity"])
    mark = len(live.kite.calls)
    _monitor(live)
    assert ticket_row(live.db, tid)["entry_fill_status"] == "filled"
    assert any(n in ("modify_gtt", "place_gtt") for n, _ in live.kite.calls[mark:]) or \
        ticket_row(live.db, tid)["kite_gtt_id"] is not None


# ---------------------------------------------------------------- attribution (report only) and F1/F2

def test_attribution_is_reported_and_never_enforced(live):
    _live_position(live)
    _restart(live)
    result = _stage1()
    [tcs] = [a for a in result["findings"]["attribution"] if a["instrument"] == "NSE:TCS"]
    assert tcs["app_attributed_quantity"] == 5 and tcs["broker_observed_total"] == 5 and tcs["state"] == "exclusive"
    live.kite.set_holding("TCS", 12, average_price=3000.0)       # the user bought more outside the app
    result = _stage1()
    [tcs] = [a for a in result["findings"]["attribution"] if a["instrument"] == "NSE:TCS"]
    assert tcs["state"] == "shared" and "UNEXPLAINED_SURPLUS" in tcs["reasons"]
    assert result["outcome"] == "passed"                       # reported, not enforced (Phase 7)


def test_F1_an_unknown_entry_outcome_is_never_resent_by_stage1(live):
    import requests
    tid = _entry(live)
    live.kite.accept_then_raise("place_order", requests.ReadTimeout("timed out"))
    _confirm(live, tid)
    assert ticket_row(live.db, tid)["submission_state"] == "unknown"
    _restart(live)
    sent = len(live.kite.calls_to("place_order"))
    result = _stage1()
    assert "UNRESOLVED_OPERATION" in result["reasons_after"]
    assert len(live.kite.calls_to("place_order")) == sent == 1
