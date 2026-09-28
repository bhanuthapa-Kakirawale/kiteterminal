import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from kiteconnect import KiteConnect

from backend import execution, order_monitor, position_quantity, risk_engine, system_events
from backend.accounts import get_trading_account
from backend.db import get_connection
from backend.kite_client import require_trading_account
from backend.models import EmergencyExitConfirmRequest
from backend.routes.tickets import _transition, confirm_and_execute_ticket

logger = logging.getLogger(__name__)
router = APIRouter()

# The one place in the app where extra friction is intentional — matched
# server-side, never trusting only a disabled frontend button.
_REQUIRED_PHRASE = "EXIT ALL"


def _build_exit_preview(conn, account) -> list[dict]:
    positions = risk_engine.list_open_positions(conn, account["id"])
    return [
        {
            # PK-13: quantity is what was ordered; open_quantity is what is
            # actually held — the most an Emergency Exit will sell.
            "ticket_id": p["id"], "symbol": p["symbol"], "quantity": p["quantity"],
            "filled_quantity": position_quantity.filled(p), "open_quantity": p["open_quantity"],
            "entry_price": p["entry_price"], "stop_loss": p["stop_loss"], "target_1": p["target_1"],
            "current_stop_loss": p["current_stop_loss"], "strategy_name": p["strategy_name"], "mode": p["mode"],
            "entry_fill_status": p["entry_fill_status"],
            "exit_in_flight": _exit_in_flight(conn, p["id"]) is not None,
        }
        for p in positions
    ]


