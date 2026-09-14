"""Order fill monitor.

Kite's place_order() only confirms that the broker ACCEPTED an order — the
fill (or an RMS/exchange rejection) arrives asynchronously. This loop follows
every live order the app placed until it reaches a terminal state and
reconciles the ticket automatically, so the app never keeps believing in a
position that never opened, or a close that never happened:

* entry COMPLETE                     -> record the real fill price/time
* entry REJECTED/CANCELLED, 0 filled -> the position never opened: remove its
                                        protective GTT, mark ticket cancelled
* entry partially filled, then ended -> flag for review (GTT covers full qty)
* exit COMPLETE                      -> record fill, re-price the journal row
* exit REJECTED/CANCELLED, 0 filled  -> the position is still open (its GTT
                                        was removed when the exit was placed):
                                        re-open it and raise an urgent alert
"""

import asyncio
import logging
import sqlite3

from backend import config, execution, journal, kite_client, reconciliation, risk_engine
from backend.accounts import get_default_account
from backend.db import get_connection
from backend.ticker import ticker_manager

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"COMPLETE", "CANCELLED", "REJECTED"}


async def run_order_monitor_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(check_pending_orders)
        except Exception:
            logger.exception("Order monitor cycle failed")
        await asyncio.sleep(config.ORDER_MONITOR_INTERVAL_SECONDS)


def _log_event(conn: sqlite3.Connection, ticket_id: int, event_type: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, ?, ?)",
        (ticket_id, event_type, detail),
    )


