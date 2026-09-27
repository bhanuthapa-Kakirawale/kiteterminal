from typing import Literal

from pydantic import BaseModel, Field


class SessionStatus(BaseModel):
    connected: bool
    user_name: str | None = None
    login_time: str | None = None
    account_type: str | None = None


class ApiError(BaseModel):
    error_code: str
    message: str


class TicketCreateRequest(BaseModel):
    # No `source`/`strategy_name` field on purpose: every HTTP-created ticket
    # is stamped source='manual' server-side, so a browser client can never
    # claim to be the (future) strategy engine.
    symbol: str
    exchange: str = "NSE"
    transaction_type: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    entry_price: float = Field(gt=0)
    entry_order_type: Literal["MARKET", "LIMIT"]
    stop_loss: float = Field(gt=0)
    target_1: float = Field(gt=0)
    target_2: float | None = None
    trailing_enabled: bool = False
    trailing_trigger: Literal["after_target_1", "manual"] | None = None
    trailing_distance: float | None = Field(default=None, gt=0)
    product_type: Literal["CNC", "MIS", "NRML"]
    use_gtt: bool = True
    rationale: str = Field(min_length=1)


class AlertActionSellRequest(BaseModel):
    # Quick Action popup's sell action (Dashboard per-stock alert -> Sell to
    # cut loss / reduce exposure). No stop_loss/target_1/product_type/source
    # fields on purpose: those are derived server-side from the broker's own
    # holdings data, never trusted from the client (routes/tickets.py's
    # alert-action sell endpoint). transaction_type is implicitly SELL.
    symbol: str
    exchange: str = "NSE"
    quantity: int = Field(gt=0)
    entry_order_type: Literal["MARKET", "LIMIT"]
    entry_price: float = Field(gt=0)
    rationale: str = Field(min_length=1)
    alert_type: Literal["breach_loss", "breach_concentration"]


class TicketRejectRequest(BaseModel):
    reason: str | None = None


class TicketConfirmRequest(BaseModel):
    # PK-11 (P1-7): the execution mode the user SAW when approving. Required
    # for a new entry (the server refuses a missing or different mode); an
    # exit ignores it and runs in its position's own mode (PK-3).
    expected_mode: Literal["dry_run", "live"] | None = None
    # PK-12H: explicit consent to send a live NEW entry as an After-Market
    # Order when the market is closed. Absent/false outside market hours is
    # refused (AMO_CONFIRMATION_REQUIRED); never inferred from the clock.
    allow_amo: bool = False


class TradingLockRequest(BaseModel):
    # PK-12A: the explicit "no new trades" switch.
    locked: bool
    reason: str | None = Field(default=None, max_length=500)


class BreakerResetRequest(BaseModel):
    # PK-12G: closing an open broker circuit breaker is a logged human act.
    reason: str | None = Field(default=None, max_length=500)


class TicketEditRequest(BaseModel):
    # Same shape as TicketCreateRequest — a full replace, only ever allowed
    # server-side when the ticket's current status is 'draft'.
    symbol: str
    exchange: str = "NSE"
    transaction_type: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    entry_price: float = Field(gt=0)
    entry_order_type: Literal["MARKET", "LIMIT"]
    stop_loss: float = Field(gt=0)
    target_1: float = Field(gt=0)
    target_2: float | None = None
    trailing_enabled: bool = False
    trailing_trigger: Literal["after_target_1", "manual"] | None = None
    trailing_distance: float | None = Field(default=None, gt=0)
    product_type: Literal["CNC", "MIS", "NRML"]
    use_gtt: bool = True
    rationale: str = Field(min_length=1)


class ExecutionModeRequest(BaseModel):
    mode: Literal["dry_run", "live"]
    confirm_real_money: bool = False


class RiskSettingsUpdateRequest(BaseModel):
    max_risk_per_trade_pct: float = Field(gt=0, le=100)
    max_capital_per_trade_pct: float = Field(gt=0, le=100)
    max_open_positions: int = Field(gt=0)
    max_single_stock_exposure_pct: float = Field(gt=0, le=100)
    max_sector_exposure_pct: float = Field(gt=0, le=100)
    max_total_portfolio_risk_pct: float = Field(gt=0, le=100)
    min_risk_reward: float = Field(gt=0)
    # Phase 6: optional fixed baseline for the Journal's equity-curve/
    # drawdown calculation. None = fall back to portfolio value at the
    # time of the first closed trade (see journal.get_starting_capital).
    starting_capital: float | None = Field(default=None, gt=0)
    # PK-12 account-level limits. null = disabled. A field the client leaves
    # OUT is kept as it is (routes/risk.py), so an older tab that does not
    # know these fields can never silently switch a safety limit off.
    max_order_notional: float | None = Field(default=None, gt=0)
    max_new_entries_per_day: int | None = Field(default=None, gt=0)
    max_entry_price_deviation_pct: float | None = Field(default=None, gt=0, le=100)
    max_daily_loss_amount: float | None = Field(default=None, gt=0)
    max_daily_loss_pct: float | None = Field(default=None, gt=0, le=100)
    broker_breaker_threshold: int | None = Field(default=None, gt=0)
    broker_breaker_window_minutes: int | None = Field(default=None, gt=0)


