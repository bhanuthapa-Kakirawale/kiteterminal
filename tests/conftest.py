import contextlib
import os
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
import real_data_guard  # noqa: E402

# Before any test runs: nothing in the suite may open, connect to, copy or
# pass on the real account database or its backups, the real .env or the
# real UI preferences, nor connect beyond loopback (tests/real_data_guard.py).
real_data_guard.install()
# F-0: backend/main.py calls load_dotenv() at import. The loaders are replaced
# BEFORE anything imports backend.main, so that call loads nothing; and every
# KITE_* variable inherited from the shell goes, so no test runs on real
# configuration. Tests set their own sentinel values (the env fixture).
if "backend.main" in sys.modules:
    raise RuntimeError("tests/conftest.py: backend.main was imported before the test isolation guard; "
                       "its load_dotenv() may already have read the real .env")
real_data_guard.neutralise_dotenv()
_SCRUBBED_AT_START = real_data_guard.scrub_kite_environment()  # names only
# V12 final readiness: captures the real db.backup_database before any test can patch it (tests/offsite_helpers.py).
import offsite_helpers  # noqa: E402,F401

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


@pytest.fixture(scope="session", autouse=True)
def _session_isolated_files(tmp_path_factory):
    """A test that does not use `env` still cannot mint an encryption key into
    the real .env (crypto.ensure_encryption_key appends to crypto.ENV_PATH) or
    write the real UI preferences: both point at session temp files. The
    database path is deliberately NOT redirected: a test that forgot `env`
    must fail on the guard, not run silently on some other database."""
    from backend import crypto, installation_state, ui_prefs
    session_dir = tmp_path_factory.mktemp("isolated_session")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(crypto, "ENV_PATH", session_dir / ".env.session")
        mp.setattr(ui_prefs, "PREFS_PATH", session_dir / "ui_prefs.json")
        # V12 final readiness, Phase C: the installation registry lives in the user profile; every test (and every
        # child process, which inherits the variable: it is not a KITE_* name) uses a session temp dir instead.
        # The real registry is protected by tests/real_data_guard.py.
        mp.setenv(installation_state.REGISTRY_ENV, str(session_dir / "installation_registry"))
        yield session_dir


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "v11_database: env creates its temporary database with the Phase 2 build (exactly schema v11, "
                   "migration 12 pending on a build that registers it); see tests/schema_builds.py")


@pytest.fixture(autouse=True)
def _build_restored():
    """Every test leaves the schema build as it found it: MIGRATIONS and LATEST_SCHEMA_VERSION (tests/schema_builds.py).
    Defined before every fixture that takes monkeypatch, so it is torn down after monkeypatch's undo and sees the
    final state: a simulated build leaking into later tests (a hand-restored build plus a monkeypatched MIGRATIONS)
    fails the test that leaked it instead of corrupting every test after it."""
    from backend import db
    before = (db.MIGRATIONS, list(db.MIGRATIONS), db.LATEST_SCHEMA_VERSION)
    yield
    assert (db.MIGRATIONS, db.LATEST_SCHEMA_VERSION) == (before[1], before[2]), "the test left the schema build changed"
    db.MIGRATIONS = before[0]   # the same list object, for modules holding a reference


