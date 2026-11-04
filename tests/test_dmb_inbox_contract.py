"""The strict DMB bundle validator (backend/dmb_inbox_contract.py): every failure class fails
closed with a stable code, and a well-formed synthetic bundle passes. Pure: no database,
no files, no broker."""
import copy
import json
from datetime import date, datetime, timedelta

import pytest

from backend import dmb_inbox_contract as contract
from backend import trading_calendar
from tests import dmb_bundles as B


def _ok():
    return B.bundle([B.packet("INFY"), B.packet("TCS", replay="NOT_REPLAYED")])


def _raw(b) -> bytes:
    return json.dumps(b).encode("utf-8")


def _code(raw: bytes, now=B.IMPORT_AT) -> str:
    with pytest.raises(contract.BundleRejected) as e:
        contract.parse_and_validate(raw, now)
    return e.value.code


def test_a_well_formed_bundle_passes():
    bundle, facts = contract.parse_and_validate(_raw(_ok()), B.IMPORT_AT)
    assert [p["symbol"] for p in bundle["packets"]] == ["INFY", "TCS"] and len(facts) == 2
    assert facts[0]["review"].isoformat() == B.REVIEW


def test_ids_are_recomputed_and_ignore_generated_at():
    p = B.packet()
    q = copy.deepcopy(p)
    q["generated_at"] = "2026-11-02T20:30:00+05:30"
    assert contract.packet_id_for(q) == p["packet_id"]
    q["metrics"]["close"] = 1534.6
    assert contract.packet_id_for(q) != p["packet_id"]
    assert contract.bundle_id_for(B.SESSION, ["b", "a"]) == contract.bundle_id_for(B.SESSION, ["a", "b"])


# --------------------------------------------------------------- 1-2. schema versions
def test_1_unsupported_bundle_schema():
    b = _ok()
    b["schema_version"] = "dmb-candidate-bundle-2"
    assert _code(_raw(b)) == "UNSUPPORTED_BUNDLE_SCHEMA"


def test_2_unsupported_packet_schema():
    p = B.packet()
    p["schema_version"] = "private-candidate-packet-1.1"
    assert _code(_raw(B.bundle([B.rehash(p)]))) == "UNSUPPORTED_PACKET_SCHEMA"


# --------------------------------------------------------------- 3-4. unknown and forbidden keys
@pytest.mark.parametrize("where", ["top", "radar_state", "quality", "rights", "rights.sources", "dimensions",
                                   "official_events", "bundle"])
def test_3_unknown_keys_at_any_depth(where):
    b = _ok()
    p = b["packets"][0]
    target = {"top": p, "radar_state": p["radar_state"], "quality": p["quality"], "rights": p["rights"],
              "rights.sources": p["rights"]["sources"][0], "dimensions": p["market_regime"]["dimensions"],
              "official_events": p["official_events"][0], "bundle": b}[where]
    target["surprise"] = 1
    assert _code(_raw(b)) == "UNKNOWN_KEY"


@pytest.mark.parametrize("key", ["side", "quantity", "qty", "entry_price", "stop_loss", "stopLoss", "target",
                                 "position_size", "positionSize", "PositionSize", "positionsize",
                                 "limit_price", "price_limit", "priceLimit", "exchange_order", "exchangeOrder",
                                 "leverage", "broker", "account_id", "order_id", "orderType", "transaction_type",
                                 "api_key", "accessToken", "product", "gtt", "trade_side"])
def test_4_forbidden_fields_at_any_depth_with_normalised_names(key):
    assert contract.key_is_forbidden(key)
    b = _ok()
    b["packets"][0]["sector_context"]["nested"] = [{"deeper": {key: 1}}]
    assert _code(_raw(b)) == "FORBIDDEN_FIELD"
    assert contract.forbidden_keys({"a": [{"b": {key: 1}}]}) == [f"$.a[0].b.{key}"]


def test_4_contract_keys_and_evidence_direction_are_not_forbidden():
    keys = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                keys.append(k)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(_ok())
    keys += list(contract.MARKET_CONTEXT_KEYS + contract.SECTOR_CONTEXT_KEYS + contract.REGIME_DIMENSION_KEYS)
    assert [k for k in keys if contract.key_is_forbidden(k)] == []
    assert not contract.key_is_forbidden("direction")


