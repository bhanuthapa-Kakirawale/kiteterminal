"""PK-22 platform hardening: schema versioning (A), startup/index idempotency
(B), historical-data integrity (D), logging/observability (E), worker and
queue robustness (F), SQLite access (G) and operational diagnostics (H).

The NSE calendar (C) is tests/test_trading_calendar.py; restart scenarios
(I) are tests/test_restart_pk22.py. Every clock is pinned; nothing depends on
when the suite runs.
"""

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from conftest import (
    approval_body,
    IST, FakeKite, account_id, as_pre_pk22_database, make_candles, seed_ohlc, set_mode, ticket_row,
)

REPO = Path(__file__).resolve().parents[1]
PIN_SESSION = date(2026, 9, 25)  # a Friday: the pinned "last completed session" of the data tests


# ============================================================ helpers

def _rows(env, sql, params=()):
    conn = env.db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _exec(env, sql, params=()):
    conn = env.db.get_connection()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _master(path) -> list:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    finally:
        conn.close()


def _digest(path) -> dict:
    """sha256 of every table's full contents (sqlite_sequence included)."""
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        out = {}
        for t in tables:
            h = hashlib.sha256()
            for row in conn.execute(f'SELECT * FROM "{t}" ORDER BY 1'):
                h.update(repr(row).encode())
            out[t] = h.hexdigest()
        return out
    finally:
        conn.close()


def _traced_init(env, monkeypatch) -> list[str]:
    statements: list[str] = []
    real = env.db.get_connection

    def traced():
        conn = real()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(env.db, "get_connection", traced)
    try:
        env.db.init_db()
    finally:
        monkeypatch.setattr(env.db, "get_connection", real)
    return statements


def _ddl(statements) -> list[str]:
    return [s for s in statements if s.strip().split(None, 1)[0].upper() in ("CREATE", "ALTER", "DROP")]


def _db_file(env) -> Path:
    return Path(env.db.DB_PATH)


# ============================================================ PK-22A schema versioning

def test_brand_new_database_migrates_to_the_current_version(env):
    from backend import db
    history = _rows(env, "SELECT version, migration_name FROM schema_version ORDER BY version")
    assert [(h["version"], h["migration_name"]) for h in history] == [(v, n) for v, n, _ in db.MIGRATIONS]
    assert history[-1]["version"] == db.LATEST_SCHEMA_VERSION == 9   # LIVE-0 added v5, SEC-1 v6, P1-D v7 + v8, DMB inbox v9
    conn = env.db.get_connection()
    try:
        status = db.schema_status(conn)
    finally:
        conn.close()
    assert status["up_to_date"] and status["supported"] and status["pending"] == []


def test_second_startup_makes_no_schema_change_and_writes_nothing(env, monkeypatch):
    # The env fixture seeds a plaintext session token AFTER its init_db (the
    # pre-PK-18 shape), which the next start scrubs: that start is the "first".
    env.db.init_db()
    assert env.db.last_init["startup_row_changes"] == 2   # token encrypted into accounts + session blanked
    before_master, before_data = _master(_db_file(env)), _digest(_db_file(env))
    statements = _traced_init(env, monkeypatch)
    assert _ddl(statements) == []                                      # no DDL at all
    assert env.db.last_init == {"applied_migrations": [], "startup_row_changes": 0,
                                "schema_version": env.db.LATEST_SCHEMA_VERSION}
    assert _master(_db_file(env)) == before_master
    assert _digest(_db_file(env)) == before_data                        # sqlite_sequence included
    assert not list(_db_file(env).parent.glob("*.pre_schema_v*_bak"))  # nothing pending: no backup


def test_migration_history_is_recorded_once(env):
    for _ in range(3):
        env.db.init_db()
    rows = _rows(env, "SELECT version, COUNT(*) AS n FROM schema_version GROUP BY version")
    assert [(r["version"], r["n"]) for r in rows] == [(1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (6, 1), (7, 1), (8, 1), (9, 1)]


@pytest.mark.parametrize("tamper", ["newer_version", "renamed_migration", "gap"])
def test_unknown_or_newer_schema_is_refused_before_any_write(env, tamper):
    from backend import db
    if tamper == "newer_version":
        _exec(env, "INSERT INTO schema_version (version, migration_name) VALUES (99, 'from_the_future')")
    elif tamper == "renamed_migration":
        _exec(env, "UPDATE schema_version SET migration_name = 'something_else' WHERE version = 2")
    else:
        _exec(env, "DELETE FROM schema_version WHERE version = 2")
    before = _digest(_db_file(env))
    with pytest.raises(db.DatabaseSchemaUnsupported) as err:
        db.init_db()
    assert err.value.error_code == "DATABASE_SCHEMA_UNSUPPORTED"
    assert "DATABASE_SCHEMA_UNSUPPORTED" in str(err.value)
    assert _digest(_db_file(env)) == before
    conn = env.db.get_connection()
    try:
        status = db.schema_status(conn)
    finally:
        conn.close()
    assert status["supported"] is False and status["up_to_date"] is False


def test_interrupted_migration_rolls_back_and_resumes(env, monkeypatch):
    from backend import db
    # A database at v2 (drop what v3/v4 created and forget them).
    for index in ("idx_trade_tickets_closes_ticket", "idx_trade_tickets_account_mode_status",
                  "idx_trade_tickets_kite_order", "idx_trade_tickets_kite_gtt"):
        _exec(env, f"DROP INDEX {index}")
    _exec(env, "DROP TABLE ohlc_history_status")
    _exec(env, "DROP TABLE symbol_transitions")
    _exec(env, "DELETE FROM schema_version WHERE version > 2")

    def crashing_v4(conn):
        conn.execute("CREATE TABLE ohlc_history_status (instrument_token INTEGER PRIMARY KEY)")
        raise RuntimeError("simulated crash half-way through migration 4")

    real_migrations = db.MIGRATIONS
    migrations = [m if m[0] != 4 else (4, m[1], crashing_v4) for m in real_migrations]
    monkeypatch.setattr(db, "MIGRATIONS", migrations)
    with pytest.raises(RuntimeError, match="simulated crash"):
        db.init_db()
    versions = [r["version"] for r in _rows(env, "SELECT version FROM schema_version ORDER BY version")]
    assert versions == [1, 2, 3]                                          # v3 committed, v4 rolled back
    names = {r["name"] for r in _rows(env, "SELECT name FROM sqlite_master")}
    assert "idx_trade_tickets_closes_ticket" in names
    assert "ohlc_history_status" not in names                             # its DDL was rolled back too

    monkeypatch.setattr(db, "MIGRATIONS", real_migrations)  # the next start resumes from v3
    db.init_db()
    assert db.last_init["applied_migrations"] == [4, 5, 6, 7, 8, 9]
    assert [r["version"] for r in _rows(env, "SELECT version FROM schema_version ORDER BY version")] == \
        [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert "ohlc_history_status" in {r["name"] for r in _rows(env, "SELECT name FROM sqlite_master")}


def test_a_migration_that_adds_foreign_key_violations_is_refused(env, monkeypatch):
    from backend import db

    def orphaning(conn):
        conn.execute("INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (987654, 'created', 'orphan')")

    latest = db.LATEST_SCHEMA_VERSION
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, (latest + 1, "test_orphan", orphaning)])
    monkeypatch.setattr(db, "LATEST_SCHEMA_VERSION", latest + 1)
    with pytest.raises(db.DatabaseMigrationFailed, match="foreign key"):
        db.init_db()
    assert not _rows(env, "SELECT 1 FROM ticket_events WHERE ticket_id = 987654")
    assert max(r["version"] for r in _rows(env, "SELECT version FROM schema_version")) == latest


def test_pending_migrations_on_an_existing_database_take_a_verified_backup_first(env):
    set_mode(env.db, "live")
    as_pre_pk22_database(env.db)
    env.db.init_db()
    backups = list(_db_file(env).parent.glob(f"test.db.pre_schema_v{env.db.LATEST_SCHEMA_VERSION}_bak*"))
    assert len(backups) == 1
    conn = sqlite3.connect(backups[0])
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # The copy is the database as it was BEFORE migrating.
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='schema_version'").fetchone()[0] == 0
        assert conn.execute("SELECT mode FROM execution_settings").fetchone()[0] == "live"
    finally:
        conn.close()


