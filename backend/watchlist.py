from kiteconnect import KiteConnect

from backend.db import get_connection


def list_watchlist(account_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT w.*,
                   i.sma_200, i.ema_20, i.ema_50, i.rsi_14, i.atr_14,
                   i.avg_volume_20, i.donchian_high_20, i.donchian_high_55,
                   i.date AS indicators_as_of
            FROM watchlist w
            LEFT JOIN indicator_snapshot i ON i.instrument_token = w.instrument_token
            WHERE w.account_id = ?
            ORDER BY w.added_at
            """,
            (account_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_to_watchlist(account_id: int, symbol: str, instrument_token: int, notes: str | None) -> dict:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO watchlist (account_id, symbol, instrument_token, notes)
            VALUES (?, ?, ?, ?)
            """,
            (account_id, symbol, instrument_token, notes),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM watchlist WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def remove_from_watchlist(account_id: int, watchlist_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM watchlist WHERE id = ? AND account_id = ?", (watchlist_id, account_id)
        )
        conn.commit()
    finally:
        conn.close()


def watchlist_instrument_tokens(account_id: int) -> list[int]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT instrument_token FROM watchlist WHERE account_id = ?", (account_id,)
        ).fetchall()
        return [r["instrument_token"] for r in rows]
    finally:
        conn.close()


def symbols_in_holdings_or_watchlist(kite: KiteConnect, account_id: int) -> list[tuple[int, str]]:
    """Union of (instrument_token, symbol) currently held or watched, for the
    daily refresh job and startup catch-up — deduplicated by instrument_token."""
    seen: dict[int, str] = {}
    for h in kite.holdings():
        token = h.get("instrument_token")
        if token:
            seen[token] = h["tradingsymbol"]

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT instrument_token, symbol FROM watchlist WHERE account_id = ?", (account_id,)
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        seen[r["instrument_token"]] = r["symbol"]

    return list(seen.items())
