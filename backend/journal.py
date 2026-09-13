import json
import logging
import sqlite3
from datetime import datetime

import numpy as np

from backend import risk_engine

logger = logging.getLogger(__name__)

SAMPLE_SIZE_WARNING_THRESHOLD = 20


def _exit_reason_from_checklist(checklist: dict | None) -> str:
    if not checklist:
        return "manual"
    if "gtt_auto_trigger" in checklist:
        # Synthesized by trailing_engine.py when a GTT disappears — the
        # engine already determined which level was closer to the fill.
        return checklist["gtt_auto_trigger"].get("closer_to", "stop_loss")
    if "thesis_invalidation" in checklist or "time_exit" in checklist:
        # Phase 5's evaluate_exit checks thesis_invalidation first, so it
        # wins if both conditions happened to be true simultaneously.
        if checklist.get("thesis_invalidation", {}).get("pass"):
            return "thesis_invalidation"
        if checklist.get("time_exit", {}).get("pass"):
            return "time_exit"
    return "manual"


def record_closed_trade(conn: sqlite3.Connection, sell_ticket_id: int) -> int | None:
    """Computes and stores exactly one closed_trades row for a SELL ticket
    that has just transitioned to status='executed' with a non-null
    closes_ticket_id. Called from both routes/tickets.py (explicit exit
    tickets) and trailing_engine.py (synthesized GTT-auto-trigger tickets)
    — one computation path either way, per this phase's "single source of
    truth" requirement. Idempotent: returns None (no-op) if a row for this
    sell_ticket already exists, so it's safe to call more than once."""
    sell = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (sell_ticket_id,)).fetchone()
    if sell is None:
        raise ValueError(f"Sell ticket {sell_ticket_id} not found")
    if sell["transaction_type"] != "SELL" or sell["closes_ticket_id"] is None or sell["status"] != "executed":
        raise ValueError(
            f"Ticket {sell_ticket_id} is not an executed exit ticket (transaction_type="
            f"{sell['transaction_type']}, closes_ticket_id={sell['closes_ticket_id']}, status={sell['status']})"
        )

    if conn.execute(
        "SELECT id FROM closed_trades WHERE sell_ticket_id = ?", (sell_ticket_id,)
    ).fetchone() is not None:
        return None

    buy = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (sell["closes_ticket_id"],)).fetchone()
    if buy is None:
        raise ValueError(f"closes_ticket_id {sell['closes_ticket_id']} on ticket {sell_ticket_id} does not exist")
    if buy["transaction_type"] != "BUY":
        raise ValueError(f"Ticket {buy['id']} referenced by closes_ticket_id is not a BUY ticket")
    if buy["mode"] != sell["mode"]:
        # Not something to paper over — a buy/sell mode mismatch means a
        # dry-run position somehow got a live exit (or vice versa), which
        # points at a bug elsewhere in the execution pipeline.
        raise ValueError(
            f"Mode mismatch closing ticket {buy['id']}: buy mode={buy['mode']!r}, sell mode={sell['mode']!r} "
            f"— refusing to silently pick one."
        )

    entry_price = buy["entry_price"]
    exit_price = sell["entry_price"]
    quantity = buy["quantity"]
    # Long-only today: every closes_ticket_id currently links a SELL exit to
    # a BUY entry (Phase 5's exit scanner only ever closes BUY positions).
    # This still branches on buy's transaction_type so a future short-closing
    # path wouldn't silently compute the wrong sign.
    if buy["transaction_type"] == "BUY":
        realized_pnl = (exit_price - entry_price) * quantity
    else:
        realized_pnl = (entry_price - exit_price) * quantity
    realized_pnl_pct = (realized_pnl / (entry_price * quantity) * 100) if entry_price and quantity else 0.0
    risk_amount = buy["risk_amount"]
    r_multiple_realized = (realized_pnl / risk_amount) if risk_amount else None

    entry_date = (buy["executed_at"] or buy["created_at"])[:10]
    exit_date = (sell["executed_at"] or sell["created_at"])[:10]
    holding_calendar_days = (datetime.fromisoformat(exit_date) - datetime.fromisoformat(entry_date)).days
    holding_trading_days = (
        int(np.busday_count(entry_date, exit_date)) if exit_date >= entry_date else 0
    )

    checklist = json.loads(sell["rule_checklist"]) if sell["rule_checklist"] else None
    exit_reason = _exit_reason_from_checklist(checklist)

    cursor = conn.execute(
        """
        INSERT INTO closed_trades (
            buy_ticket_id, sell_ticket_id, account_id, symbol, strategy_name, strategy_version, source, mode,
            entry_price, exit_price, quantity, realized_pnl, realized_pnl_pct,
            risk_amount, r_multiple_realized, entry_date, exit_date,
            holding_period_calendar_days, holding_period_trading_days, exit_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            buy["id"], sell["id"], sell["account_id"], sell["symbol"], buy["strategy_name"], buy["strategy_version"],
            buy["source"], sell["mode"],
            entry_price, exit_price, quantity, realized_pnl, realized_pnl_pct,
            risk_amount, r_multiple_realized, entry_date, exit_date,
            holding_calendar_days, holding_trading_days, exit_reason,
        ),
    )
    conn.commit()
    logger.info(
        "Recorded closed trade: %s qty=%d entry=%.2f exit=%.2f pnl=%.2f (%s, %s)",
        sell["symbol"], quantity, entry_price, exit_price, realized_pnl, sell["mode"], exit_reason,
    )
    return cursor.lastrowid


def backfill_closed_trades(conn: sqlite3.Connection) -> int:
    """One-time catch-up for exit tickets that executed before this phase
    existed (or any that somehow got missed). Safe to call on every
    startup — record_closed_trade is idempotent per sell_ticket_id."""
    rows = conn.execute(
        """
        SELECT id FROM trade_tickets
        WHERE transaction_type = 'SELL' AND closes_ticket_id IS NOT NULL AND status = 'executed'
          AND id NOT IN (SELECT sell_ticket_id FROM closed_trades)
        """
    ).fetchall()
    count = 0
    for row in rows:
        try:
            if record_closed_trade(conn, row["id"]) is not None:
                count += 1
        except ValueError:
            logger.exception("Skipping backfill for sell ticket %d", row["id"])
    if count:
        logger.info("Backfilled %d closed_trades row(s)", count)
    return count


# --------------------------------------------------------------------------
# Part B — analytics
# --------------------------------------------------------------------------

def get_starting_capital(conn: sqlite3.Connection, account: sqlite3.Row, kite) -> float:
    settings = risk_engine.get_settings(conn, account["id"])
    if settings["starting_capital"] is not None:
        return settings["starting_capital"]

    first_trade = conn.execute(
        "SELECT id FROM closed_trades WHERE account_id = ? ORDER BY exit_date, id LIMIT 1", (account["id"],)
    ).fetchone()
    if first_trade is None:
        return 0.0
    # Best-effort fallback: no historical portfolio-value series exists, so
    # this uses TODAY's portfolio value as a stand-in for "at the time of
    # the first closed trade" — an approximation, not a historically
    # accurate figure (same honest caveat as Phase 5's backtest baseline).
    try:
        ctx = risk_engine.build_portfolio_context(kite)
        return ctx.portfolio_value
    except risk_engine.RiskCheckUnavailableError:
        return 0.0


def _stats_for_group(trades: list[dict]) -> dict:
    total = len(trades)
    if total == 0:
        return {
            "trade_count": 0, "win_rate": None, "expectancy_r": None,
            "profit_factor": None, "total_realized_pnl": 0.0,
        }
    wins = [t for t in trades if t["realized_pnl"] > 0]
    r_values = [t["r_multiple_realized"] for t in trades if t["r_multiple_realized"] is not None]
    gross_profit = sum(t["realized_pnl"] for t in trades if t["realized_pnl"] > 0)
    gross_loss = sum(t["realized_pnl"] for t in trades if t["realized_pnl"] < 0)
    return {
        "trade_count": total,
        "win_rate": len(wins) / total,
        "expectancy_r": (sum(r_values) / len(r_values)) if r_values else None,
        "profit_factor": (gross_profit / abs(gross_loss)) if gross_loss < 0 else None,
        "total_realized_pnl": sum(t["realized_pnl"] for t in trades),
    }


def _equity_curve_and_drawdown(trades: list[dict], starting_capital: float) -> tuple[list[dict], float, float]:
    equity = starting_capital
    curve = [{"date": None, "equity": equity}]
    peak = equity
    max_dd_amount = 0.0
    max_dd_pct = 0.0
    for t in trades:
        equity += t["realized_pnl"]
        curve.append({"date": t["exit_date"], "equity": equity})
        peak = max(peak, equity)
        dd_amount = peak - equity
        dd_pct = (dd_amount / peak * 100) if peak > 0 else 0.0
        max_dd_amount = max(max_dd_amount, dd_amount)
        max_dd_pct = max(max_dd_pct, dd_pct)
    return curve, max_dd_amount, max_dd_pct


def compute_stats(conn: sqlite3.Connection, account_id: int, mode: str, starting_capital: float) -> dict:
    """Every figure here is filtered by `mode` first (dry_run XOR live) —
    no code path in this function ever mixes the two."""
    rows = conn.execute(
        "SELECT * FROM closed_trades WHERE account_id = ? AND mode = ? ORDER BY exit_date, id",
        (account_id, mode),
    ).fetchall()
    trades = [dict(r) for r in rows]
    total = len(trades)

    overall = _stats_for_group(trades)
    equity_curve, max_dd_amount, max_dd_pct = _equity_curve_and_drawdown(trades, starting_capital)
    overall["max_drawdown_amount"] = max_dd_amount
    overall["max_drawdown_pct"] = max_dd_pct
    overall["avg_holding_period_calendar_days"] = (
        sum(t["holding_period_calendar_days"] for t in trades) / total if total else None
    )
    overall["starting_capital"] = starting_capital
    overall["equity_curve"] = equity_curve

    by_strategy: dict[str, list[dict]] = {}
    by_symbol: dict[str, list[dict]] = {}
    by_strategy_version: dict[tuple[str, int | None], list[dict]] = {}
    for t in trades:
        by_strategy.setdefault(t["strategy_name"] or "manual", []).append(t)
        by_symbol.setdefault(t["symbol"], []).append(t)
        by_strategy_version.setdefault((t["strategy_name"] or "manual", t["strategy_version"]), []).append(t)

    strategy_breakdown = [
        {"strategy_name": name, **_stats_for_group(ts)} for name, ts in by_strategy.items()
    ]
    symbol_breakdown = sorted(
        ({"symbol": sym, **_stats_for_group(ts)} for sym, ts in by_symbol.items()),
        key=lambda s: s["total_realized_pnl"], reverse=True,
    )
    # Phase 8: lets "Momentum Breakout v1 vs v2" be compared directly
    # rather than blended into one strategy-level number — additive,
    # by_strategy/by_symbol above are unchanged.
    strategy_version_breakdown = sorted(
        (
            {"strategy_name": name, "strategy_version": version, **_stats_for_group(ts)}
            for (name, version), ts in by_strategy_version.items()
        ),
        key=lambda s: (s["strategy_name"], s["strategy_version"] or 0),
    )

    return {
        "mode": mode,
        "overall": overall,
        "by_strategy": strategy_breakdown,
        "by_symbol": symbol_breakdown,
        "by_strategy_version": strategy_version_breakdown,
        "sample_size_warning": total < SAMPLE_SIZE_WARNING_THRESHOLD,
    }
