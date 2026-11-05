import json
import logging
import sqlite3

import requests
from fastapi import APIRouter, Depends, HTTPException
from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

from backend import (
    account_safety, execution, instruments, kite_chart, lifecycle, live_arming, live_entry_gate, paper_broker,
    position_quantity,
    reconciliation,
    risk_engine,
    signal_chart,
    system_events,
    ticket_integrity,
)
from backend.accounts import get_trading_account
from backend.db import get_connection
from backend.kite_client import require_trading_account
from backend.obs import log_event
from backend.models import (
    AlertActionSellRequest,
    TicketConfirmRequest,
    TicketCreateRequest,
    TicketEditRequest,
    TicketRejectRequest,
)
from backend.ticker import ticker_manager

logger = logging.getLogger(__name__)
router = APIRouter()

# Only a ticket that passed its risk checks may execute — a 'draft' never
# has. Reject/cancel still accept a draft: withdrawing one is always safe.
_CONFIRMABLE_STATUSES = ("pending_confirmation",)
_REJECTABLE_STATUSES = ("draft", "pending_confirmation")


def _log_event(conn: sqlite3.Connection, ticket_id: int, event_type: str, detail: str | None) -> None:
    conn.execute(
        "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, ?, ?)",
        (ticket_id, event_type, detail),
    )


def _transition(
    conn: sqlite3.Connection, ticket_id: int, account_id: int,
    from_statuses: tuple[str, ...], to_status: str,
    extra_condition: tuple[str, tuple] | None = None, **columns,
) -> bool:
    """Compare-and-set status change: moves the ticket to to_status only if it
    is still in one of from_statuses. The UPDATE's WHERE clause is the lock —
    SQLite serialises writers, so of two requests racing for the same ticket
    exactly one gets rowcount 1. True means this caller won the transition and
    owns whatever follows it (placing the order, writing the event). Column
    names come from code, never from request input. extra_condition is one
    more (sql, params) predicate evaluated in the same UPDATE — used by the
    live-entry claim for the PK-12C daily entry limit."""
    assignments = ", ".join(["status = ?", *(f"{name} = ?" for name in columns)])
    placeholders = ", ".join("?" * len(from_statuses))
    extra_sql, extra_params = (f" AND {extra_condition[0]}", extra_condition[1]) if extra_condition else ("", ())
    cursor = conn.execute(
        f"UPDATE trade_tickets SET {assignments} "
        f"WHERE id = ? AND account_id = ? AND status IN ({placeholders}){extra_sql}",
        (to_status, *columns.values(), ticket_id, account_id, *from_statuses, *extra_params),
    )
    return cursor.rowcount == 1


def _raise_invalid_state(conn: sqlite3.Connection, ticket_id: int, account_id: int, verb: str):
    ticket = _get_ticket_or_404(conn, ticket_id, account_id)
    raise HTTPException(409, {
        "error_code": "CONFLICT_INVALID_STATE",
        "message": f"Cannot {verb} a ticket in status '{ticket['status']}'",
    })


def _notify(reason: str, **extra) -> None:
    # Lets every open browser tab refresh its queues/badges immediately.
    ticker_manager.broadcast({"type": "tickets_changed", "reason": reason, **extra})


