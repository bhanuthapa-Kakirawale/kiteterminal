"""Historical backtester (PK-21: execution-realistic).

A portfolio-level, event-driven daily simulation. It reuses the EXACT live
evaluate_entry / evaluate_exit / compute_entry_levels / rank_key functions
for the strategy under test, and models only two things on top of them:
what was known at each bar's close, and what price could actually have been
traded next. Read-only against ohlc_daily; never touches trade_tickets, any
live-pipeline table, or the broker (no Kite call is made at all).

EXECUTION MODEL — per trading date D, in this order:

1. At D's OPEN, sells first:
   - a protective stop the bar GAPS through (open <= stop) fills at the
     OPEN, not at the stale stop price;
   - an OCO target the bar gaps over (open >= target) fills at the open;
   - a close-based exit decided at the PREVIOUS close (EMA/thesis, time,
     RSI) fills at the open.
   Their proceeds are available to buys at the same open.
2. At D's OPEN, pending entries from signals at the previous close:
   - "next_open": a market order at D's actual open (+ entry slippage), so
     a gap-up fills at the real open, not at the planned price;
   - "next_session_limit": the live ticket's DAY LIMIT at the planned
     entry price. Open <= limit fills at the (slipped, capped) open; else
     low <= limit fills at the limit; else the signal is unfilled.
   The fill plus buy charges must fit in cash, or the signal is skipped.
3. INTRABAR protective orders, with the stop that was in force BEFORE D:
   low <= stop fills at the stop (- slippage); an OCO position's
   high >= target fills at the target (- slippage). If both are inside the
   same bar the STOP is assumed first — daily bars cannot reveal intrabar
   order, so the ambiguity is always resolved against the trade.
4. At D's CLOSE (effective from D+1 only — never tested against D's own
   low): trailing activation (high >= target_1) and the ratchet
   (close - trailing_distance, rounded down to the tick like the live
   ratchet); optional breakeven; and module.evaluate_exit, which, if it
   fires, schedules a sell at the NEXT session's open.
5. Mark to market: equity = cash + open positions at their latest close.
6. New signals from D's close, ranked deterministically by
   (-module.rank_key, symbol), capped at the strategy's
   max_signals_per_day (as live), then limited by max_open_positions and
   sized with the live risk_engine.compute_position_size on the simulated
   equity. A signal on a symbol's final bar in range has no next session
   and is recorded as unexecuted — never invented as a trade.

Positions still open at end_date are closed at the last bar <= end_date
(slippage and sell charges applied, exit_reason "backtest_end"). No row
after end_date is ever loaded (indicators.load_ohlc_frame filters in SQL),
and simulate() refuses frames that contain one.

Judgment calls (also in docs/OPUS55_PK21_IMPLEMENTATION_REPORT.md):
- Stop and target are the fixed absolute levels computed from the signal
  close and snapped to the tick, exactly as the live ticket and its GTT use
  them. They are not re-anchored to the actual fill.
- Slippage is applied ONCE, to whichever reference price fills (the open on
  a gap, the trigger level intrabar), never stacked.
- A favourable OCO target gap fills at the open (the better price); an
  intrabar target fills at target - slippage, which is conservative
  against a live LIMIT target leg.
- A symbol that was in a position at any time on D produces no new signal
  on D (one position per symbol, as live and as before PK-21).
- Historical orders fill in full: daily OHLC carries no information about
  partial fills, so none are invented (PK-13 stays a live/paper concern).
"""

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import pandas as pd

from backend import (
    backtest_analytics, backtest_costs, config, indicators, instruments, market, ohlc_integrity, risk_engine,
)
from backend.paper_broker import fill_price
from backend.strategies import STRATEGIES

ENTRY_MODELS = ("next_open", "next_session_limit")
ENTRY_MODEL_LABELS = {
    "next_open": "Next-session open (market order)",
    "next_session_limit": "Next-session DAY LIMIT at the planned entry price",
}
SAME_BAR_POLICY = "stop_first"

UNIVERSE_WARNINGS = {
    "current_watchlist": (
        "Current watchlist used — survivorship bias possible. Symbols that were delisted, "
        "suspended or dropped from the watchlist are missing, so results are likely optimistic."
    ),
    "user_supplied": (
        "User-supplied symbol list — not verified as a point-in-time universe; survivorship "
        "bias depends on how the list was chosen."
    ),
}
UNIVERSE_BIAS = {"current_watchlist": "likely", "user_supplied": "unverified"}


