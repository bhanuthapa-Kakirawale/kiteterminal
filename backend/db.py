import json
import logging
import os
import sqlite3
from pathlib import Path

from backend import crypto

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
CREATE UNIQUE INDEX IF NOT EXISTS idx_dashboard_panels_account_type ON dashboard_panels(account_id, panel_type);

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
    conn.execute(
        """
        INSERT OR IGNORE INTO risk_settings (account_id)
        SELECT id FROM accounts WHERE account_type = 'trading'
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
                INSERT OR IGNORE INTO strategy_configs (account_id, strategy_name, enabled, params)
                VALUES (?, ?, 0, ?)
                """,
                (account["id"], strategy_name, json.dumps(default_params)),
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
    conn.executescript(_PAPER_TABLES)


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


def init_db() -> None:
    crypto.ensure_encryption_key()
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
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
        _ensure_submission_tracking_columns(conn)
        _ensure_filled_quantity_column(conn)
        _ensure_single_exit_owner_index(conn)
        _ensure_closed_trades_one_per_position_index(conn)
        _ensure_instrument_tick_size_column(conn)
        _ensure_watchlist_tag_column(conn)
        _ensure_dashboard_panels_screen_column(conn)
        _ensure_reconciliation_issue_columns(conn)
        _ensure_paper_tables(conn)
        conn.executescript(_ACCOUNT_ID_INDEXES)
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
        conn.commit()

        # Backfill runs after all schema/seed steps have committed, in its
        # own transaction — it reads trade_tickets/closed_trades, which must
        # already exist by this point.
        from backend import journal
        journal.backfill_closed_trades(conn)
        conn.commit()
    finally:
        conn.close()
