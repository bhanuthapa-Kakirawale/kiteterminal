import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from kiteconnect.exceptions import GeneralException, InputException, OrderException, TokenException

from backend import (
    config, execution, instruments, journal, kite_client, market, order_monitor, reconciliation, risk_engine,
)
from backend.accounts import get_default_account
from backend.db import get_connection
from backend.ticker import ticker_manager

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
_SQL_TIME = "%Y-%m-%d %H:%M:%S"
# Tag the app puts on every order it places itself (execution.order_tag_for).
# Kite's GTT-placed orders carry no tag, so a tagged order is never a GTT's.
_APP_ORDER_TAG = re.compile(r"^kt\d+x[0-9a-z]+$")

# In-memory only (not persisted) — resets on restart, which is fine: a fresh
# process gets a clean slate of 3 retries before escalating again.
_consecutive_failures: dict[int, int] = {}
# Same idea for a ticket whose whole per-cycle check raised (PK-6).
_consecutive_errors: dict[int, int] = {}
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
    # Every Kite call in a cycle is blocking (up to the client timeout), so
    # the cycle runs in a worker thread, like order_monitor and
    # reconciliation — never on the event loop that serves the UI.
    await asyncio.to_thread(_tick_sync)


def monitored_positions(conn: sqlite3.Connection, account_id: int) -> list[sqlite3.Row]:
    """Every live open position a protective GTT is watching — trailing or
    not. A fixed stop + target position has an OCO GTT whose firing closes
    it just as surely as a trailing stop does (P0-5), so eligibility does
    not depend on trailing_enabled. 'executed' keeps out claimed and
    unknown-outcome submissions (PK-2), and an entry still waiting for its
    fill has no position yet: a GTT id alone is never enough."""
    return conn.execute(
        """
        SELECT * FROM trade_tickets
        WHERE account_id = ? AND mode = 'live' AND status = 'executed'
          AND closed_at IS NULL AND kite_gtt_id IS NOT NULL
          AND COALESCE(entry_fill_status, 'filled') != 'pending'
        ORDER BY id
        """,
        (account_id,),
    ).fetchall()


def _tick_sync() -> None:
    global _last_active_count
    conn = get_connection()
    try:
        account = get_default_account(conn)
        if account["account_type"] != "trading":
            _last_active_count = 0
            return

        tickets = monitored_positions(conn, account["id"])
        _last_active_count = len(tickets)
        if not tickets:
            return

        try:
            kite = kite_client.get_kite()
        except kite_client.NoActiveSessionError:
            return  # heartbeat still fires — UI can tell "ran but skipped" from "not running"

        # One GTT book per cycle for every position. Without it nothing can
        # be judged, so a failure here ends the cycle.
        gtt_book = {g["id"]: g for g in kite.get_gtts()}
        gtt_status = {gtt_id: g.get("status", "active") for gtt_id, g in gtt_book.items()}
        ltp_data = _trailing_ltps(kite, tickets, gtt_status)

        for ticket in tickets:
            # One position's failure must not stop the others being checked:
            # each is its own transaction, rolled back alone if it raises.
            try:
                _process_ticket(kite, conn, ticket, ltp_data, gtt_book)
                conn.commit()
                _consecutive_errors.pop(ticket["id"], None)
            except TokenException:
                conn.rollback()
                raise  # the session is gone — every later call would fail too
            except Exception as e:
                conn.rollback()
                logger.exception("GTT watcher failed for ticket %d (%s); continuing with the rest",
                                 ticket["id"], ticket["symbol"])
                _note_ticket_error(ticket, e)
    finally:
        conn.close()


def _trailing_ltps(kite, tickets: list[sqlite3.Row], gtt_status: dict) -> dict:
    """LTPs, in one batch, for the only positions that need one: trailing
    positions whose GTT is still active. A failure only postpones trailing —
    closure checks do not need an LTP."""
    keys = sorted({
        f"{t['exchange']}:{t['symbol']}" for t in tickets
        if t["trailing_enabled"] and gtt_status.get(t["kite_gtt_id"]) == "active"
    })
    if not keys:
        return {}
    try:
        return kite.ltp(keys)
    except TokenException:
        raise
    except Exception:
        logger.exception("Could not fetch LTPs for trailing; trailing skipped this cycle, GTT checks continue")
        return {}


