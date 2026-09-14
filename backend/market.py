"""NSE session clock and price-tick helpers shared by execution, data sync
and the automation scheduler."""

import math
from datetime import date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
DEFAULT_TICK_SIZE = 0.05


def now_ist() -> datetime:
    return datetime.now(IST)


def as_ist(at: datetime | None) -> datetime:
    if at is None:
        return now_ist()
    if at.tzinfo is None:
        return at.replace(tzinfo=IST)
    return at.astimezone(IST)


def is_trading_day(d: date) -> bool:
    # Weekends only — Kite exposes no exchange-holiday calendar. On a holiday
    # the broker rejects regular orders, and the automation pipeline skips
    # itself because no new daily candle gets published.
    return d.weekday() < 5


def is_market_open(at: datetime | None = None) -> bool:
    at = as_ist(at)
    return is_trading_day(at.date()) and MARKET_OPEN <= at.time() < MARKET_CLOSE


def last_complete_session_date(at: datetime | None = None) -> date:
    """Most recent trading day whose daily candle is final."""
    at = as_ist(at)
    d = at.date()
    if is_trading_day(d) and at.time() >= MARKET_CLOSE:
        return d
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


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
