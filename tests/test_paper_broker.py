"""PK-20: the paper broker. DRY RUN is a simulated broker, not "pretend it
filled": a dry-run approval places a paper order that fills only when an
observed market price allows it, and the position then runs through the
same order monitor, GTT watcher, trailing, PK-8 exit and PK-13 quantity
lifecycle as a live one.

Prices are driven through FakeKite's LTP map (the paper broker's market-data
source); the paper exchange runs one step per cycle. Nothing here ever
reaches Zerodha: FakeKite is the "live broker", and every dry-run test also
proves it received no order or GTT call.
"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest
import requests
from kiteconnect.exceptions import GeneralException, InputException

from conftest import IST, account_id, set_mode, ticket_row
from fake_kite import FakeKite
from test_baseline_api import _insert_ticket, _manual_ticket
from test_live_safety import _broker_calls, _closed_trades_for, _rows, market_open  # noqa: F401 — fixture
from backend import market as _market_module

_REAL_IS_MARKET_OPEN = _market_module.is_market_open

SYMBOL, KEY = "INFY", "NSE:INFY"
PRICE, STOP, TARGET = 1500.0, 1450.0, 1600.0


# ---------------------------------------------------------------- helpers

@pytest.fixture(autouse=True)
def no_slippage(monkeypatch):
    """Exact fill prices in most tests; slippage has its own tests."""
    from backend import config
    monkeypatch.setattr(config, "PAPER_SLIPPAGE_BPS", 0.0)


def _ltp(env, price, key=KEY):
    env.kite.ltp_map[key] = price


def _exchange(env):
    """Only the paper order monitor cycle: the exchange step, then the
    dry-run tickets' orders followed through order_monitor."""
    from backend import order_monitor
    return order_monitor.check_paper_orders(env.kite)


def _watch(env):
    """The GTT watcher / trailing cycle (live, then paper positions)."""
    from backend import trailing_engine
    asyncio.run(trailing_engine._tick_once())


def _cycle(env):
    _exchange(env)
    _watch(env)


def _approve(env, quantity=40, order_type="MARKET", price=PRICE, allow_amo=False, **overrides):
    """A dry-run BUY through the real create -> confirm path. Returns
    (ticket id, confirm response data)."""
    set_mode(env.db, "dry_run")
    env.kite.ltp_map.setdefault(KEY, PRICE)
    body = _manual_ticket(symbol=SYMBOL, quantity=quantity, entry_price=price, entry_order_type=order_type,
                          stop_loss=STOP, target_1=TARGET)
    body.update(overrides)
    t = env.client.post("/api/tickets", json=body).json()["data"]
    assert t["status"] == "pending_confirmation", t
    payload = {"expected_mode": "dry_run", **({"allow_amo": True} if allow_amo else {})}
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=payload)
    assert r.status_code == 200, r.text
    return t["id"], r.json()["data"]


def _open_position(env, quantity=40, fill_price=PRICE, **overrides) -> int:
    """A dry-run position filled by the paper exchange at `fill_price`."""
    buy_id, _ = _approve(env, quantity, **overrides)
    _ltp(env, fill_price)
    _cycle(env)
    assert _state(env, buy_id) == "OPEN", _position(env, buy_id)
    return buy_id


def _exit(env, buy_id) -> tuple[int, dict]:
    """Exit from the Positions view, then approve it: (exit id, response)."""
    r = env.client.post(f"/api/app-positions/{buy_id}/exit")
    assert r.status_code == 201, r.text
    exit_id = r.json()["data"]["id"]
    done = env.client.post(f"/api/tickets/{exit_id}/confirm")
    assert done.status_code == 200, done.text
    return exit_id, done.json()["data"]


def _position(env, position_id) -> dict:
    for view in ("active", "closed"):
        for p in env.client.get(f"/api/app-positions?view={view}").json()["data"]:
            if p["position_id"] == position_id:
                return p
    # Not a position any more (CANCELLED, FAILED ...): the ticket's own view.
    return env.client.get(f"/api/tickets/{position_id}").json()["data"]["lifecycle"]


def _state(env, position_id) -> str:
    return _position(env, position_id)["lifecycle_state"]


def _order(env, order_id) -> dict:
    [row] = _rows(env, "SELECT * FROM paper_orders WHERE paper_order_id = ?", order_id)
    return dict(row)


def _gtt(env, gtt_id) -> dict:
    [row] = _rows(env, "SELECT * FROM paper_gtts WHERE paper_gtt_id = ?", gtt_id)
    row = dict(row)
    row["legs"] = json.loads(row["legs"])
    row["trigger_values"] = json.loads(row["trigger_values"])
    return row


def _leg_quantities(env, gtt_id) -> list[int]:
    return [leg["quantity"] for leg in _gtt(env, gtt_id)["legs"]]


def _paper_orders(env) -> list[dict]:
    return [dict(r) for r in _rows(env, "SELECT * FROM paper_orders ORDER BY id")]


def _gtt_order(env, gtt_id) -> dict:
    [row] = _rows(env, "SELECT * FROM paper_orders WHERE source_gtt_id = ?", gtt_id)
    return dict(row)


