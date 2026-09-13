from fastapi import APIRouter, Depends, HTTPException
from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException, TokenException

from backend import execution, system_events
from backend.accounts import get_trading_account
from backend.db import get_connection
from backend.kite_client import require_trading_account
from backend.models import ExecutionModeRequest

router = APIRouter()


def _call(kite_fn):
    try:
        return kite_fn()
    except TokenException:
        raise HTTPException(
            status_code=401,
            detail={"error_code": "SESSION_EXPIRED", "message": "Your Zerodha session has expired. Please reconnect."},
        )
    except KiteException as e:
        raise HTTPException(status_code=502, detail={"error_code": "KITE_API_ERROR", "message": str(e)})


def _linked_ticket_map(order_ids: list, gtt_ids: list) -> dict:
    account = get_trading_account()
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, kite_order_id, kite_gtt_id FROM trade_tickets
            WHERE account_id = ? AND (kite_order_id IN ({}) OR kite_gtt_id IN ({}))
            """.format(
                ",".join("?" * len(order_ids)) or "NULL",
                ",".join("?" * len(gtt_ids)) or "NULL",
            ),
            (account["id"], *order_ids, *gtt_ids),
        ).fetchall()
    finally:
        conn.close()

    by_order_id, by_gtt_id = {}, {}
    for r in rows:
        if r["kite_order_id"]:
            by_order_id[r["kite_order_id"]] = r["id"]
        if r["kite_gtt_id"]:
            by_gtt_id[r["kite_gtt_id"]] = r["id"]
    return by_order_id, by_gtt_id


@router.get("/api/execution/mode")
def get_mode(kite: KiteConnect = Depends(require_trading_account)):
    return {"data": {"mode": execution.get_execution_mode()}}


@router.post("/api/execution/mode")
def set_mode(req: ExecutionModeRequest, kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    try:
        mode = execution.set_execution_mode(req.mode, req.confirm_real_money, account_id=account["id"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error_code": "CONFIRMATION_REQUIRED", "message": str(e)})
    return {"data": {"mode": mode}}


@router.get("/api/orders")
def list_orders(kite: KiteConnect = Depends(require_trading_account)):
    orders = _call(kite.orders)
    order_ids = [o["order_id"] for o in orders]
    by_order_id, _ = _linked_ticket_map(order_ids, [])
    for o in orders:
        o["linked_ticket_id"] = by_order_id.get(o["order_id"])
    return {"data": orders}


@router.get("/api/trades")
def list_trades(kite: KiteConnect = Depends(require_trading_account)):
    trades = _call(kite.trades)
    order_ids = [t["order_id"] for t in trades]
    by_order_id, _ = _linked_ticket_map(order_ids, [])
    for t in trades:
        t["linked_ticket_id"] = by_order_id.get(t["order_id"])
    return {"data": trades}


@router.get("/api/gtts")
def list_gtts(kite: KiteConnect = Depends(require_trading_account)):
    gtts = _call(kite.get_gtts)
    gtt_ids = [g["id"] for g in gtts]
    _, by_gtt_id = _linked_ticket_map([], gtt_ids)
    for g in gtts:
        g["linked_ticket_id"] = by_gtt_id.get(g["id"])
    return {"data": gtts}


@router.post("/api/orders/{order_id}/cancel")
def cancel_order(order_id: str, variety: str = "regular", kite: KiteConnect = Depends(require_trading_account)):
    """Phase 8 Part A.4: a prominent single-click cancel for a pending
    order — this did not exist anywhere in the app before this phase."""
    account = get_trading_account()
    _call(lambda: kite.cancel_order(variety=variety, order_id=order_id))

    conn = get_connection()
    try:
        ticket = conn.execute(
            "SELECT id FROM trade_tickets WHERE account_id = ? AND kite_order_id = ?",
            (account["id"], order_id),
        ).fetchone()
        if ticket is not None:
            conn.execute("UPDATE trade_tickets SET status = 'cancelled' WHERE id = ?", (ticket["id"],))
            conn.execute(
                "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'rejected', ?)",
                (ticket["id"], f"Order {order_id} cancelled manually via Orders & Trades"),
            )
        system_events.log_event(conn, account["id"], "order_cancelled_manually", f"order_id={order_id}")
        conn.commit()
    finally:
        conn.close()

    return {"data": {"cancelled": True, "order_id": order_id, "linked_ticket_id": ticket["id"] if ticket else None}}


@router.post("/api/gtts/{gtt_id}/cancel")
def cancel_gtt(gtt_id: int, kite: KiteConnect = Depends(require_trading_account)):
    """Phase 8 Part A.4: cancels a GTT directly. Deliberately sets the
    linked ticket's kite_gtt_id to NULL rather than leaving it dangling —
    trailing_engine._process_ticket already treats a NULL kite_gtt_id as
    "no GTT to manage, nothing to do until a human intervenes" (the same
    path a protection_failed execution takes), so the position is
    correctly left "still open, now unprotected" rather than misread as
    "closed" on the next trailing-engine tick. Without this, the engine
    would see the GTT gone and (per its Phase 6 logic for a triggered/
    deleted GTT) synthesize a fake fill and mark the position closed —
    wrong, since cancelling here means the position is still genuinely
    open, just no longer protected."""
    account = get_trading_account()
    _call(lambda: kite.delete_gtt(gtt_id))

    conn = get_connection()
    try:
        ticket = conn.execute(
            "SELECT id FROM trade_tickets WHERE account_id = ? AND kite_gtt_id = ?",
            (account["id"], gtt_id),
        ).fetchone()
        if ticket is not None:
            conn.execute("UPDATE trade_tickets SET kite_gtt_id = NULL WHERE id = ?", (ticket["id"],))
            conn.execute(
                "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'modified', ?)",
                (ticket["id"], f"GTT {gtt_id} cancelled manually — position now unprotected, still open"),
            )
        system_events.log_event(conn, account["id"], "gtt_cancelled_manually", f"gtt_id={gtt_id}")
        conn.commit()
    finally:
        conn.close()

    return {"data": {"cancelled": True, "gtt_id": gtt_id, "linked_ticket_id": ticket["id"] if ticket else None}}
