import json
import logging
import sqlite3
from datetime import date, datetime

from backend import execution, position_quantity, risk_engine, trading_calendar

logger = logging.getLogger(__name__)

SAMPLE_SIZE_WARNING_THRESHOLD = 20


def _exit_reason_from_checklist(checklist: dict | None) -> str:
    if not checklist:
        return "manual"
    if "gtt_auto_trigger" in checklist:
        # Synthesized by trailing_engine.py when a GTT disappears — the
        # engine already determined which level was closer to the fill.
        return checklist["gtt_auto_trigger"].get("closer_to", "stop_loss")
    if checklist.get("emergency_exit_all"):
        return "emergency_exit"
    if checklist.get("rsi_recovery", {}).get("pass"):
        return "rsi_recovery"
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
    if sell["entry_fill_status"] not in (None, "filled"):
        # PK-8: an accepted, partial or unconfirmed exit has not closed
        # anything — journaling it would record a trade that may never happen.
        raise ValueError(
            f"Exit ticket {sell_ticket_id} is not filled (entry_fill_status={sell['entry_fill_status']!r}) — "
            f"no closed trade to record yet"
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
    # PK-13: one row per ROUND TRIP, written once the position is flat —
    # never while shares remain (a partial exit is not a closed trade).
    if buy["closed_at"] is None:
        raise ValueError(f"Position {buy['id']} is still open — no closed trade to record yet")
    if conn.execute("SELECT id FROM closed_trades WHERE buy_ticket_id = ?", (buy["id"],)).fetchone() is not None:
        return None

    # The broker's actual fills, as order_monitor.py recorded them before
    # calling this (PK-8: a live exit is only journaled once filled); the
    # tickets' reference prices only for a dry run or a pre-fill-tracking row.
    entry_price = buy["filled_price"] or buy["entry_price"]
    # PK-13: the quantity actually bought, and every exit that sold part of
    # it — a position sold in pieces (a partial exit, then another; a GTT
    # that part-filled) is one trade at the quantity-weighted average exit.
    bought = position_quantity.filled(buy)
    legs = []
    for leg in conn.execute(
        "SELECT * FROM trade_tickets WHERE closes_ticket_id = ? AND status = 'executed' ORDER BY id", (buy["id"],)
    ).fetchall():
        sold = position_quantity.filled(leg)
        if sold is None:
            raise ValueError(f"Exit ticket {leg['id']} of position {buy['id']} has an unknown filled quantity")
        if sold:
            legs.append((sold, leg["filled_price"] or leg["entry_price"]))
    quantity = sum(q for q, _ in legs)
    if bought is None or quantity != bought:
        raise ValueError(f"Position {buy['id']} bought {bought} but its exits sold {quantity} — "
                         f"refusing to journal a mismatched round trip")
    exit_price = legs[0][1] if len(legs) == 1 else sum(q * p for q, p in legs) / quantity
    # Long-only today: every closes_ticket_id currently links a SELL exit to
    # a BUY entry (Phase 5's exit scanner only ever closes BUY positions).
    # This still branches on buy's transaction_type so a future short-closing
    # path wouldn't silently compute the wrong sign.
    if buy["transaction_type"] == "BUY":
        realized_pnl = (exit_price - entry_price) * quantity
    else:
        realized_pnl = (entry_price - exit_price) * quantity
    realized_pnl_pct = (realized_pnl / (entry_price * quantity) * 100) if entry_price and quantity else 0.0
    # The ticket's risk_amount was sized for the requested quantity; the
    # trade only ever risked what actually filled.
    risk_amount = buy["risk_amount"]
    if risk_amount and quantity != buy["quantity"]:
        risk_amount = risk_amount * quantity / buy["quantity"]
    r_multiple_realized = (realized_pnl / risk_amount) if risk_amount else None

    entry_date = (buy["executed_at"] or buy["created_at"])[:10]
    # The fill, not the order: an AMO exit placed tonight fills tomorrow.
    exit_date = (sell["filled_at"] or sell["executed_at"] or sell["created_at"])[:10]
    holding_calendar_days = (datetime.fromisoformat(exit_date) - datetime.fromisoformat(entry_date)).days
    # NSE sessions in [entry, exit) — numpy.busday_count's convention, but
    # holiday-aware (PK-22C). Rows journaled before PK-22 keep their value.
    holding_trading_days = trading_calendar.trading_days_between(
        date.fromisoformat(entry_date), date.fromisoformat(exit_date)
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


def close_position_for_exit(conn: sqlite3.Connection, sell_ticket_id: int, closes_ticket_id: int,
                            closed_at: str) -> bool:
    """Closes the original position of an exit whose full fill is PROVEN
    (PK-8): the broker reported it COMPLETE (order_monitor.py) or it is a
    dry-run simulation (routes/tickets.py). Never call it for an order that
    was merely accepted. Stamps closed_at with the fill time — conditionally,
    so a position closed already keeps its first closure — supersedes the
    position's other pending exits (PK-4), and writes the closed_trades row
    exactly once (idx_closed_trades_sell_ticket). A row written before PK-8
    at the reference price is re-priced at the fill instead. Commits.
    Returns whether this call stamped closed_at."""
    closed_now = conn.execute(
        "UPDATE trade_tickets SET closed_at = ? WHERE id = ? AND closed_at IS NULL",
        (closed_at, closes_ticket_id),
    ).rowcount == 1
    execution.supersede_sibling_exits(conn, closes_ticket_id, sell_ticket_id)
    conn.commit()
    try:
        if record_closed_trade(conn, sell_ticket_id) is None:
            sell = conn.execute("SELECT filled_price FROM trade_tickets WHERE id = ?", (sell_ticket_id,)).fetchone()
            apply_exit_fill(conn, sell_ticket_id, sell["filled_price"])
            conn.commit()
    except ValueError:
        logger.exception("Failed to record closed_trades for ticket %d", sell_ticket_id)
    return closed_now


def apply_exit_fill(conn: sqlite3.Connection, sell_ticket_id: int, fill_price: float) -> None:
    """Re-prices a closed_trades row with the broker's actual exit fill.
    Only rows written before PK-8 need it — they were written when the exit
    was placed, at the reference price. Caller commits."""
    row = conn.execute("SELECT * FROM closed_trades WHERE sell_ticket_id = ?", (sell_ticket_id,)).fetchone()
    if row is None or not fill_price:
        return
    buy = conn.execute("SELECT transaction_type FROM trade_tickets WHERE id = ?", (row["buy_ticket_id"],)).fetchone()
    quantity, entry_price = row["quantity"], row["entry_price"]
    if buy is None or buy["transaction_type"] == "BUY":
        realized_pnl = (fill_price - entry_price) * quantity
    else:
        realized_pnl = (entry_price - fill_price) * quantity
    realized_pnl_pct = (realized_pnl / (entry_price * quantity) * 100) if entry_price and quantity else 0.0
    r_multiple = (realized_pnl / row["risk_amount"]) if row["risk_amount"] else None
    conn.execute(
        "UPDATE closed_trades SET exit_price = ?, realized_pnl = ?, realized_pnl_pct = ?, r_multiple_realized = ? "
        "WHERE id = ?",
        (fill_price, realized_pnl, realized_pnl_pct, r_multiple, row["id"]),
    )


def backfill_closed_trades(conn: sqlite3.Connection) -> int:
    """One-time catch-up for exit tickets that executed before this phase
    existed (or any that somehow got missed). Safe to call on every
    startup — record_closed_trade is idempotent per sell_ticket_id. Only
    exits whose fill is proven: an EXITING exit found at startup is left to
    order_monitor, which journals it when the broker reports the fill (PK-8).
    Only positions that are closed, once each (PK-13): a filled exit that
    sold part of a position still holding shares is not a closed trade; of
    several exits of one closed position, the last is its closing exit."""
    rows = conn.execute(
        f"""
        SELECT id FROM trade_tickets
        WHERE transaction_type = 'SELL' AND closes_ticket_id IS NOT NULL AND status = 'executed'
          AND {execution.EXIT_FILL_PROVEN_SQL}
          AND closes_ticket_id IN (SELECT id FROM trade_tickets WHERE closed_at IS NOT NULL)
          AND id NOT IN (SELECT sell_ticket_id FROM closed_trades)
          AND closes_ticket_id NOT IN (SELECT buy_ticket_id FROM closed_trades)
        ORDER BY id DESC
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
