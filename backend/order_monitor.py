"""Order fill monitor.

Kite's place_order() only confirms that the broker ACCEPTED an order — the
fill (or an RMS/exchange rejection) arrives asynchronously. This loop follows
every live order the app placed until it reaches a terminal state and
reconciles the ticket automatically, so the app never keeps believing in a
position that never opened, or a close that never happened:

PK-13: the broker's cumulative filled_quantity is recorded on every ticket
it follows (monotonically — backend/position_quantity.py), and the
position's protective GTT is resized to the shares actually held whenever
that changes. The requested quantity is never assumed to be the position.

* entry working, some shares filled  -> record the fill; the GTT is resized
                                        to it (once per distinct fill)
* entry COMPLETE                     -> record the real fill price/time/qty
* entry REJECTED/CANCELLED, 0 filled -> the position never opened: remove its
                                        protective GTT, mark ticket cancelled
* entry partially filled, then ended -> 'partial': a real open position of
                                        the filled shares; GTT resized to them

For an explicit exit this monitor is the only thing that closes a live
position (PK-8, P0-7). Placing the exit made it EXITING: the position stayed
open, protected by its GTT, unjournaled.

* exit OPEN / AMO / trigger pending  -> nothing: still EXITING
* exit working, some shares filled   -> record the fill: the position's open
                                        quantity drops and its GTT is resized
                                        to what remains; urgent PARTIAL EXIT
                                        flag (once per distinct fill)
* exit COMPLETE, full quantity       -> record the broker's fill price/time;
                                        if the position is now flat: close it
                                        at that time, journal it once,
                                        supersede sibling exits, then remove
                                        the protective GTT (a failed removal
                                        is an urgent ORPHANED GTT warning,
                                        never a reopen)
* exit ended after a partial fill    -> 'partial': the sold shares count; the
                                        rest stays OPEN (not journaled), its
                                        GTT resized to it, and the exit no
                                        longer owns the position — a new exit
                                        may sell the remainder; nothing is
                                        resubmitted automatically
* exit REJECTED/CANCELLED, 0 filled  -> exit 'failed'; the position was never
                                        closed and keeps its GTT; urgent alert

It also resolves submissions whose outcome is UNKNOWN (PK-2): the broker
call failed in a way that does not prove Kite rejected the order, so the
order may or may not exist. Such a ticket stays 'confirmed' and is found by
its deterministic order tag (execution.order_tag_for) — never resubmitted:

* exactly one matching tagged order  -> adopt it (status 'executed'); the
                                        normal lifecycle above takes over (an
                                        adopted exit is EXITING like any other)
* no tagged order yet                -> keep searching, for the window
* none after the window, same day    -> 'failed' / not_found: no order exists
* several / mismatching tagged orders,
  or the order book no longer covers
  the submission day                 -> needs_review: urgent, nothing guessed

Every write here is a compare-and-set against the state the ticket was read
in, because the confirming request may be writing the same ticket.

PK-20: dry-run tickets follow exactly the same lifecycle against the paper
broker. Each cycle first follows the live orders (Kite), then advances the
simulated exchange (paper_broker.evaluate: observed prices fill paper
orders and fire paper GTTs) and follows the dry-run tickets' paper orders
through the very same functions below. A cycle only ever pairs a mode with
its own broker (paper_broker.check_mode).
"""

import asyncio
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from kiteconnect.exceptions import TokenException

from backend import (
    config, execution, instance_lock, kite_client, obs, paper_broker, position_quantity, reconciliation, risk_engine,
    worker_health,
)
from backend.obs import log_event
from backend.accounts import get_default_account
from backend.db import get_connection
from backend.trading_calendar import IST
from backend.ticker import ticker_manager

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"COMPLETE", "CANCELLED", "REJECTED"}
_SQL_TIME = "%Y-%m-%d %H:%M:%S"
_IST = IST  # PK-22C: one IST definition (trading_calendar)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def run_order_monitor_loop() -> None:
    cycle_no = 0
    while True:
        if not instance_lock.loop_may_run("order_monitor"):  # Phase 2: only the database owner
            return
        cycle_no += 1
        # PK-22E/F: one correlation id per cycle; health recorded per cycle.
        # The live and paper halves fail independently.
        with obs.correlation(f"om-{cycle_no}"):
            try:
                with worker_health.cycle("order_monitor"):
                    await asyncio.to_thread(check_pending_orders)
            except Exception:
                logger.exception("Order monitor cycle failed")
            try:
                with worker_health.cycle("order_monitor_paper"):
                    await asyncio.to_thread(check_paper_orders)
            except Exception:
                logger.exception("Paper order monitor cycle failed")
        await asyncio.sleep(config.ORDER_MONITOR_INTERVAL_SECONDS)


def _log_event(conn: sqlite3.Connection, ticket_id: int, event_type: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, ?, ?)",
        (ticket_id, event_type, detail),
    )


