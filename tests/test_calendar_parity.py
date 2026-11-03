"""NSE calendar parity between Daily Market Byte and this terminal (docs/NSE_CALENDAR_MONITORING.md).

The two repositories keep independent copies of NSE's holiday circulars. A DMB packet's
`review_session_date` is computed by DMB and re-checked here, so the regular-session calendars
must agree for every year both cover, and both must fail closed for a year neither covers
(2027 until NSE publishes its circular). DMB runs in its own interpreter as a separate process,
read-only; skipped when the checkout or its venv is absent.
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

# Sessions held outside the regular calendar that only one side lists. Neither is ever a review
# session (both sides walk REGULAR sessions), so packets are unaffected; reported in
# docs/NSE_CALENDAR_MONITORING.md for DMB's owner, not changed by this sprint.
KNOWN_SPECIAL_SESSION_DIFFERENCES = {"kite_only": ["2026-02-01"], "dmb_only": []}
# DMB special sessions for which DMB would compute a review session and this terminal refuses.
KNOWN_SPECIAL_SESSION_PACKET_DATES = ["2025-02-01", "2025-10-21", "2026-11-08"]

_DUMP = """
import datetime as dt, json, sys
from core.trading_calendar import NSE_TRADING_HOLIDAYS, SPECIAL_SESSIONS
from private_desk.handoff.evidence import review_session_for
probes = json.loads(sys.argv[1])
out = {"years": sorted(NSE_TRADING_HOLIDAYS),
       "weekday_holidays": sorted(d.isoformat() for y in NSE_TRADING_HOLIDAYS for d in NSE_TRADING_HOLIDAYS[y]
                                  if d.weekday() < 5),
       "special": sorted(d.isoformat() for d in SPECIAL_SESSIONS),
       "review": {p: (lambda r: r.isoformat() if r else None)(review_session_for(dt.date.fromisoformat(p)))
                  for p in probes}}
print(json.dumps(out))
"""


def _probes() -> list[str]:
    d, out = date(2025, 1, 1), []
    while d <= date(2026, 12, 31):
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
    """For every day of 2025-2026: DMB's review session == this terminal's next regular session,
    or both refuse (not a session / next session in an uncovered year) - except the documented
    special-session dates, where only this terminal refuses."""
    from backend import trading_calendar
    mismatches = []
    for probe, theirs in dmb["review"].items():
        d = date.fromisoformat(probe)
        if not trading_calendar.is_trading_day(d):
            ours = None
        else:
            try:
                ours = trading_calendar.next_trading_day(d).isoformat()
                if trading_calendar.coverage(int(ours[:4])) != "COVERED":
                    ours = None
            except trading_calendar.CalendarDataError:
                ours = None
        if ours != theirs:
            mismatches.append((probe, ours, theirs))
    # Known, fail-closed difference: DMB treats a special session (Budget Saturday, Muhurat) as a
    # session a packet may describe; this terminal refuses such a packet (INVALID_SESSION_DATE),
    # because the agreed rule counts regular sessions only. Never the other way round.
    assert [m[0] for m in mismatches] == KNOWN_SPECIAL_SESSION_PACKET_DATES
    assert all(ours is None for _, ours, _ in mismatches)
    from backend import dmb_inbox_contract as contract
    for probe, _, theirs in mismatches:
        with pytest.raises(contract.BundleRejected) as e:
            contract.check_sessions(date.fromisoformat(probe), date.fromisoformat(theirs),
                                    datetime.fromisoformat(theirs + "T08:00:00+05:30"))
        assert e.value.code == "INVALID_SESSION_DATE"


@needs_dmb
def test_special_sessions_differ_only_as_documented(dmb):
    from backend import trading_calendar
    kite = {d.isoformat() for d in trading_calendar.active_calendar().special_sessions if d.year in (2025, 2026)}
    theirs = {d for d in dmb["special"] if int(d[:4]) in (2025, 2026)}
    assert sorted(kite - theirs) == KNOWN_SPECIAL_SESSION_DIFFERENCES["kite_only"]
    assert sorted(theirs - kite) == KNOWN_SPECIAL_SESSION_DIFFERENCES["dmb_only"]


@needs_dmb
def test_both_fail_closed_for_2027(dmb):
    from backend import dmb_inbox_contract as contract, trading_calendar
    assert 2027 not in dmb["years"] and trading_calendar.coverage(2027) != "COVERED"
    assert dmb["review"]["2026-12-31"] is None                    # DMB: the next session is unknowable
    with pytest.raises(contract.BundleRejected) as e:               # this terminal: refused, not guessed
        contract.check_sessions(date(2026, 12, 31), date(2027, 1, 1),
                                datetime(2027, 1, 1, 8, 0, tzinfo=trading_calendar.IST))
    assert e.value.code == "CALENDAR_UNCOVERED"


def test_muhurat_is_a_session_but_never_a_review_session():
    """2026-11-08 Sunday Muhurat trading: an exchange session, not a regular one; the packet of
    Friday 2026-11-06 is reviewed on Monday 2026-11-09 and expires 15:30 IST that day."""
    from backend import dmb_inbox_contract as contract, trading_calendar
    muhurat = date(2026, 11, 8)
    assert trading_calendar.has_exchange_session(muhurat) and not trading_calendar.is_trading_day(muhurat)
    assert trading_calendar.next_trading_day(date(2026, 11, 6)) == date(2026, 11, 9)
    contract.check_sessions(date(2026, 11, 6), date(2026, 11, 9), datetime(2026, 11, 7, 9, 0, tzinfo=trading_calendar.IST))
    with pytest.raises(contract.BundleRejected) as e:
        contract.check_sessions(date(2026, 11, 6), muhurat, datetime(2026, 11, 7, 9, 0, tzinfo=trading_calendar.IST))
    assert e.value.code == "REVIEW_SESSION_MISMATCH"
    assert contract.expires_at(date(2026, 11, 9)) == datetime(2026, 11, 9, 15, 30, tzinfo=trading_calendar.IST)


@pytest.mark.parametrize("session,review", [
    ("2026-11-09", "2026-11-11"),     # Tue 2026-11-10 Diwali Balipratipada holiday
    ("2026-12-24", "2026-12-28"),     # Fri 2026-12-25 Christmas, then the weekend
    ("2026-10-19", "2026-10-21"),     # Tue 2026-10-20 Dussehra
    ("2026-11-02", "2026-11-03"),     # an ordinary weekday
])
def test_holiday_and_weekend_boundaries(session, review):
    from backend import trading_calendar
    assert trading_calendar.next_trading_day(date.fromisoformat(session)) == date.fromisoformat(review)
