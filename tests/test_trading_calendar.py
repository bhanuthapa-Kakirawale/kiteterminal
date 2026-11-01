"""PK-22C: the NSE trading calendar (backend/trading_calendar.py) and every
place that used to assume "every weekday trades".

All clocks are pinned to explicit IST datetimes; nothing here depends on
when the suite runs. The real holiday file is used where a real date is the
point (2026-09-14, Ganesh Chaturthi, is an official NSE holiday — circular
NSE/CMTR/71775); use_calendar() pins synthetic calendars elsewhere.
"""

import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from backend import trading_calendar as tc
from conftest import approval_body, IST, account_id, set_mode, ticket_row

FRI = date(2026, 9, 11)
HOLIDAY_MON = date(2026, 9, 14)   # Ganesh Chaturthi (NSE/CMTR/71775)
TUE = date(2026, 9, 15)


def ist(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


# ---------------------------------------------------------------- the data file

def test_holiday_file_loads_and_is_well_formed():
    cal = tc.load_calendar()
    assert {2023, 2024, 2025, 2026} <= set(cal.covered_years)
    assert all(d.weekday() < 5 for d in cal.holidays)                       # weekdays only
    assert sum(1 for d in cal.holidays if d.year == 2026) == 16             # 15 in the circular + 15-Jan
    assert all(cal.sources[y] for y in cal.covered_years)                    # every year cites its circular
    assert cal.holidays[HOLIDAY_MON] == "Ganesh Chaturthi"


@pytest.mark.parametrize("bad, message", [
    ({"date": "2026-09-12", "name": "Saturday"}, "weekend"),
    ({"date": "2026-09-14", "name": "Twice"}, "twice"),
    ({"date": "2025-09-15", "name": "Wrong year"}, "listed under"),
])
def test_malformed_holiday_file_is_refused(tmp_path, bad, message):
    data = json.loads(tc.DATA_FILE.read_text(encoding="utf-8"))
    data["years"]["2026"]["holidays"].append(bad)
    path = tmp_path / "cal.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(tc.CalendarDataError, match=message):
        tc.load_calendar(path)


def test_use_calendar_overrides_and_restores():
    assert tc.is_holiday(HOLIDAY_MON)
    with tc.use_calendar(holidays=[TUE]):
        assert not tc.is_holiday(HOLIDAY_MON) and tc.is_holiday(TUE)
    assert tc.is_holiday(HOLIDAY_MON) and not tc.is_holiday(TUE)


# ---------------------------------------------------------------- days

def test_weekday_weekend_and_holiday():
    assert tc.is_trading_day(FRI)
    assert not tc.is_trading_day(date(2026, 9, 12))   # Saturday
    assert not tc.is_trading_day(date(2026, 9, 13))   # Sunday
    assert not tc.is_trading_day(HOLIDAY_MON)          # holiday
    assert tc.is_trading_day(TUE)


def test_friday_to_monday_and_holiday_monday_to_tuesday():
    assert tc.next_trading_day(date(2026, 9, 18)) == date(2026, 9, 21)   # ordinary Friday -> Monday
    assert tc.next_trading_day(FRI) == TUE                                # holiday Monday skipped
    assert tc.previous_trading_day(TUE) == FRI
    with tc.use_calendar(holidays=[]):
        assert tc.next_trading_day(FRI) == HOLIDAY_MON                    # a weekend-only calendar


def test_latest_completed_session_skips_the_holiday():
    assert tc.latest_completed_session(ist(HOLIDAY_MON, 17)) == FRI
    assert tc.latest_completed_session(ist(TUE, 10)) == FRI
    assert tc.latest_completed_session(ist(TUE, 15, 31)) == TUE
    assert tc.latest_completed_session(ist(date(2026, 9, 21), 12)) == date(2026, 9, 18)


def test_market_session_state():
    assert tc.session_state(ist(HOLIDAY_MON, 10)) == "HOLIDAY"
    assert not tc.is_market_session_open(ist(HOLIDAY_MON, 10))
    assert tc.session_state(ist(date(2026, 9, 12), 10)) == "WEEKEND"
    assert tc.session_state(ist(TUE, 9, 5)) == "PRE_OPEN"
    assert tc.session_state(ist(TUE, 9, 15)) == "OPEN" and tc.is_market_session_open(ist(TUE, 9, 15))
    assert tc.session_state(ist(TUE, 15, 30)) == "CLOSED" and not tc.is_market_session_open(ist(TUE, 15, 30))


def test_special_sessions_have_candles_but_are_never_modelled_as_open():
    budget_sunday = date(2026, 2, 1)
    assert tc.special_session(budget_sunday)["kind"] == "special_live"
    assert tc.has_exchange_session(budget_sunday) and not tc.is_trading_day(budget_sunday)
    assert not tc.is_market_session_open(ist(budget_sunday, 11))          # hours not invented
    muhurat_on_holiday = date(2025, 10, 21)
    assert tc.is_holiday(muhurat_on_holiday) and tc.has_exchange_session(muhurat_on_holiday)


def test_trading_days_between_counts_sessions_not_days():
    assert tc.trading_days_between(FRI, TUE) == 1                          # Fri only; Mon is a holiday
    assert tc.trading_days_between(date(2026, 9, 18), date(2026, 9, 21)) == 1
    assert tc.trading_days_between(TUE, FRI) == 0
    with tc.use_calendar(holidays=[]):
        assert tc.trading_days_between(FRI, TUE) == 2


def test_uncovered_year_falls_back_to_weekdays_and_is_reported():
    assert tc.coverage(2026) == "COVERED" and tc.coverage(2031) == "UNCOVERED"
    assert tc.is_trading_day(date(2031, 1, 1))                              # weekday, no list: assumed open
    status = tc.calendar_status(ist(date(2026, 12, 15), 10))
    assert status["uncovered_years"] == [2027]                              # December asks for next year
    assert tc.calendar_status(ist(TUE, 10))["uncovered_years"] == []


def test_ist_day_start_is_the_calendar_date_not_the_session():
    # Daily counts reset at IST midnight even on a holiday (unchanged by PK-22).
    assert tc.ist_day_start_utc_sql(ist(HOLIDAY_MON, 10)) == "2026-09-13 18:30:00"
    assert tc.ist_day_start_utc_sql(ist(TUE, 0, 5)) == "2026-09-14 18:30:00"


# ---------------------------------------------------------------- callers

def test_market_delegates_to_the_calendar():
    from backend import market
    assert not market.is_trading_day(HOLIDAY_MON)
    assert not market.is_market_open(ist(HOLIDAY_MON, 11))
    assert market.last_complete_session_date(ist(HOLIDAY_MON, 18)) == FRI


def _strategy_ticket(signal_date: str) -> dict:
    return {"source": "strategy", "signal_date": signal_date, "created_at": signal_date + " 12:00:00"}


def test_stale_signal_across_a_holiday():
    from backend import account_safety
    # Friday's signal is still the latest completed session all through the
    # holiday Monday and until Tuesday's close.
    assert account_safety.check_stale_signal(_strategy_ticket("2026-09-11"), ist(HOLIDAY_MON, 18)) is None
    assert account_safety.check_stale_signal(_strategy_ticket("2026-09-11"), ist(TUE, 11)) is None
    failure = account_safety.check_stale_signal(_strategy_ticket("2026-09-11"), ist(TUE, 16))
    assert failure is not None and failure.code == account_safety.STALE_SIGNAL
    # Without holidays, the same Friday signal would be stale on Monday evening.
    with tc.use_calendar(holidays=[]):
        assert account_safety.check_stale_signal(_strategy_ticket("2026-09-11"), ist(HOLIDAY_MON, 18)) is not None


def test_daily_entry_count_resets_at_ist_midnight_across_a_holiday():
    from backend import account_safety
    assert account_safety.ist_day_start_utc(ist(HOLIDAY_MON, 21)) == "2026-09-13 18:30:00"
    assert account_safety.ist_day_start_utc(ist(TUE, 9, 20)) == "2026-09-14 18:30:00"


def test_paper_amo_placed_before_a_holiday_waits_for_tuesday(env, monkeypatch):
    from backend import market, order_monitor, paper_broker
    friday_evening = ist(FRI, 18)
    monkeypatch.setattr(paper_broker, "_now", lambda: friday_evening)
    set_mode(env.db, "dry_run")
    env.kite.ltp_map["NSE:INFY"] = 1500.0
    body = {"symbol": "INFY", "exchange": "NSE", "transaction_type": "BUY", "quantity": 10, "entry_price": 1500.0,
            "entry_order_type": "MARKET", "stop_loss": 1450.0, "target_1": 1600.0, "product_type": "CNC",
            "rationale": "PK-22 holiday AMO"}
    tid = env.client.post("/api/tickets", json=body).json()["data"]["id"]
    real_is_market_open = market.is_market_open
    monkeypatch.setattr(market, "is_market_open", lambda at=None: False)
    r = env.client.post(f"/api/tickets/{tid}/confirm", json=approval_body(env.db, tid, expected_mode="dry_run", allow_amo=True))
    assert r.status_code == 200, r.text
    order_id = ticket_row(env.db, tid)["kite_order_id"]
    conn = env.db.get_connection()
    try:
        order = dict(conn.execute("SELECT * FROM paper_orders WHERE paper_order_id = ?", (order_id,)).fetchone())
    finally:
        conn.close()
    assert order["session_date"] == TUE.isoformat()              # not the holiday Monday

    # The real session rules from here; evaluate() is given explicit times.
    monkeypatch.setattr(market, "is_market_open", real_is_market_open)
    paper_broker.evaluate(account_id(env.db), env.kite, now=ist(HOLIDAY_MON, 10))
    conn = env.db.get_connection()
    try:
        status = conn.execute("SELECT status FROM paper_orders WHERE paper_order_id = ?", (order_id,)).fetchone()[0]
        assert status == "AMO REQ RECEIVED"                      # the holiday opens no session
        paper_broker.evaluate(account_id(env.db), env.kite, now=ist(TUE, 9, 16))
        status = conn.execute("SELECT status FROM paper_orders WHERE paper_order_id = ?", (order_id,)).fetchone()[0]
    finally:
        conn.close()
    assert status == "COMPLETE"
    order_monitor.check_pending_orders(mode="dry_run")
    assert ticket_row(env.db, tid)["entry_fill_status"] == "filled"


def test_backtest_next_session_across_a_holiday():
    from backend.backtest import SymbolFrame
    from test_backtest_realism import Stub, run, sig
    # Bars exist only on sessions: Friday, then (Monday is a holiday) Tuesday.
    days = [FRI.isoformat(), TUE.isoformat(), "2026-09-16", "2026-09-17"]
    bars = [(100, 101, 99, 100), (102, 104, 101, 103), (103, 104, 102, 103), (103, 104, 102, 103)]
    df = pd.DataFrame([{"date": d, "open": o, "high": h, "low": l, "close": c, "volume": 100_000, "sym": "AAA"}
                       for d, (o, h, l, c) in zip(days, bars)])
    out = run([SymbolFrame("AAA", df, 0.05)], Stub({("AAA", FRI.isoformat()): sig()}))
    [trade] = out["trades"]
    assert trade["signal_date"] == FRI.isoformat()
    assert trade["entry_date"] == TUE.isoformat() and trade["entry_price"] == 102.0


def test_autopilot_does_not_run_for_a_holiday(env):
    from backend import automation
    conn = env.db.get_connection()
    try:
        conn.execute("INSERT INTO automation_runs (account_id, run_date, trigger, status) "
                     "VALUES (?, '2026-09-11', 'scheduled', 'success')", (account_id(env.db),))
        conn.commit()
    finally:
        conn.close()
    assert not automation.is_due(ist(HOLIDAY_MON, 16, 30))           # no new session to process
    assert automation.next_run_at(ist(HOLIDAY_MON, 16, 30)) == automation.scheduled_at(TUE)


def test_journal_holding_trading_days_skip_the_holiday():
    from backend import trading_calendar
    assert trading_calendar.trading_days_between(date.fromisoformat("2026-09-10"), TUE) == 2  # Thu, Fri


def test_signal_age_is_counted_in_sessions(monkeypatch):
    from backend import market, signal_chart
    monkeypatch.setattr(market, "last_complete_session_date", lambda at=None: TUE)
    freshness = signal_chart._staleness("2026-09-11", "2026-09-15")
    assert freshness["signal_age_sessions"] == 1                     # Tuesday is one session after Friday


# ---------------------------------------------------------------- maintenance CLI

def _ohlc_db(path, dates):
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ohlc_daily (instrument_token INTEGER, date TEXT)")
    conn.executemany("INSERT INTO ohlc_daily VALUES (?, ?)", [(t, d) for d in dates for t in (1, 2, 3)])
    conn.commit()
    conn.close()


def test_cli_verifies_candles_against_the_calendar(tmp_path, capsys):
    good = [d.isoformat() for d in (date(2026, 9, 10), FRI, TUE, date(2026, 9, 16))]
    _ohlc_db(tmp_path / "good.db", good)
    assert tc.main(["check", "--verify-db", str(tmp_path / "good.db")]) == 0
    _ohlc_db(tmp_path / "bad.db", good + [HOLIDAY_MON.isoformat()])       # a candle on the holiday
    assert tc.main(["check", "--verify-db", str(tmp_path / "bad.db")]) == 1
    assert "2026-09-14" in capsys.readouterr().out