def pending_tickets(conn: sqlite3.Connection, account_id: int, mode: str = "live") -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM trade_tickets
        WHERE account_id = ? AND mode = ? AND status = 'executed'
          AND entry_fill_status = 'pending' AND kite_order_id IS NOT NULL
          AND (mode = 'live' OR kite_order_id LIKE 'PAPER-%')
        ORDER BY id
        """,
        (account_id, mode),
    ).fetchall()


def unresolved_submissions(conn: sqlite3.Connection, account_id: int, now: datetime,
                           mode: str = "live") -> list[sqlite3.Row]:
    """Tickets of `mode` whose broker submission has no known outcome:
    marked 'unknown' by the confirming request, or still 'submitting' long
    after any request could be in flight (the process died mid-submission)."""
    stale_cutoff = (now - timedelta(seconds=config.SUBMISSION_STALE_SECONDS)).strftime(_SQL_TIME)
    return conn.execute(
        """
        SELECT * FROM trade_tickets
        WHERE account_id = ? AND mode = ? AND status = 'confirmed'
          AND (submission_state = ? OR (submission_state = ? AND submitted_at <= ?))
        ORDER BY id
        """,
        (account_id, mode, execution.UNKNOWN, execution.SUBMITTING, stale_cutoff),
    ).fetchall()


def check_paper_orders(market_data=None) -> dict:
    """PK-20: one dry-run cycle — advance the simulated exchange against the
    observed market (market_data: Kite, read only), then follow every
    dry-run ticket's paper order through the same lifecycle as a live one.
    Without a session there are no prices, so nothing can fill: skipped."""
    conn = get_connection()
    try:
        account = get_default_account(conn)
    finally:
        conn.close()
    if account["account_type"] != "trading":
        return {"checked": 0, "updated": 0}
    if market_data is None:
        try:
            market_data = kite_client.get_kite()
        except kite_client.NoActiveSessionError:
            return {"checked": 0, "updated": 0, "skipped": "no_session"}
    exchange = paper_broker.evaluate(account["id"], market_data)
    result = check_pending_orders(mode="dry_run")
    result["paper_exchange"] = exchange
    return result


def check_pending_orders(kite=None, mode: str = "live") -> dict:
    """One cycle for the tickets of `mode`. Live: Kite's order book (`kite`,
    or the session's client). Dry run (PK-20): the paper broker's book, on
    this cycle's own connection — no session is needed to read it."""
    updates: list[dict] = []
    checked = 0
    conn = get_connection()
    try:
        account = get_default_account(conn)
        if account["account_type"] != "trading":
            return {"checked": 0, "updated": 0}
        now = _utc_now()
        unresolved = unresolved_submissions(conn, account["id"], now, mode)
        if not unresolved and not pending_tickets(conn, account["id"], mode):
            return {"checked": 0, "updated": 0}
        if mode == "dry_run":
            kite = paper_broker.PaperBroker(conn, account["id"])
        elif kite is None:
            try:
                kite = kite_client.get_kite()
            except kite_client.NoActiveSessionError:
                return {"checked": 0, "updated": 0, "skipped": "no_session"}
        # A cycle never pairs a mode with the other mode's broker.
        paper_broker.check_mode(kite, mode)
        try:
            raw_orders = kite.orders()
        except Exception:
            logger.exception("Order monitor: could not fetch the order book")
            return {"checked": 0, "updated": 0, "skipped": "order_book_unavailable"}
        # PK-22F: a malformed row in the broker's book is skipped, never
        # allowed to abort the cycle for every other ticket.
        orders = [o for o in raw_orders if isinstance(o, dict) and o.get("order_id")]
        if len(orders) != len(raw_orders):
            logger.warning("event=order_monitor.malformed_orders skipped=%d mode=%s",
                           len(raw_orders) - len(orders), mode)
        book = {o["order_id"]: o for o in orders}

        # Unknown submissions first: one adopted here is 'executed' and
        # 'pending' by the time pending_tickets() runs below, so it continues
        # through the normal fill lifecycle in this same cycle.
        for ticket in unresolved:
            checked += 1
            with _one_ticket(conn, ticket, mode):
                updates.extend(_reconcile_submission(conn, kite, ticket, orders, now))
                conn.commit()

        today_cutoff = risk_engine._same_day_ist_cutoff_utc()
        for ticket in pending_tickets(conn, account["id"], mode):
            checked += 1
            with _one_ticket(conn, ticket, mode):
                updates.extend(_follow_pending(conn, kite, ticket, book, today_cutoff))
    finally:
        conn.close()

    # None: another writer moved the ticket first, so nothing was changed.
    updates = [u for u in updates if u is not None]

    for update in updates:
        ticker_manager.broadcast({"type": "order_update", **update})
    if updates:
        ticker_manager.broadcast({"type": "tickets_changed", "reason": "order_update"})
    return {"checked": checked, "updated": len(updates)}


@contextmanager
def _one_ticket(conn: sqlite3.Connection, ticket: sqlite3.Row, mode: str):
    """PK-22F: one ticket's failure (a malformed order, a transient DB lock,
    a broker error while removing a GTT) rolls back that ticket alone and the
    cycle continues with the next; it is retried on the next cycle. A dead
    session (TokenException) fails every later call too: it ends the cycle."""
    try:
        yield
    except TokenException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        logger.exception("event=order_monitor.ticket_failed ticket_id=%s symbol=%s mode=%s order_id=%s "
                         "(rolled back; continuing with the other tickets)",
                         ticket["id"], ticket["symbol"], mode, ticket["kite_order_id"])


