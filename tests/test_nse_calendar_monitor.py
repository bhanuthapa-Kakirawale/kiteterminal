"""The read-only NSE calendar monitor (backend/nse_calendar_monitor.py), with mocked responses only:
no test contacts NSE. It never writes the calendar and never claims a year it has not verified."""
import hashlib
import json
from pathlib import Path

import pytest

from backend import nse_calendar_monitor as mon

DATA = Path(__file__).resolve().parent.parent / "backend" / "calendar_data" / "nse_equity_holidays.json"


def _master(*dates):
    return json.dumps({"CM": [{"tradingDate": d, "weekDay": "x", "description": f"Holiday {i}"}
                              for i, d in enumerate(dates)], "FO": []}).encode()


def test_a_covered_year_is_integrated_without_any_network():
    def never(url):
        raise AssertionError("no network for an integrated year")
    r = mon.check(2026, fetch=never)
    assert r["status"] == "INTEGRATED" and "CMTR/71775" in r["integrated_source"]
    assert mon.EXIT[r["status"]] == 0


def test_2027_not_published_stays_fail_closed():
    raw = _master("26-Jan-2026", "25-Dec-2026")
    r = mon.check(2027, fetch=lambda url: raw)
    assert r["status"] == "NOT_PUBLISHED" and r["official_dates"] == []
    assert r["official_url"] == mon.OFFICIAL_URL and r["response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert "CALENDAR_UNCOVERED" in r["detail"] and mon.EXIT[r["status"]] == 10


def test_published_dates_are_reported_but_never_integrated():
    before = DATA.read_bytes()
    r = mon.check(2027, fetch=lambda url: _master("26-Jan-2027", "15-Aug-2027", "25-Dec-2026"))
    assert r["status"] == "PUBLISHED_NOT_INTEGRATED"
    assert [d["date"] for d in r["official_dates"]] == ["2027-01-26", "2027-08-15"]
    assert "NOT integrated" in r["detail"] and mon.EXIT[r["status"]] == 11
    assert DATA.read_bytes() == before
    from backend import trading_calendar
    assert trading_calendar.coverage(2027) != "COVERED"


@pytest.mark.parametrize("raw", [b"<html>Access Denied</html>", b'{"FO": []}', b'{"CM": [{"tradingDate": "bad"}]}'])
def test_an_unexpected_answer_is_invalid_never_not_published(raw):
    assert mon.check(2027, fetch=lambda url: raw)["status"] == "INVALID_RESPONSE"


def test_an_unreachable_source_is_reported_as_such():
    def down(url):
        raise ConnectionError("timed out")
    r = mon.check(2027, fetch=down)
    assert r["status"] == "UNREACHABLE" and "timed out" in r["detail"] and mon.EXIT[r["status"]] == 20
    assert mon.check(2027, offline=True)["status"] == "UNREACHABLE"


def test_only_official_nse_https_urls_are_fetched():
    for url in ("http://www.nseindia.com/api/holiday-master", "https://evil.example/api/holiday-master",
                "https://nseindia.com.evil.example/x"):
        with pytest.raises(ValueError):
            mon._fetch(url)


def test_the_cli_prints_and_exits_with_the_status(capsys, monkeypatch):
    monkeypatch.setattr(mon, "_fetch", lambda url, timeout=20.0: _master("26-Jan-2026"))
    assert mon.main(["check", "--year", "2027"]) == 10
    assert "NOT_PUBLISHED" in capsys.readouterr().out
    assert mon.main(["check", "--year", "2026", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "INTEGRATED"


def test_the_monitor_never_writes_or_schedules_anything():
    src = Path(mon.__file__).read_text(encoding="utf-8")
    for banned in ("write_text", "write_bytes", "open(", "schtasks", "subprocess", "KITE_REST_PROXY", "kiteconnect"):
        assert banned not in src, banned
