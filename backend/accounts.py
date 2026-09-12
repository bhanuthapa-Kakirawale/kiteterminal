import sqlite3

from backend.db import get_connection


def get_default_account(conn: sqlite3.Connection | None = None) -> sqlite3.Row:
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM accounts WHERE is_default = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("No account configured")
        return row
    finally:
        if owns_conn:
            conn.close()


def get_account(account_id: int, conn: sqlite3.Connection | None = None) -> sqlite3.Row:
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"No account with id {account_id}")
        return row
    finally:
        if owns_conn:
            conn.close()
