"""Coverage for the Signal Detail view's backend: the chart/evidence payload
endpoint, the Kite chart-link resolver, and the structured entry-evaluation
record the Approvals card and the detail panel both render from.

Also closes the gap TESTING.md §1.5 recorded against `rsi_mean_reversion`,
which previously had no dedicated entry/exit unit test of its own.
"""

import json
from datetime import date, timedelta

import pandas as pd
import pytest

from conftest import account_id, add_watch, last_weekday, make_candles, seed_ohlc, ticket_row

TCS_TOKEN = 2953217
INFY_TOKEN = 408065


# ---------------------------------------------------------------- fixtures / helpers

def _breakout_candles(end=None, n=320):
    """A clean momentum_breakout setup: a long uptrend, then a final bar that
    closes decisively above the prior Donchian high on heavy volume.

    Both the lookback and the volume multiple are read from the module's own
    DEFAULT_PARAMS rather than hardcoded, so a future parameter revision
    cannot silently turn this fixture into a non-signal and make the tests
    pass for the wrong reason."""
    from backend.strategies import momentum_breakout

    lookback = momentum_breakout.DEFAULT_PARAMS["donchian_lookback"]
    vol_mult = momentum_breakout.DEFAULT_PARAMS["volume_multiple"]
    candles = make_candles(end or last_weekday(date.today()), n, 1000.0, daily_drift_pct=0.12)
    prior_max = max(c["close"] for c in candles[-(lookback + 1):-1])
    last = candles[-1]
    last["close"] = round(prior_max * 1.03, 2)
    last["high"] = round(last["close"] * 1.004, 2)
    avg_vol = sum(c["volume"] for c in candles[-21:-1]) / 20
    last["volume"] = int(avg_vol * (vol_mult + 0.5))
    return candles


def _pullback_candles(end=None, n=320):
    """A trend_pullback setup: an uptrend whose second-to-last bar dips to its
    own 20-EMA, with the final bar closing back above both that EMA and the
    previous close."""
    candles = make_candles(end or last_weekday(date.today()), n, 1000.0, daily_drift_pct=0.12)
    from backend.strategies import trend_pullback

    dip = candles[-2]
    dip["low"] = round(dip["low"] * 0.94, 2)
    dip["close"] = round(dip["close"] * 0.97, 2)
    last = candles[-1]
    last["close"] = round(dip["close"] * 1.05, 2)
    last["high"] = round(last["close"] * 1.004, 2)
    # The recovery bar must now also clear the participation filter.
    avg_vol = sum(c["volume"] for c in candles[-21:-1]) / 20
    last["volume"] = int(avg_vol * (trend_pullback.DEFAULT_PARAMS["volume_multiple"] + 0.5))
    return candles


def _oversold_candles(end=None, n=320):
    """An rsi_mean_reversion setup: a steep long-run uptrend (so the SMA-200
    sits far below price and the hard trend filter still passes) followed by a
    9-day sell-off that drives RSI-14 under 35, then one up-bar so RSI is
    turning back up.

    The two halves are deliberately balanced against each other: a deeper dip
    drives RSI lower but also drags price below its own SMA-200, at which
    point the hard trend filter rejects the candidate and the fixture would be
    testing nothing. These numbers put RSI around 30 with price still ~24%
    above the SMA-200.
    """
    candles = make_candles(end or last_weekday(date.today()), n, 1000.0, daily_drift_pct=0.45)
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


def _frame(candles):
    return pd.DataFrame([
        {"date": c["date"].strftime("%Y-%m-%d"), "open": c["open"], "high": c["high"],
         "low": c["low"], "close": c["close"], "volume": c["volume"]}
        for c in candles
    ])


def _enable(client, name):
    cfg = client.get(f"/api/strategies/{name}").json()["data"]
    client.put(f"/api/strategies/{name}", json={"enabled": True, "params": cfg["params"]})
    return cfg["params"]


def _scan_one(env, name, candles, symbol="TCS", token=TCS_TOKEN):
    """Seed one symbol, enable one strategy, scan, and return its ticket."""
    seed_ohlc(env.db, token, symbol, candles)
    add_watch(env.db, symbol, token)
    _enable(env.client, name)
    result = env.client.post("/api/strategies/scan-now").json()["data"]
    ids = result["by_strategy"][name]["entry_tickets"]
    assert ids, f"{name} produced no signal: {result['by_strategy'][name]['diagnostics']}"
    return env.client.get(f"/api/tickets/{ids[0]}").json()["data"]