def _note_ticket_error(ticket: sqlite3.Row, error: Exception) -> None:
    count = _consecutive_errors.get(ticket["id"], 0) + 1
    _consecutive_errors[ticket["id"]] = count
    if count >= 3:
        ticker_manager.broadcast({
            "type": "trailing_error",
            "ticket_id": ticket["id"],
            "message": f"{ticket['symbol']}: checking its protective GTT has failed {count} times in a row: "
                       f"{type(error).__name__}: {error}",
        })


def _process_ticket(kite, conn: sqlite3.Connection, ticket: sqlite3.Row, ltp_data: dict, gtt_book: dict) -> None:
    if ticket["kite_gtt_id"] is None:
        # No GTT exists to manage (e.g. protection_failed at execution time) —
        # nothing to watch until a human intervenes. monitored_positions()
        # already excludes these; kept for any other caller.
        return

    # GTT lifecycle first, for every monitored position; the trailing
    # ratchet after it, for trailing positions only. (get_gtts() lists
    # non-active GTTs too; treating every listed id as active meant a
    # triggered stop was never noticed.)
    gtt = gtt_book.get(ticket["kite_gtt_id"])
    status = None if gtt is None else gtt.get("status", "active")
    if status != "active":
        _handle_inactive_gtt(kite, conn, ticket, status, gtt)
        return

    if not ticket["trailing_enabled"]:
        return  # fixed stop + target (OCO): nothing to ratchet

    key = f"{ticket['exchange']}:{ticket['symbol']}"
    ltp = ltp_data.get(key, {}).get("last_price")
    if ltp is None:
        return

    conn.execute("UPDATE trade_tickets SET last_trailing_check_at = datetime('now') WHERE id = ?", (ticket["id"],))

    if not ticket["trailing_active"]:
        _try_activate(conn, ticket, ltp)
        return

    _try_ratchet(kite, conn, ticket, ltp)


# ---- GTT no longer active: closure needs broker proof (PK-7, P0-6) --------
#
# A GTT leaving 'active' proves nothing about the shares. 'triggered' only
# means Kite PLACED the leg's LIMIT order — a gap through its price leaves it
# unfilled, or part-filled — and a GTT deleted in Kite, expired, or simply
# missing from get_gtts() never sold anything. So a position is closed here
# only on broker evidence that its full quantity was sold, at the broker's
# own fill price; anything less keeps it OPEN and flags what was found. No
# price is ever taken from the LTP, and no order is ever sent.

@dataclass
class ExitEvidence:
    """What Kite's order book shows about one position's exit. Only 'filled'
    may close the position."""
    kind: str  # filled | partial | unfilled | exit_failed | ambiguous | none
    order: dict | None = None
    # True when the order was found through the GTT's own
    # orders[].result.order_result.order_id — Kite's direct GTT-to-order link.
    linked: bool = False
    candidates: list = field(default_factory=list)
    reason: str = ""


def _handle_inactive_gtt(kite, conn: sqlite3.Connection, ticket: sqlite3.Row, status: str | None,
                         gtt: dict | None) -> None:
    owner = _exit_owner(conn, ticket["id"])
    if owner is not None:
        # Another exit already owns this position (PK-4: at most one may —
        # db._ensure_single_exit_owner_index), typically one whose broker
        # outcome is still unknown. Whatever the GTT did, recording anything
        # here would be a guess; flag it and let that exit resolve.
        _handle_owned_closure(conn, ticket, owner["id"], owner["status"], owner["submission_state"])
        return

    evidence = _exit_evidence(kite, conn, ticket, status, gtt)
    if evidence.kind == "filled":
        _handle_gtt_closure(conn, ticket, evidence.order, evidence.linked)
    elif evidence.kind in ("unfilled", "partial"):
        _handle_exit_not_filled(conn, ticket, status, evidence)
    elif evidence.kind == "exit_failed" and (evidence.linked or status == "triggered"):
        _handle_exit_failed(kite, conn, ticket, status, evidence)
    elif evidence.kind == "ambiguous" or status == "triggered":
        # Fired with no attributable order, or several possible ones: the
        # GTT stays watched, so a fill that shows up later still closes it.
        _handle_closure_needs_review(kite, conn, ticket, status, evidence)
    else:
        _handle_protection_lost(kite, conn, ticket, status)


