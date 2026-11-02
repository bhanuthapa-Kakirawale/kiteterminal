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
# NET-1: likewise, a real .env's KITE_REST_PROXY must not route the demo's
# (fake) Kite client. Set but empty means "direct" and blocks load_dotenv.
os.environ["KITE_REST_PROXY"] = ""

from fake_kite import FakeKite, make_candles  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))

# token, symbol, name, start price, daily drift %, sector
# `sector` None means the symbol is deliberately left OUT of sector_mapping, so
# the Signal Detail view's risk section has a real example of the sector check
# being SKIPPED rather than passed (it must never render as a pass).
UNIVERSE = [
    (2953217, "TCS", "TATA CONSULTANCY SERV LT", 3350.0, 0.030, "IT"),
    (408065, "INFY", "INFOSYS", 1180.0, 0.030, "IT"),
    (738561, "RELIANCE", "RELIANCE INDUSTRIES", 1120.0, 0.032, "Energy"),
    (341249, "HDFCBANK", "HDFC BANK", 1420.0, 0.025, "Banking"),
    (857857, "SUNPHARMA", "SUN PHARMACEUTICAL IND L", 1300.0, 0.030, "Pharma"),
    (424961, "ITC", "ITC", 430.0, -0.005, "FMCG"),
    (2939649, "LT", "LARSEN & TOUBRO", 2900.0, 0.030, "Infra"),
    (2714625, "BHARTIARTL", "BHARTI AIRTEL", 1400.0, 0.040, "Telecom"),
    # Steep drift is required for the oversold setup (price must stay above
    # its SMA-200 through a 9-day sell-off), so the START price is scaled
    # down to keep the compounded end price in a realistic range.
    (2800641, "DIVISLAB", "DIVIS LABORATORIES", 128.0, 0.450, "Pharma"),
    (4508673, "NEWLISTCO", "NEW LISTING CO", 640.0, 0.120, None),
]
# TCS is held in size on purpose: its signal below is meant to be BLOCKED by
# the single-stock-exposure risk check, so the Approvals inbox always has a
# risk-rejected card whose failed check can be inspected.
USER_HOLDINGS = {"RELIANCE": (40, 1310.0), "INFY": (60, 1390.0), "ITC": (300, 402.0), "HDFCBANK": (25, 1650.0),
                 "TCS": (260, 3480.0)}

# Which strategy each symbol's candle series is shaped to trigger. Four
# momentum candidates against a cap of three means one qualifying signal is
# always dropped, so the detail view's "ranked N of 3 kept, from 4 candidates
# that qualified" line has something real to report.
SETUPS = {
    "LT": "breakout", "NEWLISTCO": "breakout", "TCS": "breakout", "HDFCBANK": "breakout",
    "BHARTIARTL": "pullback",
    "DIVISLAB": "oversold",
}


def _avg_volume(candles, period=20):
    return sum(c["volume"] for c in candles[-(period + 1):-1]) / period


def shape_breakout(candles):
    """Final bar closes decisively above the prior Donchian high on heavy volume.

    Lookback and volume multiple are read from the strategy module rather
    than hardcoded. The 2026-09 parameter revision raised both, and hardcoded
    values here would silently stop producing signals -- leaving the demo
    server, and the E2E test that runs against it, with an empty inbox."""
    from backend.strategies import momentum_breakout as mb

    lookback = mb.DEFAULT_PARAMS["donchian_lookback"]
    prior_max = max(c["close"] for c in candles[-(lookback + 1):-1])
    last = candles[-1]
    last["close"] = round(prior_max * 1.028, 2)
    last["open"] = round(prior_max * 1.002, 2)
    last["high"] = round(last["close"] * 1.005, 2)
    last["low"] = round(last["open"] * 0.997, 2)
    last["volume"] = int(_avg_volume(candles) * (mb.DEFAULT_PARAMS["volume_multiple"] + 0.6))
    return candles