def _broker(env):
    """A paper broker on its own connection, for unit-level calls."""
    from backend import paper_broker
    conn = env.db.get_connection()
    return conn, paper_broker.PaperBroker(conn, account_id(env.db), market_data=env.kite)


def _place(env, **kwargs) -> str:
    conn, broker = _broker(env)
    try:
        args = dict(variety="regular", exchange="NSE", tradingsymbol=SYMBOL, transaction_type="BUY", quantity=10,
                    product="CNC", order_type="MARKET")
        args.update(kwargs)
        order_id = broker.place_order(**args)
        conn.commit()
        return order_id
    finally:
        conn.close()


def _evaluate(env, now=None):
    from backend import paper_broker
    return paper_broker.evaluate(account_id(env.db), env.kite, now=now)


def _live_mutations(env) -> list[str]:
    return _broker_calls(env)


# ======================================================================= MARKET

def test_1_market_approval_places_a_paper_order_and_fills_nothing(env, market_open):
    buy_id, data = _approve(env)

    row = ticket_row(env.db, buy_id)
    assert (row["mode"], row["status"], row["entry_fill_status"], row["filled_quantity"]) == (
        "dry_run", "executed", "pending", 0)
    assert row["filled_price"] is None and row["filled_at"] is None
    assert row["kite_order_id"].startswith("PAPER-O-") and row["kite_gtt_id"].startswith("PAPER-G-")
    # PK-2 submission tracking applies to the paper order.
    assert row["submission_state"] == "acknowledged" and row["order_tag"]
    order = _order(env, row["kite_order_id"])
    assert (order["status"], order["filled_quantity"], order["pending_quantity"], order["tag"]) == (
        "OPEN", 0, 40, row["order_tag"])
    assert order["ticket_id"] == buy_id
    assert data["execution_message"].startswith("PAPER")
    assert _state(env, buy_id) == "ENTRY_OPEN"
    assert _live_mutations(env) == []


def test_2_3_market_fills_at_the_first_observed_price_not_the_ticket_price(env, market_open):
    buy_id, _ = _approve(env, price=PRICE)
    _ltp(env, 1502.35)
    _exchange(env)

    row = ticket_row(env.db, buy_id)
    assert (row["entry_fill_status"], row["filled_quantity"], row["filled_price"]) == ("filled", 40, 1502.35)
    order = _order(env, row["kite_order_id"])
    assert (order["status"], order["filled_quantity"], order["average_price"]) == ("COMPLETE", 40, 1502.35)
    assert _state(env, buy_id) == "OPEN"


def test_market_order_without_an_observed_price_stays_open(env, market_open):
    buy_id, _ = _approve(env)
    env.kite.raise_on_next("ltp", requests.ConnectionError("quote service down"))
    summary = _exchange(env)

    assert summary["paper_exchange"]["skipped"] == "ltp_unavailable"
    assert ticket_row(env.db, buy_id)["entry_fill_status"] == "pending"
    assert _state(env, buy_id) == "ENTRY_OPEN"


def test_4_slippage_arithmetic_is_exact(monkeypatch):
    from backend import config, paper_broker
    fill = paper_broker.fill_price
    # BUY: observed x (1 + 5/10000), rounded UP to the tick.
    assert fill("BUY", "MARKET", 1500.0, None, 0.05, 5) == 1500.75
    assert fill("BUY", "MARKET", 1000.02, None, 0.05, 5) == 1000.55   # 1000.520001 -> up
    # SELL: observed x (1 - 5/10000), rounded DOWN to the tick.
    assert fill("SELL", "MARKET", 1600.0, None, 0.05, 5) == 1599.20
    assert fill("SELL", "MARKET", 1000.02, None, 0.05, 5) == 999.50   # 999.519990 -> down
    # Never past a LIMIT order's own price.
    assert fill("BUY", "LIMIT", 1499.90, 1500.0, 0.05, 5) == 1500.0
    assert fill("SELL", "LIMIT", 1600.10, 1600.0, 0.05, 5) == 1600.0
    # 0 bps: the observed price itself.
    assert fill("BUY", "MARKET", 1502.35, None, 0.05, 0) == 1502.35
    monkeypatch.undo()
    assert config.PAPER_SLIPPAGE_BPS == 5.0  # the documented default


def test_4_slippage_is_applied_to_real_paper_fills_and_the_journal(env, market_open, monkeypatch):
    from backend import config
    monkeypatch.setattr(config, "PAPER_SLIPPAGE_BPS", 5.0)
    buy_id = _open_position(env, fill_price=1500.0)
    assert ticket_row(env.db, buy_id)["filled_price"] == 1500.75

    _ltp(env, 1600.0)
    exit_id, _ = _exit(env, buy_id)
    _cycle(env)
    assert ticket_row(env.db, exit_id)["filled_price"] == 1599.20
    [trade] = _closed_trades_for(env, buy_id)
    assert (trade["entry_price"], trade["exit_price"], trade["quantity"], trade["mode"]) == (
        1500.75, 1599.20, 40, "dry_run")
    # Gross P&L — the journal has no cost model (PK-21).
    assert trade["realized_pnl"] == pytest.approx((1599.20 - 1500.75) * 40)


# ======================================================================= LIMIT