def _system_events(db, event_type=None):
    conn = db.get_connection()
    try:
        rows = conn.execute("SELECT * FROM system_events ORDER BY id").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows if event_type is None or r["event_type"] == event_type]


# ================================================================ E1: chart endpoint

def test_chart_endpoint_returns_candles_series_levels_and_markers(env):
    ticket = _scan_one(env, "trend_pullback", _pullback_candles())
    payload = env.client.get(f"/api/tickets/{ticket['id']}/chart").json()["data"]

    assert payload["diagnostic"] is None
    assert payload["symbol"] == "TCS"
    assert payload["strategy_name"] == "trend_pullback"

    # Candles are real OHLC rows, oldest first, in lightweight-charts shape.
    assert len(payload["candles"]) > 250
    first, last = payload["candles"][0], payload["candles"][-1]
    assert set(first) == {"time", "open", "high", "low", "close"}
    assert first["time"] < last["time"]
    assert last["low"] <= last["close"] <= last["high"]

    # trend_pullback's overlays: the SMA-200 trend filter, the 20-EMA, and the
    # band a pullback is measured against (A1).
    overlays = {o["key"]: o for o in payload["overlays"]}
    assert set(overlays) == {"sma_trend", "ema_pullback", "pullback_band"}
    assert overlays["sma_trend"]["label"] == "SMA-200"
    assert overlays["ema_pullback"]["label"] == "EMA-20"
    assert all(len(o["data"]) > 0 for o in overlays.values())

    # Volume + its 20-day average ride in a subpanel for every strategy.
    volume = next(s for s in payload["subpanels"] if s["key"] == "volume")
    assert volume["kind"] == "histogram"
    assert volume["companion"]["label"] == "Avg volume (20)"
    assert len(volume["companion"]["data"]) > 0

    # Levels are the ticket's own tick-snapped numbers, not recomputed.
    levels = payload["levels"]
    assert (levels["entry"], levels["stop"], levels["target"]) == (
        ticket["entry_price"], ticket["stop_loss"], ticket["target_1"])
    assert levels["stop"] < levels["entry"] < levels["target"]
    assert levels["risk_per_share"] == pytest.approx(ticket["entry_price"] - ticket["stop_loss"])

    # Markers name the specific bars the strategy keyed off.
    kinds = {m["kind"]: m for m in payload["markers"]}
    assert set(kinds) == {"pullback", "recovery"}
    candle_dates = {c["time"] for c in payload["candles"]}
    for marker in kinds.values():
        assert marker["date"] in candle_dates, "marker must sit on a bar the chart actually renders"

    assert payload["signal_candle_date"] == ticket["signal_date"]
    assert payload["execution_mode"] == "dry_run"
    assert payload["freshness"]["max_staleness_days"] == 5
    assert payload["freshness"]["expires_on_next_scan"] is False


def test_chart_endpoint_reads_ohlc_daily_and_never_calls_kite_historical_data(env):
    """Proves the payload comes out of ohlc_daily, not a live Kite call — a
    historical_data() request here would both re-price the newest candle
    mid-session and consume a slot in data_jobs.py's 3 req/s queue."""
    ticket = _scan_one(env, "momentum_breakout", _breakout_candles())
    env.kite.calls.clear()

    payload = env.client.get(f"/api/tickets/{ticket['id']}/chart").json()["data"]

    assert payload["candles"], "sanity: the endpoint did return candles"
    assert env.kite.calls_to("historical_data") == []
    assert [name for name, _ in env.kite.calls] == []


def test_chart_endpoint_is_gated_on_a_trading_account(env):
    """The endpoint carries the same require_trading_account dependency every
    other ticket route does, so it refuses regardless of what the UI shows.

    The gate is exercised through the account type rather than by expiring the
    session, because conftest's `env` fixture monkeypatches
    kite_client.get_kite to hand back a FakeKite unconditionally — the
    session-freshness half of require_kite is stubbed out for every test in
    this suite, so asserting on it here would be asserting on the stub.
    """
    ticket = _scan_one(env, "momentum_breakout", _breakout_candles())
    assert env.client.get(f"/api/tickets/{ticket['id']}/chart").status_code == 200

    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE accounts SET account_type = 'investment' WHERE is_default = 1")
        conn.commit()
    finally:
        conn.close()

    response = env.client.get(f"/api/tickets/{ticket['id']}/chart")
    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == "NOT_TRADING_ACCOUNT"


