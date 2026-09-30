"""PK-26 — Price-Volume Research scanner (research / exploration only).

The scanner must reproduce PK-23 features and the PK-24 score exactly, in one
batch, from stored history; expose them descriptively (no ranking, no default
filter, no band label on the score, PK-25's LIMITED status visible); read
nothing but stored data; and stay out of every production decision path.

Numbering follows the PK-26 packet's test list (1-40) where a test maps to it.
"""
import csv
import io
import json
import re
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend import ohlc_integrity, research_scanner as rs, smart_money_score as sms, volume_intelligence as vi
from backend.routes.volume_intelligence import resolve_instrument

from conftest import add_watch, last_weekday, make_candles, seed_ohlc
from test_signal_detail import TCS_TOKEN, INFY_TOKEN, _breakout_candles, _enable
from test_smart_money_score_api import DECISION_PATHS

ROOT = Path(__file__).resolve().parent.parent
RELIANCE_TOKEN, SUNPHARMA_TOKEN, RECLTD_TOKEN = 738561, 857857, 3930881
BSE_TCS_TOKEN = 139024900                                  # low byte 4: a BSE series of TCS
SCAN = "/api/research/volume-scan"


@pytest.fixture(autouse=True)
def _fresh_caches():
    vi.clear_cache()
    rs.clear_cache()
    yield
    vi.clear_cache()
    rs.clear_cache()


# ---------------------------------------------------------------- data helpers

def walk_candles(seed: int, n: int = 140, end: date | None = None, price: float = 500.0) -> list[dict]:
    """A deterministic random walk with skewed volume and occasional spikes,
    so rows differ on every feature."""
    rng = np.random.RandomState(seed)
    days, d = [], end or last_weekday(date.today())
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    out, close = [], price
    for day in days:
        prev = close
        close = max(1.0, prev * (1 + rng.normal(0.0005, 0.02)))
        open_ = prev * (1 + rng.normal(0, 0.004))
        high = max(open_, close) * (1 + abs(rng.normal(0, 0.008)))
        low = min(open_, close) * (1 - abs(rng.normal(0, 0.008)))
        volume = int(rng.lognormal(12, 0.45) * (rng.choice([1, 1, 1, 1, 1, 1, 1, 1, 3.5, 6.0])))
        out.append({"date": pd.Timestamp(day).to_pydatetime(), "open": round(open_, 2), "high": round(high, 2),
                    "low": round(low, 2), "close": round(close, 2), "volume": volume})
    return out


def spiked_candles(n=120, spike=450_000):
    candles = make_candles(last_weekday(date.today()), n, 1000.0, daily_drift_pct=0.1)
    candles[-1]["volume"] = spike
    return candles


def watch(env, symbol, token, candles):
    if candles:
        seed_ohlc(env.db, token, symbol, candles)
    add_watch(env.db, symbol, token)


UNIVERSE = (("TCS", TCS_TOKEN, 11), ("INFY", INFY_TOKEN, 22), ("RELIANCE", RELIANCE_TOKEN, 33),
            ("SUNPHARMA", SUNPHARMA_TOKEN, 44), ("RECLTD", RECLTD_TOKEN, 55))


@pytest.fixture
def universe(env):
    for symbol, token, seed in UNIVERSE:
        watch(env, symbol, token, walk_candles(seed))
    return env


def scan(env, **params):
    r = env.client.get(SCAN, params=params)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def rows_by_symbol(data):
    return {r["symbol"]: r for r in data["rows"]}


def db_digest(db) -> str:
    conn = db.get_connection()
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def set_status(db, token, symbol, status):
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


def pk23(env, token, symbol, as_of=None):
    conn = env.db.get_connection()
    try:
        return vi.features_for_instrument(conn, token, symbol, as_of=as_of)
    finally:
        conn.close()


def detail(env, token, **params):
    r = env.client.get(f"{SCAN}/{token}", params=params)
    assert r.status_code == 200, r.text
    return r.json()["data"]


# ================================================================ DATA 1-6

def test_01_batch_scan_returns_the_watchlist_instruments_in_one_batch(universe, monkeypatch):
    seed_ohlc(universe.db, 5633, "ACC", walk_candles(99))        # stored, but not watched
    per_symbol = []
    monkeypatch.setattr(vi, "features_for_instrument", lambda *a, **k: per_symbol.append(1))
    before = db_digest(universe.db)
    data = scan(universe)
    assert sorted(r["symbol"] for r in data["rows"]) == sorted(s for s, _, _ in UNIVERSE)
    assert data["universe"] == {"source": "watchlist", "account_found": True, "instruments": 5,
                                "features_available": 5, "features_unavailable": 0}
    assert per_symbol == []                        # never the per-symbol path
    assert universe.kite.calls == []               # no broker call
    assert db_digest(universe.db) == before        # read-only