def test_5_6_buy_limit_waits_above_its_limit_and_fills_at_or_below(env, market_open):
    _ltp(env, 1495.0)
    buy_id, _ = _approve(env, order_type="LIMIT", price=1490.0)
    order_id = ticket_row(env.db, buy_id)["kite_order_id"]
    assert _order(env, order_id)["price"] == 1490.0

    for price in (1495.0, 1490.05):
        _ltp(env, price)
        _cycle(env)
        assert _order(env, order_id)["status"] == "OPEN" and _state(env, buy_id) == "ENTRY_OPEN"

    _ltp(env, 1488.0)
    _cycle(env)
    assert _order(env, order_id)["average_price"] == 1488.0   # the observed price, at or below the limit
    assert ticket_row(env.db, buy_id)["filled_price"] == 1488.0 and _state(env, buy_id) == "OPEN"


def test_6_buy_limit_fills_exactly_at_its_limit(env, market_open):
    order_id = _place(env, order_type="LIMIT", price=1490.0)
    _ltp(env, 1490.0)
    _evaluate(env)
    assert (_order(env, order_id)["status"], _order(env, order_id)["average_price"]) == ("COMPLETE", 1490.0)


def test_7_8_sell_limit_waits_below_its_limit_and_fills_at_or_above(env, market_open):
    order_id = _place(env, transaction_type="SELL", order_type="LIMIT", price=1600.0)
    for price in (1590.0, 1599.95):
        _ltp(env, price)
        _evaluate(env)
        assert _order(env, order_id)["status"] == "OPEN" and _order(env, order_id)["filled_quantity"] == 0
    _ltp(env, 1600.0)
    _evaluate(env)
    assert (_order(env, order_id)["status"], _order(env, order_id)["average_price"]) == ("COMPLETE", 1600.0)

    above = _place(env, transaction_type="SELL", order_type="LIMIT", price=1600.0)
    _ltp(env, 1604.5)
    _evaluate(env)
    assert _order(env, above)["average_price"] == 1604.5


def test_sell_limit_exit_ticket_closes_only_at_its_limit(env, market_open):
    buy_id = _open_position(env)
    exit_id = _insert_ticket(env.db, source="strategy", transaction_type="SELL", quantity=40, entry_price=1600.0,
                             entry_order_type="LIMIT", status="pending_confirmation", mode="dry_run",
                             closes_ticket_id=buy_id, symbol=SYMBOL, instrument_token=408065,
                             rule_checklist=json.dumps({"time_exit": {"pass": True}}))
    r = env.client.post(f"/api/tickets/{exit_id}/confirm")
    assert r.status_code == 200, r.text
    _ltp(env, 1590.0)
    _cycle(env)
    assert _state(env, buy_id) == "EXITING"
    _ltp(env, 1600.0)
    _cycle(env)
    assert _state(env, buy_id) == "CLOSED" and ticket_row(env.db, exit_id)["filled_price"] == 1600.0


# ======================================================================= PARTIAL

def test_9_10_progressive_entry_fills_10_25_40(env, market_open):
    from backend import paper_broker
    buy_id, _ = _approve(env)
    row = ticket_row(env.db, buy_id)
    paper_broker.set_fill_plan(row["kite_order_id"], [10, 25, 40])
    gtt_id = row["kite_gtt_id"]
    assert _leg_quantities(env, gtt_id) == [40, 40]  # placed with the order, for the requested quantity

    for filled, state in ((10, "ENTRY_PARTIAL"), (25, "ENTRY_PARTIAL"), (40, "OPEN")):
        _cycle(env)
        p = _position(env, buy_id)
        assert (p["lifecycle_state"], p["filled_quantity"], p["open_quantity"]) == (state, filled, filled)
        # PK-13: the paper GTT covers the shares actually held.
        assert _leg_quantities(env, gtt_id) == [filled, filled]
        assert p["protection_status"] == "PROTECTED"
    assert ticket_row(env.db, buy_id)["entry_fill_status"] == "filled"
    assert _live_mutations(env) == []


def test_11_12_progressive_exit_leaves_a_25_share_residual_protected(env, market_open):
    from backend import paper_broker
    buy_id = _open_position(env)
    gtt_id = ticket_row(env.db, buy_id)["kite_gtt_id"]
    exit_id, _ = _exit(env, buy_id)
    exit_order = ticket_row(env.db, exit_id)["kite_order_id"]
    paper_broker.set_fill_plan(exit_order, [15])

    _cycle(env)  # 15 of 40 sold, the rest still working
    p = _position(env, buy_id)
    assert (p["lifecycle_state"], p["open_quantity"], p["exit_filled_quantity"]) == ("EXIT_PARTIAL", 25, 15)
    assert _leg_quantities(env, gtt_id) == [25, 25]
    assert _closed_trades_for(env, buy_id) == []

    # The rest of the exit is cancelled: 15 sold, 25 remain OPEN and protected.
    assert env.client.post(f"/api/orders/{exit_order}/cancel").status_code == 200
    _cycle(env)
    p = _position(env, buy_id)
    assert (p["lifecycle_state"], p["open_quantity"], p["active_exit"]) == ("EXIT_PARTIAL", 25, None)
    assert ticket_row(env.db, exit_id)["entry_fill_status"] == "partial"
    assert _leg_quantities(env, gtt_id) == [25, 25] and p["protection_status"] == "PROTECTED"
    assert _closed_trades_for(env, buy_id) == []

    second_id, _ = _exit(env, buy_id)
    assert ticket_row(env.db, second_id)["quantity"] == 25
    _cycle(env)
    assert _state(env, buy_id) == "CLOSED"
    assert len(_closed_trades_for(env, buy_id)) == 1
    assert _gtt(env, gtt_id)["status"] == "deleted"


