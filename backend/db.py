import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from backend import config, crypto, installation_state

logger = logging.getLogger(__name__)

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

CREATE TABLE IF NOT EXISTS trade_tickets (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id             INTEGER NOT NULL REFERENCES accounts(id),
    source                 TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'strategy', 'alert_action')),
    strategy_name          TEXT,
    symbol                 TEXT NOT NULL,
    exchange               TEXT NOT NULL DEFAULT 'NSE',
    instrument_token       INTEGER NOT NULL,
    transaction_type       TEXT NOT NULL CHECK (transaction_type IN ('BUY', 'SELL')),
    quantity               INTEGER NOT NULL CHECK (quantity > 0),
    entry_price            REAL NOT NULL CHECK (entry_price > 0),
    entry_order_type       TEXT NOT NULL CHECK (entry_order_type IN ('MARKET', 'LIMIT')),
    stop_loss              REAL NOT NULL CHECK (stop_loss > 0),
    target_1               REAL NOT NULL CHECK (target_1 > 0),
    target_2               REAL,
    trailing_enabled       INTEGER NOT NULL DEFAULT 0,
    trailing_trigger       TEXT CHECK (trailing_trigger IN ('after_target_1', 'manual') OR trailing_trigger IS NULL),
    trailing_distance      REAL CHECK (trailing_distance IS NULL OR trailing_distance > 0),
    current_stop_loss      REAL,
    trailing_active        INTEGER NOT NULL DEFAULT 0,
    product_type           TEXT NOT NULL CHECK (product_type IN ('CNC', 'MIS', 'NRML')),
    use_gtt                INTEGER NOT NULL DEFAULT 1,
    risk_amount            REAL,
    risk_reward_ratio      REAL,
    rationale              TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'draft'
                           CHECK (status IN ('draft', 'pending_confirmation', 'confirmed', 'executed', 'rejected', 'cancelled', 'failed', 'risk_rejected')),
    mode                   TEXT CHECK (mode IN ('dry_run', 'live') OR mode IS NULL),
    kite_order_id          TEXT,
    kite_gtt_id            INTEGER,
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    executed_at            TEXT,
    closed_at              TEXT,
    last_trailing_check_at TEXT,
    closes_ticket_id       INTEGER REFERENCES trade_tickets(id),
    risk_check_results     TEXT,
    archived               INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trade_tickets_account ON trade_tickets(account_id);
CREATE INDEX IF NOT EXISTS idx_trade_tickets_status ON trade_tickets(status);

CREATE TABLE IF NOT EXISTS ticket_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES trade_tickets(id),
    event_type  TEXT NOT NULL CHECK (event_type IN (
                    'created', 'confirmed', 'rejected', 'executed', 'execution_failed',
                    'protection_failed', 'modified', 'trailing_adjusted', 'closed', 'risk_rejected',
                    'archived'
                )),
    detail      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket ON ticket_events(ticket_id);

