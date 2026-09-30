"""Price-Volume Research (PK-26): a descriptive evidence explorer for the watchlist.

RESEARCH / EXPLORATION ONLY — not trade-signal generation.

For every watchlist instrument this computes the latest PK-23 VolumeFeatures
and, as one descriptive column, the PK-24 Smart Money Score v1.0 — in one
batch, from stored daily history, with one database query per concern (not
one per symbol, and never through the per-symbol HTTP endpoints). It answers
"where is volume unusual / expanding / contracting, where is price closing
strongly or weakly, where is OBV improving, where do accumulation-like or
distribution-like sessions appear". It does not answer "what should I trade".

What this module deliberately does NOT do (PK-25 classified Smart Money Score
v1.0 LIMITED: no incremental out-of-sample value once the strategy is known,
worse in 6/6 walk-forward folds, and a score >= 75 filter cut expectancy in
both volume strategies):

* no ranking, composite, "opportunity" or "trade" score of any kind;
* no default filter, and the default order is the symbol, never the score;
* no band label for the score (a score of 82 is shown as 82 with its
  explanation, not as "strong");
* no write of any kind, no broker call, no order, GTT or ticket change.

Dependencies are one-way: this module reads PK-23 / PK-24 / ticket state.
Nothing in the strategy, scan, ranking, risk, approval, sizing, execution,
GTT or backtest paths imports it (tested).

Identity is the instrument token. The same tradingsymbol can have an NSE and
a BSE series stored (PK-23 found six); they are never combined. The scanner
uses the watchlist's own token, exactly as PK-23's resolve_instrument does,
and names any other stored series of the symbol in the row's notes.

Parity: a row's features are exactly what volume_intelligence
.features_for_instrument returns for that token, and its score exactly what
smart_money_score.score returns on those features (tested field by field).
The only difference is how the history is read: one windowed query for all
tokens (the last LOOKBACK_BARS + 1 rows, all any feature can use) plus one
row count per token, instead of each token's full history.
"""

import csv
import io
import json
import sqlite3
from collections import OrderedDict
from threading import Lock

import pandas as pd

from backend import config, smart_money_score as sms, trading_calendar, volume_intelligence as vi

SCANNER_NAME = "Price-Volume Research"
SCANNER_SUBTITLE = "Explore price-volume evidence across the watchlist."
SCANNER_VERSION = 1
PURPOSE = ("Research view: descriptive price-volume evidence for each watchlist instrument. It is not a trade "
           "signal and not a ranking — rows are listed in the order of the column you sort by (symbol by "
           "default).")

# Rows read per instrument: the as-of bar, the 60 prior sessions of the
# percentile, and one more for the gap INTO the window (PK-23's own window).
WINDOW_ROWS = vi.LOOKBACK_BARS + 1

# ---------------------------------------------------------------- Smart Money Score v1.0: display rules
SMS_LABEL = "SMS v1.0"
SMS_LIMITATION = ("Smart Money Score v1.0 is descriptive price-volume evidence. PK-25 found no independent "
                  "out-of-sample predictive value after controlling for strategy.")
SMS_FILTER_WARNING = "Score v1.0 did not show independent out-of-sample predictive value."
SMS_VALIDATION = {
    "status": "LIMITED",
    "validated_by": "PK-25",
    "score_version": sms.SMART_MONEY_SCORE_VERSION,
    "summary": "Validation status: LIMITED — no incremental value beyond strategy was established.",
    "findings": [
        "Pooled score/return relationships were largely explained by which strategy produced the signal.",
        "Within each strategy the score's incremental value was weak and not established.",
        "Adding the score made a strategy + RVOL20 model worse out of sample, in 6 of 6 walk-forward folds.",
        "Inside both volume strategies, keeping only scores of 75 or more reduced expectancy in both "
        "out-of-sample periods.",
        "Extreme single-day up-bars often score 75 or more without that reflecting repeated sessions of "
        "evidence.",
    ],
    "use": "Shown for reference only: nothing here ranks, filters by default or prioritises by it.",
}
# The PK-25 pre-declared extreme-bar cohort, flagged so the drill-down can
# say why a high score on such a bar deserves extra care. Descriptive only.
EXTREME_UP_BAR = {"price_change_pct": 8.0, "rvol_20": 3.0, "close_location": 0.75}
EXTREME_UP_BAR_NOTE = ("This bar matches the PK-25 extreme single-day cohort (a +8% or larger day on RVOL20 of 3× "
                       "or more, closing in the top quarter of its range). PK-25 found such bars frequently score "
                       "75 or more; that reflects one bar, not repeated sessions of evidence.")