@pytest.fixture(autouse=True)
def _no_kite_rest_proxy(monkeypatch):
    """NET-1: every test starts on the direct route; a test that needs the
    proxy sets it explicitly. Defence in depth since F-0: load_dotenv is
    neutralised and inherited KITE_* variables are scrubbed, but a test that
    set the variable without monkeypatch must not leak it into the next."""
    monkeypatch.delenv("KITE_REST_PROXY", raising=False)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    calls = real_data_guard.dotenv_calls()
    refused = real_data_guard.refusals()
    unexpected = real_data_guard.unexpected_refusals()
    write = terminalreporter.write_line
    terminalreporter.section("test isolation (tests/real_data_guard.py)")
    write(f"python-dotenv neutralised: {real_data_guard.dotenv_neutralised()}; "
          f"implicit loads neutralised: {sum(c['mode'] == 'implicit' for c in calls)}; "
          f"explicit loads delegated (temporary files): {sum(c['result'] == 'delegated' for c in calls)}; "
          f"explicit protected loads refused: {sum(c['result'] == 'refused' for c in calls)}")
    names = _SCRUBBED_AT_START
    write(f"inherited KITE_* variables scrubbed at start: {len(names)}"
          + (f" ({', '.join(names)}; names only)" if names else ""))
    write(f"guard refusals: {len(refused)} (decoy {sum(r[2] for r in refused)}, "
          f"expected {sum(r[3] and not r[2] for r in refused)}, unexpected {len(unexpected)}); "
          f"network refusals: {sum(r[0] == 'network' for r in refused)}")
    for kind, target, _, _ in unexpected:
        write(f"UNEXPECTED REFUSAL: {kind} {target}", red=True)
    children = _child_guard_summary()
    if children is not None:
        # Phase 3 S3: every child process of the multi-process harness installs its own guard
        # (tests/child_bootstrap.py) and reports it; tests/mp_harness.py collects the reports.
        write(f"child processes (S3 harness): {children['children']} guard reports; refusals {children['refusals']} "
              f"(decoy {children['decoy']}, expected {children['expected']}, unexpected {len(children['unexpected'])}); "
              f"protected opens {len(children['protected'])}; missing reports {len(children['missing_reports'])}; "
              f"orphan processes {len(children['orphans'])}")
        for item in children["unexpected"] + children["protected"]:
            write(f"UNEXPECTED CHILD ACCESS: {item}", red=True)
        for item in children["missing_reports"] + [f"orphan pid {p}" for p in children["orphans"]]:
            write(f"CHILD PROCESS PROBLEM: {item}", red=True)


def _child_guard_summary():
    harness = sys.modules.get("mp_harness")
    return harness.session_summary() if harness is not None else None


def pytest_sessionfinish(session, exitstatus):
    # A refusal swallowed by application code (a broad `except`) is still an isolation failure.
    if real_data_guard.unexpected_refusals() and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    # The same for a child process of the S3 harness, and for a child report that never arrived or a
    # child process left running.
    children = _child_guard_summary()
    if children is not None and session.exitstatus == 0 and (
            children["unexpected"] or children["protected"] or children["missing_reports"] or children["orphans"]):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


# SEC-1 P1-F: a write also needs this launch's terminal credential, which the
# app issues only when a Kite login completes. Each test starts with none
# (the in-memory credential is reset around every test); the first terminal
# client issues one, exactly as the /callback would, and every further client
# in the same test reuses it — issuing again would rotate the first client's
# cookie away. Security tests that need to omit or alter it build a bare
# TestClient.
_TEST_CREDENTIAL: dict = {"token": None, "terminal_token": None}


@pytest.fixture(autouse=True)
def _fresh_terminal_credential():
    from backend import security
    security.revoke_terminal_credential()
    security.clear_login_states()
    _TEST_CREDENTIAL.update(token=None, terminal_token=None)
    yield
    security.revoke_terminal_credential()
    security.clear_login_states()
    _TEST_CREDENTIAL.update(token=None, terminal_token=None)


@pytest.fixture(autouse=True)
def _production_integration_profile():
    """Every test starts in the PRODUCTION integration profile; the isolated synthetic DMB test
    profile is entered only through the `synthetic_profile` fixture, and never outlives a test."""
    from backend import integration_profile
    integration_profile.deactivate()
    yield
    integration_profile.deactivate()


@pytest.fixture
def synthetic_profile(env):
    """The isolated synthetic test profile (backend/integration_profile.py) on env's throwaway
    database and FakeKite: synthetic DMB bundles are actionable, LIVE is impossible.
    `approved_test_source` stands for a real source only inside this profile."""
    from backend import integration_profile
    integration_profile.activate_synthetic_test_profile(test_sources=("approved_test_source",))
    assert integration_profile.synthetic_test_active()
    return env


def terminal_credential() -> str:
    """This test's current terminal credential (the cookie value), issued on first use together
    with its port-isolated terminal token (P2-G R-F3, `terminal_token()`)."""
    from backend import security
    if _TEST_CREDENTIAL["token"] is None or not security.terminal_credential_valid(_TEST_CREDENTIAL["token"]):
        _TEST_CREDENTIAL["token"], _TEST_CREDENTIAL["terminal_token"] = security.issue_terminal_credentials()
    return _TEST_CREDENTIAL["token"]


def terminal_token() -> str:
    """The terminal token paired with this test's credential: what the page keeps in localStorage
    and sends as X-Kite-Terminal-Token (P2-G R-F3)."""
    terminal_credential()
    return _TEST_CREDENTIAL["terminal_token"]