def test_01b_one_query_per_concern_not_per_symbol(universe, monkeypatch):
    from backend import db as db_module
    real = db_module.get_connection
    statements = []

    class Spy:
        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, *args):
            statements.append(" ".join(sql.split()))
            return self._c.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._c, name)

    monkeypatch.setattr("backend.routes.research_scanner.get_connection", lambda: Spy(real()))
    scan(universe)
    ohlc_reads = [s for s in statements if "ohlc_daily" in s]
    assert len(ohlc_reads) == 3, ohlc_reads       # window, counts, other series — for all 5 symbols at once


def test_02_nse_and_bse_histories_stay_separate(env):
    watch(env, "TCS", TCS_TOKEN, spiked_candles())
    seed_ohlc(env.db, BSE_TCS_TOKEN, "TCS", make_candles(last_weekday(date.today()), 120, 1000.0, volume=5_000))
    data = scan(env)
    assert [r["instrument_token"] for r in data["rows"]] == [TCS_TOKEN]
    row = data["rows"][0]
    assert row["exchange"] == "NSE" and row["rvol_20"] == 4.5
    ev = detail(env, TCS_TOKEN)["volume_features"]
    assert ev["relative_volume"]["avg_volume_20"] == 100_000       # never mixed with the 5,000 BSE series
    assert any(str(BSE_TCS_TOKEN) in n and "not combined" in n for n in row["notes"])


def test_03_deterministic_nse_preference_matches_pk23(env):
    watch(env, "TCS", TCS_TOKEN, spiked_candles())
    seed_ohlc(env.db, BSE_TCS_TOKEN, "TCS", make_candles(last_weekday(date.today()), 120, 1000.0, volume=5_000))
    conn = env.db.get_connection()
    try:
        resolved = resolve_instrument(conn, "TCS")
    finally:
        conn.close()
    tokens = {scan(env)["rows"][0]["instrument_token"] for _ in range(3)}
    assert tokens == {TCS_TOKEN} == {resolved["instrument_token"]}


def test_04_as_of_date_is_the_last_stored_session(universe):
    candles = walk_candles(11)
    latest = scan(universe)
    assert rows_by_symbol(latest)["TCS"]["as_of_date"] == candles[-1]["date"].strftime("%Y-%m-%d")
    target = candles[-11]["date"].strftime("%Y-%m-%d")
    past = scan(universe, as_of=target)
    assert {r["as_of_date"] for r in past["rows"]} == {target}
    assert past["requested_as_of"] == target
    # a non-session date resolves to the previous stored session, with a note
    saturday = candles[-11]["date"].date()
    while saturday.weekday() != 5:
        saturday += timedelta(days=1)
    row = rows_by_symbol(scan(universe, as_of=saturday.isoformat()))["TCS"]
    assert row["as_of_date"] < saturday.isoformat() and any("No candle on" in n for n in row["notes"])
    assert universe.client.get(SCAN, params={"as_of": "2026-13-45"}).status_code == 422


@pytest.mark.parametrize("status", [ohlc_integrity.SUSPECT, ohlc_integrity.NEEDS_REFRESH])
def test_05_suspect_history_is_not_computed(env, status):
    watch(env, "TCS", TCS_TOKEN, spiked_candles())
    set_status(env.db, TCS_TOKEN, "TCS", status)
    row = scan(env)["rows"][0]
    assert row["data_quality"] == vi.UNAVAILABLE and row["history_status"] == status
    for field in ("rvol_20", "volume_percentile_60", "close_location", "obv_slope_20", "accumulation_like_10",
                  "sms_score", "volume_state"):
        assert row[field] is None, field
    assert row["sms_confidence"] == sms.INSUFFICIENT_DATA and row["warnings"]


def test_05b_special_session_contamination_is_exposed(env):
    end = date(2024, 3, 8)                          # a week after the 2024-03-02 disaster-recovery Saturday
    candles = make_candles(end, 120, 500.0)
    candles.append({"date": pd.Timestamp("2024-03-02").to_pydatetime(), "open": 540.0, "high": 541.0,
                    "low": 539.0, "close": 540.0, "volume": 4_000})
    candles.sort(key=lambda c: c["date"])
    watch(env, "TCS", TCS_TOKEN, candles)
    row = scan(env, as_of="2024-03-08")["rows"][0]
    assert row["special_session"] is True and "SPECIAL_SESSION_IN_BASELINE" in row["data_quality_codes"]
    assert row["data_quality"] == vi.DEGRADED and row["sms_confidence"] != sms.HIGH