def _get_ticket_or_404(conn: sqlite3.Connection, ticket_id: int, account_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM trade_tickets WHERE id = ? AND account_id = ?", (ticket_id, account_id)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"error_code": "NOT_FOUND", "message": "Ticket not found"})
    return row


def _serialize(ticket: sqlite3.Row) -> dict:
    data = dict(ticket)
    metrics = execution.compute_risk_metrics(data)
    data["capital"] = metrics["capital"]
    data["target_2_risk_reward_ratio"] = metrics["target_2_risk_reward_ratio"]
    data["risk_check_results"] = json.loads(data["risk_check_results"]) if data.get("risk_check_results") else []
    # Parsed the same way risk_check_results is, so the frontend never has to
    # JSON.parse a field of a ticket payload itself.
    data["entry_evaluation"] = signal_chart.parse_evaluation(data.get("entry_evaluation"))
    # The three link variants are derived, not stored — see backend/kite_chart.py.
    data["kite_chart"] = {
        "url": data.get("kite_chart_url"),
        "status": data.get("kite_chart_status"),
        "options": kite_chart.fallback_chain(
            data["symbol"], data.get("exchange") or "NSE",
            data.get("instrument_token"), data.get("kite_chart_status") or kite_chart.STATUS_OK,
        ),
    }
    # PK-8: 'exiting' = exit order placed, not filled; the position is open.
    data["exit_state"] = execution.exit_state(ticket)
    # PK-19: what happened to this ticket and its order, decided here — the
    # frontend renders it. The position lifecycle is added by the callers
    # that hold a lifecycle context (_with_lifecycle).
    data["ticket_state"] = lifecycle.ticket_state(ticket)
    # SEC-1 P1-E: status 'executed' means the broker ACCEPTED the order; how
    # much of it filled is stated separately, from broker-reported fills only.
    data["order_progress"] = lifecycle.order_progress(ticket)
    return data


def _review_payload(conn: sqlite3.Connection, ticket: sqlite3.Row, session: tuple | None = None) -> dict:
    """P1-D: the ticket's review state (account_safety.review_status) — what
    the approval views show and the token an approval echoes. `session` is a
    one-element tuple holding the active sessions.id when the caller already
    looked it up (one lookup for a whole list)."""
    if session is None:
        return account_safety.review_status(conn, ticket["account_id"], ticket)
    return account_safety.review_status(conn, ticket["account_id"], ticket, session_id=session[0], session_known=True)


def _serialize_reviewed(conn: sqlite3.Connection, ticket: sqlite3.Row) -> dict:
    """_serialize plus the review state, for every response a view can open
    the approval dialog from (create, Quick-Action buy, edit, re-review)."""
    data = _serialize(ticket)
    data["review"] = _review_payload(conn, ticket)
    return data


def _with_lifecycle(conn: sqlite3.Connection, ticket: sqlite3.Row, ctx: lifecycle.Context | None = None,
                    session: tuple | None = None) -> dict:
    """A ticket payload with its PK-13 quantities, PK-19 position lifecycle
    (from one lifecycle context, built here when not given) and P1-D review
    state."""
    ctx = ctx or lifecycle.build_context(conn, ticket["account_id"])
    data = _serialize(ticket)
    data.update(_quantity_fields(ticket, ctx))
    data["lifecycle"] = lifecycle.for_ticket(ticket, ctx)
    data["review"] = _review_payload(conn, ticket, session)
    return data


def _run_risk_checks_and_transition(
    conn: sqlite3.Connection, account: sqlite3.Row, ticket_id: int, kite: KiteConnect
) -> None:
    """Thin HTTP-aware wrapper around risk_engine.run_risk_checks_and_transition
    (shared with the Phase 5 strategy engine, which is not FastAPI-aware) —
    translates RiskCheckUnavailableError into an HTTPException."""
    try:
        risk_engine.run_risk_checks_and_transition(conn, account, ticket_id, kite)
    except risk_engine.RiskCheckUnavailableError as e:
        raise HTTPException(status_code=e.status_code, detail={"error_code": e.error_code, "message": e.message})


def _refuse_unsupported_entry(req: TicketCreateRequest | TicketEditRequest) -> None:
    """PK-17: a ticket created or edited here always opens a NEW position,
    and only BUY + CNC has a modelled lifecycle. Refused, never rewritten —
    sells go through Exit, Quick Action Sell or Emergency Exit instead. Runs
    before any price check, instrument lookup, insert or broker call."""
    message = execution.unsupported_entry_message(req.transaction_type, req.product_type)
    if message is not None:
        raise HTTPException(400, {
            "error_code": execution.UNSUPPORTED_MANUAL_TRADE,
            "message": message,
            "transaction_type": req.transaction_type,
            "product_type": req.product_type,
        })


def _validate_prices(req: TicketCreateRequest | TicketEditRequest) -> None:
    if req.transaction_type == "BUY":
        if not (req.stop_loss < req.entry_price < req.target_1):
            raise HTTPException(400, {
                "error_code": "VALIDATION_ERROR",
                "message": "For a BUY, stop_loss must be < entry_price < target_1",
            })
        if req.target_2 is not None and req.target_2 <= req.target_1:
            raise HTTPException(400, {
                "error_code": "VALIDATION_ERROR",
                "message": "For a BUY, target_2 must be > target_1",
            })
    else:
        if not (req.target_1 < req.entry_price < req.stop_loss):
            raise HTTPException(400, {
                "error_code": "VALIDATION_ERROR",
                "message": "For a SELL, target_1 must be < entry_price < stop_loss",
            })
        if req.target_2 is not None and req.target_2 >= req.target_1:
            raise HTTPException(400, {
                "error_code": "VALIDATION_ERROR",
                "message": "For a SELL, target_2 must be < target_1",
            })

    if req.trailing_enabled and (req.trailing_trigger is None or req.trailing_distance is None):
        raise HTTPException(400, {
            "error_code": "VALIDATION_ERROR",
            "message": "trailing_enabled requires both trailing_trigger and trailing_distance",
        })


def _create_ticket_internal(
    req: TicketCreateRequest, kite: KiteConnect, account: sqlite3.Row, source: str
) -> dict:
    """Shared body behind both the public manual-ticket endpoint
    (source='manual') and the Quick Action popup's buy/average-down endpoint
    (source='alert_action') — same factor-out-the-shared-body pattern as
    confirm_and_execute_ticket below, which is likewise shared by the
    single-ticket confirm endpoint and Emergency Exit All's batch loop."""
    _refuse_unsupported_entry(req)
    _validate_prices(req)

    instrument = instruments.get_instrument_by_symbol(req.symbol, req.exchange)
    if instrument is None:
        raise HTTPException(400, {
            "error_code": "INSTRUMENT_NOT_FOUND",
            "message": f"Could not resolve '{req.symbol}' on {req.exchange}. "
                       f"Check spelling or refresh the instrument list.",
        })

    metrics = execution.compute_risk_metrics(req.model_dump())

    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO trade_tickets (
                account_id, source, symbol, exchange, instrument_token,
                transaction_type, quantity, entry_price, entry_order_type,
                stop_loss, target_1, target_2,
                trailing_enabled, trailing_trigger, trailing_distance,
                product_type, use_gtt, risk_amount, risk_reward_ratio,
                rationale, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft')
            """,
            (
                account["id"], source, req.symbol, req.exchange, instrument["instrument_token"],
                req.transaction_type, req.quantity, req.entry_price, req.entry_order_type,
                req.stop_loss, req.target_1, req.target_2,
                int(req.trailing_enabled), req.trailing_trigger, req.trailing_distance,
                req.product_type, int(req.use_gtt), metrics["risk_amount"], metrics["risk_reward_ratio"],
                req.rationale,
            ),
        )
        ticket_id = cursor.lastrowid
        # P1-D: creating the ticket is its first review — stamped from the
        # stored row, in the same transaction as the insert.
        _stamp_review(conn, ticket_id, account["id"], with_digest=True)
        _log_event(conn, ticket_id, "created", f"Ticket created (source={source})")
        conn.commit()

        _run_risk_checks_and_transition(conn, account, ticket_id, kite)
        _notify("created", ticket_id=ticket_id)

        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        return _serialize_reviewed(conn, ticket)
    finally:
        conn.close()


def _stamp_review(conn: sqlite3.Connection, ticket_id: int, account_id: int, with_digest: bool) -> None:
    """P1-D: records a review of a manual / Quick-Action BUY entry happening
    now — the account-safety clock and the active Kite session — and, at
    creation or edit only, the digest of the content as STORED (re-read on
    this connection, so bools/ints and the cached instrument token hash
    exactly as confirmation will read them). Caller commits. Never used for
    strategy entries or exits."""
    row = _get_ticket_or_404(conn, ticket_id, account_id)
    if not account_safety.is_review_subject(row):
        return
    columns = account_safety.review_stamp(conn, account_id)
    if with_digest:
        columns["review_digest"] = account_safety.ticket_review_digest(row)
    assignments = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(f"UPDATE trade_tickets SET {assignments} WHERE id = ? AND account_id = ?",
                 (*columns.values(), ticket_id, account_id))


@router.post("/api/tickets", status_code=201)
def create_ticket(req: TicketCreateRequest, kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    return {"data": _create_ticket_internal(req, kite, account, source="manual")}


@router.post("/api/tickets/alert-action/buy", status_code=201)
def create_alert_action_buy(req: TicketCreateRequest, kite: KiteConnect = Depends(require_trading_account)):
    # Quick Action popup's "Buy more (average down)" action. Full risk
    # engine runs, no skip logic — this is risk-ADDING, unlike the sell
    # action below. Deliberately reuses TicketCreateRequest as-is: it needs
    # the exact same required fields (stop_loss/target_1 are a fresh risk
    # decision for THIS trade, never auto-filled or exempted).
    if req.transaction_type != "BUY":
        raise HTTPException(400, {
            "error_code": "VALIDATION_ERROR",
            "message": "The alert-action buy endpoint only creates BUY tickets.",
        })
    account = get_trading_account()
    return {"data": _create_ticket_internal(req, kite, account, source="alert_action")}


@router.post("/api/tickets/alert-action/sell", status_code=201)
def create_alert_action_sell(req: AlertActionSellRequest, kite: KiteConnect = Depends(require_trading_account)):
    # Quick Action popup's "Sell to cut loss" / "Sell to reduce exposure"
    # action. Unlike a normal manual ticket, this always reduces/exits a
    # currently-held position — stop_loss/target_1/product_type are derived
    # server-side from the broker's own holdings data, never trusted from
    # the client (same "never trust the client" precedent as
    # instrument_token resolution above).
    account = get_trading_account()
    instrument = instruments.get_instrument_by_symbol(req.symbol, req.exchange)
    if instrument is None:
        raise HTTPException(400, {
            "error_code": "INSTRUMENT_NOT_FOUND",
            "message": f"Could not resolve '{req.symbol}' on {req.exchange}. "
                       f"Check spelling or refresh the instrument list.",
        })

    holdings = kite.holdings()
    holding = next((h for h in holdings if h["tradingsymbol"] == req.symbol), None)
    if holding is None:
        raise HTTPException(400, {
            "error_code": "NOT_HELD",
            "message": f"'{req.symbol}' is not currently held — nothing to sell.",
        })

    held_qty = risk_engine.get_held_quantity(kite, req.symbol)
    if req.quantity > held_qty:
        raise HTTPException(400, {
            "error_code": "EXCEEDS_HELD_QUANTITY",
            "message": f"Requested quantity {req.quantity} exceeds the {held_qty} shares currently held.",
        })

    average_price = holding["average_price"]
    product_type = holding.get("product") or "CNC"

    conn = get_connection()
    try:
        # Only link to a specific tracked ticket on a genuine FULL close of
        # what it holds (PK-13: its OPEN quantity — the shares the broker
        # filled, less any already sold — never its requested quantity). A
        # user-chosen partial sell of a tracked position is not modelled: a
        # partial reduction (the common case for the concentration action)
        # always falls through to case (b): no closes_ticket_id, validated
        # instead against held quantity by risk_engine._check_exit_or_reduction.
        open_positions = [p for p in risk_engine.list_open_positions(conn, account["id"]) if p["symbol"] == req.symbol]
        closes_ticket_id = None
        # A linked full close inherits the position's mode (PK-3); an
        # unlinked reduction stays NULL and takes the global mode at claim.
        mode = None
        link_note = "none (partial reduction)"
        if len(open_positions) == 1 and open_positions[0]["open_quantity"] == req.quantity:
            closes_ticket_id = open_positions[0]["id"]
            mode = open_positions[0]["mode"]
            link_note = "full_close"
        elif len(open_positions) > 1:
            link_note = f"none ({len(open_positions)} open tickets for this symbol, ambiguous which to link)"

        rule_checklist = json.dumps({
            "alert_action": {
                "trigger": req.alert_type,
                "held_quantity": held_qty,
                "average_price": average_price,
                "linked_ticket": link_note,
            }
        })

        cursor = conn.execute(
            """
            INSERT INTO trade_tickets (
                account_id, source, symbol, exchange, instrument_token,
                transaction_type, quantity, entry_price, entry_order_type,
                stop_loss, target_1, product_type, use_gtt,
                rationale, status, closes_ticket_id, rule_checklist, mode
            ) VALUES (?, 'alert_action', ?, ?, ?, 'SELL', ?, ?, ?, ?, ?, ?, 1, ?, 'draft', ?, ?, ?)
            """,
            (
                account["id"], req.symbol, req.exchange, instrument["instrument_token"],
                req.quantity, req.entry_price, req.entry_order_type,
                average_price, average_price, product_type,
                req.rationale, closes_ticket_id, rule_checklist, mode,
            ),
        )
        ticket_id = cursor.lastrowid
        _log_event(conn, ticket_id, "created", "Ticket created (source=alert_action): "
                                                f"{req.alert_type} on {req.symbol}")
        conn.commit()

        _run_risk_checks_and_transition(conn, account, ticket_id, kite)
        _notify("created", ticket_id=ticket_id)

        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        return {"data": _serialize_reviewed(conn, ticket)}
    finally:
        conn.close()


@router.get("/api/tickets")
def list_tickets(
    status: str | None = None,
    source: str | None = None,
    symbol: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    include_archived: bool = False,
    kite: KiteConnect = Depends(require_trading_account),
):
    account = get_trading_account()
    conn = get_connection()
    try:
        clauses = ["account_id = ?"]
        params: list = [account["id"]]
        if not include_archived:
            clauses.append("archived = 0")
        if status:
            clauses.append("status = ?")
            params.append(status)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if symbol:
            clauses.append("symbol LIKE ?")
            params.append(f"%{symbol.strip().upper()}%")
        if date_from:
            clauses.append("created_at >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("created_at <= ?")
            params.append(date_to)

        rows = conn.execute(
            f"SELECT * FROM trade_tickets WHERE {' AND '.join(clauses)} ORDER BY created_at DESC",
            params,
        ).fetchall()
        # PK-19: one lifecycle context for the whole list — a constant number
        # of queries, however many tickets (it replaced a per-ticket open
        # quantity query).
        ctx = lifecycle.build_context(conn, account["id"])
        in_flight = _exits_in_flight(ctx)
        # P1-D: one active-session lookup for every row's review state.
        session = (live_arming.active_session_id(conn, account["id"]),)
        data = []
        for r in rows:
            item = _with_lifecycle(conn, r, ctx, session)
            item["exit_in_flight_ticket_id"] = in_flight.get(r["id"])
            data.append(item)
        return {"data": data}
    finally:
        conn.close()


def _exits_in_flight(ctx: lifecycle.Context) -> dict[int, int]:
    """position id -> the exit ticket that owns it while its order is in
    flight (claimed/UNKNOWN, EXITING, unconfirmed): the position still
    exists, but an exit is in flight (PK-8). An exit that ended after a
    partial fill no longer owns it (PK-13). (execution.EXIT_IN_FLIGHT_SQL,
    evaluated on the rows the lifecycle context already read.)"""
    return {position_id: x["id"] for position_id, exits in ctx.exits.items()
            for x in exits if lifecycle.exit_in_flight(x)}


def _quantity_fields(ticket: sqlite3.Row, ctx: lifecycle.Context) -> dict:
    """PK-13: requested vs filled vs open. For a position (an executed entry):
    open_quantity = shares still held. For an exit: remaining_quantity = what
    its order has not sold."""
    bought = position_quantity.filled(ticket)
    fields = {"filled_quantity": bought, "open_quantity": None, "remaining_quantity": None}
    if ticket["status"] != "executed":
        return fields
    if ticket["closes_ticket_id"] is None and not execution.is_closing_sell(ticket):
        fields["open_quantity"] = 0 if ticket["closed_at"] else position_quantity.open_quantity_from_rows(
            ticket, ctx.exits.get(ticket["id"], []))
    elif bought is not None:
        fields["remaining_quantity"] = ticket["quantity"] - bought
    return fields


@router.get("/api/tickets/{ticket_id}/integrity")
def get_ticket_integrity(ticket_id: int, kite: KiteConnect = Depends(require_trading_account)):
    """The ticket's post-claim integrity audit (backend/ticket_integrity.py): its claim
    snapshots and any tamper detections (digests and category names only), and whether the
    append-only, hash-chained log still verifies. Read-only."""
    account = get_trading_account()
    conn = get_connection()
    try:
        _get_ticket_or_404(conn, ticket_id, account["id"])
        return {"data": {"ticket_id": ticket_id, "tampered": ticket_integrity.tampered(conn, ticket_id),
                         "events": ticket_integrity.events(conn, account["id"], ticket_id),
                         "chain": ticket_integrity.verify_chain(conn)}}
    finally:
        conn.close()


@router.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: int, kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        events = conn.execute(
            "SELECT * FROM ticket_events WHERE ticket_id = ? ORDER BY created_at ASC", (ticket_id,)
        ).fetchall()
        data = _with_lifecycle(conn, ticket)
        data["events"] = [dict(e) for e in events]
        return {"data": data}
    finally:
        conn.close()


@router.get("/api/tickets/{ticket_id}/chart")
def get_ticket_chart(ticket_id: int, kite: KiteConnect = Depends(require_trading_account)):
    """Everything the Signal Detail view needs, in one payload: candles and
    indicator series from ohlc_daily, the ticket's already-tick-snapped
    levels, the bars the strategy keyed off, and the freshness/expiry state.

    Read-only and Kite-free by design — see backend/signal_chart.py. The
    `kite` dependency is the standard server-side auth/permission gate every
    other ticket route carries, not a data source.
    """
    account = get_trading_account()
    conn = get_connection()
    try:
        # PK-19: the ticket's own state and its position's lifecycle, shown
        # side by side in the Signal Detail header.
        ticket = _with_lifecycle(conn, _get_ticket_or_404(conn, ticket_id, account["id"]))

        # Tickets created before this feature shipped have no stored chart
        # URL. Rather than showing them a dead link, resolve once on first
        # view and write it back, so the backlog of already-pending signals
        # behaves exactly like a freshly created one from here on.
        if ticket.get("kite_chart_status") is None:
            resolved = kite_chart.resolve_and_log(
                conn, account["id"], ticket["symbol"], ticket.get("exchange") or "NSE",
                ticket.get("instrument_token"),
            )
            conn.execute(
                "UPDATE trade_tickets SET kite_chart_url = ?, kite_chart_status = ? WHERE id = ?",
                (resolved["url"], resolved["status"], ticket_id),
            )
            conn.commit()
            ticket["kite_chart_url"] = resolved["url"]
            ticket["kite_chart_status"] = resolved["status"]

        settings = risk_engine.get_settings(conn, account["id"])
        return {"data": signal_chart.build_payload(ticket, settings)}
    finally:
        conn.close()


@router.post("/api/tickets/{ticket_id}/reject")
def reject_ticket(ticket_id: int, req: TicketRejectRequest, kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        if ticket["status"] not in _REJECTABLE_STATUSES:
            _raise_invalid_state(conn, ticket_id, account["id"], "reject")
        # The check above only gives an early, informative 409. The ticket
        # may be confirmed between it and here, so the transition itself is
        # conditional: a lost race must never overwrite 'confirmed'.
        if not _transition(conn, ticket_id, account["id"], _REJECTABLE_STATUSES, "rejected"):
            conn.rollback()
            _raise_invalid_state(conn, ticket_id, account["id"], "reject")
        _log_event(conn, ticket_id, "rejected", req.reason)
        conn.commit()
        _notify("rejected", ticket_id=ticket_id)
        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        return {"data": _serialize_reviewed(conn, ticket)}
    finally:
        conn.close()


@router.post("/api/tickets/{ticket_id}/revert-rejection")
def revert_rejection(ticket_id: int, kite: KiteConnect = Depends(require_trading_account)):
    """Undo a mistaken reject: puts the ticket back through risk checks so it
    lands back wherever a fresh ticket would (pending_confirmation or
    risk_rejected) — for a strategy-sourced ticket this makes it reappear in
    the Signal Queue, exactly as if it had never been rejected."""
    account = get_trading_account()
    conn = get_connection()
    try:
        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        if ticket["status"] != "rejected":
            raise HTTPException(409, {
                "error_code": "CONFLICT_INVALID_STATE",
                "message": f"Only a rejected ticket can be reverted (current status: '{ticket['status']}')",
            })
        if ticket_integrity.tampered(conn, ticket_id):
            raise HTTPException(409, {
                "error_code": ticket_integrity.TAMPER_CODE,
                "message": "This ticket's details were changed after it was claimed for execution; it can never "
                           "be reverted or approved. Create a new ticket.",
            })
        # Conditional (PK-4): a second revert, or any other change since the
        # read above, must not be overwritten.
        if not _transition(conn, ticket_id, account["id"], ("rejected",), "draft",
                           archived=0, risk_check_results=None):
            conn.rollback()
            _raise_invalid_state(conn, ticket_id, account["id"], "revert")
        _log_event(conn, ticket_id, "modified", "Rejection reverted by user; risk checks re-run")
        conn.commit()

        _run_risk_checks_and_transition(conn, account, ticket_id, kite)
        _notify("reverted", ticket_id=ticket_id)

        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        return {"data": _serialize_reviewed(conn, ticket)}
    finally:
        conn.close()


@router.post("/api/tickets/{ticket_id}/re-review")
def re_review_ticket(ticket_id: int, kite: KiteConnect = Depends(require_trading_account)):
    """SEC-1 P1-D: the explicit human re-review of a manual / Quick-Action
    BUY entry whose review is no longer current (an NSE open/close or a new
    Kite login since). Re-runs every risk check against the broker's
    portfolio as it is now, then records the review — this time, this Kite
    session — on the ticket, which stays 'pending_confirmation' (or becomes
    'risk_rejected' if a check now fails). Nothing is sent to the broker.

    The content is never re-stamped: the stored review digest is kept, and
    a ticket whose content no longer matches it is rejected instead
    (TICKET_EVIDENCE_MISMATCH) — a re-review must not bless tampered
    content. Only a row created before reviews were recorded gets its
    first digest here, from the content the user is re-reviewing. If the
    checks cannot run, nothing changes (503). Allowed while trading is
    locked: preparing a ticket under the lock is the documented flow."""
    account = get_trading_account()
    conn = get_connection()
    try:
        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        if not account_safety.is_review_subject(ticket):
            raise HTTPException(409, {
                "error_code": account_safety.TICKET_REVIEW_NOT_APPLICABLE,
                "message": f"Ticket {ticket_id} is not a manual or Quick Action buy entry: strategy signals are "
                           f"judged by their candle and exits are never held back for review.",
            })
        if ticket["status"] not in _CONFIRMABLE_STATUSES:
            _raise_invalid_state(conn, ticket_id, account["id"], "re-review")
        evidence = account_safety.evidence_problem(ticket)
        if evidence is not None:
            if _transition(conn, ticket_id, account["id"], _CONFIRMABLE_STATUSES, "rejected"):
                _log_event(conn, ticket_id, "rejected",
                           f"Refused at re-review ({evidence.code}): {evidence.reason}. No order was sent.")
                conn.commit()
                _notify("rejected", ticket_id=ticket_id)
            else:
                conn.rollback()
                _raise_invalid_state(conn, ticket_id, account["id"], "re-review")
            raise HTTPException(409, {
                "error_code": evidence.code,
                "message": f"Ticket {ticket_id}: {evidence.reason}. It cannot be re-reviewed and has been "
                           f"rejected — create a new ticket.",
            })

        try:
            ctx = risk_engine.build_portfolio_context(kite)
            report = risk_engine.run_checks(conn, account, ticket, kite, ctx=ctx)
        except Exception as e:
            # The checks were never evaluated: nothing is recorded, the
            # ticket keeps its (not current) review.
            conn.rollback()
            reason = e.message if isinstance(e, risk_engine.RiskCheckUnavailableError) else f"{type(e).__name__}: {e}"
            raise HTTPException(503, {
                "error_code": "RISK_REVALIDATION_UNAVAILABLE",
                "message": f"Risk could not be re-checked ({reason}); the ticket was not re-reviewed and is "
                           f"unchanged. Try again once the broker is reachable.",
            })

        results = json.dumps(report.as_list())
        # Compare-and-set on the state read above: a confirmation, reject or
        # another re-review that got there first is never overwritten. The
        # review generation is part of it, so of two concurrent re-reviews
        # exactly one commits its new generation; the other gets 409.
        unchanged = ("review_digest IS ? AND review_generation IS ?",
                     (ticket["review_digest"], ticket["review_generation"]))
        if report.passed:
            columns = account_safety.review_stamp(conn, account["id"])
            if ticket["review_digest"] is None:
                columns["review_digest"] = account_safety.ticket_review_digest(ticket)
            if not _transition(conn, ticket_id, account["id"], _CONFIRMABLE_STATUSES, "pending_confirmation",
                               extra_condition=unchanged, risk_check_results=results, **columns):
                conn.rollback()
                _raise_invalid_state(conn, ticket_id, account["id"], "re-review")
            window = account_safety.review_window(account_safety.now_ist())
            _log_event(conn, ticket_id, "modified",
                       f"Re-reviewed by the user under Kite session #{columns['reviewed_session_id']} "
                       f"({window[0].lower()} window, {window[1]}); risk checks re-run against the current "
                       f"portfolio: all {len(report.checks)} passed.")
        else:
            if not _transition(conn, ticket_id, account["id"], _CONFIRMABLE_STATUSES, "risk_rejected",
                               extra_condition=unchanged, risk_check_results=results):
                conn.rollback()
                _raise_invalid_state(conn, ticket_id, account["id"], "re-review")
            _log_event(conn, ticket_id, "risk_rejected",
                       "Re-review: " + "; ".join(c.message for c in report.checks if not c.passed))
        conn.commit()
        _notify("re_reviewed", ticket_id=ticket_id)

        data = _serialize_reviewed(conn, _get_ticket_or_404(conn, ticket_id, account["id"]))
        data["re_review"] = {"passed": report.passed,
                             "failed_checks": [c.rule for c in report.checks if not c.passed]}
        return {"data": data}
    finally:
        conn.close()


def _claim_mode(conn: sqlite3.Connection, ticket: sqlite3.Row, account_id: int) -> tuple[str, str]:
    """The execution mode this ticket is claimed in (PK-3), plus a note for
    the 'confirmed' event. The global mode decides only NEW entries (and an
    alert-action sell of an untracked holding). A ticket that closes a
    tracked position always executes in that position's own mode — a live
    position is never "exited" by a simulation, and a dry-run one never sends
    a real SELL. Exit creation stamps the inherited mode; this is the
    backstop. Race-free: an executed position's mode is never rewritten."""
    if ticket["closes_ticket_id"] is None:
        return execution.get_execution_mode(), ""
    position = conn.execute(
        "SELECT mode FROM trade_tickets WHERE id = ? AND account_id = ?",
        (ticket["closes_ticket_id"], account_id),
    ).fetchone()
    if position is None or position["mode"] is None:
        raise HTTPException(409, {
            "error_code": "EXIT_MODE_UNRESOLVED",
            "message": f"Exit ticket {ticket['id']} references position {ticket['closes_ticket_id']}, whose "
                       f"execution mode cannot be determined. Nothing was executed.",
        })
    if ticket["mode"] is not None and ticket["mode"] != position["mode"]:
        raise HTTPException(409, {
            "error_code": "EXIT_MODE_MISMATCH",
            "message": f"Exit ticket {ticket['id']} is marked {ticket['mode']} but position "
                       f"{ticket['closes_ticket_id']} is {position['mode']}. Nothing was executed — reject this "
                       f"ticket.",
        })
    # Exits created before PK-3 carry no mode yet; they inherit it here.
    how = "stamped at creation" if ticket["mode"] is not None else "assigned at claim"
    return position["mode"], f" — inherited from position #{ticket['closes_ticket_id']} ({how})"


def _require_displayed_mode(ticket: sqlite3.Row, mode: str, expected_mode: str | None) -> None:
    """PK-11 (P1-7): a NEW entry executes only in the mode the approving user
    was shown. `mode` is the global mode _claim_mode read — the very value the
    claim stamps and execute_ticket runs on — so a switch after this check
    cannot change what executes. A stale tab (still showing DRY RUN after
    another session switched to LIVE), or a caller that does not say which
    mode it showed, is refused (409) before the claim: nothing is written,
    nothing is sent, and the ticket stays pending_confirmation. Exits never
    come here — they run in their position's own mode (PK-3)."""
    if expected_mode is None:
        raise HTTPException(409, {
            "error_code": "EXECUTION_MODE_REQUIRED",
            "message": f"Ticket {ticket['id']}: the approval did not say which execution mode was shown "
                       f"(the server is in {mode}). Refresh and review the ticket again before approving. "
                       f"Nothing was executed.",
            "current_mode": mode,
        })
    if expected_mode != mode:
        raise HTTPException(409, {
            "error_code": "EXECUTION_MODE_CHANGED",
            "message": f"Execution mode changed (you saw {expected_mode}, the server is now {mode}). Review the "
                       f"ticket again before approving. Nothing was executed.",
            "expected_mode": expected_mode,
            "current_mode": mode,
        })


def _refuse_stale_exit(conn: sqlite3.Connection, ticket: sqlite3.Row, account_id: int) -> None:
    """Refuses (409) an exit that can no longer validly close its position —
    closed already, owned by another exit, not an open executed BUY, or a
    different quantity (risk_engine.linked_exit_problem). Nothing is claimed
    and the broker is never called. A permanent problem also rejects the
    ticket so it stops being offered for approval; while another exit's
    outcome is merely unresolved, the ticket is left as it is."""
    problem = risk_engine.linked_exit_problem(conn, ticket)
    if problem is None:
        return
    if problem.permanent:
        if _transition(conn, ticket["id"], account_id, _CONFIRMABLE_STATUSES, "rejected"):
            _log_event(conn, ticket["id"], "rejected",
                       f"Stale exit refused at confirmation ({problem.code}): {problem.message} No order was sent.")
            conn.commit()
            _notify("rejected", ticket_id=ticket["id"])
        else:
            conn.rollback()
            _raise_invalid_state(conn, ticket["id"], account_id, "confirm")
    raise HTTPException(409, {
        "error_code": problem.code,
        "message": f"{problem.message} Nothing was executed"
                   + (" — this exit ticket has been rejected." if problem.permanent else "."),
    })


def _exit_claim_condition() -> tuple[str, tuple]:
    """The claim predicate of a linked exit (PK-13), evaluated inside the
    claiming UPDATE on trade_tickets: its position is still open and holds
    exactly this exit's quantity."""
    position = "(SELECT {} FROM trade_tickets p WHERE p.id = trade_tickets.closes_ticket_id)"
    return (f"{position.format('p.closed_at')} IS NULL AND "
            f"trade_tickets.quantity = {position.format(position_quantity.open_quantity_sql('p'))}", ())


def _refuse_exit_claim(conn: sqlite3.Connection, account: sqlite3.Row, ticket: sqlite3.Row) -> None:
    """The exit lost its claim to the quantity/closed predicate: the position
    changed between the check and the claim. The fresh check says why."""
    _refuse_stale_exit(conn, ticket, account["id"])
    raise HTTPException(409, {
        "error_code": "EXIT_QUANTITY_MISMATCH",
        "message": f"Position {ticket['closes_ticket_id']} changed while this exit was being approved. Nothing "
                   f"was executed.",
    })


def _update_if_still_claimed(conn: sqlite3.Connection, ticket: sqlite3.Row, **columns) -> bool:
    """Compare-and-set for confirmation-time revalidation: applies only while
    the ticket is still exactly as this request claimed it — 'confirmed' and
    'submitting' (live, and since PK-20 dry run: a paper order is tracked
    the same way). If order_monitor has since marked the claim
    unknown/needs_review, that state is never overwritten. Kept separate
    from execution.update_ticket_if, which is the post-broker-call outcome
    write."""
    assignments = ", ".join(f"{name} = ?" for name in columns)
    return conn.execute(
        f"UPDATE trade_tickets SET {assignments} WHERE id = ? AND status = 'confirmed' AND submission_state = ?",
        (*columns.values(), ticket["id"], execution.SUBMITTING),
    ).rowcount == 1


# Nothing was sent: the claim's submission bookkeeping is withdrawn with it.
_NOT_SUBMITTED = dict(order_tag=None, submission_state=None, submitted_at=None)


def _revalidate_at_confirm(conn: sqlite3.Connection, account: sqlite3.Row, kite: KiteConnect,
                           ticket: sqlite3.Row) -> risk_engine.PortfolioContext | None:
    """Confirmation is a fresh execution gate (PK-4): the risk checks saved
    when the ticket was created may no longer hold — another approval may
    have used the room this ticket counted on. Re-runs every check against
    the broker's portfolio as it is now. Returns only if the ticket may
    execute; otherwise the ticket leaves 'confirmed' for a terminal state and
    an HTTPException is raised. The broker is never called on a refusal.

    Returns the portfolio context the checks used (None for a closing sell),
    so the account safety gate reuses it instead of fetching it again."""
    try:
        ctx = None if execution.is_closing_sell(ticket) else risk_engine.build_portfolio_context(kite)
        report = risk_engine.run_checks(conn, account, ticket, kite, ctx=ctx)
    except Exception as e:
        # The checks were never evaluated, so this is not a risk rejection —
        # but without them nothing may be placed. Fail closed. 'rejected'
        # rather than 'cancelled' so Revert can re-run the checks once the
        # broker is reachable again; the ticket is never reopened by itself.
        reason = e.message if isinstance(e, risk_engine.RiskCheckUnavailableError) else f"{type(e).__name__}: {e}"
        message = f"Risk could not be revalidated ({reason}); no order was sent."
        if ticket["mode"] == "live" and _is_broker_failure(e):
            account_safety.record_broker_failure(account["id"], "risk_revalidation", reason)
        _end_claim(conn, ticket, account["id"], "rejected", "rejected", message)
        raise HTTPException(503, {"error_code": "RISK_REVALIDATION_UNAVAILABLE", "message": message})

    results = json.dumps(report.as_list())
    if report.passed:
        if not _update_if_still_claimed(conn, ticket, risk_check_results=results):
            conn.rollback()
            _raise_lost_claim(conn, ticket, account["id"])
        _log_event(conn, ticket["id"], "modified",
                   f"Risk re-validated at confirmation against the current portfolio: all {len(report.checks)} "
                   f"check(s) passed.")
        conn.commit()
        return ctx

    failed = [c for c in report.checks if not c.passed]
    message = ("Passed creation-time risk checks but failed confirmation-time revalidation: "
               + "; ".join(c.message for c in failed) + ". No order was sent.")
    _end_claim(conn, ticket, account["id"], "risk_rejected", "risk_rejected", message, risk_check_results=results)
    raise HTTPException(409, {
        "error_code": "RISK_REVALIDATION_FAILED", "message": message,
        "failed_checks": [c.rule for c in failed],
        "failed_codes": [c.code for c in failed if c.code],
    })


def _end_claim(conn: sqlite3.Connection, ticket: sqlite3.Row, account_id: int, status: str, event_type: str,
               detail: str, **columns) -> None:
    if not _update_if_still_claimed(conn, ticket, status=status, **_NOT_SUBMITTED, **columns):
        conn.rollback()
        _raise_lost_claim(conn, ticket, account_id)
    _log_event(conn, ticket["id"], event_type, detail)
    conn.commit()
    _notify(status, ticket_id=ticket["id"])


def _raise_lost_claim(conn: sqlite3.Connection, ticket: sqlite3.Row, account_id: int):
    current = _get_ticket_or_404(conn, ticket["id"], account_id)
    raise HTTPException(409, {
        "error_code": "CONFLICT_INVALID_STATE",
        "message": f"Ticket {ticket['id']} changed while its risk was being re-validated (now '{current['status']}', "
                   f"submission {current['submission_state']}). No order was sent by this request.",
    })


def _is_broker_failure(e: BaseException) -> bool:
    """PK-12G: a broker/transport failure (network, timeout, token/session,
    5xx, unparseable reply) — not a definite validation/RMS rejection."""
    if isinstance(e, risk_engine.RiskCheckUnavailableError):
        return True
    if isinstance(e, execution.DEFINITE_REJECTIONS):
        return False
    return isinstance(e, (KiteException, requests.RequestException))


def _raise_account_safety(failures: list) -> None:
    status = 503 if any(f.http_status == 503 for f in failures) else 409
    raise HTTPException(status, {
        "error_code": failures[0].code,
        "message": " ".join(f.message for f in failures),
        "failures": [{"code": f.code, "message": f.message} for f in failures],
    })


def _account_safety_precheck(conn: sqlite3.Connection, account: sqlite3.Row, ticket: sqlite3.Row, mode: str,
                             allow_amo: bool, expected_review_token: str | None = None) -> str | None:
    """PK-12, before the claim: DB/request-only account checks for a NEW
    entry. A refusal leaves the ticket approvable ('pending_confirmation') —
    nothing is claimed, nothing sent — except a permanent failure (a stale or
    contradictory strategy signal, P1-D tampered ticket evidence), which can
    never become valid again and is rejected. Returns the order variety a
    live entry will be sent as."""
    decision = account_safety.precheck(conn, account["id"], ticket, mode, allow_amo,
                                       expected_review_token=expected_review_token)
    if decision.passed:
        return decision.variety
    account_safety.log_refusal(conn, account["id"], ticket, mode, "before claim", decision.failures)
    permanent = [f for f in decision.failures if f.permanent]
    rejected = False
    if permanent:
        rejected = _transition(conn, ticket["id"], account["id"], _CONFIRMABLE_STATUSES, "rejected")
        if rejected:
            _log_event(conn, ticket["id"], "rejected",
                       f"Refused by account safety at confirmation ({permanent[0].code}): {permanent[0].message}")
    conn.commit()
    if rejected:
        _notify("rejected", ticket_id=ticket["id"])
    _raise_account_safety(decision.failures)


def _account_safety_final_gate(conn: sqlite3.Connection, account: sqlite3.Row, kite: KiteConnect,
                               ticket: sqlite3.Row, ctx: risk_engine.PortfolioContext | None,
                               variety: str | None = None) -> None:
    """PK-12, after the claim and risk revalidation, immediately before the
    broker call. A refusal withdraws the claim (nothing was sent): the ticket
    returns to 'pending_confirmation' so it can be approved once the
    condition clears — or is rejected if the market has already crossed its
    stop."""
    decision = account_safety.final_gate(conn, account["id"], ticket, kite,
                                         ctx.portfolio_value if ctx is not None else None, variety=variety)
    if decision.passed:
        return
    reasons = "; ".join(f"{f.code}: {f.message}" for f in decision.failures)
    if any(f.permanent for f in decision.failures):
        status, event_type = "rejected", "rejected"
        detail = f"Refused by account safety before the broker call — {reasons} No order was sent."
    else:
        status, event_type = "pending_confirmation", "modified"
        detail = (f"Refused by account safety before the broker call — {reasons} No order was sent; the ticket "
                  f"is awaiting approval again.")
    account_safety.log_refusal(conn, account["id"], ticket, ticket["mode"], "before broker call", decision.failures)
    _end_claim(conn, ticket, account["id"], status, event_type, detail)
    _raise_account_safety(decision.failures)


def _refuse_disabled_live_entry(conn: sqlite3.Connection, account: sqlite3.Row, ticket: sqlite3.Row) -> None:
    """Backstop of live_entry_gate.refusal immediately before the broker call. The claim is
    withdrawn (nothing was sent) and the ticket awaits approval again."""
    failure = live_entry_gate.refusal(ticket, ticket["mode"])
    if failure is None:
        return
    account_safety.log_refusal(conn, account["id"], ticket, ticket["mode"], "before broker call", [failure])
    _end_claim(conn, ticket, account["id"], "pending_confirmation", "modified",
               f"Refused before the broker call — {failure.code}: {failure.message}")
    _raise_account_safety([failure])


def _refuse_daily_entry_limit(conn: sqlite3.Connection, account: sqlite3.Row, ticket: sqlite3.Row) -> None:
    """The live-entry claim lost only to its PK-12C predicate: today's entry
    slots are taken (possibly by an approval racing this one)."""
    settings = risk_engine.get_settings(conn, account["id"])
    used = account_safety.entries_today(conn, account["id"], account_safety.now_ist(), ticket["id"])
    failure = account_safety.daily_entry_limit_failure(settings, used)
    account_safety.log_refusal(conn, account["id"], ticket, "live", "claim", [failure])
    conn.commit()
    _raise_account_safety([failure])


def _account_safety_bookkeeping(account: sqlite3.Row, ticket: sqlite3.Row, result: execution.ExecutionResult,
                                variety: str | None) -> None:
    """PK-12G/H, after the broker call. Never changes the recorded outcome."""
    if ticket["mode"] != "live":
        return
    if result.event_type == "submission_unknown":
        account_safety.record_broker_failure(account["id"], "order_submission", result.message)
    elif result.kite_order_id is not None:
        account_safety.record_broker_success(account["id"])
        if variety == account_safety.VARIETY_AMO:
            try:
                conn = get_connection()
                try:
                    system_events.log_event(
                        conn, account["id"], "amo_entry_approved",
                        f"Ticket {ticket['id']} {ticket['transaction_type']} {ticket['quantity']} {ticket['symbol']} "
                        f"sent as an After-Market Order with the approver's explicit consent "
                        f"(order {result.kite_order_id}).")
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                logger.exception("Could not log the AMO approval of ticket %d", ticket["id"])


def _refuse_unsupported_at_confirm(conn: sqlite3.Connection, ticket: sqlite3.Row, account_id: int) -> None:
    """PK-17: a ticket awaiting approval that would open an unsupported
    position (created before PK-17, or by a manual DB edit) is refused before
    the claim, in either mode: rejected — it can never become valid — with
    the reason in its events, and never sent. The row itself is kept."""
    message = execution.unsupported_new_position(ticket)
    if message is None:
        return
    if _transition(conn, ticket["id"], account_id, _CONFIRMABLE_STATUSES, "rejected"):
        _log_event(conn, ticket["id"], "rejected",
                   f"Refused at confirmation ({execution.UNSUPPORTED_MANUAL_TRADE}): {message} No order was sent.")
        conn.commit()
        _notify("rejected", ticket_id=ticket["id"])
    else:
        conn.rollback()
        _raise_invalid_state(conn, ticket["id"], account_id, "confirm")
    raise HTTPException(409, {
        "error_code": execution.UNSUPPORTED_MANUAL_TRADE,
        "message": f"{message} Nothing was executed — this ticket has been rejected.",
        "transaction_type": ticket["transaction_type"],
        "product_type": ticket["product_type"],
    })


def _refuse_tampered_ticket(conn: sqlite3.Connection, ticket: sqlite3.Row, account_id: int) -> None:
    """Post-claim tampering was detected for this ticket earlier (backend/ticket_integrity.py):
    it can never be confirmed again - no new claim, no fresh review, nothing sent."""
    if not ticket_integrity.tampered(conn, ticket["id"]):
        return
    message = (f"Ticket {ticket['id']}'s order details were changed after it was claimed for execution (see its "
               f"integrity audit). It can never be approved; create a new ticket. Nothing was executed.")
    if ticket["status"] in _CONFIRMABLE_STATUSES and _transition(conn, ticket["id"], account_id,
                                                                 _CONFIRMABLE_STATUSES, "rejected"):
        _log_event(conn, ticket["id"], "rejected", f"Refused at confirmation ({ticket_integrity.TAMPER_CODE}): "
                                                   f"{message}")
        conn.commit()
        _notify("rejected", ticket_id=ticket["id"])
    raise HTTPException(409, {"error_code": ticket_integrity.TAMPER_CODE, "message": message})


def _verify_claimed_content(conn: sqlite3.Connection, account: sqlite3.Row, claimed: sqlite3.Row, stage: str) -> None:
    """Post-claim tampering audit, before anything is sent: the persisted row must still be the
    content this claim snapshotted. A change is recorded (append-only, committed first so it
    survives), the claim is withdrawn and the ticket rejected for good; the broker is never called."""
    current = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (claimed["id"],)).fetchone()
    changed = ticket_integrity.check(conn, account["id"], current, stage)
    if not changed:
        return
    conn.commit()
    detail = (f"Refused ({ticket_integrity.TAMPER_CODE}): the ticket's {', '.join(changed)} changed after it was "
              f"claimed for execution (detected {stage.replace('_', ' ')}). Nothing was sent to the broker; the "
              f"ticket is rejected and can never be approved — create a new ticket.")
    _end_claim(conn, claimed, account["id"], "rejected", "rejected", detail)
    raise HTTPException(409, {"error_code": ticket_integrity.TAMPER_CODE, "message": detail, "categories": changed})


def confirm_and_execute_ticket(conn: sqlite3.Connection, account: sqlite3.Row, kite: KiteConnect, ticket_id: int,
                               expected_mode: str | None = None, allow_amo: bool = False,
                               expected_review_token: str | None = None) -> dict:
    """Shared confirm-and-execute body: transitions a pending_confirmation
    ticket to 'confirmed', calls the execution engine, updates status from the
    result, and records a closed_trades row for exit tickets. Used by both
    the normal one-ticket confirm endpoint and Phase 8's Emergency Exit All
    batch flow, so there is exactly one code path for "how a ticket
    actually executes" regardless of how it got confirmed. expected_mode is
    the mode the user was shown; it binds a new entry (PK-11) and is ignored
    for an exit. allow_amo is the approver's explicit consent to send a live
    new entry as an AMO while the market is closed (PK-12H).
    expected_review_token is the review the approver's view showed (P1-D):
    required for a manual / Quick-Action BUY entry, ignored otherwise.

    A NEW entry (not a closing sell) also passes the PK-12 account safety
    gate: a pre-claim check, the daily entry limit inside the claim, and a
    final check right before the broker call. Exits never do."""
    ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
    if ticket["status"] not in _CONFIRMABLE_STATUSES:
        _raise_invalid_state(conn, ticket_id, account["id"], "confirm")
    # PK-17: only a supported NEW position may go further; exits pass.
    _refuse_unsupported_at_confirm(conn, ticket, account["id"])
    # Post-claim tampering, once detected, refuses the ticket for good.
    _refuse_tampered_ticket(conn, ticket, account["id"])

    mode, mode_note = _claim_mode(conn, ticket, account["id"])
    new_entry = not execution.is_closing_sell(ticket)
    if ticket["closes_ticket_id"] is None:
        # PK-11: refused before the claim if the user saw a different mode.
        _require_displayed_mode(ticket, mode, expected_mode)
    else:
        # PK-4: the position may have been closed, or another exit may own
        # it, since this exit was created. Refused before the claim.
        _refuse_stale_exit(conn, ticket, account["id"])
    variety = None
    entry_limit = None
    if ticket["closes_ticket_id"] is not None:
        # PK-13: the check above and this claim are one decision — the
        # position must still be open and hold exactly this exit's quantity
        # at the moment of the claim (a fill recorded in between, or another
        # exit's partial sale, changes what may be sold).
        entry_limit = _exit_claim_condition()
    if new_entry:
        # PK-12: refused before the claim — nothing written, nothing sent.
        variety = _account_safety_precheck(conn, account, ticket, mode, allow_amo, expected_review_token)
        if mode == "live":
            entry_limit = account_safety.daily_entry_claim_condition(
                risk_engine.get_settings(conn, account["id"]), account["id"], ticket_id, account_safety.now_ist())
    # The claim also records the order tag and 'submitting' in the same
    # write, before any broker call: if this process dies mid-submission, the
    # database alone tells order_monitor what to look for (PK-2). PK-20: a
    # dry-run claim too — its paper order is followed exactly like a live one.
    submission = dict(
        order_tag=execution.order_tag_for(ticket_id, ticket["created_at"]),
        submission_state=execution.SUBMITTING,
        submitted_at=execution.utc_now_sql(),
    )
    # The conditional UPDATE, not the check above, is the execution lock: two
    # requests can both pass the check, but only one can move the ticket out
    # of pending_confirmation. The loser never reaches the broker. The
    # 'confirmed' event is written in the same transaction, so it exists
    # exactly when the transition happened.
    # A live NEW entry's claim is also its PK-12C daily-entry reservation: the
    # same UPDATE counts today's live entries and takes the slot.
    try:
        claimed = _transition(conn, ticket_id, account["id"], _CONFIRMABLE_STATUSES, "confirmed",
                              extra_condition=entry_limit, mode=mode, **submission)
    except sqlite3.IntegrityError:
        # Storage backstop (db._ensure_single_exit_owner_index): another exit
        # of the same position was claimed between the check above and here.
        conn.rollback()
        raise HTTPException(409, {
            "error_code": "EXIT_ALREADY_IN_PROGRESS",
            "message": f"Another exit ticket already owns position {ticket['closes_ticket_id']}. "
                       f"Nothing was executed.",
        })
    if not claimed:
        conn.rollback()
        if entry_limit is not None and _get_ticket_or_404(conn, ticket_id, account["id"])["status"] in _CONFIRMABLE_STATUSES:
            if ticket["closes_ticket_id"] is not None:
                _refuse_exit_claim(conn, account, ticket)
            _refuse_daily_entry_limit(conn, account, ticket)
        _raise_invalid_state(conn, ticket_id, account["id"], "confirm")
    _log_event(conn, ticket_id, "confirmed", f"Confirmed in {mode} mode{mode_note}")
    # The claim's snapshot of the content this confirmation was decided on (the row the
    # pre-claim checks read), in the claim's own transaction (backend/ticket_integrity.py).
    ticket_integrity.record_claim(conn, account["id"], ticket)
    conn.commit()
    # PK-22E: the claim, traceable by this request's cid, the ticket id and
    # the order tag the broker order will carry.
    log_event(logger, "approval.claimed", account_id=account["id"], ticket_id=ticket_id, symbol=ticket["symbol"],
              mode=mode, order_tag=submission["order_tag"], position_id=ticket["closes_ticket_id"],
              variety=variety)

    ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
    _verify_claimed_content(conn, account, ticket, "post_claim")
    ctx = None
    if ticket["closes_ticket_id"] is None:
        # PK-4: this ticket won the claim; now re-check it against the
        # portfolio as it is NOW. Only if it still passes may it execute.
        ctx = _revalidate_at_confirm(conn, account, kite, ticket)
    if new_entry:
        # PK-12: the account safety gate has the last word before the broker.
        _account_safety_final_gate(conn, account, kite, ticket, ctx, variety)
    # The last look at the database before the broker call: a write that landed during
    # revalidation refuses the execution instead of leaving tampered content behind.
    _verify_claimed_content(conn, account, ticket, "pre_broker")
    # The LIVE-entry hard gate's last copy, adjacent to the only execute_ticket call: whatever path
    # reached this point, a LIVE new entry is never sent while entries are disabled.
    _refuse_disabled_live_entry(conn, account, ticket)
    # PK-20: the broker of the ticket's own mode — Kite for live, the paper
    # broker (on this request's connection, so the paper order is written in
    # the same transaction as the ticket's outcome) for dry run.
    broker = paper_broker.broker_for(ticket["mode"], kite, conn, account["id"])
    try:
        result = execution.execute_ticket(broker, ticket, entry_variety=variety)
    except Exception as e:
        # We cannot tell how far execution got, so this is an UNKNOWN
        # outcome — never a 500 that leaves the ticket for nobody to follow up.
        logger.exception("Execution of ticket %d raised", ticket_id)
        result = execution.unknown_result(ticket, e)

    won = _record_execution_result(conn, ticket, result)
    if won:
        _log_event(conn, ticket_id, result.event_type, result.message)
        conn.commit()
    else:
        # order_monitor resolved this submission first (adopted / not_found /
        # needs_review). Its state is newer, so it is never overwritten.
        conn.rollback()
        _reconcile_late_result(conn, ticket, result)
        log_event(logger, "approval.result_superseded", "warning", account_id=account["id"], ticket_id=ticket_id,
                  symbol=ticket["symbol"], mode=ticket["mode"], order_id=result.kite_order_id)
    _account_safety_bookkeeping(account, ticket, result, variety)
    # After the broker call: the broker was sent the verified in-memory row; a change to the
    # persisted row meanwhile is recorded (and reported), never silently left behind.
    persisted = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (ticket_id,)).fetchone()
    tampered_after_send = ticket_integrity.check(conn, account["id"], persisted, "post_execution")
    conn.commit()

    # PK-8: an exit closes its position only once its fill is proven. An
    # exit the broker accepted is EXITING — the position stays open,
    # protected and unjournaled, and order_monitor.py closes it when the
    # broker reports the full quantity COMPLETE. PK-20: a dry-run exit too —
    # its paper order fills only when the paper exchange observes a price.
    ticket = _get_ticket_or_404(conn, ticket_id, account["id"])

    _notify("executed", ticket_id=ticket_id, status=ticket["status"], success=result.success)
    data = _with_lifecycle(conn, ticket)
    if tampered_after_send:
        data["integrity_warning"] = {
            "error_code": ticket_integrity.TAMPER_CODE, "categories": tampered_after_send,
            "message": "The ticket's stored details changed after the broker call; the broker was sent the "
                       "verified values. The change is recorded in the ticket's integrity audit."}
    if won:
        data["execution_message"] = result.message
        data["success"] = result.success
    else:
        data["execution_message"] = (
            f"The order monitor resolved this submission first: the ticket is now '{ticket['status']}' "
            f"(submission {ticket['submission_state']}, order {ticket['kite_order_id']}). Nothing was "
            f"resubmitted. Broker call result: {result.message}"
        )
        data["success"] = ticket["status"] == "executed"
    return data