def _exit_owner(conn: sqlite3.Connection, position_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, status, submission_state FROM trade_tickets "
        "WHERE closes_ticket_id = ? AND status IN ('confirmed', 'executed') LIMIT 1",
        (position_id,),
    ).fetchone()


def _exit_evidence(kite, conn: sqlite3.Connection, ticket: sqlite3.Row, status: str | None,
                   gtt: dict | None) -> ExitEvidence:
    exit_side = execution.resolve_exit_side(ticket["transaction_type"])
    # Deliberately not caught: "could not look" must never read as "no fill".
    # The cycle rolls this position back, retries it next cycle and alerts
    # after repeated failures (_note_ticket_error).
    orders = kite.orders()
    fired = [leg["result"] for leg in (gtt or {}).get("orders") or [] if isinstance(leg.get("result"), dict)]
    if fired:
        return _linked_evidence(kite, ticket, exit_side, orders, fired)
    return _matched_evidence(conn, ticket, exit_side, orders, status, gtt)


def _linked_evidence(kite, ticket: sqlite3.Row, exit_side: str, orders: list[dict],
                     fired: list[dict]) -> ExitEvidence:
    """The GTT names the order it placed: that order, and only that order,
    is this position's exit."""
    if len(fired) > 1:
        return ExitEvidence("ambiguous", linked=True, reason=f"the GTT reports {len(fired)} fired legs")
    result = fired[0]
    order_result = result.get("order_result") or {}
    order_id = str(order_result.get("order_id") or result.get("order_id") or "")
    if not order_id:
        why = order_result.get("rejection_reason") or order_result.get("status") or result.get("status")
        return ExitEvidence("exit_failed", linked=True,
                            reason=f"Kite could not place the exit order ({why or 'no reason given'})")

    order = next((o for o in orders if str(o.get("order_id")) == order_id), None)
    if order is None:
        order = order_monitor._order_from_history(kite, order_id)
    if order is None:
        return ExitEvidence("none", linked=True, reason=f"its exit order {order_id} is not in Kite's order book")
    if not _is_exit_order_for(order, ticket, exit_side):
        return ExitEvidence("ambiguous", order=order, linked=True, candidates=[order],
                            reason=f"the order the GTT placed ({_describe(order)}) does not match this position")
    return _classify(order, ticket, linked=True)


