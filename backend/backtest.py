import json
import sqlite3
from datetime import date, timedelta

import pandas as pd
from kiteconnect import KiteConnect

from backend import backtest_analytics, indicators, risk_engine
from backend.strategies import STRATEGIES

# Judgment calls, documented in the Phase 5 plan and restated here:
# - If a single day's range touches both the stop and target_1, the stop is
#   assumed hit first (conservative/worst-case) — daily bars can't reveal
#   the true intrabar sequence. Live trading uses real GTTs reacting to
#   actual intrabar prices, so this ambiguity only exists in the simulation.
#   This applies whether the position trails (GTT_TYPE_SINGLE) or not
#   (GTT_TYPE_OCO, hard stop+target) — same tie-break either way.
# - Trailing is evaluated once per simulated day at that day's close, not
#   continuously — the only resolution daily bars allow.
# - A module with no trailing_atr_multiple (e.g. rsi_mean_reversion) produces
#   compute_entry_levels() output with trailing_distance=None — mirroring
#   the real execution engine, this simulates a hard OCO exit (stop OR
#   target, whichever hits first) rather than a ratcheting trailing stop.
# - Position sizing uses a FIXED baseline equity (today's actual portfolio
#   value, fetched once) and today's risk_settings for every simulated
#   trade — this answers "how would this system perform with my current
#   capital," not a historically-accurate equity curve (no record of what
#   the portfolio was actually worth on any past date exists).
# - A position still open at end_date is force-closed at the final
#   available day's close so every entry resolves to a countable trade.


def _finalize_trade(position: dict, exit_price: float, exit_date: str, exit_reason: str) -> dict:
    # Cast every value to native Python types here — day['close']/day['date']
    # etc. are numpy/pandas scalars, and this is the single choke point
    # where a simulated trade becomes a plain dict for storage and the API
    # response, so nothing numpy-typed leaks past this function.
    risk_per_share = position["entry_price"] - position["stop_loss"]
    exit_price = float(exit_price)
    r_multiple = (exit_price - position["entry_price"]) / risk_per_share if risk_per_share else 0.0
    return {
        "symbol": position["symbol"],
        "entry_date": str(position["entry_date"]),
        "entry_price": float(position["entry_price"]),
        "exit_date": str(exit_date),
        "exit_price": round(exit_price, 2),
        "exit_reason": exit_reason,
        "r_multiple": round(float(r_multiple), 4),
        "outcome": "win" if r_multiple > 0 else "loss",
    }


def _step_position(module, position: dict, day: pd.Series, sub_df: pd.DataFrame, params: dict):
    """Advances one open simulated position by one day. Returns
    (exited: bool, exit_price, exit_date, exit_reason). Stop/target/trailing
    mechanics are simulated here (no live equivalent to reuse — real trades
    use Kite GTTs, not a pure function); strategy-specific exit conditions
    reuse module.evaluate_exit exactly, the same function the live daily
    scanner calls."""
    has_trailing = position["trailing_distance"] is not None

    if not has_trailing:
        # Mirrors a real GTT_TYPE_OCO order (execution.py's non-trailing
        # branch): a hard exit at either leg, stop assumed first on a
        # same-day tie (documented judgment call above).
        if day["low"] <= position["current_stop"]:
            return True, position["current_stop"], day["date"], "stop_loss"
        if day["high"] >= position["target_1"]:
            return True, position["target_1"], day["date"], "target"
    else:
        if not position["trailing_active"] and day["high"] >= position["target_1"]:
            # Matches trailing_engine._try_activate: touching target_1
            # ACTIVATES trailing, it is not itself a hard take-profit exit.
            position["trailing_active"] = True

        if position["trailing_active"]:
            candidate_stop = day["close"] - position["trailing_distance"]
            if candidate_stop > position["current_stop"]:
                position["current_stop"] = candidate_stop

        if day["low"] <= position["current_stop"]:
            reason = "trailing_stop" if position["trailing_active"] else "stop_loss"
            return True, position["current_stop"], day["date"], reason

    exit_check = module.evaluate_exit(
        {
            "entry_price": position["entry_price"], "stop_loss": position["stop_loss"],
            "target_1": position["target_1"], "executed_at": position["entry_date"],
        },
        sub_df, params,
    )
    if exit_check["passed"]:
        checklist = exit_check["checklist"]
        if checklist.get("rsi_recovery", {}).get("pass"):
            reason = "rsi_recovery"
        elif checklist.get("thesis_invalidation", {}).get("pass"):
            reason = "thesis_invalidation"
        else:
            reason = "time_exit"
        return True, day["close"], day["date"], reason

    return False, None, None, None