def _record_execution_result(conn: sqlite3.Connection, ticket: sqlite3.Row, result: execution.ExecutionResult) -> bool:
    """Writes the broker result only if the ticket is still the one this
    request claimed. A claim is 'submitting' (live, and dry run since PK-20);
    order_monitor may since have marked a stale claim 'unknown' — definite
    broker evidence from this request still upgrades that. Returns False
    once the monitor has resolved the ticket itself (adopted / not_found /
    needs_review)."""
    claimed = (execution.SUBMITTING, execution.UNKNOWN)

    if result.event_type in ("executed", "protection_failed"):
        # The order is 'pending' until order_monitor.py sees the broker's —
        # or the paper broker's — fill. Nothing is ever filled here: no fill
        # price, time or quantity is known yet (PK-13, PK-20).
        return execution.update_ticket_if(
            conn, ticket["id"], status_in=("confirmed",), submission_state_in=claimed,
            status=result.status, kite_order_id=result.kite_order_id, kite_gtt_id=result.kite_gtt_id,
            current_stop_loss=ticket["stop_loss"], executed_at=execution.utc_now_sql(),
            order_variety=result.order_variety, entry_fill_status=result.entry_fill_status,
            filled_price=None, filled_at=None, filled_quantity=0,
            submission_state=execution.ACKNOWLEDGED,
        )
    if result.event_type == "submission_unknown":
        # Status stays 'confirmed': still owned by execution, never
        # confirmable again, and now followed up by order_monitor.
        return execution.update_ticket_if(
            conn, ticket["id"], status_in=("confirmed",), submission_state_in=(execution.SUBMITTING,),
            submission_state=execution.UNKNOWN, order_variety=result.order_variety,
        )
    # execution_failed: the broker definitively rejected the order.
    return execution.update_ticket_if(
        conn, ticket["id"], status_in=("confirmed",), submission_state_in=claimed,
        status=result.status, order_variety=result.order_variety,
        submission_state=execution.REJECTED,
    )