class FutureDataError(ValueError):
    """A frame handed to simulate() contains a row after end_date."""


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float
    risk_per_trade_pct: float
    capital_per_trade_pct: float
    max_open_positions: int
    entry_slippage_bps: float
    exit_slippage_bps: float
    costs_enabled: bool
    entry_model: str
    simulate_breakeven: bool
    cost_schedule: dict = field(default_factory=backtest_costs.default_schedule)

    def __post_init__(self):
        if self.entry_model not in ENTRY_MODELS:
            raise ValueError(f"entry_model must be one of {ENTRY_MODELS}, not {self.entry_model!r}")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be at least 1")

    @classmethod
    def from_overrides(cls, **overrides) -> "BacktestConfig":
        """config.BACKTEST_DEFAULTS, with every non-None override applied."""
        values = dict(config.BACKTEST_DEFAULTS)
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    def snapshot(self) -> dict:
        return {
            **asdict(self),
            "entry_model_label": ENTRY_MODEL_LABELS[self.entry_model],
            "same_bar_policy": SAME_BAR_POLICY,
            "exit_timing": "close-based exits fill at the next session open",
            "trailing_timing": "trailing/breakeven stops computed at the close, active from the next session",
            "gap_policy": "a stop the open gaps through fills at the open; a favourable target gap fills at the open",
            "partial_fills": "not modelled — historical orders fill in full",
        }


class SymbolFrame:
    """One symbol's OHLC frame plus fast per-bar lookups."""

    def __init__(self, symbol: str, df: pd.DataFrame, tick_size: float = market.DEFAULT_TICK_SIZE):
        df = df.reset_index(drop=True).copy()
        df["date"] = df["date"].astype(str).str[:10]
        self.symbol = symbol
        self.df = df
        self.tick = tick_size
        self.dates = df["date"].tolist()
        self.index = {d: i for i, d in enumerate(self.dates)}
        self.open = df["open"].astype(float).tolist()
        self.high = df["high"].astype(float).tolist()
        self.low = df["low"].astype(float).tolist()
        self.close = df["close"].astype(float).tolist()
        gap_limit = config.SUSPECT_CORPORATE_ACTION_GAP_PCT / 100
        self.suspect_gaps = {
            i for i in range(1, len(df))
            if self.close[i - 1] > 0 and abs(self.open[i] / self.close[i - 1] - 1) > gap_limit
        }

    def upto(self, i: int) -> pd.DataFrame:
        return self.df.iloc[: i + 1]

    def sliced(self, end_date: str) -> "SymbolFrame":
        """A copy holding only rows <= end_date — used to give each
        consistency-check window its own end, BEFORE any signal is computed."""
        return SymbolFrame(self.symbol, self.df[self.df["date"] <= end_date], self.tick)


# --------------------------------------------------------------------------
# Simulation core (pure: no DB, no broker)
# --------------------------------------------------------------------------

def _classify_exit(checklist: dict) -> str:
    if checklist.get("rsi_recovery", {}).get("pass"):
        return "rsi_recovery"
    if checklist.get("thesis_invalidation", {}).get("pass"):
        return "thesis_invalidation"
    return "time_exit"


def _open_exit(pos: dict, f: SymbolFrame, i: int):
    """Exit that happens AT THE OPEN: a gap through the stop, a gap over an
    OCO target, or a close-based exit scheduled at the previous close.
    Returns (reference_price, reason, gap) or None."""
    o = f.open[i]
    if o <= pos["current_stop"]:
        return o, pos["stop_reason"], True
    if pos["trailing_distance"] is None and o >= pos["initial_target"]:
        return o, "target", True
    if pos["scheduled_exit"]:
        return o, pos["scheduled_exit"], False
    return None


def _intrabar_exit(pos: dict, f: SymbolFrame, i: int):
    """Protective orders hit INSIDE the bar. The stop is tested before the
    target: if both lie within the range, the stop is assumed first."""
    if f.low[i] <= pos["current_stop"]:
        return pos["current_stop"], pos["stop_reason"], False
    if pos["trailing_distance"] is None and f.high[i] >= pos["initial_target"]:
        return pos["initial_target"], "target", False
    return None