# ---------------------------------------------------------------- the flat row: fields, filters, presets
NUMBER, BOOL, ENUM, TEXT = "number", "bool", "enum", "text"

# (name, type, label, sortable). The one catalogue the API, UI and CSV share.
FIELDS = (
    ("symbol", TEXT, "Symbol", True),
    ("exchange", TEXT, "Exchange", False),
    ("watchlist_tag", TEXT, "Tag", False),
    ("as_of_date", TEXT, "As of", True),
    ("data_quality", ENUM, "Data quality", False),
    ("rvol_20", NUMBER, "RVOL20", True),
    ("rvol_median_20", NUMBER, "RVOL20 (median-based)", True),
    ("volume_percentile_60", NUMBER, "Volume percentile (60)", True),
    ("traded_value", NUMBER, "Traded value ₹", True),
    ("avg_traded_value_20", NUMBER, "Avg traded value ₹ (20)", True),
    ("relative_traded_value_20", NUMBER, "Relative traded value", True),
    ("price_change_pct", NUMBER, "Price change %", True),
    ("close_location", NUMBER, "Close location", True),
    ("high_volume_weak_close", BOOL, "High-volume weak close", False),
    ("obv_slope_20", NUMBER, "OBV slope (20)", True),
    ("obv_trend", ENUM, "OBV trend", False),
    ("obv_new_high_20", BOOL, "OBV 20-session high", False),
    ("obv_divergence", ENUM, "OBV vs price", False),
    ("accumulation_like_10", NUMBER, "Accumulation-like (10)", True),
    ("distribution_like_10", NUMBER, "Distribution-like (10)", True),
    ("net_accumulation_10", NUMBER, "Accumulation − distribution (10)", True),
    ("accumulation_like_20", NUMBER, "Accumulation-like (20)", True),
    ("distribution_like_20", NUMBER, "Distribution-like (20)", True),
    ("contraction_ratio_5_20", NUMBER, "Prior-5 / prior-20 volume", True),
    ("volume_contracting", BOOL, "Volume contracting", False),
    ("volume_expanding", BOOL, "Volume expanding", False),
    ("volume_state", ENUM, "Volume state", False),
    ("is_breakout", BOOL, "Close above prior-30 closing high", False),
    ("in_pullback", BOOL, "In pullback", False),
    ("pullback_volume_ratio", NUMBER, "Pullback volume ratio", True),
    ("low_volume_pullback", BOOL, "Low-volume pullback", False),
    ("has_signal", BOOL, "Active strategy signal", False),
    ("sms_score", NUMBER, SMS_LABEL, True),
)
FIELD_TYPES = {name: kind for name, kind, _, _ in FIELDS}
FIELD_LABELS = {name: label for name, _, label, _ in FIELDS}
SORTABLE = tuple(name for name, _, _, sortable in FIELDS if sortable)
OPERATORS = (">=", "<=", "==")

# Default order: alphabetical by symbol. NEVER the score (PK-25).
DEFAULT_SORT = {"key": "symbol", "order": "asc"}

_THR = vi.DEFAULT_THRESHOLDS

