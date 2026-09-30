"""PK-23 research harness: volume features at historical strategy signals.

For PK-24/25 analysis, not for trading. It replays a strategy's own
evaluate_entry over stored history — exactly as the backtester and test_27
do — and, for every signal, records the Volume Intelligence features as they
stood at the signal bar. Optionally it adds research OUTCOMES (forward
returns, excursions, the PK-21 backtest trade result) in clearly separated
`outcome_*` columns.

Separation rule: features for a signal on bar i are computed from the frame
cut at i (`df.iloc[: i + 1]`), so they cannot see a later row. forward_outcomes
is the ONLY function here that reads rows after i, and its values are never
passed back into a feature. Nothing in the live scanner, API or UI imports
this module.

CLI (reads a database COPY, opened read-only and immutable):

    python -m backend.volume_research export --db <copy.db> --out signals.csv \
        [--strategy momentum_breakout] [--start 2024-09-01] [--end 2026-09-18] [--with-trades]
"""

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

from backend import volume_intelligence as vi
from backend.strategies import STRATEGIES

OUTCOME_PREFIX = "outcome_"
FORWARD_HORIZONS = (5, 10, 20)
EXCURSION_HORIZON = 20

IDENTITY_COLUMNS = ("strategy", "symbol", "instrument_token", "signal_date", "feature_version", "data_quality")

# (column, path into the VolumeFeatures object)
FEATURE_FIELDS = (
    ("volume", ("bar", "volume")),
    ("avg_volume_20", ("relative_volume", "avg_volume_20")),
    ("rvol_5", ("relative_volume", "rvol_5")),
    ("rvol_20", ("relative_volume", "rvol_20")),
    ("rvol_median_20", ("relative_volume", "rvol_median_20")),
    ("volume_percentile_60", ("relative_volume", "volume_percentile_60")),
    ("volume_zscore_20", ("relative_volume", "volume_zscore_20")),
    ("traded_value", ("liquidity", "traded_value")),
    ("avg_traded_value_20", ("liquidity", "avg_traded_value_20")),
    ("relative_traded_value_20", ("liquidity", "relative_traded_value_20")),
    ("liquidity_warning", ("liquidity", "liquidity_warning")),
    ("close_location", ("structure", "close_location")),
    ("upper_wick_pct", ("structure", "upper_wick_pct")),
    ("price_change_pct", ("structure", "price_change_pct")),
    ("gap_pct", ("structure", "gap_pct")),
    ("high_volume_strong_close", ("structure", "high_volume_strong_close")),
    ("high_volume_weak_close", ("structure", "high_volume_weak_close")),
    ("obv_slope_5", ("obv", "slope_5")),
    ("obv_slope_20", ("obv", "slope_20")),
    ("obv_divergence", ("obv", "divergence")),
    ("contraction_ratio_5_20", ("contraction", "contraction_ratio_5_20")),
    ("volume_slope_10", ("contraction", "volume_slope_10")),
    ("pre_breakout_volume_contraction", ("contraction", "pre_breakout_volume_contraction")),
    ("in_pullback", ("pullback", "in_pullback")),
    ("pullback_volume_ratio", ("pullback", "pullback_volume_ratio")),
    ("recovery_volume_ratio", ("pullback", "recovery_volume_ratio")),
    ("low_volume_pullback", ("pullback", "low_volume_pullback")),
    ("accumulation_like_10", ("evidence", "accumulation_like_10")),
    ("distribution_like_10", ("evidence", "distribution_like_10")),
    ("accumulation_like_20", ("evidence", "accumulation_like_20")),
    ("distribution_like_20", ("evidence", "distribution_like_20")),
    ("net_accumulation_20", ("evidence", "net_accumulation_20")),
    ("is_breakout_30", ("breakout", "is_breakout")),
    ("breakout_close_quality", ("breakout", "close_quality")),
    ("abnormal_volume", ("anomalies", "abnormal_volume")),
    ("extreme_volume", ("anomalies", "extreme_volume")),
    ("negative_shock", ("anomalies", "negative_shock")),
    ("positive_shock", ("anomalies", "positive_shock")),
    ("legacy_volume_multiple_20", ("legacy_volume_multiple_20",)),
)
FEATURE_COLUMNS = tuple(name for name, _ in FEATURE_FIELDS)


