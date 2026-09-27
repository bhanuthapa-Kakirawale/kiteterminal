"""PK-10 (P1-2): Emergency Exit All is fault-isolated per position.

Failure to exit ONE position — a refusal, a broker error or an unexpected
exception — never stops the attempts on the others. Every position gets its
own result, the emergency_exit_all system event is always written, and an
exit order placed is never reported as a position closed (PK-8). PK-2/PK-4/
PK-8 ownership is preserved: no sibling SELL while an exit is EXITING or
UNKNOWN. No sleeps; broker outcomes are driven through FakeKite.
"""

import pytest
from fastapi import HTTPException
from kiteconnect.exceptions import OrderException

from conftest import set_mode, ticket_row
from test_baseline_api import _insert_ticket
from test_live_safety import (  # noqa: F401 — market_open / unsafe_client are fixtures
    _monitor, _pending_exit, _precondition, _protective_oco_gtt, _rows, _sells, _unknown_ticket, market_open,
    unsafe_client,
)

_SYMBOLS = {  # symbol: (instrument_token, entry, stop, target)
    "TCS": (2953217, 3000.0, 2900.0, 3300.0),
    "INFY": (408065, 1500.0, 1450.0, 1650.0),
    "SUNPHARMA": (857857, 1700.0, 1650.0, 1900.0),
    "RELIANCE": (738561, 1300.0, 1250.0, 1450.0),
    "RECLTD": (3930881, 400.0, 380.0, 450.0),
}


def _position(env, symbol) -> int:
    """A filled live BUY 5 of `symbol`, protected by an OCO GTT, held at the broker."""
    token, entry, stop, target = _SYMBOLS[symbol]
    gtt_id = _protective_oco_gtt(env, symbol=symbol, stop=stop, target=target)
    buy_id = _insert_ticket(
        env.db, source="strategy", strategy_name="momentum_breakout", symbol=symbol, instrument_token=token,
        entry_price=entry, stop_loss=stop, target_1=target, kite_gtt_id=gtt_id, executed_at="2026-09-01 04:00:00",
        entry_fill_status="filled", filled_price=entry, current_stop_loss=stop,
    )
    env.kite.set_holding(symbol, 5, average_price=entry)
    return buy_id


def _positions(env, *symbols) -> dict[str, int]:
    set_mode(env.db, "live")
    return {s: _position(env, s) for s in symbols}


def _emergency_exit(client):
    return client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})


def _by_symbol(r) -> dict[str, dict]:
    results = r.json()["data"]["results"]
    by_symbol = {res["symbol"]: res for res in results}
    assert len(by_symbol) == len(results), f"a position was reported twice: {results}"
    return by_symbol


def _sold(env) -> list[str]:
    return [s["tradingsymbol"] for s in _sells(env)]


def _event(env) -> str:
    [logged] = _rows(env, "SELECT detail FROM system_events WHERE event_type = 'emergency_exit_all'")
    return logged["detail"]


def _unknown_exit(env, buy_id, symbol) -> int:
    """An exit of buy_id that was claimed and whose broker outcome is UNKNOWN (PK-2)."""
    token, entry, stop, target = _SYMBOLS[symbol]
    return _unknown_ticket(env, source="strategy", strategy_name="momentum_breakout_exit", symbol=symbol,
                           instrument_token=token, transaction_type="SELL", entry_order_type="MARKET",
                           entry_price=entry, stop_loss=stop, target_1=target, closes_ticket_id=buy_id)


def _exiting(env, buy_id, symbol) -> int:
    """An exit of buy_id that Kite accepted and has not filled (EXITING, PK-8)."""
    token, entry, stop, target = _SYMBOLS[symbol]
    sell_id = _pending_exit(env, buy_id, symbol=symbol, instrument_token=token, entry_price=entry,
                            stop_loss=stop, target_1=target)
    env.kite.auto_fill = False
    r = env.client.post(f"/api/tickets/{sell_id}/confirm")
    _precondition(r.status_code == 200 and r.json()["data"]["exit_state"] == "exiting", f"exit not EXITING: {r.text}")
    env.kite.auto_fill = True
    return sell_id


def _raise_for(symbol, real):
    def create(conn, account, position, ltp_data):
        if position["symbol"] == symbol:
            raise RuntimeError(f"simulated failure creating the {symbol} exit")
        return real(conn, account, position, ltp_data)
    return create


