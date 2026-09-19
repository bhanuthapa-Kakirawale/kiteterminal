import json
import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from kiteconnect import KiteConnect

from backend import execution, instruments, journal, kite_chart, risk_engine, signal_chart, system_events
from backend.accounts import get_trading_account
from backend.db import get_connection
from backend.kite_client import require_trading_account
from backend.models import (
    AlertActionSellRequest,
    TicketCreateRequest,
    TicketEditRequest,
    TicketRejectRequest,
)
from backend.ticker import ticker_manager

logger = logging.getLogger(__name__)
router = APIRouter()

_OPEN_STATUSES = ("draft", "pending_confirmation")


def _log_event(conn: sqlite3.Connection, ticket_id: int, event_type: str, detail: str | None) -> None:
    conn.execute(
        "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, ?, ?)",
        (ticket_id, event_type, detail),
    )


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
        _log_event(conn, ticket_id, "created", f"Ticket created (source={source})")
        conn.commit()

        _run_risk_checks_and_transition(conn, account, ticket_id, kite)
        _notify("created", ticket_id=ticket_id)

        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        return _serialize(ticket)
    finally:
        conn.close()


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
        # Only link to a specific tracked ticket on a genuine FULL close —
        # this app's exit model has no partial-exit/quantity-split modeling
        # (execution.py's own comment), so linking a PARTIAL sell to a
        # tracked ticket would make journal.record_closed_trade compute P&L
        # against the wrong (larger) quantity and incorrectly mark the whole
        # original position closed. A partial reduction (the common case for
        # the concentration action) always falls through to case (b): no
        # closes_ticket_id, validated instead against held quantity by
        # risk_engine._check_exit_or_reduction.
        open_positions = [p for p in risk_engine.list_open_positions(conn, account["id"]) if p["symbol"] == req.symbol]
        closes_ticket_id = None
        link_note = "none (partial reduction)"
        if len(open_positions) == 1 and open_positions[0]["quantity"] == req.quantity:
            closes_ticket_id = open_positions[0]["id"]
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
                rationale, status, closes_ticket_id, rule_checklist
            ) VALUES (?, 'alert_action', ?, ?, ?, 'SELL', ?, ?, ?, ?, ?, ?, 1, ?, 'draft', ?, ?)
            """,
            (
                account["id"], req.symbol, req.exchange, instrument["instrument_token"],
                req.quantity, req.entry_price, req.entry_order_type,
                average_price, average_price, product_type,
                req.rationale, closes_ticket_id, rule_checklist,
            ),
        )
        ticket_id = cursor.lastrowid
        _log_event(conn, ticket_id, "created", "Ticket created (source=alert_action): "
                                                f"{req.alert_type} on {req.symbol}")
        conn.commit()

        _run_risk_checks_and_transition(conn, account, ticket_id, kite)
        _notify("created", ticket_id=ticket_id)

        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        return {"data": _serialize(ticket)}
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
        return {"data": [_serialize(r) for r in rows]}
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
        data = _serialize(ticket)
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
        ticket = _serialize(_get_ticket_or_404(conn, ticket_id, account["id"]))

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
        if ticket["status"] not in _OPEN_STATUSES:
            raise HTTPException(409, {
                "error_code": "CONFLICT_INVALID_STATE",
                "message": f"Cannot reject a ticket in status '{ticket['status']}'",
            })
        conn.execute("UPDATE trade_tickets SET status = 'rejected' WHERE id = ?", (ticket_id,))
        _log_event(conn, ticket_id, "rejected", req.reason)
        conn.commit()
        _notify("rejected", ticket_id=ticket_id)
        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        return {"data": _serialize(ticket)}
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
        conn.execute(
            "UPDATE trade_tickets SET status = 'draft', archived = 0, risk_check_results = NULL WHERE id = ?",
            (ticket_id,),
        )
        _log_event(conn, ticket_id, "modified", "Rejection reverted by user; risk checks re-run")
        conn.commit()

        _run_risk_checks_and_transition(conn, account, ticket_id, kite)
        _notify("reverted", ticket_id=ticket_id)

        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        return {"data": _serialize(ticket)}
    finally:
        conn.close()


def confirm_and_execute_ticket(conn: sqlite3.Connection, account: sqlite3.Row, kite: KiteConnect, ticket_id: int) -> dict:
    """Shared confirm-and-execute body: transitions an open ticket to
    'confirmed', calls the execution engine, updates status from the
    result, and records a closed_trades row for exit tickets. Used by both
    the normal one-ticket confirm endpoint and Phase 8's Emergency Exit All
    batch flow, so there is exactly one code path for "how a ticket
    actually executes" regardless of how it got confirmed."""
    ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
    if ticket["status"] not in _OPEN_STATUSES:
        raise HTTPException(409, {
            "error_code": "CONFLICT_INVALID_STATE",
            "message": f"Cannot confirm a ticket in status '{ticket['status']}'",
        })

    mode = execution.get_execution_mode()
    conn.execute("UPDATE trade_tickets SET status = 'confirmed', mode = ? WHERE id = ?", (mode, ticket_id))
    _log_event(conn, ticket_id, "confirmed", f"Confirmed in {mode} mode")
    conn.commit()

    ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
    result = execution.execute_ticket(kite, ticket)

    if result.event_type in ("executed", "protection_failed"):
        # A live order is 'pending' until order_monitor.py sees the broker's
        # fill; a dry-run simulation is filled at its reference price.
        filled_now = result.entry_fill_status == "filled"
        conn.execute(
            """
            UPDATE trade_tickets
            SET status = ?, kite_order_id = ?, kite_gtt_id = ?,
                current_stop_loss = stop_loss, executed_at = datetime('now'),
                order_variety = ?, entry_fill_status = ?,
                filled_price = CASE WHEN ? THEN entry_price ELSE NULL END,
                filled_at = CASE WHEN ? THEN datetime('now') ELSE NULL END
            WHERE id = ?
            """,
            (result.status, result.kite_order_id, result.kite_gtt_id, result.order_variety,
             result.entry_fill_status, filled_now, filled_now, ticket_id),
        )
    else:  # execution_failed
        conn.execute(
            "UPDATE trade_tickets SET status = ?, order_variety = ? WHERE id = ?",
            (result.status, result.order_variety, ticket_id),
        )

    _log_event(conn, ticket_id, result.event_type, result.message)
    conn.commit()

    ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
    if ticket["transaction_type"] == "SELL" and ticket["closes_ticket_id"] is not None and ticket["status"] == "executed":
        # execution.py's _execute_exit already cancels the ORIGINAL
        # position's GTT synchronously — but its own docstring (Phase 6)
        # deliberately left closed_at to be set later, "by the trailing
        # engine on its next tick" (up to TRAILING_CHECK_INTERVAL_SECONDS
        # later), since that was written before any code path called
        # execute_ticket outside the scheduled trailing loop. Phase 8 added
        # exactly that: a human confirming an exit ticket (or Emergency
        # Exit All) expects the position to show as closed IMMEDIATELY,
        # not after an indeterminate wait for an unrelated background
        # loop's next cycle. Stamping closed_at here, in the same
        # transaction as the exit's own execution, closes that gap —
        # harmless no-op if some other path already set it first.
        conn.execute(
            "UPDATE trade_tickets SET closed_at = datetime('now') WHERE id = ? AND closed_at IS NULL",
            (ticket["closes_ticket_id"],),
        )
        conn.commit()
        # Phase 6: explicit exit tickets (Phase 5's exit scanner, Phase 8's
        # Emergency Exit All, or any future manual close-position flow) get
        # their closed_trades row recorded here — the same computation
        # trailing_engine.py uses for a GTT auto-triggered closure, so P&L
        # is never computed two different ways depending on how the
        # position happened to close.
        try:
            journal.record_closed_trade(conn, ticket_id)
        except ValueError:
            logger.exception("Failed to record closed_trades for ticket %d", ticket_id)

    _notify("executed", ticket_id=ticket_id, status=ticket["status"], success=result.success)
    data = _serialize(ticket)
    data["execution_message"] = result.message
    data["success"] = result.success
    return data


