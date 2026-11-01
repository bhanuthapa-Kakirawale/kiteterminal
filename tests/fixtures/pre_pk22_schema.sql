-- Synthetic pre-PK-22 database schema (DDL only; no data, no account details).
--
-- Exactly what the last code BEFORE PK-22 (commit 6446c79, the parent of
-- a9e7083 "chore: harden migrations workers and trading calendar")
-- produced: that commit's own backend/db.py init_db() was run twice (a
-- restarted install) on a brand-new temporary database and every
-- sqlite_master object was dumped in creation order. No real database or
-- backup was opened to produce it.
--
-- It replaces the earlier test dependency on the developer's local
-- backend/kite_terminal.db.pre_pk22_bak (real account data). It matches
-- migration v1 (_m001_baseline) object for object, plus the legacy
-- idx_dashboard_panels_account_type index that pre-PK-22 code re-created on
-- every start and that migration v2 exists to drop.
--
-- To regenerate: `git archive 6446c79 | tar -x -C <tmp>/code`, then with
-- <tmp>/code first on sys.path, point backend.db.DB_PATH and
-- backend.crypto.ENV_PATH at a NEW temp file (with dummy
-- KITE_TERMINAL_ENCRYPTION_KEY / KITE_API_KEY / KITE_API_SECRET), call
-- backend.db.init_db() twice, and write each
-- `SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid`
-- followed by ";". Never point it at a real database.

CREATE TABLE accounts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    label            TEXT NOT NULL,
    api_key          TEXT NOT NULL,
    api_secret_enc   TEXT NOT NULL,
    access_token_enc TEXT,
    account_type     TEXT NOT NULL DEFAULT 'investment' CHECK (account_type IN ('investment', 'trading')),
    is_default       INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    access_token    TEXT NOT NULL,
    public_token    TEXT,
    user_id         TEXT,
    user_name       TEXT,
    login_time      TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    is_active       INTEGER NOT NULL DEFAULT 1
, account_id INTEGER REFERENCES accounts(id));

CREATE TABLE holdings_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    tradingsymbol TEXT NOT NULL, exchange TEXT, isin TEXT,
    quantity INTEGER, t1_quantity INTEGER, realised_quantity INTEGER,
    average_price REAL, last_price REAL, close_price REAL,
    pnl REAL, day_change REAL, day_change_percentage REAL,
    collateral_quantity INTEGER, collateral_type TEXT
, account_id INTEGER REFERENCES accounts(id));

CREATE INDEX idx_holdings_snapshot_batch ON holdings_snapshot(snapshot_id);

CREATE TABLE positions_snapshot (
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
, account_id INTEGER REFERENCES accounts(id));

CREATE INDEX idx_positions_snapshot_batch ON positions_snapshot(snapshot_id);

CREATE TABLE mf_holdings_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    folio TEXT, fund TEXT, tradingsymbol TEXT,
    average_price REAL, last_price REAL, last_price_date TEXT,
    quantity REAL, pnl REAL, pledged_quantity REAL
, account_id INTEGER REFERENCES accounts(id));

CREATE INDEX idx_mf_holdings_snapshot_batch ON mf_holdings_snapshot(snapshot_id);

CREATE TABLE trade_tickets (
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
, rule_checklist TEXT, entry_evaluation TEXT, kite_chart_url TEXT, kite_chart_status TEXT, strategy_version INTEGER, order_variety TEXT, entry_fill_status TEXT, filled_price REAL, filled_at TEXT, signal_date TEXT, order_tag TEXT, submission_state TEXT, submitted_at TEXT, filled_quantity INTEGER CHECK (filled_quantity IS NULL OR (filled_quantity >= 0 AND filled_quantity <= quantity)));

CREATE INDEX idx_trade_tickets_account ON trade_tickets(account_id);

CREATE INDEX idx_trade_tickets_status ON trade_tickets(status);

CREATE TABLE instrument_cache (
    instrument_token INTEGER PRIMARY KEY,
    tradingsymbol    TEXT NOT NULL,
    name             TEXT,
    exchange         TEXT NOT NULL,
    segment          TEXT,
    instrument_type  TEXT,
    last_price       REAL,
    refreshed_at     TEXT NOT NULL DEFAULT (datetime('now'))
, tick_size REAL);

CREATE INDEX idx_instrument_cache_symbol ON instrument_cache(tradingsymbol);