def _end_of_bar_update(module, pos: dict, f: SymbolFrame, i: int, params: dict, cfg: BacktestConfig) -> None:
    """Everything decided at the close. The new stop only becomes the stop
    in force for the NEXT bar — it is never tested against this bar's low."""
    new_stop, new_reason = pos["current_stop"], pos["stop_reason"]
    if pos["trailing_distance"] is not None:
        if not pos["trailing_active"] and f.high[i] >= pos["initial_target"]:
            pos["trailing_active"] = True  # matches trailing_engine._try_activate
        if pos["trailing_active"]:
            candidate = market.round_to_tick(f.close[i] - pos["trailing_distance"], f.tick, "down")
            if candidate > new_stop:
                new_stop, new_reason = candidate, "trailing_stop"
        if cfg.simulate_breakeven and not pos["breakeven_done"]:
            risk = pos["entry_price"] - pos["initial_stop"]
            trigger_r = params.get("breakeven_trigger_r")
            if risk > 0 and trigger_r is not None and f.high[i] >= pos["entry_price"] + trigger_r * risk:
                pos["breakeven_done"] = True
                breakeven = market.round_to_tick(pos["entry_price"], f.tick, "up")
                if breakeven > new_stop:
                    new_stop, new_reason = breakeven, "breakeven_stop"

    if not pos["scheduled_exit"]:
        exit_check = module.evaluate_exit(
            {
                "entry_price": pos["planned_entry_price"], "stop_loss": pos["initial_stop"],
                "target_1": pos["initial_target"], "executed_at": pos["entry_date"],
            },
            f.upto(i), params,
        )
        if exit_check["passed"]:
            pos["scheduled_exit"] = _classify_exit(exit_check["checklist"])

    pos["current_stop"], pos["stop_reason"] = new_stop, new_reason


def _trade_warning(f: SymbolFrame, entry_i: int, exit_i: int) -> str | None:
    if any(entry_i <= g <= exit_i for g in f.suspect_gaps):
        return "suspected_corporate_action_gap"
    return None