@router.get("/api/kill-switch/emergency-exit-preview")
def emergency_exit_preview(kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        return {"data": _build_exit_preview(conn, account)}
    finally:
        conn.close()


def _fetch_ltps(kite: KiteConnect, positions: list) -> dict:
    if not positions:
        return {}
    keys = [f"{p['exchange']}:{p['symbol']}" for p in positions]
    try:
        return kite.ltp(keys)
    except Exception:
        return {}


def _cancel_unfilled_entry(kite: KiteConnect, conn, position) -> dict | None:
    """An entry that hasn't filled yet has nothing to sell — cancel the order
    instead (order_monitor.py then removes its GTT and closes the ticket).
    Returns None if the cancel fails — most likely the order filled a moment
    ago — so the caller falls back to a normal market exit."""
    try:
        kite.cancel_order(variety=position["order_variety"] or kite.VARIETY_REGULAR, order_id=position["kite_order_id"])
    except Exception:
        return None
    # PK-9: Kite accepting the cancel request is not a cancellation — the
    # order may still fill; order_monitor resolves the entry from Kite.
    detail = (f"Emergency exit: cancellation requested at broker for unfilled entry order "
              f"{position['kite_order_id']} — the order monitor records the outcome")
    conn.execute(
        "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'modified', ?)", (position["id"], detail)
    )
    conn.commit()
    return {"ticket_id": position["id"], "symbol": position["symbol"], "status": "entry_cancelled", "message": detail}


@router.post("/api/kill-switch/emergency-exit-confirm")
def emergency_exit_confirm(req: EmergencyExitConfirmRequest, kite: KiteConnect = Depends(require_trading_account)):
    """Best effort, one result per position (PK-10, P1-2): a failure to exit
    one position — a refusal, a broker error, or an unexpected exception —
    never stops the attempts on the others, and the emergency_exit_all
    system event is always written. Nothing here waits for fills (PK-8)."""
    if req.confirmation_phrase.strip() != _REQUIRED_PHRASE:
        raise HTTPException(400, {
            "error_code": "CONFIRMATION_PHRASE_MISMATCH",
            "message": f"Type exactly \"{_REQUIRED_PHRASE}\" to confirm — nothing was exited.",
        })

    account = get_trading_account()
    conn = get_connection()
    try:
        # No global execution mode here (PK-3): every exit runs in the mode of
        # the position it closes. list_open_positions only returns LIVE
        # positions, so Emergency Exit sells real positions for real, whatever
        # the Dry Run / Live toggle says, and never touches a dry-run one.
        try:
            positions = risk_engine.list_open_positions(conn, account["id"])
        except Exception as e:
            logger.exception("Emergency Exit could not list open positions")
            conn.rollback()
            message = (f"Emergency Exit could not list open positions ({type(e).__name__}: {e}); nothing was "
                       f"exited. Check Kite and exit positions there.")
            _log_summary_event(conn, account, message)
            raise HTTPException(503, {"error_code": "EMERGENCY_EXIT_UNAVAILABLE", "message": message})
        ltp_data = _fetch_ltps(kite, positions)

        results = []
        for pos in positions:
            attempt: dict = {}
            try:
                results.append(_exit_one(conn, kite, account, pos, ltp_data, attempt))
            except Exception as e:
                # Anything unexpected for THIS position (P0-2 style broker
                # surprises, a DB error, a bug): record it and move on to the
                # next position — never abort the loop.
                logger.exception("Emergency Exit of position %s (%s) raised", pos["id"], pos["symbol"])
                conn.rollback()
                results.append(_isolated_failure(conn, account, pos, attempt.get("ticket_id"), e))

        summary = {status: sum(r["status"] == status for r in results) for status in _SUMMARY_STATUSES}
        _log_summary_event(conn, account, _summary_detail(positions, results, summary))
        return {"data": {"count": len(positions), "results": results, "summary": summary}}
    finally:
        conn.close()


# Every per-position outcome, in the order the system event lists them.
_SUMMARY_STATUSES = ("exit_submitted", "already_exiting", "exit_outcome_unknown", "refused", "failed",
                     "entry_cancelled", "risk_check_unavailable", "closed")


def _exit_one(conn, kite: KiteConnect, account, pos, ltp_data: dict, attempt: dict) -> dict:
    """Requests the exit of one open position and returns its result. Any
    exception propagates to the caller's per-position isolation; the id of an
    exit ticket created on the way is left in attempt['ticket_id'] so the
    failure can be reported accurately."""
    in_flight = _exit_in_flight(conn, pos["id"])
    if in_flight is not None and in_flight["status"] == "confirmed":
        # An exit for this position is claimed and its broker outcome is
        # in flight or UNKNOWN (PK-2): a second SELL now could double the
        # exit. order_monitor resolves the first one from broker evidence.
        return {
            "ticket_id": in_flight["id"], "symbol": pos["symbol"], "status": "exit_outcome_unknown",
            "message": f"An exit for {pos['symbol']} (ticket {in_flight['id']}, tag {in_flight['order_tag']}) "
                       f"has an UNKNOWN broker outcome — not resubmitted. Check Kite.",
        }
    if in_flight is not None:
        # PK-8: an exit order is already at the broker, not yet (fully)
        # filled. The position is still open, but selling again now
        # could double the exit.
        return {
            "ticket_id": in_flight["id"], "symbol": pos["symbol"], "status": "already_exiting",
            "message": f"{pos['symbol']}: exit ticket {in_flight['id']} (order {in_flight['kite_order_id']}) "
                       f"is already at the broker, fill {in_flight['entry_fill_status']} — not resubmitted. "
                       f"Check Kite.",
        }
    note = ""
    if pos["mode"] == "live" and pos["entry_fill_status"] == "pending" and pos["kite_order_id"]:
        # PK-13: stop the rest of the entry, then sell only what the broker
        # confirms was bought — re-read from Kite now, since the monitor's
        # figure may be a cycle old. Never the requested quantity.
        cancelled = _cancel_unfilled_entry(kite, conn, pos)
        held = _refresh_entry_fill(conn, kite, pos)
        if cancelled is not None and not held:
            return cancelled
        note = (f" (cancellation of the rest of entry order {pos['kite_order_id']} requested)" if cancelled else
                f" (cancelling entry order {pos['kite_order_id']} FAILED — it may still fill further; check Kite)")

    held = position_quantity.open_quantity(conn, pos["id"])
    if held is None or held <= 0:
        why = ("its open quantity is unknown (a partial fill was never recorded)" if held is None else
               f"no broker-confirmed shares are held ({position_quantity.describe(pos, held)})")
        return {"ticket_id": None, "symbol": pos["symbol"], "status": "refused",
                "error_code": "QUANTITY_UNKNOWN" if held is None else "NOTHING_HELD",
                "message": f"{pos['symbol']}: not exited — {why}{note}. Nothing was sent; check the position in "
                           f"Kite."}

    ticket_id = _create_emergency_exit_ticket(conn, account, pos, ltp_data)
    attempt["ticket_id"] = ticket_id
    try:
        risk_engine.run_risk_checks_and_transition(conn, account, ticket_id, kite)
    except risk_engine.RiskCheckUnavailableError as e:
        return {"ticket_id": ticket_id, "symbol": pos["symbol"], "status": "risk_check_unavailable",
                "message": e.message}

    ticket = conn.execute("SELECT status FROM trade_tickets WHERE id = ?", (ticket_id,)).fetchone()
    if ticket["status"] != "pending_confirmation":
        # Exit tickets skip the six position-sizing checks (Phase 5
        # Part D.4) — only the exit-validity check can fail here,
        # which would mean the referenced position wasn't actually
        # a valid open executed BUY, a genuine data problem worth
        # surfacing rather than silently executing anyway.
        return {"ticket_id": ticket_id, "symbol": pos["symbol"], "status": "refused",
                "error_code": "EXIT_VALIDATION_FAILED",
                "message": f"{pos['symbol']}: the exit failed validation (ticket {ticket_id} is "
                           f"'{ticket['status']}'); no order was sent. Check the position."}

    try:
        data = confirm_and_execute_ticket(conn, account, kite, ticket_id)
    except HTTPException as e:
        # Refused at confirmation (e.g. PK-4: another exit took the
        # position meanwhile). Nothing was sent for this position;
        # keep going — one refusal must not stop the other exits.
        detail = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
        return {"ticket_id": ticket_id, "symbol": pos["symbol"], "status": "refused",
                "error_code": detail.get("error_code"), "message": detail.get("message")}
    return {
        "ticket_id": ticket_id, "symbol": pos["symbol"],
        "status": _result_status(data), "message": data["execution_message"] + note,
    }


def _refresh_entry_fill(conn, kite: KiteConnect, pos) -> int | None:
    """The entry's broker fill as Kite reports it right now, recorded
    monotonically (and the protective GTT resized to it). Returns the
    position's open quantity afterwards; unchanged if Kite cannot say."""
    order = order_monitor._order_from_history(kite, pos["kite_order_id"])
    if order is not None:
        outcome = position_quantity.record_fill(conn, pos, order.get("filled_quantity") or 0)
        if outcome == position_quantity.INCREASED:
            position_quantity.sync_protection(conn, kite, pos["id"], "Emergency Exit refreshed the entry fill")
        conn.commit()
    return position_quantity.open_quantity(conn, pos["id"])


def _isolated_failure(conn, account, pos, ticket_id: int | None, error: Exception) -> dict:
    """The result for a position whose exit attempt raised. Never guesses:
    if this click's exit ticket was already claimed, an order may exist, so
    the outcome is UNKNOWN (order_monitor follows it up), never 'failed'. An
    exit ticket that never reached the claim is withdrawn, so no stale
    emergency SELL is left waiting for approval."""
    reason = f"{type(error).__name__}: {error}"
    result = {"ticket_id": ticket_id, "symbol": pos["symbol"], "status": "failed",
              "error_code": "EMERGENCY_EXIT_ERROR",
              "message": f"{pos['symbol']}: exit attempt failed ({reason}); no order was sent for this position. "
                         f"The position is still OPEN — exit it in Kite."}
    if ticket_id is None:
        return result
    try:
        if _transition(conn, ticket_id, account["id"], ("draft", "pending_confirmation"), "rejected"):
            conn.execute(
                "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'rejected', ?)",
                (ticket_id, f"Emergency exit attempt failed before any order was sent ({reason}); withdrawn."),
            )
            conn.commit()
            return result
        conn.rollback()
        row = conn.execute("SELECT status, submission_state FROM trade_tickets WHERE id = ?", (ticket_id,)).fetchone()
    except Exception:
        logger.exception("Emergency Exit: could not inspect exit ticket %s after a failure", ticket_id)
        conn.rollback()
        row = None
    if row is not None and row["status"] in ("rejected", "risk_rejected", "cancelled"):
        return result  # never claimed: nothing was sent
    return {"ticket_id": ticket_id, "symbol": pos["symbol"], "status": "exit_outcome_unknown",
            "error_code": "EMERGENCY_EXIT_ERROR",
            "message": f"{pos['symbol']}: exit ticket {ticket_id} was claimed "
                       f"({row['status'] if row else 'state unreadable'}) but the attempt then failed ({reason}). "
                       f"An order may exist — check Kite; the order monitor follows it up. Not resubmitted."}


def _summary_detail(positions: list, results: list[dict], summary: dict) -> str:
    """What the click did, per outcome — never 'exited', since an exit
    order placed is not a position closed (PK-8)."""
    by_mode = {m: sum(p["mode"] == m for p in positions) for m in ("live", "dry_run")}
    counts = ", ".join(f"{status} {n}" for status, n in summary.items())
    listed = ", ".join(f"{r['symbol']}={r['status']}" for r in results) or "(none)"
    return (f"Emergency Exit: {len(positions)} position(s) attempted, each in its own execution mode "
            f"(live: {by_mode['live']}, dry_run: {by_mode['dry_run']}) — {counts}: {listed}")


def _log_summary_event(conn, account, detail: str) -> None:
    # A failure to write the audit event must not hide the per-position
    # results from the user — the orders were already requested.
    try:
        system_events.log_event(conn, account["id"], "emergency_exit_all", detail)
        conn.commit()
    except Exception:
        logger.exception("Emergency Exit: could not write the emergency_exit_all system event")
        conn.rollback()


def _exit_in_flight(conn, position_id: int):
    """The exit that owns this position while its order is in flight:
    claimed / UNKNOWN ('confirmed'), or at the broker and EXITING (PK-8). An
    exit that ended after a partial fill does not (PK-13): what it left is
    exited anew."""
    return conn.execute(
        f"""
        SELECT id, status, order_tag, kite_order_id, entry_fill_status FROM trade_tickets
        WHERE closes_ticket_id = ? AND {execution.EXIT_IN_FLIGHT_SQL}
        ORDER BY id LIMIT 1
        """,
        (position_id,),
    ).fetchone()


def _result_status(data: dict) -> str:
    """What this click achieved for one position. Emergency Exit never waits
    for fills (PK-8): an accepted order is 'exit_submitted' — the position is
    closed later, by order_monitor, when Kite reports the fill."""
    if data["status"] == "executed":
        return "closed" if data["entry_fill_status"] == "filled" else "exit_submitted"
    if data["status"] == "confirmed":
        return "exit_outcome_unknown"
    return data["status"]  # 'failed': the broker refused the order


def _create_emergency_exit_ticket(conn, account, position, ltp_data: dict) -> int:
    # Reference/expected fill price for this MARKET exit — the current LTP
    # when available (same source check_breakeven_candidates already uses
    # for a live price), falling back to the position's own current stop
    # if the LTP fetch failed, so ticket creation never blocks on it.
    # Sized to the position's OPEN quantity as it is now (PK-13) — never
    # what the entry requested; the caller has checked it is positive.
    quantity = position_quantity.open_quantity(conn, position["id"])
    key = f"{position['exchange']}:{position['symbol']}"
    entry_price = ltp_data.get(key, {}).get("last_price") or position["current_stop_loss"] or position["stop_loss"]

    metrics = execution.compute_risk_metrics({
        "entry_price": entry_price, "quantity": quantity,
        "stop_loss": position["stop_loss"], "target_1": position["target_1"], "target_2": None,
    })
    cursor = conn.execute(
        """
        INSERT INTO trade_tickets (
            account_id, source, strategy_name, strategy_version, symbol, exchange, instrument_token,
            transaction_type, quantity, entry_price, entry_order_type,
            stop_loss, target_1, target_2,
            trailing_enabled, trailing_trigger, trailing_distance,
            product_type, use_gtt, risk_amount, risk_reward_ratio,
            rationale, status, closes_ticket_id, rule_checklist, mode
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'SELL', ?, ?, 'MARKET', ?, ?, NULL,
                  0, NULL, NULL, ?, 1, ?, ?, ?, 'draft', ?, ?, ?)
        """,
        (
            account["id"], position["source"], position["strategy_name"], position["strategy_version"],
            position["symbol"], position["exchange"], position["instrument_token"],
            quantity, entry_price, position["stop_loss"], position["target_1"],
            position["product_type"], metrics["risk_amount"], metrics["risk_reward_ratio"],
            "Emergency exit — all positions", position["id"],
            json.dumps({"emergency_exit_all": True}), position["mode"],  # inherited, never the global mode
        ),
    )
    ticket_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'created', ?)",
        (ticket_id, "Emergency exit — all positions"),
    )
    conn.commit()
    return ticket_id