CREATE TABLE IF NOT EXISTS instrument_cache (
    instrument_token INTEGER PRIMARY KEY,
    tradingsymbol    TEXT NOT NULL,
    name             TEXT,
    exchange         TEXT NOT NULL,
    segment          TEXT,
    instrument_type  TEXT,
    last_price       REAL,
    refreshed_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_instrument_cache_symbol ON instrument_cache(tradingsymbol);

CREATE TABLE IF NOT EXISTS execution_settings (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    mode       TEXT NOT NULL DEFAULT 'dry_run' CHECK (mode IN ('dry_run', 'live')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS risk_settings (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id                    INTEGER NOT NULL REFERENCES accounts(id),
    max_risk_per_trade_pct        REAL NOT NULL DEFAULT 0.5 CHECK (max_risk_per_trade_pct > 0 AND max_risk_per_trade_pct <= 100),
    max_capital_per_trade_pct     REAL NOT NULL DEFAULT 10 CHECK (max_capital_per_trade_pct > 0 AND max_capital_per_trade_pct <= 100),
    max_open_positions            INTEGER NOT NULL DEFAULT 10 CHECK (max_open_positions > 0),
    max_single_stock_exposure_pct REAL NOT NULL DEFAULT 10 CHECK (max_single_stock_exposure_pct > 0 AND max_single_stock_exposure_pct <= 100),
    max_sector_exposure_pct       REAL NOT NULL DEFAULT 20 CHECK (max_sector_exposure_pct > 0 AND max_sector_exposure_pct <= 100),
    max_total_portfolio_risk_pct  REAL NOT NULL DEFAULT 5 CHECK (max_total_portfolio_risk_pct > 0 AND max_total_portfolio_risk_pct <= 100),
    min_risk_reward               REAL NOT NULL DEFAULT 1.5 CHECK (min_risk_reward > 0),
    updated_at                    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_risk_settings_account ON risk_settings(account_id);

CREATE TABLE IF NOT EXISTS sector_mapping (
    symbol   TEXT PRIMARY KEY,
    sector   TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dashboard_panels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    panel_type    TEXT NOT NULL,
    display_order INTEGER NOT NULL,
    size          TEXT NOT NULL DEFAULT 'medium' CHECK (size IN ('small', 'medium', 'large')),
    visible       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Its uniqueness index, (account_id, screen, panel_type), is created by
-- _ensure_dashboard_panels_screen_column once the screen column exists. The
-- original (account_id, panel_type) index is deliberately NOT created here:
-- recreating it on every start undid that migration (PK-22B).

CREATE TABLE IF NOT EXISTS risk_settings_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    field_changed TEXT NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    changed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_risk_settings_history_account ON risk_settings_history(account_id);

CREATE TABLE IF NOT EXISTS watchlist (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       INTEGER NOT NULL REFERENCES accounts(id),
    symbol           TEXT NOT NULL,
    instrument_token INTEGER NOT NULL,
    added_at         TEXT NOT NULL DEFAULT (datetime('now')),
    notes            TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_account_symbol ON watchlist(account_id, symbol);

-- Shared market data, not account-scoped: INFY's OHLC/indicators are the
-- same number regardless of which account holds or watches it.
CREATE TABLE IF NOT EXISTS ohlc_daily (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_token INTEGER NOT NULL,
    symbol           TEXT NOT NULL,
    date             TEXT NOT NULL,
    open             REAL NOT NULL,
    high             REAL NOT NULL,
    low              REAL NOT NULL,
    close            REAL NOT NULL,
    volume           INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ohlc_daily_token_date ON ohlc_daily(instrument_token, date);

-- One current row per instrument (not a historical series) — always the
-- latest computed values, recomputed in place after every OHLC refresh.
CREATE TABLE IF NOT EXISTS indicator_snapshot (
    instrument_token   INTEGER PRIMARY KEY,
    symbol             TEXT NOT NULL,
    date               TEXT NOT NULL,
    sma_200            REAL,
    ema_20             REAL,
    ema_50             REAL,
    rsi_14             REAL,
    atr_14             REAL,
    avg_volume_20      REAL,
    donchian_high_20   REAL,
    donchian_high_55   REAL,
    computed_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS strategy_configs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       INTEGER NOT NULL REFERENCES accounts(id),
    strategy_name    TEXT NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 0,
    params           TEXT NOT NULL,
    current_version  INTEGER NOT NULL DEFAULT 1,
    last_disabled_at TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_configs_account_name ON strategy_configs(account_id, strategy_name);

-- Phase 8: full history of every params change per strategy — the
-- "current pointer" is strategy_configs.params/current_version, kept in
-- sync by strategy_engine.save_config whenever a NEW version is inserted
-- here (only on an actual params change, not on enabled toggling alone).
CREATE TABLE IF NOT EXISTS strategy_config_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name   TEXT NOT NULL,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    version         INTEGER NOT NULL,
    params_snapshot TEXT NOT NULL,
    effective_from  TEXT NOT NULL DEFAULT (datetime('now')),
    effective_to    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_config_versions_unique ON strategy_config_versions(account_id, strategy_name, version);

-- Backtests are read-only against ohlc_daily/indicator history; never
-- touch trade_tickets, so no account scoping is needed here.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name         TEXT NOT NULL,
    params_snapshot       TEXT NOT NULL,
    start_date            TEXT NOT NULL,
    end_date              TEXT NOT NULL,
    symbols_tested        TEXT NOT NULL,
    total_signals         INTEGER NOT NULL,
    win_rate              REAL,
    avg_r_multiple        REAL,
    profit_factor         REAL,
    max_drawdown_pct      REAL,
    max_drawdown_amount   REAL,
    sharpe_ratio          REAL,
    sortino_ratio         REAL,
    max_consecutive_losses INTEGER,
    recovery_factor       REAL,
    avg_win               REAL,
    avg_loss              REAL,
    time_in_market_pct    REAL,
    mc_iterations         INTEGER,
    mc_median_drawdown_pct REAL,
    mc_p95_drawdown_pct   REAL,
    mc_p5_final_equity    REAL,
    mc_p95_final_equity   REAL,
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Phase 8: optional per-window consistency-check results for a backtest
-- run (see backend/backtest.py's run_backtest `run_walk_forward` flag) —
-- NOT walk-forward optimization, no parameter fitting occurs; each window
-- reuses the identical strategy logic on its own date sub-range.
CREATE TABLE IF NOT EXISTS walk_forward_windows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_run_id INTEGER NOT NULL REFERENCES backtest_runs(id),
    window_index    INTEGER NOT NULL,
    window_start    TEXT NOT NULL,
    window_end      TEXT NOT NULL,
    stats           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_walk_forward_windows_run ON walk_forward_windows(backtest_run_id);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_run_id  INTEGER NOT NULL REFERENCES backtest_runs(id),
    symbol           TEXT NOT NULL,
    entry_date       TEXT NOT NULL,
    entry_price      REAL NOT NULL,
    exit_date        TEXT,
    exit_price       REAL,
    exit_reason      TEXT,
    r_multiple       REAL,
    outcome          TEXT CHECK (outcome IN ('win', 'loss') OR outcome IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_backtest_trades_run ON backtest_trades(backtest_run_id);

-- Single source of truth for realized P&L (Phase 6) — every closed
-- position, however it closed (explicit exit ticket or a GTT auto-trigger
-- detected by the trailing engine), gets exactly one row here.
CREATE TABLE IF NOT EXISTS closed_trades (
    id                             INTEGER PRIMARY KEY AUTOINCREMENT,
    buy_ticket_id                  INTEGER NOT NULL REFERENCES trade_tickets(id),
    sell_ticket_id                 INTEGER NOT NULL REFERENCES trade_tickets(id),
    account_id                     INTEGER NOT NULL REFERENCES accounts(id),
    symbol                         TEXT NOT NULL,
    strategy_name                  TEXT,
    strategy_version               INTEGER,
    source                         TEXT NOT NULL,
    mode                           TEXT NOT NULL CHECK (mode IN ('dry_run', 'live')),
    entry_price                    REAL NOT NULL,
    exit_price                     REAL NOT NULL,
    quantity                       INTEGER NOT NULL,
    realized_pnl                   REAL NOT NULL,
    realized_pnl_pct               REAL NOT NULL,
    risk_amount                    REAL,
    r_multiple_realized            REAL,
    entry_date                     TEXT NOT NULL,
    exit_date                      TEXT NOT NULL,
    holding_period_calendar_days   INTEGER NOT NULL,
    holding_period_trading_days    INTEGER NOT NULL,
    exit_reason                    TEXT NOT NULL,
    created_at                     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_closed_trades_sell_ticket ON closed_trades(sell_ticket_id);
CREATE INDEX IF NOT EXISTS idx_closed_trades_account_mode ON closed_trades(account_id, mode);

CREATE TABLE IF NOT EXISTS stop_adjustment_proposals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id      INTEGER NOT NULL REFERENCES trade_tickets(id),
    current_stop   REAL NOT NULL,
    proposed_stop  REAL NOT NULL,
    reason         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'rejected')),
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_stop_adjustment_ticket ON stop_adjustment_proposals(ticket_id);

-- Phase 8: kill-switch action log — separate from ticket_events since not
-- every event here is ticket-specific (mode switches, batch cancels,
-- emergency-exit-all, strategy enable/disable are account-level actions).
CREATE TABLE IF NOT EXISTS system_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    event_type  TEXT NOT NULL CHECK (event_type IN (
                    'mode_switched', 'cancel_all_pending', 'strategy_disabled', 'strategy_enabled',
                    'emergency_exit_all', 'gtt_cancelled_manually', 'order_cancelled_manually'
                )),
    detail      TEXT,
    triggered_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_system_events_account ON system_events(account_id);

-- PK-12: account-level safety state of the execution-enabled trading
-- account — one row per account, never process memory, so a trading lock or
-- an open broker circuit breaker survives a restart. The limits themselves
-- live in risk_settings (history-tracked); this table holds only state.
CREATE TABLE IF NOT EXISTS account_safety_state (
    account_id          INTEGER PRIMARY KEY REFERENCES accounts(id),
    trading_locked      INTEGER NOT NULL DEFAULT 0 CHECK (trading_locked IN (0, 1)),
    trading_locked_at   TEXT,
    trading_lock_reason TEXT,
    breaker_open        INTEGER NOT NULL DEFAULT 0 CHECK (breaker_open IN (0, 1)),
    breaker_opened_at   TEXT,
    breaker_reason      TEXT,
    last_success_at     TEXT,
    last_reset_at       TEXT,
    -- broker_failure_log.id at the last broker success / manual reset:
    -- only failures after it count toward the breaker (exact, unlike a
    -- same-second timestamp comparison).
    failure_watermark_id INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- PK-12G: broker/transport failures seen on the order path (never
-- validation errors or risk refusals). Read only for the breaker's window.
CREATE TABLE IF NOT EXISTS broker_failure_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
    source      TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_broker_failure_log_account ON broker_failure_log(account_id, occurred_at);

-- Phase 8: order/position reconciliation findings. Only MISMATCHES are
-- persisted (not a running log of every clean check) — see backend/
-- reconciliation.py. Never auto-resolved; resolved=1 only via the
-- explicit "Mark Reviewed" action.
CREATE TABLE IF NOT EXISTS reconciliation_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER REFERENCES trade_tickets(id),
    check_type  TEXT NOT NULL CHECK (check_type IN ('order_status', 'position_state')),
    expected    TEXT,
    actual      TEXT,
    mismatch    INTEGER NOT NULL DEFAULT 1,
    resolved    INTEGER NOT NULL DEFAULT 0,
    detail      TEXT,
    checked_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reconciliation_log_resolved ON reconciliation_log(resolved);

-- Autopilot run log (backend/automation.py): one row per pipeline attempt
-- for a given trading session (run_date, IST).
CREATE TABLE IF NOT EXISTS automation_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    run_date     TEXT NOT NULL,
    trigger      TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('running', 'success', 'skipped', 'failed')),
    summary      TEXT,
    error        TEXT,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_automation_runs_account_date ON automation_runs(account_id, run_date);

-- Earliest date historical data has ever been requested from, per
-- instrument — lets the refresh job deepen history exactly once instead of
-- re-requesting pre-listing ranges a broker will never return.
CREATE TABLE IF NOT EXISTS ohlc_meta (
    instrument_token        INTEGER PRIMARY KEY,
    history_requested_from  TEXT NOT NULL
);
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
    # PK-22F: an explicit lock wait (the implicit default was Python's 5 s).
    # The UI, the order monitor, trailing and reconciliation share this file;
    # a writer briefly holding the lock makes others wait, not fail.
    conn = sqlite3.connect(DB_PATH, timeout=config.SQLITE_BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(config.SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}")
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
        VALUES (?, ?, ?, 'trading', 1)
        """,
        ("Primary", api_key, crypto.encrypt(api_secret)),
    )
    account_id = cursor.lastrowid

    for table in _ACCOUNT_ID_MIGRATIONS:
        conn.execute(
            f"UPDATE {table} SET account_id = ? WHERE account_id IS NULL", (account_id,)
        )


def _ensure_trading_account_type(conn: sqlite3.Connection) -> None:
    # One-time upgrade path for pre-existing databases: this feature assumes
    # the single existing account is now the trading account (no second,
    # genuinely separate investment account exists). Safe to re-run: a no-op
    # once the row is already 'trading'. Revisit this blanket UPDATE if a
    # real second, non-trading account is ever introduced.
    conn.execute("UPDATE accounts SET account_type = 'trading' WHERE account_type = 'investment'")


def _seed_execution_settings(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR IGNORE INTO execution_settings (id, mode) VALUES (1, 'dry_run')")


def _ensure_closes_ticket_id_column(conn: sqlite3.Connection) -> None:
    # Prep for a future exit-rules phase: links a closing (SELL) ticket back
    # to the BUY ticket it closes, for P&L/holding-period calculation. Not
    # read or written by any current logic — existing rows get NULL, which
    # is exactly what an ALTER TABLE ADD COLUMN with no DEFAULT produces.
    if not _column_exists(conn, "trade_tickets", "closes_ticket_id"):
        conn.execute(
            "ALTER TABLE trade_tickets ADD COLUMN closes_ticket_id INTEGER REFERENCES trade_tickets(id)"
        )


def _ensure_risk_rejected_status(conn: sqlite3.Connection) -> None:
    # SQLite can't ALTER a CHECK constraint, so extending trade_tickets.status
    # to allow 'risk_rejected' (Phase 4's risk-engine-blocked outcome, distinct
    # from a manually 'rejected' ticket) requires the standard rebuild: create
    # a replacement table with the new CHECK list, copy all data via an
    # explicit column list, drop the old table, rename the replacement into
    # place. The new risk_check_results column is added in the same rebuild
    # since it's the same table anyway (existing rows get NULL).
    #
    # Safe for the self-referencing closes_ticket_id FK: SQLite resolves FK
    # parent-table names by lookup at enforcement time, not at CREATE TABLE
    # time, so writing "REFERENCES trade_tickets(id)" (the final name) while
    # the old table still exists is valid and resolves correctly once the old
    # table is dropped and the replacement is renamed into that name.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='trade_tickets'"
    ).fetchone()
    if row is None or "'risk_rejected'" in row["sql"]:
        return  # fresh install (SCHEMA already has it) or already migrated

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        """
        CREATE TABLE trade_tickets_new (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id             INTEGER NOT NULL REFERENCES accounts(id),
            source                 TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'strategy')),
            strategy_name          TEXT,
            symbol                 TEXT NOT NULL,
            exchange               TEXT NOT NULL DEFAULT 'NSE',
            instrument_token       INTEGER NOT NULL,
            transaction_type       TEXT NOT NULL CHECK (transaction_type IN ('BUY', 'SELL')),
            quantity               INTEGER NOT NULL CHECK (quantity > 0),
            entry_price            REAL NOT NULL CHECK (entry_price > 0),
            entry_order_type       TEXT NOT NULL CHECK (entry_order_type IN ('MARKET', 'LIMIT')),
            stop_loss              REAL NOT NULL CHECK (stop_loss > 0),
            target_1               REAL NOT NULL CHECK (target_1 > 0),
            target_2               REAL,
            trailing_enabled       INTEGER NOT NULL DEFAULT 0,
            trailing_trigger       TEXT CHECK (trailing_trigger IN ('after_target_1', 'manual') OR trailing_trigger IS NULL),
            trailing_distance      REAL CHECK (trailing_distance IS NULL OR trailing_distance > 0),
            current_stop_loss      REAL,
            trailing_active        INTEGER NOT NULL DEFAULT 0,
            product_type           TEXT NOT NULL CHECK (product_type IN ('CNC', 'MIS', 'NRML')),
            use_gtt                INTEGER NOT NULL DEFAULT 1,
            risk_amount            REAL,
            risk_reward_ratio      REAL,
            rationale              TEXT NOT NULL,
            status                 TEXT NOT NULL DEFAULT 'draft'
                                   CHECK (status IN ('draft', 'pending_confirmation', 'confirmed', 'executed', 'rejected', 'cancelled', 'failed', 'risk_rejected')),
            mode                   TEXT CHECK (mode IN ('dry_run', 'live') OR mode IS NULL),
            kite_order_id          TEXT,
            kite_gtt_id            INTEGER,
            created_at             TEXT NOT NULL DEFAULT (datetime('now')),
            executed_at            TEXT,
            closed_at              TEXT,
            last_trailing_check_at TEXT,
            closes_ticket_id       INTEGER REFERENCES trade_tickets(id),
            risk_check_results     TEXT
        )
        """
    )
    columns = (
        "id, account_id, source, strategy_name, symbol, exchange, instrument_token, "
        "transaction_type, quantity, entry_price, entry_order_type, stop_loss, target_1, target_2, "
        "trailing_enabled, trailing_trigger, trailing_distance, current_stop_loss, trailing_active, "
        "product_type, use_gtt, risk_amount, risk_reward_ratio, rationale, status, mode, "
        "kite_order_id, kite_gtt_id, created_at, executed_at, closed_at, last_trailing_check_at, closes_ticket_id"
    )
    conn.execute(f"INSERT INTO trade_tickets_new ({columns}) SELECT {columns} FROM trade_tickets")
    conn.execute("DROP TABLE trade_tickets")
    conn.execute("ALTER TABLE trade_tickets_new RENAME TO trade_tickets")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_tickets_account ON trade_tickets(account_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_tickets_status ON trade_tickets(status)")

    violations = conn.execute("PRAGMA foreign_key_check(trade_tickets)").fetchall()
    if violations:
        raise RuntimeError(f"trade_tickets rebuild left foreign key violations: {[dict(v) for v in violations]}")
    conn.execute("PRAGMA foreign_keys=ON")


def _ensure_risk_rejected_event_type(conn: sqlite3.Connection) -> None:
    # Same rebuild rationale as _ensure_risk_rejected_status, for
    # ticket_events.event_type. ticket_events.ticket_id REFERENCES
    # trade_tickets(id) needs no special handling here — it's a separate
    # table and resolves by name against whatever trade_tickets is at
    # enforcement time (already rebuilt by the time this runs).
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ticket_events'"
    ).fetchone()
    if row is None or "'risk_rejected'" in row["sql"]:
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        """
        CREATE TABLE ticket_events_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id   INTEGER NOT NULL REFERENCES trade_tickets(id),
            event_type  TEXT NOT NULL CHECK (event_type IN (
                            'created', 'confirmed', 'rejected', 'executed', 'execution_failed',
                            'protection_failed', 'modified', 'trailing_adjusted', 'closed', 'risk_rejected'
                        )),
            detail      TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO ticket_events_new (id, ticket_id, event_type, detail, created_at) "
        "SELECT id, ticket_id, event_type, detail, created_at FROM ticket_events"
    )
    conn.execute("DROP TABLE ticket_events")
    conn.execute("ALTER TABLE ticket_events_new RENAME TO ticket_events")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket ON ticket_events(ticket_id)")

    violations = conn.execute("PRAGMA foreign_key_check(ticket_events)").fetchall()
    if violations:
        raise RuntimeError(f"ticket_events rebuild left foreign key violations: {[dict(v) for v in violations]}")
    conn.execute("PRAGMA foreign_keys=ON")


def _ensure_archived_column(conn: sqlite3.Connection) -> None:
    # Phase 5: soft-delete flag for rejected/risk_rejected/cancelled/failed
    # tickets. Plain nullable-default column, no CHECK constraint involved,
    # so a simple ALTER TABLE suffices (unlike the status/event_type enums).
    if not _column_exists(conn, "trade_tickets", "archived"):
        conn.execute("ALTER TABLE trade_tickets ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")


def _ensure_starting_capital_column(conn: sqlite3.Connection) -> None:
    # Phase 6: optional fixed baseline for the Journal's equity curve/
    # drawdown calculation. Nullable, no CHECK constraint — a plain ALTER
    # TABLE suffices. NULL means "fall back to portfolio value at the time
    # of the first closed trade" (see journal.py).
    if not _column_exists(conn, "risk_settings", "starting_capital"):
        conn.execute("ALTER TABLE risk_settings ADD COLUMN starting_capital REAL")


def _ensure_rule_checklist_column(conn: sqlite3.Connection) -> None:
    # Phase 5: JSON breakdown of the strategy-entry/exit conditions that
    # produced a strategy-sourced ticket — same transparency philosophy as
    # risk_check_results, not a single opaque confidence score. Nullable,
    # no CHECK constraint, so a plain ALTER TABLE suffices.
    if not _column_exists(conn, "trade_tickets", "rule_checklist"):
        conn.execute("ALTER TABLE trade_tickets ADD COLUMN rule_checklist TEXT")


def _ensure_entry_evaluation_column(conn: sqlite3.Connection) -> None:
    # Structured entry-evaluation record for a strategy-sourced entry signal:
    # the ordered list of conditions the strategy actually checked, each with
    # its own actual value / threshold / operator / pass-fail, plus the bars
    # it keyed off and where the signal ranked among that scan's candidates.
    # Same transparency philosophy as risk_check_results and rule_checklist.
    #
    # Deliberately a SIBLING of rule_checklist rather than a replacement:
    # rule_checklist is also written by exit tickets and alert-action tickets,
    # and backend/backtest.py reads its nested keys to classify exit reasons,
    # so repurposing it would reach well beyond entry signals. Nullable, no
    # CHECK constraint — a plain ALTER TABLE suffices, no table rebuild.
    if not _column_exists(conn, "trade_tickets", "entry_evaluation"):
        conn.execute("ALTER TABLE trade_tickets ADD COLUMN entry_evaluation TEXT")


def _ensure_kite_chart_event_type(conn: sqlite3.Connection) -> None:
    # Same rebuild rationale as _ensure_archived_event_type: SQLite cannot
    # ALTER a CHECK constraint, and system_events.event_type carries an
    # allow-list. A chart link that could not be resolved is worth recording
    # at the account level (it is a symptom of a stale or incomplete
    # instrument cache, not a property of one ticket), so it needs its own
    # event_type rather than being squeezed into an existing one.
    #
    # This is the ONLY table rebuild in this feature — the two new
    # trade_tickets columns above are plain ALTER TABLEs, per the rule that
    # tables are not rebuilt unless a CHECK constraint actually requires it.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='system_events'"
    ).fetchone()
    if row is None or "'kite_chart_unresolved'" in row["sql"]:
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        """
        CREATE TABLE system_events_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id  INTEGER NOT NULL REFERENCES accounts(id),
            event_type  TEXT NOT NULL CHECK (event_type IN (
                            'mode_switched', 'cancel_all_pending', 'strategy_disabled', 'strategy_enabled',
                            'emergency_exit_all', 'gtt_cancelled_manually', 'order_cancelled_manually',
                            'kite_chart_unresolved'
                        )),
            detail      TEXT,
            triggered_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO system_events_new (id, account_id, event_type, detail, triggered_at) "
        "SELECT id, account_id, event_type, detail, triggered_at FROM system_events"
    )
    conn.execute("DROP TABLE system_events")
    conn.execute("ALTER TABLE system_events_new RENAME TO system_events")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_system_events_account ON system_events(account_id)")

    violations = conn.execute("PRAGMA foreign_key_check(system_events)").fetchall()
    if violations:
        raise RuntimeError(f"system_events rebuild left foreign key violations: {[dict(v) for v in violations]}")
    conn.execute("PRAGMA foreign_keys=ON")


def _ensure_kite_chart_columns(conn: sqlite3.Connection) -> None:
    # The symbol's chart URL on kite.zerodha.com, resolved SERVER-SIDE when
    # the ticket is created rather than when the link is clicked: a resolution
    # failure then surfaces at 18:22 when the signal is generated, not at
    # 09:15 when the user is trying to act on it.
    #
    # kite_chart_status records the OUTCOME of that resolution ('ok',
    # 'stale_cache', 'unresolved') so the UI can disable a dead link with a
    # reason instead of rendering one that 404s. The ChartIQ and public
    # TradingView variants are pure string transforms of the same
    # (exchange, symbol, token) triple and are derived on read — storing them
    # would be three columns that can disagree with each other.
    for column in ("kite_chart_url", "kite_chart_status"):
        if not _column_exists(conn, "trade_tickets", column):
            conn.execute(f"ALTER TABLE trade_tickets ADD COLUMN {column} TEXT")


def _ensure_archived_event_type(conn: sqlite3.Connection) -> None:
    # Same rebuild rationale as _ensure_risk_rejected_event_type — adds
    # 'archived' as its own event_type rather than overloading 'closed'
    # (already used by the trailing engine for a different meaning: a GTT
    # that was externally triggered/deleted).
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ticket_events'"
    ).fetchone()
    if row is None or "'archived'" in row["sql"]:
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        """
        CREATE TABLE ticket_events_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id   INTEGER NOT NULL REFERENCES trade_tickets(id),
            event_type  TEXT NOT NULL CHECK (event_type IN (
                            'created', 'confirmed', 'rejected', 'executed', 'execution_failed',
                            'protection_failed', 'modified', 'trailing_adjusted', 'closed', 'risk_rejected',
                            'archived'
                        )),
            detail      TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO ticket_events_new (id, ticket_id, event_type, detail, created_at) "
        "SELECT id, ticket_id, event_type, detail, created_at FROM ticket_events"
    )
    conn.execute("DROP TABLE ticket_events")
    conn.execute("ALTER TABLE ticket_events_new RENAME TO ticket_events")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket ON ticket_events(ticket_id)")

    violations = conn.execute("PRAGMA foreign_key_check(ticket_events)").fetchall()
    if violations:
        raise RuntimeError(f"ticket_events rebuild left foreign key violations: {[dict(v) for v in violations]}")
    conn.execute("PRAGMA foreign_keys=ON")


def _ensure_stop_adjusted_breakeven_event_type(conn: sqlite3.Connection) -> None:
    # Same rebuild rationale again — Phase 5's breakeven stop-tightening
    # logs its own event_type on the original ticket, distinct from
    # 'trailing_adjusted' (used by the ATR-trailing engine for a different
    # mechanism) so the two can be told apart in ticket history.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ticket_events'"
    ).fetchone()
    if row is None or "'stop_adjusted_breakeven'" in row["sql"]:
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        """
        CREATE TABLE ticket_events_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id   INTEGER NOT NULL REFERENCES trade_tickets(id),
            event_type  TEXT NOT NULL CHECK (event_type IN (
                            'created', 'confirmed', 'rejected', 'executed', 'execution_failed',
                            'protection_failed', 'modified', 'trailing_adjusted', 'closed', 'risk_rejected',
                            'archived', 'stop_adjusted_breakeven'
                        )),
            detail      TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO ticket_events_new (id, ticket_id, event_type, detail, created_at) "
        "SELECT id, ticket_id, event_type, detail, created_at FROM ticket_events"
    )
    conn.execute("DROP TABLE ticket_events")
    conn.execute("ALTER TABLE ticket_events_new RENAME TO ticket_events")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket ON ticket_events(ticket_id)")

    violations = conn.execute("PRAGMA foreign_key_check(ticket_events)").fetchall()
    if violations:
        raise RuntimeError(f"ticket_events rebuild left foreign key violations: {[dict(v) for v in violations]}")
    conn.execute("PRAGMA foreign_keys=ON")


def _ensure_submission_unknown_event_type(conn: sqlite3.Connection) -> None:
    # Same rebuild rationale again — PK-2: a broker submission whose outcome
    # is unknown (the order may or may not exist at Kite) gets its own event
    # type, so ticket history never reads "execution failed" for an order
    # that may well be live.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ticket_events'"
    ).fetchone()
    if row is None or "'submission_unknown'" in row["sql"]:
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        """
        CREATE TABLE ticket_events_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id   INTEGER NOT NULL REFERENCES trade_tickets(id),
            event_type  TEXT NOT NULL CHECK (event_type IN (
                            'created', 'confirmed', 'rejected', 'executed', 'execution_failed',
                            'protection_failed', 'modified', 'trailing_adjusted', 'closed', 'risk_rejected',
                            'archived', 'stop_adjusted_breakeven', 'submission_unknown'
                        )),
            detail      TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO ticket_events_new (id, ticket_id, event_type, detail, created_at) "
        "SELECT id, ticket_id, event_type, detail, created_at FROM ticket_events"
    )
    conn.execute("DROP TABLE ticket_events")
    conn.execute("ALTER TABLE ticket_events_new RENAME TO ticket_events")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket ON ticket_events(ticket_id)")

    violations = conn.execute("PRAGMA foreign_key_check(ticket_events)").fetchall()
    if violations:
        raise RuntimeError(f"ticket_events rebuild left foreign key violations: {[dict(v) for v in violations]}")
    conn.execute("PRAGMA foreign_keys=ON")


_TRADE_TICKET_SUBMISSION_COLUMNS = [
    # PK-2. The Kite order tag sent with this ticket's order — the key the
    # order monitor searches the broker's order book by when the outcome of
    # a submission is unknown. See execution.order_tag_for.
    ("order_tag", "TEXT"),
    # NULL (never submitted / dry run / pre-PK-2) | submitting | acknowledged
    # | rejected | unknown | adopted | not_found | needs_review — see
    # execution.py's SUBMISSION_* constants.
    ("submission_state", "TEXT"),
    # UTC, written with the claim, before the broker call.
    ("submitted_at", "TEXT"),
]


def _ensure_submission_tracking_columns(conn: sqlite3.Connection) -> None:
    # Must run after _ensure_alert_action_source: that rebuild copies an
    # explicit column list and would silently drop these on an old database.
    for column, sql_type in _TRADE_TICKET_SUBMISSION_COLUMNS:
        if not _column_exists(conn, "trade_tickets", column):
            conn.execute(f"ALTER TABLE trade_tickets ADD COLUMN {column} {sql_type}")


def _ensure_filled_quantity_column(conn: sqlite3.Connection) -> None:
    # PK-13: trade_tickets.quantity is the REQUESTED order size; this is what
    # the broker actually filled, cumulatively — for an entry the shares
    # bought, for an exit ticket the shares that exit sold. NULL = unknown
    # (never submitted, or a historical partial whose fill was never
    # recorded). Must run after _ensure_alert_action_source (that rebuild
    # copies an explicit column list). A plain ADD COLUMN, no rebuild: the
    # CHECK is the storage invariant 0 <= filled_quantity <= quantity.
    if _column_exists(conn, "trade_tickets", "filled_quantity"):
        return
    # The column and its one-time backfill land together or not at all: DDL
    # opens no implicit transaction in Python's sqlite3, so a savepoint does.
    conn.execute("SAVEPOINT pk13_filled_quantity")
    conn.execute(
        "ALTER TABLE trade_tickets ADD COLUMN filled_quantity INTEGER "
        "CHECK (filled_quantity IS NULL OR (filled_quantity >= 0 AND filled_quantity <= quantity))"
    )
    # Only rows whose fill is certain. A dry run fills at once; a live row
    # whose fill was proven ('filled') or predates fill tracking (NULL — its
    # position was treated as fully open) filled its whole quantity; a live
    # order still 'pending' has filled nothing the app knows of. Historical
    # 'partial' / 'unconfirmed' rows stay NULL: their quantity is not guessed.
    conn.execute(
        """
        UPDATE trade_tickets SET filled_quantity = quantity
        WHERE status = 'executed'
          AND (mode = 'dry_run' OR entry_fill_status IS NULL OR entry_fill_status = 'filled')
        """
    )
    conn.execute(
        "UPDATE trade_tickets SET filled_quantity = 0 "
        "WHERE status = 'executed' AND mode = 'live' AND entry_fill_status = 'pending'"
    )
    conn.execute("RELEASE pk13_filled_quantity")


# PK-4 storage backstop, relaxed by PK-13: only an exit whose order is still
# IN FLIGHT owns its position — claimed for execution ('confirmed': in
# flight, unknown or needs_review) or at the broker awaiting its fill
# ('executed' + 'pending', or 'unconfirmed'). An exit whose order has ENDED
# — filled, or ended after a partial fill — no longer owns it: the shares it
# sold are counted (position_quantity), and whatever remains may be exited
# by a new ticket. The claim of that new ticket is checked atomically
# against the remaining open quantity (routes/tickets), so a finished exit
# can never be repeated.
_EXIT_OWNER_PREDICATE = (
    "closes_ticket_id IS NOT NULL AND (status = 'confirmed' OR "
    "(status = 'executed' AND COALESCE(entry_fill_status, '') IN ('pending', 'unconfirmed')))"
)


def _ensure_single_exit_owner_index(conn: sqlite3.Connection) -> None:
    # Two exits of one position racing through confirmation both pass the
    # application checks; the second claim then fails here instead of
    # sending a second SELL. Exits that no longer own the position (failed at
    # the broker, rejected, cancelled, or — PK-13 — ended) drop out.
    #
    # Must run after every trade_tickets rebuild (they drop indexes). A
    # future state that owns a position must be added to the predicate.
    existing = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_trade_tickets_one_exit_owner'"
    ).fetchone()
    if existing is not None and "'unconfirmed'" in existing["sql"]:
        return  # already the PK-13 predicate
    duplicates = conn.execute(
        f"""
        SELECT closes_ticket_id, COUNT(*) AS n FROM trade_tickets
        WHERE {_EXIT_OWNER_PREDICATE}
        GROUP BY closes_ticket_id HAVING n > 1
        """
    ).fetchall()
    if duplicates:
        # Existing data already violates the invariant: never block startup
        # over it (an older, stricter index, if any, stays). The
        # application-level checks still apply.
        logger.warning(
            "Not creating idx_trade_tickets_one_exit_owner: position(s) %s already have more than one "
            "in-flight exit ticket — review them in Ticket History.",
            [d["closes_ticket_id"] for d in duplicates],
        )
        return
    conn.execute("DROP INDEX IF EXISTS idx_trade_tickets_one_exit_owner")
    conn.execute(
        "CREATE UNIQUE INDEX idx_trade_tickets_one_exit_owner ON trade_tickets(closes_ticket_id) "
        f"WHERE {_EXIT_OWNER_PREDICATE}"
    )


def _ensure_closed_trades_one_per_position_index(conn: sqlite3.Connection) -> None:
    # PK-13: a position sold in pieces is journaled ONCE, when it is flat —
    # one closed_trades row per round trip (journal.record_closed_trade).
    # Storage backstop against a second row for the same position.
    duplicates = conn.execute(
        "SELECT buy_ticket_id FROM closed_trades GROUP BY buy_ticket_id HAVING COUNT(*) > 1"
    ).fetchall()
    if duplicates:
        logger.warning(
            "Not creating idx_closed_trades_buy_ticket: position(s) %s already have more than one "
            "closed_trades row.", [d["buy_ticket_id"] for d in duplicates],
        )
        return
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_closed_trades_buy_ticket ON closed_trades(buy_ticket_id)")


_ACCOUNT_SAFETY_EVENT_TYPES = (
    "'mode_switched', 'cancel_all_pending', 'strategy_disabled', 'strategy_enabled', "
    "'emergency_exit_all', 'gtt_cancelled_manually', 'order_cancelled_manually', "
    "'kite_chart_unresolved', "
    "'trading_locked', 'trading_unlocked', 'account_safety_refusal', "
    "'broker_breaker_tripped', 'broker_breaker_reset', 'amo_entry_approved'"
)


def _ensure_account_safety_event_types(conn: sqlite3.Connection) -> None:
    # PK-12: same rebuild rationale as _ensure_kite_chart_event_type — the
    # account-safety audit trail (lock on/off, refusals, breaker trips and
    # resets, explicit AMO approvals) is account-level, so it belongs in
    # system_events, whose event_type is a CHECK allow-list. Must run after
    # _ensure_kite_chart_event_type (it keeps that type).
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='system_events'"
    ).fetchone()
    if row is None or "'broker_breaker_tripped'" in row["sql"]:
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        f"""
        CREATE TABLE system_events_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id  INTEGER NOT NULL REFERENCES accounts(id),
            event_type  TEXT NOT NULL CHECK (event_type IN ({_ACCOUNT_SAFETY_EVENT_TYPES})),
            detail      TEXT,
            triggered_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO system_events_new (id, account_id, event_type, detail, triggered_at) "
        "SELECT id, account_id, event_type, detail, triggered_at FROM system_events"
    )
    conn.execute("DROP TABLE system_events")
    conn.execute("ALTER TABLE system_events_new RENAME TO system_events")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_system_events_account ON system_events(account_id)")

    violations = conn.execute("PRAGMA foreign_key_check(system_events)").fetchall()
    if violations:
        raise RuntimeError(f"system_events rebuild left foreign key violations: {[dict(v) for v in violations]}")
    conn.execute("PRAGMA foreign_keys=ON")


# PK-12: account-level limits, per account in risk_settings so they are
# history-tracked by risk_engine.update_settings like every other limit.
# NULL always means "disabled". No rupee amount is defaulted: the absolute
# notional cap and the daily loss limits start disabled until the user sets
# them. Plain ALTER TABLE ADD COLUMN (a CHECK on a new column is allowed; the
# defaults satisfy it for existing rows).
_RISK_SETTINGS_ACCOUNT_SAFETY_COLUMNS = [
    ("max_order_notional", "REAL CHECK (max_order_notional IS NULL OR max_order_notional > 0)"),
    ("max_new_entries_per_day",
     "INTEGER DEFAULT 3 CHECK (max_new_entries_per_day IS NULL OR max_new_entries_per_day > 0)"),
    ("max_entry_price_deviation_pct",
     "REAL DEFAULT 3 CHECK (max_entry_price_deviation_pct IS NULL "
     "OR (max_entry_price_deviation_pct > 0 AND max_entry_price_deviation_pct <= 100))"),
    ("max_daily_loss_amount", "REAL CHECK (max_daily_loss_amount IS NULL OR max_daily_loss_amount > 0)"),
    ("max_daily_loss_pct",
     "REAL CHECK (max_daily_loss_pct IS NULL OR (max_daily_loss_pct > 0 AND max_daily_loss_pct <= 100))"),
    ("broker_breaker_threshold",
     "INTEGER DEFAULT 3 CHECK (broker_breaker_threshold IS NULL OR broker_breaker_threshold > 0)"),
    ("broker_breaker_window_minutes",
     "INTEGER NOT NULL DEFAULT 10 CHECK (broker_breaker_window_minutes > 0)"),
]


def _ensure_account_safety_settings_columns(conn: sqlite3.Connection) -> None:
    for column, sql_type in _RISK_SETTINGS_ACCOUNT_SAFETY_COLUMNS:
        if not _column_exists(conn, "risk_settings", column):
            conn.execute(f"ALTER TABLE risk_settings ADD COLUMN {column} {sql_type}")


def _seed_risk_settings(conn: sqlite3.Connection) -> None:
    # WHERE NOT EXISTS rather than INSERT OR IGNORE: an ignored insert still
    # advances the AUTOINCREMENT counter, so every start used to write (PK-22).
    conn.execute(
        """
        INSERT INTO risk_settings (account_id)
        SELECT a.id FROM accounts a WHERE a.account_type = 'trading'
          AND NOT EXISTS (SELECT 1 FROM risk_settings r WHERE r.account_id = a.id)
        """
    )


def _seed_account_safety_state(conn: sqlite3.Connection) -> None:
    # PK-12: unlocked, breaker closed — only for the trading account(s).
    conn.execute(
        """
        INSERT OR IGNORE INTO account_safety_state (account_id)
        SELECT id FROM accounts WHERE account_type = 'trading'
        """
    )


# Mirrors strategy_engine.DEFAULT_PARAMS — duplicated here (rather than
# imported) so db.py has no dependency on strategy_engine.py at import time;
# strategy_engine.py's copy is the one actually used to evaluate signals,
# this is only the one-time seed value for a brand-new account's row.
_DEFAULT_MOMENTUM_BREAKOUT_PARAMS = {
    "donchian_lookback": 30,
    "volume_multiple": 3.0,
    "trend_filter_period": 200,
    "atr_stop_multiple": 3,
    "target_r_multiple": 3,
    "trailing_atr_multiple": 1.5,
    "thesis_invalidation_ma": 50,
    "time_exit_days": 15,
    "breakeven_trigger_r": 1.0,
    "max_signals_per_day": 3,
}

# Mirrors backend/strategies/trend_pullback.py's DEFAULT_PARAMS — duplicated
# here for the same reason as the momentum_breakout copy above (avoids a
# circular import: backend.strategies -> backend.indicators -> backend.db).
_DEFAULT_TREND_PULLBACK_PARAMS = {
    "trend_filter_period": 200,
    "pullback_ma_period": 20,
    "pullback_band_pct": 1.5,
    "pullback_lookback_days": 3,
    "volume_multiple": 3.0,
    "atr_stop_multiple": 3.5,
    "target_r_multiple": 4,
    "trailing_atr_multiple": 1.5,
    "thesis_invalidation_ma": 50,
    "time_exit_days": 15,
    "breakeven_trigger_r": 1.0,
    "max_signals_per_day": 2,
}

# Mirrors backend/strategies/rsi_mean_reversion.py's DEFAULT_PARAMS.
_DEFAULT_RSI_MEAN_REVERSION_PARAMS = {
    "trend_filter_period": 200,
    "rsi_oversold_threshold": 35,
    "rsi_turn_confirmation": True,
    "swing_low_lookback_days": 10,
    "atr_stop_multiple": 1.0,
    "target_r_multiple": 1.5,
    "rsi_exit_level": 55,
    "thesis_invalidation_ma": 50,
    "time_exit_days": 10,
    "breakeven_trigger_r": 1.0,
    "max_signals_per_day": 2,
}

_DEFAULT_STRATEGY_PARAMS = {
    "momentum_breakout": _DEFAULT_MOMENTUM_BREAKOUT_PARAMS,
    "trend_pullback": _DEFAULT_TREND_PULLBACK_PARAMS,
    "rsi_mean_reversion": _DEFAULT_RSI_MEAN_REVERSION_PARAMS,
}


def _seed_strategy_configs(conn: sqlite3.Connection) -> None:
    # enabled defaults to 0 (off) — nothing scans until explicitly turned on
    # via the Strategies UI.
    accounts = conn.execute("SELECT id FROM accounts WHERE account_type = 'trading'").fetchall()
    for account in accounts:
        for strategy_name, default_params in _DEFAULT_STRATEGY_PARAMS.items():
            conn.execute(
                """
                INSERT INTO strategy_configs (account_id, strategy_name, enabled, params)
                SELECT ?, ?, 0, ?
                WHERE NOT EXISTS (SELECT 1 FROM strategy_configs WHERE account_id = ? AND strategy_name = ?)
                """,
                (account["id"], strategy_name, json.dumps(default_params), account["id"], strategy_name),
            )


# (panel_type, display_order, size) — preserves today's Dashboard layout
# exactly as the default. See PANEL_REGISTRY in frontend/app.js for how a
# future panel_type gets added without touching this seed's shape.
_DEFAULT_DASHBOARD_PANELS = [
    ("portfolio_summary", 0, "small"),
    ("holdings_table", 1, "large"),
    ("positions_table", 2, "large"),
    ("mf_table", 3, "large"),
]


def _seed_dashboard_panels(conn: sqlite3.Connection) -> None:
    accounts = conn.execute("SELECT id FROM accounts").fetchall()
    for account in accounts:
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM dashboard_panels WHERE account_id = ?", (account["id"],)
        ).fetchone()["n"]
        if existing > 0:
            continue
        conn.executemany(
            """
            INSERT INTO dashboard_panels (account_id, panel_type, display_order, size, visible)
            VALUES (?, ?, ?, ?, 1)
            """,
            [(account["id"], panel_type, order, size) for panel_type, order, size in _DEFAULT_DASHBOARD_PANELS],
        )


def _ensure_watchlist_summary_panel(conn: sqlite3.Connection) -> None:
    # _seed_dashboard_panels only seeds the 4 defaults for an account with
    # zero rows — existing accounts (already seeded before this panel type
    # existed) need this new row added separately. Starts hidden
    # (visible=0) so it shows up via "Add Panel" rather than appearing
    # unasked-for on an already-configured dashboard.
    accounts = conn.execute("SELECT id FROM accounts").fetchall()
    for account in accounts:
        has_it = conn.execute(
            "SELECT COUNT(*) AS n FROM dashboard_panels WHERE account_id = ? AND panel_type = 'watchlist_summary'",
            (account["id"],),
        ).fetchone()["n"]
        if has_it > 0:
            continue
        max_order = conn.execute(
            "SELECT COALESCE(MAX(display_order), -1) AS m FROM dashboard_panels WHERE account_id = ?",
            (account["id"],),
        ).fetchone()["m"]
        conn.execute(
            """
            INSERT INTO dashboard_panels (account_id, panel_type, display_order, size, visible)
            VALUES (?, 'watchlist_summary', ?, 'medium', 0)
            """,
            (account["id"], max_order + 1),
        )


def _ensure_strategy_version_column(conn: sqlite3.Connection) -> None:
    # Phase 8: which strategy_config_versions row generated this ticket —
    # nullable (manual tickets have no strategy version at all), no CHECK
    # constraint, so a plain ALTER TABLE suffices.
    if not _column_exists(conn, "trade_tickets", "strategy_version"):
        conn.execute("ALTER TABLE trade_tickets ADD COLUMN strategy_version INTEGER")


def _ensure_closed_trades_strategy_version_column(conn: sqlite3.Connection) -> None:
    if not _column_exists(conn, "closed_trades", "strategy_version"):
        conn.execute("ALTER TABLE closed_trades ADD COLUMN strategy_version INTEGER")


def _ensure_strategy_configs_versioning_columns(conn: sqlite3.Connection) -> None:
    # Phase 8: strategy_configs becomes the "current pointer" into
    # strategy_config_versions once this migration runs on a database that
    # predates versioning.
    if not _column_exists(conn, "strategy_configs", "current_version"):
        conn.execute("ALTER TABLE strategy_configs ADD COLUMN current_version INTEGER NOT NULL DEFAULT 1")
    if not _column_exists(conn, "strategy_configs", "last_disabled_at"):
        conn.execute("ALTER TABLE strategy_configs ADD COLUMN last_disabled_at TEXT")


_BACKTEST_RUNS_NEW_COLUMNS = [
    ("max_drawdown_amount", "REAL"),
    ("sharpe_ratio", "REAL"),
    ("sortino_ratio", "REAL"),
    ("max_consecutive_losses", "INTEGER"),
    ("recovery_factor", "REAL"),
    ("avg_win", "REAL"),
    ("avg_loss", "REAL"),
    ("time_in_market_pct", "REAL"),
    ("mc_iterations", "INTEGER"),
    ("mc_median_drawdown_pct", "REAL"),
    ("mc_p95_drawdown_pct", "REAL"),
    ("mc_p5_final_equity", "REAL"),
    ("mc_p95_final_equity", "REAL"),
]


def _ensure_backtest_runs_rigor_columns(conn: sqlite3.Connection) -> None:
    # Phase 8: additional backtest metrics — all nullable, no CHECK
    # constraints, so plain ALTER TABLEs suffice. Null on a pre-Phase-8 run
    # simply means "not computed for this run" (old runs are never
    # retroactively recomputed).
    for column, sql_type in _BACKTEST_RUNS_NEW_COLUMNS:
        if not _column_exists(conn, "backtest_runs", column):
            conn.execute(f"ALTER TABLE backtest_runs ADD COLUMN {column} {sql_type}")


# PK-21: execution-realistic backtests. All nullable, no CHECKs, so plain
# ALTER TABLEs suffice and the migration is idempotent. NULL on a run from
# before PK-21 means "recorded by the old, optimistic fill model" — old runs
# are never recomputed. The JSON columns hold the run's execution config,
# net metrics, MTM equity curve, skipped/unexecuted signals and warnings.
_BACKTEST_RUNS_PK21_COLUMNS = [
    ("execution_config", "TEXT"),
    ("metrics", "TEXT"),
    ("equity_curve", "TEXT"),
    ("skipped_signals", "TEXT"),
    ("warnings", "TEXT"),
    ("universe_source", "TEXT"),
    ("survivorship_bias", "TEXT"),
]
_BACKTEST_TRADES_PK21_COLUMNS = [
    ("strategy_name", "TEXT"),
    ("signal_date", "TEXT"),
    ("planned_entry_price", "REAL"),
    ("quantity", "INTEGER"),
    ("initial_stop", "REAL"),
    ("initial_target", "REAL"),
    ("gross_pnl", "REAL"),
    ("entry_costs", "REAL"),
    ("exit_costs", "REAL"),
    ("costs", "REAL"),
    ("net_pnl", "REAL"),
    ("holding_days", "INTEGER"),
    ("bars_held", "INTEGER"),
    ("net_r_multiple", "REAL"),
    ("gap_fill", "INTEGER"),
    ("forced_close", "INTEGER"),
    ("data_warning", "TEXT"),
]


def _ensure_backtest_pk21_columns(conn: sqlite3.Connection) -> None:
    for column, sql_type in _BACKTEST_RUNS_PK21_COLUMNS:
        if not _column_exists(conn, "backtest_runs", column):
            conn.execute(f"ALTER TABLE backtest_runs ADD COLUMN {column} {sql_type}")
    for column, sql_type in _BACKTEST_TRADES_PK21_COLUMNS:
        if not _column_exists(conn, "backtest_trades", column):
            conn.execute(f"ALTER TABLE backtest_trades ADD COLUMN {column} {sql_type}")


def _seed_strategy_config_versions(conn: sqlite3.Connection) -> None:
    # One-time backfill: give every existing strategy_configs row a version
    # 1 history entry matching its current params, so trade_tickets.
    # strategy_version always has something real to point at even for
    # tickets created before this phase (those tickets get NULL — nothing
    # to backfill for tickets that already exist, only forward-looking).
    rows = conn.execute("SELECT * FROM strategy_configs").fetchall()
    for row in rows:
        has_version = conn.execute(
            "SELECT COUNT(*) AS n FROM strategy_config_versions WHERE account_id = ? AND strategy_name = ?",
            (row["account_id"], row["strategy_name"]),
        ).fetchone()["n"]
        if has_version:
            continue
        conn.execute(
            """
            INSERT INTO strategy_config_versions (strategy_name, account_id, version, params_snapshot, effective_from)
            VALUES (?, ?, 1, ?, ?)
            """,
            (row["strategy_name"], row["account_id"], row["params"], row["updated_at"]),
        )
        conn.execute(
            "UPDATE strategy_configs SET current_version = 1 WHERE id = ?", (row["id"],)
        )


def _ensure_todays_actions_panel(conn: sqlite3.Connection) -> None:
    # Same idempotent-per-account migration pattern as
    # _ensure_watchlist_summary_panel — starts hidden (visible=0) so it
    # shows up via "Add Panel" rather than appearing unasked-for.
    accounts = conn.execute("SELECT id FROM accounts").fetchall()
    for account in accounts:
        has_it = conn.execute(
            "SELECT COUNT(*) AS n FROM dashboard_panels WHERE account_id = ? AND panel_type = 'todays_actions'",
            (account["id"],),
        ).fetchone()["n"]
        if has_it > 0:
            continue
        max_order = conn.execute(
            "SELECT COALESCE(MAX(display_order), -1) AS m FROM dashboard_panels WHERE account_id = ?",
            (account["id"],),
        ).fetchone()["m"]
        conn.execute(
            """
            INSERT INTO dashboard_panels (account_id, panel_type, display_order, size, visible)
            VALUES (?, 'todays_actions', ?, 'medium', 0)
            """,
            (account["id"], max_order + 1),
        )


_TRADE_TICKET_EXECUTION_COLUMNS = [
    # regular | amo — how the order was sent to the broker.
    ("order_variety", "TEXT"),
    # pending | filled | partial | rejected | cancelled — tracked by
    # backend/order_monitor.py until the broker reports a terminal state.
    ("entry_fill_status", "TEXT"),
    ("filled_price", "REAL"),
    ("filled_at", "TEXT"),
    # Date of the candle a strategy signal was computed from.
    ("signal_date", "TEXT"),
]


def _ensure_execution_tracking_columns(conn: sqlite3.Connection) -> None:
    for column, sql_type in _TRADE_TICKET_EXECUTION_COLUMNS:
        if not _column_exists(conn, "trade_tickets", column):
            conn.execute(f"ALTER TABLE trade_tickets ADD COLUMN {column} {sql_type}")


def _ensure_alert_action_source(conn: sqlite3.Connection) -> None:
    # Same rebuild rationale as _ensure_risk_rejected_status: SQLite can't
    # ALTER a CHECK constraint, so widening trade_tickets.source to allow
    # 'alert_action' (the Dashboard alert Quick Action popup's sell/buy
    # tickets) needs the standard rebuild — replacement table with the new
    # CHECK list, copy all data via an explicit column list covering every
    # column that exists on this table today (including the ones added by
    # later migrations than the one that first wrote this rebuild pattern),
    # drop the old table, rename the replacement into place.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='trade_tickets'"
    ).fetchone()
    if row is None or "'alert_action'" in row["sql"]:
        return  # fresh install (SCHEMA already has it) or already migrated

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        """
        CREATE TABLE trade_tickets_new (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id             INTEGER NOT NULL REFERENCES accounts(id),
            source                 TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'strategy', 'alert_action')),
            strategy_name          TEXT,
            symbol                 TEXT NOT NULL,
            exchange               TEXT NOT NULL DEFAULT 'NSE',
            instrument_token       INTEGER NOT NULL,
            transaction_type       TEXT NOT NULL CHECK (transaction_type IN ('BUY', 'SELL')),
            quantity               INTEGER NOT NULL CHECK (quantity > 0),
            entry_price            REAL NOT NULL CHECK (entry_price > 0),
            entry_order_type       TEXT NOT NULL CHECK (entry_order_type IN ('MARKET', 'LIMIT')),
            stop_loss              REAL NOT NULL CHECK (stop_loss > 0),
            target_1               REAL NOT NULL CHECK (target_1 > 0),
            target_2               REAL,
            trailing_enabled       INTEGER NOT NULL DEFAULT 0,
            trailing_trigger       TEXT CHECK (trailing_trigger IN ('after_target_1', 'manual') OR trailing_trigger IS NULL),
            trailing_distance      REAL CHECK (trailing_distance IS NULL OR trailing_distance > 0),
            current_stop_loss      REAL,
            trailing_active        INTEGER NOT NULL DEFAULT 0,
            product_type           TEXT NOT NULL CHECK (product_type IN ('CNC', 'MIS', 'NRML')),
            use_gtt                INTEGER NOT NULL DEFAULT 1,
            risk_amount            REAL,
            risk_reward_ratio      REAL,
            rationale              TEXT NOT NULL,
            status                 TEXT NOT NULL DEFAULT 'draft'
                                   CHECK (status IN ('draft', 'pending_confirmation', 'confirmed', 'executed', 'rejected', 'cancelled', 'failed', 'risk_rejected')),
            mode                   TEXT CHECK (mode IN ('dry_run', 'live') OR mode IS NULL),
            kite_order_id          TEXT,
            kite_gtt_id            INTEGER,
            created_at             TEXT NOT NULL DEFAULT (datetime('now')),
            executed_at            TEXT,
            closed_at              TEXT,
            last_trailing_check_at TEXT,
            closes_ticket_id       INTEGER REFERENCES trade_tickets(id),
            risk_check_results     TEXT,
            archived               INTEGER NOT NULL DEFAULT 0,
            rule_checklist         TEXT,
            strategy_version       INTEGER,
            order_variety          TEXT,
            entry_fill_status      TEXT,
            filled_price           REAL,
            filled_at              TEXT,
            signal_date            TEXT
        )
        """
    )
    columns = (
        "id, account_id, source, strategy_name, symbol, exchange, instrument_token, "
        "transaction_type, quantity, entry_price, entry_order_type, stop_loss, target_1, target_2, "
        "trailing_enabled, trailing_trigger, trailing_distance, current_stop_loss, trailing_active, "
        "product_type, use_gtt, risk_amount, risk_reward_ratio, rationale, status, mode, "
        "kite_order_id, kite_gtt_id, created_at, executed_at, closed_at, last_trailing_check_at, "
        "closes_ticket_id, risk_check_results, archived, rule_checklist, strategy_version, "
        "order_variety, entry_fill_status, filled_price, filled_at, signal_date"
    )
    conn.execute(f"INSERT INTO trade_tickets_new ({columns}) SELECT {columns} FROM trade_tickets")
    conn.execute("DROP TABLE trade_tickets")
    conn.execute("ALTER TABLE trade_tickets_new RENAME TO trade_tickets")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_tickets_account ON trade_tickets(account_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_tickets_status ON trade_tickets(status)")

    violations = conn.execute("PRAGMA foreign_key_check(trade_tickets)").fetchall()
    if violations:
        raise RuntimeError(f"trade_tickets rebuild left foreign key violations: {[dict(v) for v in violations]}")
    conn.execute("PRAGMA foreign_keys=ON")


def _ensure_instrument_tick_size_column(conn: sqlite3.Connection) -> None:
    if not _column_exists(conn, "instrument_cache", "tick_size"):
        conn.execute("ALTER TABLE instrument_cache ADD COLUMN tick_size REAL")


_RECONCILIATION_ISSUE_COLUMNS = [
    ("issue_code", "TEXT"),
    ("severity", "TEXT"),
    ("symbol", "TEXT"),
    ("gtt_id", "INTEGER"),
    ("expected_quantity", "INTEGER"),
    ("broker_quantity", "INTEGER"),
    ("position_state", "TEXT"),
    ("suggested_action", "TEXT"),
]


def _ensure_reconciliation_issue_columns(conn: sqlite3.Connection) -> None:
    # PK-16: structured findings (ORPHAN_GTT, GTT_QUANTITY_TOO_HIGH, ...)
    # alongside the free-text detail. check_type keeps its two-value CHECK
    # (the broad order/position side); issue_code carries the specific
    # finding, so no table rebuild is needed. All nullable: rows written
    # before PK-16 keep NULLs and the UI falls back to their detail text.
    for column, sql_type in _RECONCILIATION_ISSUE_COLUMNS:
        if not _column_exists(conn, "reconciliation_log", column):
            conn.execute(f"ALTER TABLE reconciliation_log ADD COLUMN {column} {sql_type}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reconciliation_log_issue ON reconciliation_log(issue_code, gtt_id)"
    )


_PAPER_TABLES = """
CREATE TABLE IF NOT EXISTS paper_orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_order_id   TEXT UNIQUE,
    account_id       INTEGER NOT NULL REFERENCES accounts(id),
    ticket_id        INTEGER,
    source_gtt_id    TEXT,
    tag              TEXT,
    variety          TEXT NOT NULL CHECK (variety IN ('regular', 'amo')),
    exchange         TEXT NOT NULL,
    tradingsymbol    TEXT NOT NULL,
    transaction_type TEXT NOT NULL CHECK (transaction_type IN ('BUY', 'SELL')),
    product          TEXT NOT NULL,
    order_type       TEXT NOT NULL CHECK (order_type IN ('MARKET', 'LIMIT')),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    filled_quantity  INTEGER NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0 AND filled_quantity <= quantity),
    pending_quantity INTEGER NOT NULL,
    price            REAL,
    trigger_price    REAL,
    average_price    REAL NOT NULL DEFAULT 0,
    status           TEXT NOT NULL CHECK (status IN ('AMO REQ RECEIVED', 'OPEN', 'COMPLETE', 'CANCELLED', 'REJECTED')),
    status_message   TEXT,
    session_date     TEXT NOT NULL,
    fill_plan        TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    filled_at        TEXT,
    cancelled_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_orders_account_status ON paper_orders(account_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_orders_tag ON paper_orders(tag) WHERE tag IS NOT NULL;

CREATE TABLE IF NOT EXISTS paper_gtts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_gtt_id   TEXT UNIQUE,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    trigger_type   TEXT NOT NULL CHECK (trigger_type IN ('single', 'two-leg')),
    exchange       TEXT NOT NULL,
    tradingsymbol  TEXT NOT NULL,
    trigger_values TEXT NOT NULL,
    last_price     REAL,
    legs           TEXT NOT NULL,
    quantity       INTEGER NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('active', 'triggered', 'deleted')),
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    triggered_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_gtts_account_status ON paper_gtts(account_id, status);
"""


def _ensure_paper_tables(conn: sqlite3.Connection) -> None:
    # PK-20: the paper broker's own books — its order book and GTT book, the
    # simulated counterpart of what Kite keeps. Separate tables, so nothing
    # about a live ticket or a live broker id changes; dry-run tickets refer
    # to them by 'PAPER-O-<id>' / 'PAPER-G-<id>' in kite_order_id /
    # kite_gtt_id. Only CREATE ... IF NOT EXISTS: idempotent, no rebuild.
    _execute_script(conn, _PAPER_TABLES)


def _ensure_watchlist_tag_column(conn: sqlite3.Connection) -> None:
    # Short free-text categorical label, separate from `notes` (free-form
    # commentary) — used only for the Watchlist's optional "group by tag"
    # view. Nullable, no CHECK constraint (deliberately no fixed taxonomy),
    # so a plain ALTER TABLE suffices.
    if not _column_exists(conn, "watchlist", "tag"):
        conn.execute("ALTER TABLE watchlist ADD COLUMN tag TEXT")


def _ensure_dashboard_panels_screen_column(conn: sqlite3.Connection) -> None:
    # dashboard_panels already holds every field a configurable panel grid
    # needs (type/order/size/visible); the Watchlist screen's own panel grid
    # reuses that exact same table/mechanism rather than a parallel one —
    # this column is the only change needed to serve two screens with it.
    # Backfilled to 'dashboard' so every pre-existing row keeps behaving
    # exactly as it did before this column existed.
    if not _column_exists(conn, "dashboard_panels", "screen"):
        conn.execute("ALTER TABLE dashboard_panels ADD COLUMN screen TEXT NOT NULL DEFAULT 'dashboard'")
        # The old UNIQUE(account_id, panel_type) index would otherwise block
        # the dashboard's own 'watchlist_summary' panel_type from ever
        # coexisting with a same-named Watchlist-screen panel_type; scope
        # uniqueness to (account_id, screen, panel_type) instead.
        conn.execute("DROP INDEX IF EXISTS idx_dashboard_panels_account_type")
    # Outside the column check: a fresh database also needs the scoped index.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_dashboard_panels_account_screen_type "
        "ON dashboard_panels(account_id, screen, panel_type)"
    )


_DEFAULT_WATCHLIST_PANELS = [
    # (panel_type, display_order, size) — screen='watchlist'. Visible by
    # default (unlike the Dashboard's opt-in extras): this IS the content of
    # the Watchlist screen, not an optional add-on to it.
    ("watchlist_all", 0, "large"),
    ("watchlist_near_breakout", 1, "medium"),
]


def _ensure_watchlist_panels(conn: sqlite3.Connection) -> None:
    accounts = conn.execute("SELECT id FROM accounts").fetchall()
    for account in accounts:
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM dashboard_panels WHERE account_id = ? AND screen = 'watchlist'",
            (account["id"],),
        ).fetchone()["n"]
        if existing > 0:
            continue
        conn.executemany(
            """
            INSERT INTO dashboard_panels (account_id, screen, panel_type, display_order, size, visible)
            VALUES (?, 'watchlist', ?, ?, ?, 1)
            """,
            [(account["id"], panel_type, order, size) for panel_type, order, size in _DEFAULT_WATCHLIST_PANELS],
        )


def _scrub_plaintext_session_tokens(conn: sqlite3.Connection) -> None:
    """PK-18 (audit P1-12): the Kite access token lives only encrypted, in
    accounts.access_token_enc. sessions.access_token used to hold a plaintext
    copy, which get_kite() read, so the encryption protected nothing.

    Before blanking the column, an account whose encrypted token is missing
    gets it from its newest active session, so someone who logged in today
    stays logged in. Idempotent: a second run finds nothing to copy or blank.
    """
    rows = conn.execute(
        """
        SELECT s.account_id, s.access_token FROM sessions s JOIN accounts a ON a.id = s.account_id
        WHERE s.is_active = 1 AND s.access_token != '' AND a.access_token_enc IS NULL
        ORDER BY s.id DESC
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE accounts SET access_token_enc = ? WHERE id = ? AND access_token_enc IS NULL",
            (crypto.encrypt(row["access_token"]), row["account_id"]),
        )
    conn.execute("UPDATE sessions SET access_token = '' WHERE access_token != ''")


