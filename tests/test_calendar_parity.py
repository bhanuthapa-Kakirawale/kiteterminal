"""NSE calendar parity between Daily Market Byte and this terminal (docs/NSE_CALENDAR_MONITORING.md).

The two repositories keep independent copies of NSE's holiday circulars. A DMB packet's
`review_session_date` is computed by DMB and re-checked here, so the regular-session calendars
must agree for every year both cover, and both must fail closed for a year neither covers
(2027 until NSE publishes its circular). DMB runs in its own interpreter as a separate process,
read-only; skipped when the checkout or its venv is absent.

Readiness sprint (P2-G): both sides now carry typed, sourced session records (REGULAR /
SPECIAL_FULL / SPECIAL_SHORT / HOLIDAY / UNVERIFIED, open/close, official source, publication date,
verification) and must agree on EVERY field for every day of 2025-2026 and on UNVERIFIED for 2027.
The two differences found in the previous sprint are resolved from NSE's own circulars: DMB now
lists the 2026-02-01 Sunday Budget session (NSE/CMTR/72349, NSE/FAOP/72352), and DMB no longer
computes a review session for a special-session day (its own rule: session_date is a REGULAR
session), so both refuse such a packet.

Packet-expiry sprint (2026-11-04, owner rule): every officially notified session counts, so the
review session is the first OFFICIAL session (REGULAR / SPECIAL_FULL / SPECIAL_SHORT) after the
packet's session and the packet expires at that session's official close. Both sides' review
sessions AND expiry instants are compared for every day.
"""
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DMB = Path(os.environ.get("DMB_REPO") or ROOT.parent / "daily_byte")
DMB_PY = DMB / ("venv/Scripts/python.exe" if sys.platform == "win32" else "venv/bin/python")
needs_dmb = pytest.mark.skipif(not (DMB_PY.exists() and (DMB / "core" / "trading_calendar.py").exists()),
                               reason="the Daily Market Byte checkout or its venv is not present")

# No known difference remains (docs/NSE_CALENDAR_MONITORING.md, "Calendar parity"). A new entry
# here must be a deliberate, documented decision.
KNOWN_SPECIAL_SESSION_DIFFERENCES = {"kite_only": [], "dmb_only": []}
KNOWN_SPECIAL_SESSION_PACKET_DATES: list = []
SPECIAL_SESSION_DATES = ["2025-02-01", "2025-10-21", "2026-02-01", "2026-11-08"]

# Creation instants probed for every day: at the session's close, that evening, and before,
# during and after the following sessions (IST). Shared verbatim by both interpreters.
_CREATED_TIMES = """
def created_times(p):
    d = dt.date.fromisoformat(p)
    out = [dt.datetime.combine(d, dt.time(15, 30)), dt.datetime.combine(d, dt.time(20, 0))]
    for days in (1, 2, 3, 4):
        for hhmm in ((10, 0), (15, 0), (15, 30), (16, 0)):
            out.append(dt.datetime.combine(d + dt.timedelta(days=days), dt.time(*hhmm)))
    return [t.replace(tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))).isoformat() for t in out]
"""

_DUMP = """
import datetime as dt, json, sys
""" + _CREATED_TIMES + """
from core.trading_calendar import NSE_TRADING_HOLIDAYS, SPECIAL_SESSIONS, SESSION_TYPES, session_record
from private_desk.handoff.evidence import review_session_close, review_session_for
probes = json.loads(sys.argv[1])
out = {"years": sorted(NSE_TRADING_HOLIDAYS),
       "weekday_holidays": sorted(d.isoformat() for y in NSE_TRADING_HOLIDAYS for d in NSE_TRADING_HOLIDAYS[y]
                                  if d.weekday() < 5),
       "special": sorted(d.isoformat() for d in SPECIAL_SESSIONS),
       "session_types": list(SESSION_TYPES),
       "records": {p: session_record(dt.date.fromisoformat(p)) for p in probes},
       "review": {p: (lambda r: r.isoformat() if r else None)(review_session_for(dt.date.fromisoformat(p)))
                  for p in probes},
       "close": {p: (lambda c: c.isoformat() if c else None)(review_session_close(dt.date.fromisoformat(p)))
                 for p in probes},
       "review_at": {f"{p}@{c}": (lambda r: r.isoformat() if r else None)(
                         review_session_for(dt.date.fromisoformat(p), dt.datetime.fromisoformat(c)))
                     for p in probes for c in created_times(p)}}
print(json.dumps(out))
"""


