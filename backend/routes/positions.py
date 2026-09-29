"""PK-19: app-managed positions, in a UI-ready shape.

/api/positions is the broker's own positions passthrough (routes/portfolio.py).
These routes are about the positions THIS terminal opened, each described by
the server-derived lifecycle (backend/lifecycle.py) — the frontend displays
it and never reconstructs it from raw ticket columns.

Reading makes no broker call: everything comes from persisted, app-known
state, in a constant number of queries however many positions there are.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from kiteconnect import KiteConnect

from backend import lifecycle, risk_engine
from backend.accounts import get_trading_account
from backend.db import get_connection
from backend.kite_client import require_trading_account
from backend.routes import kill_switch, tickets

logger = logging.getLogger(__name__)
router = APIRouter()

EXIT_NOT_ALLOWED = "EXIT_NOT_ALLOWED"
EXIT_ALREADY_PENDING = "EXIT_ALREADY_PENDING"

_WHY_NOT = {
    lifecycle.SUBMISSION_UNKNOWN: "its broker submission outcome is UNKNOWN — do not resubmit; the order monitor is "
                                  "resolving it from Kite",
    lifecycle.EXITING: "an exit order is already at the broker (EXITING)",
    lifecycle.EXIT_PARTIAL: "an exit order is still working at the broker",
    lifecycle.ENTRY_OPEN: "its entry order is still working — cancel it from Orders & Trades, or use Emergency Exit",
    lifecycle.ENTRY_PARTIAL: "its entry order is still working — cancel the rest from Orders & Trades, or use "
                             "Emergency Exit",
    lifecycle.NEEDS_REVIEW: "its quantity or fill needs review in Kite first",
    lifecycle.CLOSED: "it is already closed",
    lifecycle.SUBMITTING: "its entry is still being submitted",
}


@router.get("/api/app-positions")
def list_app_positions(view: str = Query("active", pattern="^(active|closed)$"),
                       limit: int = Query(200, ge=1, le=1000),
                       kite: KiteConnect = Depends(require_trading_account)):
    """view=active: every position that is, or may be, at the broker —
    waiting to fill, UNKNOWN, open, exiting, needs review. view=closed:
    broker-proven closed positions with their journal round trip."""
    account = get_trading_account()
    conn = get_connection()
    try:
        ctx = lifecycle.build_context(conn, account["id"])
        positions = lifecycle.positions(ctx, view)[:limit]
        body = {"data": positions, "view": view}
        if view == "active":
            body["summary"] = lifecycle.summary(positions)
        return body
    finally:
        conn.close()


@router.post("/api/app-positions/{position_id}/exit", status_code=201)
def exit_position(position_id: int, kite: KiteConnect = Depends(require_trading_account)):
    """Creates ONE linked MARKET SELL exit ticket for the position's open
    quantity, in the position's own mode (PK-3), and runs its risk checks.
    Nothing is confirmed or sent here: the ticket is approved through the
    normal confirm path, which re-checks ownership, quantity and mode
    (PK-1/3/4/8/12/13/17) at the moment of approval."""
    account = get_trading_account()
    conn = get_connection()
    try:
        ctx = lifecycle.build_context(conn, account["id"])
        position = ctx.tickets.get(position_id)
        if position is None or not lifecycle.is_position_ticket(position):
            raise HTTPException(404, {"error_code": "NOT_FOUND", "message": "Position not found"})
        lc = lifecycle.derive(position, ctx)
        if lifecycle.EXIT not in lc["allowed_actions"]:
            if lc["pending_exit_ticket_id"] is not None:
                raise HTTPException(409, {
                    "error_code": EXIT_ALREADY_PENDING,
                    "message": f"An exit ticket (#{lc['pending_exit_ticket_id']}) for {position['symbol']} already "
                               f"awaits review — review it instead. Nothing was created.",
                    "ticket_id": lc["pending_exit_ticket_id"], "lifecycle_state": lc["lifecycle_state"],
                })
            why = _WHY_NOT.get(lc["lifecycle_state"], f"its state is {lc['lifecycle_label']}")
            raise HTTPException(409, {
                "error_code": EXIT_NOT_ALLOWED,
                "message": f"{position['symbol']} cannot be exited from here: {why}. Nothing was created.",
                "lifecycle_state": lc["lifecycle_state"],
            })
        # The same ownership / quantity check the confirm path runs, against
        # the exit this request would create.
        problem = risk_engine.linked_exit_problem(conn, {
            "id": 0, "closes_ticket_id": position_id, "account_id": account["id"],
            "quantity": lc["open_quantity"],
        })
        if problem is not None:
            raise HTTPException(409, {"error_code": problem.code,
                                      "message": f"{problem.message} Nothing was created."})

        ltp = kill_switch._fetch_ltps(kite, [position])
        ticket_id = kill_switch.create_linked_exit_ticket(
            conn, account, position, ltp, f"Exit requested from the Positions view ({lc['open_quantity']} share(s))",
            {"position_exit": {"pass": True, "open_quantity": lc["open_quantity"]}},
        )
        tickets._run_risk_checks_and_transition(conn, account, ticket_id, kite)
        tickets._notify("created", ticket_id=ticket_id)
        ticket = tickets._get_ticket_or_404(conn, ticket_id, account["id"])
        return {"data": tickets._with_lifecycle(conn, ticket)}
    finally:
        conn.close()