def pending_tickets(conn: sqlite3.Connection, account_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM trade_tickets
        WHERE account_id = ? AND mode = 'live' AND status = 'executed'
          AND entry_fill_status = 'pending' AND kite_order_id IS NOT NULL
        ORDER BY id
        """,
        (account_id,),
    ).fetchall()


def check_pending_orders(kite=None) -> dict:
    updates: list[dict] = []
    checked = 0
    conn = get_connection()
    try:
        account = get_default_account(conn)
        if account["account_type"] != "trading":
            return {"checked": 0, "updated": 0}
        tickets = pending_tickets(conn, account["id"])
        if not tickets:
            return {"checked": 0, "updated": 0}
        if kite is None:
            try:
                kite = kite_client.get_kite()
            except kite_client.NoActiveSessionError:
                return {"checked": 0, "updated": 0, "skipped": "no_session"}
        try:
            book = {o["order_id"]: o for o in kite.orders()}
        except Exception:
            logger.exception("Order monitor: could not fetch the order book")
            return {"checked": 0, "updated": 0, "skipped": "order_book_unavailable"}

        today_cutoff = risk_engine._same_day_ist_cutoff_utc()
        for ticket in tickets:
            checked += 1
            order = book.get(ticket["kite_order_id"]) or _order_from_history(kite, ticket["kite_order_id"])
            if order is None:
                # The broker's order book only covers the current session. An
                # order from an earlier session that it no longer knows about
                # can't be confirmed either way — stop polling it forever.
                if (ticket["executed_at"] or "") < today_cutoff:
                    updates.append(_mark_unconfirmed(conn, ticket))
                    conn.commit()
                continue
            if order.get("status") not in TERMINAL_STATUSES:
                continue
            if execution.is_closing_sell(ticket):
                updates.append(_resolve_exit(conn, kite, ticket, order))
            else:
                updates.append(_resolve_entry(conn, kite, ticket, order))
            conn.commit()
    finally:
        conn.close()

    for update in updates:
        ticker_manager.broadcast({"type": "order_update", **update})
    if updates:
        ticker_manager.broadcast({"type": "tickets_changed", "reason": "order_update"})
    return {"checked": checked, "updated": len(updates)}


def _order_from_history(kite, order_id: str) -> dict | None:
    try:
        history = kite.order_history(order_id)
    except Exception:
        return None
    return history[-1] if history else None


def _price(value) -> str:
    return f"₹{value:,.2f}" if value else "n/a"


def _mark_unconfirmed(conn: sqlite3.Connection, ticket: sqlite3.Row) -> dict:
    msg = (
        f"{ticket['symbol']}: order {ticket['kite_order_id']} is no longer in the broker's order book, so its "
        f"fill could not be confirmed. The position is treated as open — verify it in Kite."
    )
    conn.execute("UPDATE trade_tickets SET entry_fill_status = 'unconfirmed' WHERE id = ?", (ticket["id"],))
    _log_event(conn, ticket["id"], "modified", msg)
    return {"ticket_id": ticket["id"], "symbol": ticket["symbol"], "order_status": None,
            "severity": "warning", "message": msg}


def _resolve_entry(conn: sqlite3.Connection, kite, ticket: sqlite3.Row, order: dict) -> dict:
    status = order["status"]
    filled = order.get("filled_quantity") or 0
    avg = order.get("average_price") or None
    base = {"ticket_id": ticket["id"], "symbol": ticket["symbol"], "order_status": status}

    if status == "COMPLETE":
        conn.execute(
            "UPDATE trade_tickets SET entry_fill_status = 'filled', filled_price = ?, filled_at = datetime('now') "
            "WHERE id = ?",
            (avg or ticket["entry_price"], ticket["id"]),
        )
        msg = (f"{ticket['symbol']}: entry filled — {ticket['transaction_type']} {filled or ticket['quantity']} "
               f"@ {_price(avg)} (order {ticket['kite_order_id']})")
        _log_event(conn, ticket["id"], "modified", msg)
        return {**base, "severity": "success", "message": msg}

    reason = order.get("status_message") or "no reason given by the broker"
    if filled:
        conn.execute(
            "UPDATE trade_tickets SET entry_fill_status = 'partial', filled_price = ?, filled_at = datetime('now') "
            "WHERE id = ?",
            (avg, ticket["id"]),
        )
        msg = (f"{ticket['symbol']}: entry order {status.lower()} after a PARTIAL fill "
               f"({filled}/{ticket['quantity']}). The protective GTT still covers {ticket['quantity']} shares — "
               f"reduce it to {filled} in Kite.")
        _log_event(conn, ticket["id"], "modified", msg)
        reconciliation.log_mismatch(
            conn, ticket["id"], "order_status",
            expected=f"COMPLETE, filled_quantity={ticket['quantity']}",
            actual=f"{status}, filled_quantity={filled}", detail=msg,
        )
        return {**base, "severity": "urgent", "message": msg}

    gtt_note = ""
    if ticket["kite_gtt_id"] is not None:
        try:
            kite.delete_gtt(ticket["kite_gtt_id"])
            conn.execute("UPDATE trade_tickets SET kite_gtt_id = NULL WHERE id = ?", (ticket["id"],))
            gtt_note = f" Its protective GTT {ticket['kite_gtt_id']} was removed."
        except Exception as e:
            gtt_note = f" Removing its protective GTT {ticket['kite_gtt_id']} FAILED ({e}) — delete it in Kite."
    conn.execute(
        "UPDATE trade_tickets SET status = 'cancelled', entry_fill_status = ? WHERE id = ?",
        (status.lower(), ticket["id"]),
    )
    msg = f"{ticket['symbol']}: entry order {status.lower()} — {reason}. The position never opened.{gtt_note}"
    _log_event(conn, ticket["id"], "rejected", msg)
    return {**base, "severity": "warning", "message": msg}


def _resolve_exit(conn: sqlite3.Connection, kite, ticket: sqlite3.Row, order: dict) -> dict:
    status = order["status"]
    filled = order.get("filled_quantity") or 0
    avg = order.get("average_price") or None
    base = {"ticket_id": ticket["id"], "symbol": ticket["symbol"], "order_status": status}
    original_id = ticket["closes_ticket_id"]

    if status == "COMPLETE":
        fill_price = avg or ticket["entry_price"]
        conn.execute(
            "UPDATE trade_tickets SET entry_fill_status = 'filled', filled_price = ?, filled_at = datetime('now') "
            "WHERE id = ?",
            (fill_price, ticket["id"]),
        )
        journal.apply_exit_fill(conn, ticket["id"], fill_price)
        msg = f"{ticket['symbol']}: exit filled — {ticket['transaction_type']} {filled} @ {_price(fill_price)}"
        _log_event(conn, ticket["id"], "modified", msg)
        return {**base, "severity": "success", "message": msg}

    reason = order.get("status_message") or "no reason given by the broker"
    if filled:
        conn.execute(
            "UPDATE trade_tickets SET entry_fill_status = 'partial', filled_price = ?, filled_at = datetime('now') "
            "WHERE id = ?",
            (avg, ticket["id"]),
        )
        msg = (f"{ticket['symbol']}: exit order {status.lower()} after a PARTIAL fill ({filled}/{ticket['quantity']}) — "
               f"{ticket['quantity'] - filled} shares are still open and UNPROTECTED.")
        _log_event(conn, ticket["id"], "modified", msg)
        reconciliation.log_mismatch(conn, original_id, "position_state", expected="position closed",
                                    actual=f"exit {status}, filled_quantity={filled}", detail=msg)
        return {**base, "severity": "urgent", "message": msg}

    original = conn.execute("SELECT kite_gtt_id FROM trade_tickets WHERE id = ?", (original_id,)).fetchone()
    still_protected = False
    if original is not None and original["kite_gtt_id"] is not None:
        try:
            still_protected = any(
                g["id"] == original["kite_gtt_id"] and g.get("status", "active") == "active" for g in kite.get_gtts()
            )
        except Exception:
            still_protected = False

    conn.execute(
        "UPDATE trade_tickets SET status = 'failed', entry_fill_status = ? WHERE id = ?",
        (status.lower(), ticket["id"]),
    )
    if still_protected:
        conn.execute("UPDATE trade_tickets SET closed_at = NULL WHERE id = ?", (original_id,))
    else:
        conn.execute("UPDATE trade_tickets SET closed_at = NULL, kite_gtt_id = NULL WHERE id = ?", (original_id,))
    conn.execute("DELETE FROM closed_trades WHERE sell_ticket_id = ?", (ticket["id"],))

    protection = ("its protective GTT is still active" if still_protected
                  else "it is UNPROTECTED — place a stop-loss or re-issue the exit")
    msg = f"{ticket['symbol']}: exit order {status.lower()} — {reason}. The position is still OPEN; {protection}."
    _log_event(conn, ticket["id"], "execution_failed", msg)
    # original_id is None for an alert_action sell with no originating
    # ticket (see execution.is_closing_sell) — there is no tracked position
    # ticket to log an event against or reopen; ticket_events.ticket_id is
    # NOT NULL, so skip that call rather than crash the whole monitor cycle.
    if original_id is not None:
        _log_event(conn, original_id, "modified", msg)
    reconciliation.log_mismatch(conn, original_id, "position_state", expected="position closed",
                                actual=f"exit order {status}", detail=msg)
    return {**base, "severity": "urgent", "message": msg}