# ---- PK-22A: versioned, transactional migrations ------------------------------
#
# Every schema change is an ordered, named migration applied once and recorded
# in schema_version, each inside its own transaction (with its version row), so
# a crash mid-migration leaves the database at the previous version and the
# next start resumes. A database recorded by NEWER code (or by a different
# migration history) is refused before anything is written.
#
# Version 1 is everything before PK-22: the introspective _ensure_* steps.
# They stay idempotent, so a database of any age (or a brand-new one) is
# brought to the same place by them; afterwards they never run again.
# Startup invariants (seeds, the PK-18 token scrub, the closed-trade
# backfill) still run on every start: they are data-only and write nothing
# when there is nothing to do.

SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version        INTEGER PRIMARY KEY,
    migration_name TEXT NOT NULL,
    applied_at     TEXT NOT NULL DEFAULT (datetime('now')),
    duration_ms    INTEGER
)
"""


class DatabaseSchemaUnsupported(RuntimeError):
    """The database was migrated by a version of the code this one does not
    know. Refused rather than guessed at."""
    error_code = "DATABASE_SCHEMA_UNSUPPORTED"


class DatabaseMigrationFailed(RuntimeError):
    error_code = "DATABASE_MIGRATION_FAILED"


class MigrationBackupFailed(DatabaseMigrationFailed):
    """migrate()'s own pre-migration backup failed (an I/O error or its integrity check). Raised before any
    migration transaction began: nothing was migrated. A subclass, so every existing caller of migrate() sees the
    DatabaseMigrationFailed it always saw; db_maintenance migrate-v12 reports it as BACKUP_FAILED."""


class DatabaseMigrationAuthorizationRequired(RuntimeError):
    """A pending migration needs explicit operator authorization (migration 12 only; see
    _AUTHORIZATION_GATED_MIGRATIONS). Raised before any write: nothing was changed."""
    error_code = "DATABASE_MIGRATION_AUTHORIZATION_REQUIRED"


# V12 final readiness, Phase C: init_db refuses an established installation whose database is missing, empty,
# corrupt, replaced, moved or mid-maintenance (backend/installation_state.py). Raised before any write.
DatabaseInstallationStateRefused = installation_state.DatabaseInstallationStateRefused


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    """executescript() COMMITs first, which would end a migration's
    transaction half-way; this runs the same statements one by one."""
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            conn.execute(buffer)
            buffer = ""
    leftover = [part.strip() for part in buffer.splitlines() if part.strip()]
    if any(not part.startswith("--") for part in leftover):
        raise ValueError(f"Incomplete SQL statement in script: {buffer[:80]!r}")


def _m001_baseline(conn: sqlite3.Connection) -> None:
    _execute_script(conn, SCHEMA)
    _run_column_migrations(conn)
    _ensure_closes_ticket_id_column(conn)
    _ensure_risk_rejected_status(conn)
    _ensure_risk_rejected_event_type(conn)
    _ensure_archived_column(conn)
    _ensure_archived_event_type(conn)
    _ensure_rule_checklist_column(conn)
    _ensure_entry_evaluation_column(conn)
    _ensure_kite_chart_columns(conn)
    _ensure_kite_chart_event_type(conn)
    _ensure_stop_adjusted_breakeven_event_type(conn)
    _ensure_submission_unknown_event_type(conn)
    _ensure_account_safety_event_types(conn)
    _ensure_starting_capital_column(conn)
    _ensure_account_safety_settings_columns(conn)
    _ensure_strategy_version_column(conn)
    _ensure_closed_trades_strategy_version_column(conn)
    _ensure_strategy_configs_versioning_columns(conn)
    _ensure_backtest_runs_rigor_columns(conn)
    _ensure_backtest_pk21_columns(conn)
    _ensure_execution_tracking_columns(conn)
    _ensure_alert_action_source(conn)
    # PK-22 fix: that rebuild copies an explicit column list that predates
    # these three columns, so on a database old enough to need it they were
    # added above and then dropped again, never to return. Re-ensured here
    # (no-ops on every other database). Found by the synthetic old-database
    # migration test.
    _ensure_entry_evaluation_column(conn)
    _ensure_kite_chart_columns(conn)
    _ensure_submission_tracking_columns(conn)
    _ensure_filled_quantity_column(conn)
    _ensure_single_exit_owner_index(conn)
    _ensure_closed_trades_one_per_position_index(conn)
    _ensure_instrument_tick_size_column(conn)
    _ensure_watchlist_tag_column(conn)
    _ensure_dashboard_panels_screen_column(conn)
    _ensure_reconciliation_issue_columns(conn)
    _ensure_paper_tables(conn)
    _execute_script(conn, _ACCOUNT_ID_INDEXES)


def _m002_drop_legacy_dashboard_index(conn: sqlite3.Connection) -> None:
    # Before PK-22, SCHEMA re-created the (account_id, panel_type) unique
    # index on every start after the screen migration had dropped it, so
    # every restarted database carries it again. It forbids the same
    # panel_type on two screens, which the screen-scoped index exists to allow.
    conn.execute("DROP INDEX IF EXISTS idx_dashboard_panels_account_type")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_dashboard_panels_account_screen_type "
        "ON dashboard_panels(account_id, screen, panel_type)"
    )


def _m003_query_indexes(conn: sqlite3.Connection) -> None:
    # PK-22B/G (audit P2-2). Only indexes a measured hot path needs; see the
    # PK-22 report for the EXPLAIN QUERY PLAN / timing evidence.
    #  * closes_ticket_id — every "exits of this position" lookup: the
    #    correlated open-quantity subquery (position_quantity), the NOT IN of
    #    risk_engine._OPEN_POSITION_WHERE, the monitor/trailing/reconciliation
    #    per-position exit checks. The existing partial exit-owner index only
    #    covers in-flight exits.
    #  * (account_id, mode, status) — the monitor, trailing, reconciliation and
    #    risk queries all filter an account's tickets by mode and status.
    #  * kite_order_id / kite_gtt_id — GET /api/orders and /api/gtts map every
    #    broker id back to its ticket; the cancel routes look one up.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_tickets_closes_ticket ON trade_tickets(closes_ticket_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trade_tickets_account_mode_status ON trade_tickets(account_id, mode, status)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_tickets_kite_order ON trade_tickets(kite_order_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_tickets_kite_gtt ON trade_tickets(kite_gtt_id)")


_OHLC_INTEGRITY_TABLES = """
CREATE TABLE IF NOT EXISTS ohlc_history_status (
    instrument_token INTEGER PRIMARY KEY,
    symbol           TEXT NOT NULL,
    provider         TEXT NOT NULL DEFAULT 'kite',
    first_date       TEXT,
    last_date        TEXT,
    bar_count        INTEGER NOT NULL DEFAULT 0,
    downloaded_at    TEXT,
    refreshed_at     TEXT,
    data_hash        TEXT,
    status           TEXT NOT NULL DEFAULT 'OK' CHECK (status IN ('OK', 'SUSPECT', 'NEEDS_REFRESH')),
    status_reason    TEXT,
    status_at        TEXT,
    verified_gaps    TEXT
);
CREATE TABLE IF NOT EXISTS symbol_transitions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_token INTEGER NOT NULL,
    old_symbol       TEXT NOT NULL,
    new_symbol       TEXT NOT NULL,
    detected_at      TEXT NOT NULL DEFAULT (datetime('now')),
    source           TEXT NOT NULL,
    UNIQUE (instrument_token, old_symbol, new_symbol)
);
"""


def _m004_ohlc_history_integrity(conn: sqlite3.Connection) -> None:
    # PK-22D: per-instrument history metadata and trust status (see
    # backend/ohlc_integrity.py), backfilled for every instrument already
    # stored, with the corporate-action gap scan run once. ohlc_meta keeps
    # its one job (how far back history was requested) unchanged.
    from backend import ohlc_integrity
    _execute_script(conn, _OHLC_INTEGRITY_TABLES)
    ohlc_integrity.backfill_all(conn)


_LIVE_READINESS_TABLE = """
CREATE TABLE IF NOT EXISTS live_readiness (
    account_id             INTEGER PRIMARY KEY REFERENCES accounts(id),
    static_ip              TEXT,
    static_ip_confirmed_at TEXT,
    egress_ip              TEXT,
    egress_checked_at      TEXT,
    egress_error           TEXT,
    updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _m005_live_readiness(conn: sqlite3.Connection) -> None:
    # LIVE-0: the user's attestation of the static IP whitelisted in the Kite
    # developer console, and the last deliberate outbound-IP observation
    # (backend/live_preflight.py). Kite exposes neither through its API.
    # No seed row: absent means "not yet confirmed".
    _execute_script(conn, _LIVE_READINESS_TABLE)


_SEC1_EVENT_TYPES = (
    _ACCOUNT_SAFETY_EVENT_TYPES
    + ", 'reconciliation_reviewed', 'live_switch_refused', 'mode_reset_on_session_change'"
)


def _m006_sec1_live_safety(conn: sqlite3.Connection) -> None:
    # SEC-1: the audit trail of the new live-safety acts is account-level:
    #   - a reconciliation finding marked reviewed (with its reason), which
    #     can unblock live entries;
    #   - a refused switch to live;
    #   - the reset to dry run when the broker session changes.
    # system_events.event_type is a CHECK allow-list, so the table is rebuilt
    # (same pattern as _ensure_account_safety_event_types). The migration
    # runner has already turned foreign keys off and opened the transaction.
    #
    # execution_settings.live_armed_session_id is the broker session
    # (sessions.id) whose readiness check armed LIVE mode. A live new entry
    # is accepted only while that session is still the active one. Plain
    # ALTER TABLE: NULL means "not armed".
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='system_events'").fetchone()
    if row is not None and "'reconciliation_reviewed'" not in row["sql"]:
        conn.execute(
            f"""
            CREATE TABLE system_events_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id  INTEGER NOT NULL REFERENCES accounts(id),
                event_type  TEXT NOT NULL CHECK (event_type IN ({_SEC1_EVENT_TYPES})),
                detail      TEXT,
                triggered_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "INSERT INTO system_events_new (id, account_id, event_type, detail, triggered_at) "
            "SELECT id, account_id, event_type, detail, triggered_at FROM system_events"
        )
        conn.execute("DROP TABLE system_events")
        conn.execute("ALTER TABLE system_events_new RENAME TO system_events")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_system_events_account ON system_events(account_id)")
    if not _column_exists(conn, "execution_settings", "live_armed_session_id"):
        conn.execute("ALTER TABLE execution_settings ADD COLUMN live_armed_session_id INTEGER")


# SEC-1 P1-D: the human review a risk-adding manual / Quick-Action BUY entry
# rests on (backend/account_safety.py, check_ticket_review).
#   reviewed_at          UTC text of the last review (creation, edit or an
#                        explicit Re-review), on the account-safety clock;
#   reviewed_session_id  the sessions.id (Kite login) it happened under;
#   review_digest        sha256 of the ticket's economic fields at its
#                        creation or edit: a later out-of-band change is
#                        tamper evidence, never re-stamped by a Re-review.
# Plain ALTER TABLEs, each guarded (re-running the migration over a schema
# that already has the column must not fail). Existing rows stay NULL, which
# means "not reviewed": an old pending manual ticket needs a Re-review.
_P1D_REVIEW_COLUMNS = (
    ("reviewed_at", "TEXT"),
    ("reviewed_session_id", "INTEGER"),
    ("review_digest", "TEXT"),
)


def _m007_p1d_ticket_review(conn: sqlite3.Connection) -> None:
    for name, sql_type in _P1D_REVIEW_COLUMNS:
        if not _column_exists(conn, "trade_tickets", name):
            conn.execute(f"ALTER TABLE trade_tickets ADD COLUMN {name} {sql_type}")


def _m008_p1d_review_generation(conn: sqlite3.Connection) -> None:
    # SEC-1 P1-D follow-up: review_generation is a fresh random nonce written
    # with every review (creation, edit, Re-review) and bound into the review
    # token. reviewed_at has one-second resolution, so without it two reviews
    # of one ticket in the same second produced the same token and a stale
    # approval dialog stayed valid. Guarded plain ALTER. Existing rows stay
    # NULL: a review recorded without a generation needs a Re-review.
    if not _column_exists(conn, "trade_tickets", "review_generation"):
        conn.execute("ALTER TABLE trade_tickets ADD COLUMN review_generation TEXT")


# DMB research inbox (backend/dmb_inbox.py): Daily Market Byte candidate
# packets the user imported from local handoff files. Research context only:
# nothing here is read by the strategy engine, the risk engine or execution,
# and no row in these tables can create, change or delete a trade ticket.
#   dmb_inbox_imports       every import attempt, accepted or refused;
#   dmb_inbox_items         every packet version ever imported, verbatim, one
#                           row per (account, packet_id); an older version of
#                           the same logical candidate is marked superseded
#                           (once) by a newer one, never removed;
#   dmb_inbox_events        the user's actions (dismiss, note, research
#                           watchlist, start ticket) and import facts;
#   dmb_inbox_ticket_links  inbox item -> the manual ticket a "Start ticket"
#                           led to. ticket_id deliberately has no foreign key:
#                           a link never constrains, cascades into or changes
#                           trade_tickets.
# Imports and events are append-only and items are immutable apart from the
# write-once supersession pointer; triggers refuse anything else.
_DMB_INBOX_TABLES = """
CREATE TABLE IF NOT EXISTS dmb_inbox_imports (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id             INTEGER NOT NULL REFERENCES accounts(id),
    requested_session_date TEXT NOT NULL,
    requested_bundle_id    TEXT NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN ('IMPORTED', 'DUPLICATE', 'REJECTED')),
    reason_code            TEXT,
    reason_detail          TEXT,
    file_sha256            TEXT,
    file_size              INTEGER,
    bundle_schema          TEXT,
    bundle_generated_at    TEXT,
    packet_count           INTEGER,
    new_item_count         INTEGER NOT NULL DEFAULT 0,
    session_id             INTEGER,
    imported_at            TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_dmb_inbox_imports_account ON dmb_inbox_imports(account_id, id);

CREATE TABLE IF NOT EXISTS dmb_inbox_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id            INTEGER NOT NULL REFERENCES accounts(id),
    packet_id             TEXT NOT NULL,
    logical_key           TEXT NOT NULL,
    symbol                TEXT NOT NULL,
    listing_exchange      TEXT NOT NULL CHECK (listing_exchange = 'NSE'),
    session_date          TEXT NOT NULL,
    review_session_date   TEXT NOT NULL,
    evidence_as_of        TEXT NOT NULL,
    evidence_as_of_utc    TEXT NOT NULL,
    generated_at          TEXT NOT NULL,
    generated_at_utc      TEXT NOT NULL,
    data_origin           TEXT NOT NULL,
    rights_status         TEXT NOT NULL,
    bundle_id             TEXT NOT NULL,
    import_id             INTEGER NOT NULL REFERENCES dmb_inbox_imports(id),
    packet_json           TEXT NOT NULL,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_by_item_id INTEGER REFERENCES dmb_inbox_items(id),
    superseded_at         TEXT,
    UNIQUE (account_id, packet_id)
);
CREATE INDEX IF NOT EXISTS idx_dmb_inbox_items_logical ON dmb_inbox_items(account_id, logical_key);

CREATE TABLE IF NOT EXISTS dmb_inbox_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    item_id     INTEGER REFERENCES dmb_inbox_items(id),
    import_id   INTEGER REFERENCES dmb_inbox_imports(id),
    event_type  TEXT NOT NULL CHECK (event_type IN (
                    'IMPORTED', 'SUPERSEDED', 'DISMISSED', 'NOTE_ADDED', 'WATCHLIST_ADDED',
                    'WATCHLIST_REMOVED', 'TICKET_STARTED', 'TICKET_LINKED'
                )),
    detail      TEXT,
    session_id  INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_dmb_inbox_events_item ON dmb_inbox_events(item_id, id);

CREATE TABLE IF NOT EXISTS dmb_inbox_ticket_links (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    item_id        INTEGER NOT NULL REFERENCES dmb_inbox_items(id),
    ticket_id      INTEGER NOT NULL,
    start_event_id INTEGER NOT NULL REFERENCES dmb_inbox_events(id),
    linked_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (ticket_id),
    UNIQUE (start_event_id)
);
CREATE INDEX IF NOT EXISTS idx_dmb_inbox_ticket_links_item ON dmb_inbox_ticket_links(item_id);

CREATE TRIGGER IF NOT EXISTS dmb_inbox_imports_append_only_u BEFORE UPDATE ON dmb_inbox_imports
BEGIN SELECT RAISE(ABORT, 'dmb_inbox_imports is append-only'); END;
CREATE TRIGGER IF NOT EXISTS dmb_inbox_imports_append_only_d BEFORE DELETE ON dmb_inbox_imports
BEGIN SELECT RAISE(ABORT, 'dmb_inbox_imports is append-only'); END;
CREATE TRIGGER IF NOT EXISTS dmb_inbox_events_append_only_u BEFORE UPDATE ON dmb_inbox_events
BEGIN SELECT RAISE(ABORT, 'dmb_inbox_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS dmb_inbox_events_append_only_d BEFORE DELETE ON dmb_inbox_events
BEGIN SELECT RAISE(ABORT, 'dmb_inbox_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS dmb_inbox_items_keep_versions BEFORE DELETE ON dmb_inbox_items
BEGIN SELECT RAISE(ABORT, 'dmb_inbox_items keeps every packet version'); END;
CREATE TRIGGER IF NOT EXISTS dmb_inbox_items_immutable BEFORE UPDATE ON dmb_inbox_items
WHEN OLD.superseded_by_item_id IS NOT NULL
  OR NEW.superseded_by_item_id IS NULL
  OR NEW.account_id IS NOT OLD.account_id OR NEW.packet_id IS NOT OLD.packet_id
  OR NEW.logical_key IS NOT OLD.logical_key OR NEW.symbol IS NOT OLD.symbol
  OR NEW.listing_exchange IS NOT OLD.listing_exchange OR NEW.session_date IS NOT OLD.session_date
  OR NEW.review_session_date IS NOT OLD.review_session_date
  OR NEW.evidence_as_of IS NOT OLD.evidence_as_of OR NEW.evidence_as_of_utc IS NOT OLD.evidence_as_of_utc
  OR NEW.generated_at IS NOT OLD.generated_at OR NEW.generated_at_utc IS NOT OLD.generated_at_utc
  OR NEW.data_origin IS NOT OLD.data_origin OR NEW.rights_status IS NOT OLD.rights_status
  OR NEW.bundle_id IS NOT OLD.bundle_id OR NEW.import_id IS NOT OLD.import_id
  OR NEW.packet_json IS NOT OLD.packet_json OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'a dmb_inbox_items row is immutable apart from its supersession, set once'); END;
"""


def _m009_dmb_research_inbox(conn: sqlite3.Connection) -> None:
    _execute_script(conn, _DMB_INBOX_TABLES)


# Post-claim tampering audit (backend/ticket_integrity.py): append-only, hash-chained claim
# snapshots and tamper detections. No trading table is changed.
_TICKET_INTEGRITY_TABLES = """
CREATE TABLE IF NOT EXISTS ticket_integrity_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    ticket_id       INTEGER NOT NULL,
    event_type      TEXT NOT NULL CHECK (event_type IN ('CLAIM_SNAPSHOT', 'TAMPER_DETECTED')),
    stage           TEXT NOT NULL CHECK (stage IN ('claim', 'post_claim', 'pre_broker', 'post_execution',
                                                   'trailing')),
    claimed_digest  TEXT NOT NULL,
    current_digest  TEXT NOT NULL,
    categories      TEXT NOT NULL,
    detected_at     TEXT NOT NULL,
    prev_hash       TEXT NOT NULL,
    entry_hash      TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_ticket_integrity_ticket ON ticket_integrity_events(ticket_id, id);
CREATE TRIGGER IF NOT EXISTS ticket_integrity_append_only_u BEFORE UPDATE ON ticket_integrity_events
BEGIN SELECT RAISE(ABORT, 'ticket_integrity_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ticket_integrity_append_only_d BEFORE DELETE ON ticket_integrity_events
BEGIN SELECT RAISE(ABORT, 'ticket_integrity_events is append-only'); END;
"""


def _m010_ticket_integrity_audit(conn: sqlite3.Connection) -> None:
    _execute_script(conn, _TICKET_INTEGRITY_TABLES)


# ---- v11: Phase 2 cross-process coordination (docs/PHASE_2_DESIGN_REVIEW.md §5.1, §6.2, §8.1) ----
#
# The fencing epoch and owner identity (app_instance, one row), one history row per acquisition,
# the startup broker-read evidence, the startup reconciliation runs and the persisted per-account
# restriction. Only Phase 2 objects: the protection journal stays dark (Phase 3) and attribution
# tables come with Phase 7. The DDL is the design review's, verbatim. Registering it migrates every
# database on its next start; a v11 database is refused by older builds (DATABASE_SCHEMA_UNSUPPORTED),
# which also fences out builds that take no instance lock. Rollback: rollback_schema_v11 below.
_APP_INSTANCE_DDL = """
CREATE TABLE app_instance (
  id                 INTEGER PRIMARY KEY CHECK (id = 1),
  epoch              INTEGER NOT NULL CHECK (epoch >= 0),          -- monotonic, never reused
  instance_uuid      TEXT,                                         -- uuid4, new for every process start
  pid                INTEGER,                                      -- diagnostic only
  process_created_at TEXT,                                         -- OS process creation time (UTC); with pid = process identity
  host               TEXT,                                         -- socket.gethostname()
  machine_id         TEXT,                                         -- Windows MachineGuid / POSIX /etc/machine-id; diagnostic
  lock_path          TEXT,                                         -- resolved path of the lock file held
  db_path            TEXT,                                         -- resolved DB path at acquisition
  app_version        TEXT, app_commit TEXT,
  state              TEXT NOT NULL CHECK (state IN ('none','starting','owner','stopping','stopped_clean')),
  started_at         TEXT,
  heartbeat_at       TEXT,                                         -- UTC wall clock of the last heartbeat
  heartbeat_seq      INTEGER NOT NULL DEFAULT 0,                   -- +1 per heartbeat; detects a foreign writer
  lease_seconds      INTEGER NOT NULL DEFAULT 60 CHECK (lease_seconds BETWEEN 15 AND 600),
  clean_shutdown     INTEGER NOT NULL DEFAULT 1 CHECK (clean_shutdown IN (0,1)),
  shutdown_at        TEXT,
  shutdown_note      TEXT,                                         -- e.g. 'broker write in flight at exit'
  updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
)
"""
_APP_INSTANCE_SEED = "INSERT INTO app_instance (id, epoch, state) VALUES (1, 0, 'none')"

_APP_INSTANCE_HISTORY_DDL = """
CREATE TABLE app_instance_history (
  epoch               INTEGER PRIMARY KEY,
  instance_uuid       TEXT NOT NULL UNIQUE,
  pid                 INTEGER, process_created_at TEXT, host TEXT, machine_id TEXT,
  lock_path           TEXT, db_path TEXT, app_version TEXT, app_commit TEXT,
  started_at          TEXT NOT NULL,
  ended_at            TEXT,
  end_state           TEXT NOT NULL DEFAULT 'running' CHECK (end_state IN
                        ('running','stopped_clean','crashed_detected','fenced','conflict')),
  previous_epoch      INTEGER,
  previous_end        TEXT,             -- how the predecessor ended, as detected at takeover
  takeover_wait_s     REAL,             -- time spent waiting for the predecessor's lease
  note                TEXT
)
"""

_BROKER_READS_DDL = """
CREATE TABLE broker_reads (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id       INTEGER NOT NULL REFERENCES accounts(id),
  purpose          TEXT NOT NULL CHECK (purpose IN ('startup','reconciliation','prewrite','emergency_exit','resolution')),
  instance_epoch   INTEGER NOT NULL,
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  orders_at TEXT,    orders_error TEXT,    orders_count INTEGER,
  gtts_at TEXT,      gtts_error TEXT,      gtts_count INTEGER,   gtts_malformed INTEGER,
  holdings_at TEXT,  holdings_error TEXT,
  positions_at TEXT, positions_error TEXT,
  session_expired  INTEGER NOT NULL DEFAULT 0 CHECK (session_expired IN (0,1)),
  complete         INTEGER NOT NULL CHECK (complete IN (0,1)),   -- every required endpoint ok and well formed
  CHECK (complete = 0 OR (orders_error IS NULL AND gtts_error IS NULL AND holdings_error IS NULL
                          AND positions_error IS NULL AND gtts_malformed = 0))
)
"""

_STARTUP_RECONCILIATION_RUNS_DDL = """
CREATE TABLE startup_reconciliation_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  instance_epoch INTEGER NOT NULL,
  trigger TEXT NOT NULL CHECK (trigger IN ('startup','login','resume','manual','retry')),
  started_at TEXT NOT NULL, finished_at TEXT,
  stage1_outcome TEXT CHECK (stage1_outcome IN ('passed','incomplete','failed','blocked')),
  broker_read_id INTEGER REFERENCES broker_reads(id),
  inventory TEXT CHECK (inventory IS NULL OR json_valid(inventory)),
  findings TEXT CHECK (findings IS NULL OR json_valid(findings)),
  reasons_after TEXT CHECK (reasons_after IS NULL OR json_valid(reasons_after))
)
"""

# §8.1 offers "columns on account_safety_state, or a new account_restriction table, one row per
# account": the table, so the existing safety table is untouched and a rollback is a clean drop.
# The default is RESTRICTED: a row that was never derived never reads as NORMAL.
_ACCOUNT_RESTRICTION_DDL = """
CREATE TABLE account_restriction (
  account_id          INTEGER PRIMARY KEY REFERENCES accounts(id),
  restriction_state   TEXT NOT NULL DEFAULT 'RESTRICTED' CHECK (restriction_state IN ('NORMAL','RESTRICTED')),
  restriction_reasons TEXT NOT NULL DEFAULT '["STARTUP_RECONCILIATION_INCOMPLETE"]'
                        CHECK (json_valid(restriction_reasons) AND json_type(restriction_reasons) = 'array'),
  restriction_since   TEXT,
  restriction_epoch   INTEGER,
  last_startup_run_id INTEGER REFERENCES startup_reconciliation_runs(id),
  updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (restriction_state = 'RESTRICTED' OR restriction_reasons = '[]')
)
"""

# Creation order (foreign keys point backwards); a rollback drops them in reverse.
PHASE2_TABLES: tuple[tuple[str, str], ...] = (
    ("app_instance", _APP_INSTANCE_DDL),
    ("app_instance_history", _APP_INSTANCE_HISTORY_DDL),
    ("broker_reads", _BROKER_READS_DDL),
    ("startup_reconciliation_runs", _STARTUP_RECONCILIATION_RUNS_DDL),
    ("account_restriction", _ACCOUNT_RESTRICTION_DDL),
)


def _normalised_sql(sql: str | None) -> str:
    """Stored CREATE text compared without comments and whitespace differences."""
    if sql is None:
        return ""
    without_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return " ".join(without_comments.split()).rstrip(";").strip()


def phase2_schema_status(conn: sqlite3.Connection) -> str:
    """Read-only. 'absent' (none of the v11 objects), 'present' (all of them, exactly as v11 creates
    them, with the single app_instance row), 'partial' (some) or 'unexpected' (a same-named object
    in another form)."""
    present = 0
    for name, ddl in PHASE2_TABLES:
        row = conn.execute("SELECT type, sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()
        if row is None:
            continue
        if row[0] != "table" or _normalised_sql(row[1]) != _normalised_sql(ddl):
            return "unexpected"
        present += 1
    if present == 0:
        return "absent"
    if present < len(PHASE2_TABLES):
        return "partial"
    ids = [r[0] for r in conn.execute("SELECT id FROM app_instance").fetchall()]
    return "present" if ids == [1] else "unexpected"


def _schema_object_names(conn: sqlite3.Connection) -> set[str]:
    # SQLite's own objects (sqlite_sequence, the automatic index of a UNIQUE column) are not ours.
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master").fetchall() if not r[0].startswith("sqlite_")}


# Named outside the _mNNN_ pattern: when v11 was written, the 'm011' prefix was reserved for the Phase 1 dark
# journal functions (isolation test 11b). Phase 3 renumbered those to v12; this registered name stays as released.
def _migration_v11_instance_coordination(conn: sqlite3.Connection) -> None:
    status = phase2_schema_status(conn)
    if status == "present":
        # A re-run over an already migrated database (a lost schema_version table, see
        # tests/conftest.as_pre_pk22_database): every object is exactly v11's; keep its data.
        return
    if status != "absent":
        raise DatabaseMigrationFailed(
            f"DATABASE_MIGRATION_FAILED: migration 11 (phase2_instance_coordination) found the Phase 2 "
            f"objects {status} (a partial or foreign form). Nothing was changed; restore a backup or "
            "inspect the database by hand.")
    before = _schema_object_names(conn)
    for _name, ddl in PHASE2_TABLES:
        conn.execute(ddl)
    conn.execute(_APP_INSTANCE_SEED)
    added = _schema_object_names(conn) - before
    if added != {name for name, _ in PHASE2_TABLES} or phase2_schema_status(conn) != "present":
        raise DatabaseMigrationFailed(
            f"DATABASE_MIGRATION_FAILED: migration 11 produced an unexpected schema (added {sorted(added)}).")


# Migration 12's MIGRATIONS entry (registered; docs/PHASE_3_V12_REGISTRATION_REMEDIATION_REPORT.md §3). A normal
# start never applies it to a database holding data: see _AUTHORIZATION_GATED_MIGRATIONS below.
# Migration 12's own function is defined in the protection-journal section further down this module, after
# MIGRATIONS, so the list cannot name it directly (a NameError on import). This wrapper looks the name up only
# when it is called, by which time the whole module has loaded; a test that substitutes that function is reached
# through it too. Named like _migration_v11_instance_coordination, outside the _mNNN_ pattern (isolation test
# 11b allows exactly this one return line to reference a protection-journal function before that section).
def _migration_v12_protection_journal(conn: sqlite3.Connection) -> dict:
    return _m012_protection_journal(conn)


# (version, name, function) — append only; never renumber or rename a
# released migration: the name is checked against every database's history.
MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "baseline_pk0_pk21", _m001_baseline),
    (2, "pk22_drop_legacy_dashboard_index", _m002_drop_legacy_dashboard_index),
    (3, "pk22_query_indexes", _m003_query_indexes),
    (4, "pk22_ohlc_history_integrity", _m004_ohlc_history_integrity),
    (5, "live0_live_readiness", _m005_live_readiness),
    (6, "sec1_live_safety", _m006_sec1_live_safety),
    (7, "p1d_ticket_review", _m007_p1d_ticket_review),
    (8, "p1d_review_generation", _m008_p1d_review_generation),
    (9, "dmb_research_inbox", _m009_dmb_research_inbox),
    (10, "ticket_integrity_audit", _m010_ticket_integrity_audit),
    (11, "phase2_instance_coordination", _migration_v11_instance_coordination),
    (12, "phase3_protection_journal", _migration_v12_protection_journal),
]
LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]

# What the last init_db() in this process did (diagnostics, tests).
last_init: dict = {}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def migration_history(conn: sqlite3.Connection) -> list[dict]:
    if not _table_exists(conn, "schema_version"):
        return []
    return [dict(r) for r in conn.execute(
        "SELECT version, migration_name, applied_at, duration_ms FROM schema_version ORDER BY version"
    ).fetchall()]


def _check_history(history: list[dict]) -> int:
    """The current version, or DatabaseSchemaUnsupported when the history is
    not a prefix of this code's MIGRATIONS."""
    known = {version: name for version, name, _ in MIGRATIONS}
    newest = history[-1] if history else None
    if newest is not None and newest["version"] > LATEST_SCHEMA_VERSION:
        raise DatabaseSchemaUnsupported(
            f"DATABASE_SCHEMA_UNSUPPORTED: the database is at schema version {newest['version']} "
            f"({newest['migration_name']}), newer than this code supports ({LATEST_SCHEMA_VERSION}). "
            "Run the newer version of Kite Terminal, or restore a backup taken for this version."
        )
    for expected, row in enumerate(history, start=1):
        version, name = row["version"], row["migration_name"]
        if version != expected or known.get(version) != name:
            raise DatabaseSchemaUnsupported(
                f"DATABASE_SCHEMA_UNSUPPORTED: schema_version row {version} ({name!r}) does not match this "
                f"code's migration {expected} ({known.get(expected)!r}). The database was migrated by "
                "different code; refusing to guess."
            )
    return newest["version"] if newest else 0


def schema_status(conn: sqlite3.Connection) -> dict:
    """Read-only summary for diagnostics/readiness. Never raises."""
    history = migration_history(conn)
    try:
        current = _check_history(history)
        supported, error = True, None
    except DatabaseSchemaUnsupported as e:
        current = history[-1]["version"] if history else 0
        supported, error = False, str(e)
    pending = [{"version": v, "name": n} for v, n, _ in MIGRATIONS if v > current] if supported else []
    return {
        "current_version": current,
        "latest_version": LATEST_SCHEMA_VERSION,
        "pending": pending,
        "history": history,
        "supported": supported,
        "error": error,
        "up_to_date": supported and not pending,
    }


def _database_file(conn: sqlite3.Connection) -> Path | None:
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main" and row[2]:
            return Path(row[2])
    return None


def _has_user_tables(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone() is not None


def backup_database(conn: sqlite3.Connection, destination: Path) -> Path:
    """Online-backup `conn`'s database to `destination` (never overwritten:
    a timestamp is appended if it exists) and prove the copy is intact."""
    if destination.exists():
        stamp = time.strftime("%Y%m%d%H%M%S")
        destination = destination.with_name(f"{destination.name}.{stamp}")
    target = sqlite3.connect(destination)
    try:
        conn.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        target.close()
    if result != "ok":
        raise DatabaseMigrationFailed(
            f"DATABASE_MIGRATION_FAILED: the pre-migration backup {destination.name} failed its integrity "
            f"check ({result}); nothing was migrated."
        )
    return destination


def _foreign_key_violations(conn: sqlite3.Connection) -> int:
    return len(conn.execute("PRAGMA foreign_key_check").fetchall())


# ---- the migration authorization gate (docs/PHASE_3_V12_AUTHORIZATION_GATE_IMPLEMENTATION_REPORT.md) -----------
#
# Registering a migration (appending it to MIGRATIONS) and migrating the trading database are separate decisions.
# A version listed here is never applied by a normal start (init_db) or by `db_maintenance migrate`: while it is
# pending on a database that holds data, both refuse with DATABASE_MIGRATION_AUTHORIZATION_REQUIRED before any
# write. Only `python -m backend.db_maintenance migrate-v12 --authorize-v12` applies it, passing a
# V12MigrationAuthorization to migrate(). Keyed by VERSION, so a renamed v12 entry is gated all the same. There is
# deliberately no generic authorization: the only key is V12MigrationAuthorization, valid for (12, its name) only.
# A database with no user tables (a missing or brand-new file) holds nothing to protect and is created at the
# latest schema directly (user decision, 2026-11-11).
_AUTHORIZATION_GATED_MIGRATIONS = {12: "phase3_protection_journal"}
V12_MIGRATION_COMMAND = "python -m backend.db_maintenance migrate-v12 --authorize-v12"

# R2-4, approved by the user on 2026-11-11 (docs/PHASE_3_V12_FINAL_REGISTRATION_PROPOSAL.md §3): open executed
# BUY positions with a NULL mode, for every account (risk_engine._OPEN_POSITION_WHERE over mode IS NULL). Such a
# position cannot be journaled, so migration 12 refuses while any exists (db_maintenance preflight-v12 and
# migrate-v12, and again inside v12's own transaction).
V12_PREFLIGHT_OPEN_NULL_MODE_SQL = (
    "SELECT id, account_id, symbol FROM trade_tickets "
    "WHERE status = 'executed' AND mode IS NULL AND transaction_type = 'BUY' AND closed_at IS NULL "
    "AND id NOT IN (SELECT closes_ticket_id FROM trade_tickets WHERE closes_ticket_id IS NOT NULL "
    "AND status = 'executed' AND entry_fill_status IS NULL) ORDER BY id"
)


@dataclass(frozen=True)
class V12MigrationAuthorization:
    """The explicit operator authorization for migration 12, and for nothing else. Constructed only by
    db_maintenance migrate-v12 --authorize-v12 (and by tests, on temporary databases)."""
    issued_by: str
    version: int = 12
    name: str = "phase3_protection_journal"


def _valid_v12_authorization(authorization) -> bool:
    return (type(authorization) is V12MigrationAuthorization
            and authorization.version == 12
            and authorization.name == _AUTHORIZATION_GATED_MIGRATIONS[12]
            and isinstance(authorization.issued_by, str) and bool(authorization.issued_by.strip()))


def _gated_pending(pending) -> list[tuple[int, str]]:
    return [(version, name) for version, name, _ in pending if version in _AUTHORIZATION_GATED_MIGRATIONS]


def _authorization_refusal(current: int, gated: list[tuple[int, str]]) -> DatabaseMigrationAuthorizationRequired:
    versions = ", ".join(f"{version} ({name})" for version, name in gated)
    if current < 11:
        how = (f"The database is at schema version {current}, below 11: bring it to v11 with the Phase 2 build "
               f"(the build before migration 12 was registered) first, then run: {V12_MIGRATION_COMMAND}")
    else:
        how = f"Stop the application and run: {V12_MIGRATION_COMMAND}"
    return DatabaseMigrationAuthorizationRequired(
        f"DATABASE_MIGRATION_AUTHORIZATION_REQUIRED: the database is at schema version {current} and this build "
        f"registers migration {versions}, which a normal start never applies to a database holding data. Nothing "
        f"was changed. {how} (docs/PHASE_3_V12_AUTHORIZATION_GATE_IMPLEMENTATION_REPORT.md).")


def _v12_open_null_mode_buys(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(V12_PREFLIGHT_OPEN_NULL_MODE_SQL).fetchall()]


def require_startup_migration_authorization(path) -> None:
    """Read-only, before init_db writes anything (key, journal mode, backup, schema): refuses with
    DatabaseMigrationAuthorizationRequired while a gated migration is pending on a database that holds data, and
    with DatabaseSchemaUnsupported on a foreign history. A missing file is a fresh install: nothing to check."""
    path = Path(path)
    if not path.exists():
        return
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True,
                           timeout=config.SQLITE_BUSY_TIMEOUT_SECONDS)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        current = _check_history(migration_history(conn))
        gated = _gated_pending([m for m in MIGRATIONS if m[0] > current])
        if gated and _has_user_tables(conn):
            raise _authorization_refusal(current, gated)
    finally:
        conn.close()


def migrate(conn: sqlite3.Connection, *, v12_authorization: V12MigrationAuthorization | None = None,
            v12_transaction_guard=None) -> list[int]:
    """Applies every pending migration; returns the versions applied. A pending migration in
    _AUTHORIZATION_GATED_MIGRATIONS is refused, before any write, unless `v12_authorization` is a valid
    V12MigrationAuthorization (or the database holds no user table yet).

    `v12_transaction_guard` (V12 final readiness, Phase E): a callable(conn) that db_maintenance migrate-v12 passes
    with its authorization. It runs INSIDE the gated migration's own BEGIN IMMEDIATE transaction, under the write
    lock, before any DDL, next to the R2-4 re-check; any exception it raises rolls the migration back. It only
    adds a check: it is never a way round the authorization (it is ignored without a valid one)."""
    current = _check_history(migration_history(conn))  # refuses before any write
    pending = [m for m in MIGRATIONS if m[0] > current]
    if not pending:
        return []
    gated = _gated_pending(pending)
    if gated and _has_user_tables(conn) and not _valid_v12_authorization(v12_authorization):
        raise _authorization_refusal(current, gated)  # before the backup and any write

    target_version = pending[-1][0]
    db_file = _database_file(conn)
    if db_file is not None and _has_user_tables(conn):
        try:
            backup = backup_database(conn, db_file.with_name(f"{db_file.name}.pre_schema_v{target_version}_bak"))
        except MigrationBackupFailed:
            raise
        except Exception as e:  # before any BEGIN: nothing was migrated
            raise MigrationBackupFailed(
                f"DATABASE_MIGRATION_FAILED: the pre-migration backup failed ({type(e).__name__}: {e}); nothing "
                "was migrated.") from e
        logger.info("event=migration.backup schema_from=%d schema_to=%d backup=%s",
                    current, target_version, backup.name)

    applied: list[int] = []
    previous_isolation = conn.isolation_level
    conn.isolation_level = None  # explicit BEGIN/COMMIT below
    try:
        for version, name, apply in pending:
            violations_before = _foreign_key_violations(conn)
            # Table rebuilds need FK enforcement off, and the PRAGMA is a
            # no-op inside a transaction, so it is set around it.
            conn.execute("PRAGMA foreign_keys=OFF")
            started = time.perf_counter()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(SCHEMA_VERSION_DDL)
                if version in _AUTHORIZATION_GATED_MIGRATIONS:
                    # R2-4 re-checked under the write lock, in v12's own transaction (the preflight ran before it).
                    open_null_mode = _v12_open_null_mode_buys(conn)
                    if open_null_mode:
                        raise DatabaseMigrationFailed(
                            f"DATABASE_MIGRATION_FAILED: migration {version} ({name}) refused: open executed BUY "
                            f"ticket(s) {[r['id'] for r in open_null_mode]} have a NULL mode and could never be "
                            "journaled. Nothing was changed; resolve them in Ticket History first.")
                    if v12_transaction_guard is not None and _valid_v12_authorization(v12_authorization):
                        v12_transaction_guard(conn)
                apply(conn)
                violations_after = _foreign_key_violations(conn)
                if violations_after > violations_before:
                    raise DatabaseMigrationFailed(
                        f"DATABASE_MIGRATION_FAILED: migration {version} ({name}) would add "
                        f"{violations_after - violations_before} foreign key violation(s)."
                    )
                duration_ms = int((time.perf_counter() - started) * 1000)
                conn.execute(
                    "INSERT INTO schema_version (version, migration_name, duration_ms) VALUES (?, ?, ?)",
                    (version, name, duration_ms),
                )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                logger.exception("event=migration.failed version=%d name=%s (rolled back)", version, name)
                raise
            finally:
                conn.execute("PRAGMA foreign_keys=ON")
            applied.append(version)
            logger.info("event=migration.applied version=%d name=%s duration_ms=%d", version, name, duration_ms)
        conn.execute("PRAGMA optimize")
    finally:
        conn.isolation_level = previous_isolation
    return applied


def _run_startup_invariants(conn: sqlite3.Connection) -> None:
    """Data-only steps run on every start. None issues DDL, and none writes
    when there is nothing to do (a second start changes no row)."""
    _seed_default_account(conn)
    _ensure_trading_account_type(conn)
    _seed_execution_settings(conn)
    _seed_risk_settings(conn)
    _seed_account_safety_state(conn)
    _seed_dashboard_panels(conn)
    _ensure_watchlist_summary_panel(conn)
    _ensure_watchlist_panels(conn)
    _seed_strategy_configs(conn)
    _ensure_todays_actions_panel(conn)
    _seed_strategy_config_versions(conn)
    _scrub_plaintext_session_tokens(conn)


def init_db(*, fresh_install: "installation_state.FreshInstallConfirmation | None" = None) -> None:
    # First, read-only (V12 final readiness, Phase C): an established installation whose database is missing,
    # zero bytes, empty, corrupt, replaced, moved or mid-maintenance is refused, never re-created as a fresh database
    # (backend/installation_state.py; DatabaseInstallationStateRefused). `fresh_install` is passed only by
    # `db_maintenance init-fresh --confirm-new-installation --acknowledge-prior-installation`; a normal start never.
    assessment = installation_state.require_startup_ok(DB_PATH, fresh_install)
    # Then, read-only: a start never applies a gated migration (v12) to a database holding data; it refuses
    # here before the key can be minted, the journal mode set, a backup taken or anything migrated.
    require_startup_migration_authorization(DB_PATH)
    crypto.ensure_encryption_key()
    conn = get_connection()
    try:
        applied = migrate(conn)
        changes_before = conn.total_changes
        _run_startup_invariants(conn)
        conn.commit()

        # Backfill runs after all schema/seed steps have committed, in its
        # own transaction — it reads trade_tickets/closed_trades, which must
        # already exist by this point.
        from backend import journal
        journal.backfill_closed_trades(conn)
        conn.commit()
        last_init.clear()
        last_init.update({
            "applied_migrations": applied,
            "startup_row_changes": conn.total_changes - changes_before,
            "schema_version": LATEST_SCHEMA_VERSION,
        })
        installation_state.record_after_startup(DB_PATH, assessment, conn)
    finally:
        conn.close()


class SchemaRollbackRefused(RuntimeError):
    error_code = "SCHEMA_ROLLBACK_REFUSED"


def _quoted_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _tables_referencing_phase2(conn: sqlite3.Connection) -> list[str]:
    """Read-only. Every user table outside PHASE2_TABLES with a foreign key into one of them: a
    schema built on top of v11, which must be rolled back before v11 is."""
    phase2 = {name.lower() for name, _ in PHASE2_TABLES}
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
        "ORDER BY name").fetchall()]
    dependants = []
    for table in tables:
        if table.lower() in phase2:
            continue
        # foreign_key_list columns: id, seq, table, from, to, on_update, on_delete, match.
        references = conn.execute(f"PRAGMA foreign_key_list({_quoted_identifier(table)})").fetchall()
        if any(row[2].lower() in phase2 for row in references):
            dependants.append(table)
    return dependants