def test_06_insufficient_and_partial_history_are_handled(env):
    watch(env, "TCS", TCS_TOKEN, walk_candles(1, n=12))
    watch(env, "INFY", INFY_TOKEN, walk_candles(2, n=40))
    watch(env, "RELIANCE", RELIANCE_TOKEN, [])                      # watched, nothing stored
    rows = rows_by_symbol(scan(env))
    short, partial, none = rows["TCS"], rows["INFY"], rows["RELIANCE"]
    assert short["data_quality"] == vi.UNAVAILABLE and "INSUFFICIENT_HISTORY" in short["data_quality_codes"]
    assert short["rvol_20"] is None and short["sms_score"] is None
    assert none["data_quality"] == vi.UNAVAILABLE and "NO_HISTORY" in none["data_quality_codes"]
    assert none["as_of_date"] is None and none["sms_score"] is None
    assert partial["data_quality"] == vi.DEGRADED and "PARTIAL_HISTORY" in partial["data_quality_codes"]
    assert partial["rvol_20"] is not None and partial["volume_percentile_60"] is None
    assert partial["bars_available"] == 40


# ================================================================ FEATURES 7-12 (parity with PK-23 / PK-24)

def _parity(env):
    out = []
    for symbol, token, _ in UNIVERSE:
        ours = detail(env, token)
        out.append((symbol, token, ours, pk23(env, token, symbol)))
    return out


def test_07_to_10_every_pk23_feature_matches_features_for_instrument(universe):
    for symbol, token, ours, theirs in _parity(universe):
        assert ours["volume_features"] == theirs, symbol          # the whole VolumeFeatures object
        row = ours["row"]
        rv, ev, obv = theirs["relative_volume"], theirs["evidence"], theirs["obv"]
        assert row["rvol_20"] == rv["rvol_20"]                                        # 7
        assert row["volume_percentile_60"] == rv["volume_percentile_60"]              # 8
        assert (row["obv_slope_20"], row["obv_trend"], row["obv_divergence"]) == (
            obv["slope_20"], obv["trend"], obv["divergence"])                         # 9
        assert (row["accumulation_like_10"], row["distribution_like_10"], row["accumulation_like_20"]) == (
            ev["accumulation_like_10"], ev["distribution_like_10"], ev["accumulation_like_20"])   # 10


def test_07b_parity_also_holds_as_of_a_past_date(universe):
    as_of = walk_candles(11)[-30]["date"].strftime("%Y-%m-%d")
    for symbol, token, _ in UNIVERSE:
        assert detail(universe, token, as_of=as_of)["volume_features"] == pk23(universe, token, symbol, as_of)


def test_11_12_score_and_confidence_match_pk24(universe):
    for symbol, token, ours, theirs in _parity(universe):
        expected = sms.score(theirs)
        api = universe.client.get(f"/api/smart-money-score/{symbol}").json()["data"]
        assert ours["row"]["sms_score"] == expected["score"] == api["score"]                      # 11
        assert ours["row"]["sms_confidence"] == expected["confidence"] == api["confidence"]       # 12
        assert ours["smart_money"] == {k: v for k, v in expected.items() if k != "band"}
        assert ours["row"]["sms_failed_breakout_points"] == expected["components"]["failed_breakout"]["points"]
        assert isinstance(ours["row"]["sms_failed_breakout_points"], float)   # points, not the export's flag


# ================================================================ FILTERS 13-19

def _check_filter(env, params, predicate):
    everything = scan(env)["rows"]
    got = scan(env, **params)
    kept = {r["instrument_token"] for r in got["rows"]}
    expected = {r["instrument_token"] for r in everything if predicate(r)}
    assert kept == expected
    assert got["matched_rows"] == len(kept) and got["total_rows"] == len(everything)
    return got


@pytest.fixture
def varied(env):
    for i in range(12):
        watch(env, f"SYM{i:02d}", 1_000_001 + 256 * i, walk_candles(100 + i))
    return env


def _ge(field, x):
    return lambda r: r[field] is not None and r[field] >= x


def test_13_rvol_filter(varied):
    got = _check_filter(varied, {"min_rvol": 1.2}, _ge("rvol_20", 1.2))
    assert got["applied"]["conditions"] == [{"field": "rvol_20", "op": ">=", "value": 1.2}]


def test_14_volume_percentile_filter(varied):
    _check_filter(varied, {"min_volume_percentile": 60}, _ge("volume_percentile_60", 60))


def test_15_liquidity_filter(varied):
    median = float(np.median([r["avg_traded_value_20"] for r in scan(varied)["rows"]]))
    _check_filter(varied, {"min_avg_traded_value": median}, _ge("avg_traded_value_20", median))


def test_15b_close_location_filter(varied):
    _check_filter(varied, {"min_close_location": 0.5}, _ge("close_location", 0.5))


def test_16_accumulation_filter(varied):
    _check_filter(varied, {"min_accumulation_10": 1}, _ge("accumulation_like_10", 1))


def test_17_distribution_filter(varied):
    _check_filter(varied, {"max_distribution_10": 0},
                  lambda r: r["distribution_like_10"] is not None and r["distribution_like_10"] <= 0)