def authorize_client(client, cookie: str, token: str) -> None:
    """Both factors of a credential on a client, as the browser holds them after a login."""
    from backend import security
    client.cookies.set(security.CREDENTIAL_COOKIE, cookie)
    client.headers[security.TOKEN_HEADER] = token


def start_kite_login(client) -> str:
    """What GET /login leaves in the browser (P2-G login CSRF guard): a pending, single-use login
    state, cookie-bound to this client, without which /callback exchanges nothing."""
    from backend import security
    token = security.begin_login()
    client.cookies.set(security.LOGIN_STATE_COOKIE, token)
    return token


def reauthorize(client) -> str:
    """What a fresh Kite login in the browser does for the terminal: a new
    credential for this client (after a logout or a simulated restart)."""
    from backend import security
    _TEST_CREDENTIAL["token"], _TEST_CREDENTIAL["terminal_token"] = security.issue_terminal_credentials()
    authorize_client(client, _TEST_CREDENTIAL["token"], _TEST_CREDENTIAL["terminal_token"])
    return _TEST_CREDENTIAL["token"]


def terminal_client(app, **kwargs):
    from fastapi.testclient import TestClient
    from backend import security
    client = TestClient(app, base_url=TERMINAL_ORIGIN, headers=TERMINAL_HEADERS, **kwargs)
    authorize_client(client, terminal_credential(), terminal_token())
    return client


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
# The proxy is mandatory for LIVE (account_safety.check_rest_route, live_preflight KITE_REST_ROUTE),
# and arming needs an outbound-IP observation taken through it (live_preflight.egress_proof). The
# env fixture configures this value and seeds that observation. It is never contacted: env tests
# run on FakeKite, and the discard port on loopback would refuse anything that tried. The proxy
# tests (test_kite_rest_proxy.py, test_proxy_mandatory.py) set their own values.
TEST_REST_PROXY = "http://127.0.0.1:9"

_REAL_INTERIM: dict = {}


def seed_live_prerequisites(db) -> None:
    conn = db.get_connection()
    try:
        account_id = conn.execute("SELECT id FROM accounts WHERE is_default = 1").fetchone()["id"]
        conn.execute("UPDATE risk_settings SET max_order_notional = ?, max_daily_loss_amount = ? WHERE account_id = ?",
                     (GENEROUS_NOTIONAL, GENEROUS_DAILY_LOSS, account_id))
        conn.execute("INSERT OR REPLACE INTO live_readiness (account_id, static_ip, static_ip_confirmed_at, "
                     "egress_ip, egress_checked_at) VALUES (?, ?, datetime('now'), ?, datetime('now'))",
                     (account_id, TEST_STATIC_IP, TEST_STATIC_IP))
        conn.commit()
        checked_at = conn.execute("SELECT egress_checked_at FROM live_readiness WHERE account_id = ?",
                                  (account_id,)).fetchone()["egress_checked_at"]
    finally:
        conn.close()
    from backend import kite_rest_proxy, live_preflight
    live_preflight.reset_egress_routes()
    route = kite_rest_proxy.route_status()
    if route["mode"] != kite_rest_proxy.PROXY:
        route = {"mode": kite_rest_proxy.PROXY, "endpoint": kite_rest_proxy.parse(TEST_REST_PROXY).display}
    live_preflight.record_egress_route(account_id, checked_at, route)


def clear_live_prerequisites(db) -> None:
    conn = db.get_connection()
    try:
        conn.execute("UPDATE risk_settings SET max_order_notional = NULL, max_daily_loss_amount = NULL")
        conn.execute("DELETE FROM live_readiness")
        conn.commit()
    finally:
        conn.close()
    from backend import live_preflight
    live_preflight.reset_egress_routes()


