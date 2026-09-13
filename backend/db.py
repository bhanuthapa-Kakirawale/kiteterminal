import json
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

CREATE TABLE IF NOT EXISTS trade_tickets (
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


def _seed_risk_settings(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO risk_settings (account_id)
        SELECT id FROM accounts WHERE account_type = 'trading'
        """
    )


# Mirrors strategy_engine.DEFAULT_PARAMS — duplicated here (rather than
# imported) so db.py has no dependency on strategy_engine.py at import time;
# strategy_engine.py's copy is the one actually used to evaluate signals,
# this is only the one-time seed value for a brand-new account's row.
_DEFAULT_MOMENTUM_BREAKOUT_PARAMS = {
    "donchian_lookback": 20,
    "volume_multiple": 1.5,
    "trend_filter_period": 200,
    "atr_stop_multiple": 2,
    "target_r_multiple": 2,
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
    "atr_stop_multiple": 1.5,
    "target_r_multiple": 2,
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
        _ensure_stop_adjusted_breakeven_event_type(conn)
        _ensure_starting_capital_column(conn)
        _ensure_strategy_version_column(conn)
        _ensure_closed_trades_strategy_version_column(conn)
        _ensure_strategy_configs_versioning_columns(conn)
        _ensure_backtest_runs_rigor_columns(conn)
        conn.executescript(_ACCOUNT_ID_INDEXES)
        _seed_default_account(conn)
        _ensure_trading_account_type(conn)
        _seed_execution_settings(conn)
        _seed_risk_settings(conn)
        _seed_dashboard_panels(conn)
        _ensure_watchlist_summary_panel(conn)
        _seed_strategy_configs(conn)
        _ensure_todays_actions_panel(conn)
        _seed_strategy_config_versions(conn)
        conn.commit()

        # Backfill runs after all schema/seed steps have committed, in its
        # own transaction — it reads trade_tickets/closed_trades, which must
        # already exist by this point.
        from backend import journal
        journal.backfill_closed_trades(conn)
        conn.commit()
    finally:
        conn.close()
