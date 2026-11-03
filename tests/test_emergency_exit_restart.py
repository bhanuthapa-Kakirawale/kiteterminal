"""Restart, logout, fresh login and Emergency Exit (docs/DMB_KITE_OPERATIONAL_RUNBOOK.md).

Owner decision (SEC-1 P1-F), unchanged: after an application restart or a logout NO write is
accepted - Emergency Exit included - until a fresh Kite login in the browser issues this launch's
terminal credential. Since P2-G R-F2 the account READS are locked too (only the public session
status, health and config answer), and the credential has two factors (R-F3: the cookie and the
port-isolated terminal token the login hands the page). Once authorised, Emergency Exit is never
held back by reconciliation findings, worker health or missing calendar coverage: it exits, and
reports.
FakeKite, throwaway database; a "restart" is the loss of all process memory (the in-memory
credential, the worker and reconciliation state), exactly what a new process starts with.
"""
from datetime import datetime

import pytest

from conftest import clear_healthy_live_state, reauthorize, start_kite_login
from test_emergency_exit_isolation import _emergency_exit, _positions, _sold
from test_live_safety import market_open  # noqa: F401 - fixture

BROKER_WRITES = ("place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt")


def _writes(env, mark=0):
    """Broker writes since `mark` (setup places the positions' protective GTTs itself)."""
    return [n for n, _ in env.kite.calls[mark:] if n in BROKER_WRITES]


def _restart(monkeypatch):
    """A new process: nothing in memory survives - no terminal credential, no pending login,
    no worker heartbeat, no reconciliation cycle."""
    from backend import security
    security.revoke_terminal_credential()
    security.clear_login_states()
    clear_healthy_live_state(monkeypatch)


def _code(r):
    return r.json()["detail"]["error_code"]


def test_emergency_exit_while_authenticated_exits_every_live_position(env, market_open):
    _positions(env, "TCS", "INFY")
    r = _emergency_exit(env.client)
    assert r.status_code == 200, r.text
    assert sorted(_sold(env)) == ["INFY", "TCS"]
    assert r.json()["data"]["count"] == 2


def test_after_a_restart_reads_and_emergency_exit_are_refused_until_a_fresh_login(env, market_open,
                                                                                    monkeypatch):
    _positions(env, "TCS")
    mark = len(env.kite.calls)
    _restart(monkeypatch)
    status = env.client.get("/api/session/status").json()
    assert status["connected"] is True and status["terminal_authorized"] is False   # Kite session row, no credential
    assert status["user_name"] is None
    preview = env.client.get("/api/kill-switch/emergency-exit-preview")             # P2-G R-F2: reads locked
    assert preview.status_code == 401 and _code(preview) == "TERMINAL_SESSION_REQUIRED"
    r = _emergency_exit(env.client)                                                   # the old cookie + token
    assert r.status_code == 401 and _code(r) == "TERMINAL_SESSION_REQUIRED"
    assert _writes(env, mark) == []
    reauthorize(env.client)                                                           # the fresh login
    assert env.client.get("/api/session/status").json()["terminal_authorized"] is True
    assert env.client.get("/api/kill-switch/emergency-exit-preview").status_code == 200
    r = _emergency_exit(env.client)
    assert r.status_code == 200 and _sold(env) == ["TCS"]


def test_after_logout_the_old_credential_is_dead_and_a_new_kite_login_is_needed(env, market_open, monkeypatch):
    from backend import kite_client
    from backend.routes import auth

    def session_bound_fake(account_id=None):
        # The conftest FakeKite ignores the Kite session; the real get_kite does not. Mirror it:
        # no fresh active session -> NoActiveSessionError (401 NO_SESSION), else the FakeKite.
        account = kite_client._resolve_account(account_id)
        if not kite_client.is_session_fresh(kite_client.get_active_session(account["id"])):
            raise kite_client.NoActiveSessionError()
        return env.kite
    monkeypatch.setattr(kite_client, "get_kite", session_bound_fake)
    _positions(env, "TCS")
    mark = len(env.kite.calls)
    old_cookie = env.client.cookies.get("kt_session")
    assert env.client.post("/api/logout").status_code == 200
    env.client.cookies.set("kt_session", old_cookie)
    r = _emergency_exit(env.client)
    assert r.status_code == 401 and _code(r) == "TERMINAL_SESSION_REQUIRED" and _writes(env, mark) == []
    # The credential alone is not enough after a logout: the Kite session is gone too.
    reauthorize(env.client)
    r = _emergency_exit(env.client)
    assert r.status_code == 401 and _code(r) == "NO_SESSION" and _writes(env, mark) == []

    # A full fresh login: /login (P2-G state), Kite's callback, the new credential.
    class _FakeKiteConnect:
        def __init__(self, api_key, **kwargs):
            pass

        def generate_session(self, request_token, api_secret):
            return {"access_token": "fresh-token", "public_token": "p", "user_id": "DT0001", "user_name": "Demo"}

        def set_access_token(self, token):
            pass
    monkeypatch.setattr(kite_client, "KiteConnect", _FakeKiteConnect)

    async def _no_after_login():
        return None
    monkeypatch.setattr(auth, "_after_login", _no_after_login)
    env.client.cookies.delete("kt_session")
    old_token = env.client.headers["X-Kite-Terminal-Token"]
    state = start_kite_login(env.client)
    login = env.client.get(f"/callback?request_token=fresh&status=success&kt_state={state}", follow_redirects=False)
    assert login.headers["location"].startswith("/#kt_token=")
    assert env.client.cookies.get("kt_session") not in (None, old_cookie)
    # What frontend/terminal_auth.js does with the landing fragment (P2-G R-F3).
    env.client.headers["X-Kite-Terminal-Token"] = login.headers["location"].removeprefix("/#kt_token=")
    assert env.client.headers["X-Kite-Terminal-Token"] != old_token
    r = _emergency_exit(env.client)
    assert r.status_code == 200 and _sold(env) == ["TCS"]


def test_emergency_exit_is_not_held_back_by_unhealthy_workers_or_reconciliation(env, market_open, monkeypatch):
    ids = _positions(env, "TCS")
    clear_healthy_live_state(monkeypatch)                     # no worker heartbeat, no reconciliation cycle
    conn = env.db.get_connection()
    try:
        conn.execute("INSERT INTO reconciliation_log (ticket_id, check_type, expected, actual, detail, issue_code) "
                     "VALUES (?, 'position_state', '5', '0', 'drill', 'GTT_MISSING')", (ids["TCS"],))
        conn.commit()
    finally:
        conn.close()
    r = _emergency_exit(env.client)
    assert r.status_code == 200, r.text
    assert _sold(env) == ["TCS"]


def test_emergency_exit_is_not_held_back_by_missing_calendar_coverage(env, market_open, monkeypatch):
    """With the clock in 2027 (no NSE calendar yet) entries fail closed, exits still go out."""
    from backend import trading_calendar
    _positions(env, "TCS")
    monkeypatch.setattr(trading_calendar, "now_ist", lambda: datetime(2027, 1, 4, 11, 0, tzinfo=trading_calendar.IST))
    assert trading_calendar.coverage(2027) != "COVERED"
    r = _emergency_exit(env.client)
    assert r.status_code == 200, r.text
    assert _sold(env) == ["TCS"]


def test_emergency_exit_without_the_confirmation_phrase_does_nothing(env, market_open):
    _positions(env, "TCS")
    mark = len(env.kite.calls)
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "exit"})
    assert r.status_code == 400 and _writes(env, mark) == []