CREATE TABLE execution_settings (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    mode       TEXT NOT NULL DEFAULT 'dry_run' CHECK (mode IN ('dry_run', 'live')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE risk_settings (
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
, starting_capital REAL, max_order_notional REAL CHECK (max_order_notional IS NULL OR max_order_notional > 0), max_new_entries_per_day INTEGER DEFAULT 3 CHECK (max_new_entries_per_day IS NULL OR max_new_entries_per_day > 0), max_entry_price_deviation_pct REAL DEFAULT 3 CHECK (max_entry_price_deviation_pct IS NULL OR (max_entry_price_deviation_pct > 0 AND max_entry_price_deviation_pct <= 100)), max_daily_loss_amount REAL CHECK (max_daily_loss_amount IS NULL OR max_daily_loss_amount > 0), max_daily_loss_pct REAL CHECK (max_daily_loss_pct IS NULL OR (max_daily_loss_pct > 0 AND max_daily_loss_pct <= 100)), broker_breaker_threshold INTEGER DEFAULT 3 CHECK (broker_breaker_threshold IS NULL OR broker_breaker_threshold > 0), broker_breaker_window_minutes INTEGER NOT NULL DEFAULT 10 CHECK (broker_breaker_window_minutes > 0));

CREATE UNIQUE INDEX idx_risk_settings_account ON risk_settings(account_id);

CREATE TABLE sector_mapping (
    symbol   TEXT PRIMARY KEY,
    sector   TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE dashboard_panels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    panel_type    TEXT NOT NULL,
    display_order INTEGER NOT NULL,
    size          TEXT NOT NULL DEFAULT 'medium' CHECK (size IN ('small', 'medium', 'large')),
    visible       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
, screen TEXT NOT NULL DEFAULT 'dashboard');

CREATE TABLE risk_settings_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    field_changed TEXT NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    changed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_risk_settings_history_account ON risk_settings_history(account_id);

CREATE TABLE watchlist (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       INTEGER NOT NULL REFERENCES accounts(id),
    symbol           TEXT NOT NULL,
    instrument_token INTEGER NOT NULL,
    added_at         TEXT NOT NULL DEFAULT (datetime('now')),
    notes            TEXT
, tag TEXT);

CREATE UNIQUE INDEX idx_watchlist_account_symbol ON watchlist(account_id, symbol);

CREATE TABLE ohlc_daily (
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

CREATE UNIQUE INDEX idx_ohlc_daily_token_date ON ohlc_daily(instrument_token, date);

CREATE TABLE indicator_snapshot (
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

CREATE TABLE strategy_configs (
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

CREATE UNIQUE INDEX idx_strategy_configs_account_name ON strategy_configs(account_id, strategy_name);

CREATE TABLE strategy_config_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name   TEXT NOT NULL,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    version         INTEGER NOT NULL,
    params_snapshot TEXT NOT NULL,
    effective_from  TEXT NOT NULL DEFAULT (datetime('now')),
    effective_to    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX idx_strategy_config_versions_unique ON strategy_config_versions(account_id, strategy_name, version);

CREATE TABLE backtest_runs (
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
, execution_config TEXT, metrics TEXT, equity_curve TEXT, skipped_signals TEXT, warnings TEXT, universe_source TEXT, survivorship_bias TEXT);

CREATE TABLE walk_forward_windows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_run_id INTEGER NOT NULL REFERENCES backtest_runs(id),
    window_index    INTEGER NOT NULL,
    window_start    TEXT NOT NULL,
    window_end      TEXT NOT NULL,
    stats           TEXT NOT NULL
);

CREATE INDEX idx_walk_forward_windows_run ON walk_forward_windows(backtest_run_id);

CREATE TABLE backtest_trades (
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
, strategy_name TEXT, signal_date TEXT, planned_entry_price REAL, quantity INTEGER, initial_stop REAL, initial_target REAL, gross_pnl REAL, entry_costs REAL, exit_costs REAL, costs REAL, net_pnl REAL, holding_days INTEGER, bars_held INTEGER, net_r_multiple REAL, gap_fill INTEGER, forced_close INTEGER, data_warning TEXT);

CREATE INDEX idx_backtest_trades_run ON backtest_trades(backtest_run_id);

CREATE TABLE closed_trades (
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

CREATE UNIQUE INDEX idx_closed_trades_sell_ticket ON closed_trades(sell_ticket_id);

CREATE INDEX idx_closed_trades_account_mode ON closed_trades(account_id, mode);

CREATE TABLE stop_adjustment_proposals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id      INTEGER NOT NULL REFERENCES trade_tickets(id),
    current_stop   REAL NOT NULL,
    proposed_stop  REAL NOT NULL,
    reason         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'rejected')),
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at    TEXT
);

CREATE INDEX idx_stop_adjustment_ticket ON stop_adjustment_proposals(ticket_id);

CREATE TABLE account_safety_state (
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

CREATE TABLE broker_failure_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
    source      TEXT NOT NULL,
    detail      TEXT
);

CREATE INDEX idx_broker_failure_log_account ON broker_failure_log(account_id, occurred_at);

CREATE TABLE reconciliation_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER REFERENCES trade_tickets(id),
    check_type  TEXT NOT NULL CHECK (check_type IN ('order_status', 'position_state')),
    expected    TEXT,
    actual      TEXT,
    mismatch    INTEGER NOT NULL DEFAULT 1,
    resolved    INTEGER NOT NULL DEFAULT 0,
    detail      TEXT,
    checked_at  TEXT NOT NULL DEFAULT (datetime('now'))
, issue_code TEXT, severity TEXT, symbol TEXT, gtt_id INTEGER, expected_quantity INTEGER, broker_quantity INTEGER, position_state TEXT, suggested_action TEXT);

CREATE INDEX idx_reconciliation_log_resolved ON reconciliation_log(resolved);

CREATE TABLE automation_runs (
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

CREATE INDEX idx_automation_runs_account_date ON automation_runs(account_id, run_date);

CREATE TABLE ohlc_meta (
    instrument_token        INTEGER PRIMARY KEY,
    history_requested_from  TEXT NOT NULL
);

CREATE TABLE "ticket_events" (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id   INTEGER NOT NULL REFERENCES trade_tickets(id),
            event_type  TEXT NOT NULL CHECK (event_type IN (
                            'created', 'confirmed', 'rejected', 'executed', 'execution_failed',
                            'protection_failed', 'modified', 'trailing_adjusted', 'closed', 'risk_rejected',
                            'archived', 'stop_adjusted_breakeven', 'submission_unknown'
                        )),
            detail      TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

CREATE INDEX idx_ticket_events_ticket ON ticket_events(ticket_id);

CREATE TABLE "system_events" (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id  INTEGER NOT NULL REFERENCES accounts(id),
            event_type  TEXT NOT NULL CHECK (event_type IN ('mode_switched', 'cancel_all_pending', 'strategy_disabled', 'strategy_enabled', 'emergency_exit_all', 'gtt_cancelled_manually', 'order_cancelled_manually', 'kite_chart_unresolved', 'trading_locked', 'trading_unlocked', 'account_safety_refusal', 'broker_breaker_tripped', 'broker_breaker_reset', 'amo_entry_approved')),
            detail      TEXT,
            triggered_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

CREATE INDEX idx_system_events_account ON system_events(account_id);

CREATE UNIQUE INDEX idx_trade_tickets_one_exit_owner ON trade_tickets(closes_ticket_id) WHERE closes_ticket_id IS NOT NULL AND (status = 'confirmed' OR (status = 'executed' AND COALESCE(entry_fill_status, '') IN ('pending', 'unconfirmed')));

CREATE UNIQUE INDEX idx_closed_trades_buy_ticket ON closed_trades(buy_ticket_id);

CREATE UNIQUE INDEX idx_dashboard_panels_account_screen_type ON dashboard_panels(account_id, screen, panel_type);

CREATE INDEX idx_reconciliation_log_issue ON reconciliation_log(issue_code, gtt_id);

CREATE TABLE paper_orders (
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

CREATE INDEX idx_paper_orders_account_status ON paper_orders(account_id, status);

CREATE UNIQUE INDEX idx_paper_orders_tag ON paper_orders(tag) WHERE tag IS NOT NULL;

CREATE TABLE paper_gtts (
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

CREATE INDEX idx_paper_gtts_account_status ON paper_gtts(account_id, status);

CREATE INDEX idx_sessions_account ON sessions(account_id);

CREATE INDEX idx_holdings_snapshot_account ON holdings_snapshot(account_id);

CREATE INDEX idx_positions_snapshot_account ON positions_snapshot(account_id);

CREATE INDEX idx_mf_holdings_snapshot_account ON mf_holdings_snapshot(account_id);

CREATE UNIQUE INDEX idx_dashboard_panels_account_type ON dashboard_panels(account_id, panel_type);

