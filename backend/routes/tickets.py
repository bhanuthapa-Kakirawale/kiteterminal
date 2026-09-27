import json
import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from kiteconnect import KiteConnect

from backend import (
    execution, instruments, journal, kite_chart, reconciliation, risk_engine, signal_chart, system_events,
)
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
    from_statuses: tuple[str, ...], to_status: str, **columns,
) -> bool:
    """Compare-and-set status change: moves the ticket to to_status only if it
    is still in one of from_statuses. The UPDATE's WHERE clause is the lock —
    SQLite serialises writers, so of two requests racing for the same ticket
    exactly one gets rowcount 1. True means this caller won the transition and
    owns whatever follows it (placing the order, writing the event). Column
    names come from code, never from request input."""
    assignments = ", ".join(["status = ?", *(f"{name} = ?" for name in columns)])
    placeholders = ", ".join("?" * len(from_statuses))
    cursor = conn.execute(
        f"UPDATE trade_tickets SET {assignments} "
        f"WHERE id = ? AND account_id = ? AND status IN ({placeholders})",
        (to_status, *columns.values(), ticket_id, account_id, *from_statuses),
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
        # A linked full close inherits the position's mode (PK-3); an
        # unlinked reduction stays NULL and takes the global mode at claim.
        mode = None
        link_note = "none (partial reduction)"
        if len(open_positions) == 1 and open_positions[0]["quantity"] == req.quantity:
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
        in_flight = _exits_in_flight(conn, account["id"])
        data = []
        for r in rows:
            item = _serialize(r)
            item["exit_in_flight_ticket_id"] = in_flight.get(r["id"])
            data.append(item)
        return {"data": data}
    finally:
        conn.close()


def _exits_in_flight(conn: sqlite3.Connection, account_id: int) -> dict[int, int]:
    """position id -> the exit ticket that owns it but has not proven a fill
    (claimed/UNKNOWN, EXITING, partial): the position still exists, but an
    exit is in flight (PK-8)."""
    rows = conn.execute(
        f"""
        SELECT closes_ticket_id, id FROM trade_tickets
        WHERE account_id = ? AND closes_ticket_id IS NOT NULL
          AND (status = 'confirmed' OR (status = 'executed' AND NOT ({execution.EXIT_FILL_PROVEN_SQL})))
        """,
        (account_id,),
    ).fetchall()
    return {r["closes_ticket_id"]: r["id"] for r in rows}


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
        return {"data": _serialize(ticket)}
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


def _update_if_still_claimed(conn: sqlite3.Connection, ticket: sqlite3.Row, **columns) -> bool:
    """Compare-and-set for confirmation-time revalidation: applies only while
    the ticket is still exactly as this request claimed it — 'confirmed' and
    'submitting' (live) or never submitted (dry run). If order_monitor has
    since marked the claim unknown/needs_review, that state is never
    overwritten. Kept separate from execution.update_ticket_if, which is the
    post-broker-call outcome write."""
    assignments = ", ".join(f"{name} = ?" for name in columns)
    submission = "submission_state = ?" if ticket["mode"] == "live" else "submission_state IS NULL"
    params = (execution.SUBMITTING,) if ticket["mode"] == "live" else ()
    return conn.execute(
        f"UPDATE trade_tickets SET {assignments} WHERE id = ? AND status = 'confirmed' AND {submission}",
        (*columns.values(), ticket["id"], *params),
    ).rowcount == 1


# Nothing was sent: the claim's submission bookkeeping is withdrawn with it.
_NOT_SUBMITTED = dict(order_tag=None, submission_state=None, submitted_at=None)


def _revalidate_at_confirm(conn: sqlite3.Connection, account: sqlite3.Row, kite: KiteConnect,
                           ticket: sqlite3.Row) -> None:
    """Confirmation is a fresh execution gate (PK-4): the risk checks saved
    when the ticket was created may no longer hold — another approval may
    have used the room this ticket counted on. Re-runs every check against
    the broker's portfolio as it is now. Returns only if the ticket may
    execute; otherwise the ticket leaves 'confirmed' for a terminal state and
    an HTTPException is raised. The broker is never called on a refusal."""
    try:
        report = risk_engine.run_checks(conn, account, ticket, kite)
    except Exception as e:
        # The checks were never evaluated, so this is not a risk rejection —
        # but without them nothing may be placed. Fail closed. 'rejected'
        # rather than 'cancelled' so Revert can re-run the checks once the
        # broker is reachable again; the ticket is never reopened by itself.
        reason = e.message if isinstance(e, risk_engine.RiskCheckUnavailableError) else f"{type(e).__name__}: {e}"
        message = f"Risk could not be revalidated ({reason}); no order was sent."
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
        return

    failed = [c for c in report.checks if not c.passed]
    message = ("Passed creation-time risk checks but failed confirmation-time revalidation: "
               + "; ".join(c.message for c in failed) + ". No order was sent.")
    _end_claim(conn, ticket, account["id"], "risk_rejected", "risk_rejected", message, risk_check_results=results)
    raise HTTPException(409, {
        "error_code": "RISK_REVALIDATION_FAILED", "message": message,
        "failed_checks": [c.rule for c in failed],
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


def confirm_and_execute_ticket(conn: sqlite3.Connection, account: sqlite3.Row, kite: KiteConnect, ticket_id: int) -> dict:
    """Shared confirm-and-execute body: transitions a pending_confirmation
    ticket to 'confirmed', calls the execution engine, updates status from the
    result, and records a closed_trades row for exit tickets. Used by both
    the normal one-ticket confirm endpoint and Phase 8's Emergency Exit All
    batch flow, so there is exactly one code path for "how a ticket
    actually executes" regardless of how it got confirmed."""
    ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
    if ticket["status"] not in _CONFIRMABLE_STATUSES:
        _raise_invalid_state(conn, ticket_id, account["id"], "confirm")

    mode, mode_note = _claim_mode(conn, ticket, account["id"])
    if ticket["closes_ticket_id"] is not None:
        # PK-4: the position may have been closed, or another exit may own
        # it, since this exit was created. Refused before the claim.
        _refuse_stale_exit(conn, ticket, account["id"])
    # A live claim also records the order tag and 'submitting' in the same
    # write, before any broker call: if this process dies mid-submission, the
    # database alone tells order_monitor what to look for (PK-2).
    submission = {}
    if mode == "live":
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
    try:
        claimed = _transition(conn, ticket_id, account["id"], _CONFIRMABLE_STATUSES, "confirmed",
                              mode=mode, **submission)
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
        _raise_invalid_state(conn, ticket_id, account["id"], "confirm")
    _log_event(conn, ticket_id, "confirmed", f"Confirmed in {mode} mode{mode_note}")
    conn.commit()

    ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
    if ticket["closes_ticket_id"] is None:
        # PK-4: this ticket won the claim; now re-check it against the
        # portfolio as it is NOW. Only if it still passes may it execute.
        _revalidate_at_confirm(conn, account, kite, ticket)
    try:
        result = execution.execute_ticket(kite, ticket)
    except Exception as e:
        if ticket["mode"] != "live":
            raise
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

    ticket = _get_ticket_or_404(conn, ticket_id, account["id"])
    # PK-8: an exit closes its position only once its fill is proven. A live
    # exit Kite accepted is EXITING — the position stays open, protected and
    # unjournaled, and order_monitor.py closes it when the broker reports the
    # full quantity COMPLETE. Only a dry-run exit is filled here (simulated),
    # so only it is closed now, through the same journal path. Only the
    # writer that won the outcome may do this.
    if (won and ticket["transaction_type"] == "SELL" and ticket["closes_ticket_id"] is not None
            and ticket["status"] == "executed" and ticket["entry_fill_status"] == "filled"):
        journal.close_position_for_exit(conn, ticket_id, ticket["closes_ticket_id"],
                                        ticket["filled_at"] or execution.utc_now_sql())
        ticket = _get_ticket_or_404(conn, ticket_id, account["id"])

    _notify("executed", ticket_id=ticket_id, status=ticket["status"], success=result.success)
    data = _serialize(ticket)
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
    request claimed. A live claim is 'submitting'; order_monitor may since
    have marked a stale claim 'unknown' — definite broker evidence from this
    request still upgrades that. Returns False once the monitor has resolved
    the ticket itself (adopted / not_found / needs_review)."""
    live = ticket["mode"] == "live"
    claimed = (execution.SUBMITTING, execution.UNKNOWN) if live else (None,)

    if result.event_type in ("executed", "protection_failed"):
        # A live order is 'pending' until order_monitor.py sees the broker's
        # fill; a dry-run simulation is filled at its reference price.
        filled_now = result.entry_fill_status == "filled"
        now = execution.utc_now_sql()
        return execution.update_ticket_if(
            conn, ticket["id"], status_in=("confirmed",), submission_state_in=claimed,
            status=result.status, kite_order_id=result.kite_order_id, kite_gtt_id=result.kite_gtt_id,
            current_stop_loss=ticket["stop_loss"], executed_at=now,
            order_variety=result.order_variety, entry_fill_status=result.entry_fill_status,
            filled_price=ticket["entry_price"] if filled_now else None,
            filled_at=now if filled_now else None,
            submission_state=execution.ACKNOWLEDGED if live else None,
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
        submission_state=execution.REJECTED if live else None,
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
        if execution.is_closing_sell(ticket):
            # An exit's side, quantity and position link (closes_ticket_id)
            # and its inherited mode belong together; editing any of them
            # could turn it into a different trade (PK-3/PK-4).
            raise HTTPException(409, {
                "error_code": "EXIT_TICKET_NOT_EDITABLE",
                "message": "Exit / reduction tickets cannot be edited — reject it and create a new one instead.",
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