def rollback_schema_v11(conn: sqlite3.Connection) -> dict:
    """The written down-migration for v11 (docs/PHASE_2_IMPLEMENTATION_REPORT.md, rollback): drops
    exactly the five Phase 2 tables and the schema_version row 11, so a pre-Phase-2 build accepts the
    database again. Every trading table is untouched; only the coordination audit (instance history,
    startup runs, startup broker reads, the persisted restriction) is lost.

    Refused, writing nothing, unless the history is exactly v1..v11 of this code, the Phase 2
    objects are exactly v11's and no other table has a foreign key into them (a dependent schema is
    rolled back first). The caller must hold the instance lock (db_maintenance does). A file
    database is backed up first (verified, <db>.pre_rollback_v11_bak). One transaction: an
    interruption leaves the v11 database as it was."""
    if conn.in_transaction:
        raise SchemaRollbackRefused("SCHEMA_ROLLBACK_REFUSED: the connection has an open transaction.")
    history = [(h["version"], h["migration_name"]) for h in migration_history(conn)]
    expected = [(version, name) for version, name, _ in MIGRATIONS if version <= 11]
    if history != expected:
        newest = history[-1] if history else None
        raise SchemaRollbackRefused(
            f"SCHEMA_ROLLBACK_REFUSED: the schema history must be exactly v1..v11 of this code; it ends at "
            f"{newest}. Nothing was changed.")
    status = phase2_schema_status(conn)
    if status != "present":
        raise SchemaRollbackRefused(
            f"SCHEMA_ROLLBACK_REFUSED: the Phase 2 objects are {status}, not exactly v11's. Nothing was changed.")
    dependants = _tables_referencing_phase2(conn)
    if dependants:
        raise SchemaRollbackRefused(
            f"SCHEMA_ROLLBACK_REFUSED: table(s) {dependants} have foreign keys into the Phase 2 tables; dropping "
            "those would leave them dangling. Roll back the dependent schema(s) first. Nothing was changed.")
    backup = None
    db_file = _database_file(conn)
    if db_file is not None:
        backup = backup_database(conn, db_file.with_name(f"{db_file.name}.pre_rollback_v11_bak"))
    previous_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        violations_before = _foreign_key_violations(conn)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for name, _ddl in reversed(PHASE2_TABLES):
                conn.execute(f"DROP TABLE {name}")
            conn.execute("DELETE FROM schema_version WHERE version = 11")
            if phase2_schema_status(conn) != "absent" or _check_history(migration_history(conn)) != 10:
                raise SchemaRollbackRefused("SCHEMA_ROLLBACK_REFUSED: the rollback did not reach schema v10.")
            if _foreign_key_violations(conn) > violations_before:
                raise SchemaRollbackRefused("SCHEMA_ROLLBACK_REFUSED: the rollback would add foreign key violations.")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            logger.exception("event=schema_rollback.failed version=11 (rolled back)")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
    finally:
        conn.isolation_level = previous_isolation
    logger.warning("event=schema_rollback.applied from=11 to=10 backup=%s", backup.name if backup else None)
    _note_schema_change(conn, "rollback_schema_v11")
    return {"rolled_back_to": 10, "backup": str(backup) if backup else None}


