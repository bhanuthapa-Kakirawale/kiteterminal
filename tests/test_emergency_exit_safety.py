"""Emergency Exit, audited on its own (final order-path remediation, finding 4).

Through the real route POST /api/kill-switch/emergency-exit-confirm, FakeKite only:

- it needs a valid, authenticated terminal session AND a current Kite session for the trading
  account: refused after logout, after a previous day's Kite login, on terminal idle expiry and
  after the absolute lifetime (fresh authentication stays required);
- it is not an entry: not held back by the LIVE-entry gate, LIVE not being armed, a stale P1-D
  review, the daily entry limit, the breaker, the MARKET-only rule or the pre-existing-quantity
  guard, and never needs margin;
- it never creates a GTT (no new protective entry GTT as a side effect), including for a position
  whose own GTT failed;
- it does not bypass validation: it sells the broker-confirmed open quantity, never the ordered
  one, refuses an unknown quantity, and is checked by the exit-validity rule;
- its outcomes are explicit: rejection -> failed; timeout or session error on the SELL -> UNKNOWN,
  sent once, never resubmitted; a second click while exiting -> already_exiting; an asynchronous
  rejection or partial fill keeps the position open, protected and exitable;
- critical states never block it, and entries whose outcome is UNKNOWN are reported, not hidden.
"""

from datetime import datetime, timedelta

import pytest
from kiteconnect.exceptions import NetworkException, OrderException, TokenException

from backend import live_entry_gate
from conftest import IST, account_id, reauthorize, set_mode, ticket_row
from test_baseline_api import _insert_ticket
from test_emergency_exit_isolation import _emergency_exit, _positions, _sold
from test_live_safety import _monitor, market_open  # noqa: F401 - fixture

BROKER_WRITES = ("place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt")


def _writes(env, mark=0):
    return [n for n, _ in env.kite.calls[mark:] if n in BROKER_WRITES]


def _code(r):
    return r.json()["detail"]["error_code"]


@pytest.fixture
def session_bound(env, monkeypatch):
    """The conftest FakeKite ignores the Kite session; the real get_kite does not. Mirror it."""
    from backend import kite_client

    def get_kite(account_id=None):
        account = kite_client._resolve_account(account_id)
        if not kite_client.is_session_fresh(kite_client.get_active_session(account["id"])):
            raise kite_client.NoActiveSessionError()
        return env.kite
    monkeypatch.setattr(kite_client, "get_kite", get_kite)
    return env


def _sql(env, statement, params=()):
    conn = env.db.get_connection()
    try:
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- authentication and session

def test_01_a_previous_days_kite_session_is_refused_and_nothing_is_sent(session_bound, market_open):
    env = session_bound
    _positions(env, "TCS")
    mark = len(env.kite.calls)
    _sql(env, "UPDATE sessions SET login_time = ?", ((datetime.now(IST) - timedelta(days=1)).isoformat(),))
    r = _emergency_exit(env.client)
    assert r.status_code == 401 and _code(r) == "NO_SESSION", r.text
    assert _writes(env, mark) == []


def test_02_after_logout_it_is_refused_until_a_fresh_login(session_bound, market_open):
    env = session_bound
    _positions(env, "TCS")
    mark = len(env.kite.calls)
    assert env.client.post("/api/logout").status_code == 200
    r = _emergency_exit(env.client)
    assert r.status_code == 401 and _code(r) == "TERMINAL_SESSION_REQUIRED"
    reauthorize(env.client)                       # the terminal credential alone is not a Kite session
    r = _emergency_exit(env.client)
    assert r.status_code == 401 and _code(r) == "NO_SESSION"
    assert _writes(env, mark) == []


@pytest.fixture
def clock(monkeypatch):
    """A fake monotonic clock, requested BEFORE `env` so the credential is issued on it."""
    from backend import security
    now = [10_000.0]

    class _Clock:
        def advance(self, seconds: float) -> None:
            now[0] += seconds
    monkeypatch.setattr(security, "_clock", lambda: now[0])
    monkeypatch.delenv(security.SESSION_ABSOLUTE_ENV, raising=False)
    monkeypatch.delenv(security.SESSION_IDLE_ENV, raising=False)
    return _Clock()


def test_03_terminal_idle_expiry_refuses_it(clock, env, market_open):
    from backend import security
    _positions(env, "TCS")
    mark = len(env.kite.calls)
    clock.advance(31 * 60)
    r = _emergency_exit(env.client)
    assert r.status_code == 401 and r.json()["detail"]["reason"] == security.EXPIRED_IDLE, r.text
    assert _writes(env, mark) == []


