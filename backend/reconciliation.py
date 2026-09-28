import asyncio
import logging
import sqlite3

from backend import config, kite_client, position_quantity, risk_engine
from backend.accounts import get_default_account
from backend.db import get_connection

logger = logging.getLogger(__name__)


async def run_reconciliation_loop() -> None:
    """Same infinite-loop-+-sleep shape as trailing_engine.run_trailing_loop
    (Phase 3). Only checks the trading account (Part B.4 — investment
    accounts have no tickets to reconcile against)."""
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


def log_mismatch(conn: sqlite3.Connection, ticket_id: int | None, check_type: str, expected: str, actual: str,
                 detail: str) -> bool:
    """True if a new row was written; False if the same unresolved finding
    was already logged (callers alert only on True)."""
    if _already_logged(conn, ticket_id, check_type, actual):
        return False
    conn.execute(
        """
        INSERT INTO reconciliation_log (ticket_id, check_type, expected, actual, mismatch, detail)
        VALUES (?, ?, ?, ?, 1, ?)
        """,
        (ticket_id, check_type, expected, actual, detail),
    )
    logger.warning("Reconciliation mismatch (%s): expected=%r actual=%r — %s", check_type, expected, actual, detail)
    return True


def _check_order_status(conn: sqlite3.Connection, kite, account) -> None:
    # Only today's orders can be looked up (the broker's order book covers
    # the current session — older ids just error every cycle), and orders
    # still awaiting a fill are followed by order_monitor.py, not flagged.
    # PK-13: a 'partial' ticket (its order ended after a partial fill) is
    # checked too — against the quantity the app recorded as filled.
    tickets = conn.execute(
        "SELECT * FROM trade_tickets "
        "WHERE account_id = ? AND status = 'executed' AND kite_order_id IS NOT NULL "
        "AND executed_at >= ? AND COALESCE(entry_fill_status, 'filled') IN ('filled', 'partial')",
        (account["id"], risk_engine._same_day_ist_cutoff_utc()),
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
        recorded = position_quantity.filled(t)

        if t["entry_fill_status"] == "partial":
            if filled_qty is not None and filled_qty != recorded:
                log_mismatch(
                    conn, t["id"], "order_status",
                    expected=f"{status}, filled_quantity={recorded} (as recorded)",
                    actual=f"{status}, filled_quantity={filled_qty}",
                    detail=f"{t['symbol']}: TICKET FILLED QUANTITY MISMATCH — order {t['kite_order_id']} reports "
                           f"{filled_qty} filled but the app recorded {recorded}. Check the order in Kite.",
                )
        elif status != "COMPLETE" or (filled_qty is not None and filled_qty != t["quantity"]):
            log_mismatch(
                conn, t["id"], "order_status",
                expected=f"COMPLETE, filled_quantity={t['quantity']}",
                actual=f"{status}, filled_quantity={filled_qty}",
                detail=f"{t['symbol']}: order {t['kite_order_id']} does not match expected fill state",
            )


def _check_gtt_state(conn: sqlite3.Connection, kite, account) -> None:
    # One batch call, not per-ticket — deliberately duplicates part of what
    # trailing_engine.py's GTT watcher (every live protected position,
    # trailing or not, since PK-6) already detects on its own tick: a second,
    # independent check catching anything that loop's own cycle might have
    # missed, not wasted work. Logged under check_type='order_status' since
    # the reconciliation_log schema only distinguishes order-side vs
    # position-side checks, and a GTT is fundamentally an order construct.
    tickets = conn.execute(
        "SELECT * FROM trade_tickets "
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
    # get_gtts() also lists triggered/cancelled/expired GTTs — only 'active'
    # ones are still protecting anything. A GTT absent from the list and one
    # listed as e.g. 'triggered' are different findings, so they are logged
    # (and de-duplicated) separately.
    by_id = {g["id"]: g for g in gtts}
    gtt_status = {gtt_id: g.get("status", "active") for gtt_id, g in by_id.items()}

    for t in tickets:
        status = gtt_status.get(t["kite_gtt_id"])
        if status != "active":
            log_mismatch(
                conn, t["id"], "order_status",
                expected=f"GTT {t['kite_gtt_id']} active",
                actual=f"GTT {status or 'missing'} in kite.get_gtts()",
                detail=f"{t['symbol']}: protective GTT {t['kite_gtt_id']} is {status or 'missing'} at Kite. A fired or "
                       f"missing GTT does not prove the shares were sold: the position stays open until the GTT "
                       f"watcher finds a broker fill for it. Check Kite's orders and holdings.",
            )
            continue
        _check_gtt_quantity(conn, t, by_id[t["kite_gtt_id"]])


def _check_gtt_quantity(conn: sqlite3.Connection, ticket: sqlite3.Row, gtt: dict) -> None:
    """PK-13: an active protective GTT must cover exactly the shares held —
    an oversized SELL GTT is rejected by Kite for CNC (effectively no
    protection), an undersized one leaves shares unprotected."""
    desired = position_quantity.protection_quantity(conn, ticket)
    legs = position_quantity.gtt_leg_quantities(gtt)
    if desired is None or not legs or all(q == desired for q in legs):
        return
    covered = max(legs)
    kind = "OVERSIZED" if covered > desired else "UNDERSIZED"
    log_mismatch(
        conn, ticket["id"], "order_status",
        expected=f"GTT {ticket['kite_gtt_id']} covers {desired}",
        actual=f"GTT {ticket['kite_gtt_id']} covers {covered}",
        detail=f"{ticket['symbol']}: PROTECTION QUANTITY MISMATCH — protective GTT {ticket['kite_gtt_id']} is "
               f"{kind}: it covers {covered} share(s) but the position holds {desired}. The GTT watcher resizes it; "
               f"check it in Kite if this persists.",
    )


def _check_position_state(conn: sqlite3.Connection, kite, account) -> None:
    # PK-13: what each position actually holds — filled, less what its exits
    # sold (a partial exit's residual included) — never the requested
    # quantity. A working entry counts only what has filled so far.
    expected_by_symbol: dict[str, int] = {}
    for pos in risk_engine.list_open_positions(conn, account["id"]):
        held = pos["open_quantity"]
        if not held or held < 0:
            continue  # nothing bought yet (order_monitor follows it), or unknown / NEEDS REVIEW (flagged there)
        expected_by_symbol[pos["symbol"]] = expected_by_symbol.get(pos["symbol"], 0) + held
    if not expected_by_symbol:
        return

    try:
        holdings = kite.holdings()
        positions = kite.positions().get("net", [])
    except Exception:
        logger.exception("Could not fetch holdings()/positions() for position-state check")
        return

    # Unsettled (T+1) delivery quantity sits in t1_quantity, not quantity —
    # ignoring it flagged every position the day after it was bought.
    actual_by_symbol: dict[str, int] = {}
    for h in holdings:
        qty = (h.get("quantity") or 0) + (h.get("t1_quantity") or 0)
        actual_by_symbol[h["tradingsymbol"]] = actual_by_symbol.get(h["tradingsymbol"], 0) + qty
    for p in positions:
        actual_by_symbol[p["tradingsymbol"]] = actual_by_symbol.get(p["tradingsymbol"], 0) + (p.get("quantity") or 0)

    # Only a SHORTFALL is a problem: holding more than the app's tickets
    # account for is normal when the stock is also held outside the app.
    for symbol, expected_qty in expected_by_symbol.items():
        actual_qty = actual_by_symbol.get(symbol, 0)
        if actual_qty < expected_qty:
            log_mismatch(
                conn, None, "position_state",
                expected=f"{expected_qty} shares", actual=f"{actual_qty} shares",
                detail=f"{symbol}: the app's open tickets hold {expected_qty} shares but the broker shows only "
                       f"{actual_qty} — sold outside the app, or an order didn't fill.",
            )