def _probes() -> list[str]:
    d, out = date(2025, 1, 1), []
    while d <= date(2027, 1, 31):
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


@pytest.fixture(scope="module")
def dmb():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KITE_", "DMB_RUN"))}
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([str(DMB_PY), "-c", _DUMP, json.dumps(_probes())], cwd=DMB, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_dmb
def test_weekday_holidays_agree_for_every_year_both_cover(dmb):
    from backend import trading_calendar
    cal = trading_calendar.active_calendar()
    both = sorted(set(dmb["years"]) & set(cal.covered_years))
    assert both == [2025, 2026]
    kite = sorted(d.isoformat() for d in cal.holidays if d.year in both)
    assert kite == [d for d in dmb["weekday_holidays"] if int(d[:4]) in both]


@needs_dmb
def test_every_review_session_agrees(dmb):
    """For every day of 2025-2026: DMB's review session == this terminal's first official session
    after it, or both refuse (not a regular session / next session unknowable) - except the
    documented special-session dates, where only this terminal refuses."""
    from backend import trading_calendar
    mismatches = []
    for probe, theirs in dmb["review"].items():
        d = date.fromisoformat(probe)
        if d.year > 2026:
            continue                                      # 2027: test_both_fail_closed_for_2027
        if not trading_calendar.is_trading_day(d):
            ours = None
        else:
            try:
                ours = trading_calendar.next_official_session(d).isoformat()
            except trading_calendar.CalendarDataError:
                ours = None
        if ours != theirs:
            mismatches.append((probe, ours, theirs))
    assert [m[0] for m in mismatches] == KNOWN_SPECIAL_SESSION_PACKET_DATES, mismatches


@needs_dmb
def test_every_explicit_review_session_agrees(dmb):
    """The review session a packet names depends on when it was built (2026-11-04): for every day of
    2025-2026 and 18 creation instants each - at the close, that evening, and before, during and
    after each of the next four days' sessions - DMB's review_session_for(d, created) equals this
    terminal's, or both refuse (not a regular session, built before its close, calendar unknowable)."""
    import datetime as dt
    from backend import trading_calendar
    namespace = {"dt": dt}
    exec(_CREATED_TIMES, namespace)
    differing, checked = [], 0
    for probe in dmb["review"]:
        d = date.fromisoformat(probe)
        if d.year > 2026:
            continue
        for created in namespace["created_times"](probe):
            at = datetime.fromisoformat(created)
            close = datetime.combine(d, trading_calendar.MARKET_CLOSE, tzinfo=trading_calendar.IST)
            if not trading_calendar.is_trading_day(d) or at < close:
                ours = None
            else:
                try:
                    ours = trading_calendar.review_session_for(d, at).isoformat()
                except trading_calendar.CalendarDataError:
                    ours = None
            theirs = dmb["review_at"][f"{probe}@{created}"]
            checked += ours is not None
            if ours != theirs:
                differing.append((probe, created, ours, theirs))
    assert differing == [] and checked > 480 * 17


@needs_dmb
def test_every_expiry_instant_agrees(dmb):
    """For every day of 2025-2026 that is an official session on both sides, DMB's packet expiry
    (review_session_close) == this terminal's (contract.expires_at): same instant, same offset."""
    from backend import dmb_inbox_contract as contract
    differing, checked = [], 0
    for probe, theirs in dmb["close"].items():
        d = date.fromisoformat(probe)
        if d.year > 2026:
            continue
        try:
            ours = contract.expires_at(d).isoformat()
        except contract.BundleRejected:
            ours = None
        checked += ours is not None
        if ours != theirs:
            differing.append((probe, ours, theirs))
    assert differing == [] and checked > 480


@needs_dmb
def test_every_session_record_agrees(dmb):
    """Type, hours, official source, publication date and verification, for every day of
    2025-01-01..2027-01-31 (2027: UNVERIFIED on both sides)."""
    from backend import trading_calendar
    assert dmb["session_types"] == list(trading_calendar.SESSION_TYPES)
    differing = [(p, theirs, trading_calendar.session_record(date.fromisoformat(p)))
                 for p, theirs in dmb["records"].items()
                 if theirs != trading_calendar.session_record(date.fromisoformat(p))]
    assert differing == []


@needs_dmb
def test_special_sessions_are_typed_sourced_and_never_packet_sessions(dmb):
    """Both sides: the four 2025-2026 special sessions are SPECIAL_FULL / SPECIAL_SHORT with their
    official hours, DMB exports no packet for them, and this terminal refuses one."""
    from backend import dmb_inbox_contract as contract, trading_calendar
    expected = {"2025-02-01": ("SPECIAL_FULL", "09:15", "15:30"), "2025-10-21": ("SPECIAL_SHORT", "13:45", "14:45"),
                "2026-02-01": ("SPECIAL_FULL", "09:15", "15:30"), "2026-11-08": ("SPECIAL_SHORT", "14:20", "15:30")}
    for d, (session_type, opens, closes) in expected.items():
        record = dmb["records"][d]
        assert (record["session_type"], record["open"], record["close"]) == (session_type, opens, closes), d
        assert record["verification"] in ("VERIFIED", "REFERENCED") and record["source"] and record["published"], d
        assert dmb["review"][d] is None, d                                   # DMB: not a packet session
        assert not trading_calendar.is_trading_day(date.fromisoformat(d))
        assert trading_calendar.has_exchange_session(date.fromisoformat(d))
        with pytest.raises(contract.BundleRejected) as e:                    # Kite: refused
            nxt = trading_calendar.next_trading_day(date.fromisoformat(d))
            at = datetime.combine(nxt, datetime.min.time(), trading_calendar.IST)
            contract.check_sessions(date.fromisoformat(d), nxt, at, at)
        assert e.value.code == "INVALID_SESSION_DATE", d


@needs_dmb
def test_the_2026_budget_sunday_is_an_official_full_session_on_both_sides(dmb):
    """NSE/CMTR/72349 (16-Jan-2026): live trading on Sunday 01-Feb-2026 at standard timings."""
    from backend import trading_calendar
    ours = trading_calendar.session_record(date(2026, 2, 1))
    assert dmb["records"]["2026-02-01"] == ours
    assert ours["session_type"] == "SPECIAL_FULL" and (ours["open"], ours["close"]) == ("09:15", "15:30")
    assert ours["source"].startswith("NSE/CMTR/72349") and "NSE/FAOP/72352" in ours["source"]
    assert ours["published"] == "2026-01-16" and ours["verification"] == "VERIFIED"
    # Owner rule (2026-11-04): the packet of Fri 2026-01-30 is reviewed on the Budget Sunday on both
    # sides and expires at its 15:30 close, never extended to Mon 2026-02-02.
    assert dmb["review"]["2026-01-30"] == "2026-02-01" == trading_calendar.next_official_session(date(2026, 1, 30)).isoformat()
    assert dmb["close"]["2026-02-01"] == "2026-02-01T15:30:00+05:30"


@needs_dmb
def test_special_sessions_differ_only_as_documented(dmb):
    from backend import trading_calendar
    kite = {d.isoformat() for d in trading_calendar.active_calendar().special_sessions if d.year in (2025, 2026)}
    theirs = {d for d in dmb["special"] if int(d[:4]) in (2025, 2026)}
    assert sorted(kite) == sorted(theirs) == SPECIAL_SESSION_DATES
    assert sorted(kite - theirs) == KNOWN_SPECIAL_SESSION_DIFFERENCES["kite_only"]
    assert sorted(theirs - kite) == KNOWN_SPECIAL_SESSION_DIFFERENCES["dmb_only"]


@needs_dmb
def test_both_fail_closed_for_2027(dmb):
    from backend import dmb_inbox_contract as contract, trading_calendar
    assert 2027 not in dmb["years"] and trading_calendar.coverage(2027) != "COVERED"
    assert dmb["review"]["2026-12-31"] is None                    # DMB: the next session is unknowable
    assert dmb["records"]["2027-01-04"]["session_type"] == "UNVERIFIED"
    assert trading_calendar.session_record(date(2027, 1, 4))["session_type"] == "UNVERIFIED"
    with pytest.raises(contract.BundleRejected) as e:               # this terminal: refused, not guessed
        contract.check_sessions(date(2026, 12, 31), date(2027, 1, 1),
                                datetime(2026, 12, 31, 20, 0, tzinfo=trading_calendar.IST),
                                datetime(2027, 1, 1, 8, 0, tzinfo=trading_calendar.IST))
    assert e.value.code == "CALENDAR_UNCOVERED"


def test_muhurat_ends_the_previous_packets_window():
    """2026-11-08 Sunday Muhurat trading: an exchange session, not a regular one, so never a packet
    session; but it IS an official session, so Friday 2026-11-06's packet is reviewed on it and
    expires at its 15:30 IST close - never extended to Monday 2026-11-09 (owner rule, 2026-11-04)."""
    from backend import dmb_inbox_contract as contract, trading_calendar
    muhurat = date(2026, 11, 8)
    assert trading_calendar.has_exchange_session(muhurat) and not trading_calendar.is_trading_day(muhurat)
    assert trading_calendar.next_official_session(date(2026, 11, 6)) == muhurat
    friday_evening = datetime(2026, 11, 6, 20, 0, tzinfo=trading_calendar.IST)
    contract.check_sessions(date(2026, 11, 6), muhurat, friday_evening,
                            datetime(2026, 11, 7, 9, 0, tzinfo=trading_calendar.IST))
    with pytest.raises(contract.BundleRejected) as e:
        contract.check_sessions(date(2026, 11, 6), date(2026, 11, 9), friday_evening,
                                datetime(2026, 11, 7, 9, 0, tzinfo=trading_calendar.IST))
    assert e.value.code == "REVIEW_SESSION_MISMATCH"
    assert contract.expires_at(muhurat) == datetime(2026, 11, 8, 15, 30, tzinfo=trading_calendar.IST)
    # Monday 9 Nov's packet (Tue 10 Nov is a holiday) reviews on Wednesday 11 Nov.
    assert trading_calendar.next_official_session(date(2026, 11, 9)) == date(2026, 11, 11)


@pytest.mark.parametrize("session,review", [
    ("2026-11-09", "2026-11-11"),     # Tue 2026-11-10 Diwali Balipratipada holiday
    ("2026-12-24", "2026-12-28"),     # Fri 2026-12-25 Christmas, then the weekend
    ("2026-10-19", "2026-10-21"),     # Tue 2026-10-20 Dussehra
    ("2026-11-02", "2026-11-03"),     # an ordinary weekday
])
def test_holiday_and_weekend_boundaries(session, review):
    from backend import trading_calendar
    assert trading_calendar.next_official_session(date.fromisoformat(session)) == date.fromisoformat(review)
    assert trading_calendar.next_trading_day(date.fromisoformat(session)) == date.fromisoformat(review)
