"""NSE trading calendar: the one authoritative answer to "does the exchange
trade on this day, and is it trading now?" (PK-22C, audit P2-6).

Before PK-22 the terminal treated every weekday as a trading day. On an NSE
holiday that made approvals AMOs for a session that was not coming, pointed
the "last completed session" at a day with no candle, and read signal age one
day too old. The holidays now come from a local data file,
calendar_data/nse_equity_holidays.json, transcribed from NSE's yearly
"Trading holidays for the calendar year" circulars and their amendments. It
is never fetched at runtime; see the file's _about note for the yearly update.

Terms used here:
  * trading day      — a REGULAR session day: a weekday that is not a listed
                       holiday. Sessions, AMO targets, stale-signal checks and
                       the autopilot schedule use this.
  * special session  — an exchange session outside that calendar (Muhurat
                       trading, a budget-day weekend session, a DR drill). It
                       produces a daily candle, but its hours are not modelled:
                       the market is never reported open during one, and it
                       is never an AMO target (see Known Limitations, PK-22).
  * covered year     — a year present in the data file. For an uncovered year
                       every weekday is assumed to trade (the pre-PK-22
                       behaviour) and calendar_status() reports it, so
                       diagnostics can warn that the file needs its update.

backend/market.py keeps its historical function names as thin delegates, so
existing callers (and tests that pin market.is_market_open) are unchanged.
"""

import argparse
import json
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
PRE_OPEN = time(9, 0)
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
DATA_FILE = Path(__file__).parent / "calendar_data" / "nse_equity_holidays.json"

_SQL_TIME = "%Y-%m-%d %H:%M:%S"
_MAX_WALK_DAYS = 31  # no real gap between NSE sessions comes close

# ---- session records (DMB/Kite parity) ----------------------------------------
# Typed, sourced session semantics, shared string for string with Daily Market Byte's
# core/trading_calendar.py and compared day by day by tests/test_calendar_parity.py. A Sunday Budget
# session is a full session and Muhurat a short one; neither is a REGULAR session, and neither is
# ever a review session, an AMO target or "market open" here (hours of special sessions are
# recorded, not modelled - see Known Limitations, PK-22).
SEGMENT = "NSE_CM"
REGULAR = "REGULAR"                 # a weekday that is not a listed holiday, 09:15-15:30 IST
SPECIAL_FULL = "SPECIAL_FULL"       # a live session at standard timings on a weekend/holiday (Budget day)
SPECIAL_SHORT = "SPECIAL_SHORT"     # a short special session (Diwali Muhurat, a DR drill)
HOLIDAY = "HOLIDAY"                 # no session: a listed trading holiday or a weekend
UNVERIFIED = "UNVERIFIED"           # an uncovered year, or a special session without official evidence
SESSION_TYPES = (REGULAR, SPECIAL_FULL, SPECIAL_SHORT, HOLIDAY, UNVERIFIED)
VERIFIED = "VERIFIED"               # the official NSE circular itself was retrieved and read
REFERENCED = "REFERENCED"           # recorded from an official NSE reference, not itself retrieved
_EVIDENCED = (VERIFIED, REFERENCED)


@dataclass(frozen=True)
class Calendar:
    holidays: dict = field(default_factory=dict)          # date -> name
    special_sessions: dict = field(default_factory=dict)  # date -> {"kind", "note", "session_type", "open", ...}
    covered_years: frozenset = frozenset()
    sources: dict = field(default_factory=dict)            # year -> circular reference
    verified_through: str | None = None
    year_evidence: dict = field(default_factory=dict)      # year -> {"source", "published", "verification"}
    holiday_evidence: dict = field(default_factory=dict)   # date -> the same, for an amendment holiday


class CalendarDataError(ValueError):
    pass


