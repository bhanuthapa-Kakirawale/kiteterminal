import math
import random
import statistics
from datetime import date

from backend import backtest_costs

# --------------------------------------------------------------------------
# PK-21 — portfolio metrics for the execution-realistic backtester.
#
# Everything here is computed from NET results (after slippage and
# transaction costs) and from the simulation's daily mark-to-market equity
# curve (cash + open positions at their close), never from closed-trade
# cumulative P&L alone.
#
# Judgment calls (flagged explicitly):
# - Max drawdown is peak-to-trough on the DAILY MTM equity curve, so an open
#   position's unrealised loss counts the day it happens.
# - Sharpe/Sortino use the daily equity returns that curve provides (one
#   observation per simulated trading day, cash earning 0), annualised by
#   sqrt(252) with a risk-free rate of 0%. Sortino's downside deviation is
#   sqrt(mean(min(r, 0)^2)) over ALL days. Both are None with fewer than two
#   daily returns or zero dispersion. (Before PK-21 these were per-trade
#   ratios annualised with an implied trades-per-year figure.)
# - Net CAGR is only reported when the simulated span is at least 365
#   calendar days — annualising a few months of results is misleading.
# - A trade is a win when its NET P&L is positive; profit factor, average /
#   median trade, largest win / loss and expectancy are all net.
# - Exposure % is the mean over simulated days of market value / equity;
#   time in market % is the share of days with at least one open position.
# - avg_win_r / avg_loss_r / expectancy_r are NET R-multiples (net P&L over
#   the trade's initial risk, quantity x (planned entry - initial stop)).
# --------------------------------------------------------------------------

TRADING_DAYS_PER_YEAR = 252


def _max_drawdown(curve: list[dict]) -> tuple[float, float]:
    peak = None
    max_amount = 0.0
    max_pct = 0.0
    for point in curve:
        equity = point["equity"]
        peak = equity if peak is None else max(peak, equity)
        amount = peak - equity
        max_amount = max(max_amount, amount)
        if peak > 0:
            max_pct = max(max_pct, amount / peak * 100)
    return round(max_amount, 2), round(max_pct, 4)


def _daily_returns(curve: list[dict], initial_capital: float) -> list[float]:
    returns = []
    previous = initial_capital
    for point in curve:
        if previous > 0:
            returns.append(point["equity"] / previous - 1)
        previous = point["equity"]
    return returns


def _sharpe_sortino(returns: list[float]) -> tuple[float | None, float | None]:
    if len(returns) < 2:
        return None, None
    mean = sum(returns) / len(returns)
    stdev = statistics.stdev(returns)
    annualization = math.sqrt(TRADING_DAYS_PER_YEAR)
    sharpe = (mean / stdev) * annualization if stdev > 0 else None
    downside = math.sqrt(sum(min(r, 0) ** 2 for r in returns) / len(returns))
    sortino = (mean / downside) * annualization if downside > 0 else None
    return sharpe, sortino


def _max_consecutive_losses(trades: list[dict]) -> int:
    best = streak = 0
    for t in sorted(trades, key=lambda x: (x["exit_date"], x["symbol"])):
        if t["net_pnl"] <= 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return best


