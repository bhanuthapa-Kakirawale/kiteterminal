import json
import sqlite3

from backend import config, indicators
from backend.watchlist import list_watchlist


def _breakout_rank(rule_checklist: dict | None) -> float:
    """Recomputes the same breakout-strength ranking key Phase 5's
    run_entry_scan used to pick winners when max_signals_per_day capped
    results — the rank itself isn't persisted separately, so this reads it
    back out of the already-parsed rule_checklist."""
    if not rule_checklist:
        return 0.0
    return rule_checklist.get("breakout", {}).get("breakout_pct_above_level") or 0.0


def _parse_ticket(row: sqlite3.Row) -> dict:
    t = dict(row)
    t["rule_checklist"] = json.loads(t["rule_checklist"]) if t["rule_checklist"] else None
    return t


def get_buy_candidates(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM trade_tickets
        WHERE account_id = ? AND source = 'strategy' AND transaction_type = 'BUY'
          AND status = 'pending_confirmation'
        """,
        (account_id,),
    ).fetchall()
    candidates = [_parse_ticket(r) for r in rows]
    candidates.sort(key=lambda t: _breakout_rank(t["rule_checklist"]), reverse=True)
    return candidates


def get_exit_candidates(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM trade_tickets
        WHERE account_id = ? AND source = 'strategy' AND transaction_type = 'SELL'
          AND status = 'pending_confirmation'
        """,
        (account_id,),
    ).fetchall()
    return [_parse_ticket(r) for r in rows]


def get_pending_adjustments(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT sap.*, t.symbol FROM stop_adjustment_proposals sap
        JOIN trade_tickets t ON t.id = sap.ticket_id
        WHERE t.account_id = ? AND sap.status = 'pending'
        ORDER BY sap.created_at DESC
        """,
        (account_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_watch_list(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    """Watchlist symbols within config.WATCH_PROXIMITY_PCT below
    donchian_high_20 and already passing the trend filter — informational
    only, built from the existing indicator_snapshot cache (plus the
    latest cached close, for "how close" math), no new indicators computed
    and no tickets generated. This view is explicitly a PRE-MARKET
    briefing, so the last cached close (not a live LTP, which wouldn't
    exist before market open anyway) is the right price to use here."""
    watch = []
    for w in list_watchlist(account_id):
        donchian = w["donchian_high_20"]
        sma_200 = w["sma_200"]
        if donchian is None or sma_200 is None:
            continue  # insufficient history to evaluate either condition yet

        df = indicators.load_ohlc_frame(w["instrument_token"])
        if df.empty:
            continue
        last_close = float(df["close"].iloc[-1])

        if last_close <= sma_200:
            continue  # trend filter not passing
        distance_pct = (last_close - donchian) / donchian * 100 if donchian else None
        if distance_pct is None or distance_pct >= 0:
            continue  # already at/through the level — that's a signal, not a "watch"
        if distance_pct < -config.WATCH_PROXIMITY_PCT:
            continue  # too far below to be "close"

        watch.append({
            "symbol": w["symbol"], "instrument_token": w["instrument_token"],
            "last_close": last_close, "donchian_high_20": donchian,
            "distance_pct": distance_pct, "sma_200": sma_200,
        })
    watch.sort(key=lambda x: x["distance_pct"], reverse=True)  # closest to breaking out first
    return watch


def get_todays_actions(conn: sqlite3.Connection, account_id: int) -> dict:
    buy_candidates = get_buy_candidates(conn, account_id)
    exit_candidates = get_exit_candidates(conn, account_id)
    pending_adjustments = get_pending_adjustments(conn, account_id)
    watch_list = get_watch_list(conn, account_id)
    total_watchlist = len(list_watchlist(account_id))
    no_action_count = max(total_watchlist - len(watch_list), 0)

    return {
        "buy_candidates": buy_candidates,
        "exit_candidates": exit_candidates,
        "pending_adjustments": pending_adjustments,
        "watch_list": watch_list,
        "no_action_count": no_action_count,
        "total_watchlist": total_watchlist,
    }
