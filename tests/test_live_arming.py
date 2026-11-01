"""SEC-1 (P0-C): live arming is gated by readiness and bound to one session.

- POST /api/execution/mode to live runs a REAL, broker-checked Live
  Readiness evaluation (read-only broker calls only). It arms only if the
  result is READY, or blocked by nothing but the trading lock.
- A refused switch changes nothing, broadcasts nothing, and logs
  live_switch_refused.
- A successful switch stamps the active Kite session
  (execution_settings.live_armed_session_id).
- A new Kite login or a logout disarms LIVE: back to dry run, one
  mode_reset_on_session_change event. Live positions keep their exits.
- Backstop: a live new entry whose armed session is not the active one is
  refused (LIVE_NOT_ARMED_FOR_SESSION) before any broker call.
- Switching to dry run is never gated.

FakeKite and a temporary database only.
"""

import ast
from pathlib import Path

import pytest
from kiteconnect.exceptions import TokenException

from conftest import approval_body, account_id, clear_live_prerequisites, displayed_mode, make_live_ready, set_mode, ticket_row
from test_baseline_api import _insert_ticket, _manual_ticket
from test_live_safety import _live_position, _pending_exit, _rows, _sells, market_open  # noqa: F401 (fixture)

ROOT = Path(__file__).resolve().parents[1]
LIVE = {"mode": "live", "confirm_real_money": True}


def _exec(env, sql, *args):
    conn = env.db.get_connection()
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _settings(env) -> dict:
    return _rows(env, "SELECT mode, live_armed_session_id FROM execution_settings WHERE id = 1")[0]


def _active_session(env) -> int:
    return _rows(env, "SELECT id FROM sessions WHERE is_active = 1 ORDER BY id DESC LIMIT 1")[0]["id"]


def _events(env, event_type) -> list[str]:
    return [r["detail"] for r in _rows(env, "SELECT detail FROM system_events WHERE event_type = ? ORDER BY id",
                                      event_type)]


def _mode_broadcasts(env):
    return [m for m in env.broadcasts if m.get("type") == "mode_changed"]


def _switch_live(env):
    before = len(env.kite.calls)
    r = env.client.post("/api/execution/mode", json=LIVE)
    return r, [name for name, _ in env.kite.calls[before:]]


# ---------------------------------------------------------------- arming passes

def test_ready_arms_live_for_the_active_session(env):
    make_live_ready(env)
    r, calls = _switch_live(env)
    assert r.status_code == 200 and r.json()["data"]["mode"] == "live", r.text
    assert _settings(env) == {"mode": "live", "live_armed_session_id": _active_session(env)}
    [detail] = _events(env, "mode_switched")
    assert f"armed for Kite session #{_active_session(env)}" in detail and "READY" in detail
    assert _mode_broadcasts(env) == [{"type": "mode_changed", "mode": "live", "previous_mode": "dry_run"}]
    # The readiness it ran made broker READS only.
    from backend import live_preflight
    assert calls and set(calls) <= live_preflight.READ_ONLY_METHODS, calls


def test_trading_lock_may_stay_on_while_arming(env):
    from backend import account_safety
    make_live_ready(env)
    conn = env.db.get_connection()
    try:
        account_safety.set_trading_lock(conn, account_id(env.db), True, "arming first")
    finally:
        conn.close()
    r, _ = _switch_live(env)
    assert r.status_code == 200, r.text
    assert "trading lock still ON" in _events(env, "mode_switched")[0]
    assert _rows(env, "SELECT trading_locked FROM account_safety_state")[0]["trading_locked"] == 1  # never unlocked


def test_switching_to_live_again_while_armed_is_a_no_op_without_broker_calls(env):
    make_live_ready(env)
    assert _switch_live(env)[0].status_code == 200
    r, calls = _switch_live(env)
    assert r.status_code == 200 and calls == []
    assert len(_events(env, "mode_switched")) == 1 and len(_mode_broadcasts(env)) == 1


# ---------------------------------------------------------------- arming refused

def _no_stored_token(env, monkeypatch):
    pass  # env stores no access token: the plain env is not ready