def simulate(module, frames: dict[str, SymbolFrame], params: dict, cfg: BacktestConfig,
             start_date: str, end_date: str) -> dict:
    """The event-driven portfolio simulation. `frames` must not contain any
    row after end_date (FutureDataError otherwise) — the caller filters the
    data BEFORE signals exist, not the results afterwards."""
    start_date, end_date = str(start_date)[:10], str(end_date)[:10]
    for f in frames.values():
        if f.dates and f.dates[-1] > end_date:
            raise FutureDataError(f"{f.symbol} has data after end_date {end_date} ({f.dates[-1]})")

    symbols = sorted(frames)
    all_dates = sorted({d for f in frames.values() for d in f.dates if start_date <= d <= end_date})

    cash = float(cfg.initial_capital)
    positions: dict[str, dict] = {}
    pending: dict[str, dict] = {}
    last_close: dict[str, float] = {}
    trades: list[dict] = []
    skipped: list[dict] = []
    unexecuted: list[dict] = []
    curve: list[dict] = []
    sequence = 0

    def sell(pos: dict, f: SymbolFrame, i: int, reference: float, reason: str, gap: bool, forced: bool = False):
        nonlocal cash
        exit_price = fill_price("SELL", "MARKET", reference, None, f.tick, cfg.exit_slippage_bps)
        qty = pos["quantity"]
        charges = backtest_costs.compute_charges("SELL", exit_price * qty, cfg.cost_schedule, cfg.costs_enabled)
        cash += exit_price * qty - charges["total"]
        cash = round(cash, 6)
        entry_i = f.index[pos["entry_date"]]
        gross = (exit_price - pos["entry_price"]) * qty
        costs = backtest_costs.add_charges(pos["entry_charges"], charges)
        net = gross - costs["total"]
        risk_ps = pos["planned_entry_price"] - pos["initial_stop"]
        trades.append({
            "symbol": pos["symbol"], "strategy_name": module.STRATEGY_NAME,
            "signal_date": pos["signal_date"], "entry_date": pos["entry_date"],
            "entry_price": round(pos["entry_price"], 2), "planned_entry_price": pos["planned_entry_price"],
            "quantity": qty, "initial_stop": pos["initial_stop"], "initial_target": pos["initial_target"],
            "exit_date": f.dates[i], "exit_price": round(exit_price, 2), "exit_reason": reason,
            "gap_fill": bool(gap), "forced_close": bool(forced),
            "gross_pnl": round(gross, 2), "entry_costs": pos["entry_charges"]["total"],
            "exit_costs": charges["total"], "costs": costs["total"], "cost_breakdown": costs,
            "net_pnl": round(net, 2),
            "holding_days": (date.fromisoformat(f.dates[i]) - date.fromisoformat(pos["entry_date"])).days,
            "bars_held": i - entry_i,
            "r_multiple": round((exit_price - pos["entry_price"]) / risk_ps, 4),
            "net_r_multiple": round(net / (qty * risk_ps), 4),
            "outcome": "win" if net > 0 else "loss",
            "data_warning": _trade_warning(f, entry_i, i),
        })
        del positions[pos["symbol"]]

    for d in all_dates:
        todays = [s for s in symbols if d in frames[s].index]
        touched_today: set[str] = set()

        # 1. Sells at the open: gap stops/targets and scheduled close-based exits.
        for s in todays:
            pos = positions.get(s)
            if pos is None:
                continue
            f = frames[s]
            i = f.index[d]
            touched_today.add(s)
            hit = _open_exit(pos, f, i)
            if hit:
                sell(pos, f, i, *hit)

        # 2. Pending entries fill at the open (or, for a LIMIT, inside the bar).
        for s in sorted((s for s in pending if pending[s]["exec_date"] == d), key=lambda s: pending[s]["seq"]):
            entry = pending.pop(s)
            f = frames[s]
            i = f.index[d]
            filled_at_open = True
            if cfg.entry_model == "next_open":
                price = fill_price("BUY", "MARKET", f.open[i], None, f.tick, cfg.entry_slippage_bps)
            else:
                limit = entry["planned_entry_price"]
                if f.open[i] <= limit:
                    price = fill_price("BUY", "LIMIT", f.open[i], limit, f.tick, cfg.entry_slippage_bps)
                elif f.low[i] <= limit:
                    price, filled_at_open = limit, False
                else:
                    skipped.append({**entry["record"], "reason": "limit_not_reached"})
                    continue
            qty = entry["quantity"]
            charges = backtest_costs.compute_charges("BUY", price * qty, cfg.cost_schedule, cfg.costs_enabled)
            outlay = price * qty + charges["total"]
            if outlay > cash + 1e-9:
                skipped.append({**entry["record"], "reason": "insufficient_cash",
                                "required": round(outlay, 2), "available": round(cash, 2)})
                continue
            cash = round(cash - outlay, 6)
            if cash < -1e-6:
                raise AssertionError(f"cash went negative ({cash}) — capital spent twice")
            positions[s] = {
                "symbol": s, "signal_date": entry["signal_date"], "entry_date": d, "entry_price": price,
                "planned_entry_price": entry["planned_entry_price"], "quantity": qty,
                "initial_stop": entry["stop_loss"], "initial_target": entry["target_1"],
                "trailing_distance": entry["trailing_distance"], "current_stop": entry["stop_loss"],
                "stop_reason": "stop_loss", "trailing_active": False, "breakeven_done": False,
                "scheduled_exit": None, "entry_charges": charges, "filled_at_open": filled_at_open,
            }
            touched_today.add(s)

        # 3. Intrabar protective orders (stop before target). A position
        #    opened at today's open also faces today's open-gap check.
        for s in todays:
            pos = positions.get(s)
            if pos is None:
                continue
            f = frames[s]
            i = f.index[d]
            hit = None
            if pos["entry_date"] == d and pos["filled_at_open"]:
                hit = _open_exit(pos, f, i)
            hit = hit or _intrabar_exit(pos, f, i)
            if hit:
                sell(pos, f, i, *hit)

        # 4. End-of-bar decisions, effective from the next session.
        for s in todays:
            pos = positions.get(s)
            if pos is not None:
                f = frames[s]
                _end_of_bar_update(module, pos, f, f.index[d], params, cfg)

        # 5. Mark to market at today's close.
        for s in todays:
            last_close[s] = frames[s].close[frames[s].index[d]]
        market_value = sum(p["quantity"] * last_close[s] for s, p in positions.items())
        open_risk = sum(p["quantity"] * max(last_close[s] - p["current_stop"], 0.0) for s, p in positions.items())
        equity = cash + market_value
        curve.append({
            "date": d, "cash": round(cash, 2), "market_value": round(market_value, 2),
            "equity": round(equity, 2), "open_positions": len(positions),
            "open_risk": round(open_risk, 2),
            "exposure_pct": round(market_value / equity * 100, 4) if equity > 0 else None,
        })

        # 6. New signals from today's close.
        candidates = []
        for s in todays:
            if s in positions or s in pending or s in touched_today:
                continue
            f = frames[s]
            i = f.index[d]
            result = module.evaluate_entry(f.upto(i), params)
            if result["passed"]:
                candidates.append((s, i, result))
        candidates.sort(key=lambda c: (-(module.rank_key(c[2]) or 0), c[0]))
        cap = params.get("max_signals_per_day")
        for rank, (s, i, result) in enumerate(candidates, start=1):
            f = frames[s]
            record = {"symbol": s, "signal_date": d, "rank": rank, "rank_value": module.rank_key(result)}
            if cap is not None and rank > cap:
                skipped.append({**record, "reason": "max_signals_per_day"})
                continue
            if i + 1 >= len(f.dates):
                unexecuted.append({**record, "reason": "no_next_session_before_end_date"})
                continue
            levels = module.compute_entry_levels(result, params)
            if levels is None:
                skipped.append({**record, "reason": "no_valid_levels"})
                continue
            entry_price = market.round_to_tick(levels["entry_price"], f.tick)
            stop_loss = market.round_to_tick(levels["stop_loss"], f.tick)
            target_1 = market.round_to_tick(levels["target_1"], f.tick)
            if not (0 < stop_loss < entry_price < target_1):
                skipped.append({**record, "reason": "levels_collapse_on_tick_grid"})
                continue
            trailing_distance = levels.get("trailing_distance")
            if trailing_distance is not None:
                trailing_distance = max(market.round_to_tick(trailing_distance, f.tick), f.tick)
            if len(positions) + len(pending) >= cfg.max_open_positions:
                skipped.append({**record, "reason": "max_open_positions"})
                continue
            sizing = risk_engine.compute_position_size(entry_price, stop_loss, equity, {
                "max_risk_per_trade_pct": cfg.risk_per_trade_pct,
                "max_capital_per_trade_pct": cfg.capital_per_trade_pct,
            })
            if sizing["suggested_qty"] <= 0:
                skipped.append({**record, "reason": "size_zero"})
                continue
            sequence += 1
            pending[s] = {
                "seq": sequence, "signal_date": d, "exec_date": f.dates[i + 1],
                "planned_entry_price": entry_price, "stop_loss": stop_loss, "target_1": target_1,
                "trailing_distance": trailing_distance, "quantity": sizing["suggested_qty"],
                "record": {**record, "planned_entry_price": entry_price, "quantity": sizing["suggested_qty"]},
            }

    # End of range: close what is still open at its last bar <= end_date.
    for s in sorted(positions):
        f = frames[s]
        i = len(f.dates) - 1
        sell(positions[s], f, i, f.close[i], "backtest_end", False, forced=True)
    for s in sorted(pending, key=lambda s: pending[s]["seq"]):
        unexecuted.append({**pending[s]["record"], "reason": "no_next_session_before_end_date"})
    if curve:
        curve[-1] = {**curve[-1], "cash": round(cash, 2), "market_value": 0.0, "equity": round(cash, 2),
                     "open_positions": 0, "open_risk": 0.0, "exposure_pct": 0.0, "liquidated": True}

    trades.sort(key=lambda t: (t["entry_date"], t["symbol"]))
    data_warnings = []
    for s in symbols:
        f = frames[s]
        for g in sorted(f.suspect_gaps):
            if start_date <= f.dates[g] <= end_date:
                data_warnings.append({
                    "symbol": s, "date": f.dates[g], "open": f.open[g], "prev_close": f.close[g - 1],
                    "gap_pct": round((f.open[g] / f.close[g - 1] - 1) * 100, 2),
                    "warning": "suspected_corporate_action_gap",
                })

    return {
        "trades": trades, "skipped_signals": skipped, "unexecuted_signals": unexecuted,
        "equity_curve": curve, "final_cash": round(cash, 2), "data_warnings": data_warnings,
        "data_window": {
            "data_load_start": min((f.dates[0] for f in frames.values() if f.dates), default=None),
            "simulation_start": start_date, "simulation_end": end_date,
            "last_bar_used": max((f.dates[-1] for f in frames.values() if f.dates), default=None),
        },
    }