def _reconcile_late_result(conn: sqlite3.Connection, claimed: sqlite3.Row, result: execution.ExecutionResult) -> None:
    """This request's broker result arrived after order_monitor had already
    resolved the ticket, so the monitor's state stands. The one thing the
    request may still add is the protective GTT it placed for the very order
    the monitor adopted (the monitor never creates GTTs) — conditionally, so
    nothing is overwritten. Anything else is kept as evidence."""
    current = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (claimed["id"],)).fetchone()
    attached = False
    if result.kite_order_id is not None and result.kite_gtt_id is not None:
        attached = conn.execute(
            "UPDATE trade_tickets SET kite_gtt_id = ? WHERE id = ? AND kite_order_id = ? AND kite_gtt_id IS NULL",
            (result.kite_gtt_id, claimed["id"], result.kite_order_id),
        ).rowcount == 1
    if attached:
        _log_event(conn, claimed["id"], "modified",
                   f"Protective GTT {result.kite_gtt_id}, placed by the confirming request, attached to "
                   f"recovered order {result.kite_order_id}.")
    else:
        detail = (f"Late broker result for this submission, received after the order monitor had resolved it "
                  f"(status '{current['status']}', submission {current['submission_state']}); not applied: "
                  f"{result.message}")
        _log_event(conn, claimed["id"], "modified", detail)
        if result.kite_order_id is not None and result.kite_order_id != current["kite_order_id"]:
            reconciliation.log_mismatch(
                conn, claimed["id"], "order_status",
                expected=f"one broker order for ticket {claimed['id']} (tracked: {current['kite_order_id']})",
                actual=f"confirming request also received order {result.kite_order_id}", detail=detail,
            )
    conn.commit()