# Explicit, user-selected filters: query parameter -> (field, operator).
NUMERIC_FILTERS = {
    "min_rvol": ("rvol_20", ">="),
    "min_volume_percentile": ("volume_percentile_60", ">="),
    "min_avg_traded_value": ("avg_traded_value_20", ">="),
    "min_close_location": ("close_location", ">="),
    "min_accumulation_10": ("accumulation_like_10", ">="),
    "max_distribution_10": ("distribution_like_10", "<="),
    "sms_min": ("sms_score", ">="),
    "sms_max": ("sms_score", "<="),
}
CHOICE_FILTERS = {
    "obv": {
        "rising": [("obv_trend", "==", "rising")],
        "falling": [("obv_trend", "==", "falling")],
        "new_high": [("obv_new_high_20", "==", True)],
        "obv_leads_price": [("obv_divergence", "==", "obv_leads_price")],
        "price_unconfirmed": [("obv_divergence", "==", "price_unconfirmed")],
    },
    "volume": {
        "contracting": [("volume_contracting", "==", True)],
        "expanding": [("volume_expanding", "==", True)],
        "contraction_then_expansion": [("volume_state", "==", "contraction_then_expansion")],
    },
    "breakout": {
        "present": [("is_breakout", "==", True)],
        "absent": [("is_breakout", "==", False)],
    },
    "pullback": {
        "any": [("in_pullback", "==", True)],
        "low_volume": [("low_volume_pullback", "==", True)],
    },
    "signal": {
        "active": [("has_signal", "==", True)],
        "none": [("has_signal", "==", False)],
    },
}
TEXT_FILTERS = {"tag": ("watchlist_tag", "==")}


def _c(field, op, value):
    return {"field": field, "op": op, "value": value}


# Research presets: transparent AND-combinations of PK-23 primitives, using
# PK-23's own thresholds. No weights, no score, no hidden ranking.
PRESETS = (
    {"key": "unusual_participation", "label": "Unusual participation",
     "description": "Volume well above its prior-20 average and in the top decile of the prior 60 sessions.",
     "conditions": [_c("rvol_20", ">=", 2.0), _c("volume_percentile_60", ">=", _THR["abnormal_percentile"])]},
    {"key": "close_near_high_on_volume", "label": "Close near the high on elevated volume",
     "description": "Elevated RVOL20 with the close in the upper quarter of the day's range.",
     "conditions": [_c("rvol_20", ">=", _THR["elevated_rvol"]),
                    _c("close_location", ">=", _THR["strong_close"])]},
    {"key": "accumulation_like_pattern", "label": "Accumulation-like pattern",
     "description": "More accumulation-like than distribution-like sessions in the last 10, OBV rising over "
                    "20 sessions, and no high-volume weak close today.",
     "conditions": [_c("net_accumulation_10", ">=", 1), _c("obv_slope_20", ">=", _THR["obv_trend_slope"]),
                    _c("high_volume_weak_close", "==", False)]},
    {"key": "low_volume_pullback", "label": "Low-volume pullback",
     "description": "Price below its 20-session closing high on pullback volume at or below 0.8× the volume "
                    "into that high.",
     "conditions": [_c("in_pullback", "==", True),
                    _c("pullback_volume_ratio", "<=", _THR["low_volume_pullback_ratio"])]},
    {"key": "contraction_then_expansion", "label": "Contraction → expansion",
     "description": "Prior-5 average volume at or below 0.8× the prior-20 average, then elevated volume today.",
     "conditions": [_c("contraction_ratio_5_20", "<=", _THR["contraction_ratio"]),
                    _c("rvol_20", ">=", _THR["elevated_rvol"])]},
    {"key": "distribution_caution", "label": "Distribution-like / caution",
     "description": "Elevated volume, a close in the lower quarter of the range, and at least one "
                    "distribution-like session in the last 10.",
     "conditions": [_c("rvol_20", ">=", _THR["elevated_rvol"]), _c("close_location", "<=", _THR["weak_close"]),
                    _c("distribution_like_10", ">=", 1)]},
)
PRESETS_BY_KEY = {p["key"]: p for p in PRESETS}


class FilterError(ValueError):
    """A filter, preset or sort the scanner does not define."""


def conditions_from_params(params: dict) -> list[dict]:
    """Explicit filter query parameters (and an optional preset) as the
    condition list the rows are matched against. Nothing is on by default."""
    conditions: list[dict] = []
    preset = params.get("preset")
    if preset:
        if preset not in PRESETS_BY_KEY:
            raise FilterError(f"unknown preset {preset!r}; one of {sorted(PRESETS_BY_KEY)}")
        conditions.extend(dict(c) for c in PRESETS_BY_KEY[preset]["conditions"])
    for name, (field, op) in NUMERIC_FILTERS.items():
        raw = params.get(name)
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise FilterError(f"{name} must be a number")
        if value != value or value in (float("inf"), float("-inf")):
            raise FilterError(f"{name} must be a finite number")
        conditions.append(_c(field, op, value))
    for name, choices in CHOICE_FILTERS.items():
        raw = params.get(name)
        if raw is None or raw == "":
            continue
        if raw not in choices:
            raise FilterError(f"{name} must be one of {sorted(choices)}")
        conditions.extend(_c(*c) for c in choices[raw])
    for name, (field, op) in TEXT_FILTERS.items():
        raw = params.get(name)
        if raw:
            conditions.append(_c(field, op, str(raw)))
    return conditions


