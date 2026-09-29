import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_kite import FakeKite, make_candles  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))

# Every test runs against a throwaway SQLite file and a FakeKite. These
# symbols are seeded into instrument_cache so ticket/watchlist validation
# (which resolves symbols server-side) behaves exactly like production.
SEED_INSTRUMENTS = [
    (2953217, "TCS", "TATA CONSULTANCY SERV LT"),
    (408065, "INFY", "INFOSYS"),
    (738561, "RELIANCE", "RELIANCE INDUSTRIES"),
    (857857, "SUNPHARMA", "SUN PHARMACEUTICAL IND L"),
    (3930881, "RECLTD", "REC"),
]

# The UI is served from and calls back to this origin. Every ordinary test
# request carries what a same-origin browser page running app.js sends on a
# write: the application header plus a matching Origin (PK-5 / P0-8).
# Security tests that need to omit or alter these build a bare TestClient.
TERMINAL_ORIGIN = "http://127.0.0.1:8000"
TERMINAL_HEADERS = {"X-Kite-Terminal": "1", "Origin": TERMINAL_ORIGIN}


def terminal_client(app, **kwargs):
    from fastapi.testclient import TestClient
    return TestClient(app, base_url=TERMINAL_ORIGIN, headers=TERMINAL_HEADERS, **kwargs)


def last_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def seed_ohlc(db, instrument_token: int, symbol: str, candles: list[dict]) -> None:
    conn = db.get_connection()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO ohlc_daily (instrument_token, symbol, date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(instrument_token, symbol, c["date"].strftime("%Y-%m-%d"), c["open"], c["high"], c["low"], c["close"],
              c["volume"]) for c in candles],
        )
        conn.commit()
    finally:
        conn.close()


def add_watch(db, symbol: str, instrument_token: int) -> None:
    conn = db.get_connection()
    try:
        account_id = conn.execute("SELECT id FROM accounts WHERE is_default = 1").fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (account_id, symbol, instrument_token) VALUES (?, ?, ?)",
            (account_id, symbol, instrument_token),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KITE_TERMINAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("KITE_API_KEY", "test_api_key")
    monkeypatch.setenv("KITE_API_SECRET", "test_api_secret")

    from backend import crypto, db, ui_prefs
    monkeypatch.setattr(crypto, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(ui_prefs, "PREFS_PATH", tmp_path / "ui_prefs.json")
    db.init_db()

    conn = db.get_connection()
    try:
        conn.executemany(
            "INSERT INTO instrument_cache (instrument_token, tradingsymbol, name, exchange, segment, instrument_type) "
            "VALUES (?, ?, ?, 'NSE', 'NSE', 'EQ')",
            SEED_INSTRUMENTS,
        )
        account_id = conn.execute("SELECT id FROM accounts WHERE is_default = 1").fetchone()["id"]
        conn.execute(
            "INSERT INTO sessions (access_token, user_id, user_name, login_time, account_id) VALUES (?, ?, ?, ?, ?)",
            ("fake-access-token", "DT0001", "Demo Trader", datetime.now(IST).isoformat(), account_id),
        )
        conn.commit()
    finally:
        conn.close()

    fake = FakeKite(
        holdings=[{
            "tradingsymbol": "RELIANCE", "exchange": "NSE", "instrument_token": 738561, "quantity": 10,
            "t1_quantity": 0, "average_price": 1200.0, "last_price": 1300.0, "pnl": 1000.0,
            "day_change": 5.0, "day_change_percentage": 0.4,
        }],
        margins_net=1_000_000.0,
        ltp={"NSE:TCS": 3000.0, "NSE:INFY": 1500.0, "NSE:RELIANCE": 1300.0, "NSE:SUNPHARMA": 1700.0},
    )

    from backend import kite_client
    monkeypatch.setattr(kite_client, "get_kite", lambda account_id=None: fake)

    # Reconciliation's cross-cycle sightings (PK-16) are process memory;
    # FakeKite GTT ids and ticket ids restart in every test.
    from backend import reconciliation
    reconciliation.reset_cycle_memory()
    # PK-20: the paper broker's one-shot test hooks are process memory too.
    from backend import paper_broker
    paper_broker.reset_test_hooks()

    from backend.ticker import ticker_manager
    monkeypatch.setattr(ticker_manager, "start", lambda *a, **k: None)
    broadcasts: list[dict] = []
    monkeypatch.setattr(ticker_manager, "broadcast", lambda message: broadcasts.append(message))

    from backend.main import app
    client = terminal_client(app)  # startup hooks intentionally NOT run: no background loops in tests

    return SimpleNamespace(client=client, kite=fake, db=db, broadcasts=broadcasts, tmp_path=tmp_path)


@pytest.fixture
def market_open(monkeypatch):
    """Pins the NSE clock to 'open'. Since PK-12H a live NEW entry outside
    market hours is refused unless the approver explicitly asks for an AMO,
    so a test that places a live entry and is not about market hours pins
    the clock instead of depending on when the suite runs. Modules that
    define their own market_open/market_closed fixtures keep theirs."""
    from backend import market
    monkeypatch.setattr(market, "is_market_open", lambda at=None: True)


def account_id(db) -> int:
    conn = db.get_connection()
    try:
        return conn.execute("SELECT id FROM accounts WHERE is_default = 1").fetchone()["id"]
    finally:
        conn.close()


def set_mode(db, mode: str) -> None:
    conn = db.get_connection()
    try:
        conn.execute("UPDATE execution_settings SET mode = ? WHERE id = 1", (mode,))
        conn.commit()
    finally:
        conn.close()


def displayed_mode(db) -> dict:
    """The confirm body a freshly loaded tab sends for a NEW entry (PK-11):
    the execution mode it is showing, i.e. the server's current mode. Read
    straight from the database so it never touches a race test's hooks.
    Tests of a STALE tab pass their own expected_mode instead."""
    conn = db.get_connection()
    try:
        return {"expected_mode": conn.execute("SELECT mode FROM execution_settings WHERE id = 1").fetchone()["mode"]}
    finally:
        conn.close()


def ticket_row(db, ticket_id: int) -> dict:
    conn = db.get_connection()
    try:
        return dict(conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (ticket_id,)).fetchone())
    finally:
        conn.close()


__all__ = ["FakeKite", "make_candles", "seed_ohlc", "add_watch", "account_id", "set_mode", "displayed_mode", "ticket_row",
           "last_weekday", "IST", "TERMINAL_ORIGIN", "TERMINAL_HEADERS", "terminal_client"]