def _note_schema_change(conn: sqlite3.Connection, by: str) -> None:
    """A deliberate schema rollback re-binds the installation record (if there is one), so the next start does not
    take the lower version for a database put back by hand (installation_state SCHEMA_REGRESSED)."""
    db_file = _database_file(conn)
    if db_file is not None:
        installation_state.note_schema_change(db_file, by=by, conn=conn)


# ---- GTT-after-fill Phase 1: the dark protection-journal schema ---------------
#
# docs/PROPOSAL_GTT_AFTER_FILL_LIFECYCLE_REVIEW.md §5.2 and §12 (Phase 1);
# docs/PHASE_1_DARK_SCHEMA_REPORT.md.
#
# REGISTERED as migration 12 (phase3_protection_journal), applied to a database
# holding data only by `db_maintenance migrate-v12 --authorize-v12` and removed
# only by `db_maintenance rollback-schema-v12 --confirm`. The dark entry points
# apply_dark_protection_schema() / rollback_dark_protection_schema() remain for
# scratch v11 databases only: they refuse a v12 history, the account database
# and its backups.
#
# Phase 3 stage S1 amended the objects to docs/PHASE_3_DESIGN_DECISIONS.md
# §4.1 (R6) and renumbered the helpers to v12; registered as v12 by the
# separately authorized registration (DD-2).
#
# Objects, created and removed all-or-nothing in one transaction:
#   protection_operations              write-ahead journal of GTT writes (§5.2.3)
#   protection_operation_resolutions   the evidence of a manual resolution (Q3)
#   idx_protection_ops_one_open_change at most one open create/modify per position
#   idx_protection_ops_one_open_cancel at most one open cancel per (position, target GTT)
#   idx_trade_tickets_unique_gtt       a GTT protects at most one ticket. While
#                                      existing rows share a kite_gtt_id it is not
#                                      created: logged and reported, like
#                                      _ensure_single_exit_owner_index
#   trade_tickets.protection_state     the §3.3 Protection-region state; NULL =
#                                      not tracked by the journal (every existing row)
#   ticket_events event types          the 13 protection_* types of DD-7 (table rebuild)