# ======================================================================= GTT

def test_13_paper_protection_is_an_oco_gtt_sized_to_the_order(env, market_open):
    buy_id, _ = _approve(env)
    gtt = _gtt(env, ticket_row(env.db, buy_id)["kite_gtt_id"])
    assert (gtt["trigger_type"], gtt["status"], gtt["trigger_values"]) == ("two-leg", "active", [STOP, TARGET])
    assert [(leg["transaction_type"], leg["quantity"], leg["order_type"]) for leg in gtt["legs"]] == [
        ("SELL", 40, "LIMIT"), ("SELL", 40, "LIMIT")]
    assert gtt["legs"][0]["price"] == 1442.75   # the stop leg, buffered below the trigger
    assert gtt["legs"][1]["price"] == TARGET


def test_14_16_17_stop_trigger_places_an_exit_order_and_only_its_fill_closes(env, market_open):
    buy_id = _open_position(env)
    gtt_id = ticket_row(env.db, buy_id)["kite_gtt_id"]

    _ltp(env, 1449.0)
    _cycle(env)  # the stop fires: a SELL LIMIT is placed, nothing is sold yet
    gtt = _gtt(env, gtt_id)
    order = _gtt_order(env, gtt_id)
    assert gtt["status"] == "triggered"
    assert gtt["legs"][0]["result"]["order_result"]["order_id"] == order["paper_order_id"]
    assert (order["transaction_type"], order["order_type"], order["price"], order["quantity"], order["status"]) == (
        "SELL", "LIMIT", 1442.75, 40, "OPEN")
    assert order["tag"] is None   # like Kite: a GTT's order carries no app tag
    # 16: a trigger alone never closes the position.
    assert _state(env, buy_id) == "OPEN" and ticket_row(env.db, buy_id)["closed_at"] is None
    assert _closed_trades_for(env, buy_id) == []

    _cycle(env)  # 17: the order fills at the observed price; the watcher closes it
    assert _state(env, buy_id) == "CLOSED"
    [trade] = _closed_trades_for(env, buy_id)
    assert (trade["exit_price"], trade["quantity"], trade["mode"]) == (1449.0, 40, "dry_run")
    [sell] = _rows(env, "SELECT * FROM trade_tickets WHERE closes_ticket_id = ?", buy_id)
    assert (sell["mode"], sell["entry_fill_status"], sell["filled_quantity"]) == ("dry_run", "filled", 40)
    _cycle(env)
    assert len(_closed_trades_for(env, buy_id)) == 1
    assert _live_mutations(env) == []


def test_15_target_leg_of_the_oco_places_a_target_exit(env, market_open):
    buy_id = _open_position(env)
    gtt_id = ticket_row(env.db, buy_id)["kite_gtt_id"]
    _ltp(env, 1601.0)
    _cycle(env)
    order = _gtt_order(env, gtt_id)
    assert (order["price"], order["status"]) == (TARGET, "OPEN") and _state(env, buy_id) == "OPEN"
    _cycle(env)
    [trade] = _closed_trades_for(env, buy_id)
    assert trade["exit_price"] == 1601.0 and _state(env, buy_id) == "CLOSED"


def test_gap_through_the_stop_limit_leaves_the_exit_unfilled(env, market_open):
    buy_id = _open_position(env)
    gtt_id = ticket_row(env.db, buy_id)["kite_gtt_id"]
    _ltp(env, 1400.0)   # gaps through both the 1450 trigger and the 1442.75 limit
    _cycle(env)
    _cycle(env)
    assert _gtt_order(env, gtt_id)["status"] == "OPEN" and _state(env, buy_id) == "OPEN"
    assert any("EXIT NOT FILLED" in (b.get("message") or "") for b in env.broadcasts)


def test_18_partial_gtt_exit_leaves_a_residual(env, market_open):
    from backend import paper_broker
    buy_id = _open_position(env)
    gtt_id = ticket_row(env.db, buy_id)["kite_gtt_id"]
    _ltp(env, 1449.0)
    _cycle(env)
    paper_broker.set_fill_plan(_gtt_order(env, gtt_id)["paper_order_id"], [15])

    _cycle(env)
    p = _position(env, buy_id)
    assert (p["lifecycle_state"], p["open_quantity"]) == ("EXIT_PARTIAL", 25)
    assert _closed_trades_for(env, buy_id) == []

    _cycle(env)
    assert _state(env, buy_id) == "CLOSED" and len(_closed_trades_for(env, buy_id)) == 1


# ======================================================================= TRAILING

