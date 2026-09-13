import logging

import pandas as pd

from backend.db import get_connection

logger = logging.getLogger(__name__)


def sma(series: pd.Series, period: int) -> pd.Series:
    """Standard simple moving average of the given series."""
    return series.rolling(window=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Standard exponential moving average; span=period with adjust=False
    matches the conventional recursive EMA definition (today = alpha*price +
    (1-alpha)*yesterday, alpha = 2/(period+1))."""
    return series.ewm(span=period, adjust=False).mean()


def avg_volume(volume: pd.Series, period: int) -> pd.Series:
    """Simple moving average of volume over the given period."""
    return volume.rolling(window=period).mean()


def donchian_high_excl_today(close: pd.Series, period: int) -> pd.Series:
    """Donchian high = highest CLOSE over the trailing `period` days,
    EXCLUDING the current day itself — shift(1) before the rolling window
    means day D's value only ever looks at D-1 .. D-period, so "is today
    breaking out" can be compared against prior history without the current
    close leaking into its own comparison level."""
    return close.shift(1).rolling(window=period).max()


def wilders_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI, Wilder's original smoothing method (Wilder, "New Concepts in
    Technical Trading Systems", 1978) — not the simplified SMA-of-gains
    approximation some libraries use. Wilder's recursive smoothing is
    mathematically identical to an exponential moving average with
    alpha = 1/period (adjust=False reproduces the recursive formula exactly,
    rather than pandas' default reweighted/adjusted EWM)."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss  # avg_loss == 0 -> rs == inf -> rsi == 100, handled correctly by float division
    return 100 - (100 / (1 + rs))


def wilders_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR, Wilder's smoothing of True Range. True Range = max(high-low,
    abs(high-prev_close), abs(low-prev_close)), per Wilder's original
    definition; smoothed the same way as RSI above (alpha=1/period,
    adjust=False)."""
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _last_or_none(series: pd.Series, min_len: int) -> float | None:
    if len(series.dropna()) == 0 or len(series) < min_len:
        return None
    value = series.iloc[-1]
    return float(value) if pd.notna(value) else None


def load_ohlc_frame(instrument_token: int) -> pd.DataFrame:
    """All cached daily candles for one instrument, ordered by date — the
    shared read path used by both the live indicator cache writer below and
    the strategy engine / backtester (Phase 5), so there is exactly one
    query building the OHLC dataframe everything else operates on."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM ohlc_daily "
            "WHERE instrument_token = ? ORDER BY date",
            (instrument_token,),
        ).fetchall()
    finally:
        conn.close()
    return pd.DataFrame([dict(r) for r in rows])


def compute_and_store(instrument_token: int, symbol: str) -> bool:
    df = load_ohlc_frame(instrument_token)
    if len(df) < 2:
        return False  # not enough history yet to compute anything meaningful

    latest_date = df["date"].iloc[-1]

    sma_200 = _last_or_none(sma(df["close"], 200), 200)
    ema_20 = _last_or_none(ema(df["close"], 20), 20)
    ema_50 = _last_or_none(ema(df["close"], 50), 50)
    rsi_14 = _last_or_none(wilders_rsi(df["close"], 14), 15)
    atr_14 = _last_or_none(wilders_atr(df, 14), 15)
    avg_volume_20 = _last_or_none(avg_volume(df["volume"], 20), 20)
    donchian_high_20 = _last_or_none(donchian_high_excl_today(df["close"], 20), 21)
    donchian_high_55 = _last_or_none(donchian_high_excl_today(df["close"], 55), 56)

    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO indicator_snapshot
                (instrument_token, symbol, date, sma_200, ema_20, ema_50, rsi_14, atr_14,
                 avg_volume_20, donchian_high_20, donchian_high_55, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(instrument_token) DO UPDATE SET
                symbol = excluded.symbol,
                date = excluded.date,
                sma_200 = excluded.sma_200,
                ema_20 = excluded.ema_20,
                ema_50 = excluded.ema_50,
                rsi_14 = excluded.rsi_14,
                atr_14 = excluded.atr_14,
                avg_volume_20 = excluded.avg_volume_20,
                donchian_high_20 = excluded.donchian_high_20,
                donchian_high_55 = excluded.donchian_high_55,
                computed_at = excluded.computed_at
            """,
            (
                instrument_token, symbol, latest_date, sma_200, ema_20, ema_50, rsi_14, atr_14,
                avg_volume_20, donchian_high_20, donchian_high_55,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("Computed indicators for %s (%d) through %s", symbol, instrument_token, latest_date)
    return True
