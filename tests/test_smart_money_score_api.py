"""PK-24 — Smart Money Accumulation Score: API, Signal Detail payload, UI
source, research export, and the guarantee that no strategy, scan, ranking,
approval, risk, sizing or backtest path consumes the score.

Numbering continues the PK-24 packet's list (27-35) where a test maps to it.
"""
import csv
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from backend import ohlc_integrity, signal_chart, smart_money_score as sms, volume_intelligence as vi, volume_research
from backend.backtest import BacktestConfig, SymbolFrame, simulate
from backend.strategies import STRATEGIES

from conftest import displayed_mode, last_weekday, make_candles, seed_ohlc
from test_backtest_realism import GOLDEN_SIGNALS, _golden_series
from test_signal_detail import TCS_TOKEN, _breakout_candles, _scan_one
from test_volume_intelligence_api import BSE_TCS_TOKEN, _db_digest, _set_status, _spiked_candles

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _fresh_cache():
    vi.clear_cache()
    yield
    vi.clear_cache()


def _frame(candles) -> pd.DataFrame:
    return pd.DataFrame([{**c, "date": c["date"].strftime("%Y-%m-%d")} for c in candles])


def _text_without_exempt(text: str) -> str:
    return text.replace(sms.NAME, "").replace(sms.DISCLAIMER, "").lower()


# ------------------------------------------------------------ 31: endpoint

def test_31_score_endpoint_is_read_only_and_matches_the_pure_score(env):
    candles = _spiked_candles()
    seed_ohlc(env.db, TCS_TOKEN, "TCS", candles)
    before = _db_digest(env.db)
    r = env.client.get("/api/smart-money-score/tcs")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["resolved"] == {"instrument_token": TCS_TOKEN, "symbol": "TCS", "source": "instrument_cache"}
    assert data["score_version"] == sms.SMART_MONEY_SCORE_VERSION
    assert data["feature_version"] == vi.FEATURE_VERSION
    assert data["as_of_date"] == candles[-1]["date"].strftime("%Y-%m-%d")
    assert data["score"] is not None and 0 <= data["score"] <= 100
    assert data["confidence"] == sms.HIGH
    assert data["disclaimer"] == sms.DISCLAIMER
    expected = sms.score(vi.compute_at(_frame(candles), symbol="TCS", instrument_token=TCS_TOKEN,
                                       history_status=data["history_status"]))
    assert data["score"] == expected["score"] and data["components"] == expected["components"]
    # read-only: no broker call, no database change
    assert env.kite.calls == []
    assert _db_digest(env.db) == before


def test_31b_as_of_scores_that_session_only(env):
    candles = _spiked_candles()
    seed_ohlc(env.db, TCS_TOKEN, "TCS", candles)
    as_of = candles[-11]["date"].strftime("%Y-%m-%d")
    data = env.client.get(f"/api/smart-money-score/TCS?as_of={as_of}").json()["data"]
    assert data["as_of_date"] == as_of and data["requested_as_of"] == as_of
    cut = sms.score(vi.compute_at(_frame(candles[:-10]), symbol="TCS", instrument_token=TCS_TOKEN,
                                  history_status=data["history_status"]))
    assert data["score"] == cut["score"] and data["components"] == cut["components"]


def test_31c_bad_parameters_and_unknown_symbol(env):
    seed_ohlc(env.db, TCS_TOKEN, "TCS", _spiked_candles())
    assert env.client.get("/api/smart-money-score/TCS?as_of=2026-02-30").status_code == 422
    r = env.client.get("/api/smart-money-score/NOSUCHCO")
    assert r.status_code == 404 and r.json()["detail"]["error_code"] == "UNKNOWN_SYMBOL"


def test_31d_wording_in_the_api_response(env):
    seed_ohlc(env.db, TCS_TOKEN, "TCS", _spiked_candles())
    text = _text_without_exempt(env.client.get("/api/smart-money-score/TCS").text)
    assert not [p for p in sms.forbidden_phrases() if p in text]


# ------------------------------------------------------------ 32: insufficient data

def test_32_known_instrument_without_history_is_null_not_fifty(env):
    data = env.client.get("/api/smart-money-score/INFY").json()["data"]
    assert data["score"] is None and data["band"] is None
    assert data["confidence"] == sms.INSUFFICIENT_DATA
    assert "NO_HISTORY" in data["confidence_reasons"][0]