def _follow_pending(conn: sqlite3.Connection, kite, ticket: sqlite3.Row, book: dict, today_cutoff: str) -> list:
    order = book.get(ticket["kite_order_id"]) or _order_from_history(kite, ticket["kite_order_id"])
    if order is None:
        # The broker's order book only covers the current session. An
        # order from an earlier session that it no longer knows about
        # can't be confirmed either way — stop polling it forever.
        if (ticket["executed_at"] or "") < today_cutoff:
            update = _mark_unconfirmed(conn, ticket)
            conn.commit()
            return [update]
        return []
    if order.get("status") not in TERMINAL_STATUSES:
        if int(order.get("filled_quantity") or 0) > 0:
            if execution.is_closing_sell(ticket):
                update = _flag_partial_exit_in_progress(conn, kite, ticket, order)
            else:
                update = _entry_fill_in_progress(conn, kite, ticket, order)
            conn.commit()
            return [update]
        return []
    if execution.is_closing_sell(ticket):
        update = _resolve_exit(conn, kite, ticket, order)
    else:
        update = _resolve_entry(conn, kite, ticket, order)
    conn.commit()
    if update is not None:
        log_event(logger, "order.resolved", account_id=ticket["account_id"], ticket_id=ticket["id"],
                  symbol=ticket["symbol"], mode=ticket["mode"], order_id=ticket["kite_order_id"],
                  status=order.get("status"), filled=order.get("filled_quantity"),
                  side="exit" if execution.is_closing_sell(ticket) else "entry")
    return [update]


def _order_from_history(kite, order_id: str) -> dict | None:
    try:
        history = kite.order_history(order_id)
    except Exception:
        return None
    return history[-1] if history else None


def _price(value) -> str:
    return f"₹{value:,.2f}" if value else "n/a"


# ---- PK-2: unknown submission recovery -------------------------------------

def _parse_sql_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value[:19], _SQL_TIME).replace(tzinfo=timezone.utc)


def _has_tag(order: dict, tag: str) -> bool:
    return order.get("tag") == tag or tag in (order.get("tags") or [])


def _matches_ticket(order: dict, ticket: sqlite3.Row) -> bool:
    return (order.get("tradingsymbol") == ticket["symbol"]
            and order.get("exchange", ticket["exchange"]) == ticket["exchange"]
            and order.get("transaction_type") == ticket["transaction_type"]
            and int(order.get("quantity") or 0) == ticket["quantity"])


def _describe(orders: list[dict]) -> str:
    return "; ".join(
        f"{o.get('order_id')} {o.get('transaction_type')} {o.get('quantity')} {o.get('tradingsymbol')} "
        f"{o.get('status')}" for o in orders
    ) or "none"


def _set_unresolved(conn: sqlite3.Connection, ticket: sqlite3.Row, **columns) -> bool:
    return execution.update_ticket_if(
        conn, ticket["id"], status_in=("confirmed",), submission_state_in=(execution.UNKNOWN,), **columns,
    )


def _reconcile_submission(conn: sqlite3.Connection, kite, ticket: sqlite3.Row, orders: list[dict],
                          now: datetime) -> list[dict | None]:
    """Resolves one unknown submission from the broker's order book, forward
    only: to adopted, not_found or needs_review. Never places, cancels or
    retries an order, and never returns the ticket to approval."""
    symbol, tag = ticket["symbol"], ticket["order_tag"]
    base = {"ticket_id": ticket["id"], "symbol": symbol, "order_status": None}
    updates: list[dict | None] = []

    if ticket["submission_state"] == execution.SUBMITTING:
        # The request that claimed this ticket never recorded an outcome.
        if not execution.update_ticket_if(
            conn, ticket["id"], status_in=("confirmed",), submission_state_in=(execution.SUBMITTING,),
            submission_state=execution.UNKNOWN,
        ):
            return updates  # the request recorded its outcome after all
        msg = (f"{symbol}: the submission of this ticket's order (tag {tag}) never recorded an outcome — the app "
               f"stopped or lost contact mid-submission. Outcome UNKNOWN — DO NOT RESUBMIT; searching Kite for "
               f"the order.")
        _log_event(conn, ticket["id"], "submission_unknown", msg)
        updates.append({**base, "severity": "urgent", "message": msg})

    matches = [o for o in orders if tag and _has_tag(o, tag)]
    if len(matches) == 1 and _matches_ticket(matches[0], ticket):
        updates.append(_adopt(conn, kite, ticket, matches[0]))
    elif len(matches) > 1:
        updates.append(_needs_review(conn, ticket, matches, f"{len(matches)} broker orders carry this ticket's tag"))
    elif matches:
        updates.append(_needs_review(conn, ticket, matches,
                                     "the broker order with this ticket's tag does not match the ticket "
                                     "(symbol, side or quantity differ)"))
    else:
        submitted = _parse_sql_time(ticket["submitted_at"])
        if submitted is not None and now - submitted < timedelta(seconds=config.UNKNOWN_ORDER_RECONCILE_WINDOW_SECONDS):
            return updates  # keep searching; never resubmit
        if submitted is not None and submitted.astimezone(_IST).date() == now.astimezone(_IST).date():
            updates.append(_not_found(conn, ticket))
        else:
            updates.append(_needs_review(conn, ticket, [],
                                         "no tagged order was found, but the broker's order book no longer covers "
                                         "the day of the submission, so its absence proves nothing"))
    return updates