def _dig(features: dict, path: tuple):
    value = features
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def feature_record(features: dict) -> dict:
    """The flat, CSV-friendly subset of one VolumeFeatures object."""
    return {name: _dig(features, path) for name, path in FEATURE_FIELDS}


def forward_outcomes(df: pd.DataFrame, i: int) -> dict:
    """RESEARCH LABELS for a signal on bar i — they read bars after i and are
    therefore never features. Measured from the next session's open (the
    PK-21 next-open entry): forward close returns at 5/10/20 sessions and the
    maximum favourable / adverse excursion over the next 20 sessions, in %.
    A horizon without enough bars is None."""
    out = {f"{OUTCOME_PREFIX}fwd_ret_{h}_pct": None for h in FORWARD_HORIZONS}
    out[f"{OUTCOME_PREFIX}mfe_{EXCURSION_HORIZON}_pct"] = None
    out[f"{OUTCOME_PREFIX}mae_{EXCURSION_HORIZON}_pct"] = None
    if i + 1 >= len(df):
        return out
    entry = float(df["open"].iloc[i + 1])
    if entry <= 0:
        return out
    for h in FORWARD_HORIZONS:
        if i + h < len(df):
            out[f"{OUTCOME_PREFIX}fwd_ret_{h}_pct"] = round((float(df["close"].iloc[i + h]) / entry - 1) * 100, 4)
    if i + EXCURSION_HORIZON < len(df):
        span = df.iloc[i + 1: i + EXCURSION_HORIZON + 1]
        out[f"{OUTCOME_PREFIX}mfe_{EXCURSION_HORIZON}_pct"] = round((float(span["high"].max()) / entry - 1) * 100, 4)
        out[f"{OUTCOME_PREFIX}mae_{EXCURSION_HORIZON}_pct"] = round((float(span["low"].min()) / entry - 1) * 100, 4)
    return out


def signal_feature_rows(module, frames: dict[str, pd.DataFrame], params: dict, start: str, end: str, *,
                        tokens: dict[str, int] | None = None, verified_gaps: dict[str, list] | None = None,
                        with_outcomes: bool = True) -> list[dict]:
    """One row per bar in [start, end] on which module.evaluate_entry passes.

    `frames` are full date-ordered histories (they may extend past `end`;
    only outcomes may look there). Features come from vi.compute_at on the
    frame cut at the signal bar."""
    rows = []
    for symbol in sorted(frames):
        df = frames[symbol].reset_index(drop=True)
        dates = df["date"].astype(str).str[:10]
        for i in range(len(df)):
            day = dates.iloc[i]
            if day < start or day > end:
                continue
            upto = df.iloc[: i + 1]
            result = module.evaluate_entry(upto, params)
            if not result["passed"]:
                continue
            features = vi.compute_at(upto, symbol=symbol,
                                     instrument_token=(tokens or {}).get(symbol),
                                     verified_gaps=(verified_gaps or {}).get(symbol, ()),
                                     strategy=module.STRATEGY_NAME, params=params)
            comparison = features.get("strategy_comparison") or {}
            row = {
                "strategy": module.STRATEGY_NAME, "symbol": symbol,
                "instrument_token": (tokens or {}).get(symbol), "signal_date": day,
                "feature_version": features["feature_version"],
                "data_quality": features["data_quality"]["status"],
                **feature_record(features),
                "legacy_volume_pass": comparison.get("legacy_pass"),
            }
            if with_outcomes:
                row.update(forward_outcomes(df, i))
            rows.append(row)
    return rows