def simulate_with_metrics(module, frames, params, cfg, start_date, end_date) -> dict:
    sim = simulate(module, frames, params, cfg, start_date, end_date)
    sim["metrics"] = backtest_analytics.compute_portfolio_metrics(
        sim["trades"], sim["equity_curve"], cfg.initial_capital, start_date, end_date,
        sim["skipped_signals"], sim["unexecuted_signals"],
    )
    return sim


# --------------------------------------------------------------------------
# Consistency-check windows, loading, persistence
# --------------------------------------------------------------------------

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


def _window_stats(metrics: dict) -> dict:
    return {
        "total_signals": metrics["trades"], "win_rate": metrics["win_rate"],
        "avg_r_multiple": metrics["expectancy_r"], "profit_factor": metrics["profit_factor"],
        "max_drawdown_pct": metrics["max_drawdown_pct"], "max_drawdown_amount": metrics["max_drawdown_amount"],
        "net_pnl": metrics["net_pnl"], "net_return_pct": metrics["net_return_pct"],
    }


def _run_walk_forward_windows(conn, run_id, module, frames, params, cfg, start_date, end_date, window_days):
    """Consistency-check windows (Phase 8 Part D.2) — explicitly NOT
    walk-forward optimization: no parameter refitting occurs anywhere. Each
    window is a full, independent portfolio simulation starting from the
    initial capital, whose data is cut at the WINDOW's end before any signal
    is computed, so a window can never see prices from a later window."""
    windows_out = []
    for i, (w_start, w_end) in enumerate(_sequential_windows(start_date, end_date, window_days)):
        window_frames = {s: f.sliced(w_end) for s, f in frames.items()}
        sim = simulate_with_metrics(module, window_frames, params, cfg, w_start, w_end)
        stats = _window_stats(sim["metrics"])
        conn.execute(
            """
            INSERT INTO walk_forward_windows (backtest_run_id, window_index, window_start, window_end, stats)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, i, w_start, w_end, json.dumps(stats)),
        )
        windows_out.append({"window_index": i, "window_start": w_start, "window_end": w_end, "stats": stats})
    return windows_out


def load_frames(symbols: list[tuple[int, str]], end_date: str) -> tuple[dict[str, SymbolFrame], list[str]]:
    """Loads every symbol's history up to end_date — filtered in SQL, so no
    later row exists in memory — with all earlier rows kept as warm-up."""
    frames: dict[str, SymbolFrame] = {}
    missing = []
    for instrument_token, symbol in symbols:
        df = indicators.load_ohlc_frame(instrument_token, end_date=end_date)
        if len(df) < 2:
            missing.append(symbol)
            continue
        frames[symbol] = SymbolFrame(symbol, df, instruments.get_tick_size(symbol, "NSE"))
    return frames, missing


def run_backtest(
    conn: sqlite3.Connection,
    strategy_name: str,
    symbols: list[tuple[int, str]],
    params: dict,
    start_date: str,
    end_date: str,
    cfg: BacktestConfig | None = None,
    universe_source: str = "current_watchlist",
    run_walk_forward: bool = False,
    window_days: int = 90,
    run_monte_carlo: bool = False,
    mc_iterations: int = 1000,
    mc_random_seed: int | None = None,
) -> dict:
    """`symbols` is a list of (instrument_token, symbol) pairs already
    resolved by the caller (the route defaults this to the current watchlist
    when none are given, and says so via universe_source). Calls the EXACT
    SAME evaluate_entry/evaluate_exit/compute_entry_levels/rank_key the live
    scanner uses for `strategy_name` — never a parallel implementation."""
    module = STRATEGIES[strategy_name]
    cfg = cfg or BacktestConfig.from_overrides()
    start_date, end_date = str(start_date)[:10], str(end_date)[:10]

    # PK-22D: history known or suspected to be stale (an unadjusted split or
    # bonus) is never simulated silently — the symbol is excluded, with why.
    flagged = ohlc_integrity.flagged(conn, [token for token, _ in symbols])
    excluded = [
        {"symbol": symbol, "instrument_token": token, "status": flagged[token]["status"],
         "reason": json.loads(flagged[token]["status_reason"]) if flagged[token]["status_reason"] else None,
         "status_at": flagged[token]["status_at"]}
        for token, symbol in symbols if token in flagged
    ]
    symbols = [(token, symbol) for token, symbol in symbols if token not in flagged]
    frames, missing = load_frames(symbols, end_date)
    sim = simulate_with_metrics(module, frames, params, cfg, start_date, end_date)
    metrics = sim["metrics"]
    trades = sim["trades"]

    mc_summary = (
        backtest_analytics.run_monte_carlo([t["net_pnl"] for t in trades], cfg.initial_capital,
                                           mc_iterations, mc_random_seed)
        if run_monte_carlo else backtest_analytics.empty_monte_carlo()
    )

    universe = {
        "source": universe_source,
        "survivorship_bias": UNIVERSE_BIAS.get(universe_source, "unverified"),
        "warning": UNIVERSE_WARNINGS.get(universe_source, UNIVERSE_WARNINGS["user_supplied"]),
        "symbols_without_data": missing,
        "excluded_symbols": excluded,
    }
    warnings = [universe["warning"]]
    for e in excluded:
        warnings.append(
            f"{e['symbol']} EXCLUDED: its stored price history is {e['status']} "
            f"({(e['reason'] or {}).get('code', 'flagged')}) — rebuild it from System Diagnostics to include it."
        )
    if not cfg.costs_enabled:
        warnings.append("Transaction costs are DISABLED for this run — net equals gross before slippage.")
    if missing:
        warnings.append(f"No price history up to {end_date} for: {', '.join(missing)}.")
    for w in sim["data_warnings"]:
        warnings.append(
            f"{w['symbol']} {w['date']}: open {w['open']:.2f} vs previous close {w['prev_close']:.2f} "
            f"({w['gap_pct']:+.1f}%) — possible unadjusted corporate action; trades spanning it are flagged."
        )

    execution_config = {**cfg.snapshot(), "data_window": sim["data_window"],
                        "max_signals_per_day": params.get("max_signals_per_day")}
    legacy = {
        "total_signals": metrics["trades"], "win_rate": metrics["win_rate"],
        "avg_r_multiple": metrics["expectancy_r"], "profit_factor": metrics["profit_factor"],
        "max_drawdown_pct": metrics["max_drawdown_pct"], "max_drawdown_amount": metrics["max_drawdown_amount"],
        "sharpe_ratio": metrics["sharpe_ratio"], "sortino_ratio": metrics["sortino_ratio"],
        "max_consecutive_losses": metrics["max_consecutive_losses"],
        "recovery_factor": metrics["recovery_factor"], "avg_win": metrics["avg_win_r"],
        "avg_loss": metrics["avg_loss_r"], "time_in_market_pct": metrics["time_in_market_pct"],
    }
    signals = {"skipped": sim["skipped_signals"], "unexecuted": sim["unexecuted_signals"]}

    cursor = conn.execute(
        """
        INSERT INTO backtest_runs (
            strategy_name, params_snapshot, start_date, end_date, symbols_tested,
            total_signals, win_rate, avg_r_multiple, profit_factor, max_drawdown_pct, max_drawdown_amount,
            sharpe_ratio, sortino_ratio, max_consecutive_losses, recovery_factor, avg_win, avg_loss,
            time_in_market_pct, mc_iterations, mc_median_drawdown_pct, mc_p95_drawdown_pct,
            mc_p5_final_equity, mc_p95_final_equity,
            execution_config, metrics, equity_curve, skipped_signals, warnings,
            universe_source, survivorship_bias
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            strategy_name, json.dumps(params), start_date, end_date, json.dumps([s for _, s in symbols]),
            legacy["total_signals"], legacy["win_rate"], legacy["avg_r_multiple"],
            legacy["profit_factor"], legacy["max_drawdown_pct"], legacy["max_drawdown_amount"],
            legacy["sharpe_ratio"], legacy["sortino_ratio"], legacy["max_consecutive_losses"],
            legacy["recovery_factor"], legacy["avg_win"], legacy["avg_loss"],
            legacy["time_in_market_pct"], mc_summary["mc_iterations"], mc_summary["mc_median_drawdown_pct"],
            mc_summary["mc_p95_drawdown_pct"], mc_summary["mc_p5_final_equity"], mc_summary["mc_p95_final_equity"],
            json.dumps(execution_config), json.dumps(metrics), json.dumps(sim["equity_curve"]),
            json.dumps(signals), json.dumps(warnings), universe["source"], universe["survivorship_bias"],
        ),
    )
    run_id = cursor.lastrowid
    conn.executemany(
        """
        INSERT INTO backtest_trades (
            backtest_run_id, symbol, entry_date, entry_price, exit_date, exit_price, exit_reason, r_multiple, outcome,
            strategy_name, signal_date, planned_entry_price, quantity, initial_stop, initial_target,
            gross_pnl, entry_costs, exit_costs, costs, net_pnl, holding_days, bars_held, net_r_multiple,
            gap_fill, forced_close, data_warning
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (run_id, t["symbol"], t["entry_date"], t["entry_price"], t["exit_date"], t["exit_price"],
             t["exit_reason"], t["r_multiple"], t["outcome"],
             t["strategy_name"], t["signal_date"], t["planned_entry_price"], t["quantity"], t["initial_stop"],
             t["initial_target"], t["gross_pnl"], t["entry_costs"], t["exit_costs"], t["costs"], t["net_pnl"],
             t["holding_days"], t["bars_held"], t["net_r_multiple"], int(t["gap_fill"]), int(t["forced_close"]),
             t["data_warning"])
            for t in trades
        ],
    )

    walk_forward_windows = (
        _run_walk_forward_windows(conn, run_id, module, frames, params, cfg, start_date, end_date, window_days)
        if run_walk_forward else []
    )
    conn.commit()

    return {
        "id": run_id, "strategy_name": strategy_name, "start_date": start_date, "end_date": end_date,
        "symbols_tested": [s for _, s in symbols],
        "starting_equity": cfg.initial_capital,
        "execution_config": execution_config, "universe": universe, "metrics": metrics,
        "trades": trades, "equity_curve": sim["equity_curve"],
        "skipped_signals": sim["skipped_signals"], "unexecuted_signals": sim["unexecuted_signals"],
        "warnings": warnings,
        **legacy, **mc_summary,
        "walk_forward_windows": walk_forward_windows,
    }


_JSON_RUN_COLUMNS = ("execution_config", "metrics", "equity_curve", "skipped_signals", "warnings")


def _decode_run(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["params_snapshot"] = json.loads(data["params_snapshot"])
    data["symbols_tested"] = json.loads(data["symbols_tested"])
    for column in _JSON_RUN_COLUMNS:
        # NULL on a run recorded before PK-21 — those runs used the old,
        # optimistic fill model and are never recomputed.
        if data.get(column) is not None:
            data[column] = json.loads(data[column])
    return data


def get_backtest(conn: sqlite3.Connection, run_id: int) -> dict | None:
    run = conn.execute("SELECT * FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        return None
    trades = conn.execute(
        "SELECT * FROM backtest_trades WHERE backtest_run_id = ? ORDER BY entry_date, symbol", (run_id,)
    ).fetchall()
    windows = conn.execute(
        "SELECT * FROM walk_forward_windows WHERE backtest_run_id = ? ORDER BY window_index", (run_id,)
    ).fetchall()
    data = _decode_run(run)
    data["trades"] = [dict(t) for t in trades]
    data["walk_forward_windows"] = [
        {**dict(w), "stats": json.loads(w["stats"])} for w in windows
    ]
    return data


def list_backtests(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM backtest_runs ORDER BY created_at DESC, id DESC").fetchall()
    out = []
    for r in rows:
        d = _decode_run(r)
        d.pop("equity_curve", None)  # the list view never needs the full curve
        out.append(d)
    return out