def _profile_rejected(env, monkeypatch):
    make_live_ready(env)

    def rejected():
        raise TokenException("Incorrect `api_key` or `access_token`.")
    monkeypatch.setattr(env.kite, "profile", rejected)   # FakeKite.profile has no failure hook of its own


def _holdings_unreadable(env, monkeypatch):
    make_live_ready(env)
    env.kite.raise_on_next("holdings", TokenException("session expired"))


def _limits_and_static_ip_missing(env, monkeypatch):
    make_live_ready(env)
    clear_live_prerequisites(env.db)


def _reconciliation_stale(env, monkeypatch):
    from backend import reconciliation
    make_live_ready(env)
    monkeypatch.setattr(reconciliation, "last_cycle", {})


def _critical_finding(env, monkeypatch):
    make_live_ready(env)
    _exec(env, "INSERT INTO reconciliation_log (check_type, expected, actual, detail, issue_code, symbol) "
               "VALUES ('position_state', '5 shares', '0 shares', 'seeded', 'BROKER_POSITION_SHORTFALL', 'TCS')")


def _unknown_live_order(env, monkeypatch):
    make_live_ready(env)
    _insert_ticket(env.db, status="confirmed", submission_state="unknown", order_tag="kt1x1",
                   submitted_at="2026-09-01 04:00:00")


@pytest.mark.parametrize("degrade,code", [
    (_no_stored_token, "BROKER_SESSION"),
    (_profile_rejected, "BROKER_SESSION"),
    (_holdings_unreadable, "BROKER_READS"),
    (_limits_and_static_ip_missing, "STATIC_IP"),
    (_reconciliation_stale, "RECON_LAST_CYCLE"),
    (_critical_finding, "RECON_CRITICAL_UNRESOLVED"),
    (_unknown_live_order, "UNKNOWN_LIVE_ORDERS"),
], ids=lambda v: v if isinstance(v, str) else v.__name__)
def test_not_ready_refuses_the_switch_and_changes_nothing(env, monkeypatch, degrade, code):
    degrade(env, monkeypatch)
    r, calls = _switch_live(env)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error_code"] == "LIVE_READINESS_NOT_MET" and code in detail["blocking_codes"], detail
    assert _settings(env) == {"mode": "dry_run", "live_armed_session_id": None}
    assert _mode_broadcasts(env) == []
    assert len(_events(env, "live_switch_refused")) == 1 and _events(env, "mode_switched") == []
    from backend import live_preflight
    assert set(calls) <= live_preflight.READ_ONLY_METHODS, calls


def test_unconfirmed_switch_runs_no_readiness_at_all(env):
    before = len(env.kite.calls)
    r = env.client.post("/api/execution/mode", json={"mode": "live", "confirm_real_money": False})
    assert r.status_code == 400 and len(env.kite.calls) == before
    assert _events(env, "live_switch_refused") == []


def test_arming_without_a_readiness_result_is_refused(env):
    from backend import live_arming
    with pytest.raises(live_arming.LiveReadinessNotMet) as exc:
        live_arming.set_mode("live", True, account_id(env.db), readiness=None)
    assert exc.value.blocking_codes == ["LIVE_READINESS_NOT_EVALUATED"]
    with pytest.raises(live_arming.LiveReadinessNotMet) as exc:
        live_arming.set_mode("live", True, account_id(env.db),
                             readiness={"overall_status": "READY", "broker_checked": False})
    assert exc.value.blocking_codes == ["BROKER_NOT_CHECKED"]
    assert _settings(env)["mode"] == "dry_run"


def test_dry_run_is_never_gated_and_clears_the_arming(env, monkeypatch):
    from backend import reconciliation
    set_mode(env.db, "live")
    monkeypatch.setattr(reconciliation, "last_cycle", {})
    clear_live_prerequisites(env.db)
    r = env.client.post("/api/execution/mode", json={"mode": "dry_run"})
    assert r.status_code == 200
    assert _settings(env) == {"mode": "dry_run", "live_armed_session_id": None}


# ---------------------------------------------------------------- a session change disarms

