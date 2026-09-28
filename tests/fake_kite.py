"""In-memory stand-in for kiteconnect.KiteConnect, used by the test suite and
the local demo server so nothing ever reaches the real broker."""

import copy
import itertools
from datetime import date, datetime, timedelta, timezone

from kiteconnect.exceptions import InputException

TERMINAL_ORDER_STATUSES = {"COMPLETE", "CANCELLED", "REJECTED"}
IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now() -> str:
    # Kite reports order and GTT timestamps in exchange (IST) time.
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


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
        self.margins_collateral = 0.0  # equity.available.collateral (pledged holdings), included in net
        self.ltp_map = dict(ltp or {})
        self.candles = candles or {}
        self._instruments = instruments or []
        self.order_book: dict[str, dict] = {}
        self.gtt_book: dict[int, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.fail_next: dict[str, Exception] = {}
        # Raised AFTER the call has taken effect — the broker accepted it but
        # the response never arrived (see accept_then_raise).
        self.fail_after_next: dict[str, Exception] = {}
        # Called with the call's kwargs before it takes effect; lets a test
        # hold a thread inside a broker call deterministically (see on_call).
        self.before_call: dict[str, object] = {}
        self.auto_fill = True
        self._order_ids = itertools.count(250_000_000_001)
        self._gtt_ids = itertools.count(9001)

    # -- helpers for tests -------------------------------------------------
    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))
        hook = self.before_call.get(name)
        if hook is not None:
            hook(kwargs)
        exc = self.fail_next.pop(name, None)
        if exc is not None:
            raise exc

    def _raise_if_accepted_then_lost(self, name):
        exc = self.fail_after_next.pop(name, None)
        if exc is not None:
            raise exc

    def raise_on_next(self, name, exc):
        """The next `name` call raises before taking effect: the request never
        reached the broker."""
        self.fail_next[name] = exc

    def accept_then_raise(self, name, exc):
        """The next `name` call takes full effect at the broker, then raises:
        e.g. the order was accepted but the HTTP response was lost."""
        self.fail_after_next[name] = exc

    def on_call(self, name, fn):
        """Run fn(kwargs) inside every `name` call, before it takes effect.
        Pass fn=None to remove the hook."""
        if fn is None:
            self.before_call.pop(name, None)
        else:
            self.before_call[name] = fn

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
        if order["filled_quantity"] and order.get("exchange_timestamp") is None:
            order["exchange_timestamp"] = _ist_now()

    def set_gtt_status(self, gtt_id, status):
        self.gtt_book[gtt_id]["status"] = status

    def trigger_gtt(self, gtt_id, leg=0, status="COMPLETE", filled_quantity=None, average_price=None,
                    expose_result=True, placement="success", rejection_reason=""):
        """What Kite does when a GTT fires: marks it 'triggered' and places the
        leg's LIMIT order. That order need not fill — pass status='OPEN' (and
        optionally a partial filled_quantity) for a gap through the limit.

        Like Kite, the fired leg then carries a `result` whose
        order_result.order_id links the GTT to that order. expose_result=False
        leaves the link out (a GTT listing without it); placement='failed'
        means Kite could not place the order at all (e.g. circuit limit): no
        order exists and the result says why. Returns the new order id, or
        None when placement failed. Holdings are not touched; use set_holding."""
        gtt = self.gtt_book[gtt_id]
        if leg >= len(gtt["orders"]):
            raise ValueError(f"GTT {gtt_id} has no order leg {leg}")
        order = gtt["orders"][leg]
        gtt["status"] = "triggered"
        order_id = None
        if placement == "success":
            if filled_quantity is None:
                filled_quantity = order["quantity"] if status == "COMPLETE" else 0
            if average_price is None:
                average_price = order["price"] if filled_quantity else 0
            order_id = self._new_order(
                self.VARIETY_REGULAR, gtt["condition"]["exchange"], gtt["condition"]["tradingsymbol"],
                order["transaction_type"], order["quantity"], order["product"], order.get("order_type", "LIMIT"),
                order["price"], status, filled_quantity, average_price,
            )
        if expose_result:
            triggers = gtt["condition"]["trigger_values"]
            order["result"] = {
                "account_id": "DT0001", "order_id": order_id or "", "status": placement,
                "timestamp": _ist_now(), "triggered_at": triggers[min(leg, len(triggers) - 1)],
                "order_result": {"status": placement, "order_id": order_id or "",
                                 "rejection_reason": rejection_reason},
            }
        return order_id

    def set_holding(self, symbol, quantity, t1_quantity=0, average_price=None, last_price=None, exchange="NSE"):
        """Set (or, with both quantities 0, remove) the broker's holding for symbol."""
        existing = next((h for h in self._holdings if h["tradingsymbol"] == symbol), None)
        if quantity == 0 and t1_quantity == 0:
            if existing is not None:
                self._holdings.remove(existing)
            return
        if existing is None:
            existing = {"tradingsymbol": symbol, "exchange": exchange, "instrument_token": 0, "pnl": 0.0,
                        "day_change": 0.0, "day_change_percentage": 0.0}
            self._holdings.append(existing)
        price = self.ltp_map.get(f"{exchange}:{symbol}", 100.0)
        existing.update(
            quantity=quantity, t1_quantity=t1_quantity,
            average_price=average_price if average_price is not None else existing.get("average_price", price),
            last_price=last_price if last_price is not None else existing.get("last_price", price),
        )

    def set_position(self, symbol, quantity, product="CNC", exchange="NSE", last_price=None, average_price=None,
                     overnight_quantity=0):
        """Set (or, with quantity 0, remove) the broker's net position for symbol.
        Prices default to the symbol's LTP, as Kite reports them."""
        self._positions[:] = [p for p in self._positions if p["tradingsymbol"] != symbol]
        if quantity:
            price = self.ltp_map.get(f"{exchange}:{symbol}", 100.0)
            self._positions.append({
                "tradingsymbol": symbol, "exchange": exchange, "product": product, "quantity": quantity,
                "overnight_quantity": overnight_quantity,
                "last_price": last_price if last_price is not None else price,
                "average_price": average_price if average_price is not None else price,
            })

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
        equity = {"net": self.margins_net}
        if self.margins_collateral:
            # Like Kite: equity.net already includes the margin from pledged holdings.
            equity["available"] = {"collateral": self.margins_collateral}
        return {"equity": equity}

    def ltp(self, keys):
        if isinstance(keys, str):
            keys = [keys]
        self._record("ltp", keys=list(keys))
        return {k: {"instrument_token": 0, "last_price": self.ltp_map.get(k, 100.0)} for k in keys}

    # -- orders ------------------------------------------------------------
    def place_order(self, variety, exchange, tradingsymbol, transaction_type, quantity, product, order_type,
                    price=None, tag=None, **kwargs):
        self._record("place_order", variety=variety, exchange=exchange, tradingsymbol=tradingsymbol,
                     transaction_type=transaction_type, quantity=quantity, product=product,
                     order_type=order_type, price=price, tag=tag)
        key = f"{exchange}:{tradingsymbol}"
        fill_price = price if price is not None else self.ltp_map.get(key, 100.0)
        filled = self.auto_fill
        pending_status = "AMO REQ RECEIVED" if variety == self.VARIETY_AMO else "OPEN"
        order_id = self._new_order(
            variety, exchange, tradingsymbol, transaction_type, quantity, product, order_type, price,
            "COMPLETE" if filled else pending_status, quantity if filled else 0, fill_price if filled else 0,
            tag=tag,
        )
        self._raise_if_accepted_then_lost("place_order")
        return order_id

    def add_broker_order(self, tradingsymbol, transaction_type, quantity, tag=None, status="OPEN", exchange="NSE",
                         product="CNC", order_type="LIMIT", price=3000.0, variety="regular", filled_quantity=None,
                         average_price=None, order_timestamp=None):
        """An order that already exists at the broker (placed by an earlier
        process, or outside this app). Not recorded as a place_order call.
        order_timestamp (IST, 'YYYY-MM-DD HH:MM:SS') defaults to now.
        Returns the new order id."""
        if filled_quantity is None:
            filled_quantity = quantity if status == "COMPLETE" else 0
        if average_price is None:
            average_price = price if filled_quantity else 0
        return self._new_order(variety, exchange, tradingsymbol, transaction_type, quantity, product, order_type,
                               price, status, filled_quantity, average_price, tag=tag, order_timestamp=order_timestamp)

    def _new_order(self, variety, exchange, tradingsymbol, transaction_type, quantity, product, order_type, price,
                   status, filled_quantity, average_price, tag=None, order_timestamp=None):
        order_id = str(next(self._order_ids))
        placed_at = order_timestamp or _ist_now()
        self.order_book[order_id] = {
            "order_id": order_id, "variety": variety, "exchange": exchange, "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type, "quantity": quantity, "product": product,
            "order_type": order_type, "price": price or 0,
            "status": status,
            "filled_quantity": filled_quantity,
            "average_price": average_price,
            "status_message": None,
            "order_timestamp": placed_at,
            "exchange_timestamp": placed_at if filled_quantity else None,
            # Kite returns the order's tag, and (newer API) a list of tags.
            "tag": tag,
            "tags": [tag] if tag else [],
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
        self._raise_if_accepted_then_lost("cancel_order")
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
        self._raise_if_accepted_then_lost("place_gtt")
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
        self._raise_if_accepted_then_lost("modify_gtt")
        return {"trigger_id": trigger_id}

    def delete_gtt(self, trigger_id):
        self._record("delete_gtt", trigger_id=trigger_id)
        if trigger_id not in self.gtt_book:
            raise InputException(f"GTT {trigger_id} not found")
        self.gtt_book[trigger_id]["status"] = "deleted"
        self._raise_if_accepted_then_lost("delete_gtt")
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