def compute_portfolio_metrics(
    trades: list[dict], equity_curve: list[dict], initial_capital: float,
    start_date: str, end_date: str, skipped: list[dict], unexecuted: list[dict],
) -> dict:
    n = len(trades)
    ending_equity = equity_curve[-1]["equity"] if equity_curve else initial_capital
    gross = round(sum(t["gross_pnl"] for t in trades), 2)
    costs = round(sum(t["costs"] for t in trades), 2)
    net = round(sum(t["net_pnl"] for t in trades), 2)
    breakdown = backtest_costs.zero_charges()
    for t in trades:
        breakdown = backtest_costs.add_charges(breakdown, t["cost_breakdown"])

    span_days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days
    cagr = None
    if span_days >= 365 and ending_equity > 0:
        cagr = ((ending_equity / initial_capital) ** (365.25 / span_days) - 1) * 100

    dd_amount, dd_pct = _max_drawdown(equity_curve)
    sharpe, sortino = _sharpe_sortino(_daily_returns(equity_curve, initial_capital))

    net_values = [t["net_pnl"] for t in trades]
    wins = [v for v in net_values if v > 0]
    losses = [v for v in net_values if v <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    win_r = [t["net_r_multiple"] for t in trades if t["net_pnl"] > 0]
    loss_r = [t["net_r_multiple"] for t in trades if t["net_pnl"] <= 0]

    skipped_by_reason: dict[str, int] = {}
    for s in skipped:
        skipped_by_reason[s["reason"]] = skipped_by_reason.get(s["reason"], 0) + 1

    days = len(equity_curve)
    exposures = [p["exposure_pct"] for p in equity_curve if p.get("exposure_pct") is not None]

    return {
        "initial_capital": round(initial_capital, 2),
        "ending_equity": round(ending_equity, 2),
        "gross_pnl": gross,
        "costs": costs,
        "cost_breakdown": breakdown,
        "net_pnl": net,
        "gross_return_pct": round(gross / initial_capital * 100, 4),
        "net_return_pct": round((ending_equity - initial_capital) / initial_capital * 100, 4),
        "net_cagr_pct": round(cagr, 4) if cagr is not None else None,
        "max_drawdown_amount": dd_amount,
        "max_drawdown_pct": dd_pct,
        "trades": n,
        "win_rate": len(wins) / n if n else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "avg_trade": round(net / n, 2) if n else None,
        "median_trade": round(statistics.median(net_values), 2) if n else None,
        "expectancy_r": round(sum(t["net_r_multiple"] for t in trades) / n, 4) if n else None,
        "avg_win_r": round(sum(win_r) / len(win_r), 4) if win_r else None,
        "avg_loss_r": round(sum(loss_r) / len(loss_r), 4) if loss_r else None,
        "largest_win": max(net_values) if n else None,
        "largest_loss": min(net_values) if n else None,
        "max_consecutive_losses": _max_consecutive_losses(trades) if n else None,
        "avg_holding_days": round(sum(t["holding_days"] for t in trades) / n, 2) if n else None,
        "avg_bars_held": round(sum(t["bars_held"] for t in trades) / n, 2) if n else None,
        "recovery_factor": (net / dd_amount) if dd_amount > 0 else None,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "exposure_pct": round(sum(exposures) / len(exposures), 4) if exposures else None,
        "time_in_market_pct": round(sum(1 for p in equity_curve if p["open_positions"] > 0) / days * 100, 4)
        if days else None,
        "skipped_signals": len(skipped),
        "skipped_by_reason": dict(sorted(skipped_by_reason.items())),
        "unexecuted_signals": len(unexecuted),
        "simulated_days": days,
    }


# --------------------------------------------------------------------------
# Monte Carlo: reshuffle the order of the closed trades' NET rupee P&L and
# rebuild the resulting closed-trade equity path for each shuffle, to
# reframe "what's the likely drawdown" as a distribution rather than the
# single number this specific trade ORDER happened to produce.
#
# Judgment calls:
# - Closed-trade only: a reshuffle has no daily path, so these drawdowns
#   are NOT comparable with the MTM max drawdown above (they understate it).
# - Skipped entirely (all fields None) when fewer than MIN_TRADES_FOR_MC
#   trades exist.
# - No fixed random seed by default — this represents genuine
#   order-uncertainty. Pass random_seed for a reproducible run.
# --------------------------------------------------------------------------

MIN_TRADES_FOR_MONTE_CARLO = 5


def empty_monte_carlo() -> dict:
    return {
        "mc_iterations": None, "mc_median_drawdown_pct": None, "mc_p95_drawdown_pct": None,
        "mc_p5_final_equity": None, "mc_p95_final_equity": None,
    }


def run_monte_carlo(
    net_pnls: list[float], starting_equity: float, iterations: int = 1000, random_seed: int | None = None,
) -> dict:
    if len(net_pnls) < MIN_TRADES_FOR_MONTE_CARLO:
        return empty_monte_carlo()

    rng = random.Random(random_seed)
    drawdowns = []
    final_equities = []
    for _ in range(iterations):
        shuffled = net_pnls[:]
        rng.shuffle(shuffled)

        equity = starting_equity
        peak = equity
        max_dd_pct = 0.0
        for pnl in shuffled:
            equity += pnl
            peak = max(peak, equity)
            if peak > 0:
                max_dd_pct = max(max_dd_pct, (peak - equity) / peak * 100)
        drawdowns.append(max_dd_pct)
        final_equities.append(equity)

    drawdowns.sort()
    final_equities.sort()

    def percentile(sorted_values: list[float], pct: float) -> float:
        # Nearest-rank method — simple, standard, no interpolation needed.
        idx = min(int(pct / 100 * len(sorted_values)), len(sorted_values) - 1)
        return sorted_values[idx]

    return {
        "mc_iterations": iterations,
        "mc_median_drawdown_pct": percentile(drawdowns, 50),
        "mc_p95_drawdown_pct": percentile(drawdowns, 95),
        "mc_p5_final_equity": percentile(final_equities, 5),
        "mc_p95_final_equity": percentile(final_equities, 95),
    }