@pytest.mark.parametrize("status", [ohlc_integrity.NEEDS_REFRESH, ohlc_integrity.SUSPECT])
def test_32b_flagged_history_is_null(env, status):
    seed_ohlc(env.db, TCS_TOKEN, "TCS", _spiked_candles())
    _set_status(env.db, TCS_TOKEN, "TCS", status)
    data = env.client.get("/api/smart-money-score/TCS").json()["data"]
    assert data["score"] is None and data["confidence"] == sms.INSUFFICIENT_DATA
    assert data["history_status"] == status


def test_32c_stale_history_lowers_confidence(env):
    seed_ohlc(env.db, TCS_TOKEN, "TCS", make_candles(last_weekday(date.today() - timedelta(days=30)), 120, 1000.0))
    data = env.client.get("/api/smart-money-score/TCS").json()["data"]
    assert data["freshness"]["is_stale"] is True
    assert data["confidence"] == sms.MEDIUM
    assert any("staleness" in r for r in data["confidence_reasons"])


# ------------------------------------------------------------ 27-28: duplicate exchange series

def test_27_nse_and_bse_series_are_never_combined(env):
    seed_ohlc(env.db, TCS_TOKEN, "TCS", _spiked_candles())
    seed_ohlc(env.db, BSE_TCS_TOKEN, "TCS", make_candles(last_weekday(date.today()), 120, 1000.0, volume=5_000))
    nse = env.client.get("/api/smart-money-score/TCS").json()["data"]
    bse = env.client.get(f"/api/smart-money-score/TCS?instrument_token={BSE_TCS_TOKEN}").json()["data"]
    assert nse["instrument_token"] == TCS_TOKEN and bse["instrument_token"] == BSE_TCS_TOKEN
    assert nse["components"]["participation"]["inputs"]["rvol_20"] == 4.5
    assert bse["components"]["participation"]["inputs"]["rvol_20"] == 1.0
    assert nse["score"] != bse["score"]


def test_28_exchange_selection_is_deterministic(env):
    seed_ohlc(env.db, 139125252, "AWL", make_candles(last_weekday(date.today()), 80, 300.0, volume=10))
    seed_ohlc(env.db, 2076161, "AWL", make_candles(last_weekday(date.today()), 80, 300.0, volume=1000))
    results = [env.client.get("/api/smart-money-score/AWL").json()["data"] for _ in range(3)]
    assert all(r["resolved"] == {"instrument_token": 2076161, "symbol": "AWL", "source": "stored_history"}
               for r in results)
    assert results[0] == results[1] == results[2]


# ------------------------------------------------------------ 33: Signal Detail at the signal date

def test_33_old_signal_is_scored_at_its_signal_date(env):
    candles = _breakout_candles()
    ticket = _scan_one(env, "momentum_breakout", candles)
    first = env.client.get(f"/api/tickets/{ticket['id']}/chart").json()["data"]
    sm, vol = first["smart_money_score"], first["volume_intelligence"]
    assert sm["as_of_date"] == vol["as_of_date"] == ticket["signal_date"]
    assert sm == json.loads(json.dumps(sms.score(vol)))        # the SAME snapshot, scored once
    assert sm["score"] is not None
    # later sessions arrive (heavy distribution-like bars): the old signal's score must not move
    later = make_candles(last_weekday(date.today() + timedelta(days=21)), 15, candles[-1]["close"],
                         daily_drift_pct=-2.0, volume=5_000_000)
    later = [c for c in later if c["date"] > candles[-1]["date"]]
    assert later
    seed_ohlc(env.db, TCS_TOKEN, "TCS", later)
    vi.clear_cache()
    again = env.client.get(f"/api/tickets/{ticket['id']}/chart").json()["data"]["smart_money_score"]
    assert again == sm
    today = env.client.get("/api/smart-money-score/TCS").json()["data"]
    assert today["as_of_date"] > sm["as_of_date"] and today["score"] != sm["score"]