@router.get("/api/tickets/{ticket_id}/margin-preview")
def margin_preview(ticket_id: int, kite: KiteConnect = Depends(require_trading_account)):
    """The margin preflight's figures for a LIVE new entry, shown in the confirm dialog: available
    funds, Kite's own margin for this exact order, charges, the order value and the per-order cap.
    Read-only: one LTP read, one funds read and Kite's order-margin calculation (a POST that places
    nothing). The final gate repeats the check at approval; this never authorises anything."""
    account = get_trading_account()
    conn = get_connection()
    try:
        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        if execution.is_closing_sell(ticket):
            return {"data": {"applicable": False, "reason": "An exit sells shares already held: no margin is needed."}}
        mode = execution.get_execution_mode()
        if mode != "live":
            return {"data": {"applicable": False,
                             "reason": "Dry run: the paper broker uses no funds; no margin check applies."}}
        settings = risk_engine.get_settings(conn, account["id"])
    finally:
        conn.close()
    try:
        ltp = account_safety.fetch_ltp(kite, ticket)
    except Exception:
        ltp = None
    from backend import margin_preflight
    result = margin_preflight.evaluate(kite, ticket, account_safety.VARIETY_REGULAR, ltp)
    return {"data": {"applicable": True, **result, "reference_price": ltp,
                     "max_order_notional": settings["max_order_notional"],
                     "live_entries_enabled": live_entry_gate.entries_enabled()}}