def _adopt(conn: sqlite3.Connection, kite, ticket: sqlite3.Row, order: dict) -> dict | None:
    """Takes over the broker order the unknown submission created. From
    here the ticket is an ordinary placed order; the pending-order loop
    follows it to its fill exactly as if the confirm had returned its id."""
    order_id, status = order["order_id"], order.get("status")
    if not _set_unresolved(
        conn, ticket,
        status="executed", kite_order_id=order_id, current_stop_loss=ticket["stop_loss"],
        executed_at=ticket["submitted_at"] or execution.utc_now_sql(), order_variety=order.get("variety"),
        entry_fill_status="pending", filled_quantity=0, submission_state=execution.ADOPTED,
    ):
        return None
    symbol = ticket["symbol"]
    msg = (f"{symbol}: RECOVERED — broker order {order_id} with tag {ticket['order_tag']} found ({status}) and "
           f"adopted. Nothing was resubmitted.")
    _log_event(conn, ticket["id"], "executed", msg)
    never_filled = status in ("REJECTED", "CANCELLED") and not order.get("filled_quantity")
    base = {"ticket_id": ticket["id"], "symbol": symbol, "order_status": status}

    if execution.is_closing_sell(ticket):
        # Same as a confirm that got the order id back (PK-8): the exit is
        # EXITING. The position stays open with its GTT; the pending loop
        # below — in this same cycle — closes it only on a broker COMPLETE,
        # or records the rejection.
        return {**base, "severity": "warning",
                "message": msg + " The exit is EXITING: the position stays open and protected until Kite reports "
                                 "the fill."}

    if never_filled or not ticket["use_gtt"]:
        return {**base, "severity": "warning", "message": msg}
    # The request lost contact before it could place the protective GTT, and
    # this monitor does not place one: a GTT carries no tag, so creating it
    # here could duplicate protection the request placed after all.
    protection = (f"{symbol}: recovered entry order {order_id} has NO protective GTT. "
                  f"UNPROTECTED POSITION once it fills — place a stop-loss in Kite now.")
    _log_event(conn, ticket["id"], "protection_failed", protection)
    return {**base, "severity": "urgent", "message": f"{msg} {protection}"}


def _needs_review(conn: sqlite3.Connection, ticket: sqlite3.Row, matches: list[dict], reason: str) -> dict | None:
    if not _set_unresolved(conn, ticket, submission_state=execution.NEEDS_REVIEW):
        return None
    tag, evidence = ticket["order_tag"], _describe(matches)
    msg = (f"{ticket['symbol']}: RECONCILIATION PROBLEM — {reason}. Tag {tag}; tagged broker orders: {evidence}. "
           f"Nothing was adopted, placed or cancelled. Check Kite and resolve this manually — DO NOT RESUBMIT.")
    _log_event(conn, ticket["id"], "submission_unknown", msg)
    reconciliation.log_mismatch(
        conn, ticket["id"], "order_status",
        expected=f"exactly one broker order tagged {tag} for {ticket['transaction_type']} {ticket['quantity']} "
                 f"{ticket['symbol']}",
        actual=f"{len(matches)} tagged order(s): {evidence}", detail=msg,
    )
    return {"ticket_id": ticket["id"], "symbol": ticket["symbol"], "order_status": None,
            "severity": "urgent", "message": msg}


def _not_found(conn: sqlite3.Connection, ticket: sqlite3.Row) -> dict | None:
    if not _set_unresolved(conn, ticket, status="failed", submission_state=execution.NOT_FOUND):
        return None
    minutes = config.UNKNOWN_ORDER_RECONCILE_WINDOW_SECONDS // 60
    msg = (f"{ticket['symbol']}: no broker order with tag {ticket['order_tag']} appeared within {minutes} min of "
           f"the submission — the order was NOT placed. Nothing was resubmitted; create a fresh ticket if you "
           f"still want this trade.")
    if execution.is_closing_sell(ticket):
        msg += " The position is still open and its protective GTT was left in place."
    _log_event(conn, ticket["id"], "execution_failed", msg)
    return {"ticket_id": ticket["id"], "symbol": ticket["symbol"], "order_status": None,
            "severity": "warning", "message": msg}


def _gtt_is_active(kite, gtt_id) -> bool:
    if gtt_id is None:
        return False
    try:
        book = [g for g in kite.get_gtts() if isinstance(g, dict) and g.get("id") is not None]
    except Exception:
        return False  # cannot verify protection, so do not assume it
    from backend import gtt_observations
    gtt_observations.record_book(book, expected=[gtt_id])
    return any(g["id"] == gtt_id and g.get("status", "active") == "active" for g in book)


# ---- pending-order resolution ----------------------------------------------

def _claim_pending(conn: sqlite3.Connection, ticket: sqlite3.Row, assignments: str, params: tuple) -> bool:
    """The monitor's first write for a pending order, conditional on it still
    being the executed/pending ticket this cycle read."""
    return conn.execute(
        f"UPDATE trade_tickets SET {assignments} WHERE id = ? AND status = 'executed' AND entry_fill_status = 'pending'",
        (*params, ticket["id"]),
    ).rowcount == 1


def _mark_unconfirmed(conn: sqlite3.Connection, ticket: sqlite3.Row) -> dict | None:
    msg = (
        f"{ticket['symbol']}: order {ticket['kite_order_id']} is no longer in the broker's order book, so its "
        f"fill could not be confirmed. The position is treated as open — verify it in Kite."
    )
    if not _claim_pending(conn, ticket, "entry_fill_status = 'unconfirmed'", ()):
        return None
    _log_event(conn, ticket["id"], "modified", msg)
    return {"ticket_id": ticket["id"], "symbol": ticket["symbol"], "order_status": None,
            "severity": "warning", "message": msg}


