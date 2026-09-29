"""NSE session clock and price-tick helpers shared by execution, data sync
and the automation scheduler.

The calendar itself (holidays included) lives in backend/trading_calendar.py
(PK-22C); these historical names delegate to it, so every caller — and every
test that pins market.is_market_open — keeps working unchanged."""

import math
from datetime import date, datetime

from backend import trading_calendar
from backend.trading_calendar import IST, MARKET_CLOSE, MARKET_OPEN  # noqa: F401  (re-exported)

DEFAULT_TICK_SIZE = 0.05


def now_ist() -> datetime:
    return trading_calendar.now_ist()


def as_ist(at: datetime | None) -> datetime:
    return trading_calendar.as_ist(at)


def is_trading_day(d: date) -> bool:
    """A regular NSE session day: a weekday that is not an exchange holiday."""
    return trading_calendar.is_trading_day(d)


def is_market_open(at: datetime | None = None) -> bool:
    return trading_calendar.is_market_session_open(at)


def last_complete_session_date(at: datetime | None = None) -> date:
    """Most recent trading day whose daily candle is final."""
    return trading_calendar.latest_completed_session(at)


def round_to_tick(price: float, tick_size: float | None = DEFAULT_TICK_SIZE, mode: str = "nearest") -> float:
    """Snap a price onto the exchange tick grid — Kite rejects order and GTT
    prices that aren't an exact multiple of the instrument's tick size."""
    tick = tick_size if tick_size and tick_size > 0 else DEFAULT_TICK_SIZE
    steps = price / tick
    if mode == "up":
        n = math.ceil(steps - 1e-9)
    elif mode == "down":
        n = math.floor(steps + 1e-9)
    else:
        n = math.floor(steps + 0.5)
    return round(n * tick, 2)