def attach_backtest_trades(rows: list[dict], module, frames: dict[str, pd.DataFrame], params: dict,
                           start: str, end: str) -> list[dict]:
    """Adds the PK-21 default-config backtest result of each signal (if it
    became a trade) as outcome columns. The simulation gets frames cut at
    `end`, as run_backtest does."""
    from backend.backtest import BacktestConfig, SymbolFrame, simulate

    sim_frames = {s: SymbolFrame(s, df[df["date"].astype(str).str[:10] <= end]) for s, df in frames.items()}
    sim = simulate(module, sim_frames, params, BacktestConfig.from_overrides(), start, end)
    trades = {(t["symbol"], t["signal_date"]): t for t in sim["trades"]}
    for row in rows:
        trade = trades.get((row["symbol"], row["signal_date"]))
        row[f"{OUTCOME_PREFIX}traded"] = trade is not None
        row[f"{OUTCOME_PREFIX}trade_net_r"] = trade["net_r_multiple"] if trade else None
        row[f"{OUTCOME_PREFIX}trade_exit_reason"] = trade["exit_reason"] if trade else None
    return rows


def write_csv(rows: list[dict], path: Path) -> list[str]:
    """Identity, then features, then every outcome_* column last."""
    if not rows:
        columns = [*IDENTITY_COLUMNS, *FEATURE_COLUMNS, "legacy_volume_pass"]
    else:
        keys = list(rows[0])
        columns = [k for k in keys if not k.startswith(OUTCOME_PREFIX)] + \
                  [k for k in keys if k.startswith(OUTCOME_PREFIX)]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return columns


# --------------------------------------------------------------------------
# CLI over a database copy
# --------------------------------------------------------------------------

def _open_copy(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_universe(conn: sqlite3.Connection) -> tuple[dict[str, pd.DataFrame], dict[str, int], dict[str, list],
                                                     list[dict]]:
    """NSE instruments with stored history (a token present in the NSE
    instrument cache — the same symbol's BSE series is a different token with
    different volume and is never mixed in). Flagged histories are excluded."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    status = {}
    if "ohlc_history_status" in tables:
        status = {r["instrument_token"]: dict(r) for r in conn.execute(
            "SELECT instrument_token, status, verified_gaps FROM ohlc_history_status")}
    instruments = conn.execute(
        "SELECT DISTINCT d.instrument_token, c.tradingsymbol AS symbol FROM ohlc_daily d "
        "JOIN instrument_cache c ON c.instrument_token = d.instrument_token AND c.exchange = 'NSE' "
        "ORDER BY c.tradingsymbol"
    ).fetchall()
    frames, tokens, verified, excluded = {}, {}, {}, []
    for inst in instruments:
        token, symbol = inst["instrument_token"], inst["symbol"]
        st = status.get(token)
        if st and st["status"] in ("SUSPECT", "NEEDS_REFRESH"):
            excluded.append({"symbol": symbol, "instrument_token": token, "status": st["status"]})
            continue
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM ohlc_daily WHERE instrument_token = ? ORDER BY date",
            (token,),
        ).fetchall()
        frames[symbol] = pd.DataFrame([dict(r) for r in rows])
        tokens[symbol] = token
        verified[symbol] = json.loads(st["verified_gaps"]) if st and st.get("verified_gaps") else []
    return frames, tokens, verified, excluded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.volume_research")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="volume features at every historical strategy signal, as CSV")
    export.add_argument("--db", required=True, type=Path, help="a COPY of kite_terminal.db (opened read-only)")
    export.add_argument("--out", required=True, type=Path)
    export.add_argument("--strategy", choices=sorted(STRATEGIES), action="append")
    export.add_argument("--start", default="2024-09-01")
    export.add_argument("--end", default="9999-12-31")
    export.add_argument("--with-trades", action="store_true", help="add PK-21 backtest trade outcomes")
    args = parser.parse_args(argv)

    conn = _open_copy(args.db)
    try:
        frames, tokens, verified, excluded = load_universe(conn)
    finally:
        conn.close()
    rows: list[dict] = []
    for name in args.strategy or sorted(STRATEGIES):
        module = STRATEGIES[name]
        params = dict(module.DEFAULT_PARAMS)
        found = signal_feature_rows(module, frames, params, args.start, args.end, tokens=tokens,
                                    verified_gaps=verified)
        if args.with_trades:
            found = attach_backtest_trades(found, module, frames, params, args.start, args.end)
        rows.extend(found)
    columns = write_csv(rows, args.out)
    print(f"{len(rows)} signal rows, {len(columns)} columns, {len(frames)} symbols -> {args.out}")
    for e in excluded:
        print(f"excluded {e['symbol']} ({e['instrument_token']}): history {e['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