def open_live_entry_gate(monkeypatch) -> None:
    """The LIVE-entry hard gate (backend/live_entry_gate.py) refuses every LIVE new entry in the
    production build. Most of the suite tests the order path BEHIND that gate (claims, risk
    revalidation, account safety, the broker call, fills), so the env fixture lifts it in-process,
    exactly like the generous limits above. test_live_entry_gate.py tests the gate at its
    production value (and that the source constant is False)."""
    from backend import live_entry_gate
    monkeypatch.setattr(live_entry_gate, "LIVE_ENTRIES_ENABLED", True)
    # The two interim guards too: much of the suite was written against LIMIT live entries and
    # several entries of one symbol (the order path behind the guards still supports both).
    # test_presellable_guard.py and test_live_entry_gate.py restore the real guards
    # (restore_interim_guards). The margin preflight stays active for every test.
    _REAL_INTERIM.setdefault("market_rule", live_entry_gate.check_market_regular_only)
    _REAL_INTERIM.setdefault("presellable", live_entry_gate.check_no_presellable_quantity)
    monkeypatch.setattr(live_entry_gate, "check_market_regular_only", lambda ticket, mode, variety: [])
    monkeypatch.setattr(live_entry_gate, "check_no_presellable_quantity",
                        lambda conn, account_id, ticket, kite, mode: ([], {}))


def restore_interim_guards(monkeypatch) -> None:
    from backend import live_entry_gate
    monkeypatch.setattr(live_entry_gate, "check_market_regular_only", _REAL_INTERIM["market_rule"])
    monkeypatch.setattr(live_entry_gate, "check_no_presellable_quantity", _REAL_INTERIM["presellable"])


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


# Phase 2: tests never run the startup hooks, so nothing takes the instance lock or runs the startup
# reconciliation. `env` therefore takes the REAL lock on its tmp database and runs the REAL fenced
# takeover (grant_write_authority), then records a passed Stage 1 run (seed_stage1_passed), exactly like
# seed_healthy_live_state above seeds a just-reconciled process. Without the grant every write route
# answers 503 and every broker write is refused (fail closed; test_write_authority_routes.py proves it).
def grant_write_authority(db) -> None:
    from backend import instance_lock
    instance_lock.reset_for_tests()
    role = instance_lock.acquire(db.DB_PATH)
    assert role == instance_lock.LOCKED, f"the test database's instance lock could not be taken: {role}"
    assert not instance_lock.takeover(poll_seconds=0.01)["conflict"]


def seed_stage1_passed(db) -> int:
    """A passed Stage 1 run in the current epoch, after the latest login, with a complete broker read:
    the account derives NORMAL (if nothing else restricts it). Returns the run id."""
    from backend import instance_lock, startup_reconciliation
    conn = db.get_connection()
    try:
        account_id = conn.execute("SELECT id FROM accounts WHERE is_default = 1").fetchone()["id"]
        now = datetime.now(timezone.utc).isoformat()
        with instance_lock.fenced_transaction(conn):
            epoch = instance_lock.owner_epoch()
            read_id = conn.execute(
                "INSERT INTO broker_reads (account_id, purpose, instance_epoch, started_at, finished_at, orders_count, "
                "gtts_count, gtts_malformed, complete) VALUES (?, 'startup', ?, ?, ?, 0, 0, 0, 1)",
                (account_id, epoch, now, now)).lastrowid
            run_id = conn.execute(
                "INSERT INTO startup_reconciliation_runs (account_id, instance_epoch, trigger, started_at, finished_at, "
                "stage1_outcome, broker_read_id, inventory, findings, reasons_after) "
                "VALUES (?, ?, 'startup', ?, ?, 'passed', ?, '{}', '{\"seeded\": true}', '[]')",
                (account_id, epoch, now, now, read_id)).lastrowid
        startup_reconciliation.persist_state(conn, account_id, sticky=set(), run_id=run_id)
        return run_id
    finally:
        conn.close()


def release_write_authority() -> None:
    from backend import instance_lock
    instance_lock.reset_for_tests()


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
    seed_stage1_passed(env.db)  # Phase 2: the "login" above is newer than the seeded Stage 1 run
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
def owner_process(monkeypatch):
    """Phase 2: process write authority WITHOUT a database, for tests of the Kite client itself (they
    build SDK clients and send writes into a recorded transport, without `env`). Restored afterwards.
    Tests with `env` already own their tmp database through the real lock and takeover."""
    from backend import instance_lock
    monkeypatch.setattr(instance_lock._state, "role", instance_lock.OWNER)
    monkeypatch.setattr(instance_lock._state, "shutting_down", False)