# --------------------------------------------------------------- 5-7. size, depth, text
def test_5_oversized_bundle():
    assert _code(b" " * (contract.MAX_BUNDLE_BYTES + 1)) == "BUNDLE_TOO_LARGE"
    b = B.bundle([B.packet("INFY")])
    b["packets"] = b["packets"] * (contract.MAX_PACKETS + 1)
    b["packet_count"] = len(b["packets"])
    assert _code(_raw(b)) == "BUNDLE_TOO_LARGE"


def test_6_excessive_nesting():
    b = _ok()
    deep = {}
    cur = deep
    for _ in range(12):
        cur["n"] = {}
        cur = cur["n"]
    b["packets"][0]["market_context"]["advances"] = deep
    assert _code(_raw(b)) == "TOO_DEEP"
    assert _code(b"[" * 100_000 + b"]" * 100_000) in ("TOO_DEEP", "CORRUPT_JSON")


def test_7_excessive_text_and_list_length():
    p = B.packet()
    p["reasons"] = ["x" * (contract.MAX_TEXT + 1)]
    assert _code(_raw(B.bundle([B.rehash(p)]))) == "TEXT_TOO_LONG"
    p = B.packet()
    p["detector_ids"] = ["X"] * (contract.MAX_LIST + 1)
    assert _code(_raw(B.bundle([B.rehash(p)]))) == "LIST_TOO_LONG"


# --------------------------------------------------------------- 8-9. symbol, exchange
@pytest.mark.parametrize("symbol", ["infy", "INFY.NS", "../INFY", "A" * 21, "", "IN FY", "INFY;", "ИНФИ"])
def test_8_invalid_symbol(symbol):
    assert _code(_raw(B.bundle([B.packet(symbol)]))) == "INVALID_SYMBOL"


def test_8_valid_symbol_shapes():
    for s in ("M&M", "BAJAJ-AUTO", "3MINDIA"):
        assert contract.SYMBOL_RE.fullmatch(s)


def test_9_unsupported_exchange():
    p = B.packet()
    p["listing_exchange"] = "BSE"
    assert _code(_raw(B.bundle([B.rehash(p)]))) == "UNSUPPORTED_EXCHANGE"


# --------------------------------------------------------------- 10-12. provenance, quality, rights
@pytest.mark.parametrize("mutate,code", [
    (lambda p: p.update(producer={"system": "someone-else"}), "MISSING_PROVENANCE"),
    (lambda p: p.update(source_artifact_ids=[]), "MISSING_PROVENANCE"),
    (lambda p: p.pop("producer"), "MISSING_KEY"),
    (lambda p: p.pop("quality"), "MISSING_KEY"),
    (lambda p: p["quality"].update(freshness_ok="yes"), "MISSING_QUALITY"),
    (lambda p: p["quality"].pop("radar_coverage_ok"), "MISSING_KEY"),
    (lambda p: p["quality"].update(replay_status="REASON_CODES_DIFFER"), "MISSING_QUALITY"),
    (lambda p: p["rights"].update(handoff_status="BLOCKED"), "RIGHTS_NOT_APPROVED"),
    (lambda p: p["rights"]["sources"][0].update(handoff_status="UNRESOLVED"), "RIGHTS_NOT_APPROVED"),
    (lambda p: p["rights"].update(sources=[]), "RIGHTS_NOT_APPROVED"),
    (lambda p: p["rights"].update(classification_is_permission=True), "RIGHTS_NOT_APPROVED"),
    (lambda p: p["rights"].update(data_origin="UNKNOWN"), "RIGHTS_NOT_APPROVED"),
    (lambda p: p["rights"]["sources"].append({"source_name": "yahoo_finance", "handoff_status": "PERMITTED",
                                              "basis": "x"}), "RIGHTS_NOT_APPROVED"),   # synthetic + real
    (lambda p: p.pop("rights"), "MISSING_KEY"),
])
def test_10_to_12_provenance_quality_and_rights(mutate, code):
    p = B.packet()
    mutate(p)
    assert _code(_raw(B.bundle([B.rehash(p)]))) == code


