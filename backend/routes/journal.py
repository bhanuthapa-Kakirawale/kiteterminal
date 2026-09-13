from fastapi import APIRouter, Depends, Query
from kiteconnect import KiteConnect

from backend import journal, todays_actions
from backend.accounts import get_trading_account
from backend.db import get_connection
from backend.kite_client import require_trading_account

router = APIRouter()


@router.get("/api/todays-actions")
def get_todays_actions(kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        return {"data": todays_actions.get_todays_actions(conn, account["id"])}
    finally:
        conn.close()


@router.get("/api/journal/mode-summary")
def mode_summary(kite: KiteConnect = Depends(require_trading_account)):
    # Lets the frontend pick a sensible default mode tab (whichever has
    # more closed trades, or live if any exist) without guessing blind.
    account = get_trading_account()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT mode, COUNT(*) AS n FROM closed_trades WHERE account_id = ? GROUP BY mode",
            (account["id"],),
        ).fetchall()
        counts = {"dry_run": 0, "live": 0}
        for r in rows:
            counts[r["mode"]] = r["n"]
        return {"data": counts}
    finally:
        conn.close()


@router.get("/api/journal/stats")
def get_stats(mode: str = Query(pattern="^(dry_run|live)$"), kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        starting_capital = journal.get_starting_capital(conn, account, kite)
        return {"data": journal.compute_stats(conn, account["id"], mode, starting_capital)}
    finally:
        conn.close()