def test_33b_chart_payload_survives_a_score_failure(env, monkeypatch):
    ticket = _scan_one(env, "momentum_breakout", _breakout_candles())

    def boom(*a, **k):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(sms, "score", boom)
    r = env.client.get(f"/api/tickets/{ticket['id']}/chart")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["smart_money_score"]["score"] is None
    assert data["smart_money_score"]["data_quality"]["codes"] == ["COMPUTATION_FAILED"]
    assert data["volume_intelligence"]["data_quality"]["status"] == vi.OK
    assert data["candles"] and data["conditions"]


def test_33c_ticket_without_history_has_no_score():
    assert signal_chart._smart_money(None) is None


# ------------------------------------------------------------ 34-35: UI source

def _ui_section() -> str:
    app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    start = app_js.index("function sdSmartMoneyList")
    return app_js[start:app_js.index("// PK-23 — volume context")]


def test_34_ui_renders_score_confidence_and_date():
    section = _ui_section()
    assert "/ 100" in section and "Confidence:" in section and "Score as of the" in section
    assert "top_positive" in section and "top_negative" in section and "Cautions" in section
    assert "s.score == null" in section               # null score is shown as unavailable, never as 50
    app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    assert 'sdSmartMoneyHtml(d.smart_money_score)' in app_js
    assert app_js.index('sdSection("volume"') < app_js.index('sdSection("smartmoney"') < app_js.index(
        'sdSection("risk"')


def test_35_ui_shows_the_disclaimer_and_no_advice_wording():
    section = _ui_section()
    assert "s.disclaimer" in section and "sd-sm-disclaimer" in section
    lowered = section.lower()
    assert not [p for p in sms.forbidden_phrases() if p in lowered]
    for colour in ("--pnl-positive", "--pnl-negative", "--success-btn", "--danger-btn"):
        assert colour not in section
    css = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")
    block = css[css.index(".sd-sm-head"):css.index(".sd-sm-disclaimer")]
    assert "--pnl-" not in block
    assert "innerhtml" not in lowered


# ------------------------------------------------------------ research export (K)

def _golden_frames(n=4):
    return {f"S{k}": pd.DataFrame(_golden_series(1000 + k * 17)) for k in range(n)}


def test_research_rows_carry_score_features_ahead_of_outcomes():
    module = STRATEGIES["momentum_breakout"]
    params = dict(module.DEFAULT_PARAMS)
    rows = volume_research.signal_feature_rows(module, _golden_frames(), params, "2025-09-01", "2026-09-18")
    assert [[int(r["symbol"][1:]), r["signal_date"]] for r in rows] == GOLDEN_SIGNALS["momentum_breakout"]
    for r in rows:
        assert set(volume_research.SCORE_COLUMNS) <= set(r)
        assert r["smart_money_score_version"] == sms.SMART_MONEY_SCORE_VERSION
        assert r["smart_money_score"] is not None
        keys = list(r)
        first_outcome = next(i for i, k in enumerate(keys) if k.startswith("outcome_"))
        assert all(keys.index(c) < first_outcome for c in volume_research.SCORE_COLUMNS)


def test_26_research_score_never_sees_outcomes(monkeypatch):
    """Scores are identical with outcomes on, off, or replaced by garbage, and
    equal the pure score of the frame CUT at the signal date."""
    module = STRATEGIES["trend_pullback"]
    params = dict(module.DEFAULT_PARAMS)
    frames = _golden_frames()
    plain = volume_research.signal_feature_rows(module, frames, params, "2025-09-01", "2026-09-18",
                                                with_outcomes=False)
    monkeypatch.setattr(volume_research, "forward_outcomes",
                        lambda df, i: {"outcome_fwd_ret_10_pct": 1e9, "outcome_mfe_20_pct": -1e9})
    garbage = volume_research.signal_feature_rows(module, frames, params, "2025-09-01", "2026-09-18")
    assert plain
    for a, b in zip(plain, garbage):
        for column in volume_research.SCORE_COLUMNS:
            assert a[column] == b[column], column
        df = frames[a["symbol"]]
        cut = df[df["date"] <= a["signal_date"]]
        assert a["smart_money_score"] == sms.score(vi.compute_at(cut))["score"]


def test_26b_score_record_rejects_outcome_keys():
    features = vi.compute_at(pd.DataFrame(_golden_series(1000)).iloc[:300])
    with pytest.raises(ValueError, match="outcome"):
        volume_research.score_record({**features, "outcome_fwd_ret_5_pct": 3.0})