def test_12_research_classification_alone_is_never_permission():
    p = B.packet()
    p["rights"].update(handoff_status="BLOCKED", research_classification="PRIVATE_RESEARCH_ONLY")
    assert not contract.rights_permitted(p["rights"])


# --------------------------------------------------------------- 13-17. timestamps and sessions
@pytest.mark.parametrize("field,value,code", [
    ("evidence_as_of", "2026-11-02T19:45:00", "INVALID_TIMESTAMP"),          # naive
    ("evidence_as_of", "2026-11-02 19:45:00+05:30", "INVALID_TIMESTAMP"),
    ("generated_at", "2026-11-02T20:00:00", "INVALID_TIMESTAMP"),
    ("generated_at", "not a time", "INVALID_TIMESTAMP"),
    ("evidence_as_of", "2026-11-02T14:00:00+05:30", "INVALID_TIMESTAMP"),    # before the close
    ("evidence_as_of", "2026-11-02T20:30:00+05:30", "INVALID_TIMESTAMP"),    # after generation
])
def test_13_invalid_or_naive_timestamps(field, value, code):
    p = B.packet()
    p[field] = value
    assert _code(_raw(B.bundle([B.rehash(p)]))) == code


def test_14_future_generated_at():
    p = B.packet(generated_at="2026-11-02T21:05:00+05:30")
    assert _code(_raw(B.bundle([p], generated_at="2026-11-02T20:00:00+05:30"))) == "FUTURE_TIMESTAMP"
    assert _code(_raw(B.bundle([B.packet()], generated_at="2026-11-02T22:00:00+05:30"))) == "FUTURE_TIMESTAMP"


def test_15_stale_evidence_is_refused_once_the_review_session_completed():
    raw = _raw(_ok())
    contract.parse_and_validate(raw, B.DURING_REVIEW)                         # still actionable
    assert _code(raw, now=B.AFTER_REVIEW) == "STALE_EVIDENCE"


@pytest.mark.parametrize("session,review,code", [
    ("2026-11-07", "2026-11-09", "INVALID_SESSION_DATE"),     # a Saturday
    ("2026-11-10", "2026-11-11", "INVALID_SESSION_DATE"),     # an NSE holiday
    ("2026-11-31", "2026-12-01", "INVALID_SESSION_DATE"),     # not a date
])
def test_16_invalid_session_date(session, review, code):
    p = B.packet(session=session, review=review, evidence_as_of=f"{session}T19:45:00+05:30"
                 if session != "2026-11-31" else "2026-11-02T19:45:00+05:30")
    assert _code(_raw(B.bundle([p], session=session))) == code


def test_17_review_session_must_be_kites_first_official_session():
    assert _code(_raw(B.bundle([B.packet(review="2026-11-04")]))) == "REVIEW_SESSION_MISMATCH"
    # Owner rule (2026-11-04): Friday 6 Nov -> Sunday 8 Nov Muhurat; every official session counts,
    # so a packet claiming Monday 9 Nov (the next REGULAR session) is refused: never extended.
    now = B.IMPORT_AT.replace(day=6)
    mon = B.packet(session="2026-11-06", review="2026-11-09", evidence_as_of="2026-11-06T19:45:00+05:30",
                   generated_at="2026-11-06T20:00:00+05:30")
    assert _code(_raw(B.bundle([mon], session="2026-11-06", generated_at="2026-11-06T20:00:00+05:30")),
                 now=now) == "REVIEW_SESSION_MISMATCH"
    ok = B.packet(session="2026-11-06", review="2026-11-08", evidence_as_of="2026-11-06T19:45:00+05:30",
                  generated_at="2026-11-06T20:00:00+05:30")
    contract.parse_and_validate(_raw(B.bundle([ok], session="2026-11-06",
                                              generated_at="2026-11-06T20:00:00+05:30")), now)


def _at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=trading_calendar.IST)