# ---------------------------------------------------------------- 1. isolation

@pytest.mark.parametrize("where", ["ticket_creation", "risk_transition"])
def test_middle_position_raising_does_not_stop_the_others(env, unsafe_client, market_open, monkeypatch, where):
    from backend import risk_engine
    from backend.routes import kill_switch
    ids = _positions(env, "TCS", "INFY", "SUNPHARMA")
    if where == "ticket_creation":
        monkeypatch.setattr(kill_switch, "_create_emergency_exit_ticket",
                            _raise_for("INFY", kill_switch._create_emergency_exit_ticket))
    else:
        real = risk_engine.run_risk_checks_and_transition

        def risk(conn, account, ticket_id, kite):
            if ticket_row(env.db, ticket_id)["symbol"] == "INFY":
                raise RuntimeError("simulated risk engine crash")
            return real(conn, account, ticket_id, kite)
        monkeypatch.setattr(risk_engine, "run_risk_checks_and_transition", risk)

    r = _emergency_exit(unsafe_client)

    assert r.status_code == 200, r.text
    results = _by_symbol(r)
    assert [res["symbol"] for res in r.json()["data"]["results"]] == ["TCS", "INFY", "SUNPHARMA"]
    assert (results["TCS"]["status"], results["SUNPHARMA"]["status"]) == ("exit_submitted", "exit_submitted")
    assert (results["INFY"]["status"], results["INFY"]["error_code"]) == ("failed", "EMERGENCY_EXIT_ERROR")
    assert "RuntimeError" in results["INFY"]["message"]
    assert sorted(_sold(env)) == ["SUNPHARMA", "TCS"]
    assert ticket_row(env.db, ids["INFY"])["closed_at"] is None
    # No emergency SELL for INFY is left behind waiting for approval.
    assert _rows(env, "SELECT id FROM trade_tickets WHERE closes_ticket_id = ? AND status IN "
                      "('draft', 'pending_confirmation', 'confirmed', 'executed')", ids["INFY"]) == []
    if where == "risk_transition":
        assert ticket_row(env.db, results["INFY"]["ticket_id"])["status"] == "rejected"


# ---------------------------------------------------------------- 2 / 3. ownership preserved

def test_already_exiting_position_gets_no_duplicate_sell(env, market_open):
    ids = _positions(env, "TCS", "INFY", "SUNPHARMA")
    owner = _exiting(env, ids["TCS"], "TCS")
    _precondition(_sold(env) == ["TCS"], "the EXITING exit should be the only SELL so far")

    results = _by_symbol(_emergency_exit(env.client))

    assert (results["TCS"]["status"], results["TCS"]["ticket_id"]) == ("already_exiting", owner)
    assert results["INFY"]["status"] == results["SUNPHARMA"]["status"] == "exit_submitted"
    assert _sold(env).count("TCS") == 1


def test_unknown_exit_position_gets_no_duplicate_sell(env, market_open):
    ids = _positions(env, "TCS", "INFY", "SUNPHARMA")
    unknown = _unknown_exit(env, ids["TCS"], "TCS")

    results = _by_symbol(_emergency_exit(env.client))

    assert (results["TCS"]["status"], results["TCS"]["ticket_id"]) == ("exit_outcome_unknown", unknown)
    assert sorted(_sold(env)) == ["INFY", "SUNPHARMA"]
    assert ticket_row(env.db, unknown)["status"] == "confirmed"  # untouched: order_monitor resolves it


# ---------------------------------------------------------------- 4. refusals

def test_position_refused_by_lifecycle_validation_does_not_stop_the_others(env, market_open, monkeypatch):
    from backend.routes import tickets
    ids = _positions(env, "TCS", "INFY", "SUNPHARMA")
    real = tickets._refuse_stale_exit

    def refuse_infy(conn, ticket, account_id):
        if ticket["closes_ticket_id"] == ids["INFY"]:
            raise HTTPException(409, {"error_code": "EXIT_POSITION_CLOSED", "message": "closed meanwhile"})
        return real(conn, ticket, account_id)
    monkeypatch.setattr(tickets, "_refuse_stale_exit", refuse_infy)

    results = _by_symbol(_emergency_exit(env.client))

    assert (results["INFY"]["status"], results["INFY"]["error_code"]) == ("refused", "EXIT_POSITION_CLOSED")
    assert results["TCS"]["status"] == results["SUNPHARMA"]["status"] == "exit_submitted"
    assert sorted(_sold(env)) == ["SUNPHARMA", "TCS"]


