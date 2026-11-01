from fastapi import APIRouter, Depends, HTTPException
from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException, TokenException

from backend import execution, live_arming, live_preflight, paper_broker, system_events
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
            WHERE account_id = ? AND mode = 'live' AND (kite_order_id IN ({}) OR kite_gtt_id IN ({}))
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
    """SEC-1: arming LIVE requires a fresh, broker-checked Live Readiness
    pass (read-only broker calls only). It may pass with the trading lock
    still ON. Switching to dry run is never gated."""
    account = get_trading_account()
    readiness = None
    if req.mode == "live" and req.confirm_real_money and live_arming.needs_live_arming(account["id"]):
        readiness = live_preflight.evaluate(broker=True, refresh=True)
    try:
        mode = live_arming.set_mode(req.mode, req.confirm_real_money, account["id"], readiness=readiness)
    except live_arming.LiveReadinessNotMet as e:
        raise HTTPException(status_code=409, detail={"error_code": live_arming.LIVE_READINESS_NOT_MET,
                                                     "message": e.message, "blocking_codes": e.blocking_codes})
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
    order — this did not exist anywhere in the app before this phase.

    PK-9 (P1-1): Kite accepting a cancel REQUEST is not a cancellation — the
    order may fill in the same second, or already be partly filled. So the
    linked ticket's state is deliberately left alone here; order_monitor.py
    stays the only authority on the order's terminal state (CANCELLED,
    COMPLETE, partial, REJECTED) and resolves the ticket from broker
    evidence, exactly as for an order cancelled in Kite itself. An EXITING
    exit therefore keeps its position open and protected until then. If the
    broker call fails, nothing is written at all.

    PK-20: a paper order id ('PAPER-O-...') is cancelled at the paper broker
    — never sent to Kite — with exactly the same semantics: the request is
    recorded, and the order monitor resolves the dry-run ticket. A live id
    is never looked up among dry-run tickets, nor a paper id among live."""
    account = get_trading_account()
    paper = paper_broker.is_paper_id(order_id)
    conn = get_connection()
    try:
        if paper:
            broker = paper_broker.PaperBroker(conn, account["id"])
            _call(lambda: broker.cancel_order(variety=variety, order_id=order_id))
        else:
            _call(lambda: kite.cancel_order(variety=variety, order_id=order_id))

        ticket = conn.execute(
            "SELECT id FROM trade_tickets WHERE account_id = ? AND kite_order_id = ? AND mode = ?",
            (account["id"], order_id, "dry_run" if paper else "live"),
        ).fetchone()
        if ticket is not None:
            conn.execute(
                "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'modified', ?)",
                (ticket["id"], f"Cancellation requested at {'the paper broker' if paper else 'broker'} for order "
                               f"{order_id}. The broker accepting the request is not a cancellation — the order may "
                               f"still fill; the order monitor records the broker's final state."),
            )
        system_events.log_event(
            conn, account["id"], "order_cancelled_manually",
            f"cancel requested{' (paper)' if paper else ''}: order_id={order_id}, "
            f"ticket={ticket['id'] if ticket else None}",
        )
        conn.commit()
    finally:
        conn.close()

    return {"data": {
        "cancel_requested": True, "order_id": order_id, "linked_ticket_id": ticket["id"] if ticket else None,
        "message": f"Cancellation requested for order {order_id}. "
                   f"{'The paper broker' if paper else 'Kite'} decides the outcome; the linked ticket "
                   f"updates when the order monitor sees it.",
    }}


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
            "SELECT id FROM trade_tickets WHERE account_id = ? AND kite_gtt_id = ? AND mode = 'live'",
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