def test_chart_endpoint_rejects_another_accounts_ticket(env):
    """Ticket lookup is scoped by account_id, so one account can never open
    another's signal — the same _get_ticket_or_404 every ticket route uses."""
    ticket = _scan_one(env, "momentum_breakout", _breakout_candles())
    conn = env.db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO accounts (label, api_key, api_secret_enc, account_type, is_default) "
            "VALUES ('Second', 'k', 'x', 'trading', 0)")
        conn.execute("UPDATE trade_tickets SET account_id = ? WHERE id = ?", (cur.lastrowid, ticket["id"]))
        conn.commit()
    finally:
        conn.close()
    assert env.client.get(f"/api/tickets/{ticket['id']}/chart").status_code == 404


def test_chart_endpoint_gives_a_clean_diagnostic_on_insufficient_history(env):
    """Fewer bars than the 200-period trend filter needs must degrade to a
    readable explanation, not a 500 — the view still opens and still plots
    the candles that do exist."""
    candles = make_candles(last_weekday(date.today()), 60, 1000.0)
    seed_ohlc(env.db, TCS_TOKEN, "TCS", candles)
    conn = env.db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO trade_tickets (account_id, source, strategy_name, symbol, exchange, instrument_token,"
            " transaction_type, quantity, entry_price, entry_order_type, stop_loss, target_1, product_type,"
            " use_gtt, rationale, status) VALUES (?, 'strategy', 'momentum_breakout', 'TCS', 'NSE', ?, 'BUY',"
            " 5, 1000.0, 'LIMIT', 950.0, 1100.0, 'CNC', 1, 'short history', 'pending_confirmation')",
            (account_id(env.db), TCS_TOKEN),
        )
        conn.commit()
        ticket_id = cur.lastrowid
    finally:
        conn.close()

    response = env.client.get(f"/api/tickets/{ticket_id}/chart")
    assert response.status_code == 200
    payload = response.json()["data"]
    diagnostic = payload["diagnostic"]
    assert diagnostic["code"] == "INSUFFICIENT_HISTORY"
    assert diagnostic["bars_available"] == 60 and diagnostic["bars_required"] == 200
    assert "200" in diagnostic["message"]
    # The bars that DO exist are still returned, and the SMA-200 overlay is
    # present-but-empty rather than missing, so the UI can say why it's blank.
    assert len(payload["candles"]) == 60
    assert next(o for o in payload["overlays"] if o["key"] == "sma_trend")["data"] == []


def test_chart_endpoint_reports_no_history_at_all(env):
    conn = env.db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO trade_tickets (account_id, source, symbol, exchange, instrument_token,"
            " transaction_type, quantity, entry_price, entry_order_type, stop_loss, target_1, product_type,"
            " use_gtt, rationale, status) VALUES (?, 'manual', 'INFY', 'NSE', ?, 'BUY',"
            " 5, 1000.0, 'LIMIT', 950.0, 1100.0, 'CNC', 1, 'no candles', 'pending_confirmation')",
            (account_id(env.db), INFY_TOKEN),
        )
        conn.commit()
        ticket_id = cur.lastrowid
    finally:
        conn.close()

    payload = env.client.get(f"/api/tickets/{ticket_id}/chart").json()["data"]
    assert payload["diagnostic"]["code"] == "NO_HISTORY"
    assert payload["candles"] == []


def test_chart_endpoint_warns_when_the_signal_is_about_to_expire(env):
    """A pending signal computed from an older candle than the newest stored
    one is cancelled by the very next scan — the view must say so before the
    user invests any thought in it."""
    ticket = _scan_one(env, "momentum_breakout", _breakout_candles())
    conn = env.db.get_connection()
    try:
        stale = (date.fromisoformat(ticket["signal_date"]) - timedelta(days=4)).isoformat()
        conn.execute("UPDATE trade_tickets SET signal_date = ? WHERE id = ?", (stale, ticket["id"]))
        conn.commit()
    finally:
        conn.close()

    freshness = env.client.get(f"/api/tickets/{ticket['id']}/chart").json()["data"]["freshness"]
    assert freshness["expires_on_next_scan"] is True
    assert "auto-expired" in freshness["warning"]