def test_exit_failing_validation_at_creation_is_refused_not_raw_status(env, market_open, monkeypatch):
    from backend import risk_engine
    _positions(env, "TCS", "INFY")
    real = risk_engine.run_risk_checks_and_transition

    def risk(conn, account, ticket_id, kite):
        real(conn, account, ticket_id, kite)
        if ticket_row(env.db, ticket_id)["symbol"] == "TCS":
            conn.execute("UPDATE trade_tickets SET status = 'risk_rejected' WHERE id = ?", (ticket_id,))
            conn.commit()
    monkeypatch.setattr(risk_engine, "run_risk_checks_and_transition", risk)

    results = _by_symbol(_emergency_exit(env.client))

    assert (results["TCS"]["status"], results["TCS"]["error_code"]) == ("refused", "EXIT_VALIDATION_FAILED")
    assert results["INFY"]["status"] == "exit_submitted"
    assert _sold(env) == ["INFY"]


# ---------------------------------------------------------------- 5. broker errors

def test_broker_rejection_on_one_position_is_a_per_position_failure(env, market_open):
    _positions(env, "TCS", "INFY", "SUNPHARMA")
    env.kite.raise_on_next("place_order", OrderException("RMS: margin exceeded"))  # TCS's SELL, the first

    results = _by_symbol(_emergency_exit(env.client))

    assert results["TCS"]["status"] == "failed"
    assert results["INFY"]["status"] == results["SUNPHARMA"]["status"] == "exit_submitted"
    # TCS's SELL was attempted once and refused: no TCS order exists at the broker.
    assert _sold(env).count("TCS") == 1
    accepted = sorted(o["tradingsymbol"] for o in env.kite.order_book.values() if o["transaction_type"] == "SELL")
    assert accepted == ["INFY", "SUNPHARMA"]


def test_exception_after_the_claim_is_unknown_never_failed(env, unsafe_client, market_open, monkeypatch):
    from backend.routes import tickets
    ids = _positions(env, "TCS", "INFY", "SUNPHARMA")
    real = tickets._record_execution_result

    def record(conn, ticket, result):
        if ticket["symbol"] == "INFY":
            raise RuntimeError("simulated DB failure after the broker call")
        return real(conn, ticket, result)
    monkeypatch.setattr(tickets, "_record_execution_result", record)

    r = _emergency_exit(unsafe_client)

    assert r.status_code == 200, r.text
    results = _by_symbol(r)
    # The SELL may well exist (here it does): never reported as failed, and
    # never resubmitted — the claimed ticket is left for order_monitor.
    assert results["INFY"]["status"] == "exit_outcome_unknown"
    assert "check Kite" in results["INFY"]["message"]
    assert ticket_row(env.db, results["INFY"]["ticket_id"])["status"] == "confirmed"
    assert _sold(env).count("INFY") == 1
    assert results["TCS"]["status"] == results["SUNPHARMA"]["status"] == "exit_submitted"
    assert ticket_row(env.db, ids["INFY"])["closed_at"] is None


# ---------------------------------------------------------------- 6 / 7 / 8. event, result table, never "closed"

def test_system_event_written_after_partial_failure(env, unsafe_client, market_open, monkeypatch):
    from backend.routes import kill_switch
    _positions(env, "TCS", "INFY", "SUNPHARMA")
    monkeypatch.setattr(kill_switch, "_create_emergency_exit_ticket",
                        _raise_for("SUNPHARMA", kill_switch._create_emergency_exit_ticket))

    r = _emergency_exit(unsafe_client)

    assert r.status_code == 200
    summary = r.json()["data"]["summary"]
    assert (summary["exit_submitted"], summary["failed"]) == (2, 1)
    detail = _event(env)
    assert "3 position(s) attempted" in detail
    assert "exit_submitted 2" in detail and "failed 1" in detail and "already_exiting 0" in detail
    assert "TCS=exit_submitted" in detail and "SUNPHARMA=failed" in detail
    assert "exited" not in detail.replace("not exited", "")


def test_system_event_write_failure_still_returns_results(env, market_open, monkeypatch):
    from backend import system_events
    _positions(env, "TCS")

    def broken(*args, **kwargs):
        raise RuntimeError("audit table unavailable")
    monkeypatch.setattr(system_events, "log_event", broken)

    r = _emergency_exit(env.client)

    assert r.status_code == 200
    assert _by_symbol(r)["TCS"]["status"] == "exit_submitted"