def test_04_the_absolute_lifetime_cannot_be_extended_to_reach_it(clock, env, market_open):
    _positions(env, "TCS")
    mark = len(env.kite.calls)
    for _ in range(17):                           # 17 x 29 min of activity > the 8 h lifetime
        clock.advance(29 * 60)
        env.client.get("/api/tickets", headers={"X-Kite-Terminal-Activity": "user"})
    r = _emergency_exit(env.client)
    assert r.status_code == 401
    assert _writes(env, mark) == []


# ---------------------------------------------------------------- not an entry

def test_05_no_entry_gate_holds_it_back(env, market_open, monkeypatch):
    from conftest import restore_interim_guards
    from backend import account_safety
    _positions(env, "TCS", "INFY")
    # Every entry-only control, at once, in its most restrictive state:
    monkeypatch.setattr(live_entry_gate, "LIVE_ENTRIES_ENABLED", False)    # production gate
    restore_interim_guards(monkeypatch)                                     # MARKET-only + holdings guard
    set_mode(env.db, "dry_run")                                             # LIVE not armed
    conn = env.db.get_connection()
    try:
        account_safety.set_trading_lock(conn, account_id(env.db), True, "test: locked")   # trading lock ON
        conn.commit()
    finally:
        conn.close()
    _sql(env, "UPDATE account_safety_state SET breaker_open = 1, breaker_reason = 'test'")  # breaker open
    env.kite.margins_live_balance = 0.0                                     # no funds at all
    r = _emergency_exit(env.client)
    assert r.status_code == 200, r.text
    assert sorted(_sold(env)) == ["INFY", "TCS"]
    statuses = {x["symbol"]: x["status"] for x in r.json()["data"]["results"]}
    assert statuses == {"TCS": "exit_submitted", "INFY": "exit_submitted"}
    assert env.kite.calls_to("order_margins") == []


def test_06_it_never_creates_a_gtt_even_for_a_position_whose_gtt_failed(env, market_open):
    _positions(env, "TCS")
    unprotected = _insert_ticket(env.db, source="strategy", strategy_name="momentum_breakout", symbol="INFY",
                                 instrument_token=408065, entry_price=1500.0, stop_loss=1450.0, target_1=1650.0,
                                 kite_gtt_id=None, entry_fill_status="filled", filled_quantity=5,
                                 kite_order_id="250000000444", executed_at="2026-09-01 04:00:00")
    env.kite.set_holding("INFY", 5, average_price=1500.0)
    gtts_before = len(env.kite.calls_to("place_gtt"))
    r = _emergency_exit(env.client)
    assert r.status_code == 200, r.text
    assert sorted(_sold(env)) == ["INFY", "TCS"]
    assert len(env.kite.calls_to("place_gtt")) == gtts_before   # not one GTT, for either position
    assert env.kite.calls_to("modify_gtt") == []
    assert ticket_row(env.db, unprotected)["kite_gtt_id"] is None


# ---------------------------------------------------------------- validation

def test_07_it_sells_the_confirmed_open_quantity_never_the_ordered_one(env, market_open):
    set_mode(env.db, "live")
    pos = _insert_ticket(env.db, quantity=100, entry_fill_status="partial", filled_quantity=40,
                         kite_order_id="250000000445", kite_gtt_id=None, executed_at="2026-09-01 04:00:00")
    env.kite.set_holding("TCS", 40, average_price=3000.0)
    r = _emergency_exit(env.client)
    assert r.status_code == 200, r.text
    [sell] = env.kite.calls_to("place_order")
    assert (sell["transaction_type"], sell["quantity"], sell["tradingsymbol"]) == ("SELL", 40, "TCS")
    assert ticket_row(env.db, pos)["closed_at"] is None             # EXITING, not closed on submission


def test_08_an_unknown_open_quantity_is_refused_and_nothing_is_sent(env, market_open):
    set_mode(env.db, "live")
    _insert_ticket(env.db, quantity=100, entry_fill_status="partial", filled_quantity=None,
                   kite_order_id="250000000446", kite_gtt_id=None, executed_at="2026-09-01 04:00:00")
    r = _emergency_exit(env.client)
    assert r.status_code == 200, r.text
    [result] = r.json()["data"]["results"]
    assert (result["status"], result["error_code"]) == ("refused", "QUANTITY_UNKNOWN")
    assert _writes(env) == []


# ---------------------------------------------------------------- outcomes