def _matched_evidence(conn: sqlite3.Connection, ticket: sqlite3.Row, exit_side: str, orders: list[dict],
                      status: str | None, gtt: dict | None) -> ExitEvidence:
    """No GTT link (the GTT is gone from get_gtts(), or lists no result):
    an order counts only if it cannot plausibly be anything else. Several
    plausible orders, or another app position in the same symbol, and
    nothing is attributed — the most recent is never simply taken."""
    known = _app_order_ids(conn)
    since = _utc_sql_to_ist(ticket["executed_at"])
    # A triggered GTT's order carries one of its legs' prices exactly. Not
    # applied otherwise: a GTT deleted in Kite placed nothing, and a manual
    # SELL of the whole position there is still a real exit.
    leg_prices = set()
    if status == "triggered":
        leg_prices = {round(float(leg["price"]), 2) for leg in (gtt or {}).get("orders") or []
                      if leg.get("price") is not None}

    candidates = [
        o for o in orders
        if _is_exit_order_for(o, ticket, exit_side)
        and o.get("product") in (None, ticket["product_type"])
        and str(o.get("order_id")) not in known
        and not _APP_ORDER_TAG.match(str(o.get("tag") or ""))
        and not _placed_before(o, since)
        and (not leg_prices or round(float(o.get("price") or 0), 2) in leg_prices)
    ]
    live = [o for o in candidates
            if int(o.get("filled_quantity") or 0) > 0 or o.get("status") not in order_monitor.TERMINAL_STATUSES]
    if not live:
        if candidates:
            return ExitEvidence("exit_failed", order=candidates[-1], candidates=candidates,
                                reason=f"the only matching SELL order(s) ended unfilled: {_describe_all(candidates)}")
        return ExitEvidence("none", reason="no SELL order at Kite matches this position")
    if len(live) > 1:
        return ExitEvidence("ambiguous", candidates=live,
                            reason=f"{len(live)} SELL orders could each be its exit: {_describe_all(live)}")
    sharing = _other_positions_in_symbol(conn, ticket)
    if sharing:
        return ExitEvidence("ambiguous", candidates=live,
                            reason=f"SELL order {_describe(live[0])} cannot be attributed: open app position(s) "
                                   f"{', '.join(str(i) for i in sharing)} hold the same symbol")
    return _classify(live[0], ticket, linked=False)


def _classify(order: dict, ticket: sqlite3.Row, linked: bool) -> ExitEvidence:
    filled = int(order.get("filled_quantity") or 0)
    status = order.get("status")
    if status == "COMPLETE" and filled >= ticket["quantity"]:
        kind = "filled"
    elif filled > 0:
        kind = "partial"  # still working or ended: either way not a full exit
    elif status in order_monitor.TERMINAL_STATUSES:
        kind = "exit_failed"
    else:
        kind = "unfilled"
    reason = f"its exit order {_describe(order)}"
    if kind == "exit_failed":
        reason += " ended without filling"
    return ExitEvidence(kind, order=order, linked=linked, candidates=[order], reason=reason)


def _is_exit_order_for(order: dict, ticket: sqlite3.Row, exit_side: str) -> bool:
    return (order.get("tradingsymbol") == ticket["symbol"]
            and order.get("exchange", ticket["exchange"]) == ticket["exchange"]
            and order.get("transaction_type") == exit_side
            and int(order.get("quantity") or 0) == ticket["quantity"])


def _app_order_ids(conn: sqlite3.Connection) -> set[str]:
    """Orders the app already accounts for: its own entries and exits, and
    every order an earlier GTT closure was matched to."""
    rows = conn.execute(
        "SELECT kite_order_id AS id FROM trade_tickets WHERE kite_order_id IS NOT NULL "
        "UNION SELECT json_extract(rule_checklist, '$.gtt_auto_trigger.matched_order_id') FROM trade_tickets "
        "WHERE rule_checklist LIKE '%gtt_auto_trigger%'"
    ).fetchall()
    return {str(r["id"]) for r in rows if r["id"] is not None}


def _other_positions_in_symbol(conn: sqlite3.Connection, ticket: sqlite3.Row) -> list[int]:
    return [p["id"] for p in risk_engine.list_open_positions(conn, ticket["account_id"])
            if p["symbol"] == ticket["symbol"] and p["exchange"] == ticket["exchange"] and p["id"] != ticket["id"]]


def _kite_time(value) -> str | None:
    """A Kite timestamp (IST; a datetime from pykiteconnect, a string from
    the raw API) as 'YYYY-MM-DD HH:MM:SS', or None."""
    if isinstance(value, datetime):
        return value.strftime(_SQL_TIME)
    if isinstance(value, str) and len(value) >= 19:
        return value[:19]
    return None


def _utc_sql_to_ist(value: str | None) -> str | None:
    if not value:
        return None
    return datetime.strptime(value[:19], _SQL_TIME).replace(tzinfo=timezone.utc).astimezone(IST).strftime(_SQL_TIME)


def _ist_to_utc_sql(value: str) -> str:
    return datetime.strptime(value, _SQL_TIME).replace(tzinfo=IST).astimezone(timezone.utc).strftime(_SQL_TIME)