def _simulate_symbol(module, symbol: str, df: pd.DataFrame, params: dict, start_date: str, end_date: str) -> list[dict]:
    trades = []
    in_position = False
    position = None

    for i in range(len(df)):
        day = df.iloc[i]
        if day["date"] > end_date:
            break
        sub_df = df.iloc[: i + 1]

        if in_position:
            exited, exit_price, exit_date, exit_reason = _step_position(module, position, day, sub_df, params)
            if exited:
                trades.append(_finalize_trade(position, exit_price, exit_date, exit_reason))
                in_position = False
                position = None
            continue

        if day["date"] < start_date:
            continue  # earlier rows exist only to give indicators enough history

        result = module.evaluate_entry(sub_df, params)
        if not result["passed"]:
            continue
        levels = module.compute_entry_levels(result, params)
        if levels is None:
            continue

        position = {
            "symbol": symbol,
            "entry_date": day["date"], "entry_price": levels["entry_price"],
            "stop_loss": levels["stop_loss"], "target_1": levels["target_1"],
            "trailing_distance": levels.get("trailing_distance"),
            "current_stop": levels["stop_loss"], "trailing_active": False,
        }
        in_position = True

    if in_position:
        last = df.iloc[-1]
        trades.append(_finalize_trade(position, last["close"], last["date"], "backtest_end"))

    return trades


def _compute_core_stats(trades: list[dict], starting_equity: float, risk_amount_per_trade: float) -> dict:
    """Shared by the main backtest run and each walk-forward window — same
    win_rate/avg_r_multiple/profit_factor/max_drawdown math either way, so
    a window's stats are computed identically to the full-range stats."""
    total = len(trades)
    win_rate = None
    avg_r_multiple = None
    profit_factor = None
    max_drawdown_pct = None
    max_drawdown_amount = 0.0

    if total:
        wins = [t for t in trades if t["outcome"] == "win"]
        win_rate = len(wins) / total
        avg_r_multiple = sum(t["r_multiple"] for t in trades) / total

        gross_win = sum(t["r_multiple"] * risk_amount_per_trade for t in trades if t["r_multiple"] > 0)
        gross_loss = abs(sum(t["r_multiple"] * risk_amount_per_trade for t in trades if t["r_multiple"] < 0))
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

        equity = starting_equity
        peak = equity
        max_dd_amount = 0.0
        max_dd_pct = 0.0
        for t in trades:
            equity += t["r_multiple"] * risk_amount_per_trade
            peak = max(peak, equity)
            dd_amount = peak - equity
            max_dd_amount = max(max_dd_amount, dd_amount)
            if peak > 0:
                max_dd_pct = max(max_dd_pct, dd_amount / peak)
        max_drawdown_pct = max_dd_pct * 100
        max_drawdown_amount = max_dd_amount

    return {
        "total_signals": total, "win_rate": win_rate, "avg_r_multiple": avg_r_multiple,
        "profit_factor": profit_factor, "max_drawdown_pct": max_drawdown_pct,
        "max_drawdown_amount": max_drawdown_amount,
    }


def _sequential_windows(start_date: str, end_date: str, window_days: int) -> list[tuple[str, str]]:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    windows = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=window_days - 1), end)
        windows.append((cursor.isoformat(), window_end.isoformat()))
        cursor = window_end + timedelta(days=1)
    return windows