def uses_score(conditions: list[dict]) -> bool:
    return any(c["field"] == "sms_score" for c in conditions)


def matches(row: dict, conditions: list[dict]) -> bool:
    """AND of every condition. A missing (null) value never matches: an
    unavailable feature is not treated as passing or failing a threshold."""
    for c in conditions:
        value = row.get(c["field"])
        if value is None:
            return False
        if c["op"] == ">=":
            ok = value >= c["value"]
        elif c["op"] == "<=":
            ok = value <= c["value"]
        elif c["op"] == "==":
            ok = value == c["value"]
        else:
            raise FilterError(f"unknown operator {c['op']!r}")
        if not ok:
            return False
    return True


def sort_rows(rows: list[dict], key: str | None = None, order: str | None = None) -> list[dict]:
    """A user-chosen sort. Missing values always last; ties by symbol, then
    instrument token, so the same snapshot always gives the same order."""
    key = key or DEFAULT_SORT["key"]
    order = order or DEFAULT_SORT["order"]
    if key not in SORTABLE:
        raise FilterError(f"sort must be one of {list(SORTABLE)}")
    if order not in ("asc", "desc"):
        raise FilterError("order must be asc or desc")
    tie = sorted(rows, key=lambda r: (r["symbol"], r["instrument_token"]))
    present = [r for r in tie if r.get(key) is not None]
    missing = [r for r in tie if r.get(key) is None]
    present.sort(key=lambda r: r[key], reverse=order == "desc")   # stable: ties keep symbol order
    return present + missing


# ---------------------------------------------------------------- batch loading (one query per concern)
_SEGMENT_EXCHANGE = {1: "NSE", 4: "BSE"}   # a Kite token's low byte is its exchange segment


def _marks(n: int) -> str:
    return ",".join("?" * n)


def load_universe(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT symbol, instrument_token, tag FROM watchlist WHERE account_id = ? ORDER BY symbol, instrument_token",
        (account_id,),
    ).fetchall()
    return [{"symbol": r["symbol"].upper(), "instrument_token": r["instrument_token"], "tag": r["tag"]} for r in rows]