@pytest.mark.parametrize("session, review, now, code", [
    # regular: Mon 2 Nov -> Tue 3 Nov 15:30
    ("2026-11-02", "2026-11-03", _at(2026, 11, 2, 21), None),                   # before the review session
    ("2026-11-02", "2026-11-03", _at(2026, 11, 3, 11), None),                   # during it
    ("2026-11-02", "2026-11-03", _at(2026, 11, 3, 15, 30), "STALE_EVIDENCE"),   # at its close
    # Muhurat: Fri 6 Nov -> Sun 8 Nov 14:20-15:30; Monday is already too late
    ("2026-11-06", "2026-11-08", _at(2026, 11, 7, 10), None),
    ("2026-11-06", "2026-11-08", _at(2026, 11, 8, 14, 45), None),               # during Muhurat
    ("2026-11-06", "2026-11-08", _at(2026, 11, 8, 15, 30), "STALE_EVIDENCE"),
    ("2026-11-06", "2026-11-08", _at(2026, 11, 9, 10), "STALE_EVIDENCE"),
    # Budget Sunday: Fri 30 Jan -> Sun 1 Feb 09:15-15:30
    ("2026-01-30", "2026-02-01", _at(2026, 1, 31, 12), None),
    ("2026-01-30", "2026-02-01", _at(2026, 2, 1, 12), None),
    ("2026-01-30", "2026-02-01", _at(2026, 2, 1, 15, 30), "STALE_EVIDENCE"),
    ("2026-01-30", "2026-02-02", _at(2026, 1, 31, 12), "REVIEW_SESSION_MISMATCH"),  # Monday: never extended
    # holiday in between: Mon 9 Nov -> Wed 11 Nov (Tue 10 Nov holiday)
    ("2026-11-09", "2026-11-11", _at(2026, 11, 10, 12), None),
    ("2026-11-09", "2026-11-10", _at(2026, 11, 9, 21), "REVIEW_SESSION_MISMATCH"),
    # a special session is never a packet session
    ("2026-11-08", "2026-11-09", _at(2026, 11, 8, 20), "INVALID_SESSION_DATE"),
    ("2026-02-01", "2026-02-02", _at(2026, 2, 1, 20), "INVALID_SESSION_DATE"),
    # missing calendar data
    ("2026-12-31", "2027-01-01", _at(2026, 12, 31, 21), "CALENDAR_UNCOVERED"),
])
def test_packet_expiry_follows_every_official_session(session, review, now, code):
    created = (datetime.fromisoformat(session + "T20:00:00+05:30") if now.date().isoformat() != session
               else now - timedelta(minutes=30))
    p = B.packet(session=session, review=review, evidence_as_of=(created - timedelta(minutes=15)).isoformat(),
                 generated_at=created.isoformat())
    raw = _raw(B.bundle([p], session=session, generated_at=created.isoformat()))
    if code is None:
        contract.parse_and_validate(raw, now)
    else:
        assert _code(raw, now=now) == code


def test_expiry_is_the_official_close_and_fails_closed():
    assert contract.expires_at(date(2026, 11, 3)) == _at(2026, 11, 3, 15, 30)
    assert contract.expires_at(date(2026, 11, 8)) == _at(2026, 11, 8, 15, 30)      # Muhurat close
    assert contract.expires_at(date(2026, 2, 1)) == _at(2026, 2, 1, 15, 30)        # Budget close
    for not_a_session in (date(2026, 11, 10), date(2026, 11, 7), date(2027, 1, 4)):
        with pytest.raises(contract.BundleRejected) as e:
            contract.expires_at(not_a_session)
        assert e.value.code == "CALENDAR_UNCOVERED"
    # An unevidenced special session is UNVERIFIED: the walk refuses rather than skipping it.
    with trading_calendar.use_calendar(special_sessions=[date(2026, 11, 7)], covered_years=[2026]):
        assert trading_calendar.session_record(date(2026, 11, 7))["session_type"] == "UNVERIFIED"
        with pytest.raises(trading_calendar.CalendarDataError):
            trading_calendar.next_official_session(date(2026, 11, 6))


def test_missing_calendar_coverage_fails_closed():
    """2027 is not in the NSE holiday file: never assume it trades (the 2027 gate stays)."""
    assert trading_calendar.coverage(2027) == "UNCOVERED"
    p = B.packet(session="2026-12-31", review="2027-01-01", evidence_as_of="2026-12-31T19:45:00+05:30",
                 generated_at="2026-12-31T20:00:00+05:30")
    now = B.IMPORT_AT.replace(month=12, day=31)
    assert _code(_raw(B.bundle([p], session="2026-12-31", generated_at="2026-12-31T20:00:00+05:30")),
                 now=now) == "CALENDAR_UNCOVERED"