@pytest.mark.parametrize("choice,predicate", [
    ("rising", lambda r: r["obv_trend"] == "rising"),
    ("falling", lambda r: r["obv_trend"] == "falling"),
    ("new_high", lambda r: r["obv_new_high_20"] is True),
    ("obv_leads_price", lambda r: r["obv_divergence"] == "obv_leads_price"),
    ("price_unconfirmed", lambda r: r["obv_divergence"] == "price_unconfirmed"),
])
def test_18_obv_filter(varied, choice, predicate):
    _check_filter(varied, {"obv": choice}, predicate)


@pytest.mark.parametrize("param,choice,predicate", [
    ("volume", "contracting", lambda r: r["volume_contracting"] is True),
    ("volume", "expanding", lambda r: r["volume_expanding"] is True),
    ("volume", "contraction_then_expansion",
     lambda r: r["volume_contracting"] is True and r["volume_expanding"] is True),
    ("breakout", "present", lambda r: r["is_breakout"] is True),
    ("breakout", "absent", lambda r: r["is_breakout"] is False),
    ("pullback", "low_volume", lambda r: r["low_volume_pullback"] is True),
])
def test_18b_volume_breakout_and_pullback_filters(varied, param, choice, predicate):
    _check_filter(varied, {param: choice}, predicate)


def test_19_optional_score_filter_is_labelled_and_off_by_default(varied):
    default = scan(varied)
    assert default["applied"]["conditions"] == [] and default["warnings"] == []
    assert default["matched_rows"] == default["total_rows"] == 12
    got = _check_filter(varied, {"sms_min": 50, "sms_max": 60},
                        lambda r: r["sms_score"] is not None and 50 <= r["sms_score"] <= 60)
    assert got["warnings"] == ["Score v1.0 did not show independent out-of-sample predictive value."]


def test_19b_bad_filters_are_rejected(varied):
    for params in ({"min_rvol": "abc"}, {"obv": "up"}, {"preset": "top_picks"}, {"sort": "rank"},
                   {"order": "sideways"}, {"min_rvol": "nan"}):
        r = varied.client.get(SCAN, params=params)
        assert r.status_code == 422 and r.json()["detail"]["error_code"] == "INVALID_FILTER", params


def test_19c_unavailable_rows_never_pass_a_feature_filter(env):
    watch(env, "TCS", TCS_TOKEN, walk_candles(1, n=12))
    watch(env, "INFY", INFY_TOKEN, walk_candles(2))
    for params in ({"min_rvol": 0}, {"max_distribution_10": 99}, {"min_close_location": 0}, {"sms_min": 0}):
        assert [r["symbol"] for r in scan(env, **params)["rows"]] == ["INFY"], params


# ================================================================ PRESETS 20-23

PRESET_PRIMITIVES = {
    "unusual_participation": [("rvol_20", ">=", 2.0), ("volume_percentile_60", ">=", 90.0)],
    "close_near_high_on_volume": [("rvol_20", ">=", 1.5), ("close_location", ">=", 0.75)],
    "accumulation_like_pattern": [("net_accumulation_10", ">=", 1), ("obv_slope_20", ">=", 0.1),
                                  ("high_volume_weak_close", "==", False)],
    "low_volume_pullback": [("in_pullback", "==", True), ("pullback_volume_ratio", "<=", 0.8)],
    "contraction_then_expansion": [("contraction_ratio_5_20", "<=", 0.8), ("rvol_20", ">=", 1.5)],
    "distribution_caution": [("rvol_20", ">=", 1.5), ("close_location", "<=", 0.25),
                             ("distribution_like_10", ">=", 1)],
}


def _predicate(conditions):
    ops = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, "==": lambda a, b: a == b}
    return lambda r: all(r[f] is not None and ops[op](r[f], v) for f, op, v in conditions)


@pytest.mark.parametrize("key", sorted(PRESET_PRIMITIVES))
def test_20_to_23_presets_are_transparent_pk23_combinations(varied, key):
    catalogue = {p["key"]: p for p in scan(varied)["presets"]}
    preset = catalogue[key]
    listed = [(c["field"], c["op"], c["value"]) for c in preset["conditions"]]
    assert listed == PRESET_PRIMITIVES[key]
    assert all(c["field"] != "sms_score" for c in preset["conditions"])          # no hidden score
    assert preset["description"] and preset["label"]
    got = _check_filter(varied, {"preset": key}, _predicate(PRESET_PRIMITIVES[key]))
    assert got["applied"]["preset"] == key
    assert [(c["field"], c["op"], c["value"]) for c in got["applied"]["conditions"]] == listed


