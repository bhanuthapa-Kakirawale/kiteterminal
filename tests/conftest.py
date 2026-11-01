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


@pytest.fixture(autouse=True)
def _no_kite_rest_proxy(monkeypatch):
    """NET-1: backend.main's load_dotenv() would copy a real .env's
    KITE_REST_PROXY into the test process. Every test starts on the direct
    route; a test that needs the proxy sets it explicitly."""
    monkeypatch.delenv("KITE_REST_PROXY", raising=False)


def terminal_client(app, **kwargs):
    from fastapi.testclient import TestClient
    return TestClient(app, base_url=TERMINAL_ORIGIN, headers=TERMINAL_HEADERS, **kwargs)


def last_weekday(d: date) -> date:
    """The last NSE trading day on or before d (weekends and, since PK-22,
    exchange holidays skipped)."""
    from backend import trading_calendar
    while not trading_calendar.is_trading_day(d):
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


# LIVE-0: a live NEW entry now requires an absolute notional cap, a rupee
# daily loss limit and a confirmed static IP (account_safety.
# check_live_prerequisites). Every test database gets deliberately generous
# ones, so tests about other behaviour are unaffected; the LIVE-0 tests
# (test_live_preflight.py) clear them to test the gate itself. 203.0.113.10
# is a documentation address (TEST-NET-3), never a real one.
TEST_STATIC_IP = "203.0.113.10"
GENEROUS_NOTIONAL = 1_000_000_000.0
GENEROUS_DAILY_LOSS = 1_000_000_000.0


def seed_live_prerequisites(db) -> None:
    conn = db.get_connection()
    try:
        account_id = conn.execute("SELECT id FROM accounts WHERE is_default = 1").fetchone()["id"]
        conn.execute("UPDATE risk_settings SET max_order_notional = ?, max_daily_loss_amount = ? WHERE account_id = ?",
                     (GENEROUS_NOTIONAL, GENEROUS_DAILY_LOSS, account_id))
        conn.execute("INSERT OR REPLACE INTO live_readiness (account_id, static_ip, static_ip_confirmed_at) "
                     "VALUES (?, ?, datetime('now'))", (account_id, TEST_STATIC_IP))
        conn.commit()
    finally:
        conn.close()


def clear_live_prerequisites(db) -> None:
    conn = db.get_connection()
    try:
        conn.execute("UPDATE risk_settings SET max_order_notional = NULL, max_daily_loss_amount = NULL")
        conn.execute("DELETE FROM live_readiness")
        conn.commit()
    finally:
        conn.close()


# SEC-1: a live NEW entry also needs the live state it rests on, re-checked
# at confirmation (account_safety.check_live_critical_state): a fresh and
# complete reconciliation cycle and healthy order-monitor / trailing /
# reconciliation loops. Tests never run the startup hooks, so nothing would
# be running. Every test therefore starts from a healthy, just-reconciled
# process state (process memory, restored by monkeypatch), exactly like
# seed_live_prerequisites above. The SEC-1 gate tests
# (test_live_critical_gate.py) clear it to test the gate itself.
CRITICAL_WORKERS_SEEDED = ("order_monitor", "order_monitor_paper", "trailing", "reconciliation")


def _utc_sql_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def seed_healthy_live_state(monkeypatch) -> None:
    from backend import reconciliation, worker_health, workers
    monkeypatch.setattr(workers.supervisor, "is_running", lambda name: True)
    monkeypatch.setattr(worker_health, "_workers", {})
    for name in CRITICAL_WORKERS_SEEDED:
        with worker_health.cycle(name):
            pass
    monkeypatch.setattr(reconciliation, "last_cycle", {
        "checked_at": _utc_sql_now(), "gtt_book": "ok", "gtt_error": None, "holdings": "ok",
        "holdings_error": None, "active_gtts": 0, "new_findings": 0, "failed_checks": [],
    })


def clear_healthy_live_state(monkeypatch) -> None:
    """A fresh process: nothing reconciled, no worker running."""
    from backend import reconciliation, worker_health, workers
    monkeypatch.setattr(workers.supervisor, "is_running", lambda name: False)
    monkeypatch.setattr(worker_health, "_workers", {})
    monkeypatch.setattr(reconciliation, "last_cycle", {})


def make_live_ready(env) -> None:
    """SEC-1: the state in which a REAL broker-checked Live Readiness
    evaluation passes (no stub): the stored session token, today's login,
    and fresh strategy history for one watched symbol. Together with the
    healthy process state env seeds, POST /api/execution/mode can arm LIVE.
    Used by tests that switch to live through the API without testing
    readiness itself."""
    from backend import crypto, diagnostics, live_preflight, trading_calendar
    conn = env.db.get_connection()
    try:
        conn.execute("UPDATE accounts SET access_token_enc = ?", (crypto.encrypt("fake-access-token"),))
        conn.execute("UPDATE sessions SET login_time = ?", (datetime.now(IST).isoformat(),))
        conn.commit()
    finally:
        conn.close()
    seed_ohlc(env.db, 2953217, "TCS", make_candles(trading_calendar.latest_completed_session(), 60, 3000.0))
    add_watch(env.db, "TCS", 2953217)
    diagnostics.reset_cache()
    live_preflight.reset_cache()


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
    seed_live_prerequisites(db)

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
    seed_healthy_live_state(monkeypatch)  # SEC-1, after the reset above
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


def as_pre_pk22_database(db) -> None:
    """Makes the test database look as if pre-PK-22 code last started it (no
    schema_version), so the next init_db() runs the legacy migrations again.
    Since PK-22 they run once per database; a test that edits the schema to
    simulate an old database uses this before restarting."""
    conn = db.get_connection()
    try:
        conn.execute("DROP TABLE IF EXISTS schema_version")
        conn.commit()
    finally:
        conn.close()


def account_id(db) -> int:
    conn = db.get_connection()
    try:
        return conn.execute("SELECT id FROM accounts WHERE is_default = 1").fetchone()["id"]
    finally:
        conn.close()


def set_mode(db, mode: str) -> None:
    """Puts the global mode straight into the database. SEC-1: live is
    stamped as armed for the active session, the state a successful
    readiness-gated switch leaves; tests of arming itself go through
    POST /api/execution/mode."""
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE execution_settings SET mode = ?, live_armed_session_id = CASE WHEN ? = 'live' THEN "
            "(SELECT id FROM sessions WHERE is_active = 1 ORDER BY id DESC LIMIT 1) END WHERE id = 1",
            (mode, mode))
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
           "as_pre_pk22_database", "seed_live_prerequisites", "clear_live_prerequisites", "TEST_STATIC_IP",
           "seed_healthy_live_state", "clear_healthy_live_state", "make_live_ready",
           "last_weekday", "IST", "TERMINAL_ORIGIN", "TERMINAL_HEADERS", "terminal_client"]