_LEGACY_SCHEMA = """
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, api_key TEXT NOT NULL,
    api_secret_enc TEXT NOT NULL, access_token_enc TEXT,
    account_type TEXT NOT NULL DEFAULT 'investment' CHECK (account_type IN ('investment', 'trading')),
    is_default INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, access_token TEXT NOT NULL, public_token TEXT, user_id TEXT,
    user_name TEXT, login_time TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')),
    is_active INTEGER NOT NULL DEFAULT 1, account_id INTEGER REFERENCES accounts(id));
CREATE TABLE trade_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL REFERENCES accounts(id),
    source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'strategy')), strategy_name TEXT,
    symbol TEXT NOT NULL, exchange TEXT NOT NULL DEFAULT 'NSE', instrument_token INTEGER NOT NULL,
    transaction_type TEXT NOT NULL CHECK (transaction_type IN ('BUY', 'SELL')),
    quantity INTEGER NOT NULL CHECK (quantity > 0), entry_price REAL NOT NULL CHECK (entry_price > 0),
    entry_order_type TEXT NOT NULL CHECK (entry_order_type IN ('MARKET', 'LIMIT')),
    stop_loss REAL NOT NULL CHECK (stop_loss > 0), target_1 REAL NOT NULL CHECK (target_1 > 0), target_2 REAL,
    trailing_enabled INTEGER NOT NULL DEFAULT 0, trailing_trigger TEXT, trailing_distance REAL,
    current_stop_loss REAL, trailing_active INTEGER NOT NULL DEFAULT 0,
    product_type TEXT NOT NULL CHECK (product_type IN ('CNC', 'MIS', 'NRML')), use_gtt INTEGER NOT NULL DEFAULT 1,
    risk_amount REAL, risk_reward_ratio REAL, rationale TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'pending_confirmation', 'confirmed',
        'executed', 'rejected', 'cancelled', 'failed')),
    mode TEXT CHECK (mode IN ('dry_run', 'live') OR mode IS NULL), kite_order_id TEXT, kite_gtt_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')), executed_at TEXT, closed_at TEXT,
    last_trailing_check_at TEXT);
CREATE TABLE ticket_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL REFERENCES trade_tickets(id),
    event_type TEXT NOT NULL CHECK (event_type IN ('created', 'confirmed', 'rejected', 'executed',
        'execution_failed', 'protection_failed', 'modified', 'trailing_adjusted', 'closed')),
    detail TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE dashboard_panels (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL REFERENCES accounts(id),
    panel_type TEXT NOT NULL, display_order INTEGER NOT NULL,
    size TEXT NOT NULL DEFAULT 'medium' CHECK (size IN ('small', 'medium', 'large')),
    visible INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE UNIQUE INDEX idx_dashboard_panels_account_type ON dashboard_panels(account_id, panel_type);
CREATE TABLE ohlc_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT, instrument_token INTEGER NOT NULL, symbol TEXT NOT NULL,
    date TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume INTEGER NOT NULL);
CREATE UNIQUE INDEX idx_ohlc_daily_token_date ON ohlc_daily(instrument_token, date);
"""


def test_an_old_database_migrates_forward_keeping_its_rows(tmp_path, monkeypatch):
    """An early (Phase-2-era) database — the oldest status/source/event
    allow-lists, none of the later columns, the original dashboard index, a
    plaintext session token — walks the whole legacy chain plus PK-22 in one
    start, and keeps every row."""
    from cryptography.fernet import Fernet
    from backend import crypto, db
    monkeypatch.setenv("KITE_TERMINAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("KITE_API_KEY", "k")
    monkeypatch.setenv("KITE_API_SECRET", "s")
    monkeypatch.setattr(crypto, "ENV_PATH", tmp_path / ".env")
    path = tmp_path / "legacy.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute("INSERT INTO accounts (label, api_key, api_secret_enc, account_type, is_default) "
                 "VALUES ('Primary', 'k', ?, 'investment', 1)", (crypto.encrypt("s"),))
    conn.execute("INSERT INTO sessions (access_token, user_name, login_time, account_id) VALUES "
                 "('legacy-plain-token-123', 'U', '2026-09-01T09:00:00+05:30', 1)")
    conn.execute("INSERT INTO trade_tickets (account_id, symbol, instrument_token, transaction_type, quantity, "
                 "entry_price, entry_order_type, stop_loss, target_1, product_type, rationale, status, mode, "
                 "kite_order_id, executed_at) VALUES (1, 'INFY', 408065, 'BUY', 7, 1500, 'LIMIT', 1400, 1700, "
                 "'CNC', 'legacy', 'executed', 'live', 'O-1', '2026-09-01 04:00:00')")
    conn.execute("INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (1, 'executed', 'legacy event')")
    conn.execute("INSERT INTO dashboard_panels (account_id, panel_type, display_order) VALUES (1, 'holdings_table', 0)")
    split = make_candles(PIN_SESSION, 40, 1000.0)
    for i, c in enumerate(split):
        factor = 0.5 if i >= 20 else 1.0   # a 1:2 split in the middle, never back-adjusted
        conn.execute("INSERT INTO ohlc_daily (instrument_token, symbol, date, open, high, low, close, volume) "
                     "VALUES (408065, 'INFY', ?, ?, ?, ?, ?, ?)",
                     (c["date"].strftime("%Y-%m-%d"), c["open"] * factor, c["high"] * factor, c["low"] * factor,
                      c["close"] * factor, c["volume"]))
    conn.commit()
    conn.close()

    db.init_db()
    db.init_db()  # and a second start changes nothing
    assert db.last_init["applied_migrations"] == [] and db.last_init["startup_row_changes"] == 0

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        assert [r[0] for r in conn.execute("SELECT version FROM schema_version")] == [v for v, _, _ in db.MIGRATIONS]
        ticket = conn.execute("SELECT * FROM trade_tickets WHERE id = 1").fetchone()
        assert (ticket["symbol"], ticket["quantity"], ticket["kite_order_id"]) == ("INFY", 7, "O-1")
        assert ticket["filled_quantity"] == 7                               # PK-13 backfill of a legacy fill
        columns = {r[1] for r in conn.execute("PRAGMA table_info(trade_tickets)")}
        assert {"entry_evaluation", "kite_chart_url", "kite_chart_status", "order_tag", "filled_quantity"} <= columns
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='trade_tickets'").fetchone()[0]
        assert "'risk_rejected'" in sql and "'alert_action'" in sql
        assert conn.execute("SELECT detail FROM ticket_events WHERE ticket_id = 1").fetchone()[0] == "legacy event"
        assert conn.execute("SELECT account_type FROM accounts").fetchone()[0] == "trading"
        session = conn.execute("SELECT access_token, account_id FROM sessions").fetchone()
        assert session["access_token"] == "" and session["account_id"] == 1   # PK-18 scrub + account backfill
        assert crypto.decrypt(conn.execute("SELECT access_token_enc FROM accounts").fetchone()[0]) == \
            "legacy-plain-token-123"
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_dashboard_panels_account_type" not in names
        assert "idx_dashboard_panels_account_screen_type" in names
        status = conn.execute("SELECT status, bar_count FROM ohlc_history_status WHERE instrument_token = 408065").fetchone()
        assert (status["status"], status["bar_count"]) == ("SUSPECT", 40)    # the unadjusted split is flagged
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    assert len(list(tmp_path.glob(f"legacy.db.pre_schema_v{db.LATEST_SCHEMA_VERSION}_bak*"))) == 1


# Audit P2-13, without real account data. This test used to copy the
# developer's local pre-PK-22 backup of the real database. It now builds a
# synthetic pre-PK-22 database from tests/fixtures/pre_pk22_schema.sql: the
# exact DDL that the last pre-PK-22 code (commit 6446c79) produced, captured
# from that code on an empty temporary database (see the fixture header).
# It is filled with representative rows of every kind a pre-PK-22 install
# held. The real backup is never read: tests/real_data_guard.py refuses any
# access to it, suite-wide.
PRE_PK22_SCHEMA = REPO / "tests" / "fixtures" / "pre_pk22_schema.sql"

# The rows the startup invariants may ADD to a pre-PK-22 database: seeds
# for panels and strategies that did not exist yet. Pre-existing rows are
# never changed or removed (asserted below).
_SEEDED_BY_STARTUP = {"dashboard_panels", "strategy_configs", "strategy_config_versions"}