PROTECTION_OPERATION_STATES = (
    "intent", "sending", "acknowledged", "unknown", "needs_review",
    "confirmed", "adopted", "rejected", "not_sent", "absent_after_settle", "resolved_manual",
)
# Open = may still change what exists at the broker. needs_review is open on
# purpose: it blocks every automated write on the position (§5.2.3). Every
# other state is terminal; a superseded op keeps its state (the overlay, Q1).
PROTECTION_OPEN_OPERATION_STATES = ("intent", "sending", "acknowledged", "unknown", "needs_review")
# States only reachable once the request has left the process (§3.7, M-8).
_PROTECTION_SENT_STATES = ("sending", "acknowledged", "confirmed", "rejected", "unknown", "adopted",
                           "absent_after_settle")
PROTECTION_STATES = (
    "PROTECTION_NOT_REQUIRED", "PROTECTION_PENDING", "PROTECTION_INTENT", "PROTECTION_SUBMITTED",
    "PROTECTION_ACKNOWLEDGED", "PROTECTION_CONFIRMED", "PROTECTION_STALE", "PROTECTION_RESIZE_PENDING",
    "PROTECTION_FAILED", "PROTECTION_UNKNOWN", "PROTECTION_MISMATCH", "PROTECTION_TRIGGERED",
    "PROTECTION_CANCEL_PENDING", "PROTECTION_CANCEL_UNKNOWN", "PROTECTION_NEEDS_REVIEW",
)
PROTECTION_EVENT_TYPES = (
    "protection_pending", "protection_submitted", "protection_confirmed",
    "protection_unknown", "protection_adopted", "protection_cancelled",
    "protection_rejected", "protection_not_sent", "protection_absent_after_settle",
    "protection_needs_review", "protection_superseded", "protection_resolved",
    "protection_late_gtt_appeared",
)