def test_new_login_resets_live_to_dry_run(env, monkeypatch):
    from backend import kite_client
    _fake_login(monkeypatch)
    set_mode(env.db, "live")
    kite_client.exchange_request_token("req-token-1")
    assert _settings(env) == {"mode": "dry_run", "live_armed_session_id": None}
    [detail] = _events(env, "mode_reset_on_session_change")
    assert "new Kite login" in detail
    assert _mode_broadcasts(env)[-1] == {"type": "mode_changed", "mode": "dry_run", "previous_mode": "live"}


def test_login_in_dry_run_logs_nothing(env, monkeypatch):
    from backend import kite_client
    _fake_login(monkeypatch)
    kite_client.exchange_request_token("req-token-1")
    assert _events(env, "mode_reset_on_session_change") == [] and _mode_broadcasts(env) == []


def test_logout_resets_live_to_dry_run(env):
    set_mode(env.db, "live")
    assert env.client.post("/api/logout").status_code == 200
    assert _settings(env) == {"mode": "dry_run", "live_armed_session_id": None}
    assert "logged out" in _events(env, "mode_reset_on_session_change")[0]


def test_entry_armed_for_an_earlier_session_is_refused_before_any_broker_call(env, market_open):
    set_mode(env.db, "live")                                     # armed for the seeded session
    tid = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]["id"]
    # A new session appears WITHOUT going through the login path (the
    # backstop's case): the stamped session is no longer the active one.
    _exec(env, "UPDATE sessions SET is_active = 0")
    _exec(env, "INSERT INTO sessions (access_token, user_id, user_name, login_time, account_id, is_active) "
               "VALUES ('', 'DT0001', 'Demo Trader', datetime('now'), ?, 1)", account_id(env.db))
    before = len(env.kite.calls)
    r = env.client.post(f"/api/tickets/{tid}/confirm", json=approval_body(env.db, tid))
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "LIVE_NOT_ARMED_FOR_SESSION", r.text
    assert ticket_row(env.db, tid)["status"] == "pending_confirmation"
    assert [n for n, _ in env.kite.calls[before:] if n in ("place_order", "place_gtt", "ltp")] == []


def test_live_position_still_exits_after_the_session_reset(env, market_open, monkeypatch):
    # Disarming changes the global mode only: an exit runs in its position's
    # own mode (PK-3), so a live position is still sold for real.
    from backend import kite_client
    _fake_login(monkeypatch)
    set_mode(env.db, "live")
    buy_id, _ = _live_position(env)
    sell_id = _pending_exit(env, buy_id)
    kite_client.exchange_request_token("req-token-2")
    assert _settings(env)["mode"] == "dry_run"
    r = env.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200 and r.json()["data"]["mode"] == "live", r.text
    assert [s["tradingsymbol"] for s in _sells(env)] == ["TCS"]


def _fake_login(monkeypatch):
    from backend import kite_client

    class FakeKiteConnect:
        def __init__(self, api_key):
            self.api_key = api_key

        def generate_session(self, request_token, api_secret):
            return {"access_token": "tok", "public_token": "pub", "user_id": "DT0001", "user_name": "Demo Trader"}

        def set_access_token(self, token):
            pass

    monkeypatch.setattr(kite_client, "KiteConnect", FakeKiteConnect)


# ---------------------------------------------------------------- one mode setter

def test_no_backend_code_calls_the_ungated_mode_setter():
    """execution.set_execution_mode (pre-SEC-1, no readiness) stays only for
    the PK-25 research freeze on execution.py. Nothing may call it: the
    only mode writer is live_arming."""
    offenders = []
    for path in (ROOT / "backend").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "set_execution_mode":
                offenders.append(str(path.relative_to(ROOT)))
            if isinstance(node, ast.Name) and node.id == "set_execution_mode" and path.name != "execution.py":
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_only_live_arming_and_the_seed_write_the_mode():
    writers = []
    for path in (ROOT / "backend").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "UPDATE execution_settings" in text:
            writers.append(path.name)
    assert sorted(writers) == ["execution.py", "live_arming.py"], writers   # execution.py: the frozen, uncalled setter
