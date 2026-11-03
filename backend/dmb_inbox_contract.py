"""Strict validator for Daily Market Byte handoff bundles (`dmb-candidate-bundle-1`).

Pure: no database, no file system, no broker. `backend/dmb_inbox.py` reads the file and calls
`parse_and_validate`; any failure raises `BundleRejected` with a stable reason code and the
whole bundle is refused (all or nothing - nothing from a bundle that fails is stored).

The wire contract is owned by DMB (daily_byte docs/PRIVATE_DESK_KITE_INTEGRATION.md). This
module re-implements it independently - the two repositories share no code - and adds the
checks only this side can make: Kite's own NSE trading calendar, the clock, and freshness.

What a valid packet is:
- exactly the 1.2 keys at every depth (an unknown key anywhere is refused), the right types,
  bounded sizes, depth and text;
- no order / account / credential field at any depth, after normalising the key
  (`positionSize`, `position-size` and `positionsize` are one key);
- an NSE symbol `^[A-Z0-9&-]{1,20}$`, listing exchange NSE;
- provenance (producer = Daily Market Byte's packet builder, source artifact ids) and quality
  metadata present;
- rights: handoff_status PERMITTED, every source PERMITTED, classification never a
  permission - a packet without that never enters Kite;
- packet_id and bundle_id recomputed from the content (tampering or corruption is refused);
- explicit-offset timestamps; generated_at not in the future; evidence after the session's
  close and not after generation;
- session_date a regular NSE session in a covered calendar year, and review_session_date the
  next regular session (Kite's calendar - a mismatch is refused, never re-interpreted);
- not stale: a packet is actionable until its review session completes (15:30 IST on
  review_session_date); a bundle already past that is refused at import.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, time, timedelta, timezone

from backend import trading_calendar

BUNDLE_SCHEMA = "dmb-candidate-bundle-1"
PACKET_SCHEMA = "private-candidate-packet-1.2"
PRODUCER = {"system": "daily-market-byte", "component": "private_desk", "packet_schema": PACKET_SCHEMA}
LISTING_EXCHANGE = "NSE"
SYNTHETIC_SOURCE = "synthetic_handoff_fixture"
DATA_ORIGINS = ("SYNTHETIC", "DMB_EVIDENCE")
DIRECTIONS = ("ALIGNED_POSITIVE", "ALIGNED_NEGATIVE", "MIXED", "NON_DIRECTIONAL")

MAX_BUNDLE_BYTES = 2_000_000
MAX_PACKETS = 250
MAX_DEPTH = 8            # per packet; a bundle adds 2
MAX_TEXT = 2_000
MAX_KEY = 64
MAX_LIST = 100
FUTURE_SKEW = timedelta(seconds=120)
SYMBOL_RE = re.compile(r"^[A-Z0-9&-]{1,20}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CODE_RE = re.compile(r"^[A-Z0-9_]{1,48}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?([+-]\d{2}:\d{2}|Z)$")

RADAR_STATE_KEYS = ("attention_level", "novelty_type", "appearance", "families", "direction",
                    "persistence", "selected_for_story")
METRIC_KEYS = ("price_change_pct", "relative_volume", "vs_nifty_5d_pp", "vs_nifty_20d_pp",
               "close", "replay_status")
MARKET_CONTEXT_KEYS = ("advances", "declines", "unchanged", "breadth_denominator",
                       "unusual_volume", "unusual_volume_den", "range_up", "range_down",
                       "range_den", "sectors_more_adv", "sectors_more_dec", "sector_count",
                       "nifty_change_pct", "india_vix")
SECTOR_CONTEXT_KEYS = ("sector", "constituents", "advances", "declines", "unchanged",
                       "breadth_covered", "unusual_volume", "range_up", "range_down",
                       "new_52w_high", "new_52w_low", "median_change_pct", "radar_candidates")
MARKET_REGIME_KEYS = ("label", "session_date", "calculation_version", "dimensions", "reason_code")
REGIME_DIMENSION_KEYS = ("TREND", "BREADTH", "SECTORS", "VOLUME", "VOLATILITY", "RELATIVE", "FLOWS")
OFFICIAL_EVENT_KEYS = ("kind", "status", "source_date")
QUALITY_KEYS = ("freshness_ok", "warnings", "replay_status", "radar_coverage_ok")
RIGHTS_KEYS = ("research_classification", "classification_is_permission", "public_publication",
               "data_origin", "handoff_status", "handoff_basis", "sources")
RIGHTS_SOURCE_KEYS = ("source_name", "handoff_status", "basis")
PACKET_KEYS = ("schema_version", "packet_id", "session_date", "listing_exchange", "symbol",
               "company", "sector", "radar_state", "detector_ids", "reasons", "metrics",
               "market_context", "sector_context", "official_events", "source_artifact_ids",
               "evidence_as_of", "review_session_date", "quality", "producer", "rights",
               "generated_at", "notes", "market_regime")
BUNDLE_KEYS = ("schema_version", "bundle_id", "session_date", "generated_at", "packet_count",
               "packets")


class BundleRejected(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _reject(code: str, detail: str):
    raise BundleRejected(code, detail)


# ---------------------------------------------------------------- forbidden keys
_FORBIDDEN_WORDS = frozenset({
    "order", "orders", "quantity", "qty", "trigger", "stop", "stoploss", "target", "entry",
    "exit", "side", "transaction", "buy", "sell", "account", "product", "lot", "leverage",
    "broker", "tag", "sl", "tp", "credential", "password", "secret", "token", "pnl",
    "margin", "holding", "holdings", "position", "positions", "variety", "validity", "gtt",
    "amo", "squareoff",
})
_FORBIDDEN_PHRASES = tuple(tuple(p.split()) for p in (
    "position size", "limit price", "price limit", "exchange order", "stop loss",
    "take profit", "trade side", "order type", "order id", "api key", "access token",
    "client id", "user id", "trigger price", "entry price", "exit price", "target price",
    "stop price", "risk amount", "trade direction", "order instruction",
))
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def key_words(key: str) -> list:
    spaced = _CAMEL.sub(" ", str(key))
    return [w for w in re.split(r"[^0-9A-Za-z]+", spaced.lower()) if w]


def key_is_forbidden(key: str) -> bool:
    words = key_words(key)
    joined = "".join(words)
    for w in words:
        if w in _FORBIDDEN_WORDS or (w.endswith("s") and w[:-1] in _FORBIDDEN_WORDS):
            return True
    if joined in _FORBIDDEN_WORDS:
        return True
    for phrase in _FORBIDDEN_PHRASES:
        n = len(phrase)
        if any(tuple(words[i:i + n]) == phrase for i in range(len(words) - n + 1)):
            return True
        if "".join(phrase) in joined:
            return True
    return False


def forbidden_keys(obj, path: str = "$") -> list:
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if key_is_forbidden(k):
                found.append(f"{path}.{k}")
            found += forbidden_keys(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += forbidden_keys(v, f"{path}[{i}]")
    return found


# ---------------------------------------------------------------- canonical ids
def _normalise(obj):
    if isinstance(obj, float):
        if not math.isfinite(obj):
            _reject("MALFORMED", "NaN or Infinity")
        return 0.0 if obj == 0 else obj
    if isinstance(obj, dict):
        return {k: _normalise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalise(v) for v in obj]
    return obj


def canonical_bytes(obj) -> bytes:
    return json.dumps(_normalise(obj), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def packet_id_for(packet: dict) -> str:
    body = {k: v for k, v in packet.items() if k not in ("generated_at", "packet_id")}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def bundle_id_for(session_date: str, packet_ids) -> str:
    return hashlib.sha256(canonical_bytes({"schema_version": BUNDLE_SCHEMA,
                                           "session_date": session_date,
                                           "packet_ids": sorted(packet_ids)})).hexdigest()


# ---------------------------------------------------------------- JSON parsing
def _no_duplicate_keys(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            _reject("CORRUPT_JSON", f"duplicate key {k!r}")
        out[k] = v
    return out


def _no_constants(name):
    _reject("CORRUPT_JSON", f"{name} is not JSON")


def parse_json(raw: bytes):
    if len(raw) > MAX_BUNDLE_BYTES:
        _reject("BUNDLE_TOO_LARGE", f"{len(raw)} bytes > {MAX_BUNDLE_BYTES}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _reject("CORRUPT_JSON", "not UTF-8")
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constants)
    except BundleRejected:
        raise
    except RecursionError:
        _reject("TOO_DEEP", "nesting too deep to parse")
    except ValueError as exc:
        _reject("CORRUPT_JSON", str(exc)[:200])


# ---------------------------------------------------------------- shape helpers
def _depth(obj, d=0) -> int:
    if isinstance(obj, dict):
        return max([_depth(v, d + 1) for v in obj.values()] or [d + 1])
    if isinstance(obj, list):
        return max([_depth(v, d + 1) for v in obj] or [d + 1])
    return d


def _bounded(obj, path="$"):
    if isinstance(obj, str):
        if len(obj) > MAX_TEXT:
            _reject("TEXT_TOO_LONG", f"{path} is longer than {MAX_TEXT} characters")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if len(k) > MAX_KEY:
                _reject("TEXT_TOO_LONG", f"a key at {path} is longer than {MAX_KEY} characters")
            _bounded(v, f"{path}.{k}")
    elif isinstance(obj, list):
        if len(obj) > MAX_LIST:
            _reject("LIST_TOO_LONG", f"{path} has more than {MAX_LIST} items")
        for i, v in enumerate(obj):
            _bounded(v, f"{path}[{i}]")


def _keys(obj, allowed, path, required=()):
    if not isinstance(obj, dict):
        _reject("MALFORMED", f"{path} must be an object")
    unknown = sorted(set(obj) - set(allowed))
    if unknown:
        _reject("UNKNOWN_KEY", f"{path} has unknown key(s) {unknown}")
    missing = sorted(set(required) - set(obj))
    if missing:
        _reject("MISSING_KEY", f"{path} is missing {missing}")


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _num_or_none(v, path):
    if v is not None and not _is_num(v):
        _reject("MALFORMED", f"{path} must be a number or null")


def _str_or_none(v, path):
    if v is not None and not isinstance(v, str):
        _reject("MALFORMED", f"{path} must be a string or null")


def _code(v, path, nullable=False):
    if v is None and nullable:
        return
    if not isinstance(v, str) or not CODE_RE.fullmatch(v):
        _reject("MALFORMED", f"{path} must be an upper-case code")


def _str_list(v, path):
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        _reject("MALFORMED", f"{path} must be an array of strings")


def parse_date(v, path) -> date:
    if not isinstance(v, str) or not DATE_RE.fullmatch(v):
        _reject("INVALID_SESSION_DATE", f"{path} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(v)
    except ValueError:
        _reject("INVALID_SESSION_DATE", f"{path} is not a calendar date")


def parse_timestamp(v, path) -> datetime:
    if not isinstance(v, str) or not TS_RE.fullmatch(v):
        _reject("INVALID_TIMESTAMP", f"{path} must be ISO 8601 with an explicit offset")
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        _reject("INVALID_TIMESTAMP", f"{path} is not a valid timestamp")
    if t.tzinfo is None or t.utcoffset() is None:
        _reject("INVALID_TIMESTAMP", f"{path} is timezone-naive")
    return t


def utc_sql(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- session semantics
def expires_at(review_session_date: date) -> datetime:
    """A packet is actionable until its review session completes: 15:30 IST that day."""
    return datetime.combine(review_session_date, trading_calendar.MARKET_CLOSE,
                            tzinfo=trading_calendar.IST)


def check_sessions(session: date, review: date, now: datetime) -> None:
    """Kite's calendar, fail closed: both years covered, session_date a regular session that
    has completed, review_session_date exactly the next regular session."""
    for d, path in ((session, "session_date"), (review, "review_session_date")):
        if trading_calendar.coverage(d.year) != "COVERED":
            _reject("CALENDAR_UNCOVERED", f"{path} {d} is in a year the NSE calendar does not cover")
    if not trading_calendar.is_trading_day(session):
        _reject("INVALID_SESSION_DATE", f"{session} is not a regular NSE session")
    if trading_calendar.latest_completed_session(now) < session:
        _reject("INVALID_SESSION_DATE", f"session {session} has not completed yet")
    try:
        expected = trading_calendar.next_trading_day(session)
    except trading_calendar.CalendarDataError as exc:
        _reject("CALENDAR_UNCOVERED", str(exc))
    if review != expected:
        _reject("REVIEW_SESSION_MISMATCH",
                f"review_session_date {review} is not the next NSE session after {session} ({expected})")


# ---------------------------------------------------------------- rights
def rights_permitted(r: dict) -> bool:
    """PERMITTED only when DMB explicitly permitted every source; a classification never
    counts. A synthetic packet must rest on the synthetic source alone."""
    if not isinstance(r, dict) or r.get("handoff_status") != "PERMITTED":
        return False
    if r.get("classification_is_permission") is not False:
        return False
    sources = r.get("sources") or []
    if not sources or any(s.get("handoff_status") != "PERMITTED" for s in sources):
        return False
    if r.get("data_origin") not in DATA_ORIGINS:
        return False
    if r.get("data_origin") == "SYNTHETIC" and [s.get("source_name") for s in sources] != [SYNTHETIC_SOURCE]:
        return False
    return True


# ---------------------------------------------------------------- packet / bundle
def validate_packet(p, now: datetime) -> dict:
    """Raises BundleRejected; returns the parsed facts the inbox stores."""
    hits = forbidden_keys(p)
    if hits:
        _reject("FORBIDDEN_FIELD", f"order / account field(s) {hits[:5]}")
    _keys(p, PACKET_KEYS, "$", required=PACKET_KEYS)
    if p["schema_version"] != PACKET_SCHEMA:
        _reject("UNSUPPORTED_PACKET_SCHEMA", repr(p["schema_version"])[:80])
    if _depth(p) > MAX_DEPTH:
        _reject("TOO_DEEP", f"packet nesting deeper than {MAX_DEPTH}")
    _bounded(p)
    if p["listing_exchange"] != LISTING_EXCHANGE:
        _reject("UNSUPPORTED_EXCHANGE", repr(p["listing_exchange"])[:40])
    if not isinstance(p["symbol"], str) or not SYMBOL_RE.fullmatch(p["symbol"]):
        _reject("INVALID_SYMBOL", repr(p["symbol"])[:40])
    if not isinstance(p["packet_id"], str) or not HEX64_RE.fullmatch(p["packet_id"]):
        _reject("MALFORMED", "packet_id must be 64 lower-case hex characters")
    _str_or_none(p["company"], "$.company")
    _str_or_none(p["sector"], "$.sector")

    rs = p["radar_state"]
    _keys(rs, RADAR_STATE_KEYS, "$.radar_state", required=RADAR_STATE_KEYS)
    for k in ("attention_level", "novelty_type", "appearance", "persistence"):
        _code(rs[k], f"$.radar_state.{k}", nullable=True)
    if rs["direction"] not in DIRECTIONS:
        _reject("MALFORMED", "radar_state.direction is not an evidence direction")
    _str_list(rs["families"], "$.radar_state.families")
    if not isinstance(rs["selected_for_story"], bool):
        _reject("MALFORMED", "radar_state.selected_for_story must be a boolean")

    m = p["metrics"]
    _keys(m, METRIC_KEYS, "$.metrics", required=METRIC_KEYS)
    for k in METRIC_KEYS:
        if k != "replay_status":
            _num_or_none(m[k], f"$.metrics.{k}")
    _code(m["replay_status"], "$.metrics.replay_status", nullable=True)
    for name, keys in (("market_context", MARKET_CONTEXT_KEYS), ("sector_context", SECTOR_CONTEXT_KEYS)):
        _keys(p[name], keys, f"$.{name}")
        for k, v in p[name].items():
            if k == "sector":
                _str_or_none(v, f"$.{name}.{k}")
            else:
                _num_or_none(v, f"$.{name}.{k}")
    mr = p["market_regime"]
    _keys(mr, MARKET_REGIME_KEYS, "$.market_regime")
    for k in ("label", "session_date", "calculation_version", "reason_code"):
        if k in mr:
            _str_or_none(mr[k], f"$.market_regime.{k}")
    if "dimensions" in mr:
        _keys(mr["dimensions"], REGIME_DIMENSION_KEYS, "$.market_regime.dimensions")
        for k, v in mr["dimensions"].items():
            _code(v, f"$.market_regime.dimensions.{k}", nullable=True)
    if not isinstance(p["official_events"], list):
        _reject("MALFORMED", "official_events must be an array")
    for i, e in enumerate(p["official_events"]):
        _keys(e, OFFICIAL_EVENT_KEYS, f"$.official_events[{i}]", required=("kind", "status"))
        for k, v in e.items():
            _str_or_none(v, f"$.official_events[{i}].{k}")
    for name in ("detector_ids", "reasons", "notes", "source_artifact_ids"):
        _str_list(p[name], f"$.{name}")

    if p["producer"] != PRODUCER:
        _reject("MISSING_PROVENANCE", "producer is not Daily Market Byte's packet builder")
    if not p["source_artifact_ids"]:
        _reject("MISSING_PROVENANCE", "no source artifact ids")
    q = p["quality"]
    _keys(q, QUALITY_KEYS, "$.quality", required=QUALITY_KEYS)
    if not isinstance(q["freshness_ok"], bool) or not isinstance(q["radar_coverage_ok"], bool):
        _reject("MISSING_QUALITY", "quality flags must be booleans")
    _str_list(q["warnings"], "$.quality.warnings")
    if q["replay_status"] != m["replay_status"]:
        _reject("MISSING_QUALITY", "quality.replay_status disagrees with metrics.replay_status")

    r = p["rights"]
    _keys(r, RIGHTS_KEYS, "$.rights", required=RIGHTS_KEYS)
    if not isinstance(r["sources"], list):
        _reject("RIGHTS_NOT_APPROVED", "rights.sources must be an array")
    for i, s in enumerate(r["sources"]):
        _keys(s, RIGHTS_SOURCE_KEYS, f"$.rights.sources[{i}]", required=RIGHTS_SOURCE_KEYS)
    if not rights_permitted(r):
        _reject("RIGHTS_NOT_APPROVED", "private handoff is not permitted for every source of this packet")

    session = parse_date(p["session_date"], "$.session_date")
    review = parse_date(p["review_session_date"], "$.review_session_date")
    check_sessions(session, review, now)
    as_of = parse_timestamp(p["evidence_as_of"], "$.evidence_as_of")
    generated = parse_timestamp(p["generated_at"], "$.generated_at")
    if generated > now + FUTURE_SKEW:
        _reject("FUTURE_TIMESTAMP", "generated_at is in the future")
    if as_of > generated:
        _reject("INVALID_TIMESTAMP", "evidence_as_of is after generated_at")
    close = datetime.combine(session, trading_calendar.MARKET_CLOSE, tzinfo=trading_calendar.IST)
    if as_of < close:
        _reject("INVALID_TIMESTAMP", "evidence_as_of is before the session's close")
    if now >= expires_at(review):
        _reject("STALE_EVIDENCE", f"the review session {review} has completed")

    if p["packet_id"] != packet_id_for(p):
        _reject("PACKET_ID_MISMATCH", "packet_id does not match the packet content")
    return {"session": session, "review": review, "evidence_as_of": as_of, "generated_at": generated}


def validate_bundle(bundle, now: datetime) -> list:
    """Raises BundleRejected; returns one fact dict per packet (same order)."""
    _keys(bundle, BUNDLE_KEYS, "$", required=BUNDLE_KEYS)
    if bundle["schema_version"] != BUNDLE_SCHEMA:
        _reject("UNSUPPORTED_BUNDLE_SCHEMA", repr(bundle["schema_version"])[:80])
    if not isinstance(bundle["bundle_id"], str) or not HEX64_RE.fullmatch(bundle["bundle_id"]):
        _reject("MALFORMED", "bundle_id must be 64 lower-case hex characters")
    parse_date(bundle["session_date"], "$.session_date")
    generated = parse_timestamp(bundle["generated_at"], "$.generated_at")
    if generated > now + FUTURE_SKEW:
        _reject("FUTURE_TIMESTAMP", "bundle generated_at is in the future")
    packets = bundle["packets"]
    if not isinstance(packets, list) or not packets:
        _reject("MALFORMED", "packets must be a non-empty array")
    if len(packets) > MAX_PACKETS:
        _reject("BUNDLE_TOO_LARGE", f"more than {MAX_PACKETS} packets")
    if isinstance(bundle["packet_count"], bool) or bundle["packet_count"] != len(packets):
        _reject("MALFORMED", "packet_count does not match the packets")
    facts, ids, logical = [], set(), set()
    for p in packets:
        f = validate_packet(p, now)
        if p["session_date"] != bundle["session_date"]:
            _reject("MALFORMED", "a packet belongs to another session than its bundle")
        if p["packet_id"] in ids:
            _reject("DUPLICATE_PACKET_ID", f"packet {p['packet_id'][:12]} appears twice")
        key = logical_key(p)
        if key in logical:
            _reject("CONFLICTING_PACKET", f"two versions of {p['symbol']} in one bundle")
        ids.add(p["packet_id"])
        logical.add(key)
        facts.append(f)
    if bundle["bundle_id"] != bundle_id_for(bundle["session_date"], ids):
        _reject("BUNDLE_ID_MISMATCH", "bundle_id does not match its packets")
    return facts


def logical_key(p: dict) -> str:
    """One logical candidate = one Radar candidate of one session: DMB's
    radar_candidate_history primary key is (session_date, instrument)."""
    return f"DMB:{p['listing_exchange']}:{p['symbol']}:{p['session_date']}"


def parse_and_validate(raw: bytes, now: datetime) -> tuple:
    """`(bundle, facts)` or BundleRejected."""
    bundle = parse_json(raw)
    if not isinstance(bundle, dict):
        _reject("MALFORMED", "a bundle must be a JSON object")
    if _depth(bundle) > MAX_DEPTH + 2:
        _reject("TOO_DEEP", "bundle nesting too deep")
    return bundle, validate_bundle(bundle, now)


__all__ = ["BundleRejected", "parse_and_validate", "validate_bundle", "validate_packet",
           "forbidden_keys", "key_is_forbidden", "packet_id_for", "bundle_id_for", "logical_key",
           "rights_permitted", "expires_at", "check_sessions", "utc_sql", "BUNDLE_SCHEMA",
           "PACKET_SCHEMA", "SYMBOL_RE", "HEX64_RE", "DATE_RE", "MAX_BUNDLE_BYTES"]
