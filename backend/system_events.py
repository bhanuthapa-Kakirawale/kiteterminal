import sqlite3

from backend.db import get_connection


def log_event(conn: sqlite3.Connection, account_id: int, event_type: str, detail: str | None) -> None:
    """Records a kill-switch-family action to system_events. Separate from
    ticket_events since not every event here is ticket-specific (mode
    switches, batch cancels, strategy enable/disable, emergency-exit-all
    are account-level actions). Caller is responsible for commit()."""
    conn.execute(
        "INSERT INTO system_events (account_id, event_type, detail) VALUES (?, ?, ?)",
        (account_id, event_type, detail),
    )


def log_event_own_connection(account_id: int, event_type: str, detail: str | None) -> None:
    """Convenience wrapper for callers that don't already hold an open
    connection (e.g. execution.py's set_execution_mode)."""
    conn = get_connection()
    try:
        log_event(conn, account_id, event_type, detail)
        conn.commit()
    finally:
        conn.close()


def list_events(conn: sqlite3.Connection, account_id: int, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM system_events WHERE account_id = ? ORDER BY triggered_at DESC, id DESC LIMIT ?",
        (account_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