def test_chart_endpoint_serves_rsi_subpanel_with_both_threshold_lines(env):
    ticket = _scan_one(env, "rsi_mean_reversion", _oversold_candles())
    payload = env.client.get(f"/api/tickets/{ticket['id']}/chart").json()["data"]

    rsi = next(s for s in payload["subpanels"] if s["key"] == "rsi")
    assert rsi["label"] == "RSI-14"
    assert rsi["scale"] == {"min": 0, "max": 100}
    # 35 = the entry threshold, 55 = the RSI-recovery exit threshold (C3).
    assert [g["value"] for g in rsi["guides"]] == [35.0, 55.0]
    assert all(0 <= p["value"] <= 100 for p in rsi["data"])
    # Its stop candidate is charted too, since this strategy's stop is the
    # tighter of the swing low and an ATR-based level.
    assert any(o["key"] == "swing_low" for o in payload["overlays"])
    assert any(m["kind"] == "swing_low" for m in payload["markers"])


def test_chart_endpoint_states_the_exit_plan_without_inventing_an_armed_flag(env):
    """A signal that has not been executed has no entry date, so the
    thesis-invalidation exit's "armed" state is genuinely not yet
    computable — the payload must explain the rule rather than assert a
    boolean it cannot know."""
    ticket = _scan_one(env, "rsi_mean_reversion", _oversold_candles())
    plan = env.client.get(f"/api/tickets/{ticket['id']}/chart").json()["data"]["exit_plan"]

    names = [rule["name"] for rule in plan["shared_rules"]]
    assert names[0] == "RSI recovery exit", "this strategy's own exit is evaluated first (D5)"
    assert {"Thesis invalidation", "Time exit"} <= set(names)
    thesis = next(r for r in plan["shared_rules"] if r["name"] == "Thesis invalidation")
    assert "not armed yet" in thesis["detail"]
    assert "No trailing stop" in plan["trailing"]


def test_chart_endpoint_exposes_rank_against_the_daily_cap(env):
    """A3: "ranked 1 of 2 kept, 5 candidates qualified" — whether a signal
    barely made the cut is unrecoverable once the candidate list is
    truncated, so it is captured at scan time."""
    for token, symbol in ((TCS_TOKEN, "TCS"), (INFY_TOKEN, "INFY"), (738561, "RELIANCE")):
        seed_ohlc(env.db, token, symbol, _breakout_candles())
        add_watch(env.db, symbol, token)
    params = dict(_enable(env.client, "momentum_breakout"), max_signals_per_day=2)
    env.client.put("/api/strategies/momentum_breakout", json={"enabled": True, "params": params})

    result = env.client.post("/api/strategies/scan-now").json()["data"]
    by_strategy = result["by_strategy"]["momentum_breakout"]
    ids = by_strategy["entry_tickets"]
    assert len(ids) == 2, "the daily cap must be enforced"
    assert by_strategy["diagnostics"]["qualified"] == 3
    assert by_strategy["diagnostics"]["kept"] == 2

    ranks = []
    for ticket_id in ids:
        rank = env.client.get(f"/api/tickets/{ticket_id}/chart").json()["data"]["rank"]
        assert rank["qualified_count"] == 3
        assert rank["max_signals_per_day"] == 2
        ranks.append(rank["position"])
    assert sorted(ranks) == [1, 2]


# ================================================================ E2: Kite chart links

def test_kite_url_format_and_chartiq_variant():
    from backend import kite_chart

    assert kite_chart.build_kite_url("NSE", "RELIANCE", 738561) == (
        "https://kite.zerodha.com/chart/ext/tvc/NSE/RELIANCE/738561")
    assert kite_chart.build_kite_url("NSE", "RELIANCE", 738561, "ciq") == (
        "https://kite.zerodha.com/chart/ext/ciq/NSE/RELIANCE/738561")


def test_kite_url_preserves_non_eq_series_suffixes():
    """NSE series suffixes ('-BE', and the '-RR'/'-IV' variants that really do
    appear in this app's own instrument cache) identify a DIFFERENT
    instrument. Stripping one would chart the wrong security."""
    from backend import kite_chart

    assert kite_chart.build_kite_url("NSE", "SUZLON-BE", 111111).endswith("/NSE/SUZLON-BE/111111")
    assert kite_chart.build_kite_url("NSE", "BIRET-RR", 563969).endswith("/NSE/BIRET-RR/563969")
    assert kite_chart.build_kite_url("NSE", "IRBINVIT-IV", 5329153).endswith("/NSE/IRBINVIT-IV/5329153")