def test_20b_preset_thresholds_are_pk23_thresholds():
    thr = vi.DEFAULT_THRESHOLDS
    assert PRESET_PRIMITIVES["close_near_high_on_volume"][1][2] == thr["strong_close"]
    assert PRESET_PRIMITIVES["distribution_caution"][1][2] == thr["weak_close"]
    assert PRESET_PRIMITIVES["contraction_then_expansion"][0][2] == thr["contraction_ratio"]
    assert PRESET_PRIMITIVES["low_volume_pullback"][1][2] == thr["low_volume_pullback_ratio"]
    assert PRESET_PRIMITIVES["accumulation_like_pattern"][1][2] == thr["obv_trend_slope"]
    assert PRESET_PRIMITIVES["unusual_participation"][1][2] == thr["abnormal_percentile"]


def test_20c_presets_are_named_descriptively():
    text = json.dumps([{k: p[k] for k in ("key", "label", "description")} for p in rs.PRESETS]).lower()
    for word in ("buy", "sell", "top pick", "best", "setup", "recommend", "opportunity", "bullish"):
        assert word not in text, word


def test_22b_low_volume_pullback_preset_finds_a_real_one(env):
    candles = make_candles(last_weekday(date.today()), 100, 1000.0, daily_drift_pct=0.3, volume=200_000)
    for c in candles[-6:-1]:                         # a 5-session pullback on thin volume
        c["close"] = round(c["close"] * 0.97, 2)
        c["open"] = c["close"]
        c["low"] = round(c["close"] * 0.995, 2)
        c["high"] = round(c["close"] * 1.004, 2)
        c["volume"] = 60_000
    watch(env, "TCS", TCS_TOKEN, candles)
    watch(env, "INFY", INFY_TOKEN, walk_candles(5))
    row = rows_by_symbol(scan(env))["TCS"]
    assert row["in_pullback"] is True and row["pullback_volume_ratio"] <= 0.8
    assert "TCS" in [r["symbol"] for r in scan(env, preset="low_volume_pullback")["rows"]]


def test_23b_distribution_caution_preset_finds_a_weak_close_on_volume(env):
    candles = make_candles(last_weekday(date.today()), 100, 1000.0)
    prev = candles[-2]["close"]
    candles[-1].update(open=prev * 1.01, high=prev * 1.02, low=prev * 0.95, close=prev * 0.953, volume=400_000)
    watch(env, "TCS", TCS_TOKEN, candles)
    row = scan(env, preset="distribution_caution")["rows"]
    assert [r["symbol"] for r in row] == ["TCS"] and row[0]["distribution_like_10"] >= 1


# ================================================================ NO RANKING 24-26

def test_24_default_order_is_symbol_not_smart_money(varied):
    data = scan(varied)
    assert data["sort"]["default"] == {"key": "symbol", "order": "asc"} == rs.DEFAULT_SORT
    assert data["applied"]["sort"]["key"] == "symbol" and data["applied"]["sort"]["is_default"] is True
    symbols = [r["symbol"] for r in data["rows"]]
    assert symbols == sorted(symbols)
    scores = [r["sms_score"] for r in data["rows"]]
    assert scores != sorted(scores, reverse=True)           # the fixture's scores are not in symbol order


def test_25_no_composite_rank_or_hidden_ordering(varied):
    data = scan(varied)
    forbidden = re.compile(r"rank|composite|opportunit|trade_score|priority|weight|recommend", re.I)
    for row in data["rows"]:
        assert not [k for k in row if forbidden.search(k)], row.keys()
    assert data["scanner"]["ranking"] is None
    assert "sms_score" not in json.dumps(data["presets"])
    source = (ROOT / "backend" / "research_scanner.py").read_text(encoding="utf-8")
    assert "rank_key" not in source and "strategy_engine" not in source


def test_26_user_may_sort_any_numeric_column_including_the_score(varied):
    for key in ("rvol_20", "volume_percentile_60", "avg_traded_value_20", "close_location", "obv_slope_20",
                "accumulation_like_10", "distribution_like_10", "sms_score"):
        for order in ("asc", "desc"):
            data = scan(varied, sort=key, order=order)
            values = [r[key] for r in data["rows"] if r[key] is not None]
            assert values == sorted(values, reverse=order == "desc"), (key, order)
            assert data["applied"]["sort"] == {"key": key, "order": order, "is_default": False}


def test_26b_missing_values_sort_last_and_ties_break_by_symbol():
    rows = [{"symbol": s, "instrument_token": t, "v": v} for s, t, v in
            [("B", 2, 1.0), ("A", 1, None), ("C", 3, 1.0), ("D", 4, 2.0)]]
    original = rs.SORTABLE
    try:
        rs.SORTABLE = original + ("v",)
        assert [r["symbol"] for r in rs.sort_rows(rows, "v", "desc")] == ["D", "B", "C", "A"]
        assert [r["symbol"] for r in rs.sort_rows(rows, "v", "asc")] == ["B", "C", "D", "A"]
    finally:
        rs.SORTABLE = original


# ================================================================ VALIDATION TEXT 27-29

LIMITED_TEXT = ("Smart Money Score v1.0 is descriptive price-volume evidence. PK-25 found no independent "
                "out-of-sample predictive value after controlling for strategy.")


