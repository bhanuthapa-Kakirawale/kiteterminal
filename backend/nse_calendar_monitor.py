"""Read-only monitor: has NSE published the official trading-holiday calendar for a year?

    python -m backend.nse_calendar_monitor check --year 2027 [--json]

It never changes anything. It reads this terminal's calendar file (is the year integrated?) and,
unless `--offline`, makes one HTTPS GET to NSE's own holiday master
(https://www.nseindia.com/api/holiday-master?type=trading) - never any other host, never via the
Kite REST proxy, never a broker call. It does not write the calendar, does not schedule itself
and does not decide anything: integrating a year is an operator step from NSE's official
circular (docs/NSE_CALENDAR_MONITORING.md). Until then both applications stay fail-closed for the
year (CALENDAR_UNCOVERED).

Statuses (exit code):
  INTEGRATED                0   the year is in backend/calendar_data/nse_equity_holidays.json
  NOT_PUBLISHED            10   NSE answered and lists no Capital Market holiday in that year
  PUBLISHED_NOT_INTEGRATED 11   NSE lists dates for the year: obtain and verify the circular,
                                then integrate (the dates printed are NOT integrated by this tool)
  UNREACHABLE              20   NSE could not be reached or refused the request
  INVALID_RESPONSE         21   NSE answered with something that is not its holiday master
Every network result reports the URL, the retrieval time and the response's SHA-256.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

from backend import trading_calendar

OFFICIAL_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
OFFICIAL_HOSTS = frozenset({"www.nseindia.com", "nsearchives.nseindia.com"})
SEGMENT = "CM"
EXIT = {"INTEGRATED": 0, "NOT_PUBLISHED": 10, "PUBLISHED_NOT_INTEGRATED": 11, "UNREACHABLE": 20,
        "INVALID_RESPONSE": 21}
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) kite-terminal-calendar-monitor",
            "Accept": "application/json", "Referer": "https://www.nseindia.com/resources/exchange-communication-holidays"}


def _fetch(url: str, timeout: float = 20.0) -> bytes:
    import requests
    if urlsplit(url).scheme != "https" or urlsplit(url).hostname not in OFFICIAL_HOSTS:
        raise ValueError(f"not an official NSE https URL: {url}")
    with requests.Session() as s:
        s.trust_env = False                         # no system proxy, no Kite REST proxy
        s.get("https://www.nseindia.com/", headers=_HEADERS, timeout=timeout)    # NSE sets its cookies here
        r = s.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=False)
        r.raise_for_status()
        return r.content


def _parse_dates(raw: bytes, year: int) -> list[dict]:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get(SEGMENT), list):
        raise ValueError(f"no {SEGMENT} list in the response")
    out = []
    for entry in data[SEGMENT]:
        d = datetime.strptime(str(entry["tradingDate"]), "%d-%b-%Y").date()
        if d.year == year:
            out.append({"date": d.isoformat(), "description": str(entry.get("description", ""))[:120]})
    return sorted(out, key=lambda e: e["date"])


def check(year: int, *, offline: bool = False, fetch=_fetch, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cal = trading_calendar.load_calendar()
    result = {"year": year, "checked_at": now.isoformat(timespec="seconds"),
              "integrated": year in cal.covered_years, "integrated_source": cal.sources.get(year),
              "official_url": None, "retrieved_at": None, "response_sha256": None, "official_dates": None,
              "detail": None}
    if result["integrated"]:
        return {**result, "status": "INTEGRATED",
                "detail": f"{year} is integrated from {cal.sources.get(year)}."}
    if offline:
        return {**result, "status": "UNREACHABLE", "detail": "offline: NSE was not contacted."}
    result["official_url"] = OFFICIAL_URL
    try:
        raw = fetch(OFFICIAL_URL)
    except Exception as e:                           # network, HTTP status, refused URL
        return {**result, "status": "UNREACHABLE", "detail": f"{type(e).__name__}: {str(e)[:200]}"}
    result.update(retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  response_sha256=hashlib.sha256(raw).hexdigest())
    try:
        dates = _parse_dates(raw, year)
    except Exception as e:
        return {**result, "status": "INVALID_RESPONSE", "detail": f"{type(e).__name__}: {str(e)[:200]}"}
    if not dates:
        return {**result, "status": "NOT_PUBLISHED", "official_dates": [],
                "detail": f"NSE's holiday master lists no {SEGMENT} holiday in {year}. Both applications stay "
                          f"fail-closed for {year} (CALENDAR_UNCOVERED)."}
    return {**result, "status": "PUBLISHED_NOT_INTEGRATED", "official_dates": dates,
            "detail": f"NSE lists {len(dates)} {SEGMENT} holiday(s) for {year}. NOT integrated: obtain NSE's "
                      f"'Trading holidays for the calendar year {year}' circular, verify these dates against it, "
                      f"then integrate in both applications (docs/NSE_CALENDAR_MONITORING.md)."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.nse_calendar_monitor")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="is the official NSE calendar for a year published / integrated?")
    c.add_argument("--year", type=int, required=True)
    c.add_argument("--offline", action="store_true", help="only report whether the year is integrated")
    c.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    r = check(args.year, offline=args.offline)
    if args.json:
        print(json.dumps(r, indent=2, sort_keys=True))
    else:
        print(f"NSE {r['year']} calendar: {r['status']}")
        for key in ("integrated_source", "official_url", "retrieved_at", "response_sha256"):
            if r.get(key):
                print(f"  {key:18} {r[key]}")
        for d in r.get("official_dates") or []:
            print(f"  official (unverified) {d['date']}  {d['description']}")
        print(f"  {r['detail']}")
    return EXIT[r["status"]]


if __name__ == "__main__":
    sys.exit(main())
