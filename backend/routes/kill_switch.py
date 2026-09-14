import json

from fastapi import APIRouter, Depends, HTTPException
from kiteconnect import KiteConnect

from backend import execution, risk_engine, system_events
from backend.accounts import get_trading_account
from backend.db import get_connection
from backend.kite_client import require_trading_account
from backend.models import EmergencyExitConfirmRequest
from backend.routes.tickets import confirm_and_execute_ticket

router = APIRouter()

# The one place in the app where extra friction is intentional — matched
# server-side, never trusting only a disabled frontend button.
_REQUIRED_PHRASE = "EXIT ALL"


def _build_exit_preview(conn, account) -> list[dict]:
    positions = risk_engine.list_open_positions(conn, account["id"])
    return [
        {
            "ticket_id": p["id"], "symbol": p["symbol"], "quantity": p["quantity"],
            "entry_price": p["entry_price"], "stop_loss": p["stop_loss"], "target_1": p["target_1"],
            "current_stop_loss": p["current_stop_loss"], "strategy_name": p["strategy_name"], "mode": p["mode"],
            "entry_fill_status": p["entry_fill_status"],
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
    detail = f"Emergency exit: unfilled entry order {position['kite_order_id']} cancelled"
    conn.execute(
        "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'modified', ?)", (position["id"], detail)
    )
    conn.commit()
    return {"ticket_id": position["id"], "symbol": position["symbol"], "status": "entry_cancelled", "message": detail}


@router.post("/api/kill-switch/emergency-exit-confirm")
def emergency_exit_confirm(req: EmergencyExitConfirmRequest, kite: KiteConnect = Depends(require_trading_account)):
    if req.confirmation_phrase.strip() != _REQUIRED_PHRASE:
        raise HTTPException(400, {
            "error_code": "CONFIRMATION_PHRASE_MISMATCH",
            "message": f"Type exactly \"{_REQUIRED_PHRASE}\" to confirm — nothing was exited.",
        })

    account = get_trading_account()
    conn = get_connection()
    try:
        mode = execution.get_execution_mode()
        positions = risk_engine.list_open_positions(conn, account["id"])
        ltp_data = _fetch_ltps(kite, positions)

        results = []
        for pos in positions:
            if mode == "live" and pos["entry_fill_status"] == "pending" and pos["kite_order_id"]:
                cancelled = _cancel_unfilled_entry(kite, conn, pos)
                if cancelled is not None:
                    results.append(cancelled)
                    continue

            ticket_id = _create_emergency_exit_ticket(conn, account, pos, ltp_data)
            try:
                risk_engine.run_risk_checks_and_transition(conn, account, ticket_id, kite)
            except risk_engine.RiskCheckUnavailableError as e:
                results.append({"ticket_id": ticket_id, "symbol": pos["symbol"], "status": "risk_check_unavailable", "message": e.message})
                continue

            ticket = conn.execute("SELECT status FROM trade_tickets WHERE id = ?", (ticket_id,)).fetchone()
            if ticket["status"] != "pending_confirmation":
                # Exit tickets skip the six position-sizing checks (Phase 5
                # Part D.4) — only the exit-validity check can fail here,
                # which would mean the referenced position wasn't actually
                # a valid open executed BUY, a genuine data problem worth
                # surfacing rather than silently executing anyway.
                results.append({"ticket_id": ticket_id, "symbol": pos["symbol"], "status": ticket["status"]})
                continue

            data = confirm_and_execute_ticket(conn, account, kite, ticket_id)
            results.append({
                "ticket_id": ticket_id, "symbol": pos["symbol"],
                "status": data["status"], "message": data["execution_message"],
            })

        system_events.log_event(
            conn, account["id"], "emergency_exit_all",
            f"{len(positions)} position(s) exited in {mode} mode: {', '.join(p['symbol'] for p in positions) or '(none)'}",
        )
        conn.commit()
        return {"data": {"count": len(positions), "results": results}}
    finally:
        conn.close()


def _create_emergency_exit_ticket(conn, account, position, ltp_data: dict) -> int:
    # Reference/expected fill price for this MARKET exit — the current LTP
    # when available (same source check_breakeven_candidates already uses
    # for a live price), falling back to the position's own current stop
    # if the LTP fetch failed, so ticket creation never blocks on it.
    key = f"{position['exchange']}:{position['symbol']}"
    entry_price = ltp_data.get(key, {}).get("last_price") or position["current_stop_loss"] or position["stop_loss"]

    metrics = execution.compute_risk_metrics({
        "entry_price": entry_price, "quantity": position["quantity"],
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
            rationale, status, closes_ticket_id, rule_checklist
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'SELL', ?, ?, 'MARKET', ?, ?, NULL,
                  0, NULL, NULL, ?, 1, ?, ?, ?, 'draft', ?, ?)
        """,
        (
            account["id"], position["source"], position["strategy_name"], position["strategy_version"],
            position["symbol"], position["exchange"], position["instrument_token"],
            position["quantity"], entry_price, position["stop_loss"], position["target_1"],
            position["product_type"], metrics["risk_amount"], metrics["risk_reward_ratio"],
            "Emergency exit — all positions", position["id"],
            json.dumps({"emergency_exit_all": True}),
        ),
    )
    ticket_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'created', ?)",
        (ticket_id, "Emergency exit — all positions"),
    )
    conn.commit()
    return ticket_id