def test_27_pk25_limited_warning_is_present(varied):
    data = scan(varied)
    sm = data["smart_money"]
    assert sm["limitation"] == LIMITED_TEXT
    assert sm["validation"]["status"] == "LIMITED"
    assert sm["validation"]["summary"] == ("Validation status: LIMITED — no incremental value beyond strategy "
                                           "was established.")
    d = detail(varied, 1_000_001)
    assert d["smart_money_display"]["limitation"] == LIMITED_TEXT
    assert d["smart_money_display"]["validation"]["status"] == "LIMITED"


def test_28_institutional_proof_disclaimer_is_present(varied):
    assert scan(varied)["smart_money"]["disclaimer"] == "Price-volume evidence, not proof of institutional buying."
    assert detail(varied, 1_000_001)["smart_money"]["disclaimer"] == sms.DISCLAIMER


def _high_score_candles():
    """Contraction, then a breakout on volume with a close at the high after
    repeated accumulation-like sessions — scores well above 75."""
    candles = make_candles(last_weekday(date.today()), 120, 1000.0, daily_drift_pct=0.15, volume=100_000)
    for i in (-9, -6, -3):
        prev = candles[i - 1]["close"]
        candles[i].update(open=prev, low=prev * 0.999, close=prev * 1.02, high=prev * 1.0205, volume=220_000)
        for j in range(i + 1, min(i + 3, 0)):
            candles[j].update(open=candles[j - 1]["close"], close=candles[j - 1]["close"] * 1.001,
                              low=candles[j - 1]["close"] * 0.998, high=candles[j - 1]["close"] * 1.003,
                              volume=60_000)
    prev = candles[-2]["close"]
    candles[-1].update(open=prev * 1.005, low=prev * 1.004, close=prev * 1.04, high=prev * 1.0405, volume=380_000)
    return candles


def test_29_high_score_receives_no_positive_trading_label(env):
    watch(env, "TCS", TCS_TOKEN, _high_score_candles())
    data = scan(env)
    row = data["rows"][0]
    assert row["sms_score"] >= 75, row["sms_score"]
    d = detail(env, TCS_TOKEN)
    assert "band" not in d["smart_money"]
    shown = json.dumps({k: v for k, v in row.items() if k.startswith("sms_")}) + json.dumps(
        {k: v for k, v in d["smart_money"].items() if k not in ("components", "weights", "disclaimer")})
    for word in ("strong", "buy", "sell", "top pick", "best", "bullish", "opportunity"):
        assert word not in shown.lower(), word


# ================================================================ DRILL-DOWN 30-31, signal context

def test_30_31_drilldown_is_the_pk23_and_pk24_objects(universe):
    for symbol, token, ours, theirs in _parity(universe):
        assert ours["row"] == rows_by_symbol(scan(universe))[symbol]
        assert ours["volume_features"]["interpretation"] == theirs["interpretation"]
        expected = sms.score(theirs)
        assert ours["smart_money"]["top_positive"] == expected["top_positive"]
        assert ours["smart_money"]["top_negative"] == expected["top_negative"]
    assert universe.client.get(f"{SCAN}/5633").status_code == 404


def test_signal_context_is_shown_beside_the_evidence_not_combined(env):
    watch(env, "TCS", TCS_TOKEN, _breakout_candles())
    watch(env, "INFY", INFY_TOKEN, walk_candles(7))
    before = scan(env)
    _enable(env.client, "momentum_breakout")
    ids = env.client.post("/api/strategies/scan-now").json()["data"]["by_strategy"]["momentum_breakout"][
        "entry_tickets"]
    assert ids
    tickets_before = db_digest(env.db)
    rs.clear_cache()
    after = rows_by_symbol(scan(env))
    tcs = after["TCS"]
    assert [s["strategy"] for s in tcs["active_signals"]] == ["momentum_breakout"] and tcs["has_signal"]
    assert tcs["active_signals"][0]["status"] == "awaiting approval"
    assert after["INFY"]["active_signals"] == [] and after["INFY"]["has_signal"] is False
    old = rows_by_symbol(before)["TCS"]
    for key in old:                                 # the signal changes no evidence value
        if key not in ("active_signals", "has_signal"):
            assert tcs[key] == old[key], key
    assert db_digest(env.db) == tickets_before      # reading signals writes nothing
    assert [r["symbol"] for r in scan(env, signal="active")["rows"]] == ["TCS"]
    # an as-of view does not attach today's signals
    assert rows_by_symbol(scan(env, as_of=tcs["as_of_date"]))["TCS"]["active_signals"] == []


# ================================================================ ISOLATION 32-36

@pytest.mark.parametrize("path", DECISION_PATHS)
def test_32_to_35_no_decision_path_imports_the_scanner(path):
    source = (ROOT / path).read_text(encoding="utf-8")
    assert "research_scanner" not in source and "volume-scan" not in source