def _sql_list(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


# The R6 §4.1 DDL, character for character apart from layout. Its explanatory
# comments live here, not in the SQL, so the stored schema carries none:
#   mode       [R2-4] copied verbatim from trade_tickets.mode by the DD-5
#              INSERT ... SELECT; the ticket's non-NULL domain is exactly
#              ('dry_run', 'live'), a NULL ticket mode yields no row; never
#              execution_settings.mode.
#   table CHECKs, in order:
#     kind / reason / direction (DD-3, DD-4): one closed reason list per kind;
#       direction is set exactly for a modify, and fixed for four reasons.
#       [R3-1] an attribution_cap op is sent (first send or retry) only while
#       DX classifies it caps_sell_exposure, re-proved at every send; never as
#       none/redundant. Enforced by the engine's CAS, not expressible here.
#     retry bound (DD-4): only cancels and exposure-reducing modifies may be
#       sent more than once.
#     targets and results (Phase 1, kept): modify / cancel name their GTT; a
#       create that was acknowledged, confirmed or adopted knows its GTT id.
#     sent_at (D§4.1, M-8): 'intent' and 'not_sent' have sent nothing, every
#       sent state has; absent_after_settle is a sent create with >= 2
#       complete reads after it.
#     overlay (Q1): the superseded_* triplet is all set or all NULL, and never
#       on 'intent' (an intent that is superseded becomes 'not_sent').
#     resolution (Q3): resolution_id is set exactly for resolved_manual.
#     open vs terminal: resolved_at is NULL exactly while the op is open.
# target_gtt_id / result_gtt_id / linked_gtt_id carry INTEGER affinity like
# trade_tickets.kite_gtt_id, so a paper id ('PAPER-G-<n>', §5.8) is kept as
# text. Not STRICT (P1-Q8). The two-way foreign key with the resolutions table
# is legal in SQLite: the resolution row is inserted first, then the op's CAS
# sets resolution_id.
_PROTECTION_OPEN_SQL = _sql_list(PROTECTION_OPEN_OPERATION_STATES)
_PROTECTION_OPERATIONS_DDL = f"""CREATE TABLE protection_operations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    op_uuid               TEXT NOT NULL UNIQUE,
    account_id            INTEGER NOT NULL REFERENCES accounts(id),
    position_id           INTEGER NOT NULL REFERENCES trade_tickets(id),
    mode                  TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    kind                  TEXT NOT NULL CHECK (kind IN ('create', 'modify', 'cancel')),
    reason                TEXT NOT NULL CHECK (reason IN ('fill', 'user_recreate',
                              'fill_resize', 'exit_fill', 'trailing', 'attribution_cap', 'user_modify',
                              'position_closed', 'supersede_cleanup', 'duplicate_cleanup', 'exit_prepare',
                              'user_cancel')),
    direction             TEXT CHECK (direction IS NULL OR direction IN ('up', 'down', 'tighten', 'loosen')),
    state                 TEXT NOT NULL CHECK (state IN ({_sql_list(PROTECTION_OPERATION_STATES)})),
    basis_filled_quantity INTEGER NOT NULL CHECK (basis_filled_quantity >= 0),
    basis_exits_filled    INTEGER NOT NULL CHECK (basis_exits_filled >= 0),
    requested_quantity    INTEGER NOT NULL CHECK (requested_quantity >= 0),
    request_fingerprint   TEXT NOT NULL CHECK (json_valid(request_fingerprint)),
    request_payload       TEXT NOT NULL CHECK (json_valid(request_payload)),
    target_gtt_id         INTEGER,
    result_gtt_id         INTEGER,
    instance_epoch        INTEGER NOT NULL,
    attempts              INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    complete_reads_after  INTEGER NOT NULL DEFAULT 0 CHECK (complete_reads_after >= 0),
    superseded_at         TEXT,
    superseded_by_kind    TEXT CHECK (superseded_by_kind IS NULL OR
                              superseded_by_kind IN ('emergency_exit', 'exit', 'close', 'user')),
    superseded_by_ref     TEXT,
    resolution_id         INTEGER REFERENCES protection_operation_resolutions(id),
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at               TEXT,
    acknowledged_at       TEXT,
    resolved_at           TEXT,
    last_checked_at       TEXT,
    evidence              TEXT CHECK (evidence IS NULL OR json_valid(evidence)),
    CHECK ((kind = 'create' AND reason IN ('fill', 'user_recreate') AND direction IS NULL)
        OR (kind = 'modify' AND reason IN ('fill_resize', 'exit_fill', 'trailing', 'attribution_cap', 'user_modify')
                            AND direction IS NOT NULL)
        OR (kind = 'cancel' AND reason IN ('position_closed', 'supersede_cleanup', 'duplicate_cleanup',
                                           'exit_prepare', 'user_cancel') AND direction IS NULL)),
    CHECK (reason != 'fill_resize' OR direction = 'up'),
    CHECK (reason NOT IN ('exit_fill', 'attribution_cap') OR direction = 'down'),
    CHECK (reason != 'trailing' OR direction = 'tighten'),
    CHECK (attempts <= 1 OR kind = 'cancel' OR (kind = 'modify' AND direction IN ('down', 'tighten'))),
    CHECK (kind = 'create' OR target_gtt_id IS NOT NULL),
    CHECK (kind != 'create' OR state NOT IN ('acknowledged', 'confirmed', 'adopted') OR result_gtt_id IS NOT NULL),
    CHECK (state NOT IN ('intent', 'not_sent') OR sent_at IS NULL),
    CHECK (state NOT IN ({_sql_list(_PROTECTION_SENT_STATES)}) OR sent_at IS NOT NULL),
    CHECK (state != 'absent_after_settle' OR (kind = 'create' AND sent_at IS NOT NULL AND complete_reads_after >= 2)),
    CHECK ((superseded_at IS NULL) = (superseded_by_kind IS NULL)
       AND (superseded_at IS NULL) = (superseded_by_ref IS NULL)),
    CHECK (NOT (state = 'intent' AND superseded_at IS NOT NULL)),
    CHECK ((state = 'resolved_manual') = (resolution_id IS NOT NULL)),
    CHECK ((state IN ({_PROTECTION_OPEN_SQL})) = (resolved_at IS NULL))
)"""
# At most one open exposure-changing op per position (D§3 Q3, kept).
_PROTECTION_OPS_ONE_OPEN_CHANGE_DDL = (
    "CREATE UNIQUE INDEX idx_protection_ops_one_open_change ON protection_operations(position_id)\n"
    f"  WHERE kind IN ('create', 'modify') AND state IN ({_PROTECTION_OPEN_SQL})"
)
# At most one open cancel per (position, target GTT) (amends D§3 Q3; M-7): an
# Emergency Exit can cancel every known GTT id beside an unresolved change.
_PROTECTION_OPS_ONE_OPEN_CANCEL_DDL = (
    "CREATE UNIQUE INDEX idx_protection_ops_one_open_cancel ON protection_operations(position_id, target_gtt_id)\n"
    f"  WHERE kind = 'cancel' AND state IN ({_PROTECTION_OPEN_SQL})"
)
# One evidence-backed manual resolution per op (Q3): no 'other' outcome; the
# evidence read is a v11 broker_reads row (hence _require_current_schema); the
# engine also requires it complete and taken at resolution time.
_PROTECTION_OPERATION_RESOLUTIONS_DDL = """CREATE TABLE protection_operation_resolutions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id            INTEGER NOT NULL UNIQUE REFERENCES protection_operations(id),
    outcome          TEXT NOT NULL CHECK (outcome IN ('linked_gtt', 'no_app_gtt', 'deleted_in_kite_verified')),
    linked_gtt_id    INTEGER,
    reason           TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    evidence_read_id INTEGER NOT NULL REFERENCES broker_reads(id),
    resolver         TEXT NOT NULL CHECK (length(trim(resolver)) > 0),
    instance_epoch   INTEGER NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((outcome = 'linked_gtt') = (linked_gtt_id IS NOT NULL))
)"""
# (name, CREATE statement) of the objects that exist exactly as written here.
_DARK_SCHEMA_OBJECTS = (
    ("protection_operations", _PROTECTION_OPERATIONS_DDL),
    ("protection_operation_resolutions", _PROTECTION_OPERATION_RESOLUTIONS_DDL),
    ("idx_protection_ops_one_open_change", _PROTECTION_OPS_ONE_OPEN_CHANGE_DDL),
    ("idx_protection_ops_one_open_cancel", _PROTECTION_OPS_ONE_OPEN_CANCEL_DDL),
)
_UNIQUE_GTT_INDEX_DDL = (
    "CREATE UNIQUE INDEX idx_trade_tickets_unique_gtt ON trade_tickets(kite_gtt_id) "
    "WHERE kite_gtt_id IS NOT NULL"
)
_PROTECTION_STATE_COLUMN_DDL = (
    "protection_state TEXT CHECK (protection_state IS NULL OR protection_state IN "
    f"({_sql_list(PROTECTION_STATES)}))"
)
_TICKET_EVENTS_COLUMNS = ("id", "ticket_id", "event_type", "detail", "created_at")
# Appended to (and, on rollback, removed from) the end of the event_type
# allow-list, so a rollback restores the table's stored SQL byte for byte.
_PROTECTION_EVENT_TYPES_SUFFIX = ", " + _sql_list(PROTECTION_EVENT_TYPES)
_EVENT_TYPE_LIST_RE = re.compile(r"CHECK\s*\(\s*event_type\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
_TICKET_EVENTS_HEADER_RE = re.compile(r"""^\s*CREATE\s+TABLE\s+(?:"ticket_events"|ticket_events)(?=[\s(])""",
                                      re.IGNORECASE)

# The account database and every backup beside it. Fixed at import: tests
# redirect DB_PATH, never this.
_ACCOUNT_DB_FILE = Path(__file__).parent / "kite_terminal.db"


class DarkSchemaRefused(RuntimeError):
    """The dark schema was not applied or removed: the database is not in a
    state this code can change safely. Nothing was written."""
    error_code = "DARK_SCHEMA_REFUSED"


def _normalised_sql(sql: str | None) -> str | None:
    return " ".join(sql.split()) if sql is not None else None


def _schema_object(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT type, sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()


def _ticket_events_sql(conn: sqlite3.Connection) -> str:
    row = _schema_object(conn, "ticket_events")
    if row is None or row["type"] != "table":
        raise DarkSchemaRefused("DARK_SCHEMA_REFUSED: ticket_events is missing.")
    return row["sql"]


def _event_type_list_span(sql: str) -> tuple[int, int]:
    """(start, end) of the event_type allow-list's content, trailing blanks
    excluded."""
    matches = list(_EVENT_TYPE_LIST_RE.finditer(sql))
    if len(matches) != 1:
        raise DarkSchemaRefused("DARK_SCHEMA_REFUSED: ticket_events has no single event_type allow-list.")
    content = matches[0].group(1)
    start = matches[0].start(1)
    return start, start + len(content.rstrip())


def _ticket_event_types(sql: str) -> list[str]:
    start, end = _event_type_list_span(sql)
    return re.findall(r"'([^']*)'", sql[start:end])


def dark_protection_schema_status(conn: sqlite3.Connection) -> dict:
    """Read-only. state: 'absent' (none of the objects), 'present' (all of
    them, each exactly as this code creates it; the unique GTT index may be
    missing, see unique_gtt_index), 'partial' (some) or 'unexpected' (an
    object of that name exists in another form)."""
    objects = {name: _schema_object(conn, name) for name, _ in _DARK_SCHEMA_OBJECTS}
    gtt_index = _schema_object(conn, "idx_trade_tickets_unique_gtt")
    column = _column_exists(conn, "trade_tickets", "protection_state")
    events_sql = _ticket_events_sql(conn)
    event_types = _ticket_event_types(events_sql)
    added_events = [t for t in PROTECTION_EVENT_TYPES if t in event_types]

    present = {
        **{name: row is not None for name, row in objects.items()},
        "trade_tickets.protection_state": column,
        "ticket_events.protection_event_types": bool(added_events),
    }
    # A Phase 1-era dark schema (old table, 6-type suffix) is 'unexpected' here.
    mismatched = [name for name, ddl in _DARK_SCHEMA_OBJECTS
                  if objects[name] is not None and _normalised_sql(objects[name]["sql"]) != _normalised_sql(ddl)]
    if gtt_index is not None and _normalised_sql(gtt_index["sql"]) != _normalised_sql(_UNIQUE_GTT_INDEX_DDL):
        mismatched.append("idx_trade_tickets_unique_gtt")
    if column and _normalised_sql(_PROTECTION_STATE_COLUMN_DDL) not in _normalised_sql(
            _schema_object(conn, "trade_tickets")["sql"]):
        mismatched.append("trade_tickets.protection_state")
    if added_events and not events_sql[:_event_type_list_span(events_sql)[1]].endswith(
            _PROTECTION_EVENT_TYPES_SUFFIX):
        mismatched.append("ticket_events.protection_event_types")

    if mismatched:
        state = "unexpected"
    elif all(present.values()):
        state = "present"
    elif gtt_index is None and not any(present.values()):
        state = "absent"
    else:
        state = "partial"
    return {
        "state": state,
        "objects": present,
        "unique_gtt_index": gtt_index is not None,
        "mismatched": mismatched,
    }


def _refuse_account_database(conn: sqlite3.Connection) -> None:
    db_file = _database_file(conn)
    if db_file is None:
        return  # in-memory
    resolved = Path(os.path.normcase(os.path.realpath(db_file)))
    account = Path(os.path.normcase(os.path.realpath(_ACCOUNT_DB_FILE)))
    if resolved.parent == account.parent and resolved.name.startswith(account.name):
        raise DarkSchemaRefused(
            f"DARK_SCHEMA_REFUSED: {db_file.name} is the account database or one of its backups. The dark "
            "protection schema is not authorised on account data."
        )


def _require_no_transaction(conn: sqlite3.Connection, label: str) -> None:
    # Checked first: the backup would wait forever on this connection's own
    # write lock, and BEGIN IMMEDIATE would fail half-way.
    if conn.in_transaction:
        raise DarkSchemaRefused(f"DARK_SCHEMA_REFUSED: {label} needs a connection with no open transaction.")


def _require_current_schema(conn: sqlite3.Connection) -> None:
    current = _check_history(migration_history(conn))  # DatabaseSchemaUnsupported if foreign
    if current != LATEST_SCHEMA_VERSION:
        raise DarkSchemaRefused(
            f"DARK_SCHEMA_REFUSED: the database is at schema version {current}, not {LATEST_SCHEMA_VERSION}. "
            "Run init_db() first."
        )


def _require_dark_history(conn: sqlite3.Connection) -> None:
    """The dark entry points run only on a database whose history is exactly v1..v11 of this code (runbook §6,
    registration-gate item). Before registration this equals _require_current_schema. After registration (LATEST
    12) it still refuses a v12 history, so a dark rollback can never drop the v12 objects and leave schema_version
    row 12 behind: on a v12 database the journal is removed only by rollback_schema_v12."""
    current = _check_history(migration_history(conn))  # DatabaseSchemaUnsupported if foreign or newer than code
    if current != 11:
        hint = ("Run init_db() first." if current < 11 else
                "On a v12 database the journal is created by migration 12 and removed only by rollback_schema_v12.")
        raise DarkSchemaRefused(
            f"DARK_SCHEMA_REFUSED: the database is at schema version {current}, not 11. {hint}")


def _run_dark_step(conn: sqlite3.Connection, label: str, step) -> dict:
    """One transaction, as migrate() runs a migration: foreign keys off around
    it (table rebuilds), BEGIN IMMEDIATE, no new foreign key violations, then
    COMMIT; any exception (BaseException included) rolls everything back."""
    _require_no_transaction(conn, label)
    previous_isolation = conn.isolation_level
    foreign_keys_were_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.isolation_level = None
    try:
        violations_before = _foreign_key_violations(conn)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = step(conn)
            violations_after = _foreign_key_violations(conn)
            if violations_after > violations_before:
                raise DatabaseMigrationFailed(
                    f"DATABASE_MIGRATION_FAILED: {label} would add "
                    f"{violations_after - violations_before} foreign key violation(s)."
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            logger.exception("event=dark_schema.failed step=%s (rolled back)", label)
            raise
        finally:
            conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys_were_on else 'OFF'}")
    finally:
        conn.isolation_level = previous_isolation
    return result


def _backup_before(conn: sqlite3.Connection, suffix: str) -> str | None:
    db_file = _database_file(conn)
    if db_file is None or not _has_user_tables(conn):
        return None
    return backup_database(conn, db_file.with_name(f"{db_file.name}.{suffix}")).name


def _rebuild_ticket_events(conn: sqlite3.Connection, new_sql: str) -> None:
    """Replaces ticket_events with `new_sql` (its CREATE TABLE), keeping every
    row, its indexes and its AUTOINCREMENT high-water mark."""
    columns = tuple(r["name"] for r in conn.execute("PRAGMA table_info(ticket_events)").fetchall())
    if columns != _TICKET_EVENTS_COLUMNS:
        raise DarkSchemaRefused(f"DARK_SCHEMA_REFUSED: ticket_events has unexpected columns {columns}.")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger', 'view') AND tbl_name = 'ticket_events'"
                    ).fetchone() is not None:
        raise DarkSchemaRefused("DARK_SCHEMA_REFUSED: ticket_events has a trigger or view; not rebuilding it.")
    create_sql, renamed = _TICKET_EVENTS_HEADER_RE.subn("CREATE TABLE ticket_events_new", new_sql, count=1)
    if not renamed:
        raise DarkSchemaRefused("DARK_SCHEMA_REFUSED: the ticket_events definition is not in the expected form.")
    indexes = [r["sql"] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'ticket_events' AND sql IS NOT NULL "
        "ORDER BY name"
    ).fetchall()]
    sequence = conn.execute("SELECT rowid, seq FROM sqlite_sequence WHERE name = 'ticket_events'").fetchone()
    rows_before = conn.execute("SELECT COUNT(*) FROM ticket_events").fetchone()[0]
    column_list = ", ".join(_TICKET_EVENTS_COLUMNS)

    conn.execute(create_sql)
    conn.execute(f"INSERT INTO ticket_events_new ({column_list}) SELECT {column_list} FROM ticket_events")
    conn.execute("DROP TABLE ticket_events")
    conn.execute("ALTER TABLE ticket_events_new RENAME TO ticket_events")
    for sql in indexes:
        conn.execute(sql)
    if sequence is not None:
        # The rebuild re-creates this row; put it back where and as it was.
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'ticket_events'")
        conn.execute("INSERT INTO sqlite_sequence (rowid, name, seq) VALUES (?, 'ticket_events', ?)",
                     (sequence["rowid"], sequence["seq"]))

    if conn.execute("SELECT COUNT(*) FROM ticket_events").fetchone()[0] != rows_before:
        raise DatabaseMigrationFailed("DATABASE_MIGRATION_FAILED: the ticket_events rebuild lost rows.")
    if _normalised_sql(_ticket_events_sql(conn)) != _normalised_sql(
            _TICKET_EVENTS_HEADER_RE.sub('CREATE TABLE "ticket_events"', new_sql, count=1)):
        raise DatabaseMigrationFailed("DATABASE_MIGRATION_FAILED: ticket_events was not rebuilt as intended.")


def _duplicate_gtt_ids(conn: sqlite3.Connection) -> list:
    """GTT ids linked to more than one ticket: what idx_trade_tickets_unique_gtt cannot be built over."""
    return [r["kite_gtt_id"] for r in conn.execute(
        "SELECT kite_gtt_id FROM trade_tickets WHERE kite_gtt_id IS NOT NULL "
        "GROUP BY kite_gtt_id HAVING COUNT(*) > 1 ORDER BY kite_gtt_id"
    ).fetchall()]


def _ensure_unique_gtt_index(conn: sqlite3.Connection) -> dict:
    if _schema_object(conn, "idx_trade_tickets_unique_gtt") is not None:
        return {"unique_gtt_index": "present", "duplicate_gtt_ids": []}
    duplicates = _duplicate_gtt_ids(conn)
    if duplicates:
        # Existing data already breaks the invariant. As with the exit-owner
        # index: not created, never blocks, reported for review.
        logger.warning(
            "Not creating idx_trade_tickets_unique_gtt: GTT id(s) %s are linked to more than one ticket — "
            "review them in Ticket History.", duplicates,
        )
        return {"unique_gtt_index": "skipped_duplicate_gtt_ids", "duplicate_gtt_ids": duplicates}
    conn.execute(_UNIQUE_GTT_INDEX_DDL)
    return {"unique_gtt_index": "created", "duplicate_gtt_ids": []}


_V12_NAME = "phase3_protection_journal"
_V12_OBJECT_NAMES = frozenset({name for name, _ in _DARK_SCHEMA_OBJECTS} | {"idx_trade_tickets_unique_gtt"})


def _m012_registered(conn: sqlite3.Connection) -> dict:
    """Migration 12 as MIGRATIONS will run it once registered (M-9, DD-6; Phase 3 S2). Runs inside
    migrate()'s transaction, which takes the backup and rolls everything back on any exception, so it
    takes no backup, opens no transaction and checks no history (that is still v11 here) and, unlike
    the dark entry points, does not refuse the account database: migrating it is what registration is
    for. Registered as MIGRATIONS[11] through _migration_v12_protection_journal."""
    status = dark_protection_schema_status(conn)
    if status["state"] == "present" and status["unique_gtt_index"]:
        # A re-run over an already migrated database (a lost schema_version table), or a scratch
        # database the dark apply already created: every object is exactly v12's; keep its data.
        return {"action": "already_present"}
    if status["state"] == "present":
        raise DatabaseMigrationFailed(
            f"DATABASE_MIGRATION_FAILED: migration 12 ({_V12_NAME}) found the Phase 3 objects present without "
            "idx_trade_tickets_unique_gtt (a dark apply that skipped it over duplicate GTT ids), which is not "
            "exactly v12's schema. Nothing was changed; restore a backup or inspect the database by hand.")
    if status["state"] != "absent":
        raise DatabaseMigrationFailed(
            f"DATABASE_MIGRATION_FAILED: migration 12 ({_V12_NAME}) found the Phase 3 objects {status['state']} "
            f"(a partial or foreign form; mismatched: {status['mismatched']}). Nothing was changed; restore a "
            "backup or inspect the database by hand.")
    duplicates = _duplicate_gtt_ids(conn)  # before any DDL
    if duplicates:
        raise DatabaseMigrationFailed(
            f"DATABASE_MIGRATION_FAILED: migration 12 ({_V12_NAME}) cannot create idx_trade_tickets_unique_gtt: "
            f"GTT id(s) {duplicates} are linked to more than one ticket. Nothing was changed; the database "
            "stays at v11. Review those tickets in Ticket History, then start again.")
    before = _schema_object_names(conn)
    events_sql = _ticket_events_sql(conn)
    _, list_end = _event_type_list_span(events_sql)
    for _name, ddl in _DARK_SCHEMA_OBJECTS:
        conn.execute(ddl)
    conn.execute(f"ALTER TABLE trade_tickets ADD COLUMN {_PROTECTION_STATE_COLUMN_DDL}")
    _rebuild_ticket_events(conn, events_sql[:list_end] + _PROTECTION_EVENT_TYPES_SUFFIX + events_sql[list_end:])
    conn.execute(_UNIQUE_GTT_INDEX_DDL)  # never skipped: the duplicates were refused above
    added = _schema_object_names(conn) - before
    status = dark_protection_schema_status(conn)
    if added != _V12_OBJECT_NAMES or status["state"] != "present" or not status["unique_gtt_index"]:
        raise DatabaseMigrationFailed(
            f"DATABASE_MIGRATION_FAILED: migration 12 ({_V12_NAME}) produced an unexpected schema "
            f"(added {sorted(added)}, state {status['state']}).")
    return {"action": "applied"}


def _m012_protection_journal(conn: sqlite3.Connection, *, registered: bool = True) -> dict:
    """Creates the Phase 3 objects. The bare function (registered=True) is the
    future MIGRATIONS entry: see _m012_registered; every refusal there is a
    DatabaseMigrationFailed. The dark caller passes registered=False: it runs
    inside _run_dark_step's transaction, only after the status check has found
    the objects all absent, and skips (and reports) the unique GTT index while
    existing tickets share a GTT id."""
    if registered:
        try:
            return _m012_registered(conn)
        except DarkSchemaRefused as e:  # a ticket_events shape the rebuild cannot handle
            raise DatabaseMigrationFailed(
                f"DATABASE_MIGRATION_FAILED: migration 12 ({_V12_NAME}) refused the database: {e} "
                "Nothing was changed (the migration is rolled back).") from e
    if dark_protection_schema_status(conn)["state"] != "absent":
        raise DarkSchemaRefused("DARK_SCHEMA_REFUSED: the dark schema changed while it was being applied.")
    events_sql = _ticket_events_sql(conn)
    _, list_end = _event_type_list_span(events_sql)
    for _name, ddl in _DARK_SCHEMA_OBJECTS:
        conn.execute(ddl)
    conn.execute(f"ALTER TABLE trade_tickets ADD COLUMN {_PROTECTION_STATE_COLUMN_DDL}")
    _rebuild_ticket_events(conn, events_sql[:list_end] + _PROTECTION_EVENT_TYPES_SUFFIX + events_sql[list_end:])
    result = _ensure_unique_gtt_index(conn)
    if dark_protection_schema_status(conn)["state"] != "present":
        raise DatabaseMigrationFailed("DATABASE_MIGRATION_FAILED: the dark schema is incomplete after applying.")
    return result


def _r012_protection_journal(conn: sqlite3.Connection) -> dict:
    """Removes exactly the objects _m012 creates. Runs inside _run_dark_step's
    transaction, and only after the checks that they hold no data."""
    events_sql = _ticket_events_sql(conn)
    _, list_end = _event_type_list_span(events_sql)
    list_start = list_end - len(_PROTECTION_EVENT_TYPES_SUFFIX)
    conn.execute("DROP INDEX IF EXISTS idx_trade_tickets_unique_gtt")
    conn.execute("DROP INDEX idx_protection_ops_one_open_cancel")
    conn.execute("DROP INDEX idx_protection_ops_one_open_change")
    conn.execute("DROP TABLE protection_operation_resolutions")  # the child first; foreign keys are off here
    conn.execute("DROP TABLE protection_operations")
    conn.execute("ALTER TABLE trade_tickets DROP COLUMN protection_state")
    _rebuild_ticket_events(conn, events_sql[:list_start] + events_sql[list_end:])
    if dark_protection_schema_status(conn)["state"] != "absent":
        raise DatabaseMigrationFailed("DATABASE_MIGRATION_FAILED: dark schema objects remain after the rollback.")
    return {}


def apply_dark_protection_schema(conn: sqlite3.Connection) -> dict:
    """Creates the dark schema on a fully migrated scratch database. Repeating
    it changes nothing (except creating the unique GTT index once duplicates
    that blocked it are gone). Refuses the account database, a database not
    at LATEST_SCHEMA_VERSION, and any partial or foreign form of the objects."""
    _refuse_account_database(conn)
    _require_no_transaction(conn, "apply_dark_protection_schema")
    _require_dark_history(conn)
    status = dark_protection_schema_status(conn)
    if status["state"] in ("partial", "unexpected"):
        raise DarkSchemaRefused(
            f"DARK_SCHEMA_REFUSED: the dark schema is {status['state']} ({status}); not touching it."
        )
    if status["state"] == "present" and status["unique_gtt_index"]:
        return {"action": "already_present", "unique_gtt_index": "present", "duplicate_gtt_ids": [],
                "backup": None}
    backup = _backup_before(conn, "pre_dark_protection_schema_bak")
    if status["state"] == "absent":
        result = _run_dark_step(conn, "apply_dark_protection_schema",
                                lambda c: _m012_protection_journal(c, registered=False))
        action = "applied"
    else:
        result = _run_dark_step(conn, "apply_dark_protection_schema", _ensure_unique_gtt_index)
        action = "already_present"
    logger.info("event=dark_schema.applied action=%s unique_gtt_index=%s", action, result["unique_gtt_index"])
    return {"action": action, **result, "backup": backup}


def _protection_held_data(conn: sqlite3.Connection) -> dict:
    """Read-only, on a database whose Phase 3 objects are present: what a
    rollback of them would delete."""
    return {
        "protection_operations rows": conn.execute("SELECT COUNT(*) FROM protection_operations").fetchone()[0],
        # Counted on its own: a resolution row is data even if its op is gone.
        "protection_operation_resolutions rows": conn.execute(
            "SELECT COUNT(*) FROM protection_operation_resolutions").fetchone()[0],
        "trade_tickets.protection_state values": conn.execute(
            "SELECT COUNT(*) FROM trade_tickets WHERE protection_state IS NOT NULL").fetchone()[0],
        "protection_* ticket_events": conn.execute(
            f"SELECT COUNT(*) FROM ticket_events WHERE event_type IN ({_sql_list(PROTECTION_EVENT_TYPES)})"
        ).fetchone()[0],
    }


def rollback_dark_protection_schema(conn: sqlite3.Connection) -> dict:
    """Removes the dark schema and nothing else. Refuses while any of it holds
    data (a journal row, a resolution row, a protection_state, a protection_*
    event of the suffix), so no application data can be deleted; repeating it
    changes nothing."""
    _refuse_account_database(conn)
    _require_no_transaction(conn, "rollback_dark_protection_schema")
    _require_dark_history(conn)
    status = dark_protection_schema_status(conn)
    if status["state"] == "absent":
        return {"action": "already_absent", "backup": None}
    if status["state"] != "present":
        raise DarkSchemaRefused(
            f"DARK_SCHEMA_REFUSED: the dark schema is {status['state']} ({status}); not touching it."
        )
    held = _protection_held_data(conn)
    if any(held.values()):
        raise DarkSchemaRefused(
            f"DARK_SCHEMA_REFUSED: the dark schema holds data ({held}); a rollback would delete it."
        )
    backup = _backup_before(conn, "pre_dark_protection_rollback_bak")
    _run_dark_step(conn, "rollback_dark_protection_schema", _r012_protection_journal)
    logger.info("event=dark_schema.rolled_back")
    return {"action": "rolled_back", "backup": backup}


def v12_schema_problems(conn: sqlite3.Connection, *, empty_journal: bool) -> list[str]:
    """Read-only, for db_maintenance migrate-v12's verification: why the database's Phase 3 objects are not exactly
    v12's (unique GTT index included). With empty_journal (right after migration 12), also any data they hold: a
    journal row, a resolution row, a protection_state value or a protection_* event. [] = verified."""
    try:
        status = dark_protection_schema_status(conn)
    except DarkSchemaRefused as e:
        return [str(e)]
    problems = []
    if status["state"] != "present" or not status["unique_gtt_index"]:
        problems.append(f"the v12 objects are {status['state']} (unique GTT index "
                        f"{'present' if status['unique_gtt_index'] else 'missing'}; mismatched {status['mismatched']})")
    elif empty_journal:
        held = _protection_held_data(conn)
        if any(held.values()):
            problems.append(f"the v12 objects hold data right after migration 12 ({held})")
    return problems


V12_TABLE_NAMES = ("protection_operations", "protection_operation_resolutions")


def v12_objects_state(conn: sqlite3.Connection) -> str:
    """Read-only, for db_maintenance (failure classification, rollback verification): 'absent', 'present',
    'partial' or 'unexpected' for the Phase 3 objects."""
    return dark_protection_schema_status(conn)["state"]


def rollback_schema_v12(conn: sqlite3.Connection) -> dict:
    """The written down-migration for v12 (docs/PHASE_3_DESIGN_DECISIONS.md M-9; Phase 3 S2): drops
    exactly the Phase 3 objects (via _r012_protection_journal) and the schema_version row 12, so a
    Phase 2 (v11) build accepts the database again. Every trading table and every v11 table
    (broker_reads included) is untouched.

    Refused, writing nothing and taking no backup, unless this code registers v12 as
    phase3_protection_journal, the history is exactly v1..v12 of this code, the Phase 3 objects are
    exactly v12's (unique GTT index included) and they hold no data: no journal row, no resolution row
    (an orphan one included), no protection_state value, no protection_* event. Once they hold data,
    the way back is restoring a verified backup. On a build without v12 it always refuses. Exposed
    as `python -m backend.db_maintenance rollback-schema-v12 --confirm`.

    The caller must hold the instance lock. A file database is backed up first (verified,
    <db>.pre_rollback_v12_bak). One transaction: an interruption leaves the v12 database as it was."""
    if conn.in_transaction:
        raise SchemaRollbackRefused("SCHEMA_ROLLBACK_REFUSED: the connection has an open transaction.")
    history = [(h["version"], h["migration_name"]) for h in migration_history(conn)]
    expected = [(version, name) for version, name, _ in MIGRATIONS if version <= 12]
    # The length and name checks matter: on a build without v12, `expected` is v1..v11 and a v11
    # database would match it.
    if len(expected) != 12 or expected[-1] != (12, _V12_NAME) or history != expected:
        newest = history[-1] if history else None
        registered = len(expected) == 12 and expected[-1] == (12, _V12_NAME)
        raise SchemaRollbackRefused(
            f"SCHEMA_ROLLBACK_REFUSED: the schema history must be exactly v1..v12 of this code; it ends at "
            f"{newest}{'' if registered else ' and this build does not register migration 12'}. "
            "Nothing was changed.")
    try:
        status = dark_protection_schema_status(conn)
    except DarkSchemaRefused as e:
        raise SchemaRollbackRefused(f"SCHEMA_ROLLBACK_REFUSED: {e} Nothing was changed.") from e
    if status["state"] != "present" or not status["unique_gtt_index"]:
        raise SchemaRollbackRefused(
            f"SCHEMA_ROLLBACK_REFUSED: the Phase 3 objects are {status['state']} (unique GTT index "
            f"{'present' if status['unique_gtt_index'] else 'missing'}), not exactly v12's. Nothing was changed.")
    held = _protection_held_data(conn)
    if any(held.values()):
        raise SchemaRollbackRefused(
            f"SCHEMA_ROLLBACK_REFUSED: the Phase 3 objects hold data ({held}); a rollback would delete it. "
            "Restore a verified backup instead. Nothing was changed.")
    backup = None
    db_file = _database_file(conn)
    if db_file is not None:
        try:
            backup = backup_database(conn, db_file.with_name(f"{db_file.name}.pre_rollback_v12_bak"))
        except MigrationBackupFailed:
            raise
        except Exception as e:  # before any BEGIN: nothing was changed
            raise MigrationBackupFailed(
                f"DATABASE_MIGRATION_FAILED: the pre-rollback backup failed ({type(e).__name__}: {e}); nothing "
                "was rolled back.") from e
    previous_isolation = conn.isolation_level
    foreign_keys_were_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.isolation_level = None
    try:
        violations_before = _foreign_key_violations(conn)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-checked under the write lock: nothing may have been written since the checks above.
            if any(_protection_held_data(conn).values()):
                raise SchemaRollbackRefused("SCHEMA_ROLLBACK_REFUSED: the Phase 3 objects gained data; nothing "
                                            "was changed.")
            _r012_protection_journal(conn)
            conn.execute("DELETE FROM schema_version WHERE version = 12")
            if (_check_history(migration_history(conn)) != 11
                    or dark_protection_schema_status(conn)["state"] != "absent"):
                raise SchemaRollbackRefused("SCHEMA_ROLLBACK_REFUSED: the rollback did not reach schema v11.")
            if _foreign_key_violations(conn) > violations_before:
                raise SchemaRollbackRefused("SCHEMA_ROLLBACK_REFUSED: the rollback would add foreign key violations.")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            logger.exception("event=schema_rollback.failed version=12 (rolled back)")
            raise
        finally:
            conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys_were_on else 'OFF'}")
    finally:
        conn.isolation_level = previous_isolation
    logger.warning("event=schema_rollback.applied from=12 to=11 backup=%s", backup.name if backup else None)
    _note_schema_change(conn, "rollback_schema_v12")
    return {"rolled_back_to": 11, "backup": str(backup) if backup else None}