def _fill_exceeds_order(conn: sqlite3.Connection, ticket: sqlite3.Row, order: dict,
                        position_id: int | None) -> dict | None:
    """The broker reports more filled than was ordered — impossible for a
    correct report. Nothing is recorded, closed or sent: needs a human."""
    filled = order.get("filled_quantity")
    msg = (f"{ticket['symbol']}: BROKER FILL NEEDS REVIEW — order {ticket['kite_order_id']} reports {filled} filled "
           f"but only {ticket['quantity']} were ordered. The figure was NOT recorded; the position is unchanged and "
           f"nothing is sent. Check the order in Kite.")
    update = {"ticket_id": ticket["id"], "symbol": ticket["symbol"], "order_status": order.get("status"),
              "severity": "urgent", "message": msg}
    return _flag(conn, position_id if position_id is not None else ticket["id"], update, "order_status",
                 expected=f"filled_quantity <= {ticket['quantity']}",
                 actual=f"order {ticket['kite_order_id']} filled_quantity={filled}")


def _protection_note(sync: position_quantity.SyncResult, held: int | None) -> str:
    if sync.message:
        return " " + sync.message
    return f" Its protective GTT covers the {held} share(s) held." if held else ""


def _entry_fill_in_progress(conn: sqlite3.Connection, kite, ticket: sqlite3.Row, order: dict) -> dict | None:
    """An entry still working has filled some shares: they are a real
    position now. Record the fill and resize the protective GTT to it — the
    GTT must never cover shares that were not bought (PK-13)."""
    filled = int(order.get("filled_quantity") or 0)
    outcome = position_quantity.record_fill(conn, ticket, filled)
    if outcome == position_quantity.EXCEEDS_REQUESTED:
        return _fill_exceeds_order(conn, ticket, order, None)
    if outcome != position_quantity.INCREASED:
        return None  # already recorded (or an older, lower report): nothing new
    sync = position_quantity.sync_protection(conn, kite, ticket["id"],
                                             f"entry order {ticket['kite_order_id']} has filled {filled}")
    msg = (f"{ticket['symbol']}: PARTIAL FILL — entry order {ticket['kite_order_id']} is {order.get('status')} with "
           f"{filled}/{ticket['quantity']} filled; the position holds {filled} share(s) so far and the rest of the "
           f"order is still working.{_protection_note(sync, filled)}")
    _log_event(conn, ticket["id"], "modified", msg)
    return {"ticket_id": ticket["id"], "symbol": ticket["symbol"], "order_status": order.get("status"),
            "severity": "urgent" if sync.urgent else "warning", "message": msg}


def _close_if_sold_out(conn: sqlite3.Connection, kite, position_id: int, filled: int) -> str:
    """The entry order just ended. If every share it bought was already sold
    while it worked (e.g. an Emergency Exit sized to the fill so far), the
    position is flat now: closed at its last exit's fill."""
    if position_quantity.open_quantity(conn, position_id) != 0:
        return ""
    last_exit = conn.execute(
        "SELECT id, filled_at FROM trade_tickets WHERE closes_ticket_id = ? AND status = 'executed' "
        "AND COALESCE(filled_quantity, 0) > 0 ORDER BY id DESC LIMIT 1",
        (position_id,),
    ).fetchone()
    if last_exit is None:
        return ""
    result = position_quantity.close_if_flat(conn, kite, position_id, last_exit["id"],
                                             last_exit["filled_at"] or execution.utc_now_sql())
    if result.closed:
        _log_event(conn, position_id, "closed",
                   f"Entry order ended with all {filled} filled share(s) already sold (last by exit ticket "
                   f"{last_exit['id']}); position closed.")
        return f" All {filled} share(s) were already sold — position #{position_id} closed." + result.message
    return result.message