def _placed_before(order: dict, since_ist: str | None) -> bool:
    # A SELL placed before this position was even entered cannot be its
    # exit. An order whose time cannot be read is kept (and still has to be
    # the only plausible one).
    placed = _kite_time(order.get("order_timestamp"))
    return placed is not None and since_ist is not None and placed < since_ist


def _describe(order: dict) -> str:
    return (f"{order.get('order_id')} {order.get('status')} {int(order.get('filled_quantity') or 0)}/"
            f"{order.get('quantity')} filled")


def _describe_all(orders: list[dict]) -> str:
    return "; ".join(_describe(o) for o in orders)


def _held_quantity(kite, symbol: str) -> int | None:
    """Supporting evidence only — never a reason to close: the broker's
    figure covers the whole symbol, shares held outside the app included,
    and says nothing about which order sold or at what price."""
    try:
        return risk_engine.get_held_quantity(kite, symbol)
    except TokenException:
        raise
    except Exception:
        logger.exception("Could not read the held quantity of %s", symbol)
        return None


def _held_note(held: int | None, ticket: sqlite3.Row) -> str:
    if held is None:
        return "The broker's held quantity could not be read."
    if held <= 0:
        return (f"Kite now shows no {ticket['symbol']} held — the shares appear to have gone, but the order and "
                f"price that sold them cannot be identified.")
    return f"Kite shows {held} {ticket['symbol']} held (this position is {ticket['quantity']})."


def _flag(conn: sqlite3.Connection, ticket: sqlite3.Row, check_type: str, expected: str, actual: str,
          detail: str) -> None:
    """Records a finding once per distinct `actual`, and alerts only then:
    an unchanged state re-checked every cycle stays quiet, while any change
    (e.g. more shares filled) alerts again."""
    if reconciliation.log_mismatch(conn, ticket["id"], check_type, expected=expected, actual=actual, detail=detail):
        _log_event(conn, ticket["id"], "modified", detail)
        ticker_manager.broadcast({"type": "trailing_error", "ticket_id": ticket["id"], "message": detail})


def _gtt_state(ticket: sqlite3.Row, status: str | None) -> str:
    return f"protective GTT {ticket['kite_gtt_id']} is {status or 'missing from Kite'}"


def _handle_exit_not_filled(conn: sqlite3.Connection, ticket: sqlite3.Row, status: str | None,
                            evidence: ExitEvidence) -> None:
    order, qty = evidence.order, ticket["quantity"]
    filled = int(order.get("filled_quantity") or 0)
    ended = order.get("status") in order_monitor.TERMINAL_STATUSES
    if filled == 0:
        headline = "GTT TRIGGERED — EXIT NOT FILLED — POSITION MAY BE UNPROTECTED"
        advice = (f"The GTT no longer protects it; its SELL LIMIT @ {order.get('price')} is waiting unfilled. "
                  f"Check that order in Kite now (modify it, or cancel it and place a new stop).")
    else:
        headline = "GTT EXIT PARTIALLY FILLED — POSITION NOT CLOSED"
        left = f"{qty - filled} share(s) are still held"
        advice = (f"{left} and UNPROTECTED: the exit order has ended. Place a stop-loss in Kite." if ended else
                  f"{left} while the rest of the order waits; check it in Kite.")
        advice += (f" Filled so far: {filled} @ avg {order.get('average_price')}. The app still shows the full "
                   f"{qty} until partial exits are modelled.")
    detail = (f"{headline}: {ticket['symbol']} (ticket {ticket['id']}) — {_gtt_state(ticket, status)}; "
              f"{evidence.reason}. The position is kept OPEN and no exit is recorded. {advice}")
    if ended:
        # Nothing left at Kite to watch: this GTT is spent and its order over.
        conn.execute("UPDATE trade_tickets SET kite_gtt_id = NULL WHERE id = ?", (ticket["id"],))
    _flag(conn, ticket, "order_status", expected=f"exit order filled {qty}/{qty}",
          actual=f"exit order {order.get('order_id')} {order.get('status')} {filled}/{qty} filled", detail=detail)