def test_a_session_that_has_not_completed_is_refused():
    with pytest.raises(contract.BundleRejected) as e:
        contract.check_sessions(B.date_of("2026-11-03"), B.date_of("2026-11-04"), B.DURING_REVIEW, B.DURING_REVIEW)
    assert e.value.code == "INVALID_SESSION_DATE"


# --------------------------------------------------------------- 18-19. corrupt JSON, duplicates
@pytest.mark.parametrize("raw", [b"", b"{", b"\xff\xfe", b"[]", b'{"a": NaN}', b'{"a": Infinity}',
                                 b"not json"])
def test_18_corrupt_json(raw):
    assert _code(raw) in ("CORRUPT_JSON", "MALFORMED")


def test_18_duplicate_json_keys_are_refused():
    raw = _raw(_ok())
    smuggled = raw.replace(b'"symbol": "INFY"', b'"symbol": "INFY", "symbol": "TCS"', 1)
    assert _code(smuggled) == "CORRUPT_JSON"


def test_19_duplicate_packet_ids_and_conflicting_versions():
    p = B.packet()
    b = B.bundle([p])
    b["packets"].append(copy.deepcopy(p))
    b["packet_count"] = 2
    assert _code(_raw(b)) == "DUPLICATE_PACKET_ID"
    v2 = B.packet(close=1540.0)                                  # same candidate, other content
    b = B.bundle([B.packet(), v2])
    assert _code(_raw(b)) == "CONFLICTING_PACKET"


def test_tampered_content_or_ids_are_refused():
    b = _ok()
    b["packets"][0]["metrics"]["close"] = 1.0                    # id no longer matches content
    assert _code(_raw(b)) == "PACKET_ID_MISMATCH"
    b = _ok()
    b["bundle_id"] = "0" * 64
    assert _code(_raw(b)) == "BUNDLE_ID_MISMATCH"
    b = _ok()
    b["packet_count"] = 5
    assert _code(_raw(b)) == "MALFORMED"


def test_evidence_direction_must_be_an_evidence_code():
    for bad in ("BUY", "SELL", "LONG", None):
        p = B.packet()
        p["radar_state"]["direction"] = bad
        assert _code(_raw(B.bundle([B.rehash(p)]))) == "MALFORMED"