def _resolve_entry(conn: sqlite3.Connection, kite, ticket: sqlite3.Row, order: dict) -> dict | None:
    status = order["status"]
    filled = int(order.get("filled_quantity") or 0)
    avg = order.get("average_price") or None
    base = {"ticket_id": ticket["id"], "symbol": ticket["symbol"], "order_status": status}

    if status == "COMPLETE":
        # Kite's COMPLETE is a full fill; a report without a figure is too.
        filled = filled or ticket["quantity"]
        if filled > ticket["quantity"]:
            return _fill_exceeds_order(conn, ticket, order, None)
        # The claim stays the monitor's first write (it is the lock between
        # racing writers); the fill is recorded in the same transaction.
        if not _claim_pending(conn, ticket, "entry_fill_status = 'filled', filled_price = ?, "
                              "filled_at = datetime('now')", (avg or ticket["entry_price"],)):
            return None
        position_quantity.record_fill(conn, ticket, filled)
        msg = (f"{ticket['symbol']}: entry filled — {ticket['transaction_type']} {filled} "
               f"@ {_price(avg)} (order {ticket['kite_order_id']})")
        if filled < ticket["quantity"]:
            msg += f" — only {filled} of the {ticket['quantity']} ordered; the position is {filled} share(s)"
        _log_event(conn, ticket["id"], "modified", msg)
        prior = ticket["filled_quantity"] or 0
        severity = "success"
        if 0 < prior < ticket["quantity"] or filled < ticket["quantity"]:
            # An earlier partial fill resized the GTT below the order (or the
            # broker filled less): make it cover what is now held. A fill
            # straight to COMPLETE left the GTT exactly as placed.
            sync = position_quantity.sync_protection(conn, kite, ticket["id"],
                                                     f"entry order {ticket['kite_order_id']} COMPLETE")
            msg += _protection_note(sync, None)
            severity = "urgent" if sync.urgent else severity
        msg += _close_if_sold_out(conn, kite, ticket["id"], filled)
        still_open = conn.execute("SELECT closed_at FROM trade_tickets WHERE id = ?",
                                  (ticket["id"],)).fetchone()["closed_at"] is None
        if still_open and ticket["use_gtt"] and not _gtt_is_active(kite, ticket["kite_gtt_id"]):
            # Every filled entry's protection is verified at the broker, never
            # assumed: a recovered entry never had its GTT placed by this app
            # (see _adopt), and an entry whose GTT placement failed after the
            # order was accepted (protection_failed) has none either — even
            # when it fills straight to COMPLETE. No GTT is created here: a
            # GTT carries no tag, so a retry could duplicate protection.
            what = "recovered entry" if ticket["submission_state"] == execution.ADOPTED else "entry"
            warning = (f"{ticket['symbol']}: {what} {ticket['kite_order_id']} is now FILLED with NO active "
                       f"protective GTT — UNPROTECTED POSITION, place a stop-loss in Kite now.")
            _log_event(conn, ticket["id"], "protection_failed", warning)
            reconciliation.log_mismatch(
                conn, ticket["id"], "position_state", expected="filled position protected by an active GTT",
                actual=f"kite_gtt_id={ticket['kite_gtt_id']}, no active GTT", detail=warning,
                issue_code=reconciliation.PROTECTION_MISSING, severity=reconciliation.URGENT,
                symbol=ticket["symbol"], gtt_id=ticket["kite_gtt_id"],
            )
            return {**base, "severity": "urgent", "message": f"{msg}. {warning}"}
        return {**base, "severity": severity, "message": msg}

    reason = order.get("status_message") or "no reason given by the broker"
    if filled:
        # PK-13: the order is over, but the shares it bought are a real,
        # open position of exactly that size — never a cancelled ticket.
        if filled > ticket["quantity"]:
            return _fill_exceeds_order(conn, ticket, order, None)
        if not _claim_pending(conn, ticket, "entry_fill_status = 'partial', filled_price = ?, "
                              "filled_at = datetime('now')", (avg,)):
            return None
        position_quantity.record_fill(conn, ticket, filled)
        held = position_quantity.open_quantity(conn, ticket["id"])
        msg = (f"{ticket['symbol']}: entry order {status.lower()} after a PARTIAL fill "
               f"({filled}/{ticket['quantity']}) — {reason}. The position is OPEN with {held} share(s); the "
               f"unfilled {ticket['quantity'] - filled} will not be bought.")
        sync = position_quantity.sync_protection(conn, kite, ticket["id"],
                                                 f"entry order {status.lower()} after filling {filled}")
        msg += _protection_note(sync, held)
        _log_event(conn, ticket["id"], "modified", msg)
        msg += _close_if_sold_out(conn, kite, ticket["id"], filled)
        return {**base, "severity": "urgent" if sync.urgent else "warning", "message": msg}

    if not _claim_pending(conn, ticket, "status = 'cancelled', entry_fill_status = ?", (status.lower(),)):
        return None
    gtt_note, severity = "", "warning"
    if ticket["kite_gtt_id"] is not None:
        # C4: only a cancellation the broker confirms clears the GTT; a failed or
        # unconfirmed one keeps the id (reconciliation keeps watching it), records a
        # persistent finding and is alerted as urgent.
        from backend import gtt_cancellation
        outcome = gtt_cancellation.cancel(kite, ticket["kite_gtt_id"])
        if outcome.confirmed:
            conn.execute("UPDATE trade_tickets SET kite_gtt_id = NULL WHERE id = ?", (ticket["id"],))
            gtt_note = f" Its protective GTT {ticket['kite_gtt_id']} was cancelled ({outcome.detail})"
        else:
            gtt_cancellation.record_finding(
                conn, outcome, ticket_id=ticket["id"], symbol=ticket["symbol"],
                context=f"entry order {ticket['kite_order_id']} {status.lower()} with nothing filled")
            severity = "urgent"
            gtt_note = (f" Its protective GTT {ticket['kite_gtt_id']} was NOT confirmed cancelled "
                        f"({outcome.state}): {outcome.detail} Delete it in Kite now.")
    msg = f"{ticket['symbol']}: entry order {status.lower()} — {reason}. The position never opened.{gtt_note}"
    _log_event(conn, ticket["id"], "rejected", msg)
    return {**base, "severity": severity, "message": msg}


def _fill_time_utc(order: dict) -> str:
    """When the broker filled the order, as UTC SQL text. Kite reports
    exchange (IST) time — a datetime from pykiteconnect, a string from the
    raw API. Now, if the order carries no usable timestamp."""
    for key in ("exchange_timestamp", "order_timestamp"):
        value = order.get(key)
        if isinstance(value, datetime):
            value = value.strftime(_SQL_TIME)
        if isinstance(value, str) and len(value) >= 19:
            try:
                ist = datetime.strptime(value[:19], _SQL_TIME).replace(tzinfo=_IST)
            except ValueError:
                continue
            return ist.astimezone(timezone.utc).strftime(_SQL_TIME)
    return execution.utc_now_sql()


