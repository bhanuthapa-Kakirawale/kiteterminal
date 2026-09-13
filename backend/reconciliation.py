import asyncio
import logging
import sqlite3

from backend import config, kite_client, risk_engine
from backend.accounts import get_default_account
from backend.db import get_connection

logger = logging.getLogger(__name__)


async def run_reconciliation_loop() -> None:
    """Same infinite-loop-+-sleep shape as trailing_engine.run_trailing_loop
    (Phase 3) / data_jobs.run_daily_refresh_loop (Phase 4.5). Only checks
    the trading account (Part B.4 — investment accounts have no tickets to
    reconcile against)."""
    while True:
        try:
            await asyncio.to_thread(_run_cycle)
        except Exception:
            logger.exception("Reconciliation cycle failed")
        await asyncio.sleep(config.RECONCILIATION_INTERVAL_SECONDS)


def _run_cycle() -> None:
    conn = get_connection()
    try:
        account = get_default_account(conn)
        if account["account_type"] != "trading":
            return
        try:
            kite = kite_client.get_kite()
        except kite_client.NoActiveSessionError:
            return

        _check_order_status(conn, kite, account)
        _check_gtt_state(conn, kite, account)
        _check_position_state(conn, kite, account)
        conn.commit()
    finally:
        conn.close()


def _already_logged(conn: sqlite3.Connection, ticket_id: int | None, check_type: str, actual: str) -> bool:
    # Don't re-log the identical unresolved finding every cycle — only a
    # NEW/changed actual value (or a still-different one) gets a fresh row.
    # This keeps the log meaningful without needing the user to dismiss the
    # same finding repeatedly while it hasn't changed.
    row = conn.execute(
        """
        SELECT 1 FROM reconciliation_log
        WHERE (ticket_id IS ? OR (ticket_id IS NULL AND ? IS NULL))
          AND check_type = ? AND actual = ? AND resolved = 0
        LIMIT 1
        """,
        (ticket_id, ticket_id, check_type, actual),
    ).fetchone()
    return row is not None


def _log_mismatch(conn: sqlite3.Connection, ticket_id: int | None, check_type: str, expected: str, actual: str, detail: str) -> None:
    if _already_logged(conn, ticket_id, check_type, actual):
        return
    conn.execute(
        """
        INSERT INTO reconciliation_log (ticket_id, check_type, expected, actual, mismatch, detail)
        VALUES (?, ?, ?, ?, 1, ?)
        """,
        (ticket_id, check_type, expected, actual, detail),
    )
    logger.warning("Reconciliation mismatch (%s): expected=%r actual=%r — %s", check_type, expected, actual, detail)


def _check_order_status(conn: sqlite3.Connection, kite, account) -> None:
    tickets = conn.execute(
        "SELECT id, symbol, quantity, kite_order_id FROM trade_tickets "
        "WHERE account_id = ? AND status = 'executed' AND kite_order_id IS NOT NULL",
        (account["id"],),
    ).fetchall()
    for t in tickets:
        try:
            history = kite.order_history(t["kite_order_id"])
        except Exception:
            logger.exception("Could not fetch order_history for order %s", t["kite_order_id"])
            continue
        if not history:
            continue
        latest = history[-1]
        status = latest.get("status")
        filled_qty = latest.get("filled_quantity")

        if status != "COMPLETE" or (filled_qty is not None and filled_qty != t["quantity"]):
            _log_mismatch(
                conn, t["id"], "order_status",
                expected=f"COMPLETE, filled_quantity={t['quantity']}",
                actual=f"{status}, filled_quantity={filled_qty}",
                detail=f"{t['symbol']}: order {t['kite_order_id']} does not match expected fill state",
            )


def _check_gtt_state(conn: sqlite3.Connection, kite, account) -> None:
    # One batch call, not per-ticket — deliberately duplicates part of what
    # trailing_engine.py already detects on its own tick: a second,
    # independent check catching anything that loop's own cycle might have
    # missed, not wasted work. Logged under check_type='order_status' since
    # the reconciliation_log schema only distinguishes order-side vs
    # position-side checks, and a GTT is fundamentally an order construct.
    tickets = conn.execute(
        "SELECT id, symbol, kite_gtt_id FROM trade_tickets "
        "WHERE account_id = ? AND status = 'executed' AND kite_gtt_id IS NOT NULL AND closed_at IS NULL",
        (account["id"],),
    ).fetchall()
    if not tickets:
        return
    try:
        gtts = kite.get_gtts()
    except Exception:
        logger.exception("Could not fetch get_gtts()")
        return
    active_gtt_ids = {g["id"] for g in gtts}

    for t in tickets:
        if t["kite_gtt_id"] not in active_gtt_ids:
            _log_mismatch(
                conn, t["id"], "order_status",
                expected=f"GTT {t['kite_gtt_id']} active",
                actual="GTT not found in kite.get_gtts()",
                detail=f"{t['symbol']}: GTT disappeared — trailing_engine should detect this on its own next "
                       f"tick and close the position; this is an independent, earlier flag of the same event.",
            )


def _check_position_state(conn: sqlite3.Connection, kite, account) -> None:
    expected_by_symbol: dict[str, int] = {}
    for pos in risk_engine.list_open_positions(conn, account["id"]):
        expected_by_symbol[pos["symbol"]] = expected_by_symbol.get(pos["symbol"], 0) + pos["quantity"]
    if not expected_by_symbol:
        return

    try:
        holdings = kite.holdings()
        positions = kite.positions().get("net", [])
    except Exception:
        logger.exception("Could not fetch holdings()/positions() for position-state check")
        return

    actual_by_symbol: dict[str, int] = {}
    for h in holdings:
        actual_by_symbol[h["tradingsymbol"]] = actual_by_symbol.get(h["tradingsymbol"], 0) + h["quantity"]
    for p in positions:
        actual_by_symbol[p["tradingsymbol"]] = actual_by_symbol.get(p["tradingsymbol"], 0) + p["quantity"]

    for symbol, expected_qty in expected_by_symbol.items():
        actual_qty = actual_by_symbol.get(symbol, 0)
        if actual_qty != expected_qty:
            _log_mismatch(
                conn, None, "position_state",
                expected=f"{expected_qty} shares", actual=f"{actual_qty} shares",
                detail=f"Portfolio state differs from trade ticket — {symbol} expected {expected_qty} shares, "
                       f"broker shows {actual_qty}.",
            )
