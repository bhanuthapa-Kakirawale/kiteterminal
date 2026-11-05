"""Session expiry for the terminal credential (docs/SECURITY_P2G_AUDIT.md §4 item 8, closed).

The two-factor credential (P2-G R-F3) dies, server-side, at the earlier of an absolute lifetime and
an inactivity timeout. Only requests the page marks as user activity push the inactivity deadline;
nothing pushes the absolute one. Expiry revokes the credential at once, which closes every
WebSocket opened under it (4401). Logout and a restart still revoke immediately. Only the browser
whose credential timed out is told why. FakeKite, throwaway database, a fake monotonic clock.
"""
import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import TERMINAL_ORIGIN, reauthorize, terminal_credential, terminal_token

WS_URL = TERMINAL_ORIGIN.replace("http://", "ws://") + "/ws/ticks"
USER = {"X-Kite-Terminal-Activity": "user"}
MIN = 60.0


@pytest.fixture
def clock(monkeypatch):
    """A fake monotonic clock. Requested BEFORE `env`, so the credential is issued on it."""
    from backend import security
    now = [10_000.0]

    class _Clock:
        def advance(self, seconds: float) -> None:
            now[0] += seconds
    monkeypatch.setattr(security, "_clock", lambda: now[0])
    monkeypatch.delenv(security.SESSION_ABSOLUTE_ENV, raising=False)
    monkeypatch.delenv(security.SESSION_IDLE_ENV, raising=False)
    return _Clock()


@pytest.fixture
def quiet_ticker(monkeypatch):
    from backend.ticker import ticker_manager
    monkeypatch.setattr(ticker_manager, "add_listener", lambda q: None)
    monkeypatch.setattr(ticker_manager, "remove_listener", lambda q: None)


def _browser(**kw):
    from backend.main import app
    return TestClient(app, base_url=TERMINAL_ORIGIN, **kw)


def _ws(cookie: str, token: str):
    return _browser().websocket_connect(WS_URL, subprotocols=["kt.v1", f"kt-token.{token}"],
                                        headers={"Origin": TERMINAL_ORIGIN, "Cookie": f"kt_session={cookie}"})


def _close_code(ws, timeout: float = 10.0) -> int:
    """The code the server closes `ws` with, waiting at most `timeout` seconds: a socket that
    outlives its session fails the test instead of hanging the suite."""
    result = {}

    def _receive():
        try:
            while True:
                ws.receive_json()
        except WebSocketDisconnect as e:
            result["code"] = e.code
    reader = threading.Thread(target=_receive, daemon=True)
    reader.start()
    reader.join(timeout)
    assert "code" in result, "the socket is still open after its session expired"
    return result["code"]


def _refused(r, reason: str | None = None) -> None:
    assert r.status_code == 401, r.text
    detail = r.json()["detail"]
    assert detail["error_code"] == "TERMINAL_SESSION_REQUIRED"
    assert detail.get("reason") == reason


# ============================================================ configuration
def test_conservative_defaults():
    from backend import security
    assert (security.DEFAULT_SESSION_ABSOLUTE_MINUTES, security.DEFAULT_SESSION_IDLE_MINUTES) == (480, 30)
    assert security.session_timeouts() == (480, 30)


@pytest.mark.parametrize("absolute,idle,expected", [
    ("240", "15", (240, 15)),
    ("0", "-5", (480, 30)),                # not positive: the defaults, never "no limit"
    ("eight hours", "1.5", (480, 30)),     # not a whole number: the defaults
    ("100000", "100000", (720, 120)),      # above the caps: clamped
    ("20", "60", (20, 20)),                # idle never exceeds the absolute lifetime
])
def test_settings_are_validated_and_fail_safe(monkeypatch, absolute, idle, expected):
    from backend import security
    monkeypatch.setenv(security.SESSION_ABSOLUTE_ENV, absolute)
    monkeypatch.setenv(security.SESSION_IDLE_ENV, idle)
    assert security.session_timeouts() == expected


def test_the_credential_cookie_carries_the_absolute_lifetime_as_max_age(clock, monkeypatch):
    from starlette.responses import Response
    from backend import security
    monkeypatch.setenv(security.SESSION_ABSOLUTE_ENV, "240")
    response = Response()
    security.set_credential_cookie(response, "value", secure=False)
    cookie = response.headers["set-cookie"].lower()
    assert "max-age=14400" in cookie and "httponly" in cookie and "samesite=strict" in cookie


