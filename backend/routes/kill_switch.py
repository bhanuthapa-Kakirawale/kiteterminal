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
        # No global execution mode here (PK-3): every exit runs in the mode of
        # the position it closes. list_open_positions only returns LIVE
        # positions, so Emergency Exit sells real positions for real, whatever
        # the Dry Run / Live toggle says, and never touches a dry-run one.
        positions = risk_engine.list_open_positions(conn, account["id"])
        ltp_data = _fetch_ltps(kite, positions)

        results = []
        for pos in positions:
            in_flight = _exit_in_flight(conn, pos["id"])
            if in_flight is not None and in_flight["status"] == "confirmed":
                # An exit for this position is claimed and its broker outcome is
                # in flight or UNKNOWN (PK-2): a second SELL now could double the
                # exit. order_monitor resolves the first one from broker evidence.
                results.append({
                    "ticket_id": in_flight["id"], "symbol": pos["symbol"], "status": "exit_outcome_unknown",
                    "message": f"An exit for {pos['symbol']} (ticket {in_flight['id']}, tag {in_flight['order_tag']}) "
                               f"has an UNKNOWN broker outcome — not resubmitted. Check Kite.",
                })
                continue
            if in_flight is not None:
                # PK-8: an exit order is already at the broker, not yet (fully)
                # filled. The position is still open, but selling again now
                # could double the exit.
                results.append({
                    "ticket_id": in_flight["id"], "symbol": pos["symbol"], "status": "already_exiting",
                    "message": f"{pos['symbol']}: exit ticket {in_flight['id']} (order {in_flight['kite_order_id']}) "
                               f"is already at the broker, fill {in_flight['entry_fill_status']} — not resubmitted. "
                               f"Check Kite.",
                })
                continue
            if pos["mode"] == "live" and pos["entry_fill_status"] == "pending" and pos["kite_order_id"]:
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

            try:
                data = confirm_and_execute_ticket(conn, account, kite, ticket_id)
            except HTTPException as e:
                # Refused at confirmation (e.g. PK-4: another exit took the
                # position meanwhile). Nothing was sent for this position;
                # keep going — one refusal must not stop the other exits.
                detail = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
                results.append({"ticket_id": ticket_id, "symbol": pos["symbol"], "status": "refused",
                                "error_code": detail.get("error_code"), "message": detail.get("message")})
                continue
            results.append({
                "ticket_id": ticket_id, "symbol": pos["symbol"],
                "status": _result_status(data), "message": data["execution_message"],
            })

        by_mode = {m: sum(p["mode"] == m for p in positions) for m in ("live", "dry_run")}
        system_events.log_event(
            conn, account["id"], "emergency_exit_all",
            f"exit requested for {len(positions)} position(s), each in its own execution mode "
            f"(live: {by_mode['live']}, dry_run: {by_mode['dry_run']}): "
            f"{', '.join(p['symbol'] for p in positions) or '(none)'}",
        )
        conn.commit()
        return {"data": {"count": len(positions), "results": results}}
    finally:
        conn.close()


def _exit_in_flight(conn, position_id: int):
    """The exit that owns this position without a proven fill: claimed /
    UNKNOWN ('confirmed'), or at the broker and EXITING / partial (PK-8)."""
    return conn.execute(
        f"""
        SELECT id, status, order_tag, kite_order_id, entry_fill_status FROM trade_tickets
        WHERE closes_ticket_id = ?
          AND (status = 'confirmed' OR (status = 'executed' AND NOT ({execution.EXIT_FILL_PROVEN_SQL})))
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
            rationale, status, closes_ticket_id, rule_checklist, mode
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'SELL', ?, ?, 'MARKET', ?, ?, NULL,
                  0, NULL, NULL, ?, 1, ?, ?, ?, 'draft', ?, ?, ?)
        """,
        (
            account["id"], position["source"], position["strategy_name"], position["strategy_version"],
            position["symbol"], position["exchange"], position["instrument_token"],
            position["quantity"], entry_price, position["stop_loss"], position["target_1"],
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