def test_09_a_synchronous_rejection_is_failed_and_the_position_stays_open(env, market_open):
    pos = _positions(env, "TCS")["TCS"]
    env.kite.raise_on_next("place_order", OrderException("RMS: blocked"))
    [result] = _emergency_exit(env.client).json()["data"]["results"]
    assert result["status"] == "failed"
    assert ticket_row(env.db, pos)["closed_at"] is None and ticket_row(env.db, pos)["kite_gtt_id"] is not None


@pytest.mark.parametrize("exc", [NetworkException("Gateway timeout"), TokenException("Token is invalid")],
                         ids=["timeout", "session-expired-at-kite"])
def test_10_an_ambiguous_sell_is_unknown_sent_once_and_never_resubmitted(env, market_open, exc):
    _positions(env, "TCS")
    env.kite.accept_then_raise("place_order", exc)
    [result] = _emergency_exit(env.client).json()["data"]["results"]
    assert result["status"] == "exit_outcome_unknown"
    [again] = _emergency_exit(env.client).json()["data"]["results"]
    assert again["status"] == "exit_outcome_unknown"
    assert len(env.kite.calls_to("place_order")) == 1


def test_11_a_second_click_while_exiting_sends_nothing_more(env, market_open):
    _positions(env, "TCS")
    env.kite.auto_fill = False
    assert _emergency_exit(env.client).json()["data"]["results"][0]["status"] == "exit_submitted"
    assert _emergency_exit(env.client).json()["data"]["results"][0]["status"] == "already_exiting"
    assert len(env.kite.calls_to("place_order")) == 1


def test_12_an_asynchronous_rejection_keeps_the_position_open_protected_and_exitable(env, market_open):
    pos = _positions(env, "TCS")["TCS"]
    env.kite.auto_fill = False
    [result] = _emergency_exit(env.client).json()["data"]["results"]
    exit_row = ticket_row(env.db, result["ticket_id"])
    env.kite.set_order_state(exit_row["kite_order_id"], "REJECTED", filled_quantity=0, status_message="RMS")
    _monitor(env)
    row = ticket_row(env.db, pos)
    assert row["closed_at"] is None and row["kite_gtt_id"] is not None
    assert _emergency_exit(env.client).json()["data"]["results"][0]["status"] == "exit_submitted"


def test_13_an_asynchronous_partial_fill_leaves_the_rest_open_and_exitable(env, market_open):
    pos = _positions(env, "TCS")["TCS"]
    env.kite.auto_fill = False
    [result] = _emergency_exit(env.client).json()["data"]["results"]
    exit_row = ticket_row(env.db, result["ticket_id"])
    env.kite.set_order_state(exit_row["kite_order_id"], "CANCELLED", filled_quantity=2, average_price=3000.0)
    env.kite.set_holding("TCS", 3, average_price=3000.0)
    _monitor(env)
    assert ticket_row(env.db, pos)["closed_at"] is None
    _emergency_exit(env.client)
    last = env.kite.calls_to("place_order")[-1]
    assert (last["transaction_type"], last["quantity"]) == ("SELL", 3)


# ---------------------------------------------------------------- critical states and unknown entries

def test_14_critical_states_never_block_it(env, market_open, monkeypatch):
    from conftest import clear_healthy_live_state
    from backend import reconciliation
    _positions(env, "TCS")
    clear_healthy_live_state(monkeypatch)                         # no worker, no reconciliation cycle
    conn = env.db.get_connection()
    try:
        reconciliation.log_mismatch(conn, None, "position_state", expected="x", actual="y",
                                    detail="critical test finding", issue_code=reconciliation.ORPHAN_GTT,
                                    symbol="TCS", gtt_id=1)
        conn.commit()
    finally:
        conn.close()
    r = _emergency_exit(env.client)
    assert r.status_code == 200, r.text
    [result] = r.json()["data"]["results"]
    assert result["status"] == "exit_submitted" and "warning" in result


def test_15_entries_of_unknown_outcome_are_reported_not_silently_left_out(env, market_open):
    from test_live_safety import _unknown_ticket
    _positions(env, "TCS")
    unknown = _unknown_ticket(env)
    r = _emergency_exit(env.client)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert [u["ticket_id"] for u in data["not_exitable_unknown"]] == [unknown]
    assert "cannot sell a quantity it cannot prove" in data["not_exitable_unknown"][0]["message"]
    assert data["summary"]["not_exitable_unknown"] == 1
    assert _sold(env) == ["TCS"]                                  # never a SELL for the unknown entry
