import math
import random
from datetime import date

# --------------------------------------------------------------------------
# Phase 8 Part D — additional backtest metrics, all computed from the same
# backtest_trades data run_backtest() already generates. No new market data.
#
# Judgment calls (flagged explicitly, per the Phase 8 spec's request):
# - Risk-free rate = 0%, not silently assumed — used in both Sharpe and
#   Sortino below.
# - Trade outcomes have variable holding periods (not one observation per
#   fixed calendar day), so there's no natural "daily return" series to
#   annualize the textbook way. Sharpe/Sortino are computed PER-TRADE
#   (mean / stdev of r_multiple across trades) and then annualized using
#   an IMPLIED trades-per-year figure derived from this run's own
#   total_signals and date range (trades_per_year = total_signals /
#   (backtest_days / 365.25)). This is a standard convention for
#   trade-level (not fixed-frequency) backtests, but the annualization
#   itself is an assumption about how many similar trades would occur
#   over a full year — it is NOT directly comparable to a daily-return
#   Sharpe from a different kind of strategy.
# - Sortino's downside deviation is computed over ALL trades
#   (sqrt(mean(min(r, 0)^2))), not just the losing subset — one of two
#   common conventions; the other (stdev of losers only) would give a
#   different number for the same trade sequence.
# - avg_win/avg_loss are reported in R-multiples (consistent with
#   avg_r_multiple elsewhere in this app), not rupees.
# --------------------------------------------------------------------------


def _trade_holding_days(trade: dict) -> int:
    entry = date.fromisoformat(trade["entry_date"])
    exit_ = date.fromisoformat(trade["exit_date"])
    return max((exit_ - entry).days, 0)


def compute_rigor_metrics(
    trades: list[dict], start_date: str, end_date: str, risk_amount_per_trade: float, max_drawdown_amount: float,
) -> dict:
    total = len(trades)
    if total == 0:
        return {
            "sharpe_ratio": None, "sortino_ratio": None, "max_consecutive_losses": None,
            "recovery_factor": None, "avg_win": None, "avg_loss": None, "time_in_market_pct": None,
        }

    r_values = [t["r_multiple"] for t in trades]
    mean_r = sum(r_values) / total

    backtest_days = max((date.fromisoformat(end_date) - date.fromisoformat(start_date)).days, 1)
    trades_per_year = total / (backtest_days / 365.25)
    annualization = math.sqrt(trades_per_year)

    if total >= 2:
        variance = sum((r - mean_r) ** 2 for r in r_values) / (total - 1)  # sample stdev, ddof=1
        stdev_r = math.sqrt(variance)
        sharpe_ratio = (mean_r / stdev_r) * annualization if stdev_r > 0 else None
    else:
        sharpe_ratio = None

    downside_sq_mean = sum(min(r, 0) ** 2 for r in r_values) / total
    downside_dev = math.sqrt(downside_sq_mean)
    sortino_ratio = (mean_r / downside_dev) * annualization if downside_dev > 0 else None

    max_consecutive_losses = 0
    current_streak = 0
    for t in sorted(trades, key=lambda x: x["exit_date"]):
        if t["outcome"] == "loss":
            current_streak += 1
            max_consecutive_losses = max(max_consecutive_losses, current_streak)
        else:
            current_streak = 0

    net_profit = sum(r * risk_amount_per_trade for r in r_values)
    recovery_factor = (net_profit / max_drawdown_amount) if max_drawdown_amount > 0 else None

    wins = [r for r in r_values if r > 0]
    losses = [r for r in r_values if r <= 0]
    avg_win = (sum(wins) / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None

    total_holding_days = sum(_trade_holding_days(t) for t in trades)
    time_in_market_pct = (total_holding_days / backtest_days * 100) if backtest_days else None

    return {
        "sharpe_ratio": sharpe_ratio, "sortino_ratio": sortino_ratio,
        "max_consecutive_losses": max_consecutive_losses, "recovery_factor": recovery_factor,
        "avg_win": avg_win, "avg_loss": avg_loss, "time_in_market_pct": time_in_market_pct,
    }


# --------------------------------------------------------------------------
# Monte Carlo: reshuffle the trade-outcome sequence and reconstruct the
# resulting equity curve for each shuffle, to reframe "what's the likely
# drawdown" as a distribution rather than the single number this specific
# trade ORDER happened to produce.
#
# Judgment calls:
# - Skipped entirely (all fields None) when fewer than MIN_TRADES_FOR_MC
#   trades exist — reshuffling a handful of trades doesn't produce a
#   meaningful distribution, so nulls are returned rather than a
#   fabricated-looking distribution from too little data.
# - No fixed random seed by default — this is meant to represent genuine
#   order-uncertainty, so repeated runs legitimately differ each time.
#   Pass random_seed for a reproducible run (e.g. for testing).
# --------------------------------------------------------------------------

MIN_TRADES_FOR_MONTE_CARLO = 5


def run_monte_carlo(
    trades: list[dict], starting_equity: float, risk_amount_per_trade: float,
    iterations: int = 1000, random_seed: int | None = None,
) -> dict:
    total = len(trades)
    if total < MIN_TRADES_FOR_MONTE_CARLO:
        return {
            "mc_iterations": None, "mc_median_drawdown_pct": None, "mc_p95_drawdown_pct": None,
            "mc_p5_final_equity": None, "mc_p95_final_equity": None,
        }

    rng = random.Random(random_seed)
    r_values = [t["r_multiple"] for t in trades]

    drawdowns = []
    final_equities = []
    for _ in range(iterations):
        shuffled = r_values[:]
        rng.shuffle(shuffled)

        equity = starting_equity
        peak = equity
        max_dd_pct = 0.0
        for r in shuffled:
            equity += r * risk_amount_per_trade
            peak = max(peak, equity)
            if peak > 0:
                max_dd_pct = max(max_dd_pct, (peak - equity) / peak * 100)
        drawdowns.append(max_dd_pct)
        final_equities.append(equity)

    drawdowns.sort()
    final_equities.sort()

    def percentile(sorted_values: list[float], pct: float) -> float:
        # Nearest-rank method — simple, standard, no interpolation needed
        # for a distribution this large (>= 5 * iterations samples).
        idx = min(int(pct / 100 * len(sorted_values)), len(sorted_values) - 1)
        return sorted_values[idx]

    return {
        "mc_iterations": iterations,
        "mc_median_drawdown_pct": percentile(drawdowns, 50),
        "mc_p95_drawdown_pct": percentile(drawdowns, 95),
        "mc_p5_final_equity": percentile(final_equities, 5),
        "mc_p95_final_equity": percentile(final_equities, 95),
    }