def test_result_lists_every_position_once_with_its_outcome(env, market_open):
    ids = _positions(env, "TCS", "INFY", "SUNPHARMA")

    r = _emergency_exit(env.client)

    data = r.json()["data"]
    assert data["count"] == 3
    assert [(res["symbol"], res["status"]) for res in data["results"]] == [
        ("TCS", "exit_submitted"), ("INFY", "exit_submitted"), ("SUNPHARMA", "exit_submitted")]
    for res in data["results"]:
        exit_row = ticket_row(env.db, res["ticket_id"])
        assert exit_row["closes_ticket_id"] == ids[res["symbol"]] and exit_row["transaction_type"] == "SELL"
        assert res["message"]


def test_no_position_is_reported_closed_on_submission(env, market_open):
    ids = _positions(env, "TCS", "INFY")  # FakeKite fills at once: the orders are COMPLETE at the broker

    r = _emergency_exit(env.client)

    assert {res["status"] for res in r.json()["data"]["results"]} == {"exit_submitted"}
    assert r.json()["data"]["summary"]["closed"] == 0
    assert all(ticket_row(env.db, buy_id)["closed_at"] is None for buy_id in ids.values())
    _monitor(env)  # only the broker-fill evidence closes them (PK-8)
    assert all(ticket_row(env.db, buy_id)["closed_at"] is not None for buy_id in ids.values())


# ---------------------------------------------------------------- 9 / 10. empty and mixed

def test_empty_portfolio_is_handled_cleanly(env):
    set_mode(env.db, "live")

    r = _emergency_exit(env.client)

    assert r.status_code == 200
    data = r.json()["data"]
    assert (data["count"], data["results"]) == (0, [])
    assert set(data["summary"].values()) == {0}
    assert "0 position(s) attempted" in _event(env)
    assert env.kite.calls_to("place_order") == []


def test_listing_failure_is_logged_and_reported(env, monkeypatch):
    from backend import risk_engine

    def broken(conn, account_id):
        raise RuntimeError("positions unavailable")
    monkeypatch.setattr(risk_engine, "list_open_positions", broken)

    r = _emergency_exit(env.client)

    assert r.status_code == 503 and r.json()["detail"]["error_code"] == "EMERGENCY_EXIT_UNAVAILABLE"
    assert "could not list open positions" in _event(env)
    assert env.kite.calls_to("place_order") == []


def test_mixed_outcomes_in_one_click(env, unsafe_client, market_open, monkeypatch):
    from backend.routes import kill_switch, tickets
    ids = _positions(env, "TCS", "INFY", "SUNPHARMA", "RELIANCE", "RECLTD")
    _exiting(env, ids["TCS"], "TCS")
    _unknown_exit(env, ids["INFY"], "INFY")
    monkeypatch.setattr(kill_switch, "_create_emergency_exit_ticket",
                        _raise_for("RELIANCE", kill_switch._create_emergency_exit_ticket))
    real = tickets._refuse_stale_exit

    def refuse_recltd(conn, ticket, account_id):
        if ticket["closes_ticket_id"] == ids["RECLTD"]:
            raise HTTPException(409, {"error_code": "EXIT_ALREADY_IN_PROGRESS", "message": "taken meanwhile"})
        return real(conn, ticket, account_id)
    monkeypatch.setattr(tickets, "_refuse_stale_exit", refuse_recltd)
    sells_before = len(_sells(env))

    r = _emergency_exit(unsafe_client)

    assert r.status_code == 200, r.text
    assert {s: res["status"] for s, res in _by_symbol(r).items()} == {
        "TCS": "already_exiting", "INFY": "exit_outcome_unknown", "SUNPHARMA": "exit_submitted",
        "RELIANCE": "failed", "RECLTD": "refused",
    }
    assert [s["tradingsymbol"] for s in _sells(env)[sells_before:]] == ["SUNPHARMA"]
    summary = r.json()["data"]["summary"]
    assert (summary["exit_submitted"], summary["already_exiting"], summary["exit_outcome_unknown"],
            summary["refused"], summary["failed"]) == (1, 1, 1, 1, 1)
    assert "5 position(s) attempted" in _event(env)
