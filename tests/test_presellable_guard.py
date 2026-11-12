"""Interim LIVE-entry guards, behind the hard gate (lifted in-test by conftest `env`).

1. Regular MARKET only: a LIVE LIMIT or AMO entry is refused server-side. Secondary only: a MARKET
   order can still stay pending or fill partly, so its GTT can still be live before the fill
   (test_10 shows exactly that).
2. No pre-existing sellable quantity: a protective GTT is a sell on the INSTRUMENT, acting on the
   account's demat holdings, not on the entry. A LIVE entry is refused if any quantity of the
   instrument already exists anywhere in the account (holdings, T1, collateral, positions, open
   orders, active GTTs, other live app positions), whoever owns it, and fails closed when that
   cannot be established from fresh, consistent broker data.

These are interim measures, not the fix (docs/PROPOSAL_EXECUTION_GTT_AFTER_FILL.md).
FakeKite and scratch databases only.
"""

import pytest
from kiteconnect.exceptions import NetworkException

from backend import live_entry_gate
from conftest import account_id, approval_body, restore_interim_guards, set_mode, ticket_row
from test_baseline_api import _insert_ticket, _manual_ticket


@pytest.fixture(autouse=True)
def _real_market_rule(env, monkeypatch):
    """conftest `env` lifts both interim guards for the rest of the suite; here they are real."""
    restore_interim_guards(monkeypatch)


def _create(env, **overrides) -> dict:
    body = dict(entry_order_type="MARKET")
    body.update(overrides)
    r = env.client.post("/api/tickets", json=_manual_ticket(**body))
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _confirm(env, ticket_id: int, **body):
    payload = approval_body(env.db, ticket_id)
    payload.update(body)
    return env.client.post(f"/api/tickets/{ticket_id}/confirm", json=payload)


def _codes(r) -> set[str]:
    detail = r.json()["detail"]
    return {f["code"] for f in detail.get("failures", [])} | {detail["error_code"]}


def _nothing_sent(env) -> None:
    assert env.kite.calls_to("place_order") == [] and env.kite.calls_to("place_gtt") == []


def _refused(env, code: str, status: int = 409, **ticket) -> dict:
    set_mode(env.db, "live")
    t = _create(env, **ticket)
    r = _confirm(env, t["id"])
    assert r.status_code == status, r.text
    assert code in _codes(r), r.text
    _nothing_sent(env)
    row = ticket_row(env.db, t["id"])
    assert row["status"] == "pending_confirmation", row  # never claimed for good, never rejected
    return r.json()["detail"]


# ---------------------------------------------------------------- regular MARKET only

def test_01_live_limit_entry_is_refused(env, market_open):
    detail = _refused(env, "LIVE_ENTRY_ORDER_TYPE_UNSUPPORTED", entry_order_type="LIMIT")
    assert "MARKET" in detail["message"]


def test_02_live_amo_entry_is_refused_even_with_consent(env, monkeypatch):
    from backend import market
    monkeypatch.setattr(market, "is_market_open", lambda at=None: False)
    set_mode(env.db, "live")
    t = _create(env)
    r = _confirm(env, t["id"], allow_amo=True)
    assert r.status_code == 409 and "LIVE_AMO_UNSUPPORTED" in _codes(r), r.text
    _nothing_sent(env)


def test_03_dry_run_limit_and_amo_entries_are_unaffected(env, monkeypatch):
    from backend import market
    set_mode(env.db, "dry_run")
    monkeypatch.setattr(market, "is_market_open", lambda at=None: True)
    t = _create(env, entry_order_type="LIMIT")
    assert _confirm(env, t["id"]).status_code == 200
    monkeypatch.setattr(market, "is_market_open", lambda at=None: False)
    t = _create(env, symbol="INFY", entry_price=1500.0, stop_loss=1450.0, target_1=1650.0)
    assert _confirm(env, t["id"], allow_amo=True).status_code == 200
    _nothing_sent(env)


def test_04_a_clean_account_market_entry_passes_every_interim_guard(env, market_open):
    set_mode(env.db, "live")
    t = _create(env)
    r = _confirm(env, t["id"])
    assert r.status_code == 200, r.text
    orders = env.kite.calls_to("place_order")
    assert len(orders) == 1 and orders[0]["order_type"] == "MARKET" and orders[0]["variety"] == "regular"


# ---------------------------------------------------------------- pre-existing sellable quantity