def test_kite_url_resolution_prefers_the_exchange_on_the_ticket(env):
    """A symbol listed on both NSE and BSE must resolve on the exchange the
    ticket was created for — charting the other listing would show a
    different price series than the signal was computed from."""
    from backend import kite_chart

    conn = env.db.get_connection()
    try:
        conn.execute(
            "INSERT INTO instrument_cache (instrument_token, tradingsymbol, name, exchange, segment,"
            " instrument_type) VALUES (500325, 'RELIANCE', 'RELIANCE INDUSTRIES', 'BSE', 'BSE', 'EQ')")
        conn.commit()
    finally:
        conn.close()

    assert kite_chart.resolve("RELIANCE", "NSE")["instrument_token"] == 738561
    assert kite_chart.resolve("RELIANCE", "BSE")["instrument_token"] == 500325
    assert "/BSE/RELIANCE/500325" in kite_chart.resolve("RELIANCE", "BSE")["url"]


def test_kite_url_unresolvable_symbol_disables_the_link_and_logs_a_system_event(env):
    from backend import kite_chart

    conn = env.db.get_connection()
    try:
        result = kite_chart.resolve_and_log(conn, account_id(env.db), "NOSUCHSYM", "NSE", None)
        conn.commit()
    finally:
        conn.close()

    assert result["status"] == kite_chart.STATUS_UNRESOLVED
    assert result["url"] is None

    events = _system_events(env.db, "kite_chart_unresolved")
    assert len(events) == 1 and "NOSUCHSYM" in events[0]["detail"]

    options = kite_chart.fallback_chain("NOSUCHSYM", "NSE", None, result["status"])
    kite_tiers = [o for o in options if o["key"].startswith("kite_")]
    assert all(o["enabled"] is False and o["url"] is None and o["reason"] for o in kite_tiers), \
        "a dead Kite link must be disabled with a stated reason, not rendered"


def test_kite_url_still_built_but_flagged_when_the_instrument_cache_is_stale(env):
    """INSTRUMENT_CACHE_MAX_AGE_HOURS = 24. Past that, the token may be out of
    date (a corporate-action rename is exactly this case), so the link is
    still offered but marked."""
    from backend import kite_chart

    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE instrument_cache SET refreshed_at = datetime('now', '-30 hours')")
        conn.commit()
    finally:
        conn.close()

    result = kite_chart.resolve("TCS", "NSE")
    assert result["status"] == kite_chart.STATUS_STALE_CACHE
    assert result["url"].endswith(f"/NSE/TCS/{TCS_TOKEN}")
    assert result["cache_age_hours"] > 24
    assert "corporate action" in result["note"]
    # Still usable — a flagged link is more useful than no link.
    assert all(o["enabled"] for o in kite_chart.fallback_chain("TCS", "NSE", TCS_TOKEN, result["status"]))


def test_kite_url_falls_back_to_the_tickets_own_token_when_renamed(env):
    """A symbol renamed by a corporate action drops out of the refreshed
    instrument cache while the app's own candles still sit under the old
    token. The link is built from the ticket's token and flagged."""
    from backend import kite_chart

    result = kite_chart.resolve("OLDNAME", "NSE", instrument_token=139125252)
    assert result["status"] == kite_chart.STATUS_STALE_CACHE
    assert result["url"].endswith("/NSE/OLDNAME/139125252")


def test_kite_fallback_chain_has_all_four_tiers_in_order():
    from backend import kite_chart

    options = kite_chart.fallback_chain("TCS", "NSE", TCS_TOKEN, kite_chart.STATUS_OK)
    assert [o["key"] for o in options] == ["kite_tvc", "kite_ciq", "tradingview_public", "copy_symbol"]
    assert options[0]["url"] == "https://kite.zerodha.com/chart/ext/tvc/NSE/TCS/2953217"
    assert options[1]["url"] == "https://kite.zerodha.com/chart/ext/ciq/NSE/TCS/2953217"
    # The public TradingView chart works logged out, which is what makes it a
    # genuine universal fallback rather than a third variant of the same link.
    assert options[2]["url"] == "https://www.tradingview.com/chart/?symbol=NSE%3ATCS"
    assert options[3]["url"] is None and options[3]["enabled"] is True
    assert "Kite web session" in options[0]["note"]


