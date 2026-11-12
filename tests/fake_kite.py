"""In-memory stand-in for kiteconnect.KiteConnect, used by the test suite and
the local demo server so nothing ever reaches the real broker."""

import copy
import itertools
from datetime import date, datetime, timedelta, timezone

from kiteconnect.exceptions import InputException, TokenException

TERMINAL_ORDER_STATUSES = {"COMPLETE", "CANCELLED", "REJECTED"}
IST = timezone(timedelta(hours=5, minutes=30))


class SimulatedCrash(BaseException):
    """The process died at this point (Phase 0 harness). A BaseException, so
    no `except Exception` in the code under test can absorb it. Use with
    raise_on_next (died before the request left) or accept_then_raise (the
    broker took effect, then the process died)."""


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
        # equity.available.live_balance; None = net minus collateral (cash actually available).
        self.margins_live_balance = None
        self.margins_enabled = True
        # order_margins(): per-symbol replacement rows (a dict, or a callable(params) -> dict), and
        # the delivery charges Kite would add, as a fraction of the order value.
        self.order_margin_rows: dict[str, object] = {}
        self.order_charges_fraction = 0.001
        self.ltp_map = dict(ltp or {})
        # quote(): per "EXCH:SYMBOL" {"upper_circuit_limit": .., "lower_circuit_limit": ..} overrides
        # (a dict, or None to omit the circuit). Default: +/-20% of the LTP, like an NSE band.
        self.circuit_map: dict[str, dict | None] = {}
        # Like Kite, report the day's CNC net position of every instrument this fake's order book
        # has filled (app orders, GTT-placed orders and add_broker_order alike). Off by default;
        # rows set explicitly with set_position() always win for their (exchange, symbol).
        self.positions_from_fills = False
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
        # -- Phase 0 mock scenarios (GTT-after-fill review §9.2). All opt-in and off by default.
        # They model CONFIGURABLE broker behaviour for tests; none of them is verified Zerodha
        # behaviour. "UNVERIFIED" marks an open question of the review's §10.
        # Raised by every recorded call while set (session expiry, proxy outage); see fail_every_call.
        self.fail_all: Exception | None = None
        # UNVERIFIED (§10 read-after-write): a new GTT is missing from the next N get_gtts() listings.
        self.gtt_list_lag_calls = 0
        self._gtt_hidden: dict[int, int] = {}
        # UNVERIFIED (§10 item 10, shape): GTT records carry created_at / updated_at when True.
        self.gtt_timestamps = False
        # Snapshots the next orders() calls return instead of the live book (serve_orders_snapshot).
        self._orders_replay: list[list[dict]] = []
        # UNVERIFIED: "raise" makes order_history() fail for orders placed before today (IST).
        self.order_history_previous_days = "ok"
        # -- Phase 3 S3 models (docs/PHASE_3_S3_IMPLEMENTATION_REPORT.md §4). Opt-in and off by default.
        # Like the Phase 0 scenarios they are CONFIGURABLE models, never verified Zerodha behaviour; the
        # B-nn labels are the broker-assumption register of docs/PHASE_3_DESIGN_DECISIONS.md §5.
        # Calendar days rollover() has moved this fake's clock forward (0 = today, the default).
        self.day_offset = 0
        # UNVERIFIED (B-27, G5): delete_gtt of a GTT that is already deleted. "noop" (the default, the
        # existing behaviour) succeeds again; "error" raises InputException and changes nothing.
        self.repeat_delete_semantics = "noop"
        # UNVERIFIED (B-27, G5): a modify_gtt that changes nothing (a repeat of the previous modify).
        # "noop" (the default, the existing behaviour) applies it and succeeds; "error" raises InputException.
        self.repeat_modify_semantics = "noop"
        # UNVERIFIED (B-02): place_gtt is refused (InputException, nothing created) while a SELL leg exceeds
        # the quantity the broker shows for the symbol: holdings + T1 + the day's CNC net position.
        self.place_gtt_refuse_until_holding_visible = False
        # UNVERIFIED (B-24): one place_gtt request creates two identical GTTs; only the first id is returned.
        self.place_gtt_creates_duplicate = False
        # UNVERIFIED (B-13): an order placed by trigger_gtt is missing from the next N orders() listings.
        self.hide_triggered_order_calls = 0
        self._order_hidden: dict[str, int] = {}
        # UNVERIFIED (B-28, B-08): "kept" (the default) lists a deleted GTT with status 'deleted';
        # "pruned" leaves deleted GTTs out of get_gtts().
        self.deleted_gtt_listing = "kept"
        # UNVERIFIED (B-08, M-10): an incomplete get_gtts(). gtt_listing_limit = N returns only the first N
        # GTTs (one page); ids in gtt_listing_omit_ids are left out (a partial listing).
        self.gtt_listing_limit: int | None = None
        self.gtt_listing_omit_ids: set[int] = set()
        # UNVERIFIED: an active GTT expires this many rollover days after it was placed (None = never).
        self.gtt_expiry_days: int | None = None
        self._gtt_placed_day: dict[int, int] = {}
        # Orders of earlier days (after rollover): gone from orders(), still known to order_history().
        self._earlier_orders: dict[str, dict] = {}

    # -- helpers for tests -------------------------------------------------
    def _now(self) -> datetime:
        """This fake's exchange (IST) time: real time, moved forward by rollover()."""
        return datetime.now(IST) + timedelta(days=self.day_offset)

    def _now_str(self) -> str:
        return self._now().strftime("%Y-%m-%d %H:%M:%S")

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if self.fail_all is not None:
            raise self.fail_all
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

    def fail_every_call(self, exc):
        """Every recorded call raises exc before taking effect, until
        clear_fail_every_call(): a broker that is unreachable (pass e.g.
        requests.ConnectionError for a proxy outage) or a session that ended.
        profile(), trades(), mf_holdings() and instruments() are not recorded
        calls and are unaffected."""
        self.fail_all = exc

    def clear_fail_every_call(self):
        self.fail_all = None

    def expire_session(self, message="Incorrect `api_key` or `access_token`."):
        """The Kite session ended: every recorded call raises TokenException.
        The message is illustrative, not a verified Kite response."""
        self.fail_every_call(TokenException(message))

    def snapshot_orders(self) -> list[dict]:
        """A copy of the order book as orders() would return it now."""
        return copy.deepcopy(list(self.order_book.values()))

    def serve_orders_snapshot(self, snapshot):
        """The next orders() call returns this (older or duplicate) snapshot
        instead of the live book, once. Queued snapshots are served in order."""
        self._orders_replay.append(copy.deepcopy(list(snapshot)))

    def set_order_state(self, order_id, status, filled_quantity=None, average_price=None, status_message=None):
        order = self.order_book[order_id]
        order["status"] = status
        if filled_quantity is not None:
            order["filled_quantity"] = filled_quantity
        if average_price is not None:
            order["average_price"] = average_price
        order["status_message"] = status_message
        if order["filled_quantity"] and order.get("exchange_timestamp") is None:
            order["exchange_timestamp"] = self._now_str()

    def set_gtt_status(self, gtt_id, status):
        self.gtt_book[gtt_id]["status"] = status

    def trigger_gtt(self, gtt_id, leg=0, status="COMPLETE", filled_quantity=None, average_price=None,
                    expose_result=True, placement="success", rejection_reason="", status_message=None,
                    ddpi_rejected=False):
        """What Kite does when a GTT fires: marks it 'triggered' and places the
        leg's LIMIT order. That order need not fill — pass status='OPEN' (and
        optionally a partial filled_quantity) for a gap through the limit.

        Like Kite, the fired leg then carries a `result` whose
        order_result.order_id links the GTT to that order. expose_result=False
        leaves the link out (a GTT listing without it); placement='failed'
        means Kite could not place the order at all (e.g. circuit limit): no
        order exists and the result says why. Returns the new order id, or
        None when placement failed. Holdings are not touched; use set_holding.

        UNVERIFIED (B-05, U1): ddpi_rejected=True models a CNC sell that the
        exchange refuses because the holding was not authorised for an
        unattended sale (no DDPI/TPIN): the order exists, REJECTED, nothing
        filled. The message is illustrative, not a verified Kite text.
        With hide_triggered_order_calls set, the placed order is missing from
        the next N orders() listings (B-13)."""
        if ddpi_rejected:
            status, filled_quantity = "REJECTED", 0
            if status_message is None:
                status_message = "Holding authorisation (DDPI/TPIN) required for this sell (simulated)"
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
            self.order_book[order_id]["status_message"] = status_message
            if self.hide_triggered_order_calls:
                self._order_hidden[order_id] = int(self.hide_triggered_order_calls)
        if expose_result:
            triggers = gtt["condition"]["trigger_values"]
            order["result"] = {
                "account_id": "DT0001", "order_id": order_id or "", "status": placement,
                "timestamp": self._now_str(), "triggered_at": triggers[min(leg, len(triggers) - 1)],
                "order_result": {"status": placement, "order_id": order_id or "",
                                 "rejection_reason": rejection_reason},
            }
        return order_id

    def trigger_gtt_against_holdings(self, gtt_id, leg=0, policy="reject"):
        """UNVERIFIED (§10): what Kite does when a triggered SELL leg exceeds
        the sellable holding is not known. This fires the GTT and, when the
        leg's quantity exceeds this fake's holding (quantity + t1_quantity) of
        the symbol, resolves it by `policy`:
        - "reject": the placed order is REJECTED, nothing filled;
        - "partial": only the held quantity fills, the rest is CANCELLED;
        - "oversell": the full quantity fills (a short).
        Within the holding the leg fills completely. Holdings are not touched;
        returns the new order id."""
        if policy not in ("reject", "partial", "oversell"):
            raise ValueError(f"unknown policy {policy!r}")
        gtt = self.gtt_book[gtt_id]
        if leg >= len(gtt["orders"]):
            raise ValueError(f"GTT {gtt_id} has no order leg {leg}")
        symbol = gtt["condition"]["tradingsymbol"]
        exchange = gtt["condition"]["exchange"]
        held = sum(int(h.get("quantity") or 0) + int(h.get("t1_quantity") or 0) for h in self._holdings
                   if h["tradingsymbol"] == symbol and h.get("exchange", "NSE") == exchange)
        wanted = int(gtt["orders"][leg]["quantity"])
        if wanted <= held or policy == "oversell":
            return self.trigger_gtt(gtt_id, leg=leg)
        if policy == "reject":
            return self.trigger_gtt(gtt_id, leg=leg, status="REJECTED", filled_quantity=0,
                                    status_message="Insufficient holdings (simulated)")
        return self.trigger_gtt(gtt_id, leg=leg, status="CANCELLED", filled_quantity=held,
                                status_message="Partially filled against holdings (simulated)")

    def set_holding(self, symbol, quantity, t1_quantity=0, average_price=None, last_price=None, exchange="NSE",
                    instrument_token=0):
        """Set (or, with both quantities 0, remove) the broker's holding for exchange:symbol."""
        existing = next((h for h in self._holdings
                         if h["tradingsymbol"] == symbol and h.get("exchange", "NSE") == exchange), None)
        if quantity == 0 and t1_quantity == 0:
            if existing is not None:
                self._holdings.remove(existing)
            return
        if existing is None:
            existing = {"tradingsymbol": symbol, "exchange": exchange, "instrument_token": instrument_token, "pnl": 0.0,
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
        """Set (or, with quantity 0, remove) the broker's net position for exchange:symbol.
        Prices default to the symbol's LTP, as Kite reports them."""
        self._positions[:] = [p for p in self._positions
                              if not (p["tradingsymbol"] == symbol and p.get("exchange", "NSE") == exchange)]
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
        return {"net": self._net_rows(), "day": []}

    def _net_rows(self) -> list[dict]:
        """positions()['net'] without recording a call."""
        net = copy.deepcopy(self._positions)
        if self.positions_from_fills:
            explicit = {(p.get("exchange", "NSE"), p["tradingsymbol"], p.get("product", "CNC")) for p in net}
            derived: dict[tuple, int] = {}
            for o in self.order_book.values():
                filled = int(o.get("filled_quantity") or 0)
                if not filled:
                    continue
                key = (o["exchange"], o["tradingsymbol"], o.get("product") or "CNC")
                derived[key] = derived.get(key, 0) + (filled if o["transaction_type"] == "BUY" else -filled)
            for (exchange, symbol, product), quantity in derived.items():
                if (exchange, symbol, product) in explicit:
                    continue
                price = self.ltp_map.get(f"{exchange}:{symbol}", 100.0)
                net.append({"tradingsymbol": symbol, "exchange": exchange, "product": product, "quantity": quantity,
                            "overnight_quantity": 0, "last_price": price, "average_price": price})
        return net

    def _visible_quantity(self, exchange, symbol) -> int:
        """What the broker shows this account holding of exchange:symbol: settled + T1 holdings plus the
        day's CNC net position (B-02 model)."""
        held = sum(int(h.get("quantity") or 0) + int(h.get("t1_quantity") or 0) for h in self._holdings
                   if h["tradingsymbol"] == symbol and h.get("exchange", "NSE") == exchange)
        day = sum(int(p.get("quantity") or 0) for p in self._net_rows()
                  if p["tradingsymbol"] == symbol and p.get("exchange", "NSE") == exchange
                  and (p.get("product") or "CNC") == "CNC")
        return held + day

    def rollover(self, days: int = 1):
        """UNVERIFIED model of the trading-day boundary (proposal §9.2 item 6), applied `days` times:
        - yesterday's T1 holding settles (t1_quantity moves into quantity);
        - the day's CNC net position becomes T1 (a net sale reduces the settled quantity, never below 0);
        - the day's orders leave orders() (order_history() still answers them, subject to
          order_history_previous_days) and every position row is cleared;
        - GTTs are kept; with gtt_expiry_days set, an active GTT that old becomes 'expired';
        - this fake's clock moves one calendar day forward."""
        for _ in range(days):
            day_net = [p for p in self._net_rows() if (p.get("product") or "CNC") == "CNC"]
            for h in self._holdings:
                h["quantity"] = int(h.get("quantity") or 0) + int(h.get("t1_quantity") or 0)
                h["t1_quantity"] = 0
            for p in day_net:
                symbol, exchange, qty = p["tradingsymbol"], p.get("exchange", "NSE"), int(p["quantity"])
                holding = next((h for h in self._holdings
                                if h["tradingsymbol"] == symbol and h.get("exchange", "NSE") == exchange), None)
                settled = int(holding["quantity"]) if holding else 0
                if qty > 0:
                    self.set_holding(symbol, settled, t1_quantity=qty, exchange=exchange)
                elif qty < 0 and holding is not None:
                    self.set_holding(symbol, max(0, settled + qty), exchange=exchange)
            self._earlier_orders.update(self.order_book)
            self.order_book = {}
            self._order_hidden.clear()
            self._positions[:] = []
            self.day_offset += 1
            if self.gtt_expiry_days is not None:
                for gid, gtt in self.gtt_book.items():
                    placed = self._gtt_placed_day.get(gid, 0)
                    if gtt["status"] == "active" and self.day_offset - placed >= self.gtt_expiry_days:
                        gtt["status"] = "expired"

    def mf_holdings(self):
        return []

    def _equity_funds(self) -> dict:
        equity = {"enabled": self.margins_enabled, "net": self.margins_net}
        # Like Kite: equity.net already includes the margin from pledged holdings (collateral).
        live = self.margins_live_balance
        if live is None:
            numeric = all(isinstance(v, (int, float)) and not isinstance(v, bool)
                          for v in (self.margins_net, self.margins_collateral))
            # A test that sets an unusable net (malformed broker data) gets it passed through as is.
            live = self.margins_net - self.margins_collateral if numeric else self.margins_net
        equity["available"] = {"collateral": self.margins_collateral, "cash": live, "live_balance": live,
                               "opening_balance": live, "adhoc_margin": 0.0, "intraday_payin": 0.0}
        equity["utilised"] = {"debits": 0.0}
        return equity

    def margins(self, segment=None):
        self._record("margins", segment=segment)
        if segment == "equity":
            return self._equity_funds()
        return {"equity": self._equity_funds()}

    def order_margins(self, params):
        """Kite's POST /margins/orders: the margin each order would block. A cash-equity CNC BUY
        blocks its full value; charges are added separately. Places nothing."""
        self._record("order_margins", params=copy.deepcopy(params))
        rows = []
        for order in params:
            key = f"{order['exchange']}:{order['tradingsymbol']}"
            override = self.order_margin_rows.get(order["tradingsymbol"])
            if callable(override):
                rows.append(override(order))
                continue
            if override is not None:
                rows.append(copy.deepcopy(override))
                continue
            price = order.get("price") or self.ltp_map.get(key, 100.0)
            value = float(price) * int(order["quantity"])
            rows.append({"type": "equity", "tradingsymbol": order["tradingsymbol"], "exchange": order["exchange"],
                         "span": 0, "exposure": 0, "option_premium": 0, "additional": 0, "bo": 0, "cash": 0,
                         "var": value, "pnl": {"realised": 0, "unrealised": 0}, "leverage": 1,
                         "charges": {"total": round(value * self.order_charges_fraction, 2)}, "total": value})
        return rows

    def quote(self, keys):
        """GET /quote, the fields the terminal reads: last price and circuit limits."""
        if isinstance(keys, str):
            keys = [keys]
        self._record("quote", keys=list(keys))
        out = {}
        for k in keys:
            last = self.ltp_map.get(k, 100.0)
            row = {"instrument_token": 0, "last_price": last,
                   "upper_circuit_limit": round(last * 1.2, 2), "lower_circuit_limit": round(last * 0.8, 2)}
            if k in self.circuit_map:
                override = self.circuit_map[k]
                if override is None:
                    row.pop("upper_circuit_limit")
                    row.pop("lower_circuit_limit")
                else:
                    row.update(override)
            out[k] = row
        return out

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
        placed_at = order_timestamp or self._now_str()
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
        if self._orders_replay:
            return self._orders_replay.pop(0)
        listing = [o for oid, o in self.order_book.items() if not self._order_hidden.get(oid)]
        # Each listing brings a hidden triggered order one call closer to being visible (B-13).
        for oid in list(self._order_hidden):
            self._order_hidden[oid] -= 1
            if self._order_hidden[oid] <= 0:
                del self._order_hidden[oid]
        return copy.deepcopy(listing)

    def order_history(self, order_id):
        self._record("order_history", order_id=order_id)
        order = self.order_book.get(order_id) or self._earlier_orders.get(order_id)
        if order is None:
            raise InputException(f"Order {order_id} not found")
        if self.order_history_previous_days == "raise" \
                and str(order.get("order_timestamp") or "")[:10] < self._now().strftime("%Y-%m-%d"):
            raise InputException(f"Order {order_id} not found (earlier-day order, simulated)")
        return [copy.deepcopy(order)]

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
        if self.place_gtt_refuse_until_holding_visible:
            visible = self._visible_quantity(exchange, tradingsymbol)
            wanted = max([int(o.get("quantity") or 0) for o in orders if o.get("transaction_type") == "SELL"],
                         default=0)
            if wanted > visible:
                raise InputException(f"Insufficient holdings to place this GTT: {wanted} > {visible} (simulated)")
        gtt_id = self._new_gtt(trigger_type, tradingsymbol, exchange, trigger_values, last_price, orders)
        if self.place_gtt_creates_duplicate:
            self._new_gtt(trigger_type, tradingsymbol, exchange, trigger_values, last_price, orders)
        self._raise_if_accepted_then_lost("place_gtt")
        return {"trigger_id": gtt_id}

    def _new_gtt(self, trigger_type, tradingsymbol, exchange, trigger_values, last_price, orders) -> int:
        gtt_id = next(self._gtt_ids)
        self.gtt_book[gtt_id] = {
            "id": gtt_id, "type": trigger_type, "status": "active",
            "condition": {"exchange": exchange, "tradingsymbol": tradingsymbol,
                          "trigger_values": list(trigger_values), "last_price": last_price},
            "orders": copy.deepcopy(orders),
        }
        if self.gtt_timestamps:
            self.gtt_book[gtt_id]["created_at"] = self.gtt_book[gtt_id]["updated_at"] = self._now_str()
        if self.gtt_list_lag_calls:
            self._gtt_hidden[gtt_id] = self.gtt_list_lag_calls
        self._gtt_placed_day[gtt_id] = self.day_offset
        return gtt_id

    def modify_gtt(self, trigger_id, trigger_type, tradingsymbol, exchange, trigger_values, last_price, orders):
        self._record("modify_gtt", trigger_id=trigger_id, trigger_type=trigger_type, tradingsymbol=tradingsymbol,
                     trigger_values=list(trigger_values), last_price=last_price, orders=copy.deepcopy(orders))
        gtt = self.gtt_book.get(trigger_id)
        if gtt is None or gtt["status"] != "active":
            raise InputException(f"GTT {trigger_id} is not active")
        if self.repeat_modify_semantics == "error" and gtt["condition"]["trigger_values"] == list(trigger_values) \
                and gtt["condition"]["last_price"] == last_price and gtt["orders"] == list(orders):
            raise InputException(f"GTT {trigger_id} is unchanged by this modification (simulated)")
        gtt["condition"]["trigger_values"] = list(trigger_values)
        gtt["condition"]["last_price"] = last_price
        gtt["orders"] = copy.deepcopy(orders)
        if self.gtt_timestamps:
            gtt["updated_at"] = self._now_str()
        self._raise_if_accepted_then_lost("modify_gtt")
        return {"trigger_id": trigger_id}

    def delete_gtt(self, trigger_id):
        self._record("delete_gtt", trigger_id=trigger_id)
        if trigger_id not in self.gtt_book:
            raise InputException(f"GTT {trigger_id} not found")
        if self.repeat_delete_semantics == "error" and self.gtt_book[trigger_id]["status"] == "deleted":
            raise InputException(f"GTT {trigger_id} is already deleted (simulated)")
        self.gtt_book[trigger_id]["status"] = "deleted"
        if self.gtt_timestamps:
            self.gtt_book[trigger_id]["updated_at"] = self._now_str()
        self._raise_if_accepted_then_lost("delete_gtt")
        return {"trigger_id": trigger_id}

    def get_gtts(self):
        self._record("get_gtts")
        listing = [g for gid, g in self.gtt_book.items() if not self._gtt_hidden.get(gid)]
        # Each listing brings a lagging GTT one call closer to being visible.
        for gid in list(self._gtt_hidden):
            self._gtt_hidden[gid] -= 1
            if self._gtt_hidden[gid] <= 0:
                del self._gtt_hidden[gid]
        if self.deleted_gtt_listing == "pruned":
            listing = [g for g in listing if g["status"] != "deleted"]
        if self.gtt_listing_omit_ids:
            listing = [g for g in listing if g["id"] not in self.gtt_listing_omit_ids]
        if self.gtt_listing_limit is not None:
            listing = listing[:self.gtt_listing_limit]
        return copy.deepcopy(listing)

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