def test_settings_changed_after_login_never_lengthen_that_session(clock, env, monkeypatch):
    from backend import security
    monkeypatch.setenv(security.SESSION_IDLE_ENV, "120")
    monkeypatch.setenv(security.SESSION_ABSOLUTE_ENV, "720")
    clock.advance(30 * MIN)
    _refused(env.client.get("/api/holdings"), security.EXPIRED_IDLE)


# ============================================================ idle expiry
def test_idle_expiry_locks_reads_and_writes(clock, env):
    from backend import security
    cookie = terminal_credential()
    clock.advance(30 * MIN - 1)
    assert env.client.get("/api/holdings").status_code == 200
    clock.advance(1)
    _refused(env.client.get("/api/holdings"), security.EXPIRED_IDLE)
    _refused(env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"}), security.EXPIRED_IDLE)
    assert not security.terminal_credential_valid(cookie)


def test_background_requests_do_not_count_as_activity(clock, env):
    from backend import security
    for _ in range(5):                                   # a page polling every 5 minutes, nobody there
        clock.advance(5 * MIN)
        assert env.client.get("/api/holdings").status_code == 200
    clock.advance(5 * MIN)                               # 30 minutes since login
    _refused(env.client.get("/api/holdings"), security.EXPIRED_IDLE)


def test_user_activity_pushes_the_idle_deadline(clock, env):
    for _ in range(6):                                   # 2 h of use, a click every 20 minutes
        clock.advance(20 * MIN)
        assert env.client.get("/api/holdings", headers=USER).status_code == 200
    clock.advance(29 * MIN)
    assert env.client.get("/api/holdings").status_code == 200


def test_a_user_marked_write_counts_as_activity(clock, env):
    clock.advance(20 * MIN)
    assert env.client.post("/api/ui-prefs", json={"theme": "dark"}, headers=USER).status_code == 200
    clock.advance(20 * MIN)
    assert env.client.get("/api/holdings").status_code == 200


def test_the_session_check_is_the_pages_heartbeat_only_with_both_factors(clock, env):
    from backend import security
    clock.advance(20 * MIN)
    status = env.client.get("/api/session/status", headers=USER).json()
    assert status["terminal_authorized"] is True and status["session_expired"] is None
    clock.advance(20 * MIN)                              # 40 min since login, 20 since the heartbeat
    assert env.client.get("/api/holdings").status_code == 200
    # Someone without the factors sending the marker extends nothing.
    _browser(headers=USER).get("/api/session/status")
    _browser(headers={**USER, security.TOKEN_HEADER: terminal_token()}).get("/api/session/status")
    clock.advance(10 * MIN)
    _refused(env.client.get("/api/holdings"), security.EXPIRED_IDLE)


def test_activity_never_revives_an_expired_session(clock, env):
    from backend import security
    clock.advance(31 * MIN)
    _refused(env.client.get("/api/holdings", headers=USER), security.EXPIRED_IDLE)
    clock.advance(1)
    _refused(env.client.get("/api/holdings", headers=USER), security.EXPIRED_IDLE)


# ============================================================ absolute expiry
def test_absolute_lifetime_holds_however_active_the_user_is(clock, env):
    from backend import security
    for _ in range(47):                                  # a click every 10 minutes for 7 h 50 min
        clock.advance(10 * MIN)
        assert env.client.get("/api/holdings", headers=USER).status_code == 200
    clock.advance(9 * MIN + 59)
    assert env.client.get("/api/holdings", headers=USER).status_code == 200
    clock.advance(1)                                     # exactly 8 h after login
    _refused(env.client.get("/api/holdings", headers=USER), security.EXPIRED_ABSOLUTE)
    assert security.credential_seconds_remaining() is None


def test_only_a_fresh_login_starts_a_new_lifetime(clock, env):
    from backend import security
    clock.advance(8 * 60 * MIN)
    _refused(env.client.get("/api/holdings"), security.EXPIRED_ABSOLUTE)
    reauthorize(env.client)                              # a fresh Zerodha login (password + 2FA)
    assert env.client.get("/api/holdings").status_code == 200
    assert security.credential_seconds_remaining() == pytest.approx(30 * MIN)


# ============================================================ expired credentials
def test_an_expired_pair_never_works_again_even_after_a_new_login(clock, env):
    old_cookie, old_token = terminal_credential(), terminal_token()
    clock.advance(30 * MIN)
    assert env.client.get("/api/holdings").status_code == 401
    reauthorize(env.client)
    stale = _browser(headers={"X-Kite-Terminal-Token": old_token, "X-Kite-Terminal": "1",
                              "Origin": TERMINAL_ORIGIN})
    stale.cookies.set("kt_session", old_cookie)
    assert stale.get("/api/holdings").status_code == 401
    assert stale.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"}).status_code == 401
    assert env.client.get("/api/holdings").status_code == 200


def test_an_expired_session_changes_nothing(clock, env):
    conn = env.db.get_connection()
    before = conn.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0]
    conn.close()
    clock.advance(30 * MIN)
    r = env.client.post("/api/tickets", json={"symbol": "TCS", "exchange": "NSE", "side": "BUY", "quantity": 1,
                                              "order_type": "LIMIT", "product": "CNC", "entry_price": 3000,
                                              "stop_loss": 2900, "target": 3200, "rationale": "x"})
    assert r.status_code == 401
    conn = env.db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == before
    conn.close()