def test_scan_stores_the_kite_chart_url_on_the_ticket_at_creation_time(env):
    """B1: resolution happens when the signal is created, so a failure
    surfaces at 18:22 rather than at 09:15 when the user wants to act."""
    ticket = _scan_one(env, "momentum_breakout", _breakout_candles())
    assert ticket["kite_chart_status"] == "ok"
    assert ticket["kite_chart_url"] == f"https://kite.zerodha.com/chart/ext/tvc/NSE/TCS/{TCS_TOKEN}"
    assert ticket["kite_chart"]["options"][0]["url"] == ticket["kite_chart_url"]


def test_chart_endpoint_backfills_the_link_for_a_pre_existing_pending_ticket(env):
    """Signals already sitting in Approvals when this feature shipped have no
    stored URL. They must not show a dead link — the endpoint resolves once
    on first view and writes it back."""
    ticket = _scan_one(env, "momentum_breakout", _breakout_candles())
    conn = env.db.get_connection()
    try:
        conn.execute(
            "UPDATE trade_tickets SET kite_chart_url = NULL, kite_chart_status = NULL WHERE id = ?",
            (ticket["id"],))
        conn.commit()
    finally:
        conn.close()

    payload = env.client.get(f"/api/tickets/{ticket['id']}/chart").json()["data"]
    assert payload["kite_chart"]["status"] == "ok"
    assert payload["kite_chart"]["url"].endswith(f"/NSE/TCS/{TCS_TOKEN}")
    assert ticket_row(env.db, ticket["id"])["kite_chart_url"] == payload["kite_chart"]["url"]


# ================================================================ E3: entry-evaluation record

@pytest.mark.parametrize("strategy,candles_fn", [
    ("momentum_breakout", _breakout_candles),
    ("trend_pullback", _pullback_candles),
    ("rsi_mean_reversion", _oversold_candles),
])
def test_entry_evaluation_record_is_persisted_and_self_consistent(env, strategy, candles_fn):
    ticket = _scan_one(env, strategy, candles_fn())
    evaluation = ticket["entry_evaluation"]

    assert evaluation["strategy_name"] == strategy
    assert evaluation["signal_date"] == ticket["signal_date"]
    assert evaluation["params"], "the parameters this signal was judged by must be captured"

    conditions = evaluation["conditions"]
    # trend_pullback gained a fourth condition (the volume filter) in the
    # 2026-09 parameter revision; the others still check three.
    assert len(conditions) == (4 if strategy == "trend_pullback" else 3)
    for condition in conditions:
        assert {"name", "description", "actual_value", "threshold", "operator", "passed"} <= set(condition)
        assert condition["passed"] is True, "an approved signal's conditions must all have passed"
        assert condition["description"], "every condition explains its own rule in plain English"

    # The ticket only exists because every condition passed — if a module ever
    # adds a condition to this list but forgets it in its `passed` expression,
    # this is what catches it.
    from backend.strategies import STRATEGIES, conditions as cond
    assert cond.all_passed(conditions) is True
    assert evaluation["strategy_label"] == STRATEGIES[strategy].STRATEGY_LABEL


def test_card_description_is_generated_from_the_entry_evaluation_record(env):
    """The "one shared code path" requirement: the Approvals card's one-line
    rationale must be rendered from the same record the detail panel
    itemises, so the two can never describe the signal differently."""
    from backend.strategies import STRATEGIES, conditions as cond

    ticket = _scan_one(env, "trend_pullback", _pullback_candles())
    evaluation = ticket["entry_evaluation"]

    expected = cond.build_rationale(
        STRATEGIES["trend_pullback"].STRATEGY_LABEL, evaluation["conditions"])
    assert ticket["rationale"] == expected

    # Every condition's own summary appears verbatim in the card's sentence.
    for condition in evaluation["conditions"]:
        assert condition["summary"] in ticket["rationale"]