def test_19_trailing_ratchets_the_paper_gtt(env, market_open):
    buy_id = _open_position(env, trailing_enabled=True, trailing_trigger="after_target_1", trailing_distance=20.0)
    gtt_id = ticket_row(env.db, buy_id)["kite_gtt_id"]
    gtt = _gtt(env, gtt_id)
    assert (gtt["trigger_type"], gtt["trigger_values"], _leg_quantities(env, gtt_id)) == ("single", [STOP], [40])

    _ltp(env, 1605.0)
    _cycle(env)   # target touched: trailing activates
    assert ticket_row(env.db, buy_id)["trailing_active"] == 1
    _ltp(env, 1620.0)
    _cycle(env)   # ratchet: stop 1450 -> 1600
    gtt = _gtt(env, gtt_id)
    assert gtt["trigger_values"] == [1600.0] and gtt["legs"][0]["price"] == 1592.0
    assert gtt["legs"][0]["quantity"] == 40 and gtt["status"] == "active"
    assert ticket_row(env.db, buy_id)["current_stop_loss"] == 1600.0
    assert env.kite.calls_to("modify_gtt") == [] and _live_mutations(env) == []

    _ltp(env, 1599.0)
    _cycle(env)
    _cycle(env)
    [trade] = _closed_trades_for(env, buy_id)
    assert trade["exit_price"] == 1599.0


# ======================================================================= CANCEL

def test_20_cancel_of_an_open_paper_entry_is_resolved_by_the_monitor(env, market_open):
    _ltp(env, 1495.0)
    buy_id, _ = _approve(env, order_type="LIMIT", price=1490.0)
    row = ticket_row(env.db, buy_id)
    gtt_id = row["kite_gtt_id"]
    assert "CANCEL_ORDER" in _position(env, buy_id)["allowed_actions"]

    r = env.client.post(f"/api/orders/{row['kite_order_id']}/cancel")
    assert r.status_code == 200 and r.json()["data"]["linked_ticket_id"] == buy_id
    # PK-9: the request changes no ticket state by itself.
    assert ticket_row(env.db, buy_id)["status"] == "executed" and _state(env, buy_id) == "ENTRY_OPEN"
    assert _order(env, row["kite_order_id"])["status"] == "CANCELLED"

    _cycle(env)
    row = ticket_row(env.db, buy_id)
    assert (row["status"], row["entry_fill_status"], row["kite_gtt_id"]) == ("cancelled", "cancelled", None)
    assert _gtt(env, gtt_id)["status"] == "deleted"
    assert _state(env, buy_id) == "CANCELLED"
    assert env.kite.calls_to("cancel_order") == [] and _live_mutations(env) == []


def test_21_fill_wins_the_race_against_a_cancel(env, market_open):
    buy_id, _ = _approve(env)
    order_id = ticket_row(env.db, buy_id)["kite_order_id"]
    _ltp(env, 1501.0)
    _evaluate(env)   # the paper exchange fills first

    r = env.client.post(f"/api/orders/{order_id}/cancel")
    assert r.status_code == 502 and "COMPLETE" in r.json()["detail"]["message"]
    _cycle(env)
    assert ticket_row(env.db, buy_id)["entry_fill_status"] == "filled" and _state(env, buy_id) == "OPEN"


def test_21_cancel_after_a_partial_fill_keeps_the_filled_shares(env, market_open):
    from backend import paper_broker
    buy_id, _ = _approve(env)
    row = ticket_row(env.db, buy_id)
    paper_broker.set_fill_plan(row["kite_order_id"], [10])
    _evaluate(env)
    assert env.client.post(f"/api/orders/{row['kite_order_id']}/cancel").status_code == 200
    _cycle(env)
    p = _position(env, buy_id)
    assert (p["lifecycle_state"], p["open_quantity"], p["entry_partial"]) == ("OPEN", 10, True)
    assert ticket_row(env.db, buy_id)["entry_fill_status"] == "partial"
    assert _leg_quantities(env, row["kite_gtt_id"]) == [10, 10]


def test_cancel_of_an_unknown_or_finished_paper_order_changes_nothing(env, market_open):
    r = env.client.post("/api/orders/PAPER-O-999/cancel")
    assert r.status_code == 502
    assert env.kite.calls_to("cancel_order") == []


# ======================================================================= RESTART

def _restart(env):
    """What a process restart leaves: the database (migrations re-run) and
    nothing else — the paper broker holds no process state."""
    from backend import paper_broker
    env.db.init_db()
    paper_broker.reset_test_hooks()


def test_22_open_paper_order_survives_a_restart_and_fills_once(env, market_open):
    _ltp(env, 1495.0)
    buy_id, _ = _approve(env, order_type="LIMIT", price=1490.0)
    _cycle(env)
    _restart(env)
    _restart(env)

    _ltp(env, 1489.0)
    _cycle(env)
    _cycle(env)
    assert len(_paper_orders(env)) == 1
    row = ticket_row(env.db, buy_id)
    assert (row["entry_fill_status"], row["filled_quantity"], row["filled_price"]) == ("filled", 40, 1489.0)


def test_23_exiting_survives_a_restart_and_closes_once(env, market_open):
    buy_id = _open_position(env)
    exit_id, _ = _exit(env, buy_id)
    env.kite.raise_on_next("ltp", requests.ConnectionError("no quotes"))
    _cycle(env)
    assert _state(env, buy_id) == "EXITING"

    _restart(env)
    _cycle(env)
    _cycle(env)
    assert _state(env, buy_id) == "CLOSED"
    assert len(_closed_trades_for(env, buy_id)) == 1
    assert len([o for o in _paper_orders(env) if o["transaction_type"] == "SELL"]) == 1


