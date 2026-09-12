import os
import sqlite3
from pathlib import Path

from backend import crypto

DB_PATH = Path(__file__).parent / "kite_terminal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    label            TEXT NOT NULL,
    api_key          TEXT NOT NULL,
    api_secret_enc   TEXT NOT NULL,
    access_token_enc TEXT,
    account_type     TEXT NOT NULL DEFAULT 'investment' CHECK (account_type IN ('investment', 'trading')),
    is_default       INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    access_token    TEXT NOT NULL,
    public_token    TEXT,
    user_id         TEXT,
    user_name       TEXT,
    login_time      TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    is_active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS holdings_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    tradingsymbol TEXT NOT NULL, exchange TEXT, isin TEXT,
    quantity INTEGER, t1_quantity INTEGER, realised_quantity INTEGER,
    average_price REAL, last_price REAL, close_price REAL,
    pnl REAL, day_change REAL, day_change_percentage REAL,
    collateral_quantity INTEGER, collateral_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_holdings_snapshot_batch ON holdings_snapshot(snapshot_id);

CREATE TABLE IF NOT EXISTS positions_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    position_type TEXT NOT NULL,
    tradingsymbol TEXT NOT NULL, exchange TEXT, product TEXT,
    quantity INTEGER, overnight_quantity INTEGER,
    average_price REAL, last_price REAL, close_price REAL,
    pnl REAL, m2m REAL, unrealised REAL, realised REAL,
    buy_quantity INTEGER, buy_price REAL, buy_value REAL,
    sell_quantity INTEGER, sell_price REAL, sell_value REAL,
    multiplier REAL
);
CREATE INDEX IF NOT EXISTS idx_positions_snapshot_batch ON positions_snapshot(snapshot_id);

CREATE TABLE IF NOT EXISTS mf_holdings_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    folio TEXT, fund TEXT, tradingsymbol TEXT,
    average_price REAL, last_price REAL, last_price_date TEXT,
    quantity REAL, pnl REAL, pledged_quantity REAL
);
CREATE INDEX IF NOT EXISTS idx_mf_holdings_snapshot_batch ON mf_holdings_snapshot(snapshot_id);
"""

# account_id indexes are created after _run_column_migrations, since the
# column doesn't exist on a pre-Phase-2 database until that migration runs.
_ACCOUNT_ID_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_sessions_account ON sessions(account_id);
CREATE INDEX IF NOT EXISTS idx_holdings_snapshot_account ON holdings_snapshot(account_id);
CREATE INDEX IF NOT EXISTS idx_positions_snapshot_account ON positions_snapshot(account_id);
CREATE INDEX IF NOT EXISTS idx_mf_holdings_snapshot_account ON mf_holdings_snapshot(account_id);
"""

_ACCOUNT_ID_MIGRATIONS = [
    "sessions",
    "holdings_snapshot",
    "positions_snapshot",
    "mf_holdings_snapshot",
]


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _run_column_migrations(conn: sqlite3.Connection) -> None:
    for table in _ACCOUNT_ID_MIGRATIONS:
        if not _column_exists(conn, table, "account_id"):
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN account_id INTEGER REFERENCES accounts(id)"
            )


def _seed_default_account(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
    if count > 0:
        return

    api_key = os.environ["KITE_API_KEY"]
    api_secret = os.environ["KITE_API_SECRET"]

    cursor = conn.execute(
        """
        INSERT INTO accounts (label, api_key, api_secret_enc, account_type, is_default)
        VALUES (?, ?, ?, 'investment', 1)
        """,
        ("Primary", api_key, crypto.encrypt(api_secret)),
    )
    account_id = cursor.lastrowid

    for table in _ACCOUNT_ID_MIGRATIONS:
        conn.execute(
            f"UPDATE {table} SET account_id = ? WHERE account_id IS NULL", (account_id,)
        )


def init_db() -> None:
    crypto.ensure_encryption_key()
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        _run_column_migrations(conn)
        conn.executescript(_ACCOUNT_ID_INDEXES)
        _seed_default_account(conn)
        conn.commit()
    finally:
        conn.close()
