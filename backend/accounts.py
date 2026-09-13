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


def get_trading_account(conn: sqlite3.Connection | None = None) -> sqlite3.Row:
    """Returns the default account only if it's enabled for trading.

    Raises PermissionError if the account exists but isn't account_type='trading' —
    callers scoping an endpoint to trading-only functionality should catch this.
    """
    account = get_default_account(conn)
    if account["account_type"] != "trading":
        raise PermissionError("Account is not enabled for trading")
    return account