# --------------------------------------------------------------- explicit review session (2026-11-04)
# A packet names its review session. It must be the first official session after session_date that
# was still open, or yet to open, when the packet was created: a special session that had closed by
# then is passed over (the packet is explicitly for the next one), a regular one never is (the
# evidence is no longer the latest). A packet for a session that had already closed is stale.
@pytest.mark.parametrize("session, created, review, now, code", [
    # regular: Mon 2 Nov -> Tue 3 Nov
    ("2026-11-02", _at(2026, 11, 2, 20), "2026-11-03", _at(2026, 11, 3, 11), None),           # before
    ("2026-11-02", _at(2026, 11, 3, 10), "2026-11-03", _at(2026, 11, 3, 11), None),           # during
    ("2026-11-02", _at(2026, 11, 3, 16), "2026-11-03", _at(2026, 11, 3, 17), "STALE_EVIDENCE"),  # after
    ("2026-11-02", _at(2026, 11, 3, 16), "2026-11-04", _at(2026, 11, 4, 10), "STALE_EVIDENCE"),  # never carried on
    ("2026-11-02", _at(2026, 11, 2, 14), "2026-11-03", _at(2026, 11, 2, 21), "INVALID_SESSION_DATE"),  # mid-session
    # weekend: Fri 18 Sep -> Mon 21 Sep
    ("2026-09-18", _at(2026, 9, 19, 11), "2026-09-21", _at(2026, 9, 21, 10), None),
    ("2026-09-18", _at(2026, 9, 21, 16), "2026-09-21", _at(2026, 9, 21, 17), "STALE_EVIDENCE"),
    # Muhurat: Fri 6 Nov, Sun 8 Nov 14:20-15:30, Mon 9 Nov
    ("2026-11-06", _at(2026, 11, 6, 20), "2026-11-08", _at(2026, 11, 7, 10), None),           # before Muhurat
    ("2026-11-06", _at(2026, 11, 8, 15), "2026-11-08", _at(2026, 11, 8, 15, 10), None),       # during Muhurat
    ("2026-11-06", _at(2026, 11, 8, 15), "2026-11-09", _at(2026, 11, 8, 15, 10), "REVIEW_SESSION_MISMATCH"),
    ("2026-11-06", _at(2026, 11, 7, 10), "2026-11-09", _at(2026, 11, 7, 11), "REVIEW_SESSION_MISMATCH"),
    ("2026-11-06", _at(2026, 11, 8, 16), "2026-11-09", _at(2026, 11, 9, 10), None),           # after: for Monday
    ("2026-11-06", _at(2026, 11, 8, 16), "2026-11-09", _at(2026, 11, 9, 15, 30), "STALE_EVIDENCE"),  # Monday close
    ("2026-11-06", _at(2026, 11, 8, 16), "2026-11-08", _at(2026, 11, 8, 17), "STALE_EVIDENCE"),
    ("2026-11-06", _at(2026, 11, 9, 16), "2026-11-11", _at(2026, 11, 10, 12), "STALE_EVIDENCE"),  # Monday closed too
    # Budget Sunday: Fri 30 Jan, Sun 1 Feb 09:15-15:30, Mon 2 Feb
    ("2026-01-30", _at(2026, 1, 31, 12), "2026-02-01", _at(2026, 2, 1, 12), None),
    ("2026-01-30", _at(2026, 2, 1, 16), "2026-02-02", _at(2026, 2, 2, 10), None),
    ("2026-01-30", _at(2026, 2, 1, 16), "2026-02-01", _at(2026, 2, 1, 17), "STALE_EVIDENCE"),
    ("2026-01-30", _at(2026, 1, 31, 12), "2026-02-02", _at(2026, 1, 31, 13), "REVIEW_SESSION_MISMATCH"),
    # holiday: Mon 9 Nov, Tue 10 Nov holiday, Wed 11 Nov
    ("2026-11-09", _at(2026, 11, 10, 12), "2026-11-11", _at(2026, 11, 10, 13), None),
    ("2026-11-09", _at(2026, 11, 10, 12), "2026-11-10", _at(2026, 11, 10, 13), "REVIEW_SESSION_MISMATCH"),
    # unknown calendar
    ("2026-12-31", _at(2026, 12, 31, 20), "2027-01-01", _at(2026, 12, 31, 21), "CALENDAR_UNCOVERED"),
])
def test_the_packet_names_its_review_session(session, created, review, now, code):
    p = B.packet(session=session, review=review, evidence_as_of=max(created - timedelta(minutes=15), datetime.combine(
        date.fromisoformat(session), trading_calendar.MARKET_CLOSE, tzinfo=trading_calendar.IST)).isoformat(),
        generated_at=created.isoformat())
    raw = _raw(B.bundle([p], session=session, generated_at=created.isoformat()))
    if code is None:
        contract.parse_and_validate(raw, now)
    else:
        assert _code(raw, now=now) == code


def test_review_session_for_is_the_next_official_session_at_the_close():
    """Created at its own session's close, every packet reviews on the first official session after
    it, for every regular day the calendar covers - the earlier rule, unchanged for that case."""
    d, checked = date(2025, 1, 1), 0
    while d < date(2026, 12, 31):
        if trading_calendar.is_trading_day(d):
            close = datetime.combine(d, trading_calendar.MARKET_CLOSE, tzinfo=trading_calendar.IST)
            assert trading_calendar.review_session_for(d, close) == trading_calendar.next_official_session(d), d
            checked += 1
        d += timedelta(days=1)
    assert checked > 480


def test_review_session_for_fails_closed_on_an_unevidenced_session():
    with trading_calendar.use_calendar(special_sessions=[date(2026, 11, 7)], covered_years=[2026]):
        with pytest.raises(trading_calendar.CalendarDataError):
            trading_calendar.review_session_for(date(2026, 11, 6), _at(2026, 11, 6, 20))
    with pytest.raises(trading_calendar.CalendarDataError):
        trading_calendar.review_session_for(date(2026, 12, 31), _at(2026, 12, 31, 20))