def test_only_the_browser_that_held_it_is_told_its_session_expired(clock, env):
    from backend import security
    clock.advance(30 * MIN)
    r = env.client.get("/api/holdings")
    assert "30 minutes without activity" in r.json()["detail"]["message"]
    status = env.client.get("/api/session/status").json()
    assert status["terminal_authorized"] is False and status["user_name"] is None
    assert status["session_expired"] == security.EXPIRED_IDLE
    assert "Log in to Zerodha again" in status["session_expired_message"]
    other = _browser().get("/api/session/status").json()            # anyone else: nothing
    assert other["session_expired"] is None and other["session_expired_message"] is None
    forged = _browser()
    forged.cookies.set("kt_session", "x" * 43)
    _refused(forged.get("/api/holdings", headers={"X-Kite-Terminal-Token": "y" * 43}), None)


def test_expiry_is_logged_without_any_secret(clock, env, caplog):
    cookie, token = terminal_credential(), terminal_token()
    clock.advance(30 * MIN)
    with caplog.at_level(logging.WARNING):
        env.client.get("/api/holdings")
    text = caplog.text
    assert "event=terminal_session.expired reason=SESSION_IDLE_TIMEOUT" in text
    assert cookie not in text and token not in text


# ============================================================ logout and restart
def test_logout_revokes_immediately_and_is_not_reported_as_expiry(clock, env):
    cookie, token = terminal_credential(), terminal_token()
    assert env.client.post("/api/logout").status_code == 200
    stale = _browser()
    stale.cookies.set("kt_session", cookie)
    _refused(stale.get("/api/holdings", headers={"X-Kite-Terminal-Token": token}), None)
    assert stale.get("/api/session/status").json()["session_expired"] is None


def test_a_restart_forgets_the_credential_and_its_deadlines(clock, env, monkeypatch):
    from backend import security
    cookie, token = terminal_credential(), terminal_token()
    monkeypatch.setattr(security, "_credential", security._TerminalCredential())   # a new process
    _refused(env.client.get("/api/holdings"), None)
    assert security.credential_seconds_remaining() is None
    assert not security.request_authorized(cookie, token)


# ============================================================ WebSocket
def test_idle_expiry_closes_an_open_socket(clock, env, quiet_ticker):
    with _ws(terminal_credential(), terminal_token()) as ws:
        clock.advance(30 * MIN)
        assert _close_code(ws) == 4401


def test_absolute_expiry_closes_an_open_socket_despite_activity(clock, env, quiet_ticker):
    for _ in range(47):
        clock.advance(10 * MIN)
        env.client.get("/api/holdings", headers=USER)
    with _ws(terminal_credential(), terminal_token()) as ws:
        clock.advance(10 * MIN)
        assert _close_code(ws) == 4401


def test_an_expired_pair_cannot_open_a_socket(clock, env, quiet_ticker):
    cookie, token = terminal_credential(), terminal_token()
    clock.advance(30 * MIN)
    with pytest.raises(WebSocketDisconnect) as refused:
        with _ws(cookie, token) as ws:
            _close_code(ws)                              # reached only if the handshake was accepted
    assert refused.value.code == 1008


def test_activity_while_a_socket_is_open_keeps_it_open(clock, env, quiet_ticker):
    """The socket re-reads the moving inactivity deadline; streamed ticks are not activity."""
    from backend import security
    with _ws(terminal_credential(), terminal_token()) as ws:
        clock.advance(20 * MIN)
        env.client.get("/api/holdings", headers=USER)
        clock.advance(20 * MIN)                          # 40 min since login, 20 since activity
        time.sleep(1.5)                                  # longer than the socket's check interval
        assert security.credential_seconds_remaining() == pytest.approx(10 * MIN)
        clock.advance(10 * MIN)
        assert _close_code(ws) == 4401
