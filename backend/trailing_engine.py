import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from kiteconnect.exceptions import GeneralException, InputException, OrderException

from backend import config, execution, journal, kite_client
from backend.accounts import get_default_account
from backend.db import get_connection
from backend.ticker import ticker_manager

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# In-memory only (not persisted) — resets on restart, which is fine: a fresh
# process gets a clean slate of 3 retries before escalating again.
_consecutive_failures: dict[int, int] = {}
_last_active_count = 0


async def run_trailing_loop() -> None:
    while True:
        try:
            await _tick_once()
        except Exception:
            logger.exception("Trailing engine tick failed")
        ticker_manager.broadcast({
            "type": "trailing_heartbeat",
            "last_run_at": datetime.now(IST).isoformat(),
            "active_ticket_count": _last_active_count,
        })
        await asyncio.sleep(config.TRAILING_CHECK_INTERVAL_SECONDS)


async def _tick_once() -> None:
    global _last_active_count
    conn = get_connection()
    try:
        account = get_default_account(conn)
        if account["account_type"] != "trading":
            _last_active_count = 0
            return

        tickets = conn.execute(
            """
            SELECT * FROM trade_tickets
            WHERE account_id = ? AND mode = 'live' AND status = 'executed'
              AND trailing_enabled = 1 AND closed_at IS NULL
            """,
            (account["id"],),
        ).fetchall()
        _last_active_count = len(tickets)
        if not tickets:
            return

        try:
            kite = kite_client.get_kite()
        except kite_client.NoActiveSessionError:
            return  # heartbeat still fires — UI can tell "ran but skipped" from "not running"

        gtts = kite.get_gtts()
        active_gtt_ids = {g["id"] for g in gtts}
        keys = [f"{t['exchange']}:{t['symbol']}" for t in tickets]
        ltp_data = kite.ltp(keys)

        for ticket in tickets:
            _process_ticket(kite, conn, ticket, ltp_data, active_gtt_ids)
        conn.commit()
    finally:
        conn.close()


def _process_ticket(kite, conn: sqlite3.Connection, ticket: sqlite3.Row, ltp_data: dict, active_gtt_ids: set) -> None:
    if ticket["kite_gtt_id"] is None:
        # No GTT exists to manage (e.g. protection_failed at execution time) —
        # nothing for the trailing engine to do until a human intervenes.
        return

    if ticket["kite_gtt_id"] not in active_gtt_ids:
        _handle_gtt_closure(kite, conn, ticket)
        return

    key = f"{ticket['exchange']}:{ticket['symbol']}"
    ltp = ltp_data.get(key, {}).get("last_price")
    if ltp is None:
        return

    conn.execute("UPDATE trade_tickets SET last_trailing_check_at = datetime('now') WHERE id = ?", (ticket["id"],))

    if not ticket["trailing_active"]:
        _try_activate(conn, ticket, ltp)
        return

    _try_ratchet(kite, conn, ticket, ltp)


def _find_matching_fill(kite, ticket: sqlite3.Row, exit_side: str) -> dict | None:
    """Best-effort match of the order Kite placed when this ticket's GTT
    triggered — there's no direct GTT-to-order link in the API, so this
    matches on tradingsymbol/exchange/transaction_type/quantity/COMPLETE
    status, most recent first. Returns None if nothing matches (e.g. API
    lag), in which case the caller falls back to an estimated fill price
    rather than blocking the closure."""
    try:
        orders = kite.orders()
    except Exception:
        logger.exception("Could not fetch orders to match GTT closure fill for ticket %d", ticket["id"])
        return None

    candidates = [
        o for o in orders
        if o.get("tradingsymbol") == ticket["symbol"]
        and o.get("exchange") == ticket["exchange"]
        and o.get("transaction_type") == exit_side
        and o.get("status") == "COMPLETE"
        and o.get("filled_quantity") == ticket["quantity"]
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda o: o.get("order_timestamp") or "", reverse=True)
    return candidates[0]