def load_calendar(path: Path = DATA_FILE) -> Calendar:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    holidays: dict[date, str] = {}
    sources: dict[int, str] = {}
    year_evidence: dict[int, dict] = {}
    holiday_evidence: dict[date, dict] = {}
    for year_text, year in data["years"].items():
        year_no = int(year_text)
        sources[year_no] = year.get("source", "")
        year_evidence[year_no] = _evidence(year, f"year {year_no}")
        for entry in year["holidays"]:
            d = date.fromisoformat(entry["date"])
            if d.year != year_no:
                raise CalendarDataError(f"{d} is listed under {year_no}")
            if d.weekday() >= 5:
                raise CalendarDataError(f"{d} is a weekend: only weekday holidays are listed")
            if d in holidays:
                raise CalendarDataError(f"{d} is listed twice")
            holidays[d] = entry["name"]
            if "source" in entry:
                holiday_evidence[d] = _evidence(entry, f"holiday {d}")
    specials: dict[date, dict] = {}
    for entry in data.get("special_sessions", []):
        d = date.fromisoformat(entry["date"])
        if d in specials:
            raise CalendarDataError(f"special session {d} is listed twice")
        session_type = entry.get("session_type")
        if session_type not in (SPECIAL_FULL, SPECIAL_SHORT):
            raise CalendarDataError(f"special session {d}: session_type must be {SPECIAL_FULL} or {SPECIAL_SHORT}")
        evidence = _evidence(entry, f"special session {d}")
        if evidence["verification"] in _EVIDENCED and not (entry.get("open") and entry.get("close")
                                                            and evidence["source"] and evidence["published"]):
            raise CalendarDataError(f"special session {d}: an evidenced session needs open, close, source, published")
        specials[d] = {"kind": entry["kind"], "note": entry.get("note", ""), "session_type": session_type,
                       "open": entry.get("open"), "close": entry.get("close"), **evidence}
    return Calendar(holidays=holidays, special_sessions=specials, covered_years=frozenset(sources),
                    sources=sources, verified_through=data.get("verified_against_ohlc_through"),
                    year_evidence=year_evidence, holiday_evidence=holiday_evidence)


def _evidence(entry: dict, where: str) -> dict:
    verification = entry.get("verification", UNVERIFIED)
    if verification not in (*_EVIDENCED, UNVERIFIED):
        raise CalendarDataError(f"{where}: unknown verification {verification!r}")
    return {"source": entry.get("source"), "published": entry.get("published"), "verification": verification}


_active: Calendar = load_calendar()


@contextmanager
def use_calendar(holidays=None, special_sessions=None, covered_years=None):
    """Swap in an explicit calendar (tests). `holidays` maps date -> name (or
    is an iterable of dates); unspecified parts are empty."""
    global _active
    previous = _active
    if holidays is not None and not isinstance(holidays, dict):
        holidays = {d: "Test holiday" for d in holidays}
    holidays = dict(holidays or {})
    years = frozenset(covered_years) if covered_years is not None else frozenset(d.year for d in holidays)
    _active = Calendar(
        holidays=holidays,
        special_sessions={d: {"kind": "special_live", "note": "test", "session_type": SPECIAL_FULL,
                              "verification": UNVERIFIED} for d in (special_sessions or ())},
        covered_years=years,
        sources={y: "test fixture" for y in years},
    )
    try:
        yield _active
    finally:
        _active = previous


def active_calendar() -> Calendar:
    return _active


# ---- clock -------------------------------------------------------------------

def now_ist() -> datetime:
    return datetime.now(IST)


def as_ist(at: datetime | None) -> datetime:
    if at is None:
        return now_ist()
    if at.tzinfo is None:
        return at.replace(tzinfo=IST)
    return at.astimezone(IST)


# ---- days --------------------------------------------------------------------

def is_holiday(d: date) -> bool:
    return d in _active.holidays


def holiday_name(d: date) -> str | None:
    return _active.holidays.get(d)


def special_session(d: date) -> dict | None:
    return _active.special_sessions.get(d)


def session_record(d: date) -> dict:
    """The typed, sourced record of `d` for the NSE CM segment (SESSION_TYPES). Fail closed: an
    uncovered year, or a special session without official evidence, is UNVERIFIED."""
    record = {"date": d.isoformat(), "segment": SEGMENT, "session_type": UNVERIFIED, "open": None, "close": None,
              "source": None, "published": None, "verification": UNVERIFIED}
    if d.year not in _active.covered_years:
        return record
    special = _active.special_sessions.get(d)
    if special is not None:
        if special.get("verification") not in _EVIDENCED:
            return {**record, "source": special.get("source")}
        return {**record, "session_type": special["session_type"], "open": special["open"], "close": special["close"],
                "source": special["source"], "published": special["published"],
                "verification": special["verification"]}
    evidence = (_active.holiday_evidence.get(d) or _active.year_evidence.get(d.year)
                or {"source": _active.sources.get(d.year), "published": None, "verification": UNVERIFIED})
    base = {**record, **evidence}
    if is_trading_day(d):
        return {**base, "session_type": REGULAR, "open": MARKET_OPEN.strftime("%H:%M"),
                "close": MARKET_CLOSE.strftime("%H:%M")}
    return {**base, "session_type": HOLIDAY}