def test_24_paper_gtt_survives_a_restart_and_still_protects(env, market_open):
    buy_id = _open_position(env)
    _restart(env)
    _ltp(env, 1449.0)
    _cycle(env)
    _cycle(env)
    assert _state(env, buy_id) == "CLOSED" and len(_closed_trades_for(env, buy_id)) == 1


def test_fill_plan_survives_a_restart(env, market_open):
    from backend import paper_broker
    buy_id, _ = _approve(env)
    paper_broker.set_fill_plan(ticket_row(env.db, buy_id)["kite_order_id"], [10, 25, 40])
    _cycle(env)
    _restart(env)
    _cycle(env)
    assert _position(env, buy_id)["filled_quantity"] == 25


def test_migration_is_idempotent_and_keeps_rows(env):
    def schema():
        return [(r["name"], r["sql"]) for r in _rows(env, "SELECT name, sql FROM sqlite_master ORDER BY name")]
    # One re-run first: a fresh database's second init_db re-creates a
    # pre-PK-20 dashboard index (unrelated to the paper tables); from then
    # on the schema is stable.
    env.db.init_db()
    before = schema()
    tickets = len(_rows(env, "SELECT id FROM trade_tickets"))
    env.db.init_db()
    env.db.init_db()
    assert schema() == before
    assert {"paper_orders", "paper_gtts", "idx_paper_orders_tag"} <= {name for name, _ in before}
    assert len(_rows(env, "SELECT id FROM trade_tickets")) == tickets


# ======================================================================= ISOLATION

def test_25_the_whole_dry_run_lifecycle_never_calls_live_order_apis(env, market_open):
    from backend import paper_broker
    buy_id, _ = _approve(env)
    paper_broker.set_fill_plan(ticket_row(env.db, buy_id)["kite_order_id"], [10, 40])
    _cycle(env)
    _cycle(env)
    exit_id, _ = _exit(env, buy_id)
    _cycle(env)
    assert _state(env, buy_id) == "CLOSED"
    assert _live_mutations(env) == []
    assert env.kite.order_book == {} and env.kite.gtt_book == {}


def test_25_mode_guard_refuses_every_crossover(env):
    from backend import execution, paper_broker
    kite = FakeKite(ltp={KEY: PRICE})
    base = {"id": 1, "created_at": "2026-09-01 04:00:00", "order_tag": None, "symbol": SYMBOL, "exchange": "NSE",
            "quantity": 5, "entry_price": PRICE, "entry_order_type": "MARKET", "stop_loss": STOP,
            "target_1": TARGET, "trailing_enabled": 0, "transaction_type": "BUY", "product_type": "CNC",
            "closes_ticket_id": None, "source": "manual"}
    # A dry-run ticket handed the live broker: refused, nothing sent.
    result = execution.execute_ticket(kite, {**base, "mode": "dry_run"}, entry_variety="regular")
    assert (result.success, result.status) == (False, "failed") and "Refused before any broker call" in result.message
    assert kite.calls == []
    # A live ticket handed the paper broker: refused, nothing written.
    conn = env.db.get_connection()
    try:
        paper = paper_broker.PaperBroker(conn, account_id(env.db), market_data=kite)
        result = execution.execute_ticket(paper, {**base, "mode": "live"}, entry_variety="regular")
        assert result.status == "failed"
        assert conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0
        with pytest.raises(paper_broker.ModeIsolationError):
            paper_broker.check_mode(kite, "dry_run")
        with pytest.raises(paper_broker.ModeIsolationError):
            paper_broker.check_mode(paper, "live")
        # Nothing the paper broker does not simulate falls through to Kite.
        with pytest.raises(paper_broker.PaperBrokerUnsupported):
            paper.margins()
        with pytest.raises(InputException):
            paper.cancel_order(variety="regular", order_id="250000000001")   # a Kite id
    finally:
        conn.close()


def test_26_live_paths_never_see_paper_orders(env, market_open):
    from backend import order_monitor, reconciliation
    _ltp(env, 1495.0)
    waiting, _ = _approve(env, order_type="LIMIT", price=1490.0)
    filled = _open_position(env)
    env.kite.calls.clear()

    # The live monitor follows live tickets only.
    assert order_monitor.check_pending_orders(env.kite) == {"checked": 0, "updated": 0}
    conn = env.db.get_connection()
    try:
        assert order_monitor.pending_tickets(conn, account_id(env.db)) == []
        assert [t["id"] for t in order_monitor.pending_tickets(conn, account_id(env.db), "dry_run")] == [waiting]
    finally:
        conn.close()
    # Reconciliation never asks Kite about a paper order.
    reconciliation._run_cycle()
    assert not any(str(c.get("order_id", "")).startswith("PAPER") for c in env.kite.calls_to("order_history"))
    # The Orders & GTT pages list Kite's books only.
    assert not any(o["order_id"].startswith("PAPER") for o in env.client.get("/api/orders").json()["data"])
    assert not any(str(g["id"]).startswith("PAPER") for g in env.client.get("/api/gtts").json()["data"])
    assert ticket_row(env.db, filled)["kite_gtt_id"].startswith("PAPER-G-")
    assert _live_mutations(env) == []