def test_momentum_breakout_conditions_match_the_design_document(env):
    """DESIGN.md §6: close > SMA-200 AND close > 20-day Donchian high
    (excl. today) AND volume > 1.5x avg-20 volume."""
    candles = _breakout_candles()
    from backend.strategies import momentum_breakout

    params = dict(momentum_breakout.DEFAULT_PARAMS)
    result = momentum_breakout.evaluate_entry(_frame(candles), params)
    assert result["passed"] is True
    by_name = {c["name"]: c for c in result["conditions"]}
    assert set(by_name) == {"Uptrend filter", "Donchian breakout", "Volume surge"}

    trend = by_name["Uptrend filter"]
    assert trend["operator"] == ">" and trend["actual_value"] > trend["threshold"]
    assert trend["threshold_label"].startswith("SMA-200")

    breakout = by_name["Donchian breakout"]
    lookback = momentum_breakout.DEFAULT_PARAMS["donchian_lookback"]
    prior_max = max(c["close"] for c in candles[-(lookback + 1):-1])
    assert breakout["threshold"] == pytest.approx(prior_max)
    assert breakout["actual_value"] == pytest.approx(candles[-1]["close"])

    volume = by_name["Volume surge"]
    assert volume["threshold"] == momentum_breakout.DEFAULT_PARAMS["volume_multiple"]
    assert volume["actual_value"] > volume["threshold"]

    assert [m["kind"] for m in result["markers"]] == ["breakout"]


def test_trend_pullback_conditions_match_the_design_document(env):
    """DESIGN.md §6: close > SMA-200 AND a low within 1.5% of the 20-EMA in
    the last 3 days AND close > prev close AND close > EMA."""
    from backend.strategies import trend_pullback

    params = dict(trend_pullback.DEFAULT_PARAMS)
    candles = _pullback_candles()
    result = trend_pullback.evaluate_entry(_frame(candles), params)
    assert result["passed"] is True
    by_name = {c["name"]: c for c in result["conditions"]}
    assert set(by_name) == {"Uptrend filter", "Pullback to 20-EMA", "Recovery confirmed",
                            "Volume surge"}
    volume = by_name["Volume surge"]
    assert volume["threshold"] == trend_pullback.DEFAULT_PARAMS["volume_multiple"]
    assert volume["actual_value"] > volume["threshold"]

    pullback = by_name["Pullback to 20-EMA"]
    assert pullback["operator"] == "≤" and pullback["actual_value"] <= pullback["threshold"]
    assert "1.5%" in pullback["description"]

    # The compound recovery rule reports the BINDING side as its threshold —
    # showing only one half would let a marginal recovery look comfortable.
    recovery = by_name["Recovery confirmed"]
    prev_close, last_close = candles[-2]["close"], candles[-1]["close"]
    assert recovery["actual_value"] == pytest.approx(last_close)
    assert recovery["threshold"] >= prev_close
    assert "BOTH" in recovery["description"]

    kinds = [m["kind"] for m in result["markers"]]
    assert kinds == ["pullback", "recovery"]


def test_rsi_mean_reversion_entry_conditions_match_the_design_document(env):
    """Closes the TESTING.md §1.5 gap: rsi_mean_reversion had no dedicated
    entry test. DESIGN.md §6: hard close > SMA-200 filter AND RSI-14 < 35
    AND (optionally) RSI turning up."""
    from backend import indicators
    from backend.strategies import rsi_mean_reversion

    params = dict(rsi_mean_reversion.DEFAULT_PARAMS)
    df = _frame(_oversold_candles())
    result = rsi_mean_reversion.evaluate_entry(df, params)
    assert result["passed"] is True

    by_name = {c["name"]: c for c in result["conditions"]}
    assert set(by_name) == {"Uptrend filter (hard)", "RSI oversold", "RSI turning up"}

    rsi_series = indicators.wilders_rsi(df["close"], 14)
    oversold = by_name["RSI oversold"]
    assert oversold["threshold"] == 35
    assert oversold["operator"] == "<"
    assert oversold["actual_value"] == pytest.approx(float(rsi_series.iloc[-1]))
    assert oversold["actual_value"] < 35

    turning = by_name["RSI turning up"]
    assert turning["threshold"] == pytest.approx(float(rsi_series.iloc[-2]))
    assert turning["actual_value"] > turning["threshold"]

    assert "never bypassed" in by_name["Uptrend filter (hard)"]["description"]


def test_rsi_mean_reversion_trend_filter_is_a_hard_reject(env):
    """The hard filter exists specifically to stop this strategy buying a
    falling knife: no RSI reading, however oversold, may bypass it."""
    from backend.strategies import rsi_mean_reversion

    params = dict(rsi_mean_reversion.DEFAULT_PARAMS)
    # A sustained downtrend, so price sits below its SMA-200 while RSI is deeply oversold.
    candles = make_candles(last_weekday(date.today()), 320, 4000.0, daily_drift_pct=-0.25)
    last = candles[-1]
    last["close"] = round(candles[-2]["close"] * 1.01, 2)
    last["high"] = round(last["close"] * 1.004, 2)
    result = rsi_mean_reversion.evaluate_entry(_frame(candles), params)

    by_name = {c["name"]: c for c in result["conditions"]}
    assert by_name["Uptrend filter (hard)"]["passed"] is False
    assert result["passed"] is False, "a failing trend filter blocks the signal outright"