def _seed_pre_pk22_rows(conn, crypto) -> None:
    """Representative pre-PK-22 data (synthetic; placeholder identifiers)."""
    def ticket(**fields):
        base = dict(account_id=1, source="manual", symbol="TCS", exchange="NSE", instrument_token=2953217,
                    transaction_type="BUY", quantity=5, entry_price=3000.0, entry_order_type="LIMIT",
                    stop_loss=2900.0, target_1=3300.0, product_type="CNC", rationale="synthetic pre-PK-22",
                    status="pending_confirmation")
        base.update(fields)
        conn.execute(f"INSERT INTO trade_tickets ({', '.join(base)}) VALUES ({', '.join('?' * len(base))})",
                     tuple(base.values()))

    conn.execute("INSERT INTO accounts (id, label, api_key, api_secret_enc, access_token_enc, account_type, "
                 "is_default) VALUES (1, 'Primary', 'synthetic_key', ?, ?, 'trading', 1)",
                 (crypto.encrypt("synthetic_secret"), crypto.encrypt("synthetic-access-token")))
    conn.execute("INSERT INTO sessions (access_token, user_id, user_name, login_time, account_id) "
                 "VALUES ('', 'XX0000', 'Synthetic Trader', '2026-09-25T09:00:00+05:30', 1)")
    conn.execute("INSERT INTO execution_settings (id, mode) VALUES (1, 'live')")
    conn.execute("INSERT INTO risk_settings (account_id, max_order_notional, max_daily_loss_amount, "
                 "max_new_entries_per_day) VALUES (1, 50000, 2000, 2)")
    conn.execute("INSERT INTO account_safety_state (account_id, trading_locked, trading_locked_at, "
                 "trading_lock_reason) VALUES (1, 1, '2026-09-25 04:00:00', 'pre-PK-22 lock')")
    # 1: an OPEN live position (filled, protected); 2+3: a CLOSED live round
    # trip; 4: a dry-run paper fill; 5: approvable; 6: risk-rejected;
    # 7: an alert-action sell; 8: a live order with an UNKNOWN outcome.
    ticket(id=1, source="strategy", strategy_name="momentum_breakout", status="executed", mode="live",
           kite_order_id="O-OPEN", kite_gtt_id=9001, executed_at="2026-09-24 04:00:00", order_tag="kt1xpre",
           submission_state="acknowledged", entry_fill_status="filled", filled_price=3001.5,
           filled_at="2026-09-24 04:00:05", filled_quantity=5, current_stop_loss=2900.0, signal_date="2026-09-23")
    ticket(id=2, symbol="INFY", instrument_token=408065, quantity=7, entry_price=1500.0, stop_loss=1400.0,
           target_1=1700.0, status="executed", mode="live", kite_order_id="O-INFY", executed_at="2026-09-22 04:00:00",
           entry_fill_status="filled", filled_price=1500.0, filled_quantity=7, closed_at="2026-09-24 05:00:00")
    ticket(id=3, source="strategy", strategy_name="momentum_breakout_exit", symbol="INFY", instrument_token=408065,
           transaction_type="SELL", quantity=7, entry_price=1600.0, entry_order_type="MARKET", stop_loss=1400.0,
           target_1=1700.0, status="executed", mode="live", kite_order_id="O-INFY-X", closes_ticket_id=2,
           executed_at="2026-09-24 04:59:00", entry_fill_status="filled", filled_price=1600.0, filled_quantity=7)
    ticket(id=4, symbol="RELIANCE", instrument_token=738561, quantity=10, entry_price=1300.0, stop_loss=1250.0,
           target_1=1450.0, status="executed", mode="dry_run", kite_order_id="PAPER-O-1", kite_gtt_id=None,
           entry_fill_status="filled", filled_price=1300.65, filled_quantity=10, executed_at="2026-09-23 04:00:00")
    ticket(id=5, symbol="SUNPHARMA", instrument_token=857857, entry_price=1700.0, stop_loss=1650.0, target_1=1900.0,
           risk_check_results='[{"rule": "max_open_positions", "passed": true}]')
    ticket(id=6, status="risk_rejected", risk_check_results='[{"rule": "max_open_positions", "passed": false}]')
    ticket(id=7, source="alert_action", symbol="RELIANCE", instrument_token=738561, transaction_type="SELL",
           quantity=3, entry_price=1290.0, entry_order_type="MARKET", stop_loss=1250.0, target_1=1290.0,
           status="rejected")
    ticket(id=8, status="confirmed", mode="live", order_tag="kt8xpre", submission_state="unknown",
           submitted_at="2026-09-25 04:10:00")
    for tid, event, detail in ((1, "executed", "order_id=O-OPEN"), (3, "executed", "exit filled"),
                               (6, "risk_rejected", "max_open_positions"), (8, "submission_unknown", "timeout")):
        conn.execute("INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, ?, ?)", (tid, event, detail))
    conn.execute(
        "INSERT INTO closed_trades (buy_ticket_id, sell_ticket_id, account_id, symbol, strategy_name, source, mode, "
        "entry_price, exit_price, quantity, realized_pnl, realized_pnl_pct, entry_date, exit_date, "
        "holding_period_calendar_days, holding_period_trading_days, exit_reason) VALUES (2, 3, 1, 'INFY', NULL, "
        "'manual', 'live', 1500, 1600, 7, 700, 6.67, '2026-09-22', '2026-09-24', 2, 2, 'manual')")
    for event, detail in (("mode_switched", "dry_run -> live"), ("account_safety_refusal", "TRADING_LOCKED"),
                          ("trading_locked", "pre-PK-22 lock")):
        conn.execute("INSERT INTO system_events (account_id, event_type, detail) VALUES (1, ?, ?)", (event, detail))
    conn.execute("INSERT INTO reconciliation_log (check_type, expected, actual, detail, issue_code, severity, symbol, "
                 "expected_quantity, broker_quantity) VALUES ('position_state', '5 shares', '3 shares', 'shortfall', "
                 "'BROKER_POSITION_SHORTFALL', 'urgent', 'TCS', 5, 3)")
    conn.execute("INSERT INTO reconciliation_log (ticket_id, check_type, expected, actual, detail, resolved) "
                 "VALUES (1, 'order_status', 'COMPLETE', 'COMPLETE', 'reviewed earlier', 1)")
    conn.execute("INSERT INTO broker_failure_log (account_id, source, detail) VALUES (1, 'order_submission', 'timeout')")
    conn.execute("INSERT INTO paper_orders (paper_order_id, account_id, ticket_id, tag, variety, exchange, "
                 "tradingsymbol, transaction_type, product, order_type, quantity, filled_quantity, pending_quantity, "
                 "price, average_price, status, session_date) VALUES ('PAPER-O-1', 1, 4, 'kt4xpre', 'regular', "
                 "'NSE', 'RELIANCE', 'BUY', 'CNC', 'LIMIT', 10, 10, 0, 1300, 1300.65, 'COMPLETE', '2026-09-23')")
    conn.execute("INSERT INTO dashboard_panels (account_id, panel_type, display_order) VALUES (1, 'holdings_table', 0)")
    conn.execute("INSERT INTO dashboard_panels (account_id, panel_type, display_order, visible) "
                 "VALUES (1, 'positions_table', 1, 0)")
    conn.execute("INSERT INTO strategy_configs (account_id, strategy_name, enabled, params) "
                 "VALUES (1, 'momentum_breakout', 1, '{\"lookback\": 20}')")
    for symbol, token in (("TCS", 2953217), ("INFY", 408065)):
        conn.execute("INSERT INTO watchlist (account_id, symbol, instrument_token, tag) VALUES (1, ?, ?, 'core')",
                     (symbol, token))
    for symbol, token, split in (("TCS", 2953217, False), ("INFY", 408065, True)):
        for i, c in enumerate(make_candles(PIN_SESSION, 40, 1000.0)):
            factor = 0.5 if split and i >= 20 else 1.0  # INFY: a 1:2 split, never back-adjusted
            conn.execute("INSERT INTO ohlc_daily (instrument_token, symbol, date, open, high, low, close, volume) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (token, symbol, c["date"].strftime("%Y-%m-%d"), c["open"] * factor, c["high"] * factor,
                          c["low"] * factor, c["close"] * factor, c["volume"]))


