"""Local demo server: the real app, backed by a throwaway database full of
realistic synthetic data and the in-memory FakeKite — for exercising the UI
end-to-end (and taking screenshots) with no Zerodha session and no way to
touch the real account or the real database.

    .venv/Scripts/python.exe tests/demo_server.py [--port 8765]
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography.fernet import Fernet  # noqa: E402

# Set before backend.main runs load_dotenv(), which never overrides existing
# variables — so the real .env (and its keys) is never used by the demo.
os.environ["KITE_TERMINAL_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["KITE_API_KEY"] = "demo_api_key"
os.environ["KITE_API_SECRET"] = "demo_api_secret"

from fake_kite import FakeKite, make_candles  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))

# token, symbol, name, start price, daily drift %, sector
UNIVERSE = [
    (2953217, "TCS", "TATA CONSULTANCY SERV LT", 3350.0, -0.010, "IT"),
    (408065, "INFY", "INFOSYS", 1180.0, 0.030, "IT"),
    (738561, "RELIANCE", "RELIANCE INDUSTRIES", 1120.0, 0.032, "Energy"),
    (341249, "HDFCBANK", "HDFC BANK", 1420.0, 0.025, "Banking"),
    (857857, "SUNPHARMA", "SUN PHARMACEUTICAL IND L", 1300.0, 0.030, "Pharma"),
    (424961, "ITC", "ITC", 430.0, -0.005, "FMCG"),
    (2939649, "LT", "LARSEN & TOUBRO", 2900.0, 0.030, "Infra"),
    (2714625, "BHARTIARTL", "BHARTI AIRTEL", 1400.0, 0.040, "Telecom"),
]
USER_HOLDINGS = {"RELIANCE": (40, 1310.0), "INFY": (60, 1390.0), "ITC": (300, 402.0), "HDFCBANK": (25, 1650.0),
                 "TCS": (10, 3480.0)}


def build_fake(market) -> FakeKite:
    session = market.last_complete_session_date()
    candles, ltp, closes = {}, {}, {}
    for token, sym, _, start, drift, _ in UNIVERSE:
        series = make_candles(session, 760, start, daily_drift_pct=drift, volume=250_000)
        candles[token] = series
        closes[sym] = series[-1]["close"]
        ltp[f"NSE:{sym}"] = round(series[-1]["close"] * 1.004, 2)
    return FakeKite(margins_net=450_000.0, ltp=ltp, candles=candles), closes


def seed_holdings(fake: FakeKite, closes: dict) -> None:
    tokens = {sym: token for token, sym, *_ in UNIVERSE}
    rows = []
    # The app's own live positions (SUNPHARMA 22, INFY 20) are held at the broker too.
    held = dict(USER_HOLDINGS)
    held["SUNPHARMA"] = (22, 1498.0)
    held["INFY"] = (80, 1400.0)
    for sym, (qty, avg) in held.items():
        last = fake.ltp_map[f"NSE:{sym}"]
        prev = closes[sym]
        rows.append({
            "tradingsymbol": sym, "exchange": "NSE", "instrument_token": tokens[sym], "quantity": qty,
            "t1_quantity": 0, "average_price": avg, "last_price": last, "close_price": prev,
            "pnl": round((last - avg) * qty, 2), "day_change": round(last - prev, 2),
            "day_change_percentage": round((last - prev) / prev * 100, 2),
        })
    fake._holdings = rows


def _insert(conn, **fields) -> int:
    cols = ", ".join(fields)
    cur = conn.execute(f"INSERT INTO trade_tickets ({cols}) VALUES ({', '.join('?' * len(fields))})",
                       tuple(fields.values()))
    conn.execute("INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'created', ?)",
                 (cur.lastrowid, f"Ticket created (source={fields.get('source', 'manual')})"))
    return cur.lastrowid


def seed_database(db, fake, market) -> None:
    from backend import indicators, journal, risk_engine, strategy_engine
    from backend.accounts import get_default_account
    from backend.strategies import STRATEGIES

    session = market.last_complete_session_date()
    conn = db.get_connection()
    account = get_default_account(conn)
    aid = account["id"]

    conn.executemany(
        "INSERT INTO instrument_cache (instrument_token, tradingsymbol, name, exchange, segment, instrument_type, tick_size) "
        "VALUES (?, ?, ?, 'NSE', 'NSE', 'EQ', 0.05)",
        [(token, sym, name) for token, sym, name, *_ in UNIVERSE],
    )
    conn.executemany("INSERT OR IGNORE INTO sector_mapping (symbol, sector) VALUES (?, ?)",
                     [(sym, sector) for _, sym, _, _, _, sector in UNIVERSE])
    conn.execute(
        "INSERT INTO sessions (access_token, user_id, user_name, login_time, account_id) VALUES (?, ?, ?, ?, ?)",
        ("demo-token", "DT0001", "Demo Trader", datetime.now(IST).isoformat(), aid),
    )
    conn.execute("UPDATE execution_settings SET mode = 'dry_run' WHERE id = 1")
    for token, sym, *_ in UNIVERSE:
        conn.execute("INSERT INTO watchlist (account_id, symbol, instrument_token, notes) VALUES (?, ?, ?, ?)",
                     (aid, sym, token, "core swing universe" if sym in ("LT", "BHARTIARTL") else None))
        conn.executemany(
            "INSERT INTO ohlc_daily (instrument_token, symbol, date, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(token, sym, c["date"].strftime("%Y-%m-%d"), c["open"], c["high"], c["low"], c["close"], c["volume"])
             for c in fake.candles[token]],
        )
        conn.execute("INSERT INTO ohlc_meta (instrument_token, history_requested_from) VALUES (?, ?)",
                     (token, (session - timedelta(days=1100)).isoformat()))
    conn.executemany("UPDATE dashboard_panels SET visible = ?, size = ?, display_order = ? WHERE account_id = ? AND panel_type = ?", [
        (1, "large", 0, aid, "portfolio_summary"), (1, "medium", 1, aid, "holdings_table"),
        (1, "small", 2, aid, "todays_actions"), (1, "large", 3, aid, "watchlist_summary"),
        (1, "large", 4, aid, "positions_table"), (0, "large", 5, aid, "mf_table"),
    ])
    conn.commit()
    for token, sym, *_ in UNIVERSE:
        indicators.compute_and_store(token, sym)

    for name in ("momentum_breakout", "trend_pullback"):
        strategy_engine.save_config(conn, aid, name, True, dict(STRATEGIES[name].DEFAULT_PARAMS))

    signal = session.isoformat()
    ctx = risk_engine.build_portfolio_context(fake)

    def risk_checked(**fields) -> int:
        tid = _insert(conn, account_id=aid, status="draft", **fields)
        conn.commit()
        risk_engine.run_risk_checks_and_transition(conn, account, tid, fake, ctx=ctx)
        return tid

    # --- entry signals awaiting approval --------------------------------
    risk_checked(
        source="strategy", strategy_name="momentum_breakout", strategy_version=1, symbol="LT", exchange="NSE",
        instrument_token=2939649, transaction_type="BUY", quantity=21, entry_price=3612.15, entry_order_type="LIMIT",
        stop_loss=3480.30, target_1=3875.85, trailing_enabled=1, trailing_trigger="after_target_1",
        trailing_distance=98.90, product_type="CNC", use_gtt=1, risk_amount=2768.85, risk_reward_ratio=2.0,
        signal_date=signal, rule_checklist=json.dumps({"breakout": {"pass": True, "breakout_pct_above_level": 1.8}}),
        rationale="Momentum breakout: close ₹3,608.50 broke above 20-day high ₹3,545.20 on 2.4x avg volume, "
                  "trend confirmed (close above 200-day SMA ₹3,210.40).",
    )
    risk_checked(
        source="strategy", strategy_name="trend_pullback", strategy_version=1, symbol="BHARTIARTL", exchange="NSE",
        instrument_token=2714625, transaction_type="BUY", quantity=40, entry_price=1905.40, entry_order_type="LIMIT",
        stop_loss=1858.25, target_1=1999.70, trailing_enabled=1, trailing_trigger="after_target_1",
        trailing_distance=47.15, product_type="CNC", use_gtt=1, risk_amount=1886.0, risk_reward_ratio=2.0,
        signal_date=signal,
        rationale="Trend pullback: close ₹1,903.50 pulled back to touch the 20-EMA (₹1,871.30) and is now confirmed "
                  "recovering above it, with the 200-day uptrend still intact.",
    )
    # --- blocked by the risk engine today --------------------------------
    risk_checked(
        source="strategy", strategy_name="trend_pullback", strategy_version=1, symbol="TCS", exchange="NSE",
        instrument_token=2953217, transaction_type="BUY", quantity=24, entry_price=3134.60, entry_order_type="LIMIT",
        stop_loss=3052.10, target_1=3299.60, trailing_enabled=1, trailing_trigger="after_target_1",
        trailing_distance=82.50, product_type="CNC", use_gtt=1, risk_amount=1980.0, risk_reward_ratio=2.0,
        signal_date=signal,
        rationale="Trend pullback: close ₹3,131.45 recovered above its 20-EMA after a 3-day dip.",
    )

    # --- open live positions (placed by the app) -----------------------
    sun_gtt = fake.place_gtt("single", "SUNPHARMA", "NSE", [1452.0], 1560.0, [])["trigger_id"]
    sun = _insert(conn, account_id=aid, source="strategy", strategy_name="momentum_breakout", strategy_version=1,
                  symbol="SUNPHARMA", exchange="NSE", instrument_token=857857, transaction_type="BUY", quantity=22,
                  entry_price=1498.0, entry_order_type="LIMIT", stop_loss=1452.0, target_1=1590.0,
                  trailing_enabled=1, trailing_trigger="after_target_1", trailing_distance=38.0,
                  current_stop_loss=1452.0, product_type="CNC", use_gtt=1, risk_amount=1012.0,
                  risk_reward_ratio=2.0, status="executed", mode="live", kite_order_id="250000000101",
                  kite_gtt_id=sun_gtt, order_variety="regular", entry_fill_status="filled", filled_price=1497.6,
                  executed_at=(datetime.utcnow() - timedelta(days=18)).strftime("%Y-%m-%d %H:%M:%S"),
                  rationale="Momentum breakout above the 20-day high on 2.1x volume.")
    infy_gtt = fake.place_gtt("single", "INFY", "NSE", [1402.0], 1500.0, [])["trigger_id"]
    infy = _insert(conn, account_id=aid, source="strategy", strategy_name="trend_pullback", strategy_version=1,
                   symbol="INFY", exchange="NSE", instrument_token=408065, transaction_type="BUY", quantity=20,
                   entry_price=1446.40, entry_order_type="LIMIT", stop_loss=1402.0, target_1=1535.20,
                   trailing_enabled=1, trailing_trigger="after_target_1", trailing_distance=34.0,
                   current_stop_loss=1402.0, product_type="CNC", use_gtt=1, risk_amount=888.0,
                   risk_reward_ratio=2.0, status="executed", mode="live", kite_order_id="250000000102",
                   kite_gtt_id=infy_gtt, order_variety="amo", entry_fill_status="filled", filled_price=1446.40,
                   executed_at=(datetime.utcnow() - timedelta(days=6)).strftime("%Y-%m-%d %H:%M:%S"),
                   rationale="Trend pullback to the 20-EMA inside a 200-day uptrend.")
    conn.execute("INSERT INTO stop_adjustment_proposals (ticket_id, current_stop, proposed_stop, reason) "
                 "VALUES (?, 1402.0, 1446.40, 'Position up 1.12R (trigger 1.0R), tightening stop to breakeven')",
                 (infy,))
    # --- exit signal awaiting approval ---------------------------------
    _insert(conn, account_id=aid, source="strategy", strategy_name="momentum_breakout_exit", strategy_version=1,
            symbol="SUNPHARMA", exchange="NSE", instrument_token=857857, transaction_type="SELL", quantity=22,
            entry_price=1561.05, entry_order_type="MARKET", stop_loss=1452.0, target_1=1590.0, product_type="CNC",
            use_gtt=1, risk_amount=2398.0, risk_reward_ratio=0.27, status="pending_confirmation",
            closes_ticket_id=sun,
            risk_check_results=json.dumps([{"rule": "exit_or_reduction", "label": "Exit / Reduction Validation", "passed": True,
                                            "skipped": False, "actual_value": None, "limit_value": None,
                                            "unit": "pct", "message": "Exit ticket — closes a specific tracked position, position-sizing checks not applicable"}]),
            rule_checklist=json.dumps({"time_exit": {"pass": True, "days_held": 16, "limit_days": 15}}),
            rationale="held 16 trading days without hitting stop or target (limit 15)")

    # --- closed dry-run trades for the Journal -------------------------
    history = [
        ("RELIANCE", "momentum_breakout", 1188.0, 1261.5, 30, 95), ("HDFCBANK", "trend_pullback", 1510.0, 1487.2, 25, 88),
        ("LT", "momentum_breakout", 3050.0, 3244.0, 12, 80), ("ITC", "trend_pullback", 418.0, 409.1, 180, 71),
        ("INFY", "momentum_breakout", 1322.0, 1397.4, 28, 60), ("BHARTIARTL", "trend_pullback", 1702.0, 1768.0, 22, 47),
        ("SUNPHARMA", "momentum_breakout", 1384.0, 1362.5, 26, 33), ("TCS", "trend_pullback", 3290.0, 3402.0, 11, 21),
        ("RELIANCE", "trend_pullback", 1295.0, 1331.2, 30, 12),
    ]
    tokens = {sym: token for token, sym, *_ in UNIVERSE}
    for sym, strat, entry, exit_, qty, days_ago in history:
        opened = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        closed = (datetime.utcnow() - timedelta(days=days_ago - 6)).strftime("%Y-%m-%d %H:%M:%S")
        stop = round(entry * 0.97, 2)
        buy = _insert(conn, account_id=aid, source="strategy", strategy_name=strat, strategy_version=1, symbol=sym,
                      exchange="NSE", instrument_token=tokens[sym], transaction_type="BUY", quantity=qty,
                      entry_price=entry, entry_order_type="LIMIT", stop_loss=stop, target_1=round(entry * 1.06, 2),
                      product_type="CNC", use_gtt=1, risk_amount=round((entry - stop) * qty, 2),
                      risk_reward_ratio=2.0, status="executed", mode="dry_run", entry_fill_status="filled",
                      filled_price=entry, executed_at=opened, closed_at=closed, created_at=opened,
                      rationale=f"{strat.replace('_', ' ')} signal")
        sell = _insert(conn, account_id=aid, source="strategy", strategy_name=f"{strat}_exit", symbol=sym,
                       exchange="NSE", instrument_token=tokens[sym], transaction_type="SELL", quantity=qty,
                       entry_price=exit_, entry_order_type="MARKET", stop_loss=stop, target_1=round(entry * 1.06, 2),
                       product_type="CNC", use_gtt=1, status="executed", mode="dry_run", closes_ticket_id=buy,
                       entry_fill_status="filled", filled_price=exit_, executed_at=closed, created_at=closed,
                       rule_checklist=json.dumps({"thesis_invalidation": {"pass": exit_ < entry}, "time_exit": {"pass": exit_ >= entry}}),
                       rationale="exit signal")
        conn.commit()
        journal.record_closed_trade(conn, sell)

    # --- autopilot history (today's session already done, so nothing auto-runs on start) ---
    for run_date, status, summary in (
        ((session - timedelta(days=1)).isoformat(), "skipped",
         {"reason": "No candle available yet (exchange holiday) — nothing new to scan."}),
        (session.isoformat(), "success",
         {"scan": {"enabled": True, "by_strategy": {
             "momentum_breakout": {"entry_tickets": [1], "exit_tickets": [5]},
             "trend_pullback": {"entry_tickets": [2, 3], "exit_tickets": []}}},
          "expired_signals": [], "symbols_synced": 8}),
    ):
        conn.execute(
            "INSERT INTO automation_runs (account_id, run_date, trigger, status, summary, finished_at) "
            "VALUES (?, ?, 'scheduled', ?, ?, datetime('now'))",
            (aid, run_date, status, json.dumps(summary)),
        )
    conn.commit()
    conn.close()


def _serve_frontend_from(app, frontend_dir: Path) -> None:
    """Swap in a different frontend build (e.g. an older one, for a
    before/after comparison) without touching backend/main.py."""
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    for route in app.routes:
        if getattr(route, "path", None) == "/static":
            route._base_app = route.app = StaticFiles(directory=frontend_dir)
    app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) != "/"]
    app.add_api_route("/", lambda: FileResponse(frontend_dir / "index.html"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--frontend-dir", default=None, help="serve another frontend build instead of ./frontend")
    args = parser.parse_args()

    workdir = Path(args.workdir or tempfile.mkdtemp(prefix="kite-demo-"))
    workdir.mkdir(parents=True, exist_ok=True)

    from backend import crypto, db, instruments, kite_client, market, ui_prefs
    db.DB_PATH = workdir / "demo.db"
    crypto.ENV_PATH = workdir / ".env"
    ui_prefs.PREFS_PATH = workdir / "ui_prefs.json"
    if db.DB_PATH.exists():
        db.DB_PATH.unlink()

    fake, closes = build_fake(market)
    seed_holdings(fake, closes)
    kite_client.get_kite = lambda account_id=None: fake
    instruments.refresh_instrument_cache_if_stale = lambda *a, **k: False
    from backend.ticker import ticker_manager
    ticker_manager.start = lambda *a, **k: None

    db.init_db()
    seed_database(db, fake, market)

    import uvicorn
    from backend.main import app
    if args.frontend_dir:
        _serve_frontend_from(app, Path(args.frontend_dir))
    print(f"Demo server on http://127.0.0.1:{args.port}  (db: {db.DB_PATH})", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