def _flag(conn: sqlite3.Connection, ticket_id: int | None, update: dict, check_type: str, expected: str,
          actual: str) -> dict | None:
    """Records a finding once per distinct `actual`: the same unchanged state
    seen every cycle alerts once, a change (e.g. more shares filled) again."""
    if not reconciliation.log_mismatch(conn, ticket_id, check_type, expected=expected, actual=actual,
                                       detail=update["message"]):
        return None
    if ticket_id is not None:
        _log_event(conn, ticket_id, "modified", update["message"])
    return update


def _flag_partial_exit_in_progress(conn: sqlite3.Connection, kite, ticket: sqlite3.Row,
                                   order: dict) -> dict | None:
    """An exit still working has sold some shares (PK-13): they are gone —
    record the fill, so the position's open quantity drops, and resize its
    protective GTT to what remains. The exit still owns the position."""
    filled, qty, original_id = int(order.get("filled_quantity") or 0), ticket["quantity"], ticket["closes_ticket_id"]
    outcome = position_quantity.record_fill(conn, ticket, filled)
    if outcome == position_quantity.EXCEEDS_REQUESTED:
        return _fill_exceeds_order(conn, ticket, order, original_id)
    protection = ""
    if outcome == position_quantity.INCREASED and original_id is not None:
        sync = position_quantity.sync_protection(conn, kite, original_id,
                                                 f"exit order {ticket['kite_order_id']} has sold {filled}")
        protection = _protection_note(sync, None)
    held = f" {position_quantity.open_quantity(conn, original_id)} share(s) of position #{original_id} remain OPEN;" \
        if original_id is not None else ""
    msg = (f"{ticket['symbol']}: PARTIAL EXIT — exit order {ticket['kite_order_id']} is {order.get('status')} with "
           f"{filled}/{qty} filled.{held} nothing is journaled until the position is flat.{protection} The rest of "
           f"the order is still working — check it in Kite; nothing is resubmitted automatically.")
    update = {"ticket_id": ticket["id"], "symbol": ticket["symbol"], "order_status": order.get("status"),
              "severity": "urgent", "message": msg}
    return _flag(conn, original_id or ticket["id"], update, "order_status",
                 expected=f"exit order filled {qty}/{qty}",
                 actual=f"exit order {ticket['kite_order_id']} {order.get('status')} {filled}/{qty} filled")


def _resolve_exit(conn: sqlite3.Connection, kite, ticket: sqlite3.Row, order: dict) -> dict | None:
    """PK-8: the only place a live explicit exit closes its position — and
    only on broker proof that the full quantity filled."""
    status = order["status"]
    filled = int(order.get("filled_quantity") or 0)
    avg = order.get("average_price") or None
    base = {"ticket_id": ticket["id"], "symbol": ticket["symbol"], "order_status": status}
    original_id = ticket["closes_ticket_id"]

    if filled > ticket["quantity"]:
        return _fill_exceeds_order(conn, ticket, order, original_id)
    if status == "COMPLETE":
        if filled == ticket["quantity"] and avg:
            return _finalize_exit(conn, kite, ticket, order, filled, avg)
        # Never closed on a figure the broker did not give — least of all
        # the reference price. Stays EXITING; flagged once.
        msg = (f"{ticket['symbol']}: exit order {ticket['kite_order_id']} is COMPLETE but reports {filled}/"
               f"{ticket['quantity']} filled at average price {avg}. The position is NOT closed and nothing is "
               f"journaled until the fill is clear — check the order in Kite.")
        return _flag(conn, original_id or ticket["id"], {**base, "severity": "urgent", "message": msg},
                     "order_status", expected=f"COMPLETE, {ticket['quantity']} filled at an average price",
                     actual=f"COMPLETE, filled_quantity={filled}, average_price={avg}")

    reason = order.get("status_message") or "no reason given by the broker"
    if filled:
        # PK-13: the order is over. The shares it sold are sold; the rest of
        # the position is still held, still protected (its GTT resized to
        # it) and no longer owned by this exit — a new exit may sell it.
        fill_time = _fill_time_utc(order)
        if not _claim_pending(conn, ticket, "entry_fill_status = 'partial', filled_price = ?, filled_at = ?",
                              (avg, fill_time)):
            return None
        position_quantity.record_fill(conn, ticket, filled)
        msg = (f"{ticket['symbol']}: exit order {status.lower()} after a PARTIAL fill ({filled}/{ticket['quantity']}) "
               f"— {reason}. PARTIAL EXIT: {filled} share(s) sold @ {_price(avg)}.")
        if original_id is not None:
            held = position_quantity.open_quantity(conn, original_id)
            sync = position_quantity.sync_protection(conn, kite, original_id,
                                                     f"exit order {status.lower()} after selling {filled}")
            msg += (f" {held} share(s) of position #{original_id} remain OPEN and nothing is journaled until it is "
                    f"flat.{_protection_note(sync, held)} This exit no longer holds the position — exit the "
                    f"remaining {held} again if needed; nothing is resubmitted automatically.")
            if held == 0:
                msg += position_quantity.close_if_flat(conn, kite, original_id, ticket["id"], fill_time).message
        _log_event(conn, ticket["id"], "modified", msg)
        if original_id is not None:
            _log_event(conn, original_id, "modified", msg)
        reconciliation.log_mismatch(conn, original_id, "position_state",
                                    expected=f"exit filled {ticket['quantity']}/{ticket['quantity']}",
                                    actual=f"exit {status}, filled_quantity={filled}", detail=msg)
        return {**base, "severity": "urgent", "message": msg}

    if not _claim_pending(conn, ticket, "status = 'failed', entry_fill_status = ?", (status.lower(),)):
        return None
    # Nothing was sold, and since PK-8 nothing was closed, removed or
    # journaled when the exit was placed — so there is nothing to undo. The
    # GTT check only makes the alert accurate: if the GTT is no longer active
    # (fired, or deleted in Kite), the PK-7 watcher resolves it next tick.
    protection = "it has no tracked protective GTT — place a stop-loss or re-issue the exit"
    original = (conn.execute("SELECT kite_gtt_id FROM trade_tickets WHERE id = ?", (original_id,)).fetchone()
                if original_id is not None else None)
    if original is not None and original["kite_gtt_id"] is not None:
        gtt_id = original["kite_gtt_id"]
        protection = (f"its protective GTT {gtt_id} is still active" if _gtt_is_active(kite, gtt_id) else
                      f"its protective GTT {gtt_id} is NOT active at Kite — check it now; the GTT watcher "
                      f"follows it up")
    msg = (f"{ticket['symbol']}: exit order {status.lower()} — {reason}. Nothing was sold: the position is still "
           f"OPEN; {protection}.")
    _log_event(conn, ticket["id"], "execution_failed", msg)
    # original_id is None for an alert_action sell with no originating
    # ticket (see execution.is_closing_sell) — there is no tracked position
    # ticket to log an event against; ticket_events.ticket_id is NOT NULL,
    # so skip that call rather than crash the whole monitor cycle.
    if original_id is not None:
        _log_event(conn, original_id, "modified", msg)
    reconciliation.log_mismatch(conn, original_id, "position_state", expected="exit order filled",
                                actual=f"exit order {status}", detail=msg)
    return {**base, "severity": "urgent", "message": msg}