def test_paper_activity_never_counts_against_live_account_controls(env, market_open):
    from backend import account_safety
    buy_id = _open_position(env)
    _ltp(env, 1400.0)
    _exit(env, buy_id)
    _cycle(env)
    assert _state(env, buy_id) == "CLOSED"
    conn = env.db.get_connection()
    try:
        status = account_safety.status(conn, account_id(env.db))
    finally:
        conn.close()
    assert status["entries_today"] == 0 and status["realized_live_pnl_today"] == 0
    assert status["breaker_open"] is False and status["breaker_recent_failures"] == 0


def test_trading_lock_still_blocks_a_dry_run_approval(env, market_open):
    set_mode(env.db, "dry_run")
    t = env.client.post("/api/tickets", json=_manual_ticket(symbol=SYMBOL, quantity=40, entry_price=PRICE,
                                                             stop_loss=STOP, target_1=TARGET)).json()["data"]
    assert env.client.post("/api/account-safety/trading-lock", json={"locked": True, "reason": "drill"}).status_code == 200
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json={"expected_mode": "dry_run"})
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "TRADING_LOCKED"
    assert _paper_orders(env) == []


# ======================================================================= LIFECYCLE

def test_27_31_positions_view_walks_the_live_lifecycle(env, market_open):
    from backend import paper_broker
    buy_id, _ = _approve(env)
    p = _position(env, buy_id)
    assert (p["lifecycle_state"], p["mode"], p["paper"]) == ("ENTRY_OPEN", "dry_run", True)          # 27
    assert p["kite_order_id"].startswith("PAPER-O-")

    paper_broker.set_fill_plan(p["kite_order_id"], [15])
    _cycle(env)
    assert _state(env, buy_id) == "ENTRY_PARTIAL"                                                        # 31
    _cycle(env)
    p = _position(env, buy_id)
    assert (p["lifecycle_state"], p["protection_status"], p["open_quantity"]) == ("OPEN", "PROTECTED", 40)  # 28
    assert "EXIT" in p["allowed_actions"] and "EMERGENCY_EXIT" not in p["allowed_actions"]

    exit_id, _ = _exit(env, buy_id)
    env.kite.raise_on_next("ltp", requests.ConnectionError("no quotes"))
    _cycle(env)
    p = _position(env, buy_id)
    assert p["lifecycle_state"] == "EXITING" and p["active_exit"]["kite_order_id"].startswith("PAPER-O-")  # 29
    assert "CANCEL_ORDER" in p["allowed_actions"]

    _cycle(env)
    p = _position(env, buy_id)
    assert (p["lifecycle_state"], p["is_closed"]) == ("CLOSED", True)                                   # 30
    assert p["closed_trade"]["quantity"] == 40


def test_legacy_dry_run_position_is_not_protection_tracked_but_exits_through_paper(env, market_open):
    legacy = _insert_ticket(env.db, mode="dry_run", symbol=SYMBOL, instrument_token=408065, quantity=5,
                            entry_price=PRICE, stop_loss=STOP, target_1=TARGET, entry_fill_status="filled",
                            filled_quantity=5, filled_price=PRICE, kite_gtt_id=None,
                            executed_at="2026-09-01 04:00:00")
    p = _position(env, legacy)
    assert (p["lifecycle_state"], p["protection_status"], p["paper"]) == ("OPEN", "NOT_APPLICABLE", False)
    _exit(env, legacy)
    assert _state(env, legacy) == "EXITING"
    _cycle(env)
    assert _state(env, legacy) == "CLOSED" and len(_closed_trades_for(env, legacy)) == 1


# ======================================================================= SESSIONS / AMO

def _pin_paper_clock(monkeypatch, at):
    from backend import paper_broker
    monkeypatch.setattr(paper_broker, "_now", lambda: at)


def test_amo_paper_order_waits_for_the_next_session(env, monkeypatch):
    from backend import market, order_monitor
    tuesday_evening = datetime(2026, 9, 29, 18, 0, tzinfo=IST)
    _pin_paper_clock(monkeypatch, tuesday_evening)
    monkeypatch.setattr(market, "is_market_open", lambda at=None: False)
    set_mode(env.db, "dry_run")
    _ltp(env, PRICE)
    body = _manual_ticket(symbol=SYMBOL, quantity=40, entry_price=PRICE, entry_order_type="MARKET",
                          stop_loss=STOP, target_1=TARGET)
    tid = env.client.post("/api/tickets", json=body).json()["data"]["id"]

    r = env.client.post(f"/api/tickets/{tid}/confirm", json={"expected_mode": "dry_run"})
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "AMO_CONFIRMATION_REQUIRED"
    r = env.client.post(f"/api/tickets/{tid}/confirm", json={"expected_mode": "dry_run", "allow_amo": True})
    assert r.status_code == 200 and r.json()["data"]["order_variety"] == "amo"
    order = _order(env, ticket_row(env.db, tid)["kite_order_id"])
    assert (order["status"], order["variety"], order["session_date"]) == ("AMO REQ RECEIVED", "amo", "2026-09-30")

    monkeypatch.setattr(market, "is_market_open", _REAL_IS_MARKET_OPEN)
    _evaluate(env, now=datetime(2026, 9, 29, 20, 0, tzinfo=IST))    # overnight: nothing
    assert _order(env, order["paper_order_id"])["status"] == "AMO REQ RECEIVED"
    _evaluate(env, now=datetime(2026, 9, 30, 9, 16, tzinfo=IST))    # the session opens: activated and filled
    assert _order(env, order["paper_order_id"])["status"] == "COMPLETE"
    order_monitor.check_pending_orders(mode="dry_run")
    assert ticket_row(env.db, tid)["entry_fill_status"] == "filled"


