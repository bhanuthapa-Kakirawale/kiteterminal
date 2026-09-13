"""Pluggable strategy modules. Each module exposes:
  STRATEGY_NAME, EXIT_STRATEGY_NAME, DEFAULT_PARAMS
  evaluate_entry(df, params) -> dict
  evaluate_exit(position, df, params) -> dict
  compute_entry_levels(candidate, params) -> dict | None
  rank_key(candidate) -> float

The batch-level "scan the whole watchlist / all open positions, build
tickets, run the risk engine" work is intentionally NOT part of this
interface — that logic is identical across every strategy (Part A.4 of
the Phase 7 plan) and lives once, generically, in strategy_engine.py's
run_entry_scan/run_exit_scan, parameterized by whichever module is passed
in. Modules only own their own signal-evaluation logic.
"""

from backend.strategies import momentum_breakout, rsi_mean_reversion, trend_pullback

STRATEGIES = {
    momentum_breakout.STRATEGY_NAME: momentum_breakout,
    trend_pullback.STRATEGY_NAME: trend_pullback,
    rsi_mean_reversion.STRATEGY_NAME: rsi_mean_reversion,
}
