"""PK-23 — Volume Intelligence API, Signal Detail payload, research harness,
and the guarantee that no strategy, scan, sizing or risk path consumes it.

Numbering continues the PK-23 packet's list (32-34) where a test maps to it.
"""
import csv
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from backend import ohlc_integrity, volume_intelligence as vi, volume_research
from backend.strategies import STRATEGIES

from conftest import add_watch, last_weekday, make_candles, seed_ohlc
from test_backtest_realism import GOLDEN_SIGNALS, _golden_series
from test_signal_detail import TCS_TOKEN, _breakout_candles, _oversold_candles, _scan_one

BSE_TCS_TOKEN = 139024900     # low byte 4: a BSE series of the same tradingsymbol
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _fresh_cache():
    vi.clear_cache()
    yield
    vi.clear_cache()


def _spiked_candles(n=120, spike=450_000):
    candles = make_candles(last_weekday(date.today()), n, 1000.0, daily_drift_pct=0.1)
    candles[-1]["volume"] = spike
    return candles


def _set_status(db, token, symbol, status):
    conn = db.get_connection()
    try:
        ohlc_integrity.summarize(conn, token, symbol)
        if status == ohlc_integrity.NEEDS_REFRESH:
            ohlc_integrity.mark_needs_refresh(conn, token, symbol, {"ratio": 0.5, "dates": ["x", "y"]})
        elif status == ohlc_integrity.SUSPECT:
            conn.execute("UPDATE ohlc_history_status SET status = 'SUSPECT' WHERE instrument_token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def _db_digest(db) -> str:
    conn = db.get_connection()
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


# ------------------------------------------------------------ 32: latest features

def test_32_latest_feature_response(env):
    candles = _spiked_candles()
    seed_ohlc(env.db, TCS_TOKEN, "TCS", candles)
    before = _db_digest(env.db)
    r = env.client.get("/api/volume-intelligence/tcs")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["resolved"] == {"instrument_token": TCS_TOKEN, "symbol": "TCS", "source": "instrument_cache"}
    assert data["as_of_date"] == candles[-1]["date"].strftime("%Y-%m-%d")
    assert data["feature_version"] == vi.FEATURE_VERSION
    assert data["data_quality"]["status"] == vi.OK
    assert data["relative_volume"]["rvol_20"] == 4.5          # 450,000 / 100,000 prior-only
    assert data["relative_volume"]["volume_percentile_60"] == 100.0
    assert data["freshness"]["is_stale"] is False
    assert data["interpretation"][0].startswith("RVOL20: 4.50×")
    # read-only: no broker call, no database change
    assert env.kite.calls == []
    assert _db_digest(env.db) == before


def test_32b_as_of_ignores_later_rows_and_matches_the_pure_engine(env):
    candles = _spiked_candles()
    seed_ohlc(env.db, TCS_TOKEN, "TCS", candles)
    as_of = candles[-11]["date"].strftime("%Y-%m-%d")
    data = env.client.get(f"/api/volume-intelligence/TCS?as_of={as_of}").json()["data"]
    assert data["as_of_date"] == as_of
    frame = pd.DataFrame([{**c, "date": c["date"].strftime("%Y-%m-%d")} for c in candles[:-10]])
    expected = vi.compute_at(frame, as_of, symbol="TCS", instrument_token=TCS_TOKEN,
                             history_status=ohlc_integrity.OK)
    for group in ("relative_volume", "liquidity", "structure", "obv", "evidence", "anomalies"):
        assert data[group] == expected[group]


def test_32c_strategy_comparison_uses_the_saved_strategy_params(env):
    seed_ohlc(env.db, TCS_TOKEN, "TCS", _spiked_candles())
    params = env.client.get("/api/strategies/momentum_breakout").json()["data"]["params"]
    env.client.put("/api/strategies/momentum_breakout", json={"enabled": False,
                                                              "params": {**params, "volume_multiple": 4.0}})
    cmp_ = env.client.get("/api/volume-intelligence/TCS?strategy=momentum_breakout").json()["data"][
        "strategy_comparison"]
    assert cmp_["required_multiple"] == 4.0
    assert cmp_["legacy_multiple"] == pytest.approx(450_000 / ((19 * 100_000 + 450_000) / 20), abs=1e-4)
    # 3.83x including the bar fails a 4.0 rule, while the prior-only RVOL is 4.5x
    assert cmp_["legacy_pass"] is False and cmp_["rvol_20_prior_only"] == 4.5
    rsi = env.client.get("/api/volume-intelligence/TCS?strategy=rsi_mean_reversion").json()["data"]
    assert rsi["strategy_comparison"]["has_volume_rule"] is False


def test_32d_symbol_resolves_to_the_nse_series_never_the_bse_one(env):
    seed_ohlc(env.db, TCS_TOKEN, "TCS", _spiked_candles())
    seed_ohlc(env.db, BSE_TCS_TOKEN, "TCS", make_candles(last_weekday(date.today()), 120, 1000.0, volume=5_000))
    data = env.client.get("/api/volume-intelligence/TCS").json()["data"]
    assert data["instrument_token"] == TCS_TOKEN and data["relative_volume"]["rvol_20"] == 4.5
    bse = env.client.get(f"/api/volume-intelligence/TCS?instrument_token={BSE_TCS_TOKEN}").json()["data"]
    assert bse["instrument_token"] == BSE_TCS_TOKEN and bse["relative_volume"]["avg_volume_20"] == 5_000
    # a symbol with no cache/watchlist entry, stored under both: the NSE token wins
    seed_ohlc(env.db, 139125252, "AWL", make_candles(last_weekday(date.today()), 60, 300.0, volume=10))
    seed_ohlc(env.db, 2076161, "AWL", make_candles(last_weekday(date.today()), 60, 300.0, volume=1000))
    awl = env.client.get("/api/volume-intelligence/AWL").json()["data"]
    assert awl["resolved"] == {"instrument_token": 2076161, "symbol": "AWL", "source": "stored_history"}


def test_32e_watchlist_token_is_used_first(env):
    seed_ohlc(env.db, BSE_TCS_TOKEN, "TCS", _spiked_candles())
    add_watch(env.db, "TCS", BSE_TCS_TOKEN)
    data = env.client.get("/api/volume-intelligence/TCS").json()["data"]
    assert data["resolved"]["source"] == "watchlist" and data["instrument_token"] == BSE_TCS_TOKEN


# ------------------------------------------------------------ 33: unavailable / flagged / stale

def test_33_unknown_symbol_is_404(env):
    r = env.client.get("/api/volume-intelligence/NOSUCHCO")
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "UNKNOWN_SYMBOL"


def test_33b_known_instrument_without_history(env):
    data = env.client.get("/api/volume-intelligence/INFY").json()["data"]
    assert data["data_quality"]["status"] == vi.UNAVAILABLE
    assert data["data_quality"]["codes"] == ["NO_HISTORY"]
    assert data["relative_volume"] is None
    assert data["interpretation"][0].startswith("Volume features are unavailable")


@pytest.mark.parametrize("status, code", [(ohlc_integrity.NEEDS_REFRESH, "HISTORY_NEEDS_REFRESH"),
                                          (ohlc_integrity.SUSPECT, "HISTORY_SUSPECT")])
def test_33c_flagged_history_is_reported_not_computed(env, status, code):
    seed_ohlc(env.db, TCS_TOKEN, "TCS", _spiked_candles())
    _set_status(env.db, TCS_TOKEN, "TCS", status)
    data = env.client.get("/api/volume-intelligence/TCS").json()["data"]
    assert data["history_status"] == status
    assert data["data_quality"]["status"] == vi.UNAVAILABLE and code in data["data_quality"]["codes"]
    assert data["relative_volume"] is None


def test_33d_stale_history_is_flagged(env):
    old_end = last_weekday(date.today() - timedelta(days=30))
    seed_ohlc(env.db, TCS_TOKEN, "TCS", make_candles(old_end, 120, 1000.0))
    data = env.client.get("/api/volume-intelligence/TCS").json()["data"]
    assert data["freshness"]["is_stale"] is True
    assert any("behind the last completed session" in w for w in data["warnings"])


def test_33e_bad_query_parameters(env):
    seed_ohlc(env.db, TCS_TOKEN, "TCS", _spiked_candles())
    assert env.client.get("/api/volume-intelligence/TCS?as_of=2026-13-45").status_code == 422
    assert env.client.get("/api/volume-intelligence/TCS?strategy=nope").status_code == 422


def test_33f_cache_never_serves_features_from_replaced_history(env):
    candles = _spiked_candles()
    seed_ohlc(env.db, TCS_TOKEN, "TCS", candles)
    first = env.client.get("/api/volume-intelligence/TCS").json()["data"]
    again = env.client.get("/api/volume-intelligence/TCS").json()["data"]
    assert again == first and vi.cache_stats == {"hits": 1, "misses": 1}
    candles[-1]["volume"] = 200_000          # e.g. a PK-22 rebuild rewrote the bar
    seed_ohlc(env.db, TCS_TOKEN, "TCS", candles)
    rebuilt = env.client.get("/api/volume-intelligence/TCS").json()["data"]
    assert rebuilt["relative_volume"]["rvol_20"] == 2.0
    assert vi.cache_stats["misses"] == 2
    # the status flag is part of the key too
    _set_status(env.db, TCS_TOKEN, "TCS", ohlc_integrity.NEEDS_REFRESH)
    assert env.client.get("/api/volume-intelligence/TCS").json()["data"]["data_quality"]["status"] == vi.UNAVAILABLE


# ------------------------------------------------------------ 34: wording

def test_34_interpretation_never_claims_who_traded_or_what_happens_next(env):
    seed_ohlc(env.db, TCS_TOKEN, "TCS", _spiked_candles())
    text = env.client.get("/api/volume-intelligence/TCS?strategy=momentum_breakout").text.lower()
    assert not [p for p in vi.FORBIDDEN_PHRASES if p in text]
    app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    start = app_js.index("function sdVolumeRow")
    section = app_js[start:app_js.index("function sdRenderEvidence")].lower()
    assert not [p for p in vi.FORBIDDEN_PHRASES if p in section]
    assert "innerhtml" not in section          # rendered via esc() into the evidence panel's html


# ------------------------------------------------------------ Signal Detail payload

def test_chart_payload_carries_volume_context_at_the_signal_bar(env):
    ticket = _scan_one(env, "momentum_breakout", _breakout_candles())
    payload = env.client.get(f"/api/tickets/{ticket['id']}/chart").json()["data"]
    vol = payload["volume_intelligence"]
    assert vol["as_of_date"] == ticket["signal_date"]
    assert vol["data_quality"]["status"] == vi.OK
    cmp_ = vol["strategy_comparison"]
    volume_condition = next(c for c in payload["conditions"] if c["name"] == "Volume surge")
    assert cmp_["legacy_multiple"] == pytest.approx(volume_condition["actual_value"], abs=1e-4)
    assert cmp_["legacy_pass"] is True and cmp_["has_volume_rule"] is True
    assert vol["breakout"]["is_breakout"] is True
    assert vol["relative_volume"]["rvol_20"] > cmp_["legacy_multiple"]   # prior-only is the larger number


def test_chart_payload_for_a_strategy_without_a_volume_rule(env):
    ticket = _scan_one(env, "rsi_mean_reversion", _oversold_candles())
    vol = env.client.get(f"/api/tickets/{ticket['id']}/chart").json()["data"]["volume_intelligence"]
    assert vol["strategy_comparison"]["has_volume_rule"] is False


def test_chart_payload_survives_a_volume_failure(env, monkeypatch):
    ticket = _scan_one(env, "momentum_breakout", _breakout_candles())

    def boom(*a, **k):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(vi, "features_for_instrument", boom)
    r = env.client.get(f"/api/tickets/{ticket['id']}/chart")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["volume_intelligence"]["data_quality"]["codes"] == ["COMPUTATION_FAILED"]
    assert data["candles"] and data["conditions"]


# ------------------------------------------------------------ strategies untouched (P)

@pytest.mark.parametrize("path", ["backend/strategy_engine.py", "backend/risk_engine.py", "backend/backtest.py",
                                  "backend/execution.py", "backend/strategies/momentum_breakout.py",
                                  "backend/strategies/trend_pullback.py", "backend/strategies/rsi_mean_reversion.py",
                                  "backend/strategies/shared.py", "backend/strategies/conditions.py"])
def test_no_decision_path_consumes_volume_intelligence(path):
    source = (ROOT / path).read_text(encoding="utf-8")
    assert "volume_intelligence" not in source and "volume_research" not in source


def test_scan_ticket_record_is_unchanged_by_pk23(env):
    ticket = _scan_one(env, "momentum_breakout", _breakout_candles())
    evaluation = json.loads(ticket["entry_evaluation"]) if isinstance(ticket["entry_evaluation"], str) \
        else ticket["entry_evaluation"]
    assert set(evaluation) == {"strategy_name", "strategy_label", "strategy_version", "signal_date", "signal_close",
                               "atr_14", "conditions", "markers", "rank", "sizing", "levels_raw", "tick_size",
                               "params"}


# ------------------------------------------------------------ research harness (O)

def _golden_frames(n=4):
    return {f"S{k}": pd.DataFrame(_golden_series(1000 + k * 17)) for k in range(n)}


def test_research_rows_are_the_real_signals_with_features_and_separate_outcomes():
    module = STRATEGIES["momentum_breakout"]
    params = dict(module.DEFAULT_PARAMS)
    rows = volume_research.signal_feature_rows(module, _golden_frames(), params, "2025-09-01", "2026-09-18")
    assert [[int(r["symbol"][1:]), r["signal_date"]] for r in rows] == GOLDEN_SIGNALS["momentum_breakout"]
    for r in rows:
        assert r["legacy_volume_pass"] is True
        assert set(volume_research.FEATURE_COLUMNS) <= set(r)
        assert any(k.startswith("outcome_") for k in r)
        assert not set(volume_research.FEATURE_COLUMNS) & {k for k in r if k.startswith("outcome_")}


def test_research_features_never_see_future_rows():
    """The same signals computed from frames CUT at each signal date give the
    same feature values: only outcome_* columns may look forward."""
    module = STRATEGIES["trend_pullback"]
    params = dict(module.DEFAULT_PARAMS)
    frames = _golden_frames()
    rows = volume_research.signal_feature_rows(module, frames, params, "2025-09-01", "2026-09-18")
    assert rows
    for r in rows:
        cut = {r["symbol"]: frames[r["symbol"]][frames[r["symbol"]]["date"] <= r["signal_date"]]}
        again = volume_research.signal_feature_rows(module, cut, params, r["signal_date"], r["signal_date"],
                                                    with_outcomes=False)
        assert len(again) == 1
        for column in volume_research.FEATURE_COLUMNS:
            assert again[0][column] == r[column], column


def test_forward_outcomes_are_measured_from_the_next_open():
    df = pd.DataFrame({"date": [f"d{i:02d}" for i in range(30)], "open": [100.0] * 30,
                       "high": [100.0 + i for i in range(30)], "low": [90.0] * 30,
                       "close": [100.0 + i / 2 for i in range(30)], "volume": [1] * 30})
    out = volume_research.forward_outcomes(df, 3)
    assert out["outcome_fwd_ret_5_pct"] == pytest.approx((100 + 8 / 2) / 100 * 100 - 100)
    assert out["outcome_mfe_20_pct"] == pytest.approx(23.0)      # high of bar 23
    assert out["outcome_mae_20_pct"] == pytest.approx(-10.0)
    tail = volume_research.forward_outcomes(df, 25)
    assert tail["outcome_fwd_ret_5_pct"] is None and tail["outcome_mfe_20_pct"] is None


def test_research_export_cli_on_a_database_copy(env, tmp_path):
    seed_ohlc(env.db, TCS_TOKEN, "TCS", _breakout_candles())
    copy = tmp_path / "copy.db"
    src = env.db.get_connection()
    dst = sqlite3.connect(copy)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    out = tmp_path / "signals.csv"
    assert volume_research.main(["export", "--db", str(copy), "--out", str(out), "--strategy",
                                 "momentum_breakout", "--start", "2000-01-01", "--with-trades"]) == 0
    with open(out, encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames
        rows = list(reader)
    assert rows and rows[-1]["symbol"] == "TCS" and rows[-1]["strategy"] == "momentum_breakout"
    first_outcome = next(i for i, c in enumerate(header) if c.startswith("outcome_"))
    assert all(c.startswith("outcome_") for c in header[first_outcome:])
    assert "outcome_traded" in header