def test_session_rules_for_paper_placement(env, monkeypatch):
    from backend import market
    monkeypatch.setattr(market, "is_market_open", lambda at=None: False)
    with pytest.raises(InputException, match="markets are closed"):
        _place(env)
    monkeypatch.setattr(market, "is_market_open", lambda at=None: True)
    with pytest.raises(InputException, match="After-Market Orders are not accepted"):
        _place(env, variety="amo")


def test_unfilled_regular_order_expires_at_the_end_of_its_session(env, market_open, monkeypatch):
    from backend import market
    _ltp(env, 1495.0)
    buy_id, _ = _approve(env, order_type="LIMIT", price=1490.0)
    order_id = ticket_row(env.db, buy_id)["kite_order_id"]
    monkeypatch.setattr(market, "is_market_open", _REAL_IS_MARKET_OPEN)
    summary = _evaluate(env, now=market.now_ist() + timedelta(days=1))
    assert summary["expired"] == 1
    assert _order(env, order_id)["status"] == "CANCELLED"
    assert "DAY validity" in _order(env, order_id)["status_message"]
    from backend import order_monitor
    order_monitor.check_pending_orders(mode="dry_run")
    assert ticket_row(env.db, buy_id)["status"] == "cancelled"


def test_market_closed_nothing_fills_or_triggers(env, market_open, monkeypatch):
    from backend import market
    buy_id = _open_position(env)
    _approve(env, quantity=5)
    monkeypatch.setattr(market, "is_market_open", lambda at=None: False)
    _ltp(env, 1300.0)
    summary = _evaluate(env)
    assert (summary["filled"], summary["triggered"], summary["ltp_calls"]) == (0, 0, 0)
    assert _state(env, buy_id) == "OPEN"


# ======================================================================= UNKNOWN (PK-2 hook)

def test_accepted_but_response_lost_is_recovered_by_tag_never_resubmitted(env, market_open):
    from backend import paper_broker
    paper_broker.accept_then_raise("place_order", requests.ConnectionError("response lost"))
    set_mode(env.db, "dry_run")
    _ltp(env, PRICE)
    body = _manual_ticket(symbol=SYMBOL, quantity=40, entry_price=PRICE, entry_order_type="MARKET",
                          stop_loss=STOP, target_1=TARGET)
    tid = env.client.post("/api/tickets", json=body).json()["data"]["id"]
    r = env.client.post(f"/api/tickets/{tid}/confirm", json={"expected_mode": "dry_run"})
    assert r.status_code == 200 and "UNKNOWN" in r.json()["data"]["execution_message"]
    row = ticket_row(env.db, tid)
    assert (row["status"], row["submission_state"], row["kite_order_id"]) == ("confirmed", "unknown", None)
    [order] = _paper_orders(env)
    assert order["tag"] == row["order_tag"]
    assert _state(env, tid) == "SUBMISSION_UNKNOWN"

    _cycle(env)
    row = ticket_row(env.db, tid)
    assert (row["status"], row["submission_state"], row["kite_order_id"]) == (
        "executed", "adopted", order["paper_order_id"])
    assert row["entry_fill_status"] == "filled"
    assert len(_paper_orders(env)) == 1   # nothing resubmitted


def test_duplicate_tag_is_never_a_second_order(env, market_open):
    _place(env, tag="kt1xabc")
    with pytest.raises(GeneralException):
        _place(env, tag="kt1xabc")
    assert len(_paper_orders(env)) == 1


# ======================================================================= VALIDATION / PERFORMANCE

def test_unsupported_order_types_are_refused(env, market_open):
    for order_type in ("SL", "SL-M"):
        with pytest.raises(InputException, match="not supported"):
            _place(env, order_type=order_type, price=1500.0, trigger_price=1490.0)
    with pytest.raises(InputException):
        _place(env, order_type="LIMIT")   # no price
    assert _paper_orders(env) == []


def test_no_market_data_call_when_nothing_is_working(env, market_open):
    env.kite.calls.clear()
    summary = _evaluate(env)
    assert summary["ltp_calls"] == 0 and env.kite.calls_to("ltp") == []


def test_one_batched_ltp_call_for_many_orders(env, market_open):
    for symbol in ("INFY", "TCS", "SUNPHARMA", "INFY"):
        _place(env, tradingsymbol=symbol, order_type="LIMIT", price=1.0)
    env.kite.calls.clear()
    _evaluate(env)
    [call] = env.kite.calls_to("ltp")
    assert call["keys"] == ["NSE:INFY", "NSE:SUNPHARMA", "NSE:TCS"]