def _handle_exit_failed(kite, conn: sqlite3.Connection, ticket: sqlite3.Row, status: str | None,
                        evidence: ExitEvidence) -> None:
    held = _held_quantity(kite, ticket["symbol"])
    detail = (f"GTT TRIGGERED BUT ITS EXIT ORDER FAILED: {ticket['symbol']} (ticket {ticket['id']}) — "
              f"{_gtt_state(ticket, status)}; {evidence.reason}. {_held_note(held, ticket)} "
              f"The position is still OPEN but UNPROTECTED. Place a stop-loss in Kite now.")
    gtt_id = ticket["kite_gtt_id"]
    conn.execute("UPDATE trade_tickets SET kite_gtt_id = NULL WHERE id = ?", (ticket["id"],))
    _flag(conn, ticket, "order_status", expected=f"GTT {gtt_id} exit order filled",
          actual=f"GTT {gtt_id} fired; exit order failed", detail=detail)


def _handle_closure_needs_review(kite, conn: sqlite3.Connection, ticket: sqlite3.Row, status: str | None,
                                 evidence: ExitEvidence) -> None:
    held = _held_quantity(kite, ticket["symbol"])
    detail = (f"CLOSURE NEEDS REVIEW: {ticket['symbol']} (ticket {ticket['id']}) — {_gtt_state(ticket, status)}, "
              f"but no broker fill can be attributed to this position: {evidence.reason}. {_held_note(held, ticket)} "
              f"The position is kept OPEN; no exit, price or journal entry is assumed. Check Kite's orders and "
              f"holdings.")
    _flag(conn, ticket, "position_state", expected=f"broker fill of {ticket['quantity']} for this position",
          actual=f"GTT {status or 'missing'}; closure unproven: {evidence.reason}; "
                 f"held {'unknown' if held is None else held}",
          detail=detail)


def _handle_gtt_closure(conn: sqlite3.Connection, ticket: sqlite3.Row, fill: dict, linked: bool) -> None:
    """The broker sold this position's full quantity (`fill`, a COMPLETE
    order). Synthesizes a proper executed SELL ticket (closes_ticket_id set)
    at the broker's fill price and time before marking the original closed,
    and runs it through the exact same closed_trades computation exit tickets
    use — one code path for "how did this position's P&L get computed,"
    regardless of whether a human/strategy explicitly exited or Kite
    auto-triggered the stop/target."""
    exit_side = execution.resolve_exit_side(ticket["transaction_type"])
    fill_price = fill["average_price"]
    matched_order_id = fill["order_id"]
    fill_time_ist = _kite_time(fill.get("exchange_timestamp")) or _kite_time(fill.get("order_timestamp"))
    exit_at = _ist_to_utc_sql(fill_time_ist) if fill_time_ist else execution.utc_now_sql()
    rationale = (f"Stop-loss/target hit via GTT auto-trigger: Kite order {matched_order_id} sold "
                 f"{ticket['quantity']} @ {fill_price}")

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
            "evidence": "gtt_order_link" if linked else "order_match",
        }
    })

    # Earlier tickets' writes in this tick stay durable whatever happens below.
    conn.commit()
    try:
        cursor = conn.execute(
            """
            INSERT INTO trade_tickets (
                account_id, source, strategy_name, symbol, exchange, instrument_token,
                transaction_type, quantity, entry_price, entry_order_type,
                stop_loss, target_1, target_2,
                trailing_enabled, product_type, use_gtt, risk_amount, risk_reward_ratio,
                rationale, status, mode, executed_at, closes_ticket_id, rule_checklist,
                entry_fill_status, filled_price, filled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'MARKET', ?, ?, NULL,
                      0, ?, 1, ?, ?, ?, 'executed', ?, ?, ?, ?, 'filled', ?, ?)
            """,
            (
                ticket["account_id"], ticket["source"], ticket["strategy_name"], ticket["symbol"],
                ticket["exchange"], ticket["instrument_token"], exit_side, ticket["quantity"], fill_price,
                ticket["stop_loss"], ticket["target_1"], ticket["product_type"],
                metrics["risk_amount"], metrics["risk_reward_ratio"], rationale, ticket["mode"], exit_at,
                ticket["id"], rule_checklist, fill_price, exit_at,
            ),
        )
    except sqlite3.IntegrityError:
        # An exit was claimed between the owner check and this insert.
        conn.rollback()
        _handle_owned_closure(conn, ticket, None, "confirmed", None)
        return
    sell_ticket_id = cursor.lastrowid
    _log_event(conn, sell_ticket_id, "created", rationale)
    _log_event(conn, sell_ticket_id, "executed", rationale)

    conn.execute("UPDATE trade_tickets SET closed_at = ? WHERE id = ?", (exit_at, ticket["id"]))
    _log_event(conn, ticket["id"], "closed",
               f"Kite order {matched_order_id} filled {ticket['quantity']} @ {fill_price} after the protective GTT "
               f"left 'active'; recorded as closed via ticket {sell_ticket_id}.")
    execution.supersede_sibling_exits(conn, ticket["id"], sell_ticket_id)
    conn.commit()

    try:
        journal.record_closed_trade(conn, sell_ticket_id)
    except ValueError:
        logger.exception("Failed to record closed_trades row for synthesized exit ticket %d", sell_ticket_id)
    ticker_manager.broadcast({"type": "tickets_changed", "reason": "gtt_closed", "ticket_id": ticket["id"]})