def shape_pullback(candles):
    """Second-to-last bar dips to its own 20-EMA; the final bar closes back
    above both that EMA and the previous close, on heavy volume."""
    from backend.strategies import trend_pullback as tp

    dip = candles[-2]
    dip["low"] = round(dip["low"] * 0.94, 2)
    dip["close"] = round(dip["close"] * 0.97, 2)
    dip["open"] = round(dip["close"] * 1.01, 2)
    last = candles[-1]
    last["open"] = dip["close"]
    last["close"] = round(dip["close"] * 1.09, 2)
    last["high"] = round(last["close"] * 1.004, 2)
    last["low"] = round(dip["close"] * 0.998, 2)
    # The recovery bar must clear the participation filter added in 2026-09.
    last["volume"] = int(_avg_volume(candles) * (tp.DEFAULT_PARAMS["volume_multiple"] + 0.6))
    return candles


def shape_oversold(candles):
    """A steep long-run uptrend (so the hard SMA-200 filter still passes)
    followed by a 9-day sell-off that drives RSI-14 under 35, then one up-bar
    so RSI is turning back up. The two halves are balanced deliberately: a
    deeper dip drives RSI lower but also drags price below its own SMA-200, at
    which point this strategy's hard trend filter rejects the candidate."""
    dip_days = 9
    price = candles[-(dip_days + 2)]["close"]
    for candle in candles[-(dip_days + 1):-1]:
        price = round(price * 0.982, 2)
        candle["open"] = round(price * 1.004, 2)
        candle["close"] = price
        candle["high"] = round(price * 1.006, 2)
        candle["low"] = round(price * 0.994, 2)
    last = candles[-1]
    last["open"] = price
    last["close"] = round(price * 1.008, 2)
    last["high"] = round(last["close"] * 1.004, 2)
    last["low"] = round(price * 0.998, 2)
    return candles


_SHAPERS = {"breakout": shape_breakout, "pullback": shape_pullback, "oversold": shape_oversold}


def build_fake(market) -> FakeKite:
    session = market.last_complete_session_date()
    candles, ltp, closes = {}, {}, {}
    for token, sym, _, start, drift, _ in UNIVERSE:
        series = make_candles(session, 760, start, daily_drift_pct=drift, volume=250_000)
        # Shape the tail so the scan below produces genuine signals from this
        # data, rather than the signals being hand-written constants that
        # happen to sit nowhere near the candles a chart would plot.
        shaper = _SHAPERS.get(SETUPS.get(sym))
        if shaper:
            series = shaper(series)
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
                     [(sym, sector) for _, sym, _, _, _, sector in UNIVERSE if sector])
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

    # All three strategies are enabled so the Signal Detail view can be
    # explored against every overlay set it knows how to draw — including
    # rsi_mean_reversion's RSI subpanel, which has no equivalent in the other
    # two.
    for name in ("momentum_breakout", "trend_pullback", "rsi_mean_reversion"):
        strategy_engine.save_config(conn, aid, name, True, dict(STRATEGIES[name].DEFAULT_PARAMS))

    signal = session.isoformat()
    ctx = risk_engine.build_portfolio_context(fake)

    def risk_checked(**fields) -> int:
        tid = _insert(conn, account_id=aid, status="draft", **fields)
        conn.commit()
        risk_engine.run_risk_checks_and_transition(conn, account, tid, fake, ctx=ctx)
        return tid

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
    conn.commit()

    # --- entry signals, produced by the REAL scan ----------------------
    # Running the actual scanner rather than inserting hand-written tickets is
    # what makes the demo usable for this feature: the tickets come back with
    # a genuine entry_evaluation record, a rationale generated from it, a
    # resolved Kite chart URL, and price levels that actually sit on the
    # candles the chart will plot. Hand-written constants looked fine on a
    # card and fell apart the moment anything drew them against real bars.
    #
    # It runs AFTER the open positions above so the scanner's
    # "don't re-propose a symbol I already hold" guard behaves realistically.
    for name in ("momentum_breakout", "trend_pullback", "rsi_mean_reversion"):
        module = STRATEGIES[name]
        diagnostics: dict = {}
        created = strategy_engine.run_entry_scan(
            module, conn, account, fake, dict(module.DEFAULT_PARAMS), ctx=ctx, diagnostics=diagnostics)
        print(f"  seeded {name}: {len(created)} signal(s) "
              f"({diagnostics.get('qualified')} qualified, {diagnostics.get('kept')} kept)", flush=True)
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