def test_32b_nothing_but_its_route_imports_the_scanner():
    users = [p.relative_to(ROOT).as_posix() for p in (ROOT / "backend").rglob("*.py")
             if "research_scanner" in p.read_text(encoding="utf-8") and p.name != "research_scanner.py"
             or p.parent.name == "routes" and p.name == "research_scanner.py"]
    assert sorted(users) == ["backend/main.py", "backend/routes/research_scanner.py"]


def test_32c_strategy_scan_ranking_and_approval_never_call_the_scanner(env, monkeypatch):
    calls = []
    monkeypatch.setattr(rs, "build_scan", lambda *a, **k: calls.append("build_scan"))
    monkeypatch.setattr(rs, "evaluate", lambda *a, **k: calls.append("evaluate"))
    seed_ohlc(env.db, TCS_TOKEN, "TCS", _breakout_candles())
    add_watch(env.db, "TCS", TCS_TOKEN)
    _enable(env.client, "momentum_breakout")
    result = env.client.post("/api/strategies/scan-now").json()["data"]
    ids = result["by_strategy"]["momentum_breakout"]["entry_tickets"]
    assert ids
    env.client.get(f"/api/tickets/{ids[0]}")
    env.client.get("/api/tickets")
    assert calls == []


def test_32d_scanning_changes_no_strategy_output(env):
    seed_ohlc(env.db, TCS_TOKEN, "TCS", _breakout_candles())
    add_watch(env.db, "TCS", TCS_TOKEN)
    _enable(env.client, "momentum_breakout")
    for _ in range(2):
        scan(env)
        detail(env, TCS_TOKEN)
    env.client.get(f"{SCAN}.csv")
    ids = env.client.post("/api/strategies/scan-now").json()["data"]["by_strategy"]["momentum_breakout"][
        "entry_tickets"]
    ticket = env.client.get(f"/api/tickets/{ids[0]}").json()["data"]
    assert "sms" not in json.dumps(ticket.get("entry_evaluation") or {}).lower()
    assert "research" not in json.dumps(ticket.get("entry_evaluation") or {}).lower()


def _manifest():
    from backend import smart_money_validation as smv
    return smv, smv.read_manifest(ROOT / "research" / "pk25")


def test_36_pk25_manifest_remains_valid_and_matches_the_display():
    smv, manifest = _manifest()
    assert not manifest.get("superseded_by")
    frozen = manifest["model_freeze"]
    for key, files in (("model_file_sha256", smv.MODEL_FILES), ("production_file_sha256", smv.PRODUCTION_FILES)):
        for path in files:
            assert smv.normalised_sha256(ROOT / path) == frozen[key][path], path
    holdout = json.loads((ROOT / "research" / "pk25" / "holdout_results.json").read_text(encoding="utf-8"))
    assert holdout["classification"]["label"] == rs.SMS_VALIDATION["status"] == "LIMITED"
    assert frozen["smart_money_score_version"] == rs.SMS_VALIDATION["score_version"]


# ================================================================ EXPORT 37-39

def _csv(env, **params):
    r = env.client.get(f"{SCAN}.csv", params=params)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    return r.text


def test_37_csv_is_deterministic_and_matches_the_rows(varied):
    first = _csv(varied, sort="rvol_20", order="desc", min_volume_percentile=10)
    rs.clear_cache()
    vi.clear_cache()
    second = _csv(varied, sort="rvol_20", order="desc", min_volume_percentile=10)
    assert first == second
    assert ",-0.0," not in first                     # a zero penalty is written as 0.0
    records = list(csv.DictReader(io.StringIO(first)))
    shown = scan(varied, sort="rvol_20", order="desc", min_volume_percentile=10)["rows"]
    assert [int(r["instrument_token"]) for r in records] == [r["instrument_token"] for r in shown]
    for rec, row in zip(records, shown):
        assert float(rec["rvol_20"]) == row["rvol_20"] and rec["as_of_date"] == row["as_of_date"]
        assert rec["sms_score"] == ("" if row["sms_score"] is None else str(row["sms_score"]))
        assert rec["sms_validation_status"] == "LIMITED" and rec["sms_version"] == "1.0"


def test_38_no_outcome_fields_in_the_export(varied):
    header = next(csv.reader(io.StringIO(_csv(varied))))
    assert header == list(rs.CSV_COLUMNS)
    assert not [c for c in header if c.startswith("outcome_") or "fwd" in c or "forward" in c]
    original = rs.CSV_COLUMNS
    try:
        rs.CSV_COLUMNS = original + ("outcome_fwd_ret_5_pct",)
        with pytest.raises(ValueError):
            rs.export_csv([])
    finally:
        rs.CSV_COLUMNS = original