@router.post("/api/tickets/{ticket_id}/confirm")
def confirm_ticket(ticket_id: int, kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        data = confirm_and_execute_ticket(conn, account, kite, ticket_id)
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
        rows = conn.execute(
            "SELECT * FROM trade_tickets WHERE account_id = ? AND status IN (?, ?)",
            (account["id"], *_OPEN_STATUSES),
        ).fetchall()

        cancelled_ids = []
        for ticket in rows:
            conn.execute("UPDATE trade_tickets SET status = 'rejected' WHERE id = ?", (ticket["id"],))
            _log_event(conn, ticket["id"], "rejected", "Cancelled via Cancel All Pending")
            cancelled_ids.append(ticket["id"])
        conn.commit()

        symbols = sorted({t["symbol"] for t in rows})
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
        if not (
            ticket["trailing_trigger"] == "manual"
            and ticket["status"] == "executed"
            and ticket["mode"] == "live"
            and ticket["closed_at"] is None
        ):
            raise HTTPException(409, {
                "error_code": "CONFLICT_INVALID_STATE",
                "message": "Manual trailing can only be started on an executed, live, "
                           "still-open ticket with trailing_trigger='manual'",
            })
        conn.execute("UPDATE trade_tickets SET trailing_active = 1 WHERE id = ?", (ticket_id,))
        _log_event(conn, ticket_id, "trailing_adjusted", "Trailing manually activated by user")
        conn.commit()
        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        return {"data": _serialize(ticket)}
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

        conn.execute(
            """
            UPDATE trade_tickets SET
                symbol = ?, exchange = ?, instrument_token = ?, transaction_type = ?,
                quantity = ?, entry_price = ?, entry_order_type = ?,
                stop_loss = ?, target_1 = ?, target_2 = ?,
                trailing_enabled = ?, trailing_trigger = ?, trailing_distance = ?,
                product_type = ?, use_gtt = ?, risk_amount = ?, risk_reward_ratio = ?,
                rationale = ?, status = 'draft', risk_check_results = NULL
            WHERE id = ?
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
        _log_event(conn, ticket_id, "modified", diff)
        conn.commit()

        _run_risk_checks_and_transition(conn, account, ticket_id, kite)
        _notify("modified", ticket_id=ticket_id)

        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        return {"data": _serialize(ticket)}
    finally:
        conn.close()


@router.delete("/api/tickets/{ticket_id}")
def delete_ticket(ticket_id: int, kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
        status = ticket["status"]

        if status == "draft":
            conn.execute("DELETE FROM ticket_events WHERE ticket_id = ?", (ticket_id,))
            conn.execute("DELETE FROM trade_tickets WHERE id = ?", (ticket_id,))
            conn.commit()
            _notify("deleted", ticket_id=ticket_id)
            return {"data": {"deleted": True, "archived": False}}

        if status in _SOFT_DELETE_STATUSES:
            _log_event(conn, ticket_id, "archived", f"Archived by user (was '{status}')")
            conn.execute("UPDATE trade_tickets SET archived = 1 WHERE id = ?", (ticket_id,))
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
