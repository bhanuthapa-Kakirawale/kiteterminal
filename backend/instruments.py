import logging
import sqlite3

from kiteconnect import KiteConnect

from backend import config, market
from backend.db import get_connection

logger = logging.getLogger(__name__)


def refresh_instrument_cache(kite: KiteConnect) -> int:
    """Rebuilds instrument_cache from Kite's NSE instrument master, filtered
    to NSE equities. Full replace rather than incremental diffing — the
    instrument master doesn't need it, and this keeps the logic simple."""
    rows = [
        row
        for row in kite.instruments("NSE")
        if row.get("segment") == "NSE" and row.get("instrument_type") == "EQ"
    ]

    conn = get_connection()
    try:
        conn.execute("DELETE FROM instrument_cache")
        conn.executemany(
            """
            INSERT INTO instrument_cache
                (instrument_token, tradingsymbol, name, exchange, segment, instrument_type, last_price, tick_size)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["instrument_token"],
                    row["tradingsymbol"],
                    row.get("name"),
                    row.get("exchange", "NSE"),
                    row.get("segment"),
                    row.get("instrument_type"),
                    row.get("last_price"),
                    row.get("tick_size"),
                )
                for row in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("Refreshed instrument cache: %d NSE equities", len(rows))
    return len(rows)


def refresh_instrument_cache_if_stale(
    kite: KiteConnect, max_age_hours: float = config.INSTRUMENT_CACHE_MAX_AGE_HOURS
) -> bool:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT MAX(refreshed_at) AS latest,
                   (julianday('now') - julianday(MAX(refreshed_at))) * 24 AS age_hours
            FROM instrument_cache
            """
        ).fetchone()
    finally:
        conn.close()

    is_stale = row["latest"] is None or row["age_hours"] is None or row["age_hours"] > max_age_hours
    if not is_stale:
        return False

    refresh_instrument_cache(kite)
    return True


def search_instruments(query: str, limit: int = 20) -> list[dict]:
    query = query.strip().upper()
    if len(query) < 2:
        return []

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT instrument_token, tradingsymbol, name, exchange
            FROM instrument_cache
            WHERE tradingsymbol LIKE ? OR name LIKE ?
            ORDER BY tradingsymbol
            LIMIT ?
            """,
            (f"{query}%", f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_tick_size(symbol: str, exchange: str = "NSE", conn: sqlite3.Connection | None = None) -> float:
    """`conn`: reuse the caller's connection — a per-position loop otherwise
    opens one connection per position (PK-22G, measured in reconciliation)."""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        row = conn.execute(
            "SELECT tick_size FROM instrument_cache WHERE tradingsymbol = ? AND exchange = ?",
            (symbol.strip().upper(), exchange),
        ).fetchone()
    finally:
        if own:
            conn.close()
    tick = row["tick_size"] if row is not None else None
    return tick if tick and tick > 0 else market.DEFAULT_TICK_SIZE


def get_instrument_by_symbol(symbol: str, exchange: str = "NSE"):
    """Authoritative server-side symbol -> instrument resolution. Ticket
    creation must always resolve through this — a client-sent
    instrument_token is never trusted."""
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM instrument_cache WHERE tradingsymbol = ? AND exchange = ?",
            (symbol.strip().upper(), exchange),
        ).fetchone()
    finally:
        conn.close()