def is_trading_day(d: date) -> bool:
    """A regular NSE session day: a weekday that is not a listed holiday."""
    return d.weekday() < 5 and d not in _active.holidays


def has_exchange_session(d: date) -> bool:
    """Any session that produces a daily candle, special sessions included."""
    return is_trading_day(d) or d in _active.special_sessions


def previous_trading_day(d: date) -> date:
    for _ in range(_MAX_WALK_DAYS):
        d -= timedelta(days=1)
        if is_trading_day(d):
            return d
    raise CalendarDataError(f"no trading day within {_MAX_WALK_DAYS} days before {d}")


def next_trading_day(d: date) -> date:
    for _ in range(_MAX_WALK_DAYS):
        d += timedelta(days=1)
        if is_trading_day(d):
            return d
    raise CalendarDataError(f"no trading day within {_MAX_WALK_DAYS} days after {d}")


# Every officially notified session counts for a DMB packet's window (owner rule, 2026-11-04):
# a special session (Budget weekend, Muhurat) ENDS the previous packet's window. Parity: DMB's
# private_desk.handoff.evidence.review_session_for / review_session_close.
OFFICIAL_SESSION_TYPES = (REGULAR, SPECIAL_FULL, SPECIAL_SHORT)


def next_official_session(d: date) -> date:
    """The first officially notified NSE session after `d` - regular, special full-day or special
    short alike. Fail closed: an UNVERIFIED day on the way (an uncovered year, a special session
    without official evidence) raises CalendarDataError, never a guess."""
    for _ in range(_MAX_WALK_DAYS):
        d += timedelta(days=1)
        kind = session_record(d)["session_type"]
        if kind in OFFICIAL_SESSION_TYPES:
            return d
        if kind != HOLIDAY:
            raise CalendarDataError(f"{d} is {kind}: the NSE calendar cannot establish the next session")
    raise CalendarDataError(f"no official session within {_MAX_WALK_DAYS} days after {d}")


def official_session_close(d: date) -> datetime:
    """The official close (IST) of the session on `d`, from its typed record: 15:30 for a regular
    day, the circular's close for a special one. CalendarDataError when `d` is no evidenced session."""
    record = session_record(d)
    if record["session_type"] not in OFFICIAL_SESSION_TYPES or not record["close"]:
        raise CalendarDataError(f"{d} is not an evidenced NSE session ({record['session_type']})")
    return datetime.combine(d, time.fromisoformat(record["close"]), tzinfo=IST)


def trading_days_between(start: date, end: date) -> int:
    """Trading days d with start <= d < end: numpy.busday_count's convention,
    holiday-aware. For two trading days, the sessions from one to the other."""
    if end <= start:
        return 0
    count, d = 0, start
    while d < end:
        if is_trading_day(d):
            count += 1
        d += timedelta(days=1)
    return count


# ---- sessions ----------------------------------------------------------------

def is_market_session_open(at: datetime | None = None) -> bool:
    at = as_ist(at)
    return is_trading_day(at.date()) and MARKET_OPEN <= at.time() < MARKET_CLOSE


def latest_completed_session(at: datetime | None = None) -> date:
    """The most recent regular session whose daily candle is final."""
    at = as_ist(at)
    d = at.date()
    if is_trading_day(d) and at.time() >= MARKET_CLOSE:
        return d
    return previous_trading_day(d)


def next_session_date(at: datetime | None = None) -> date:
    """The regular session an order placed at `at` outside market hours waits
    for: today's if before the open of a trading day, else the next one."""
    at = as_ist(at)
    d = at.date()
    if is_trading_day(d) and at.time() < MARKET_OPEN:
        return d
    return next_trading_day(d)