def _handle_owned_closure(conn: sqlite3.Connection, ticket: sqlite3.Row, owner_id: int | None,
                          owner_status: str, owner_submission: str | None) -> None:
    owner = f"exit ticket {owner_id}" if owner_id is not None else "another exit ticket"
    detail = (
        f"{ticket['symbol']}: protective GTT {ticket['kite_gtt_id']} is no longer active, but {owner} already owns "
        f"this position ({owner_status}, submission {owner_submission or 'n/a'}). Not recorded as a second exit — "
        f"POSSIBLE DOUBLE SELL. Check Kite's holdings and orders now."
    )
    actual = f"GTT {ticket['kite_gtt_id']} fired while {owner} owns the position"
    if reconciliation._already_logged(conn, ticket["id"], "position_state", actual):
        return  # already flagged; re-checked each tick until the owning exit resolves
    reconciliation.log_mismatch(conn, ticket["id"], "position_state",
                                expected="one exit for this position", actual=actual, detail=detail)
    conn.commit()
    ticker_manager.broadcast({"type": "trailing_error", "ticket_id": ticket["id"], "message": detail})


def _handle_protection_lost(kite, conn: sqlite3.Connection, ticket: sqlite3.Row, gtt_status: str | None) -> None:
    detail = (
        f"PROTECTION MISSING — POSITION STILL OPEN: {ticket['symbol']} (ticket {ticket['id']}) — "
        f"{_gtt_state(ticket, gtt_status)} and no exit fill was found — the position is still OPEN but UNPROTECTED. "
        f"{_held_note(_held_quantity(kite, ticket['symbol']), ticket)} Place a stop-loss in Kite."
    )
    gtt_id = ticket["kite_gtt_id"]
    conn.execute("UPDATE trade_tickets SET kite_gtt_id = NULL WHERE id = ?", (ticket["id"],))
    _flag(conn, ticket, "order_status", expected=f"GTT {gtt_id} active",
          actual=f"GTT {gtt_status or 'missing'}", detail=detail)


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
    tick = instruments.get_tick_size(ticket["symbol"], ticket["exchange"])

    # Snapped onto the tick grid (Kite rejects off-grid trigger prices), in
    # the direction that keeps the trail distance at least as wide as asked.
    if ticket["transaction_type"] == "BUY":
        candidate = market.round_to_tick(ltp - distance, tick, "down")
        should_update = candidate > current_stop
    else:
        candidate = market.round_to_tick(ltp + distance, tick, "up")
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
                "price": execution.buffered_stop_price(candidate, ticket["transaction_type"], tick),
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
