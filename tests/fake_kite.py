"""In-memory stand-in for kiteconnect.KiteConnect, used by the test suite and
the local demo server so nothing ever reaches the real broker."""

import copy
import itertools
from datetime import date, datetime, timedelta

from kiteconnect.exceptions import InputException

TERMINAL_ORDER_STATUSES = {"COMPLETE", "CANCELLED", "REJECTED"}


class FakeKite:
    VARIETY_REGULAR = "regular"
    VARIETY_AMO = "amo"
    GTT_TYPE_SINGLE = "single"
    GTT_TYPE_OCO = "two-leg"

    def __init__(self, holdings=None, positions=None, margins_net=1_000_000.0, ltp=None, candles=None, instruments=None):
        self.access_token = "fake-access-token"
        self._holdings = holdings if holdings is not None else []
        self._positions = positions if positions is not None else []
        self.margins_net = margins_net
        self.ltp_map = dict(ltp or {})
        self.candles = candles or {}
        self._instruments = instruments or []
        self.order_book: dict[str, dict] = {}
        self.gtt_book: dict[int, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.fail_next: dict[str, Exception] = {}
        self.auto_fill = True
        self._order_ids = itertools.count(250_000_000_001)
        self._gtt_ids = itertools.count(9001)

    # -- helpers for tests -------------------------------------------------
    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))
        exc = self.fail_next.pop(name, None)
        if exc is not None:
            raise exc

    def calls_to(self, name):
        return [kw for n, kw in self.calls if n == name]

    def set_order_state(self, order_id, status, filled_quantity=None, average_price=None, status_message=None):
        order = self.order_book[order_id]
        order["status"] = status
        if filled_quantity is not None:
            order["filled_quantity"] = filled_quantity
        if average_price is not None:
            order["average_price"] = average_price
        order["status_message"] = status_message

    def set_gtt_status(self, gtt_id, status):
        self.gtt_book[gtt_id]["status"] = status

    # -- portfolio ---------------------------------------------------------
    def profile(self):
        return {"user_name": "Demo Trader", "user_id": "DT0001"}

    def holdings(self):
        self._record("holdings")
        return copy.deepcopy(self._holdings)

    def positions(self):
        self._record("positions")
        return {"net": copy.deepcopy(self._positions), "day": []}

    def mf_holdings(self):
        return []

    def margins(self):
        self._record("margins")
        return {"equity": {"net": self.margins_net}}

    def ltp(self, keys):
        if isinstance(keys, str):
            keys = [keys]
        self._record("ltp", keys=list(keys))
        return {k: {"instrument_token": 0, "last_price": self.ltp_map.get(k, 100.0)} for k in keys}

    # -- orders ------------------------------------------------------------
    def place_order(self, variety, exchange, tradingsymbol, transaction_type, quantity, product, order_type,
                    price=None, **kwargs):
        self._record("place_order", variety=variety, exchange=exchange, tradingsymbol=tradingsymbol,
                     transaction_type=transaction_type, quantity=quantity, product=product,
                     order_type=order_type, price=price)
        order_id = str(next(self._order_ids))
        key = f"{exchange}:{tradingsymbol}"
        fill_price = price if price is not None else self.ltp_map.get(key, 100.0)
        filled = self.auto_fill
        pending_status = "AMO REQ RECEIVED" if variety == self.VARIETY_AMO else "OPEN"
        self.order_book[order_id] = {
            "order_id": order_id, "variety": variety, "exchange": exchange, "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type, "quantity": quantity, "product": product,
            "order_type": order_type, "price": price or 0,
            "status": "COMPLETE" if filled else pending_status,
            "filled_quantity": quantity if filled else 0,
            "average_price": fill_price if filled else 0,
            "status_message": None,
            "order_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return order_id

    def orders(self):
        self._record("orders")
        return copy.deepcopy(list(self.order_book.values()))

    def order_history(self, order_id):
        self._record("order_history", order_id=order_id)
        if order_id not in self.order_book:
            raise InputException(f"Order {order_id} not found")
        return [copy.deepcopy(self.order_book[order_id])]

    def trades(self):
        return []

    def cancel_order(self, variety, order_id, **kwargs):
        self._record("cancel_order", variety=variety, order_id=order_id)
        if order_id in self.order_book:
            self.order_book[order_id]["status"] = "CANCELLED"
        return order_id

    # -- GTT ---------------------------------------------------------------
    def place_gtt(self, trigger_type, tradingsymbol, exchange, trigger_values, last_price, orders):
        self._record("place_gtt", trigger_type=trigger_type, tradingsymbol=tradingsymbol, exchange=exchange,
                     trigger_values=list(trigger_values), last_price=last_price, orders=copy.deepcopy(orders))
        gtt_id = next(self._gtt_ids)
        self.gtt_book[gtt_id] = {
            "id": gtt_id, "type": trigger_type, "status": "active",
            "condition": {"exchange": exchange, "tradingsymbol": tradingsymbol,
                          "trigger_values": list(trigger_values), "last_price": last_price},
            "orders": copy.deepcopy(orders),
        }
        return {"trigger_id": gtt_id}

    def modify_gtt(self, trigger_id, trigger_type, tradingsymbol, exchange, trigger_values, last_price, orders):
        self._record("modify_gtt", trigger_id=trigger_id, trigger_type=trigger_type, tradingsymbol=tradingsymbol,
                     trigger_values=list(trigger_values), last_price=last_price, orders=copy.deepcopy(orders))
        gtt = self.gtt_book.get(trigger_id)
        if gtt is None or gtt["status"] != "active":
            raise InputException(f"GTT {trigger_id} is not active")
        gtt["condition"]["trigger_values"] = list(trigger_values)
        gtt["condition"]["last_price"] = last_price
        gtt["orders"] = copy.deepcopy(orders)
        return {"trigger_id": trigger_id}

    def delete_gtt(self, trigger_id):
        self._record("delete_gtt", trigger_id=trigger_id)
        if trigger_id not in self.gtt_book:
            raise InputException(f"GTT {trigger_id} not found")
        self.gtt_book[trigger_id]["status"] = "deleted"
        return {"trigger_id": trigger_id}

    def get_gtts(self):
        self._record("get_gtts")
        return copy.deepcopy(list(self.gtt_book.values()))

    # -- market data -------------------------------------------------------
    def historical_data(self, instrument_token, from_date, to_date, interval, **kwargs):
        self._record("historical_data", instrument_token=instrument_token, from_date=from_date, to_date=to_date)
        rows = self.candles.get(instrument_token, [])
        return [copy.deepcopy(c) for c in rows if from_date <= c["date"].date() <= to_date]

    def instruments(self, exchange=None):
        return copy.deepcopy(self._instruments)


def make_candles(end: date, n: int, start_price: float, daily_drift_pct: float = 0.1, volume: int = 100_000,
                 wiggle_pct: float = 0.6) -> list[dict]:
    """Deterministic weekday-only daily candles ending on `end` (inclusive).
    A small alternating wiggle keeps ATR/RSI well-defined without randomness."""
    days = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()

    candles = []
    price = start_price
    for i, d in enumerate(days):
        price *= 1 + daily_drift_pct / 100
        wiggle = price * wiggle_pct / 100 * (1 if i % 2 == 0 else -1)
        close = price + wiggle
        open_ = price - wiggle / 2
        high = max(open_, close) * 1.004
        low = min(open_, close) * 0.996
        candles.append({"date": datetime(d.year, d.month, d.day), "open": round(open_, 2), "high": round(high, 2),
                        "low": round(low, 2), "close": round(close, 2), "volume": volume})
    return candles