def session_state(at: datetime | None = None) -> str:
    """WEEKEND | HOLIDAY | PRE_OPEN | OPEN | CLOSED. A special session's day
    reports its underlying state (WEEKEND/HOLIDAY): its hours are not modelled."""
    at = as_ist(at)
    d, t = at.date(), at.time()
    if d.weekday() >= 5:
        return "WEEKEND"
    if d in _active.holidays:
        return "HOLIDAY"
    if MARKET_OPEN <= t < MARKET_CLOSE:
        return "OPEN"
    if PRE_OPEN <= t < MARKET_OPEN:
        return "PRE_OPEN"
    return "CLOSED"


def ist_day_start_utc_sql(at: datetime | None = None) -> str:
    """Midnight of `at`'s IST date, as the UTC 'YYYY-MM-DD HH:MM:SS' text the
    ticket timestamps use: the boundary of every "today" count."""
    midnight = as_ist(at).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).strftime(_SQL_TIME)


# ---- coverage / status -------------------------------------------------------

def coverage(year: int) -> str:
    return "COVERED" if year in _active.covered_years else "UNCOVERED"


def calendar_status(at: datetime | None = None) -> dict:
    at = as_ist(at)
    today = at.date()
    next_year_needed = at.month == 12  # NSE publishes next year's list in December
    uncovered = [y for y in ([today.year, today.year + 1] if next_year_needed else [today.year])
                 if coverage(y) == "UNCOVERED"]
    return {
        "now_ist": at.isoformat(timespec="seconds"),
        "session_state": session_state(at),
        "is_trading_day": is_trading_day(today),
        "holiday_name": holiday_name(today),
        "special_session": special_session(today),
        "latest_completed_session": latest_completed_session(at).isoformat(),
        "next_trading_day": next_trading_day(today).isoformat(),
        "covered_years": sorted(_active.covered_years),
        "uncovered_years": uncovered,
        "sources": {str(y): s for y, s in sorted(_active.sources.items())},
        "verified_against_ohlc_through": _active.verified_through,
        "special_sessions_modelled": False,
    }


# ---- maintenance CLI -----------------------------------------------------------

def verify_against_ohlc(db_path: Path) -> list[str]:
    """Cross-check the calendar against stored daily candles (read-only).
    A covered weekday with no candles for most symbols should be a holiday;
    a candle on a listed holiday or a weekend should be a special session."""
    # immutable=1: a pure read that never creates -wal/-shm files next to the copy.
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        counts = dict(conn.execute("SELECT date, COUNT(*) FROM ohlc_daily GROUP BY date").fetchall())
    finally:
        conn.close()
    if not counts:
        return ["ohlc_daily is empty: nothing to verify against"]
    median = sorted(counts.values())[len(counts) // 2]
    problems = []
    d, end = date.fromisoformat(min(counts)), date.fromisoformat(max(counts))
    while d <= end:
        if d.year in _active.covered_years:
            n = counts.get(d.isoformat(), 0)
            traded = n >= median * 0.5
            if traded and not has_exchange_session(d):
                problems.append(f"{d} {d:%a}: {n} candles, but the calendar has no session")
            elif not traded and is_trading_day(d):
                problems.append(f"{d} {d:%a}: {n} candles, but the calendar says it is a trading day")
        d += timedelta(days=1)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.trading_calendar")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="validate the holiday file (and optionally cross-check a DB copy)")
    check.add_argument("--verify-db", type=Path, help="a COPY of kite_terminal.db to cross-check candles against")
    args = parser.parse_args(argv)

    calendar = load_calendar()  # raises on a malformed file
    print(f"Holiday file OK: years {sorted(calendar.covered_years)}, {len(calendar.holidays)} weekday holidays, "
          f"{len(calendar.special_sessions)} special sessions.")
    for year in sorted(calendar.covered_years):
        print(f"  {year}: {sum(1 for d in calendar.holidays if d.year == year)} holidays — {calendar.sources[year]}")
    status = calendar_status()
    if status["uncovered_years"]:
        print(f"WARNING: no holiday list for {status['uncovered_years']}: every weekday is assumed to trade.")
    if args.verify_db:
        problems = verify_against_ohlc(args.verify_db)
        if problems:
            print(f"{len(problems)} mismatch(es) against {args.verify_db}:")
            for p in problems:
                print("  " + p)
            return 1
        print(f"Candles in {args.verify_db} agree with the calendar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