def test_rsi_mean_reversion_exit_checks_its_own_condition_before_the_shared_ones(env):
    """DESIGN.md §6: "Exit checks RSI >= 55 (recovered to neutral) first, then
    falls back to shared checks." Also closes the §1.5 exit-test gap."""
    from backend import indicators
    from backend.strategies import rsi_mean_reversion

    params = dict(rsi_mean_reversion.DEFAULT_PARAMS)
    # A strong recovery drives RSI-14 back above the 55 exit level.
    candles = make_candles(last_weekday(date.today()), 320, 1000.0, daily_drift_pct=0.30)
    df = _frame(candles)
    position = {
        "entry_price": float(df["close"].iloc[-30]), "stop_loss": 1.0,
        "target_1": 10_000_000.0, "executed_at": str(df["date"].iloc[-30]),
    }
    result = rsi_mean_reversion.evaluate_exit(position, df, params)

    last_rsi = float(indicators.wilders_rsi(df["close"], 14).iloc[-1])
    assert last_rsi >= params["rsi_exit_level"]
    assert result["checklist"]["rsi_recovery"]["pass"] is True
    assert result["passed"] is True
    assert "RSI recovered to neutral" in result["rationale"]
    # The shared checks are still evaluated and reported alongside it, never
    # skipped just because the strategy's own condition already fired.
    assert {"thesis_invalidation", "time_exit"} <= set(result["checklist"])


def test_rsi_mean_reversion_stop_is_the_tighter_of_swing_low_and_atr(env):
    """compute_entry_levels resolves the spec's min()/"tighter" contradiction
    in favour of the genuinely tighter (higher) stop for a long."""
    from backend.strategies import rsi_mean_reversion

    params = dict(rsi_mean_reversion.DEFAULT_PARAMS)
    df = _frame(_oversold_candles())
    candidate = rsi_mean_reversion.evaluate_entry(df, params)
    levels = rsi_mean_reversion.compute_entry_levels(candidate, params)

    atr_stop = levels["entry_price"] - candidate["atr_14"] * params["atr_stop_multiple"]
    assert levels["stop_loss"] == pytest.approx(round(max(candidate["swing_low"], atr_stop), 2))
    assert levels["stop_loss"] < levels["entry_price"] < levels["target_1"]
    # This strategy deliberately has no trailing stop.
    assert levels["trailing_distance"] is None


def test_entry_evaluation_absent_on_manual_tickets_without_breaking_serialisation(env):
    """A manual ticket has no strategy evaluation. The field must come back as
    null rather than making the ticket payload unparseable."""
    body = {
        "symbol": "TCS", "exchange": "NSE", "transaction_type": "BUY", "quantity": 5,
        "entry_price": 3000.0, "entry_order_type": "LIMIT", "stop_loss": 2900.0,
        "target_1": 3300.0, "target_2": None, "trailing_enabled": False,
        "trailing_trigger": None, "trailing_distance": None, "product_type": "CNC",
        "use_gtt": True, "rationale": "manual",
    }
    ticket = env.client.post("/api/tickets", json=body).json()["data"]
    assert ticket["entry_evaluation"] is None
    # The chart link still resolves — the view is useful for manual tickets too.
    assert ticket["kite_chart"]["options"][0]["url"].endswith(f"/NSE/TCS/{TCS_TOKEN}")

    payload = env.client.get(f"/api/tickets/{ticket['id']}/chart").json()["data"]
    assert payload["conditions"] == [] and payload["markers"] == []


def test_entry_evaluation_survives_a_malformed_record(env):
    """A corrupt JSON blob should degrade the evidence panel, never break the
    ticket list it is attached to."""
    ticket = _scan_one(env, "momentum_breakout", _breakout_candles())
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE trade_tickets SET entry_evaluation = '{not json' WHERE id = ?", (ticket["id"],))
        conn.commit()
    finally:
        conn.close()

    listed = env.client.get("/api/tickets").json()["data"]
    assert [t for t in listed if t["id"] == ticket["id"]][0]["entry_evaluation"] is None
    assert env.client.get(f"/api/tickets/{ticket['id']}/chart").status_code == 200