@router.post("/api/tickets/{ticket_id}/confirm")
def confirm_ticket(ticket_id: int, req: TicketConfirmRequest | None = None,
                   kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        data = confirm_and_execute_ticket(conn, account, kite, ticket_id,
                                          expected_mode=req.expected_mode if req is not None else None,
                                          allow_amo=req.allow_amo if req is not None else False,
                                          expected_review_token=req.expected_review_token if req is not None else None)
        return {"data": data}
    finally:
        conn.close()


@router.post("/api/tickets/cancel-all-pending")
def cancel_all_pending(kite: KiteConnect = Depends(require_trading_account)):
    """Kill-switch action (Phase 8, Part A.2): rejects every ticket
    currently in draft or pending_confirmation status in one action."""
    account = get_trading_account()
    conn = get_connection()
    try:
        placeholders = ", ".join("?" * len(_REJECTABLE_STATUSES))
        candidates = conn.execute(
            f"SELECT * FROM trade_tickets WHERE account_id = ? AND status IN ({placeholders}) ORDER BY id",
            (account["id"], *_REJECTABLE_STATUSES),
        ).fetchall()

        # The SELECT is only a candidate list. Each cancellation is its own
        # conditional transition, so a ticket confirmed since the SELECT is
        # left alone and not reported as cancelled.
        cancelled = []
        for ticket in candidates:
            if _transition(conn, ticket["id"], account["id"], _REJECTABLE_STATUSES, "rejected"):
                _log_event(conn, ticket["id"], "rejected", "Cancelled via Cancel All Pending")
                cancelled.append(ticket)
        conn.commit()

        cancelled_ids = [t["id"] for t in cancelled]
        symbols = sorted({t["symbol"] for t in cancelled})
        system_events.log_event(
            conn, account["id"], "cancel_all_pending",
            f"Cancelled {len(cancelled_ids)} ticket(s): {', '.join(symbols) if symbols else '(none)'}",
        )
        conn.commit()

        _notify("cancelled_all")
        return {"data": {"cancelled_ticket_ids": cancelled_ids, "count": len(cancelled_ids), "symbols": symbols}}
    finally:
        conn.close()


@router.post("/api/tickets/{ticket_id}/start_trailing")
def start_trailing(ticket_id: int, kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        # PK-20: a paper position trails its paper GTT exactly like a live
        # one; a legacy dry-run position has no GTT to trail.
        broker_tracked = ticket["mode"] == "live" or paper_broker.is_paper_id(ticket["kite_order_id"])
        if not (
            ticket["trailing_trigger"] == "manual"
            and ticket["status"] == "executed"
            and broker_tracked
            and ticket["closed_at"] is None
        ):
            raise HTTPException(409, {
                "error_code": "CONFLICT_INVALID_STATE",
                "message": "Manual trailing can only be started on an executed, still-open live or paper "
                           "ticket with trailing_trigger='manual'",
            })
        conn.execute("UPDATE trade_tickets SET trailing_active = 1 WHERE id = ?", (ticket_id,))
        _log_event(conn, ticket_id, "trailing_adjusted", "Trailing manually activated by user")
        conn.commit()
        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        return {"data": _serialize_reviewed(conn, ticket)}
    finally:
        conn.close()


_EDITABLE_FIELDS = (
    "symbol", "exchange", "transaction_type", "quantity", "entry_price", "entry_order_type",
    "stop_loss", "target_1", "target_2", "trailing_enabled", "trailing_trigger",
    "trailing_distance", "product_type", "use_gtt", "rationale",
)

# Statuses that are terminal, real-money records — never offered any
# delete/archive option, not even a disabled button on the frontend.
_PERMANENT_STATUSES = ("confirmed", "executed")
_SOFT_DELETE_STATUSES = ("rejected", "risk_rejected", "cancelled", "failed")


def _diff_summary(old: sqlite3.Row, new: TicketEditRequest) -> str:
    changes = []
    new_values = {
        "symbol": new.symbol, "exchange": new.exchange, "transaction_type": new.transaction_type,
        "quantity": new.quantity, "entry_price": new.entry_price, "entry_order_type": new.entry_order_type,
        "stop_loss": new.stop_loss, "target_1": new.target_1, "target_2": new.target_2,
        "trailing_enabled": int(new.trailing_enabled), "trailing_trigger": new.trailing_trigger,
        "trailing_distance": new.trailing_distance, "product_type": new.product_type,
        "use_gtt": int(new.use_gtt), "rationale": new.rationale,
    }
    for field in _EDITABLE_FIELDS:
        old_value = old[field]
        new_value = new_values[field]
        if old_value != new_value:
            changes.append(f"{field}: {old_value} → {new_value}")
    return "; ".join(changes) if changes else "(no fields changed)"


@router.put("/api/tickets/{ticket_id}")
def edit_ticket(ticket_id: int, req: TicketEditRequest, kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        if ticket["status"] != "draft":
            raise HTTPException(409, {
                "error_code": "CONFLICT_INVALID_STATE",
                "message": f"Cannot edit a ticket in status '{ticket['status']}' — only draft tickets can be edited.",
            })
        if execution.is_closing_sell(ticket):
            # An exit's side, quantity and position link (closes_ticket_id)
            # and its inherited mode belong together; editing any of them
            # could turn it into a different trade (PK-3/PK-4).
            raise HTTPException(409, {
                "error_code": "EXIT_TICKET_NOT_EDITABLE",
                "message": "Exit / reduction tickets cannot be edited — reject it and create a new one instead.",
            })

        _refuse_unsupported_entry(req)
        _validate_prices(req)

        instrument = instruments.get_instrument_by_symbol(req.symbol, req.exchange)
        if instrument is None:
            raise HTTPException(400, {
                "error_code": "INSTRUMENT_NOT_FOUND",
                "message": f"Could not resolve '{req.symbol}' on {req.exchange}. "
                           f"Check spelling or refresh the instrument list.",
            })

        diff = _diff_summary(ticket, req)
        metrics = execution.compute_risk_metrics(req.model_dump())

        cursor = conn.execute(
            """
            UPDATE trade_tickets SET
                symbol = ?, exchange = ?, instrument_token = ?, transaction_type = ?,
                quantity = ?, entry_price = ?, entry_order_type = ?,
                stop_loss = ?, target_1 = ?, target_2 = ?,
                trailing_enabled = ?, trailing_trigger = ?, trailing_distance = ?,
                product_type = ?, use_gtt = ?, risk_amount = ?, risk_reward_ratio = ?,
                rationale = ?, status = 'draft', risk_check_results = NULL
            WHERE id = ? AND status = 'draft'
            """,
            (
                req.symbol, req.exchange, instrument["instrument_token"], req.transaction_type,
                req.quantity, req.entry_price, req.entry_order_type,
                req.stop_loss, req.target_1, req.target_2,
                int(req.trailing_enabled), req.trailing_trigger, req.trailing_distance,
                req.product_type, int(req.use_gtt), metrics["risk_amount"], metrics["risk_reward_ratio"],
                req.rationale, ticket_id,
            ),
        )
        if cursor.rowcount != 1:
            # Cancelled/rejected since the read above: never turn it back
            # into a draft (PK-4).
            conn.rollback()
            _raise_invalid_state(conn, ticket_id, account["id"], "edit")
        # P1-D: an edit is a new review of new content — re-stamped from the
        # row as now stored, in the same transaction.
        _stamp_review(conn, ticket_id, account["id"], with_digest=True)
        _log_event(conn, ticket_id, "modified", diff)
        conn.commit()

        _run_risk_checks_and_transition(conn, account, ticket_id, kite)
        _notify("modified", ticket_id=ticket_id)

        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        return {"data": _serialize_reviewed(conn, ticket)}
    finally:
        conn.close()


@router.delete("/api/tickets/{ticket_id}")
def delete_ticket(ticket_id: int, kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        status = ticket["status"]

        # Both writes below are conditional on the status just read (PK-4): a
        # ticket that has since moved on — e.g. a draft whose risk check just
        # made it approvable, or a rejected ticket reverted meanwhile — is
        # never deleted or hidden.
        if status == "draft":
            conn.execute("DELETE FROM ticket_events WHERE ticket_id = ?", (ticket_id,))
            if conn.execute("DELETE FROM trade_tickets WHERE id = ? AND status = 'draft'", (ticket_id,)).rowcount != 1:
                conn.rollback()  # restores the events too
                _raise_invalid_state(conn, ticket_id, account["id"], "delete")
            conn.commit()
            _notify("deleted", ticket_id=ticket_id)
            return {"data": {"deleted": True, "archived": False}}

        if status in _SOFT_DELETE_STATUSES:
            placeholders = ", ".join("?" * len(_SOFT_DELETE_STATUSES))
            if conn.execute(
                f"UPDATE trade_tickets SET archived = 1 WHERE id = ? AND status IN ({placeholders})",
                (ticket_id, *_SOFT_DELETE_STATUSES),
            ).rowcount != 1:
                conn.rollback()
                _raise_invalid_state(conn, ticket_id, account["id"], "archive")
            _log_event(conn, ticket_id, "archived", f"Archived by user (was '{status}')")
            conn.commit()
            _notify("archived", ticket_id=ticket_id)
            return {"data": {"deleted": False, "archived": True}}

        if status in _PERMANENT_STATUSES:
            raise HTTPException(409, {
                "error_code": "CONFLICT_INVALID_STATE",
                "message": f"Tickets in status '{status}' are permanent trading records and cannot be deleted or archived.",
            })

        # pending_confirmation: reject it first, then it becomes archivable.
        raise HTTPException(409, {
            "error_code": "CONFLICT_INVALID_STATE",
            "message": "Reject this ticket first, then it can be archived.",
        })
    finally:
        conn.close()