# PK-12 fields of RiskSettingsUpdateRequest: only written when explicitly sent.
ACCOUNT_SAFETY_SETTING_FIELDS = (
    "max_order_notional", "max_new_entries_per_day", "max_entry_price_deviation_pct",
    "max_daily_loss_amount", "max_daily_loss_pct", "broker_breaker_threshold", "broker_breaker_window_minutes",
)


class SectorMappingUpsertRequest(BaseModel):
    symbol: str
    sector: str = Field(min_length=1)


class MomentumBreakoutParams(BaseModel):
    donchian_lookback: int = Field(gt=0)
    volume_multiple: float = Field(gt=0)
    trend_filter_period: int = Field(gt=0)
    atr_stop_multiple: float = Field(gt=0)
    target_r_multiple: float = Field(gt=0)
    trailing_atr_multiple: float = Field(gt=0)
    thesis_invalidation_ma: int = Field(gt=0)
    time_exit_days: int = Field(gt=0)
    breakeven_trigger_r: float = Field(gt=0)
    max_signals_per_day: int = Field(gt=0)


class TrendPullbackParams(BaseModel):
    trend_filter_period: int = Field(gt=0)
    pullback_ma_period: int = Field(gt=0)
    pullback_band_pct: float = Field(gt=0)
    pullback_lookback_days: int = Field(gt=0)
    volume_multiple: float = Field(gt=0)
    atr_stop_multiple: float = Field(gt=0)
    target_r_multiple: float = Field(gt=0)
    trailing_atr_multiple: float = Field(gt=0)
    thesis_invalidation_ma: int = Field(gt=0)
    time_exit_days: int = Field(gt=0)
    breakeven_trigger_r: float = Field(gt=0)
    max_signals_per_day: int = Field(gt=0)


class RsiMeanReversionParams(BaseModel):
    trend_filter_period: int = Field(gt=0)
    rsi_oversold_threshold: float = Field(gt=0, lt=100)
    rsi_turn_confirmation: bool
    swing_low_lookback_days: int = Field(gt=0)
    atr_stop_multiple: float = Field(gt=0)
    target_r_multiple: float = Field(gt=0)
    rsi_exit_level: float = Field(gt=0, lt=100)
    thesis_invalidation_ma: int = Field(gt=0)
    time_exit_days: int = Field(gt=0)
    breakeven_trigger_r: float = Field(gt=0)
    max_signals_per_day: int = Field(gt=0)


# Dispatch used by routes/strategies.py to validate an incoming params dict
# against the right pydantic schema for a given strategy_name — each
# strategy owns its own params shape (Phase 7 Part A.1), so a single
# generic dict type would accept typos/wrong-shaped params silently.
STRATEGY_PARAMS_MODELS: dict[str, type[BaseModel]] = {
    "momentum_breakout": MomentumBreakoutParams,
    "trend_pullback": TrendPullbackParams,
    "rsi_mean_reversion": RsiMeanReversionParams,
}


class StrategyConfigUpdateRequest(BaseModel):
    enabled: bool
    params: dict  # validated against STRATEGY_PARAMS_MODELS[strategy_name] in the route


class EmergencyExitConfirmRequest(BaseModel):
    confirmation_phrase: str


class BacktestRequest(BaseModel):
    strategy_name: str
    start_date: str
    end_date: str
    symbols: list[str] | None = None  # None = every symbol currently on the watchlist
    params: dict | None = None  # None = the account's saved params for this strategy
    # Phase 8 Part D: opt-in rigor add-ons, off by default so a plain
    # backtest call isn't slowed down or randomized unless asked for.
    run_walk_forward: bool = False
    window_days: int = Field(default=90, gt=0)
    run_monte_carlo: bool = False
    mc_iterations: int = Field(default=1000, gt=0)
    mc_random_seed: int | None = None