def _finalize_exit(conn: sqlite3.Connection, kite, ticket: sqlite3.Row, order: dict, filled: int,
                   avg: float) -> dict | None:
    """The broker filled the exit's full quantity: record the fill and, if
    that leaves the position flat, close it at the fill time, journal it
    once, then remove its GTT. The compare-and-set on the exit ticket makes
    this happen exactly once, however many monitor cycles (or processes) see
    the same COMPLETE order. PK-13: a position that still holds shares — its
    entry filled further after this exit was sized — stays OPEN, its GTT
    resized to what remains."""
    fill_time = _fill_time_utc(order)
    if not _claim_pending(conn, ticket, "entry_fill_status = 'filled', filled_price = ?, filled_at = ?",
                          (avg, fill_time)):
        return None
    position_quantity.record_fill(conn, ticket, filled)
    symbol, original_id = ticket["symbol"], ticket["closes_ticket_id"]
    msg = (f"{symbol}: exit FILLED — {ticket['transaction_type']} {filled} @ {_price(avg)} "
           f"(order {ticket['kite_order_id']})")
    _log_event(conn, ticket["id"], "modified", msg)
    base = {"ticket_id": ticket["id"], "symbol": symbol, "order_status": order["status"]}
    if original_id is None:
        # An alert-action sell of an untracked holding: no position to close.
        conn.commit()
        return {**base, "severity": "success", "message": msg}

    position = conn.execute("SELECT closed_at FROM trade_tickets WHERE id = ?", (original_id,)).fetchone()
    if position is not None and position["closed_at"] is not None:
        msg += f" — position #{original_id} was already marked closed."
        note, urgent = position_quantity.remove_protective_gtt(conn, kite, ticket["id"], original_id)
        conn.commit()
        return {**base, "severity": "urgent" if urgent else "success", "message": msg + note}

    # Same transaction as the claim above: the exit is 'filled' exactly when
    # a flat position is closed (close_if_flat commits both).
    result = position_quantity.close_if_flat(conn, kite, original_id, ticket["id"], fill_time)
    if result.closed:
        _log_event(conn, original_id, "closed",
                   f"Exit ticket {ticket['id']}: Kite order {ticket['kite_order_id']} filled {filled} @ {avg}; "
                   f"position closed at the broker fill.")
        conn.commit()
        return {**base, "severity": "urgent" if result.urgent else "success",
                "message": msg + f" — position #{original_id} closed." + result.message}

    held = position_quantity.open_quantity(conn, original_id)
    protection = ""
    if held is not None and held > 0:
        sync = position_quantity.sync_protection(conn, kite, original_id,
                                                 f"exit order {ticket['kite_order_id']} sold {filled}")
        protection = _protection_note(sync, held)
    msg += (f" — position #{original_id} still holds {held} share(s) (its entry filled beyond what this exit was "
            f"sized for, or is still working), so it stays OPEN and nothing is journaled yet.{protection}"
            f"{result.message}")
    _log_event(conn, original_id, "modified", msg)
    conn.commit()
    return {**base, "severity": "urgent", "message": msg}


def _remove_protective_gtt(conn: sqlite3.Connection, kite, exit_ticket: sqlite3.Row,
                           position_id: int) -> tuple[str, bool]:
    # Moved to position_quantity (PK-13); kept for existing callers.
    return position_quantity.remove_protective_gtt(conn, kite, exit_ticket["id"], position_id)