@pytest.fixture
def env(tmp_path, monkeypatch, request):
    monkeypatch.setenv("KITE_TERMINAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("KITE_API_KEY", "test_api_key")
    monkeypatch.setenv("KITE_API_SECRET", "test_api_secret")

    from backend import crypto, db, installation_state, ui_prefs
    monkeypatch.setattr(crypto, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    # V12 final readiness, Phase C: each test is its own "machine" for the installation registry (a sibling of
    # tmp_path, so tmp_path's listing is unchanged); otherwise one test's installation would be another's
    # OTHER_INSTALLATION_PRESENT. Children inherit it (not a KITE_* name).
    monkeypatch.setenv(installation_state.REGISTRY_ENV, str(tmp_path.parent / f"{tmp_path.name}_registry"))
    monkeypatch.setattr(ui_prefs, "PREFS_PATH", tmp_path / "ui_prefs.json")
    if not (os.environ.get("KITE_REST_PROXY") or "").strip():  # a test's own proxy fixture may have run first
        monkeypatch.setenv("KITE_REST_PROXY", TEST_REST_PROXY)
    # A fresh (empty) file is created at this build's latest schema. A test marked v11_database gets exactly the
    # database the Phase 2 build creates instead (schema v11, every object as v11 made it), on either build.
    import schema_builds
    v11 = request.node.get_closest_marker("v11_database") is not None
    with schema_builds.phase2_build(db) if v11 else contextlib.nullcontext():
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
    open_live_entry_gate(monkeypatch)
    from backend import gtt_observations
    gtt_observations.reset()

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
    kite_client.fence_broker(fake)  # Phase 2: the same write fence a real client gets in create_kite_connect
    monkeypatch.setattr(kite_client, "get_kite", lambda account_id=None: fake)

    # Phase 2: this test process owns its tmp database (real lock, real fenced takeover) and a passed
    # Stage 1 run is on record, so the account derives NORMAL. Released after the test.
    request.addfinalizer(release_write_authority)
    grant_write_authority(db)
    seed_stage1_passed(db)
    # A login or resume would start Stage 1 as a background task; tests never run background tasks
    # (like the loops). Recorded instead; test_startup_reconciliation.py runs Stage 1 explicitly.
    from backend import startup_reconciliation
    stage1_requests: list[str] = []
    monkeypatch.setattr(startup_reconciliation, "request_run", lambda trigger: stage1_requests.append(trigger))

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

    return SimpleNamespace(client=client, kite=fake, db=db, broadcasts=broadcasts, tmp_path=tmp_path,
                           stage1_requests=stage1_requests)


@pytest.fixture
def v11_env(request):
    """env on a database at exactly schema v11, created by the Phase 2 build (the v11_database marker), on
    either build. Request it INSTEAD of env (not alongside it)."""
    request.node.add_marker("v11_database")
    return request.getfixturevalue("env")


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
    schema_version), so the next start runs the legacy migrations again.
    Since PK-22 they run once per database; a test that edits the schema to
    simulate an old database uses this before restarting. Restart it with
    schema_builds.restart_old_database: since the V12 authorization gate a
    normal start never applies migration 12 to a database holding data, so on
    a build that registers v12 the Phase 2 build brings it to v11 and v12 is
    then applied the authorized way (tests/schema_builds.py)."""
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


def approval_body(db, ticket_id: int, **extra) -> dict:
    """The confirm body a freshly loaded view sends (PK-11 + SEC-1 P1-D): the
    execution mode it shows and, for a manual / Quick-Action BUY entry, the
    review token it shows (account_safety.review_token of the stored row).
    Strategy entries and exits carry no token. Tests of a STALE view pass
    their own expected_mode / expected_review_token in `extra`."""
    from backend import account_safety
    body = displayed_mode(db)
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (ticket_id,)).fetchone()
    finally:
        conn.close()
    token = account_safety.review_token(row) if row is not None else None
    if token is not None:
        body["expected_review_token"] = token
    body.update(extra)
    return body


def ticket_row(db, ticket_id: int) -> dict:
    conn = db.get_connection()
    try:
        return dict(conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (ticket_id,)).fetchone())
    finally:
        conn.close()


__all__ = ["FakeKite", "make_candles", "seed_ohlc", "add_watch", "account_id", "set_mode", "displayed_mode",
           "approval_body", "ticket_row", "terminal_credential", "terminal_token", "authorize_client", "reauthorize",
           "start_kite_login",
           "as_pre_pk22_database", "seed_live_prerequisites", "clear_live_prerequisites", "TEST_STATIC_IP",
           "seed_healthy_live_state", "clear_healthy_live_state", "make_live_ready",
           "last_weekday", "IST", "TERMINAL_ORIGIN", "TERMINAL_HEADERS", "terminal_client"]