def _handle_gtt_closure(kite, conn: sqlite3.Connection, ticket: sqlite3.Row) -> None:
    """A GTT that was being monitored has disappeared from Kite (triggered
    or deleted externally). Historically this only set closed_at on the
    original ticket, with no record anywhere of what price the position
    actually exited at. Phase 6 needs that price to compute realized P&L,
    so this now synthesizes a proper executed SELL ticket (closes_ticket_id
    set) before marking the original closed, and runs it through the exact
    same closed_trades computation exit tickets use — one code path for
    "how did this position's P&L get computed," regardless of whether a
    human/strategy explicitly exited or Kite auto-triggered the stop/target."""
    exit_side = execution.resolve_exit_side(ticket["transaction_type"])
    fill = _find_matching_fill(kite, ticket, exit_side)

    if fill is not None:
        fill_price = fill["average_price"]
        matched_order_id = fill["order_id"]
        rationale = f"Stop-loss/target hit via GTT auto-trigger (order {matched_order_id})"
    else:
        # No matching order found (API lag or an untraceable external
        # change) — fall back to a fresh LTP rather than blocking the
        # closure, but say plainly that it's an estimate.
        try:
            key = f"{ticket['exchange']}:{ticket['symbol']}"
            fill_price = kite.ltp(key)[key]["last_price"]
        except Exception:
            fill_price = ticket["current_stop_loss"] or ticket["stop_loss"]
        matched_order_id = None
        rationale = "Stop-loss/target hit via GTT auto-trigger (estimated fill price — no matching Kite order found)"

    current_stop = ticket["current_stop_loss"] or ticket["stop_loss"]
    dist_to_stop = abs(fill_price - current_stop)
    dist_to_target = abs(fill_price - ticket["target_1"])
    closer_to = "stop_loss" if dist_to_stop <= dist_to_target else "target"

    metrics = execution.compute_risk_metrics({
        "entry_price": fill_price, "quantity": ticket["quantity"],
        "stop_loss": ticket["stop_loss"], "target_1": ticket["target_1"], "target_2": None,
    })
    rule_checklist = json.dumps({
        "gtt_auto_trigger": {
            "pass": True, "closer_to": closer_to, "fill_price": fill_price, "matched_order_id": matched_order_id,
        }
    })

    cursor = conn.execute(
        """
        INSERT INTO trade_tickets (
            account_id, source, strategy_name, symbol, exchange, instrument_token,
            transaction_type, quantity, entry_price, entry_order_type,
            stop_loss, target_1, target_2,
            trailing_enabled, product_type, use_gtt, risk_amount, risk_reward_ratio,
            rationale, status, mode, executed_at, closes_ticket_id, rule_checklist
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'MARKET', ?, ?, NULL,
                  0, ?, 1, ?, ?, ?, 'executed', ?, datetime('now'), ?, ?)
        """,
        (
            ticket["account_id"], ticket["source"], ticket["strategy_name"], ticket["symbol"],
            ticket["exchange"], ticket["instrument_token"], exit_side, ticket["quantity"], fill_price,
            ticket["stop_loss"], ticket["target_1"], ticket["product_type"],
            metrics["risk_amount"], metrics["risk_reward_ratio"], rationale, ticket["mode"],
            ticket["id"], rule_checklist,
        ),
    )
    sell_ticket_id = cursor.lastrowid
    _log_event(conn, sell_ticket_id, "created", rationale)
    _log_event(conn, sell_ticket_id, "executed", rationale)

    conn.execute("UPDATE trade_tickets SET closed_at = datetime('now') WHERE id = ?", (ticket["id"],))
    _log_event(conn, ticket["id"], "closed",
               f"GTT no longer active on Kite; recorded as closed via ticket {sell_ticket_id}.")
    conn.commit()

    try:
        journal.record_closed_trade(conn, sell_ticket_id)
    except ValueError:
        logger.exception("Failed to record closed_trades row for synthesized exit ticket %d", sell_ticket_id)


def _try_activate(conn: sqlite3.Connection, ticket: sqlite3.Row, ltp: float) -> None:
    if ticket["trailing_trigger"] == "manual":
        return  # only POST /api/tickets/{id}/start_trailing can activate this

    touched = (ltp >= ticket["target_1"]) if ticket["transaction_type"] == "BUY" else (ltp <= ticket["target_1"])
    if not touched:
        return

    conn.execute("UPDATE trade_tickets SET trailing_active = 1 WHERE id = ?", (ticket["id"],))
    _log_event(conn, ticket["id"], "trailing_adjusted", f"Target 1 touched at LTP {ltp}; trailing activated.")


def _try_ratchet(kite, conn: sqlite3.Connection, ticket: sqlite3.Row, ltp: float) -> None:
    current_stop = ticket["current_stop_loss"]
    distance = ticket["trailing_distance"]

    if ticket["transaction_type"] == "BUY":
        candidate = ltp - distance
        should_update = candidate > current_stop
    else:
        candidate = ltp + distance
        should_update = candidate < current_stop

    if not should_update:
        return

    exit_side = execution.resolve_exit_side(ticket["transaction_type"])
    try:
        kite.modify_gtt(
            trigger_id=ticket["kite_gtt_id"],
            trigger_type=kite.GTT_TYPE_SINGLE,
            tradingsymbol=ticket["symbol"],
            exchange=ticket["exchange"],
            trigger_values=[candidate],
            last_price=ltp,
            orders=[{
                "transaction_type": exit_side,
                "quantity": ticket["quantity"],
                "order_type": "LIMIT",
                "product": ticket["product_type"],
                "price": execution.buffered_stop_price(candidate, ticket["transaction_type"]),
            }],
        )
    except (OrderException, InputException, GeneralException) as e:
        _consecutive_failures[ticket["id"]] = _consecutive_failures.get(ticket["id"], 0) + 1
        _log_event(conn, ticket["id"], "trailing_adjusted",
                   f"FAILED to move stop from {current_stop} to {candidate}: {e}. Will retry next cycle.")
        if _consecutive_failures[ticket["id"]] >= 3:
            ticker_manager.broadcast({
                "type": "trailing_error",
                "ticket_id": ticket["id"],
                "message": f"Trailing stop update has failed {_consecutive_failures[ticket['id']]} times in a row "
                           f"for {ticket['symbol']}: {e}",
            })
        return

    conn.execute("UPDATE trade_tickets SET current_stop_loss = ? WHERE id = ?", (candidate, ticket["id"]))
    _log_event(conn, ticket["id"], "trailing_adjusted", f"Stop moved from {current_stop} to {candidate} (LTP {ltp})")
    _consecutive_failures[ticket["id"]] = 0


def _log_event(conn: sqlite3.Connection, ticket_id: int, event_type: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, ?, ?)",
        (ticket_id, event_type, detail),
    )