def test_05_settled_holding_of_the_instrument_refuses(env, market_open):
    env.kite.set_holding("TCS", 3)
    detail = _refused(env, "PRE_EXISTING_SELLABLE_QUANTITY")
    assert "quantity=3" in detail["message"]


def test_06_t1_quantity_alone_refuses(env, market_open):
    env.kite.set_holding("TCS", 0, t1_quantity=2)
    _refused(env, "PRE_EXISTING_SELLABLE_QUANTITY")


def test_07_pledged_collateral_quantity_alone_refuses(env, market_open):
    env.kite.set_holding("TCS", 1)
    holding = next(h for h in env.kite._holdings if h["tradingsymbol"] == "TCS")
    holding.update(quantity=0, collateral_quantity=4)
    _refused(env, "PRE_EXISTING_SELLABLE_QUANTITY")


def test_08_holding_listed_on_another_exchange_or_under_another_symbol_with_the_same_isin(env, market_open):
    env.kite._holdings.append({"tradingsymbol": "TCS", "exchange": "BSE", "quantity": 0, "t1_quantity": 0,
                               "isin": "INE467B01029", "average_price": 1.0, "last_price": 1.0})
    env.kite._holdings.append({"tradingsymbol": "TCSLTD", "exchange": "BSE", "quantity": 7, "t1_quantity": 0,
                               "isin": "INE467B01029", "average_price": 1.0, "last_price": 1.0})
    detail = _refused(env, "PRE_EXISTING_SELLABLE_QUANTITY")
    assert "TCSLTD" in detail["message"]


def test_09_an_intraday_or_overnight_position_refuses(env, market_open):
    env.kite.set_position("TCS", 2, product="MIS")
    _refused(env, "PRE_EXISTING_SELLABLE_QUANTITY")


def test_09b_a_short_position_also_refuses(env, market_open):
    env.kite.set_position("TCS", -2, product="MIS")
    _refused(env, "PRE_EXISTING_SELLABLE_QUANTITY")


def test_10_market_only_is_not_proof_a_market_entry_can_stay_pending_with_its_gtt_live(env, market_open):
    """The reason MARKET-only is secondary: accepted, not filled, GTT already live — and never
    labelled protected."""
    set_mode(env.db, "live")
    env.kite.auto_fill = False
    t = _create(env)
    r = _confirm(env, t["id"])
    assert r.status_code == 200, r.text
    lc = env.client.get(f"/api/tickets/{t['id']}").json()["data"]["lifecycle"]
    assert lc["lifecycle_state"] == "ENTRY_OPEN"
    assert lc["protection_status"] == "PENDING_FILL" and lc["protection_alert"] is True
    # ...and while it is pending, a second entry for the same instrument is refused: the open
    # order and the active GTT are both pre-existing quantity for it.
    t2 = _create(env)
    r = _confirm(env, t2["id"])
    assert r.status_code == 409 and "PRE_EXISTING_SELLABLE_QUANTITY" in _codes(r), r.text
    assert len(env.kite.calls_to("place_order")) == 1


def test_11_an_open_order_for_the_instrument_refuses(env, market_open):
    env.kite.add_broker_order("TCS", "BUY", 1, status="OPEN")
    _refused(env, "PRE_EXISTING_SELLABLE_QUANTITY")


def test_12_an_active_gtt_on_the_instrument_refuses_but_an_ended_one_does_not(env, market_open):
    gtt = env.kite.place_gtt(trigger_type="single", tradingsymbol="TCS", exchange="NSE", trigger_values=[2800.0],
                             last_price=3000.0, orders=[{"transaction_type": "SELL", "quantity": 1,
                                                         "order_type": "LIMIT", "product": "CNC", "price": 2790.0}])
    env.kite.calls.clear()
    _refused(env, "PRE_EXISTING_SELLABLE_QUANTITY")
    env.kite.set_gtt_status(gtt["trigger_id"], "deleted")
    t = _create(env, symbol="TCS")
    assert _confirm(env, t["id"]).status_code == 200


def test_13_an_app_owned_live_position_in_the_instrument_also_refuses(env, market_open):
    """Ownership is irrelevant: a new entry's GTT could sell the app position's shares too."""
    _insert_ticket(env.db, entry_fill_status="filled", filled_quantity=5, kite_order_id="250000000777",
                   kite_gtt_id=None, use_gtt=0)
    detail = _refused(env, "PRE_EXISTING_SELLABLE_QUANTITY")
    assert "app ticket #" in detail["message"]