def seed_lifecycle_fixtures(db, fake) -> None:
    """PK-19 (--lifecycle-fixtures, off by default): one app position in each
    lifecycle state the Positions view must show, for browser validation.
    Seeded AFTER everything above, and only on request, so the default demo —
    and tests/e2e_ui.py against it — is unchanged. Each live state is backed
    by a matching FakeKite order / GTT, so the demo's own background loops
    (order monitor, GTT watcher, reconciliation) keep it stable."""
    from backend import journal
    from backend.accounts import get_default_account

    tokens = {sym: token for token, sym, *_ in UNIVERSE}
    conn = db.get_connection()
    aid = get_default_account(conn)["id"]
    ago = lambda days: (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731

    def gtt(sym, qty, stop, last):
        legs = [{"transaction_type": "SELL", "quantity": qty, "order_type": "LIMIT", "product": "CNC",
                 "price": round(stop * 0.995, 2)}]
        return fake.place_gtt("single", sym, "NSE", [stop], last, legs)["trigger_id"]

    def position(sym, qty, price, **fields):
        base = dict(account_id=aid, source="manual", symbol=sym, exchange="NSE", instrument_token=tokens[sym],
                    transaction_type="BUY", quantity=qty, entry_price=price, entry_order_type="LIMIT",
                    stop_loss=round(price * 0.96, 2), target_1=round(price * 1.08, 2),
                    current_stop_loss=round(price * 0.96, 2), product_type="CNC", use_gtt=1,
                    risk_amount=round(price * 0.04 * qty, 2), risk_reward_ratio=2.0, status="executed", mode="live",
                    executed_at=ago(3), entry_fill_status="filled", filled_quantity=qty, filled_price=price,
                    filled_at=ago(3), order_variety="regular", rationale=f"PK-19 lifecycle fixture ({sym})")
        base.update(fields)
        return _insert(conn, **base)

    def exit_of(pid, sym, qty, price, **fields):
        base = dict(account_id=aid, source="manual", symbol=sym, exchange="NSE", instrument_token=tokens[sym],
                    transaction_type="SELL", quantity=qty, entry_price=price, entry_order_type="MARKET",
                    stop_loss=round(price * 0.9, 2), target_1=round(price * 1.1, 2), product_type="CNC", use_gtt=1,
                    status="executed", mode="live", closes_ticket_id=pid, executed_at=ago(0),
                    rationale="PK-19 lifecycle fixture exit")
        base.update(fields)
        return _insert(conn, **base)

    def finding(ticket_id, code, detail, **fields):
        conn.execute(
            "INSERT INTO reconciliation_log (ticket_id, check_type, expected, actual, mismatch, detail, issue_code, "
            "severity, symbol, gtt_id, expected_quantity, broker_quantity, suggested_action) "
            "VALUES (?, 'order_status', ?, ?, 1, ?, ?, 'urgent', ?, ?, ?, ?, 'CHECK KITE')",
            (ticket_id, fields.get("expected", "protected"), fields.get("actual", code), detail, code,
             fields.get("symbol"), fields.get("gtt_id"), fields.get("expected_quantity"),
             fields.get("broker_quantity")))

    # ENTRY_PARTIAL — LT: 40 of 100 filled, the order still working.
    order = fake.add_broker_order("LT", "BUY", 100, status="OPEN", filled_quantity=40, price=2990.0,
                                  average_price=2990.0)
    position("LT", 100, 2990.0, kite_order_id=order, kite_gtt_id=gtt("LT", 40, 2870.0, 2990.0),
             entry_fill_status="pending", filled_quantity=40, filled_price=None, filled_at=None)
    fake.set_holding("LT", 40, average_price=2990.0)
    # ENTRY_OPEN — NEWLISTCO: order at the broker, nothing filled.
    order = fake.add_broker_order("NEWLISTCO", "BUY", 50, status="OPEN", price=700.0)
    position("NEWLISTCO", 50, 700.0, kite_order_id=order, kite_gtt_id=gtt("NEWLISTCO", 50, 672.0, 700.0),
             entry_fill_status="pending", filled_quantity=0, filled_price=None, filled_at=None)
    # EXITING — BHARTIARTL: 30 held, exit order working, nothing sold.
    pid = position("BHARTIARTL", 30, 1690.0, kite_gtt_id=gtt("BHARTIARTL", 30, 1622.0, 1690.0))
    order = fake.add_broker_order("BHARTIARTL", "SELL", 30, status="OPEN", price=1750.0)
    exit_of(pid, "BHARTIARTL", 30, 1750.0, kite_order_id=order, entry_fill_status="pending", filled_quantity=0)
    fake.set_holding("BHARTIARTL", 30, average_price=1690.0)
    # EXIT_PARTIAL — HDFCBANK: 40 bought, a working exit sold 15, 25 remain.
    pid = position("HDFCBANK", 40, 1600.0, kite_gtt_id=gtt("HDFCBANK", 25, 1536.0, 1600.0))
    order = fake.add_broker_order("HDFCBANK", "SELL", 40, status="OPEN", filled_quantity=15, price=1640.0,
                                  average_price=1640.0)
    exit_of(pid, "HDFCBANK", 40, 1640.0, kite_order_id=order, entry_fill_status="pending", filled_quantity=15)
    # OPEN, UNPROTECTED — ITC: no protective GTT.
    pid = position("ITC", 100, 440.0, kite_gtt_id=None)
    finding(pid, "PROTECTION_MISSING", "ITC: 100 share(s) are open but it has NO protective GTT. Place a stop-loss "
            "in Kite.", symbol="ITC", expected_quantity=100, broker_quantity=0)
    # OPEN, GTT QUANTITY MISMATCH — RELIANCE: holds 30, the finding says the GTT covered 50.
    gtt_id = gtt("RELIANCE", 30, 1260.0, 1310.0)
    pid = position("RELIANCE", 30, 1310.0, kite_gtt_id=gtt_id)
    finding(pid, "GTT_QUANTITY_TOO_HIGH", "RELIANCE: PROTECTION QUANTITY MISMATCH — protective GTT is OVERSIZED: it "
            "covers 50 share(s) but the position holds 30.", symbol="RELIANCE", gtt_id=gtt_id, expected_quantity=30,
            broker_quantity=50)
    # SUBMISSION_UNKNOWN (entry) — DIVISLAB: the broker call's outcome is unknown.
    position("DIVISLAB", 20, 190.0, status="confirmed", kite_gtt_id=None, executed_at=None, order_variety=None,
             entry_fill_status=None, filled_quantity=None, filled_price=None, filled_at=None,
             submission_state="unknown", order_tag="ktdemo1", submitted_at=ago(0))
    # SUBMISSION_UNKNOWN (exit) — TCS: 10 held, the exit's broker evidence is ambiguous.
    pid = position("TCS", 10, 3400.0, kite_gtt_id=gtt("TCS", 10, 3264.0, 3400.0))
    exit_of(pid, "TCS", 10, 3450.0, status="confirmed", executed_at=None, submission_state="needs_review",
            order_tag="ktdemo2", submitted_at=ago(0))
    # OPEN, DRY RUN — INFY: a simulated position; its mode is its own.
    position("INFY", 12, 1450.0, mode="dry_run", kite_gtt_id=None, order_variety=None)
    # CLOSED — SUNPHARMA: a live round trip closed on its exit fill, journaled.
    pid = position("SUNPHARMA", 15, 1420.0, kite_gtt_id=None, executed_at=ago(9), filled_at=ago(9),
                   closed_at=ago(1))
    sell = exit_of(pid, "SUNPHARMA", 15, 1500.0, entry_fill_status="filled", filled_quantity=15,
                   filled_price=1500.0, filled_at=ago(1), executed_at=ago(1), kite_order_id="250000000999")
    conn.commit()
    journal.record_closed_trade(conn, sell)
    conn.commit()
    conn.close()
    print("  seeded PK-19 lifecycle fixtures", flush=True)


def install_paper_controls(app, fake) -> None:
    """PK-20, opt-in (--paper-controls), demo server only — these routes do
    not exist in backend/main.py. They drive the paper broker's market for a
    browser rehearsal: set a symbol's simulated LTP (FakeKite is the demo's
    market-data source), run one paper cycle now instead of waiting for the
    background loops, and make a working paper order fill progressively."""
    from fastapi import Body, HTTPException
    from backend import order_monitor, paper_broker, trailing_engine

    # With these controls the paper exchange advances only on demand
    # (POST /api/demo/paper/cycle), so a rehearsal can observe every state;
    # the background order-monitor loop keeps following live orders.
    run_paper_cycle = order_monitor.check_paper_orders
    order_monitor.check_paper_orders = lambda *a, **k: {"skipped": "demo: paper cycles run on demand"}

    @app.post("/api/demo/paper/ltp")
    def demo_paper_ltp(body: dict = Body(...)):
        key = f"{body.get('exchange', 'NSE')}:{str(body['symbol']).upper()}"
        fake.ltp_map[key] = float(body["price"])
        return {"data": {"key": key, "price": fake.ltp_map[key]}}

    @app.post("/api/demo/paper/cycle")
    def demo_paper_cycle():
        result = run_paper_cycle(fake)
        trailing_engine._tick_sync()
        return {"data": result}

    @app.post("/api/demo/paper/fill-plan")
    def demo_paper_fill_plan(body: dict = Body(...)):
        try:
            paper_broker.set_fill_plan(str(body["order_id"]), list(body["plan"]))
        except ValueError as e:
            raise HTTPException(400, {"error_code": "BAD_FILL_PLAN", "message": str(e)})
        return {"data": {"order_id": body["order_id"], "plan": body["plan"]}}


def install_review_controls(app, fake) -> None:
    """SEC-1 P1-D browser validation, opt-in (--review-controls), demo server
    only — these routes do not exist in backend/main.py. They let a
    Playwright run control what a real session cannot be scripted to do:

    - POST /api/demo/clock pins the server's one trading clock
      (trading_calendar.now_ist), so the review window, market hours and AMO
      timing all follow it. {"time": "11:00"} pins that IST time on the
      trading day after the session the demo data was seeded for (its
      strategy signals stay current), {"ist": "2026-11-03T11:00:00"} pins an
      exact instant, {"ist": null} restores the real clock. The answer gives
      the pinned instant, for the browser's own clock.
    - GET /demo/kite-login runs the REAL kite_client.exchange_request_token
      with a fake KiteConnect: the real session replacement (and the P0-C
      LIVE disarm) happens, without a Kite round trip. Like /callback it then
      issues the terminal credential cookie (SEC-1 P1-F). A GET, because
      after a logout there is no credential to send a POST with.
    - POST /api/demo/tamper/{ticket_id} changes a pending ticket's stored
      quantity behind the app's back, in this throwaway database.
    - POST /api/demo/holding-loss {"symbol": "ITC", "pct": 12} marks a
      FakeKite holding that far below its average cost, so the dashboard
      raises the per-stock loss alert whose Quick Action offers a BUY.
    """
    from datetime import datetime as _dt
    from fastapi import Body, HTTPException, Request
    from fastapi.responses import JSONResponse
    from backend import db, kite_client, security, trading_calendar

    real_now = trading_calendar.now_ist
    pinned = {"at": None}
    # The demo's candles end at the session completed when it started; the
    # trading day after it is when those signals are current.
    trade_day = trading_calendar.next_trading_day(trading_calendar.latest_completed_session(real_now()))

    def demo_now():
        return pinned["at"] if pinned["at"] is not None else real_now()
    trading_calendar.now_ist = demo_now

    @app.post("/api/demo/clock")
    def demo_clock(body: dict = Body(...)):
        try:
            if body.get("time") is not None:
                hh, mm = (int(x) for x in str(body["time"]).split(":"))
                pinned["at"] = _dt(trade_day.year, trade_day.month, trade_day.day, hh, mm, tzinfo=trading_calendar.IST)
            elif body.get("ist") is not None:
                parsed = _dt.fromisoformat(str(body["ist"]))
                pinned["at"] = parsed.replace(tzinfo=trading_calendar.IST) if parsed.tzinfo is None else parsed
            else:
                pinned["at"] = None
        except ValueError:
            raise HTTPException(400, {"error_code": "BAD_CLOCK", "message": "time HH:MM or ist ISO datetime"})
        return {"data": {"now_ist": demo_now().isoformat(), "trade_day": trade_day.isoformat()}}

    class _FakeKiteConnect:
        def __init__(self, api_key, **kwargs):
            self.api_key = api_key

        def generate_session(self, request_token, api_secret):
            return {"access_token": "demo-token", "public_token": "demo-public", "user_id": "DT0001",
                    "user_name": "Demo Trader"}

        def set_access_token(self, token):
            pass

    @app.get("/demo/kite-login")
    def demo_kite_login(request: Request):
        # Mirrors /callback: the session replacement, then this launch's
        # terminal credential (SEC-1 P1-F) for the browser that "logged in".
        kite_client.KiteConnect = _FakeKiteConnect
        kite_client.exchange_request_token("demo-request-token")
        response = JSONResponse({"data": {"logged_in": True}})
        security.set_credential_cookie(response, security.issue_terminal_credential(), secure=False)
        return response

    @app.post("/api/demo/holding-loss")
    def demo_holding_loss(body: dict = Body(...)):
        symbol, pct = str(body["symbol"]).upper(), float(body.get("pct", 12))
        holding = next((h for h in fake._holdings if h["tradingsymbol"] == symbol), None)
        if holding is None:
            raise HTTPException(404, {"error_code": "NOT_HELD", "message": f"{symbol} is not a demo holding"})
        last = round(holding["average_price"] * (1 - pct / 100), 2)
        holding["last_price"] = last
        holding["pnl"] = round((last - holding["average_price"]) * holding["quantity"], 2)
        fake.ltp_map[f"NSE:{symbol}"] = last
        return {"data": {"symbol": symbol, "last_price": last}}

    @app.post("/api/demo/tamper/{ticket_id}")
    def demo_tamper(ticket_id: int):
        conn = db.get_connection()
        try:
            changed = conn.execute(
                "UPDATE trade_tickets SET quantity = quantity + 1 WHERE id = ? AND status = 'pending_confirmation'",
                (ticket_id,)).rowcount
            conn.commit()
        finally:
            conn.close()
        return {"data": {"tampered": changed == 1}}


def install_demo_authorization(app) -> None:
    """SEC-1 P1-F, demo server only (never in backend/main.py): the demo has
    no Kite login, so GET /demo/authorize stands in for the /callback that
    issues this launch's terminal credential cookie, then opens the UI. Open
    it once per browser; a restart of the demo needs it again, exactly like
    a restarted terminal needs a fresh Zerodha login. It hands the
    credential to whatever asks — acceptable only because this process holds
    synthetic data and a FakeKite."""
    from fastapi.responses import RedirectResponse
    from backend import security

    @app.get("/demo/authorize")
    def demo_authorize():
        response = RedirectResponse(url="/")
        security.set_credential_cookie(response, security.issue_terminal_credential(), secure=False)
        return response


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
    parser.add_argument("--lifecycle-fixtures", action="store_true",
                        help="PK-19: also seed one app position per lifecycle state (off by default)")
    parser.add_argument("--paper-controls", action="store_true",
                        help="PK-20: demo-only routes to set simulated LTPs and run paper cycles (off by default)")
    parser.add_argument("--market-open", action="store_true",
                        help="pin the NSE clock to 'open' (a deterministic paper rehearsal at any hour)")
    parser.add_argument("--review-controls", action="store_true",
                        help="SEC-1 P1-D: demo-only routes to pin the trading clock, simulate a new Kite login and "
                             "tamper with a ticket (off by default)")
    args = parser.parse_args()

    workdir = Path(args.workdir or tempfile.mkdtemp(prefix="kite-demo-"))
    workdir.mkdir(parents=True, exist_ok=True)

    from backend import crypto, db, instruments, kite_client, market, ui_prefs
    db.DB_PATH = workdir / "demo.db"
    crypto.ENV_PATH = workdir / ".env"
    ui_prefs.PREFS_PATH = workdir / "ui_prefs.json"
    if db.DB_PATH.exists():
        db.DB_PATH.unlink()

    if args.market_open:
        market.is_market_open = lambda at=None: True
    fake, closes = build_fake(market)
    seed_holdings(fake, closes)
    kite_client.get_kite = lambda account_id=None: fake
    instruments.refresh_instrument_cache_if_stale = lambda *a, **k: False
    from backend.ticker import ticker_manager
    ticker_manager.start = lambda *a, **k: None

    db.init_db()
    seed_database(db, fake, market)
    if args.lifecycle_fixtures:
        seed_lifecycle_fixtures(db, fake)

    import uvicorn
    from backend.main import app
    if args.frontend_dir:
        _serve_frontend_from(app, Path(args.frontend_dir))
    install_demo_authorization(app)
    if args.review_controls:
        install_review_controls(app, fake)
    if args.paper_controls:
        install_paper_controls(app, fake)
    print(f"Demo server on http://127.0.0.1:{args.port}  (db: {db.DB_PATH})", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