def test_research_export_cli_writes_score_columns_before_outcomes(env, tmp_path):
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
                                 "momentum_breakout", "--start", "2000-01-01"]) == 0
    with open(out, encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames
        rows = list(reader)
    assert rows and rows[-1]["smart_money_score"] != ""
    first_outcome = next(i for i, c in enumerate(header) if c.startswith("outcome_"))
    assert all(header.index(c) < first_outcome for c in volume_research.SCORE_COLUMNS)


# ------------------------------------------------------------ 29-30: strategy isolation (P)

DECISION_PATHS = ("backend/strategy_engine.py", "backend/risk_engine.py", "backend/backtest.py",
                  "backend/backtest_analytics.py", "backend/backtest_costs.py", "backend/execution.py",
                  "backend/automation.py", "backend/todays_actions.py", "backend/paper_broker.py",
                  "backend/trailing_engine.py", "backend/order_monitor.py", "backend/workers.py",
                  "backend/lifecycle.py", "backend/position_quantity.py", "backend/routes/tickets.py",
                  "backend/routes/strategies.py", "backend/strategies/momentum_breakout.py",
                  "backend/strategies/trend_pullback.py", "backend/strategies/rsi_mean_reversion.py",
                  "backend/strategies/shared.py", "backend/strategies/conditions.py",
                  "backend/strategies/__init__.py")


@pytest.mark.parametrize("path", DECISION_PATHS)
def test_29_no_decision_path_imports_the_score(path):
    source = (ROOT / path).read_text(encoding="utf-8")
    assert "smart_money" not in source


@pytest.mark.parametrize("name", sorted(GOLDEN_SIGNALS))
def test_29b_strategy_signals_are_unchanged_with_the_score_disabled(name, monkeypatch):
    def tripwire(*a, **k):
        raise AssertionError("a strategy consulted the smart money score")
    monkeypatch.setattr(sms, "score", tripwire)
    module = STRATEGIES[name]
    params = dict(module.DEFAULT_PARAMS)
    hits = []
    for n in range(4):
        df = pd.DataFrame(_golden_series(1000 + n * 17))
        for i in range(len(df)):
            if df["date"].iloc[i] >= "2025-09-01" and module.evaluate_entry(df.iloc[: i + 1], params)["passed"]:
                hits.append([n, df["date"].iloc[i]])
    assert hits == GOLDEN_SIGNALS[name]


def test_29c_backtest_trade_selection_never_calls_the_score(monkeypatch):
    calls = []
    monkeypatch.setattr(sms, "score", lambda *a, **k: calls.append(1) or {})
    module = STRATEGIES["momentum_breakout"]
    frames = {f"S{n}": SymbolFrame(f"S{n}", pd.DataFrame(_golden_series(1000 + n * 17))).sliced("2026-08-31")
              for n in range(4)}
    out = simulate(module, frames, dict(module.DEFAULT_PARAMS), BacktestConfig.from_overrides(),
                   "2025-09-01", "2026-08-31")
    assert out["trades"] and calls == []
    golden = {(f"S{n}", d) for n, d in GOLDEN_SIGNALS["momentum_breakout"]}
    assert {(t["symbol"], t["signal_date"]) for t in out["trades"]} <= golden


def test_30_scan_ranking_risk_and_approval_never_call_the_score(env, monkeypatch):
    calls = []
    real = sms.score

    def counting(features):
        calls.append(features.get("as_of_date"))
        return real(features)
    monkeypatch.setattr(sms, "score", counting)
    ticket = _scan_one(env, "momentum_breakout", _breakout_candles())
    assert calls == []                                   # scan, rank, size, risk-check
    evaluation = ticket["entry_evaluation"]
    evaluation = json.loads(evaluation) if isinstance(evaluation, str) else evaluation
    assert "smart_money" not in json.dumps(evaluation) and "smart_money" not in json.dumps(ticket)
    env.client.get(f"/api/tickets/{ticket['id']}")
    env.client.get("/api/tickets")
    env.client.post(f"/api/tickets/{ticket['id']}/confirm", json=displayed_mode(env.db))
    assert calls == []                                   # listing and approval
    env.client.get(f"/api/tickets/{ticket['id']}/chart")
    assert calls == [ticket["signal_date"]]              # only the evidence panel scores (tripwire works)