def _run_walk_forward_windows(
    conn: sqlite3.Connection, run_id: int, module, dfs_by_symbol: dict, params: dict,
    start_date: str, end_date: str, window_days: int, starting_equity: float, risk_amount_per_trade: float,
) -> list[dict]:
    """Consistency-check windows (Phase 8 Part D.2) — explicitly NOT
    walk-forward optimization: no parameter refitting occurs anywhere.
    Each window reuses the identical per-symbol simulation function on its
    own date sub-range, so the only thing varying between windows is which
    calendar period is being measured."""
    windows_out = []
    for i, (w_start, w_end) in enumerate(_sequential_windows(start_date, end_date, window_days)):
        window_trades: list[dict] = []
        for symbol, df in dfs_by_symbol.items():
            window_trades.extend(_simulate_symbol(module, symbol, df, params, w_start, w_end))
        window_trades.sort(key=lambda t: t["entry_date"])
        stats = _compute_core_stats(window_trades, starting_equity, risk_amount_per_trade)

        conn.execute(
            """
            INSERT INTO walk_forward_windows (backtest_run_id, window_index, window_start, window_end, stats)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, i, w_start, w_end, json.dumps(stats)),
        )
        windows_out.append({"window_index": i, "window_start": w_start, "window_end": w_end, "stats": stats})
    return windows_out


def run_backtest(
    conn: sqlite3.Connection,
    account: sqlite3.Row,
    kite: KiteConnect,
    strategy_name: str,
    symbols: list[tuple[int, str]],
    params: dict,
    start_date: str,
    end_date: str,
    run_walk_forward: bool = False,
    window_days: int = 90,
    run_monte_carlo: bool = False,
    mc_iterations: int = 1000,
    mc_random_seed: int | None = None,
) -> dict:
    """Read-only against ohlc_daily — never touches trade_tickets or any
    live-pipeline table. `symbols` is a list of (instrument_token, symbol)
    pairs already resolved by the caller (the route defaults this to the
    current watchlist when none are given). Calls the EXACT SAME
    evaluate_entry/evaluate_exit/compute_entry_levels functions the live
    scanner uses for `strategy_name`, looked up from the shared registry —
    never a separate/parallel implementation per strategy."""
    module = STRATEGIES[strategy_name]

    dfs_by_symbol: dict[str, pd.DataFrame] = {}
    all_trades: list[dict] = []
    for instrument_token, symbol in symbols:
        df = indicators.load_ohlc_frame(instrument_token)
        if len(df) < 2:
            continue
        dfs_by_symbol[symbol] = df
        all_trades.extend(_simulate_symbol(module, symbol, df, params, start_date, end_date))

    all_trades.sort(key=lambda t: t["entry_date"])

    settings = risk_engine.get_settings(conn, account["id"])
    ctx = risk_engine.build_portfolio_context(kite)  # RiskCheckUnavailableError propagates to the caller
    starting_equity = ctx.portfolio_value
    risk_amount_per_trade = starting_equity * settings["max_risk_per_trade_pct"] / 100

    core_stats = _compute_core_stats(all_trades, starting_equity, risk_amount_per_trade)
    rigor_metrics = backtest_analytics.compute_rigor_metrics(
        all_trades, start_date, end_date, risk_amount_per_trade, core_stats["max_drawdown_amount"],
    )
    mc_summary = (
        backtest_analytics.run_monte_carlo(all_trades, starting_equity, risk_amount_per_trade, mc_iterations, mc_random_seed)
        if run_monte_carlo else
        {"mc_iterations": None, "mc_median_drawdown_pct": None, "mc_p95_drawdown_pct": None,
         "mc_p5_final_equity": None, "mc_p95_final_equity": None}
    )

    cursor = conn.execute(
        """
        INSERT INTO backtest_runs (
            strategy_name, params_snapshot, start_date, end_date, symbols_tested,
            total_signals, win_rate, avg_r_multiple, profit_factor, max_drawdown_pct, max_drawdown_amount,
            sharpe_ratio, sortino_ratio, max_consecutive_losses, recovery_factor, avg_win, avg_loss,
            time_in_market_pct, mc_iterations, mc_median_drawdown_pct, mc_p95_drawdown_pct,
            mc_p5_final_equity, mc_p95_final_equity
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            strategy_name, json.dumps(params), start_date, end_date, json.dumps([s for _, s in symbols]),
            core_stats["total_signals"], core_stats["win_rate"], core_stats["avg_r_multiple"],
            core_stats["profit_factor"], core_stats["max_drawdown_pct"], core_stats["max_drawdown_amount"],
            rigor_metrics["sharpe_ratio"], rigor_metrics["sortino_ratio"], rigor_metrics["max_consecutive_losses"],
            rigor_metrics["recovery_factor"], rigor_metrics["avg_win"], rigor_metrics["avg_loss"],
            rigor_metrics["time_in_market_pct"], mc_summary["mc_iterations"], mc_summary["mc_median_drawdown_pct"],
            mc_summary["mc_p95_drawdown_pct"], mc_summary["mc_p5_final_equity"], mc_summary["mc_p95_final_equity"],
        ),
    )
    run_id = cursor.lastrowid
    conn.executemany(
        """
        INSERT INTO backtest_trades (
            backtest_run_id, symbol, entry_date, entry_price, exit_date, exit_price, exit_reason, r_multiple, outcome
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (run_id, t["symbol"], t["entry_date"], t["entry_price"], t["exit_date"], t["exit_price"],
             t["exit_reason"], t["r_multiple"], t["outcome"])
            for t in all_trades
        ],
    )

    walk_forward_windows = (
        _run_walk_forward_windows(
            conn, run_id, module, dfs_by_symbol, params, start_date, end_date,
            window_days, starting_equity, risk_amount_per_trade,
        )
        if run_walk_forward else []
    )
    conn.commit()

    return {
        "id": run_id, "strategy_name": strategy_name, "start_date": start_date, "end_date": end_date,
        "symbols_tested": [s for _, s in symbols],
        "starting_equity": starting_equity, "risk_amount_per_trade": risk_amount_per_trade,
        "trades": all_trades,
        **core_stats, **rigor_metrics, **mc_summary,
        "walk_forward_windows": walk_forward_windows,
    }


def get_backtest(conn: sqlite3.Connection, run_id: int) -> dict | None:
    run = conn.execute("SELECT * FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        return None
    trades = conn.execute(
        "SELECT * FROM backtest_trades WHERE backtest_run_id = ? ORDER BY entry_date", (run_id,)
    ).fetchall()
    windows = conn.execute(
        "SELECT * FROM walk_forward_windows WHERE backtest_run_id = ? ORDER BY window_index", (run_id,)
    ).fetchall()
    data = dict(run)
    data["params_snapshot"] = json.loads(data["params_snapshot"])
    data["symbols_tested"] = json.loads(data["symbols_tested"])
    data["trades"] = [dict(t) for t in trades]
    data["walk_forward_windows"] = [
        {**dict(w), "stats": json.loads(w["stats"])} for w in windows
    ]
    return data


def list_backtests(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM backtest_runs ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["params_snapshot"] = json.loads(d["params_snapshot"])
        d["symbols_tested"] = json.loads(d["symbols_tested"])
        out.append(d)
    return out