def test_14_another_symbol_held_does_not_refuse(env, market_open):
    env.kite.set_holding("INFY", 50)
    set_mode(env.db, "live")
    t = _create(env)
    assert _confirm(env, t["id"]).status_code == 200


# ---------------------------------------------------------------- fails closed (unit, stub broker)

class _Broker:
    def __init__(self, holdings=None, positions=None, orders=None, gtts=None, fail=None):
        self._h = [] if holdings is None else holdings
        self._p = {"net": [], "day": []} if positions is None else positions
        self._o = [] if orders is None else orders
        self._g = [] if gtts is None else gtts
        self.fail = fail or {}

    def _maybe(self, name, value):
        if name in self.fail:
            raise self.fail[name]
        return value

    def holdings(self):
        return self._maybe("holdings", self._h)

    def positions(self):
        return self._maybe("positions", self._p)

    def orders(self):
        return self._maybe("orders", self._o)

    def get_gtts(self):
        return self._maybe("get_gtts", self._g)


def _check(env, broker, mode="live"):
    tid = _insert_ticket(env.db, status="confirmed", entry_order_type="MARKET")
    conn = env.db.get_connection()
    try:
        row = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (tid,)).fetchone()
        return live_entry_gate.check_no_presellable_quantity(conn, account_id(env.db), row, broker, mode)
    finally:
        conn.close()


def _holding(**fields):
    base = {"tradingsymbol": "TCS", "exchange": "NSE", "quantity": 0, "t1_quantity": 0}
    base.update(fields)
    return base


@pytest.mark.parametrize("broker", [
    _Broker(fail={"holdings": NetworkException("timeout")}),
    _Broker(fail={"positions": NetworkException("timeout")}),
    _Broker(fail={"orders": NetworkException("timeout")}),
    _Broker(fail={"get_gtts": NetworkException("timeout")}),
    _Broker(holdings={"not": "a list"}),
    _Broker(holdings=[_holding(quantity="5")]),
    _Broker(holdings=[_holding(quantity=None)]),
    _Broker(holdings=[{"tradingsymbol": "TCS", "exchange": "NSE"}]),
    _Broker(holdings=[_holding(quantity=-1)]),
    _Broker(holdings=[_holding(quantity=float("nan"))]),
    _Broker(holdings=[_holding(), _holding()]),
    _Broker(positions={"day": []}),
    _Broker(positions={"net": [{"tradingsymbol": "TCS", "exchange": "NSE", "product": "CNC"}], "day": []}),
    _Broker(orders=[{"tradingsymbol": "TCS", "order_id": "1", "status": None}]),
    _Broker(gtts=[{"id": 1, "status": "active"}]),
], ids=["holdings-error", "positions-error", "orders-error", "gtts-error", "holdings-not-list", "qty-string",
        "qty-null", "qty-missing", "qty-negative", "qty-nan", "duplicate-row", "positions-no-net",
        "position-qty-missing", "order-no-status", "gtt-no-condition"])
def test_15_unavailable_or_inconsistent_broker_data_fails_closed(env, broker):
    failures, evidence = _check(env, broker)
    assert [f.code for f in failures] == ["HOLDINGS_CHECK_UNAVAILABLE"]
    assert failures[0].http_status == 503 and "error" in evidence


def test_16_consistent_clean_data_passes_and_the_check_is_live_only(env):
    failures, evidence = _check(env, _Broker(holdings=[_holding(tradingsymbol="INFY", quantity=9)]))
    assert failures == [] and evidence["found"] == []
    failures, evidence = _check(env, _Broker(fail={"holdings": NetworkException("x")}), mode="dry_run")
    assert failures == [] and evidence == {}


def test_17_holdings_read_failure_at_confirm_refuses_with_503(env, market_open, monkeypatch):
    """Integration: the guard's own fresh read fails (the earlier risk revalidation read worked)."""
    set_mode(env.db, "live")
    t = _create(env)
    real = live_entry_gate.check_no_presellable_quantity

    def failing_broker(conn, aid, ticket, kite, mode):
        return real(conn, aid, ticket, _Broker(fail={"holdings": NetworkException("proxy timeout")}), mode)
    monkeypatch.setattr(live_entry_gate, "check_no_presellable_quantity", failing_broker)
    r = _confirm(env, t["id"])
    assert r.status_code == 503 and "HOLDINGS_CHECK_UNAVAILABLE" in _codes(r), r.text
    _nothing_sent(env)
    assert ticket_row(env.db, t["id"])["status"] == "pending_confirmation"
