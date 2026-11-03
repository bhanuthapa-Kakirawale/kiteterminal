"""SEC-1: live arming. The only supported way to change the execution mode.

LIVE is "armed" for exactly one broker session:
- Switching to it requires a broker-checked Live Readiness pass: READY, or
  blocked only by the trading lock, which may stay ON while arming
  (live_preflight.evaluate(broker=True)).
- The switch stamps the active sessions.id in
  execution_settings.live_armed_session_id.
- A new Kite login or a logout disarms it, and the mode goes back to dry run
  (disarm_live).
- As a backstop, a live NEW entry is refused unless the stamped session is
  still the active one (account_safety.check_live_armed).

Switching to dry run is never gated. Exits never depend on any of this: they
run in their position's own mode (PK-3), so disarming never touches a
position, its exit or its protective GTT.

This lives outside backend/execution.py on purpose. That file's hash is
pinned by the PK-25 research freeze (research/pk25/validation_manifest.json),
and SEC-1 changes no execution behaviour. execution.set_execution_mode, the
pre-SEC-1 setter, is no longer called by any backend code
(tests/test_live_arming.py enforces it).
"""

import logging
import sqlite3

from backend import integration_profile, obs, system_events
from backend.db import get_connection
from backend.ticker import ticker_manager

logger = logging.getLogger(__name__)

LIVE_READINESS_NOT_MET = "LIVE_READINESS_NOT_MET"
SYNTHETIC_TEST_PROFILE = "SYNTHETIC_TEST_PROFILE"


class LiveReadinessNotMet(RuntimeError):
    """The switch to live was refused: readiness did not pass."""

    def __init__(self, message: str, blocking_codes: list[str]):
        super().__init__(message)
        self.message = message
        self.blocking_codes = blocking_codes


def _mode_row(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT mode, live_armed_session_id FROM execution_settings WHERE id = 1").fetchone()


def active_session_id(conn: sqlite3.Connection, account_id: int) -> int | None:
    row = conn.execute("SELECT id FROM sessions WHERE is_active = 1 AND account_id = ? ORDER BY id DESC LIMIT 1",
                       (account_id,)).fetchone()
    return row["id"] if row is not None else None


def live_armed_for_active_session(conn: sqlite3.Connection, account_id: int) -> bool:
    """True only while the mode is live AND was armed for the session that is
    active right now."""
    row = _mode_row(conn)
    if row is None or row["mode"] != "live" or row["live_armed_session_id"] is None:
        return False
    return row["live_armed_session_id"] == active_session_id(conn, account_id)


def needs_live_arming(account_id: int) -> bool:
    """Would a switch to live have to pass readiness? False only when live is
    already armed for the active session; the switch is then a no-op."""
    conn = get_connection()
    try:
        return not live_armed_for_active_session(conn, account_id)
    finally:
        conn.close()


def _arming_refusal(readiness: dict | None) -> tuple[str, list[str]] | None:
    if readiness is None:
        return "the Live Readiness check was not run", ["LIVE_READINESS_NOT_EVALUATED"]
    if not readiness.get("broker_checked"):
        return "Live Readiness ran without its broker check", ["BROKER_NOT_CHECKED"]
    if readiness.get("overall_status") == "READY" or readiness.get("ready_except_trading_lock"):
        return None
    codes = list(readiness.get("blocking_codes") or ["NOT_READY"])
    return f"Live Readiness is NOT READY ({', '.join(codes)})", codes


def _broadcast(mode: str, previous_mode: str) -> None:
    # PK-11: every open tab updates the mode it displays. A convenience only:
    # a stale tab is still refused at confirm (expected_mode).
    ticker_manager.broadcast({"type": "mode_changed", "mode": mode, "previous_mode": previous_mode})


def set_mode(mode: str, confirm_real_money: bool, account_id: int, readiness: dict | None = None) -> str:
    """Switch the global execution mode.

    `readiness` is a live_preflight.evaluate(broker=True) result. It is
    required whenever this call ARMS live. If it does not pass, this raises
    LiveReadinessNotMet: nothing changes, and one live_switch_refused event
    is written."""
    if mode not in ("dry_run", "live"):
        raise ValueError(f"unknown execution mode {mode!r}")
    if mode == "live" and not confirm_real_money:
        raise ValueError("confirm_real_money must be true to switch to live mode")
    conn = get_connection()
    try:
        previous_mode = _mode_row(conn)["mode"]
        if mode == "live":
            refusal = None
            if integration_profile.synthetic_test_active():
                # The isolated synthetic DMB test profile can never execute live, whatever the
                # readiness (backend/integration_profile.py).
                refusal = ("this terminal runs the isolated synthetic test profile, which never executes live",
                           [SYNTHETIC_TEST_PROFILE])
            elif live_armed_for_active_session(conn, account_id):
                return mode  # already armed for this session: a no-op
            refusal = refusal or _arming_refusal(readiness)
            session_id = active_session_id(conn, account_id)
            if refusal is None and session_id is None:
                refusal = "there is no active Kite session to arm", ["BROKER_SESSION"]
            if refusal is not None:
                message, codes = refusal
                system_events.log_event(conn, account_id, "live_switch_refused",
                                        f"{previous_mode} -> live refused: {message}. The mode is unchanged.")
                conn.commit()
                obs.log_event(logger, "mode.live_switch_refused", "warning", account_id=account_id,
                              blocking=",".join(codes))
                raise LiveReadinessNotMet(
                    f"Not switched to LIVE: {message}. Resolve the blocking checks on the Live Readiness page "
                    f"(with its broker check), then switch again. Nothing changed.", codes)
            conn.execute("UPDATE execution_settings SET mode = 'live', live_armed_session_id = ?, "
                         "updated_at = datetime('now') WHERE id = 1", (session_id,))
            lock_note = ", trading lock still ON" if readiness.get("ready_except_trading_lock") else ""
            system_events.log_event(
                conn, account_id, "mode_switched",
                f"{previous_mode} -> live (armed for Kite session #{session_id}; Live Readiness "
                f"{readiness.get('overall_status')} with broker check at {readiness.get('checked_at')} UTC"
                f"{lock_note})")
        else:
            conn.execute("UPDATE execution_settings SET mode = 'dry_run', live_armed_session_id = NULL, "
                         "updated_at = datetime('now') WHERE id = 1")
            if mode != previous_mode:
                system_events.log_event(conn, account_id, "mode_switched", f"{previous_mode} -> {mode}")
        conn.commit()
    finally:
        conn.close()
    if mode != previous_mode:
        _broadcast(mode, previous_mode)
    return mode


def disarm_live(conn: sqlite3.Connection, account_id: int, why: str) -> bool:
    """The broker session changed (a new login, a logout), so the readiness
    check that armed LIVE no longer describes it: back to dry run. Logs one
    mode_reset_on_session_change event if the mode was live. Never touches a
    position.

    The caller commits, then calls broadcast_mode_reset if this returned
    True."""
    row = _mode_row(conn)
    if row is None or (row["mode"] != "live" and row["live_armed_session_id"] is None):
        return False
    was_live = row["mode"] == "live"
    conn.execute("UPDATE execution_settings SET mode = 'dry_run', live_armed_session_id = NULL, "
                 "updated_at = datetime('now') WHERE id = 1")
    if was_live:
        system_events.log_event(conn, account_id, "mode_reset_on_session_change",
                                f"live -> dry_run: {why}. LIVE must be armed again after Live Readiness passes. "
                                f"Open live positions keep their exits and protective GTTs.")
    return was_live


def broadcast_mode_reset() -> None:
    _broadcast("dry_run", "live")