def _snapshot(path) -> dict:
    """{table: (columns, rows keyed by rowid)} for every table."""
    conn = sqlite3.connect(path)
    try:
        out = {}
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            select = ", ".join(f'"{c}"' for c in columns)
            out[table] = (columns, {r[0]: r[1:] for r in conn.execute(f'SELECT rowid, {select} FROM "{table}"')})
        return out
    finally:
        conn.close()


def test_a_synthetic_pre_pk22_database_migrates_keeping_every_row(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from backend import crypto, db, lifecycle, live_preflight, live_arming, risk_engine
    monkeypatch.setenv("KITE_TERMINAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("KITE_API_KEY", "synthetic_key")
    monkeypatch.setenv("KITE_API_SECRET", "synthetic_secret")
    monkeypatch.setattr(crypto, "ENV_PATH", tmp_path / ".env")
    path = tmp_path / "pre_pk22.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    conn = sqlite3.connect(path)
    conn.executescript(PRE_PK22_SCHEMA.read_text(encoding="utf-8"))
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'schema_version'").fetchone()[0] == 0
    _seed_pre_pk22_rows(conn, crypto)
    conn.commit()
    conn.close()
    before = _snapshot(path)

    db.init_db()
    db.init_db()  # a second start changes nothing
    assert db.last_init["applied_migrations"] == [] and db.last_init["startup_row_changes"] == 0
    after = _snapshot(path)

    # Every pre-existing table survives with every pre-existing row unchanged
    # in its original columns. Only the startup seeds may add rows.
    for table, (columns, rows) in before.items():
        assert table in after, f"table {table} disappeared"
        new_columns, new_rows = after[table]
        index = [new_columns.index(c) for c in columns]
        kept = {rowid: tuple(row[i] for i in index) for rowid, row in new_rows.items() if rowid in rows}
        assert kept == rows, f"{table}: pre-existing rows changed or were lost"
        if table not in _SEEDED_BY_STARTUP:
            assert set(new_rows) == set(rows), f"{table}: unexpected rows added by the migration"
    assert len(before["trade_tickets"][1]) == 8 and len(before["ohlc_daily"][1]) == 80

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        assert [r[0] for r in conn.execute("SELECT version FROM schema_version")] == [v for v, _, _ in db.MIGRATIONS]
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_dashboard_panels_account_type" not in names                   # v2
        assert "idx_dashboard_panels_account_screen_type" in names
        assert {"idx_trade_tickets_closes_ticket", "idx_trade_tickets_account_mode_status",
                "idx_trade_tickets_kite_order", "idx_trade_tickets_kite_gtt"} <= names    # v3
        statuses = {r["symbol"]: (r["status"], r["bar_count"]) for r in conn.execute(
            "SELECT symbol, status, bar_count FROM ohlc_history_status")}            # v4
        assert statuses["INFY"] == ("SUSPECT", 40) and statuses["TCS"][1] == 40
        assert conn.execute("SELECT COUNT(*) FROM live_readiness").fetchone()[0] == 0   # v5: no IP is assumed
        settings = conn.execute("SELECT mode, live_armed_session_id FROM execution_settings").fetchone()
        assert (settings["mode"], settings["live_armed_session_id"]) == ("live", None)  # v6: never armed by a migration
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'system_events'").fetchone()[0]
        assert "'reconciliation_reviewed'" in sql and "'live_switch_refused'" in sql
        columns = {r[1] for r in conn.execute("PRAGMA table_info(trade_tickets)")}
        assert {"reviewed_at", "reviewed_session_id", "review_digest"} <= columns         # v7
        assert "review_generation" in columns                                             # v8
        assert conn.execute("SELECT COUNT(*) FROM trade_tickets WHERE reviewed_at IS NOT NULL OR "
                            "reviewed_session_id IS NOT NULL OR review_digest IS NOT NULL OR "
                            "review_generation IS NOT NULL").fetchone()[0] == 0
        assert crypto.decrypt(conn.execute("SELECT access_token_enc FROM accounts").fetchone()[0]) == \
            "synthetic-access-token"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

    # The data still MEANS the same to the running app.
    conn = db.get_connection()
    try:
        assert [p["id"] for p in risk_engine.list_open_positions(conn, 1)] == [1]   # 2 closed, 4 is dry run
        ctx = lifecycle.build_context(conn, 1)
        assert lifecycle.derive(ctx.tickets[1], ctx)["lifecycle_state"] == lifecycle.OPEN
        assert lifecycle.derive(ctx.tickets[2], ctx)["lifecycle_state"] == lifecycle.CLOSED
        assert [u["id"] for u in live_preflight.unresolved_live_submissions(conn, 1)] == [8]
        assert live_preflight._critical_findings_check(conn)["status"] == live_preflight.FAIL   # still blocks live
        assert not live_arming.live_armed_for_active_session(conn, 1)
        assert conn.execute("SELECT trading_locked FROM account_safety_state").fetchone()[0] == 1
    finally:
        conn.close()

    # The automatic pre-migration backup is the database exactly as it was.
    [backup] = tmp_path.glob(f"pre_pk22.db.pre_schema_v{db.LATEST_SCHEMA_VERSION}_bak*")
    assert _snapshot(backup) == before


# ============================================================ PK-22B indexes / idempotency

def test_fresh_database_never_recreates_the_legacy_dashboard_index(env):
    for _ in range(2):
        env.db.init_db()
        names = {r["name"] for r in _rows(env, "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_dashboard_panels_account_type" not in names
        assert "idx_dashboard_panels_account_screen_type" in names


def test_restarted_database_carrying_the_legacy_index_loses_it(env):
    _exec(env, "CREATE UNIQUE INDEX idx_dashboard_panels_account_type ON dashboard_panels(account_id, panel_type)")
    as_pre_pk22_database(env.db)
    env.db.init_db()
    names = {r["name"] for r in _rows(env, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_dashboard_panels_account_type" not in names
    # the same panel type may now exist on two screens
    acct = account_id(env.db)
    _exec(env, "INSERT INTO dashboard_panels (account_id, screen, panel_type, display_order) "
               "VALUES (?, 'watchlist', 'holdings_table', 9)", (acct,))


@pytest.mark.parametrize("query, index", [
    ("SELECT id FROM trade_tickets WHERE closes_ticket_id = 1", "idx_trade_tickets_closes_ticket"),
    ("SELECT id FROM trade_tickets WHERE kite_order_id = 'x'", "idx_trade_tickets_kite_order"),
    ("SELECT id FROM trade_tickets WHERE kite_gtt_id = 1", "idx_trade_tickets_kite_gtt"),
    ("SELECT id FROM trade_tickets WHERE account_id = 1 AND mode = 'live' AND status = 'executed'",
     "idx_trade_tickets_account_mode_status"),
])
def test_hot_queries_use_their_index(env, query, index):
    plan = " ".join(r["detail"] for r in _rows(env, f"EXPLAIN QUERY PLAN {query}"))
    assert index in plan, plan


# ============================================================ PK-22D historical data integrity

TOKEN, SYMBOL = 408065, "INFY"


@pytest.fixture
def data_env(env, monkeypatch):
    from backend import config, market
    monkeypatch.setattr(market, "last_complete_session_date", lambda at=None: PIN_SESSION)
    monkeypatch.setattr(config, "HISTORICAL_RATE_LIMIT_DELAY_SECONDS", 0)
    env.kite.candles[TOKEN] = make_candles(PIN_SESSION, 300, 1000.0)
    return env


def _run(coro):
    return asyncio.run(coro)


def _status(env, token=TOKEN):
    rows = _rows(env, "SELECT * FROM ohlc_history_status WHERE instrument_token = ?", (token,))
    return rows[0] if rows else None


def _adjust(candles, factor, before: date):
    return [{**c, **{k: round(c[k] * factor, 2) for k in ("open", "high", "low", "close")}}
            if c["date"].date() < before else dict(c) for c in candles]


def test_detect_back_adjustment_rules():
    from backend.ohlc_integrity import detect_back_adjustment as detect
    d = [datetime(2026, 9, day) for day in (21, 22, 23, 24, 25)]
    stored = {x.strftime("%Y-%m-%d"): 100.0 for x in d}
    halved = [{"date": x, "close": 50.0} for x in d]
    assert detect(stored, halved)["ratio"] == 0.5                                   # consistent split factor
    assert detect(stored, [{"date": d[0], "close": 50.0}]) is None                  # one date is not proof
    assert detect(stored, [{"date": d[4], "close": 50.0}, {"date": d[3], "close": 100}]) is None  # newest ignored
    assert detect(stored, [{"date": d[0], "close": 50.0}, {"date": d[1], "close": 80.0}]) is None  # no single factor
    assert detect(stored, [{"date": x, "close": 101.0} for x in d]) is None        # within tolerance


def test_normal_refresh_keeps_history_ok(data_env):
    from backend import ohlc
    _run(ohlc.refresh_latest(data_env.kite, TOKEN, SYMBOL))            # first: full backfill
    _run(ohlc.refresh_latest(data_env.kite, TOKEN, SYMBOL))            # then an ordinary overlap refresh
    status = _status(data_env)
    assert status["status"] == "OK" and status["bar_count"] == 300
    assert status["data_hash"] and status["downloaded_at"] and status["refreshed_at"]


def test_back_adjusted_provider_history_marks_needs_refresh(data_env):
    from backend import ohlc
    _run(ohlc.refresh_latest(data_env.kite, TOKEN, SYMBOL))
    # Kite back-adjusts everything before the split day (1:2); ours is now stale.
    data_env.kite.candles[TOKEN] = _adjust(data_env.kite.candles[TOKEN], 0.5, date(2026, 9, 25))
    _run(ohlc.refresh_latest(data_env.kite, TOKEN, SYMBOL))
    status = _status(data_env)
    assert status["status"] == "NEEDS_REFRESH"
    reason = json.loads(status["status_reason"])
    assert reason["code"] == "PROVIDER_HISTORY_ADJUSTED" and reason["ratio"] == pytest.approx(0.5, abs=0.01)


def test_a_revised_latest_close_is_not_mistaken_for_an_adjustment(data_env):
    from backend import ohlc
    _run(ohlc.refresh_latest(data_env.kite, TOKEN, SYMBOL))
    last = data_env.kite.candles[TOKEN][-1]
    last["close"] = round(last["close"] * 1.03, 2)          # only the newest stored candle changed
    _run(ohlc.refresh_latest(data_env.kite, TOKEN, SYMBOL))
    assert _status(data_env)["status"] == "OK"


def test_rebuild_replaces_the_series_atomically_and_clears_the_flag(data_env):
    from backend import ohlc
    _run(ohlc.refresh_latest(data_env.kite, TOKEN, SYMBOL))
    data_env.kite.candles[TOKEN] = _adjust(data_env.kite.candles[TOKEN], 0.5, date(2026, 9, 25))
    _run(ohlc.refresh_latest(data_env.kite, TOKEN, SYMBOL))
    assert _status(data_env)["status"] == "NEEDS_REFRESH"

    # A failed download leaves everything as it was.
    before = _rows(data_env, "SELECT date, close FROM ohlc_daily WHERE instrument_token = ? ORDER BY date", (TOKEN,))
    data_env.kite.raise_on_next("historical_data", RuntimeError("provider down"))
    with pytest.raises(RuntimeError):
        _run(ohlc.rebuild_history(data_env.kite, TOKEN, SYMBOL))
    assert _rows(data_env, "SELECT date, close FROM ohlc_daily WHERE instrument_token = ? ORDER BY date",
                 (TOKEN,)) == before
    assert _status(data_env)["status"] == "NEEDS_REFRESH"

    # An empty download is refused too.
    saved = data_env.kite.candles[TOKEN]
    data_env.kite.candles[TOKEN] = []
    with pytest.raises(ohlc.HistoryRebuildError):
        _run(ohlc.rebuild_history(data_env.kite, TOKEN, SYMBOL))
    data_env.kite.candles[TOKEN] = saved

    _run(ohlc.rebuild_history(data_env.kite, TOKEN, SYMBOL))
    after = {r["date"]: r["close"] for r in _rows(
        data_env, "SELECT date, close FROM ohlc_daily WHERE instrument_token = ?", (TOKEN,))}
    assert after == {c["date"].strftime("%Y-%m-%d"): c["close"] for c in saved}      # exactly the provider's series
    status = _status(data_env)
    assert status["status"] == "OK" and json.loads(status["status_reason"])["code"] == "REBUILT_FROM_PROVIDER"


def test_an_unverified_gap_is_suspect_until_a_rebuild_confirms_it(data_env):
    from backend import ohlc
    crash = [dict(c) for c in data_env.kite.candles[TOKEN]]
    for c in crash[-20:]:
        for k in ("open", "high", "low", "close"):
            c[k] = round(c[k] * 0.6, 2)   # a real 40% crash the provider also shows
    data_env.kite.candles[TOKEN] = crash
    _run(ohlc.refresh_latest(data_env.kite, TOKEN, SYMBOL))
    assert _status(data_env)["status"] == "SUSPECT"
    _run(ohlc.rebuild_history(data_env.kite, TOKEN, SYMBOL))
    assert _status(data_env)["status"] == "OK"
    assert json.loads(_status(data_env)["verified_gaps"])                       # remembered as real
    _run(ohlc.refresh_latest(data_env.kite, TOKEN, SYMBOL))
    assert _status(data_env)["status"] == "OK"                                   # not re-flagged


def test_a_symbol_change_is_recorded_not_merged(data_env):
    from backend import ohlc
    _run(ohlc.refresh_latest(data_env.kite, TOKEN, "OLDNAME"))
    _run(ohlc.refresh_latest(data_env.kite, TOKEN, SYMBOL))
    transitions = _rows(data_env, "SELECT instrument_token, old_symbol, new_symbol FROM symbol_transitions")
    assert transitions == [{"instrument_token": TOKEN, "old_symbol": "OLDNAME", "new_symbol": SYMBOL}]
    # Another token's series is never touched by it.
    assert not _rows(data_env, "SELECT 1 FROM ohlc_daily WHERE instrument_token != ?", (TOKEN,))


def _flag(env, token, symbol, status="NEEDS_REFRESH"):
    _exec(env, "INSERT INTO ohlc_history_status (instrument_token, symbol, status, status_reason, status_at) "
               "VALUES (?, ?, ?, ?, '2026-09-25 10:00:00') ON CONFLICT(instrument_token) DO UPDATE SET "
               "status = excluded.status, status_reason = excluded.status_reason",
          (token, symbol, status, json.dumps({"code": "PROVIDER_HISTORY_ADJUSTED", "ratio": 0.5})))


def test_backtest_excludes_flagged_history_and_says_why(env):
    from backend import backtest
    from backend.backtest import BacktestConfig
    from backend.strategies import STRATEGIES
    for token, sym in ((408065, "INFY"), (2953217, "TCS")):
        seed_ohlc(env.db, token, sym, make_candles(date(2026, 6, 30), 400, 1000.0))
    _flag(env, 408065, "INFY")
    conn = env.db.get_connection()
    try:
        result = backtest.run_backtest(conn, "momentum_breakout", [(408065, "INFY"), (2953217, "TCS")],
                                       dict(STRATEGIES["momentum_breakout"].DEFAULT_PARAMS),
                                       "2025-09-01", "2026-06-30", cfg=BacktestConfig.from_overrides())
    finally:
        conn.close()
    [excluded] = result["universe"]["excluded_symbols"]
    assert excluded["symbol"] == "INFY" and excluded["status"] == "NEEDS_REFRESH"
    assert excluded["reason"]["code"] == "PROVIDER_HISTORY_ADJUSTED"
    assert any("INFY EXCLUDED" in w for w in result["warnings"])
    assert all(t["symbol"] != "INFY" for t in result["trades"])


def test_live_scan_skips_symbols_whose_history_needs_refresh(env, monkeypatch):
    from backend import market, strategy_engine
    monkeypatch.setattr(market, "last_complete_session_date", lambda at=None: date(2026, 9, 25))
    from backend.accounts import get_default_account
    from conftest import add_watch
    add_watch(env.db, "INFY", 408065)
    add_watch(env.db, "TCS", 2953217)
    for token, sym in ((408065, "INFY"), (2953217, "TCS")):
        seed_ohlc(env.db, token, sym, make_candles(date(2026, 9, 25), 260, 1000.0))
    _flag(env, 408065, "INFY")
    seen = []

    class Probe:
        STRATEGY_NAME = "probe"

        @staticmethod
        def evaluate_entry(df, params):
            seen.append(len(df))
            return {"passed": False}

        @staticmethod
        def rank_key(c):
            return 0

    diag = {}
    conn = env.db.get_connection()
    try:
        strategy_engine.run_entry_scan(Probe, conn, get_default_account(conn), env.kite,
                                       {"max_signals_per_day": 3, "trend_filter_period": 2}, diagnostics=diag)
    finally:
        conn.close()
    assert diag["history_needs_refresh"] == ["INFY"]
    assert len(seen) == 1 and diag["evaluated"] == 1   # only TCS was evaluated; INFY never reached the strategy


def test_history_health_api_and_manual_rebuild_route(data_env):
    from backend import ohlc
    _run(ohlc.refresh_latest(data_env.kite, TOKEN, SYMBOL))
    r = data_env.client.get("/api/data/history-health")
    assert r.status_code == 200 and r.json()["data"]["counts"]["OK"] == 1
    r = data_env.client.post("/api/data/history/999999/rebuild")
    assert r.status_code == 404 and r.json()["detail"]["error_code"] == "UNKNOWN_INSTRUMENT"
    r = data_env.client.post(f"/api/data/history/{TOKEN}/rebuild")
    assert r.status_code == 202 and r.json()["data"]["symbol"] == SYMBOL


def test_data_job_queue_dedupes_pending_jobs():
    from backend import data_jobs

    async def scenario():
        first = await data_jobs.enqueue_refresh(1, "A")
        again = await data_jobs.enqueue_refresh(1, "A")
        other = await data_jobs.enqueue_rebuild(1, "A")
        return first, again, other, data_jobs.pending_jobs()

    assert asyncio.run(scenario()) == (True, False, True, 2)


# ============================================================ PK-22E logging / observability

@pytest.fixture
def log_file(tmp_path, monkeypatch):
    """A real rotating file handler with the redaction filter installed on it."""
    from backend import config, obs
    from backend.redaction import install_log_redaction
    monkeypatch.setattr(config, "LOG_FILE_MAX_BYTES", 4000)
    monkeypatch.setattr(config, "LOG_FILE_BACKUP_COUNT", 2)
    path = tmp_path / "logs" / "kt.log"
    handler = obs.install_file_logging(str(path))
    install_log_redaction()
    yield path, handler
    for name in ("", "uvicorn", "uvicorn.access"):
        logging.getLogger(name).removeHandler(handler)
    handler.close()


def test_structured_events_are_stable_and_redacted_in_the_log_file(log_file):
    from backend import obs
    from backend.redaction import register_secret
    path, handler = log_file
    register_secret("tok_SECRET_value_123456")
    logger = logging.getLogger("backend.test_pk22")
    logger.setLevel(logging.INFO)
    with obs.correlation("rq-test00000001"):
        obs.log_event(logger, "order.result", account_id=1, ticket_id=42, symbol="INFY", mode="live",
                      order_id="250000000001", access_token="tok_SECRET_value_123456", note="has space")
        try:
            raise RuntimeError("broker said access_token=tok_SECRET_value_123456")
        except RuntimeError:
            logger.exception("event=order.failed ticket_id=42")
    handler.flush()
    text = path.read_text(encoding="utf-8")
    assert "tok_SECRET_value_123456" not in text
    line = next(ln for ln in text.splitlines() if "event=order.result" in ln)
    assert "account_id=1 ticket_id=42 symbol=INFY mode=live order_id=250000000001" in line
    assert 'note="has space"' in line and "cid=rq-test00000001" in line
    assert "[rq-test00000001]" in line                                   # the formatter's cid field too


def test_log_file_rotation_is_bounded(log_file):
    path, handler = log_file
    logger = logging.getLogger("backend.test_pk22_rotation")
    for i in range(400):
        logger.warning("line %04d %s", i, "x" * 60)
    handler.flush()
    files = sorted(path.parent.glob("kt.log*"))
    assert 1 < len(files) <= 3                                           # the file + at most 2 backups
    assert all(f.stat().st_size <= 4000 + 200 for f in files)


def test_launcher_enables_the_log_file_by_default(monkeypatch):
    import dotenv
    import uvicorn
    from backend import server
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    for name in ("HOST", "PORT", "ALLOW_REMOTE_BIND", "DEV_RELOAD", "WORKERS", "TRUSTED_PROXY_IPS"):
        monkeypatch.delenv(f"KITE_TERMINAL_{name}", raising=False)
    for name in ("HOST", "PORT", "RELOAD", "WORKERS", "TRUSTED_PROXY_IPS"):
        monkeypatch.setenv(server.RUNTIME_PREFIX + name, "placeholder")
    monkeypatch.setenv("KITE_TERMINAL_LOG_FILE", "placeholder")
    monkeypatch.delenv("KITE_TERMINAL_LOG_FILE")
    import os
    assert server.main([]) == 0
    assert os.environ["KITE_TERMINAL_LOG_FILE"].replace("\\", "/").endswith("logs/kite_terminal.log")


def test_request_ids_are_issued_and_only_safe_ones_accepted(env):
    r = env.client.get("/api/health")
    assert r.status_code == 200 and r.headers["x-request-id"].startswith("rq-")
    r = env.client.get("/api/health", headers={"X-Request-ID": "client-trace-0001"})
    assert r.headers["x-request-id"] == "client-trace-0001"
    r = env.client.get("/api/health", headers={"X-Request-ID": "bad id\\u0000 with spaces"})
    assert r.headers["x-request-id"].startswith("rq-")
    # a refused request carries one too
    r = env.client.get("/api/health", headers={"Host": "evil.example"})
    assert r.status_code == 400 and r.headers["x-request-id"].startswith("rq-")


def test_confirm_flow_is_traceable_by_one_correlation_id(env, market_open, caplog):
    set_mode(env.db, "live")
    body = {"symbol": "INFY", "exchange": "NSE", "transaction_type": "BUY", "quantity": 5, "entry_price": 1500.0,
            "entry_order_type": "LIMIT", "stop_loss": 1450.0, "target_1": 1600.0, "product_type": "CNC",
            "rationale": "PK-22 trace"}
    tid = env.client.post("/api/tickets", json=body).json()["data"]["id"]
    caplog.set_level(logging.INFO)
    r = env.client.post(f"/api/tickets/{tid}/confirm", json=approval_body(env.db, tid, expected_mode="live"))
    assert r.status_code == 200, r.text
    cid = r.headers["x-request-id"]
    events = {rec.kt_event: rec for rec in caplog.records if hasattr(rec, "kt_event")}
    for name in ("approval.claimed", "order.submitting", "order.result"):
        assert name in events, sorted(events)
        assert events[name].kt_fields["ticket_id"] == tid
        assert f"cid={cid}" in events[name].getMessage()
    tag = ticket_row(env.db, tid)["order_tag"]
    assert events["approval.claimed"].kt_fields["order_tag"] == tag == events["order.result"].kt_fields["order_tag"]
    order_id = events["order.result"].kt_fields["order_id"]
    assert order_id == ticket_row(env.db, tid)["kite_order_id"]
    # The monitor's own cycle later reports the same ticket and order id.
    from backend import obs, order_monitor
    env.kite.auto_fill = True
    with obs.correlation("om-7"):
        order_monitor.check_pending_orders(kite=env.kite)
    resolved = [rec for rec in caplog.records if getattr(rec, "kt_event", None) == "order.resolved"]
    assert resolved and resolved[-1].kt_fields["ticket_id"] == tid and resolved[-1].kt_fields["order_id"] == order_id
    assert "cid=om-7" in resolved[-1].getMessage()


# ============================================================ PK-22F workers / queues

def _pending_ticket(env, symbol, order_id):
    conn = env.db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO trade_tickets (account_id, source, symbol, exchange, instrument_token, transaction_type, "
            "quantity, entry_price, entry_order_type, stop_loss, target_1, product_type, rationale, status, mode, "
            "kite_order_id, executed_at, entry_fill_status, filled_quantity, submission_state) VALUES "
            "(?, 'manual', ?, 'NSE', 1, 'BUY', 5, 100, 'LIMIT', 90, 120, 'CNC', 't', 'executed', 'live', ?, "
            "datetime('now'), 'pending', 0, 'acknowledged')",
            (account_id(env.db), symbol, order_id))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_one_bad_ticket_or_malformed_order_does_not_stop_the_monitor_cycle(env, monkeypatch):
    from backend import order_monitor
    bad_order = env.kite.add_broker_order("TCS", "BUY", 5, status="COMPLETE", price=100.0)
    good_order = env.kite.add_broker_order("INFY", "BUY", 5, status="COMPLETE", price=100.0)
    bad = _pending_ticket(env, "TCS", bad_order)
    good = _pending_ticket(env, "INFY", good_order)
    real_orders = env.kite.orders
    monkeypatch.setattr(env.kite, "orders", lambda: [*real_orders(), {"status": "COMPLETE"}, "junk"])
    real_resolve = order_monitor._resolve_entry

    def resolve(conn, kite, ticket, order):
        if ticket["id"] == bad:
            raise KeyError("malformed broker payload")
        return real_resolve(conn, kite, ticket, order)

    monkeypatch.setattr(order_monitor, "_resolve_entry", resolve)
    result = order_monitor.check_pending_orders(kite=env.kite)
    assert result["checked"] == 2
    assert ticket_row(env.db, good)["entry_fill_status"] == "filled"     # processed despite the bad one
    assert ticket_row(env.db, bad)["entry_fill_status"] == "pending"     # rolled back, retried next cycle


def test_worker_recovers_after_repeated_transient_errors(monkeypatch):
    from backend import config, order_monitor, worker_health
    worker_health.reset()
    monkeypatch.setattr(config, "ORDER_MONITOR_INTERVAL_SECONDS", 0)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return {"checked": 0, "updated": 0}

    monkeypatch.setattr(order_monitor, "check_pending_orders", flaky)
    monkeypatch.setattr(order_monitor, "check_paper_orders", lambda: {"checked": 0})

    async def scenario():
        task = asyncio.create_task(order_monitor.run_order_monitor_loop())
        while calls["n"] < 4:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    health = worker_health.snapshot()["order_monitor"]
    assert health["failures"] == 2 and health["consecutive_failures"] == 0
    assert health["last_ok_at"] and "database is locked" in health["last_error"]
    worker_health.reset()


def test_reconciliation_keeps_other_checks_findings_when_one_raises(env, monkeypatch):
    from backend import reconciliation

    def broken(conn, kite, account):
        conn.execute("INSERT INTO reconciliation_log (check_type, detail) VALUES ('order_status', 'half-written')")
        raise KeyError("malformed order history")

    def probe(conn, kite, account, snapshot=None):
        conn.execute("INSERT INTO reconciliation_log (check_type, detail) VALUES ('position_state', 'kept')")

    monkeypatch.setattr(reconciliation, "_check_order_status", broken)
    monkeypatch.setattr(reconciliation, "_check_position_state", probe)
    with pytest.raises(reconciliation.ReconciliationChecksFailed):
        reconciliation._run_cycle()
    details = [r["detail"] for r in _rows(env, "SELECT detail FROM reconciliation_log")]
    assert "kept" in details and "half-written" not in details
    assert reconciliation.cycle_status()["failed_checks"] == ["order_status"]


def test_malformed_broker_rows_are_skipped_by_reconciliation():
    from backend.reconciliation import BrokerSnapshot
    kite = FakeKite(holdings=[{"tradingsymbol": "INFY", "quantity": 5}, {"quantity": 9}, "junk"])
    kite.get_gtts = lambda: [{"id": 1, "status": "active"}, {"status": "active"}, "junk"]
    snap = BrokerSnapshot()
    snap.load_gtts(kite)
    snap.load_held(kite)
    assert [g["id"] for g in snap.gtts] == [1]
    assert snap.held == {"INFY": 5}


def test_trailing_live_failure_does_not_skip_paper_positions(env, monkeypatch):
    from backend import paper_broker, trailing_engine
    monkeypatch.setattr(trailing_engine, "monitored_positions",
                        lambda conn, acct, mode="live": [{"id": 1, "mode": mode}])
    seen = []

    def tick_mode(conn, kite, tickets):
        seen.append("paper" if paper_broker.is_paper_broker(kite) else "live")
        if seen[-1] == "live":
            raise RuntimeError("live GTT book unreadable")

    monkeypatch.setattr(trailing_engine, "_tick_mode", tick_mode)
    with pytest.raises(RuntimeError, match="unreadable"):
        trailing_engine._tick_sync()
    assert seen == ["live", "paper"]


def test_ticker_skips_malformed_ticks(monkeypatch):
    from backend import ticker
    captured = {}

    class FakeTicker:
        MODE_QUOTE = "quote"

        def __init__(self, *a):
            captured["ws"] = self

        def connect(self, threaded=True):
            pass

        def close(self):
            pass

    monkeypatch.setattr(ticker, "KiteTicker", FakeTicker)
    tm = ticker.TickerManager()
    loop = asyncio.new_event_loop()
    try:
        tm.set_loop(loop)
        got = []

        class Sink:
            def put_nowait(self, m):
                got.append(m)

        tm.add_listener(Sink())
        tm.start("k", "t", [1])
        ws = captured["ws"]
        ws.on_ticks(ws, [{"instrument_token": 1, "last_price": 10.0}, {"last_price": 3}, "junk",
                         {"instrument_token": 2, "last_price": 5.0, "ohlc": None}])
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
    tm.stop()
    assert got == [{"type": "ticks", "data": [
        {"instrument_token": 1, "last_price": 10.0, "close_price": None},
        {"instrument_token": 2, "last_price": 5.0, "close_price": None}]}]
    tm._broadcast({"type": "tickets_changed"})   # a closed loop never raises into the caller


def test_supervisor_never_duplicates_and_stops_everything():
    from backend import worker_health
    from backend.workers import Supervisor
    worker_health.reset()

    async def forever():
        await asyncio.sleep(3600)

    async def crashes():
        raise ValueError("boom")

    async def scenario():
        sup = Supervisor()
        loops = {"order_monitor": forever, "trailing": forever, "crasher": crashes}
        assert sorted(sup.start(loops)) == ["crasher", "order_monitor", "trailing"]
        first = dict(sup._loops)
        await asyncio.sleep(0.01)   # the crasher runs, raises, and its done-callback fires
        crashed = worker_health.snapshot()["crasher"]
        assert crashed["task_state"] == "crashed" and "boom" in crashed["last_error"]
        assert sup.start(loops) == ["crasher"]                     # only the dead one restarts
        assert sup._loops["order_monitor"] is first["order_monitor"]
        one_off = sup.spawn("after_login", forever())
        stopped = await sup.stop(timeout=2)
        assert not stopped["timed_out"]
        assert all(t.done() for t in first.values()) and one_off.cancelled()
        assert not sup.is_running("order_monitor")
        assert sup.start({"order_monitor": forever}) == ["order_monitor"]   # a restart after stop works
        await sup.stop(timeout=2)

    asyncio.run(scenario())
    assert worker_health.snapshot()["order_monitor"]["task_state"] == "stopped"
    worker_health.reset()


def test_app_startup_twice_starts_one_set_of_workers_and_shutdown_stops_them(env, monkeypatch):
    from backend import main, server, workers
    # The real supervisor, not the SEC-1 "everything is running" seed.
    monkeypatch.setattr(workers.supervisor, "is_running", type(workers.supervisor).is_running.__get__(workers.supervisor))
    started = []

    def make(name):
        async def loop():
            started.append(name)
            await asyncio.sleep(3600)
        return loop

    monkeypatch.setattr(main, "_BACKGROUND_LOOPS", {n: make(n) for n in ("order_monitor", "trailing")})
    monkeypatch.setattr(server, "enforce_runtime_policy", lambda *a, **k: None)
    monkeypatch.setattr(main.automation, "is_due", lambda at=None: True)

    async def scenario():
        await main.on_startup()
        await main.on_startup()                  # e.g. a reload in the same process
        await asyncio.sleep(0.01)
        assert sorted(started) == ["order_monitor", "trailing"]
        assert workers.supervisor.is_running("order_monitor")
        await main.on_shutdown()
        assert not workers.supervisor.is_running("order_monitor")
        assert not workers.supervisor.is_running("trailing")

    asyncio.run(scenario())


def test_tick_queue_is_bounded_and_keeps_the_latest_tick():
    from backend.tick_queue import ClientQueue
    q = ClientQueue(max_events=10, max_tokens=50)
    for i in range(100_000):
        q.put_nowait({"type": "ticks", "data": [{"instrument_token": i % 50, "last_price": float(i)}]})
    assert q.pending_ticks() == 50                                     # bounded by instruments, not ticks
    message = asyncio.run(q.get())
    latest = {t["instrument_token"]: t["last_price"] for t in message["data"]}
    assert latest[0] == 99_950.0 and latest[49] == 99_999.0           # the newest price per instrument
    assert q.pending_ticks() == 0
    many = ClientQueue(max_events=10, max_tokens=3)
    for token in range(10):
        many.put_nowait({"type": "ticks", "data": [{"instrument_token": token, "last_price": 1.0}]})
    assert many.pending_ticks() == 3 and many.dropped_instruments == 7


def test_tick_queue_events_are_ordered_first_and_never_dropped_silently():
    from backend.tick_queue import ClientQueue, QueueOverflow
    q = ClientQueue(max_events=3, max_tokens=10)
    q.put_nowait({"type": "ticks", "data": [{"instrument_token": 1, "last_price": 1.0}]})
    q.put_nowait({"type": "order_update", "n": 1})
    q.put_nowait({"type": "tickets_changed", "n": 2})

    async def drain(n):
        return [await q.get() for _ in range(n)]

    assert [m["type"] for m in asyncio.run(drain(3))] == ["order_update", "tickets_changed", "ticks"]
    for n in range(4):
        q.put_nowait({"type": "order_update", "n": n})
    assert q.overflowed
    with pytest.raises(QueueOverflow):
        asyncio.run(q.get())                                            # the socket is closed, not an event lost


def test_websocket_overflow_closes_the_socket_with_1013(env, monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    from backend.routes import ws
    from backend.tick_queue import ClientQueue

    def overflowing():
        q = ClientQueue(max_events=1)
        q.put_nowait({"type": "order_update"})
        q.put_nowait({"type": "order_update"})
        return q

    monkeypatch.setattr(ws, "ClientQueue", overflowing)
    with env.client.websocket_connect("ws://127.0.0.1:8000/ws/ticks") as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == 1013


# ============================================================ PK-22G SQLite access

def test_connections_wait_for_a_busy_writer_instead_of_failing(env):
    from backend import config
    conn = env.db.get_connection()
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == int(config.SQLITE_BUSY_TIMEOUT_SECONDS * 1000)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()
    holder_ready = threading.Event()

    def hold_lock():
        holder = env.db.get_connection()
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE execution_settings SET mode = 'dry_run'")
        holder_ready.set()
        time.sleep(0.4)
        holder.commit()
        holder.close()

    thread = threading.Thread(target=hold_lock)
    thread.start()
    holder_ready.wait(5)
    set_mode(env.db, "live")          # would raise "database is locked" without the busy wait
    thread.join()
    assert _rows(env, "SELECT mode FROM execution_settings")[0]["mode"] == "live"


def test_maintenance_cli_reports_checks_and_backs_up(env, capsys):
    from backend import db_maintenance
    assert db_maintenance.main(["status", "--db", str(_db_file(env))]) == 0
    assert "schema:    v9 of v9" in capsys.readouterr().out
    assert db_maintenance.main(["integrity", "--db", str(_db_file(env))]) == 0
    assert "integrity_check: ok" in capsys.readouterr().out
    assert db_maintenance.main(["backup", "--db", str(_db_file(env))]) == 0
    [backup] = list(_db_file(env).parent.glob("test.db.manual_*_bak"))
    assert sqlite3.connect(backup).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db_maintenance.main(["analyze", "--db", str(_db_file(env))]) == 0


# ============================================================ PK-22H diagnostics

SENTINELS = ("sentinel-access-token-7f3a91", "sentinel-api-secret-19bd44", "sentinel-request-token-5c2e08")


def _make_ready(env, monkeypatch):
    from backend import crypto, workers
    _exec(env, "UPDATE accounts SET access_token_enc = ?", (crypto.encrypt(SENTINELS[0]),))
    _exec(env, "UPDATE sessions SET login_time = ?", (datetime.now(IST).isoformat(),))
    monkeypatch.setattr(workers.supervisor, "is_running", lambda name: True)


def test_health_is_always_up(env):
    r = env.client.get("/api/health")
    assert r.status_code == 200 and r.json()["data"]["status"] == "ok" and r.json()["data"]["database"] == "ok"


def test_readiness_reports_each_blocker_and_does_not_need_an_open_market(env, monkeypatch):
    from backend import account_safety, diagnostics
    from conftest import clear_healthy_live_state
    clear_healthy_live_state(monkeypatch)  # a fresh process: no worker runs (env seeds healthy ones, SEC-1)
    r = env.client.get("/api/ready")
    assert r.status_code == 503
    codes = {b["code"] for b in r.json()["data"]["blockers"]}
    assert {"WORKER_NOT_RUNNING:order_monitor", "WORKER_NOT_RUNNING:trailing",
            "WORKER_NOT_RUNNING:reconciliation"} <= codes              # no workers run in tests

    _make_ready(env, monkeypatch)
    r = env.client.get("/api/ready")
    assert r.status_code == 200 and r.json()["data"]["ready"] is True, r.json()
    assert r.json()["data"]["market_open_required"] is False

    conn = env.db.get_connection()
    try:
        account_safety.set_trading_lock(conn, account_id(env.db), True, "test")
    finally:
        conn.close()
    assert "TRADING_LOCKED" in {b["code"] for b in env.client.get("/api/ready").json()["data"]["blockers"]}
    _exec(env, "UPDATE account_safety_state SET trading_locked = 0, breaker_open = 1, breaker_reason = 'x'")
    assert "BREAKER_OPEN" in {b["code"] for b in env.client.get("/api/ready").json()["data"]["blockers"]}
    _exec(env, "UPDATE account_safety_state SET breaker_open = 0")
    _exec(env, "UPDATE sessions SET login_time = '2026-01-02T09:00:00+05:30'")
    assert "BROKER_SESSION_STALE" in {b["code"] for b in env.client.get("/api/ready").json()["data"]["blockers"]}
    _exec(env, "UPDATE sessions SET login_time = ?", (datetime.now(IST).isoformat(),))
    _exec(env, "DELETE FROM schema_version WHERE version = (SELECT MAX(version) FROM schema_version)")
    assert "SCHEMA_NOT_CURRENT" in {b["code"] for b in env.client.get("/api/ready").json()["data"]["blockers"]}
    monkeypatch.setattr(diagnostics, "integrity_status", lambda refresh=False: {"ok": False, "result": ["corrupt"]})
    assert "DB_INTEGRITY_FAILED" in {b["code"] for b in env.client.get("/api/ready").json()["data"]["blockers"]}


def test_diagnostics_report_is_complete_and_leaks_no_secret(env, monkeypatch):
    from backend import crypto
    from backend.redaction import register_secret
    _make_ready(env, monkeypatch)
    monkeypatch.setenv("KITE_API_SECRET", SENTINELS[1])
    _exec(env, "UPDATE accounts SET api_secret_enc = ?", (crypto.encrypt(SENTINELS[1]),))
    _exec(env, "UPDATE sessions SET public_token = ?", (SENTINELS[2],))
    register_secret(SENTINELS[0])
    r = env.client.get("/api/diagnostics?refresh=true")
    assert r.status_code == 200
    text = r.text
    for secret in (*SENTINELS, "test_api_key", "fake-access-token"):
        assert secret not in text, secret
    data = r.json()["data"]
    assert data["schema"]["current_version"] == 9 and data["database_integrity"]["ok"] is True
    assert data["broker_session"]["state"] == "VALID" and data["broker_session"]["verified_with_broker"] is False
    assert {"order_monitor", "trailing", "reconciliation"} <= set(data["workers"])
    assert data["market"]["session_state"] in ("OPEN", "CLOSED", "PRE_OPEN", "HOLIDAY", "WEEKEND")
    assert "counts" in data["historical_data"] and data["security"]["workers"] == 1
    assert data["app"]["version"] == "live0"
    assert env.kite.calls_to("orders") == [] and env.kite.calls_to("get_gtts") == []   # no broker call