def load_windows(conn: sqlite3.Connection, tokens: list[int], as_of: str | None) -> tuple[dict, dict]:
    """The last WINDOW_ROWS candles on or before `as_of` of every token, in
    one query, and each token's candle count on or before `as_of`."""
    if not tokens:
        return {}, {}
    date_sql, args = ("AND date <= ?", [as_of]) if as_of else ("", [])
    rows = conn.execute(
        f"""
        SELECT instrument_token, date, open, high, low, close, volume FROM (
            SELECT instrument_token, date, open, high, low, close, volume,
                   ROW_NUMBER() OVER (PARTITION BY instrument_token ORDER BY date DESC) AS rn
            FROM ohlc_daily WHERE instrument_token IN ({_marks(len(tokens))}) {date_sql}
        ) WHERE rn <= ? ORDER BY instrument_token, date
        """,
        (*tokens, *args, WINDOW_ROWS),
    ).fetchall()
    grouped: dict[int, list] = {t: [] for t in tokens}
    for r in rows:
        grouped[r["instrument_token"]].append(
            {"date": r["date"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
             "volume": r["volume"]})
    counts = {r["instrument_token"]: r["n"] for r in conn.execute(
        f"SELECT instrument_token, COUNT(*) AS n FROM ohlc_daily WHERE instrument_token IN ({_marks(len(tokens))}) "
        f"{date_sql} GROUP BY instrument_token",
        (*tokens, *args),
    ).fetchall()}
    return {t: pd.DataFrame(v) for t, v in grouped.items()}, counts


def load_statuses(conn: sqlite3.Connection, tokens: list[int]) -> dict[int, tuple]:
    """PK-22 history status and verified gaps per token (absent = unknown)."""
    if not tokens:
        return {}
    out = {}
    for r in conn.execute(
        f"SELECT instrument_token, status, verified_gaps FROM ohlc_history_status "
        f"WHERE instrument_token IN ({_marks(len(tokens))})", tokens,
    ).fetchall():
        out[r["instrument_token"]] = (r["status"], json.loads(r["verified_gaps"]) if r["verified_gaps"] else [])
    return out


def load_identity(conn: sqlite3.Connection, universe: list[dict]) -> tuple[dict, dict]:
    """Exchange per token (instrument cache, else the token's segment byte)
    and every OTHER stored series of each watched symbol — kept separate."""
    tokens = [u["instrument_token"] for u in universe]
    symbols = sorted({u["symbol"] for u in universe})
    if not tokens:
        return {}, {}
    exchange = {r["instrument_token"]: r["exchange"] for r in conn.execute(
        f"SELECT instrument_token, exchange FROM instrument_cache WHERE instrument_token IN ({_marks(len(tokens))})",
        tokens,
    ).fetchall()}
    for t in tokens:
        exchange.setdefault(t, _SEGMENT_EXCHANGE.get(t & 0xFF, "UNKNOWN"))
    stored: dict[str, set] = {}
    for r in conn.execute(
        f"SELECT DISTINCT UPPER(symbol) AS symbol, instrument_token FROM ohlc_daily "
        f"WHERE UPPER(symbol) IN ({_marks(len(symbols))})", symbols,
    ).fetchall():
        stored.setdefault(r["symbol"], set()).add(r["instrument_token"])
    others = {}
    for u in universe:
        extra = sorted(stored.get(u["symbol"], set()) - {u["instrument_token"]})
        others[u["instrument_token"]] = [{"instrument_token": t, "exchange": _SEGMENT_EXCHANGE.get(t & 0xFF, "UNKNOWN")}
                                         for t in extra]
    return exchange, others


def load_active_signals(conn: sqlite3.Connection, account_id: int) -> dict[int, list]:
    """Strategy ENTRY signals currently awaiting a decision, per token. Read
    only; shown beside the evidence, never combined with it."""
    out: dict[int, list] = {}
    for r in conn.execute(
        """
        SELECT id, instrument_token, strategy_name, signal_date, status FROM trade_tickets
        WHERE account_id = ? AND source = 'strategy' AND closes_ticket_id IS NULL AND transaction_type = 'BUY'
          AND status IN ('draft', 'pending_confirmation') AND archived = 0
        ORDER BY strategy_name, id
        """,
        (account_id,),
    ).fetchall():
        out.setdefault(r["instrument_token"], []).append(
            {"strategy": r["strategy_name"], "signal_date": r["signal_date"], "ticket_id": r["id"],
             "status": "awaiting approval" if r["status"] == "pending_confirmation" else "draft"})
    return out


# ---------------------------------------------------------------- per-instrument evaluation, cached
_CACHE_SIZE = 512
_cache: "OrderedDict[tuple, str]" = OrderedDict()
_cache_lock = Lock()
cache_stats = {"hits": 0, "misses": 0}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
        cache_stats.update(hits=0, misses=0)


def cache_key(token: int, symbol: str, frame: pd.DataFrame, bars: int, history_status, verified,
              requested: str | None, last_session: str) -> tuple:
    """Everything the cached evidence depends on. The history digest is over
    exactly the rows read, so a refresh, a PK-22 rebuild or any edited bar
    gives a new key; the PK-22 status and verified gaps are in it as well,
    and so are both definition versions. `last_session` only matters for
    the latest view (staleness lowers the score's confidence)."""
    return (token, symbol, requested, vi._history_digest(frame), bars, history_status, tuple(sorted(verified)),
            vi.FEATURE_VERSION, sms.SMART_MONEY_SCORE_VERSION, sms.REQUIRED_FEATURE_VERSION,
            config.VOLUME_INTEL_LOW_LIQUIDITY_INR, None if requested else last_session)


def evaluate(token: int, symbol: str, frame: pd.DataFrame, bars: int, history_status, verified,
             requested: str | None, last_session: str) -> tuple[dict, dict]:
    """(PK-23 features, PK-24 score) for one instrument — the same objects
    features_for_instrument and score() produce. Each caller gets a copy."""
    frame = vi._normalise(frame)
    key = cache_key(token, symbol, frame, bars, history_status, verified, requested, last_session)
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            cache_stats["hits"] += 1
    if cached is None:
        features = vi.compute_at(frame, requested, symbol=symbol, instrument_token=token,
                                 history_status=history_status, verified_gaps=verified)
        features["data_quality"]["bars_available"] = bars   # the full count, not the window's
        # exactly features_for_instrument's post-processing
        features["freshness"] = vi._freshness(features["as_of_date"], features["requested_as_of"])
        if features["freshness"]["is_stale"]:
            features["warnings"].append(
                f"The newest stored candle is {features['as_of_date']}, {features['freshness']['age_days']} days "
                f"behind the last completed session — beyond the {config.MAX_DATA_STALENESS_DAYS}-day limit.")
        cached = json.dumps({"features": features, "score": sms.score(features)})
        with _cache_lock:
            cache_stats["misses"] += 1
            _cache[key] = cached
            while len(_cache) > _CACHE_SIZE:
                _cache.popitem(last=False)
    out = json.loads(cached)
    return out["features"], out["score"]


def smart_money_view(score: dict) -> dict:
    """The PK-24 score object as the scanner shows it: everything except the
    band label (a 75+ score is not presented as 'strong' — PK-25)."""
    return {k: v for k, v in score.items() if k != "band"}


def _volume_state(contracting, expanding):
    if contracting is None and expanding is None:
        return None
    if contracting and expanding:
        return "contraction_then_expansion"
    if expanding:
        return "expanding"
    if contracting:
        return "contracting"
    return "normal"


def _is_extreme_up_bar(price_change, rvol, close_location):
    if None in (price_change, rvol, close_location):
        return None
    return (price_change >= EXTREME_UP_BAR["price_change_pct"] and rvol >= EXTREME_UP_BAR["rvol_20"]
            and close_location >= EXTREME_UP_BAR["close_location"])


def build_row(entry: dict, exchange: str, other_series: list, features: dict, score: dict,
              signals: list) -> dict:
    """One flat, descriptive scanner row. Nothing here is a rank or a
    combination of the evidence with the signal."""
    g = lambda group: features.get(group) or {}   # noqa: E731 — an UNAVAILABLE group is None
    rv, liq, st, obv = g("relative_volume"), g("liquidity"), g("structure"), g("obv")
    con, pb, ev, bo, an = g("contraction"), g("pullback"), g("evidence"), g("breakout"), g("anomalies")
    quality = features.get("data_quality") or {}
    codes = list(quality.get("codes") or ())
    contracting, expanding = con.get("pre_breakout_volume_contraction"), con.get("current_volume_expansion")
    notes = list(features.get("notes") or ())
    for other in other_series:
        notes.append(f"A {other['exchange']} series of {entry['symbol']} (token {other['instrument_token']}) is "
                     f"also stored; its volume is not comparable and is not combined with this one.")
    warnings = list(features.get("warnings") or ()) + [w for w in score.get("warnings") or ()
                                                       if w not in (features.get("warnings") or ())]
    components = score.get("components") or {}
    return {
        "instrument_token": entry["instrument_token"],
        "symbol": entry["symbol"],
        "exchange": exchange,
        "watchlist_tag": entry.get("tag"),
        "as_of_date": features.get("as_of_date"),
        "requested_as_of": features.get("requested_as_of"),
        "history_status": features.get("history_status"),
        "data_quality": quality.get("status"),
        "data_quality_codes": codes,
        "special_session": any(c.startswith("SPECIAL_SESSION") for c in codes),
        "bars_available": quality.get("bars_available"),
        "is_stale": (features.get("freshness") or {}).get("is_stale"),
        "age_days": (features.get("freshness") or {}).get("age_days"),
        "rvol_20": rv.get("rvol_20"),
        "rvol_median_20": rv.get("rvol_median_20"),
        "volume_percentile_60": rv.get("volume_percentile_60"),
        "volume": (features.get("bar") or {}).get("volume"),
        "traded_value": liq.get("traded_value"),
        "avg_traded_value_20": liq.get("avg_traded_value_20"),
        "relative_traded_value_20": liq.get("relative_traded_value_20"),
        "liquidity_warning": liq.get("liquidity_warning"),
        "price_change_pct": st.get("price_change_pct"),
        "close_location": st.get("close_location"),
        "high_volume_weak_close": st.get("high_volume_weak_close"),
        "obv_slope_20": obv.get("slope_20"),
        "obv_trend": obv.get("trend"),
        "obv_new_high_20": obv.get("obv_new_high_20"),
        "obv_divergence": obv.get("divergence"),
        "accumulation_like_10": ev.get("accumulation_like_10"),
        "distribution_like_10": ev.get("distribution_like_10"),
        "net_accumulation_10": ev.get("net_accumulation_10"),
        "accumulation_like_20": ev.get("accumulation_like_20"),
        "distribution_like_20": ev.get("distribution_like_20"),
        "contraction_ratio_5_20": con.get("contraction_ratio_5_20"),
        "volume_contracting": contracting,
        "volume_expanding": expanding,
        "volume_state": _volume_state(contracting, expanding),
        "is_breakout": bo.get("is_breakout"),
        "breakout_close_quality": bo.get("close_quality"),
        "in_pullback": pb.get("in_pullback"),
        "pullback_depth_pct": pb.get("pullback_depth_pct"),
        "pullback_volume_ratio": pb.get("pullback_volume_ratio"),
        "low_volume_pullback": pb.get("low_volume_pullback"),
        "abnormal_volume": an.get("abnormal_volume"),
        "extreme_volume": an.get("extreme_volume"),
        "extreme_up_bar": _is_extreme_up_bar(st.get("price_change_pct"), rv.get("rvol_20"),
                                             st.get("close_location")),
        "sms_score": score.get("score"),
        "sms_version": score.get("score_version"),
        "sms_confidence": score.get("confidence"),
        "sms_coverage": score.get("coverage"),
        # read from the score's own component structure — not the PK-24
        # research export's sms_failed_breakout column, which holds a flag
        "sms_failed_breakout_points": (components.get("failed_breakout") or {}).get("points"),
        "active_signals": [{k: s[k] for k in ("strategy", "signal_date", "status", "ticket_id")} for s in signals],
        "has_signal": bool(signals),
        "warnings": warnings,
        "notes": notes,
        "feature_version": features.get("feature_version"),
    }


# ---------------------------------------------------------------- the batch scan
def _account_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM accounts WHERE is_default = 1 ORDER BY id LIMIT 1").fetchone()
    return None if row is None else row["id"]


def build_scan(conn: sqlite3.Connection, as_of: str | None = None, only_token: int | None = None) -> dict:
    """Latest (or as-of) evidence for every watchlist instrument of the
    default account. Read-only: SELECTs only, no broker, no write."""
    account_id = _account_id(conn)
    universe = load_universe(conn, account_id) if account_id is not None else []
    if only_token is not None:
        universe = [u for u in universe if u["instrument_token"] == only_token]
    tokens = [u["instrument_token"] for u in universe]
    frames, counts = load_windows(conn, tokens, as_of)
    statuses = load_statuses(conn, tokens)
    exchange, others = load_identity(conn, universe)
    # Current signals belong to the latest candle; an as-of view does not show them.
    signals = load_active_signals(conn, account_id) if (account_id is not None and not as_of) else {}
    last_session = trading_calendar.latest_completed_session().isoformat()

    rows, evidence = [], {}
    for entry in universe:
        token = entry["instrument_token"]
        status, verified = statuses.get(token, (None, []))
        features, score = evaluate(token, entry["symbol"], frames[token], counts.get(token, 0), status, verified,
                                   as_of, last_session)
        rows.append(build_row(entry, exchange[token], others[token], features, score, signals.get(token, [])))
        evidence[token] = {"features": features, "smart_money": smart_money_view(score)}
    available = sum(1 for r in rows if r["data_quality"] != vi.UNAVAILABLE)
    return {
        "rows": sort_rows(rows),
        "evidence": evidence,
        "universe": {"source": "watchlist", "account_found": account_id is not None, "instruments": len(rows),
                     "features_available": available, "features_unavailable": len(rows) - available},
        "requested_as_of": as_of,
        "latest_completed_session": last_session,
    }


def meta() -> dict:
    """Static description of the scanner: purpose, fields, filters, presets,
    sort policy and the Smart Money Score v1.0 display rules."""
    return {
        "scanner": {"name": SCANNER_NAME, "subtitle": SCANNER_SUBTITLE, "version": SCANNER_VERSION,
                    "purpose": PURPOSE, "ranking": None,
                    "ordering": "Rows are ordered only by the column you choose; there is no composite rank."},
        "versions": {"feature_version": vi.FEATURE_VERSION, "score_version": sms.SMART_MONEY_SCORE_VERSION},
        "fields": [{"name": n, "type": t, "label": lbl, "sortable": s} for n, t, lbl, s in FIELDS],
        "sort": {"default": dict(DEFAULT_SORT), "sortable": list(SORTABLE)},
        "filters": {"numeric": {k: {"field": f, "op": op} for k, (f, op) in NUMERIC_FILTERS.items()},
                    "choice": {k: {c: [_c(*x) for x in v] for c, v in ch.items()} for k, ch in CHOICE_FILTERS.items()},
                    "text": {k: {"field": f, "op": op} for k, (f, op) in TEXT_FILTERS.items()}},
        "presets": [{**p, "conditions": [dict(c) for c in p["conditions"]]} for p in PRESETS],
        "thresholds": {k: _THR[k] for k in ("elevated_rvol", "strong_close", "weak_close", "contraction_ratio",
                                            "low_volume_pullback_ratio", "abnormal_percentile", "obv_trend_slope")},
        "smart_money": {"label": SMS_LABEL, "name": sms.NAME, "version": sms.SMART_MONEY_SCORE_VERSION,
                        "disclaimer": sms.DISCLAIMER, "limitation": SMS_LIMITATION,
                        "filter_warning": SMS_FILTER_WARNING, "validation": SMS_VALIDATION,
                        "extreme_up_bar": {"definition": dict(EXTREME_UP_BAR), "note": EXTREME_UP_BAR_NOTE}},
    }


# ---------------------------------------------------------------- CSV export
CSV_COLUMNS = (
    "scanner_version", "feature_version", "as_of_date", "requested_as_of", "instrument_token", "symbol", "exchange",
    "history_status", "data_quality", "data_quality_codes", "special_session", "is_stale",
    "rvol_20", "rvol_median_20", "volume_percentile_60", "volume", "traded_value", "avg_traded_value_20",
    "relative_traded_value_20", "liquidity_warning", "price_change_pct", "close_location", "high_volume_weak_close",
    "obv_slope_20", "obv_trend", "obv_new_high_20", "obv_divergence",
    "accumulation_like_10", "distribution_like_10", "net_accumulation_10", "accumulation_like_20",
    "distribution_like_20", "contraction_ratio_5_20", "volume_contracting", "volume_expanding", "volume_state",
    "is_breakout", "breakout_close_quality", "in_pullback", "pullback_depth_pct", "pullback_volume_ratio",
    "low_volume_pullback", "abnormal_volume", "extreme_volume", "extreme_up_bar",
    "sms_score", "sms_version", "sms_confidence", "sms_failed_breakout_points", "sms_validation_status",
    "active_strategy_signals", "warnings",
)


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return " | ".join(str(v) for v in value)
    if isinstance(value, float) and value == 0:
        return "0.0"          # a zero penalty is -0.0 in the score object; same number, plainer text
    return str(value)


def export_csv(rows: list[dict]) -> str:
    """The rows exactly as shown (already filtered and sorted), as CSV.
    Market data and derived evidence only: no ticket or order id, no
    holdings, no watchlist notes — and no forward outcome of any kind."""
    leaked = [c for c in CSV_COLUMNS if c.startswith("outcome_")]
    if leaked:
        raise ValueError(f"outcome columns must never be in the live scanner export: {leaked}")
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        record = {
            **r,
            "scanner_version": SCANNER_VERSION,
            "sms_validation_status": SMS_VALIDATION["status"],
            "active_strategy_signals": [f"{s['strategy']}@{s['signal_date']}" for s in r.get("active_signals") or ()],
        }
        writer.writerow([_cell(record.get(c)) for c in CSV_COLUMNS])
    return out.getvalue()
