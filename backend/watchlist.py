import logging

from kiteconnect import KiteConnect

from backend.db import get_connection

logger = logging.getLogger(__name__)


def list_watchlist(account_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT w.*,
                   i.sma_200, i.ema_20, i.ema_50, i.rsi_14, i.atr_14,
                   i.avg_volume_20, i.donchian_high_20, i.donchian_high_55,
                   i.date AS indicators_as_of,
                   (SELECT o.close FROM ohlc_daily o
                    WHERE o.instrument_token = w.instrument_token
                    ORDER BY o.date DESC LIMIT 1) AS last_close
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


def seed_live_quotes(kite: KiteConnect, rows: list[dict]) -> list[dict]:
    """Fills in last_price/day_change_percentage from a one-time REST call so
    a row shows a real number on first paint — before this, a row's price
    came ONLY from a live WebSocket tick, and Kite's ticker pushes a price
    for a token only once on subscribe (a snapshot easily missed if no
    browser is listening yet, e.g. right at backend startup) or when the
    price next changes. That silent gap is why the Watchlist could show
    every row blank until an unrelated add/remove forced a full resubscribe
    through an already-connected browser. This REST seed makes the first
    render correct regardless of ticker timing; live ticks still keep it
    updating afterward exactly as before."""
    # watchlist rows have no exchange column (the whole app only ever
    # resolves/watches NSE equities — see instruments.get_instrument_by_symbol's
    # default), so "NSE:<symbol>" is the same key used everywhere else.
    if not rows:
        return rows
    keys = [f"NSE:{r['symbol']}" for r in rows]
    try:
        quotes = kite.ltp(keys)
    except Exception:
        logger.exception("Failed to seed live LTP for watchlist (%d symbols)", len(rows))
        return rows
    for r in rows:
        q = quotes.get(f"NSE:{r['symbol']}")
        if not q:
            continue
        last_price = q.get("last_price")
        r["last_price"] = last_price
        if last_price is not None and r.get("last_close"):
            r["day_change_percentage"] = (last_price - r["last_close"]) / r["last_close"] * 100
    return rows


def add_to_watchlist(
    account_id: int, symbol: str, instrument_token: int, notes: str | None, tag: str | None = None
) -> dict:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO watchlist (account_id, symbol, instrument_token, notes, tag)
            VALUES (?, ?, ?, ?, ?)
            """,
            (account_id, symbol, instrument_token, notes, tag),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM watchlist WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def set_tag(account_id: int, watchlist_id: int, tag: str | None) -> dict | None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE watchlist SET tag = ? WHERE id = ? AND account_id = ?",
            (tag, watchlist_id, account_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM watchlist WHERE id = ? AND account_id = ?", (watchlist_id, account_id)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def distinct_tags(account_id: int) -> list[str]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT tag FROM watchlist WHERE account_id = ? AND tag IS NOT NULL AND tag != '' "
            "ORDER BY tag COLLATE NOCASE",
            (account_id,),
        ).fetchall()
        return [r["tag"] for r in rows]
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