def test_39_no_personal_account_data_in_the_export(env):
    watch(env, "TCS", TCS_TOKEN, _breakout_candles())
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE watchlist SET notes = 'my private note', tag = 'secret-tag'")
        conn.commit()
    finally:
        conn.close()
    _enable(env.client, "momentum_breakout")
    ids = env.client.post("/api/strategies/scan-now").json()["data"]["by_strategy"]["momentum_breakout"][
        "entry_tickets"]
    text = _csv(env)
    header = next(csv.reader(io.StringIO(text)))
    for word in ("ticket", "order", "holding", "quantity", "pnl", "notes", "account", "tag", "gtt"):
        assert not [c for c in header if word in c], word
    assert "my private note" not in text and "secret-tag" not in text
    assert ids and "momentum_breakout@" in text        # the active signal, by strategy and candle only


# ================================================================ CACHE 40

def test_40_history_rebuild_invalidates_the_cached_row(env):
    candles = spiked_candles()
    watch(env, "TCS", TCS_TOKEN, candles)
    assert scan(env)["rows"][0]["rvol_20"] == 4.5
    warm = scan(env)
    assert warm["timing"]["cache_hits"] == 1 and warm["timing"]["cache_misses"] == 0
    # a PK-22 rebuild replaces the series with a fresh download and marks it OK
    candles[-1]["volume"] = 200_000
    conn = env.db.get_connection()
    try:
        conn.execute("DELETE FROM ohlc_daily WHERE instrument_token = ?", (TCS_TOKEN,))
        conn.commit()
    finally:
        conn.close()
    seed_ohlc(env.db, TCS_TOKEN, "TCS", candles)
    conn = env.db.get_connection()
    try:
        ohlc_integrity.mark_rebuilt(conn, TCS_TOKEN, "TCS")
        conn.commit()
    finally:
        conn.close()
    after = scan(env)
    assert after["rows"][0]["rvol_20"] == 2.0 and after["rows"][0]["history_status"] == ohlc_integrity.OK
    assert after["timing"]["cache_misses"] == 1
    assert after["rows"][0]["sms_score"] == sms.score(pk23(env, TCS_TOKEN, "TCS"))["score"]


def test_40b_status_change_alone_invalidates_the_cached_row(env):
    watch(env, "TCS", TCS_TOKEN, spiked_candles())
    assert scan(env)["rows"][0]["data_quality"] == vi.OK
    set_status(env.db, TCS_TOKEN, "TCS", ohlc_integrity.NEEDS_REFRESH)
    row = scan(env)["rows"][0]
    assert row["data_quality"] == vi.UNAVAILABLE and row["sms_score"] is None


def test_40b2_an_edited_bar_alone_invalidates_the_cached_row(env):
    candles = spiked_candles()
    watch(env, "TCS", TCS_TOKEN, candles)
    assert scan(env)["rows"][0]["rvol_20"] == 4.5
    conn = env.db.get_connection()
    try:                                        # same row count, same (absent) status — only the content
        conn.execute("UPDATE ohlc_daily SET volume = 300000 WHERE instrument_token = ? AND date = ?",
                     (TCS_TOKEN, candles[-1]["date"].strftime("%Y-%m-%d")))
        conn.commit()
    finally:
        conn.close()
    after = scan(env)
    assert after["rows"][0]["rvol_20"] == 3.0 and after["timing"]["cache_misses"] == 1


def test_40c_cache_key_carries_both_definition_versions():
    frame = vi._normalise(pd.DataFrame(walk_candles(3, n=70)).assign(date=lambda d: d["date"].astype(str)))
    key = rs.cache_key(1, "X", frame, 70, None, [], None, "2026-09-29")
    assert vi.FEATURE_VERSION in key and sms.SMART_MONEY_SCORE_VERSION in key
    assert vi._history_digest(frame) in key
    edited = frame.copy()
    edited.loc[len(edited) - 1, "volume"] += 1
    assert rs.cache_key(1, "X", edited, 70, None, [], None, "2026-09-29") != key


# ================================================================ read-only / wording

def test_scanner_routes_are_read_only_and_never_call_the_broker(varied):
    before = db_digest(varied.db)
    scan(varied, preset="unusual_participation", sort="sms_score", order="desc")
    detail(varied, 1_000_001)
    _csv(varied)
    assert varied.kite.calls == []
    assert db_digest(varied.db) == before


def test_scanner_text_uses_no_advice_or_attribution_wording(varied):
    data = scan(varied)
    exempt = {sms.DISCLAIMER, sms.NAME, rs.SMS_LIMITATION}
    texts = []

    def walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
        elif isinstance(obj, str) and obj not in exempt:
            texts.append(obj.lower())

    walk({k: data[k] for k in ("scanner", "presets", "smart_money", "fields")})
    joined = "\n".join(texts)
    for phrase in ("buy", "sell", "top pick", "best stock", "recommend", "bullish", "bearish", "opportunit",
                   "institution", "smart money buying", "will rise", "will fall", "guarantee", "operator"):
        assert phrase not in joined, phrase
