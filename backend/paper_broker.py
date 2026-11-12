"""PK-20: the paper broker — a simulated broker for DRY RUN.

DRY RUN used to mean "pretend it filled": a dry-run approval was marked
filled at the ticket's own price, a dry-run exit closed and journaled the
position on the spot, and no order, GTT, monitor, trailing or partial-fill
code ever ran before real money was at stake (audit P1-13).

DRY RUN now means "simulated broker". PaperBroker exposes the subset of the
KiteConnect interface the terminal uses — place/cancel orders, the order
book, place/modify/delete GTTs, the GTT book — backed by the paper_orders and
paper_gtts tables. Execution, the order monitor, the GTT watcher / trailing
engine and the PK-13 quantity machinery run against it exactly as they run
against Kite for a live ticket; only the broker object differs.

    live ticket     -> KiteConnect (real orders)
    dry-run ticket  -> PaperBroker (paper orders; market data read from Kite)

Isolation. A paper order id is 'PAPER-O-<n>', a paper GTT id 'PAPER-G-<n>';
dry-run tickets carry them in kite_order_id / kite_gtt_id. PaperBroker
refuses any id without that prefix, never forwards an order or GTT call
anywhere (unknown attributes raise), and check_mode() refuses a paper broker
for a live ticket and anything else for a dry-run one. Its only outside call
is a READ: ltp() on the market-data client it was given.

Transactions. A PaperBroker works on the caller's connection and never
commits: a paper order is written in the same transaction as the ticket
update that records it, and the monitor / GTT watcher can modify a paper GTT
while holding their own write transaction (a second connection would wait on
that lock). evaluate() — the simulated exchange — is the one entry point
that opens its own connection and commits.

The simulated exchange (evaluate(), run by the order monitor every cycle):

* nothing happens while the market is closed; with no working paper order
  and no active paper GTT it makes no market-data call at all, otherwise
  exactly one batched ltp() call for every symbol involved;
* an order never fills when placed — only at a price observed by a later
  evaluation. No price, no fill: an order is never filled at an invented
  price (the ticket's reference price is never used);
* MARKET fills at the observed price; a BUY LIMIT only once the price is at
  or below its limit, a SELL LIMIT only at or above; slippage
  (config.PAPER_SLIPPAGE_BPS) makes every fill slightly worse than the
  observed price, rounded to the tick against the trader, never past a limit;
* liquidity: a fill takes the whole remaining quantity. A fill plan (a test
  / demo hook, persisted) makes it fill progressively, e.g. 10 -> 25 -> 40;
* DAY validity: an order still working when its session closes is
  cancelled (keeping any partial fill). An AMO waits for the next session
  and becomes an ordinary open order when it opens;
* a GTT whose trigger is crossed is marked 'triggered' and places its leg's
  LIMIT order, linked through orders[leg].result.order_result.order_id
  exactly as Kite links it. The trigger itself changes no position; the
  order fills (or not) in a later evaluation like any other.

Order statuses are Kite's own strings — 'AMO REQ RECEIVED', 'OPEN',
'COMPLETE', 'CANCELLED', 'REJECTED' — and a partially filled working order
is 'OPEN' with 0 < filled_quantity < quantity, as Kite reports it, so the
order monitor needs no paper-specific states. SL / SL-M orders are refused:
the terminal never places them (its stops are GTTs).
"""

import copy
import functools
import json
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

from kiteconnect.exceptions import GeneralException, InputException

from backend import config, instance_lock, instruments, market, trading_calendar
from backend.db import get_connection

logger = logging.getLogger(__name__)

PAPER_PREFIX = "PAPER-"
ORDER_PREFIX = "PAPER-O-"
GTT_PREFIX = "PAPER-G-"

AMO_PENDING = "AMO REQ RECEIVED"
WORKING_STATUSES = (AMO_PENDING, "OPEN")
TERMINAL_STATUSES = ("COMPLETE", "CANCELLED", "REJECTED")

_SQL_TIME = "%Y-%m-%d %H:%M:%S"
_TAG = re.compile(r"^kt(\d+)x[0-9a-z]+$")
# Non-active GTTs stay listed this long, like Kite's GTT book.
_GTT_BOOK_DAYS = 7


class PaperBrokerUnsupported(AttributeError):
    """The code asked the paper broker for something it does not simulate.
    An AttributeError, so nothing can mistake it for a broker response."""


class ModeIsolationError(Exception):
    """A live ticket met the paper broker, or a dry-run ticket met anything
    else. Raised before any broker call."""


# ---- identity / mode isolation ------------------------------------------------

def is_paper_id(value) -> bool:
    return isinstance(value, str) and value.startswith(PAPER_PREFIX)


def is_paper_broker(broker) -> bool:
    return isinstance(broker, PaperBroker)


# G7: the trading mode fails closed. Exactly "dry_run" routes to the paper
# broker and exactly "live" to Kite (where every live gate still applies);
# anything else (NULL, "", "LIVE", " live", "paper", a non-str) refuses every
# broker write before any call. No case-folding, no stripping, no default:
# an unrecognised mode is never "probably live" or "probably dry run". This
# is the broker boundary: execution.execute_ticket, the order monitor, the
# GTT watcher, protection upkeep and Emergency Exit all pass through
# check_mode() / broker_for(), and no environment variable, request field or
# setting is consulted here.
DRY_RUN = "dry_run"
LIVE = "live"
VALID_MODES = (DRY_RUN, LIVE)


class InvalidTradingMode(ModeIsolationError):
    """The trading mode is missing or not exactly 'dry_run' / 'live'. Raised
    before any broker call."""


def is_valid_mode(mode) -> bool:
    return type(mode) is str and mode in VALID_MODES


def require_valid_mode(mode) -> str:
    if not is_valid_mode(mode):
        raise InvalidTradingMode(f"Trading mode {mode!r} is not exactly 'dry_run' or 'live': broker writes are "
                                 f"refused and nothing was sent.")
    return mode


def check_mode(broker, mode: str | None) -> None:
    """The one mode/broker binding rule: a dry-run ticket is only ever
    handled by the paper broker, a live one only by anything else, and an
    unrecognised mode by nothing."""
    require_valid_mode(mode)
    paper = is_paper_broker(broker)
    if mode == DRY_RUN and not paper:
        raise ModeIsolationError("A dry-run ticket can only use the paper broker; nothing was sent.")
    if mode == LIVE and paper:
        raise ModeIsolationError("A live ticket can never use the paper broker; nothing was sent.")


def broker_for(mode: str | None, kite, conn: sqlite3.Connection, account_id: int):
    """The broker a ticket of this mode executes against: Kite for exactly
    'live', a paper broker (reading market data from `kite`) for exactly
    'dry_run'. Any other mode raises InvalidTradingMode."""
    require_valid_mode(mode)
    if mode == DRY_RUN:
        return PaperBroker(conn, account_id, market_data=kite)
    return kite


# ---- clock / time helpers ------------------------------------------------------

def _now() -> datetime:
    """The paper exchange's clock (IST). Tests pin it."""
    return market.now_ist()


def _utc_sql(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime(_SQL_TIME)


def _ist_text(utc_sql: str | None) -> str | None:
    """A stored UTC time as Kite reports times: IST 'YYYY-MM-DD HH:MM:SS'."""
    if not utc_sql:
        return None
    at = datetime.strptime(utc_sql[:19], _SQL_TIME).replace(tzinfo=timezone.utc)
    return at.astimezone(market.IST).strftime(_SQL_TIME)


def _next_session(at: datetime) -> date:
    """The trading session an After-Market Order placed at `at` waits for:
    today's if placed before the open of a trading day, else the next
    trading day's — NSE holidays skipped (PK-22C)."""
    return trading_calendar.next_session_date(at)


def _session_over(order, now: datetime) -> bool:
    session = date.fromisoformat(order["session_date"])
    if now.date() > session:
        return True
    return now.date() == session and now.time() >= market.MARKET_CLOSE and not market.is_market_open(now)


def _today_cutoff_utc(now: datetime) -> str:
    return trading_calendar.ist_day_start_utc_sql(now)


# ---- pricing -------------------------------------------------------------------

def fill_price(transaction_type: str, order_type: str, observed: float, limit: float | None,
               tick_size: float = market.DEFAULT_TICK_SIZE, slippage_bps: float | None = None) -> float:
    """The price a paper order fills at, given the observed market price:
    BUY  = observed x (1 + bps/10000), rounded UP to the tick,
    SELL = observed x (1 - bps/10000), rounded DOWN to the tick;
    a LIMIT order never fills beyond its own limit price."""
    bps = config.PAPER_SLIPPAGE_BPS if slippage_bps is None else slippage_bps
    slip = bps / 10_000
    if transaction_type == "BUY":
        price = market.round_to_tick(observed * (1 + slip), tick_size, "up")
        return min(price, limit) if order_type == "LIMIT" else price
    price = market.round_to_tick(observed * (1 - slip), tick_size, "down")
    return max(price, limit) if order_type == "LIMIT" else price


def is_fillable(transaction_type: str, order_type: str, observed: float, limit: float | None) -> bool:
    if order_type != "LIMIT":
        return True
    return observed <= limit if transaction_type == "BUY" else observed >= limit


# ---- test / demo hooks (never reachable from the production UI) ------------------

_accept_then_raise: dict[str, Exception] = {}


def accept_then_raise(name: str, exc: Exception) -> None:
    """The next `name` call (place_order, cancel_order, place_gtt,
    modify_gtt, delete_gtt) takes full effect, then raises `exc`: the
    paper counterpart of "Kite accepted it but the response was lost"
    (PK-2). One-shot, in memory only."""
    _accept_then_raise[name] = exc


def reset_test_hooks() -> None:
    _accept_then_raise.clear()


def _raise_if_accepted_then_lost(name: str) -> None:
    exc = _accept_then_raise.pop(name, None)
    if exc is not None:
        raise exc


def set_fill_plan(order_id: str, plan: list[int]) -> None:
    """Makes a working paper order fill progressively: `plan` lists
    cumulative filled quantities (e.g. [10, 25, 40]); each evaluation that
    finds the order fillable advances it one step. Persisted, so it
    survives a restart. Test / demo hook."""
    steps = [int(q) for q in plan]
    if not steps or any(q <= 0 for q in steps) or steps != sorted(set(steps)):
        raise ValueError("A fill plan is a strictly increasing list of positive cumulative quantities")
    conn = get_connection()
    try:
        if conn.execute(
            "UPDATE paper_orders SET fill_plan = ?, updated_at = datetime('now') "
            "WHERE paper_order_id = ? AND status IN (?, ?)",
            (json.dumps(steps), order_id, *WORKING_STATUSES),
        ).rowcount != 1:
            raise ValueError(f"No working paper order {order_id}")
        conn.commit()
    finally:
        conn.close()


def reject_order(order_id: str, reason: str) -> None:
    """The simulated exchange rejects a working order (test / demo hook):
    it ends REJECTED, keeping whatever it had filled."""
    conn = get_connection()
    try:
        if conn.execute(
            "UPDATE paper_orders SET status = 'REJECTED', status_message = ?, updated_at = datetime('now') "
            "WHERE paper_order_id = ? AND status IN (?, ?)",
            (reason, order_id, *WORKING_STATUSES),
        ).rowcount != 1:
            raise ValueError(f"No working paper order {order_id}")
        conn.commit()
    finally:
        conn.close()


# ---- the broker ------------------------------------------------------------------

def _order_dict(row) -> dict:
    return {
        "order_id": row["paper_order_id"], "variety": row["variety"], "exchange": row["exchange"],
        "tradingsymbol": row["tradingsymbol"], "transaction_type": row["transaction_type"],
        "quantity": row["quantity"], "product": row["product"], "order_type": row["order_type"],
        "price": row["price"] or 0, "trigger_price": row["trigger_price"] or 0,
        "status": row["status"], "status_message": row["status_message"],
        "filled_quantity": row["filled_quantity"], "pending_quantity": row["pending_quantity"],
        "cancelled_quantity": (row["quantity"] - row["filled_quantity"]
                               if row["status"] in ("CANCELLED", "REJECTED") else 0),
        "average_price": row["average_price"] or 0,
        "order_timestamp": _ist_text(row["created_at"]), "exchange_timestamp": _ist_text(row["filled_at"]),
        "tag": row["tag"], "tags": [row["tag"]] if row["tag"] else [],
        "paper": True,
    }


def _gtt_dict(row) -> dict:
    return {
        "id": row["paper_gtt_id"], "type": row["trigger_type"], "status": row["status"],
        "created_at": _ist_text(row["created_at"]), "updated_at": _ist_text(row["updated_at"]),
        "condition": {"exchange": row["exchange"], "tradingsymbol": row["tradingsymbol"],
                      "trigger_values": json.loads(row["trigger_values"]), "last_price": row["last_price"]},
        "orders": json.loads(row["legs"]),
        "paper": True,
    }


def gtt_dict(row) -> dict:
    """A paper_gtts row exactly as PaperBroker.get_gtts() lists it."""
    return _gtt_dict(row)


def _owner_only(method):
    """Phase 2 (docs/PHASE_2_DESIGN_REVIEW.md §7.3): a paper write changes the shared database, so, like a
    Kite write, only the process that owns the database may make one. Refused otherwise with
    instance_lock.InstanceNotOwner (a definite rejection: nothing was written)."""
    @functools.wraps(method)
    def write(self, *args, **kwargs):
        with instance_lock.broker_write(f"paper.{method.__name__}", "WRITE"):
            return method(self, *args, **kwargs)
    return write


class PaperBroker:
    """A KiteConnect stand-in for dry-run tickets. See the module docstring."""

    VARIETY_REGULAR = "regular"
    VARIETY_AMO = "amo"
    GTT_TYPE_SINGLE = "single"
    GTT_TYPE_OCO = "two-leg"

    def __init__(self, conn: sqlite3.Connection, account_id: int, market_data=None):
        self._conn = conn
        self._account_id = account_id
        self._market = market_data

    def __getattr__(self, name):
        # Only reached for attributes this class does not define: never fall
        # through to anything that could touch a real account.
        raise PaperBrokerUnsupported(f"The paper broker does not simulate {name!r}; nothing was sent to Kite.")

    # -- market data (a read, delegated) ----------------------------------------
    def ltp(self, keys):
        if self._market is None:
            raise GeneralException("The paper broker has no market-data source for LTP")
        return self._market.ltp(keys)

    # -- portfolio (net paper fills; supporting notes only) ---------------------
    def holdings(self):
        rows = self._conn.execute(
            "SELECT tradingsymbol, exchange, SUM(CASE WHEN transaction_type = 'BUY' THEN filled_quantity "
            "ELSE -filled_quantity END) AS qty FROM paper_orders WHERE account_id = ? "
            "GROUP BY tradingsymbol, exchange",
            (self._account_id,),
        ).fetchall()
        return [{"tradingsymbol": r["tradingsymbol"], "exchange": r["exchange"], "quantity": r["qty"],
                 "t1_quantity": 0, "paper": True} for r in rows if r["qty"] and r["qty"] > 0]

    def positions(self):
        return {"net": [], "day": []}

    # -- orders ------------------------------------------------------------------
    @_owner_only
    def place_order(self, variety, exchange, tradingsymbol, transaction_type, quantity, product, order_type,
                    price=None, trigger_price=None, tag=None, **kwargs):
        order_id = self._insert_order(variety, exchange, tradingsymbol, transaction_type, quantity, product,
                                      order_type, price, trigger_price, tag, source_gtt_id=None, now=_now())
        _raise_if_accepted_then_lost("place_order")
        return order_id

    def _insert_order(self, variety, exchange, tradingsymbol, transaction_type, quantity, product, order_type,
                      price, trigger_price, tag, source_gtt_id, now: datetime) -> str:
        if order_type in ("SL", "SL-M"):
            raise InputException(f"Paper broker: {order_type} orders are not supported (the terminal never places "
                                 f"them; its stops are GTTs).")
        if order_type not in ("MARKET", "LIMIT"):
            raise InputException(f"Paper broker: invalid order_type {order_type!r}")
        if transaction_type not in ("BUY", "SELL"):
            raise InputException(f"Paper broker: invalid transaction_type {transaction_type!r}")
        if variety not in (self.VARIETY_REGULAR, self.VARIETY_AMO):
            raise InputException(f"Paper broker: invalid variety {variety!r}")
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            raise InputException(f"Paper broker: invalid quantity {quantity!r}")
        if quantity <= 0:
            raise InputException(f"Paper broker: quantity must be positive, got {quantity}")
        if order_type == "LIMIT" and not (price and price > 0):
            raise InputException("Paper broker: a LIMIT order needs a positive price")
        # Kite's session rules: regular orders only while the market is open,
        # After-Market Orders only while it is closed.
        open_now = market.is_market_open(now)
        if variety == self.VARIETY_REGULAR and not open_now:
            raise InputException("Paper broker: markets are closed — a regular order is not accepted now (use AMO).")
        if variety == self.VARIETY_AMO and open_now:
            raise InputException("Paper broker: the market is open — After-Market Orders are not accepted now.")
        session = now.date() if variety == self.VARIETY_REGULAR else _next_session(now)
        match = _TAG.match(tag or "")
        created = _utc_sql(now)
        try:
            cursor = self._conn.execute(
                """
                INSERT INTO paper_orders (
                    account_id, ticket_id, source_gtt_id, tag, variety, exchange, tradingsymbol,
                    transaction_type, product, order_type, quantity, filled_quantity, pending_quantity,
                    price, trigger_price, average_price, status, session_date, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (self._account_id, int(match.group(1)) if match else None, source_gtt_id, tag or None, variety,
                 exchange, tradingsymbol, transaction_type, product, order_type, quantity, quantity,
                 price if order_type == "LIMIT" else None, trigger_price or None,
                 AMO_PENDING if variety == self.VARIETY_AMO else "OPEN", session.isoformat(), created, created),
            )
        except sqlite3.IntegrityError:
            # A second order with the same tag would be a duplicate submission
            # of one ticket. Not a definite rejection: an order with this tag
            # exists, so PK-2 recovery (which finds orders by tag) resolves it.
            raise GeneralException(f"Paper broker: an order tagged {tag} already exists; nothing new was placed.")
        order_id = f"{ORDER_PREFIX}{cursor.lastrowid}"
        self._conn.execute("UPDATE paper_orders SET paper_order_id = ? WHERE id = ?", (order_id, cursor.lastrowid))
        return order_id

    def _order_row(self, order_id):
        if not is_paper_id(order_id):
            raise InputException(f"Paper broker: {order_id!r} is not a paper order")
        row = self._conn.execute(
            "SELECT * FROM paper_orders WHERE paper_order_id = ? AND account_id = ?", (order_id, self._account_id),
        ).fetchone()
        if row is None:
            raise InputException(f"Paper broker: order {order_id} not found")
        return row

    def orders(self):
        """Like Kite's order book: today's orders, plus any still working
        (an AMO waiting overnight)."""
        rows = self._conn.execute(
            "SELECT * FROM paper_orders WHERE account_id = ? AND (status IN (?, ?) OR created_at >= ? "
            "OR updated_at >= ?) ORDER BY id",
            (self._account_id, *WORKING_STATUSES, _today_cutoff_utc(_now()), _today_cutoff_utc(_now())),
        ).fetchall()
        return [_order_dict(r) for r in rows]

    def order_history(self, order_id):
        return [_order_dict(self._order_row(order_id))]

    def trades(self):
        return []

    @_owner_only
    def cancel_order(self, variety, order_id, **kwargs):
        row = self._order_row(order_id)
        if row["status"] not in WORKING_STATUSES or self._conn.execute(
            "UPDATE paper_orders SET status = 'CANCELLED', status_message = 'Cancelled by user (paper)', "
            "cancelled_at = datetime('now'), updated_at = datetime('now') "
            "WHERE paper_order_id = ? AND status IN (?, ?)",
            (order_id, *WORKING_STATUSES),
        ).rowcount != 1:
            current = self._order_row(order_id)["status"]
            raise InputException(f"Paper broker: order {order_id} cannot be cancelled — it is {current}.")
        _raise_if_accepted_then_lost("cancel_order")
        return order_id

    # -- GTTs --------------------------------------------------------------------
    def _validate_gtt(self, trigger_type, trigger_values, orders) -> tuple[list, list]:
        triggers = [float(t) for t in trigger_values or []]
        legs = [{
            "transaction_type": leg["transaction_type"], "quantity": int(leg["quantity"]),
            "order_type": leg.get("order_type", "LIMIT"), "product": leg["product"], "price": float(leg["price"]),
        } for leg in orders or []]
        expected = {self.GTT_TYPE_SINGLE: 1, self.GTT_TYPE_OCO: 2}.get(trigger_type)
        if expected is None:
            raise InputException(f"Paper broker: invalid GTT type {trigger_type!r}")
        if len(triggers) != expected or len(legs) != expected:
            raise InputException(f"Paper broker: a {trigger_type} GTT needs {expected} trigger(s) and leg(s)")
        if any(leg["quantity"] <= 0 or leg["price"] <= 0 for leg in legs):
            raise InputException("Paper broker: GTT legs need a positive quantity and price")
        if any(leg["order_type"] != "LIMIT" for leg in legs):
            raise InputException("Paper broker: GTT legs are LIMIT orders")
        if expected == 2 and not triggers[0] < triggers[1]:
            raise InputException("Paper broker: an OCO GTT's triggers must be [lower, upper]")
        return triggers, legs

    @_owner_only
    def place_gtt(self, trigger_type, tradingsymbol, exchange, trigger_values, last_price, orders):
        triggers, legs = self._validate_gtt(trigger_type, trigger_values, orders)
        cursor = self._conn.execute(
            "INSERT INTO paper_gtts (account_id, trigger_type, exchange, tradingsymbol, trigger_values, last_price, "
            "legs, quantity, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')",
            (self._account_id, trigger_type, exchange, tradingsymbol, json.dumps(triggers), last_price,
             json.dumps(legs), max(leg["quantity"] for leg in legs)),
        )
        gtt_id = f"{GTT_PREFIX}{cursor.lastrowid}"
        self._conn.execute("UPDATE paper_gtts SET paper_gtt_id = ? WHERE id = ?", (gtt_id, cursor.lastrowid))
        _raise_if_accepted_then_lost("place_gtt")
        return {"trigger_id": gtt_id}

    def _gtt_row(self, trigger_id):
        if not is_paper_id(trigger_id):
            raise InputException(f"Paper broker: {trigger_id!r} is not a paper GTT")
        row = self._conn.execute(
            "SELECT * FROM paper_gtts WHERE paper_gtt_id = ? AND account_id = ?", (trigger_id, self._account_id),
        ).fetchone()
        if row is None:
            raise InputException(f"Paper broker: GTT {trigger_id} not found")
        return row

    @_owner_only
    def modify_gtt(self, trigger_id, trigger_type, tradingsymbol, exchange, trigger_values, last_price, orders):
        row = self._gtt_row(trigger_id)
        triggers, legs = self._validate_gtt(trigger_type, trigger_values, orders)
        if row["status"] != "active" or self._conn.execute(
            "UPDATE paper_gtts SET trigger_type = ?, trigger_values = ?, last_price = ?, legs = ?, quantity = ?, "
            "updated_at = datetime('now') WHERE paper_gtt_id = ? AND status = 'active'",
            (trigger_type, json.dumps(triggers), last_price, json.dumps(legs),
             max(leg["quantity"] for leg in legs), trigger_id),
        ).rowcount != 1:
            raise InputException(f"Paper broker: GTT {trigger_id} is not active")
        _raise_if_accepted_then_lost("modify_gtt")
        return {"trigger_id": trigger_id}

    @_owner_only
    def delete_gtt(self, trigger_id):
        row = self._gtt_row(trigger_id)
        if row["status"] != "active" or self._conn.execute(
            "UPDATE paper_gtts SET status = 'deleted', updated_at = datetime('now') "
            "WHERE paper_gtt_id = ? AND status = 'active'", (trigger_id,),
        ).rowcount != 1:
            raise InputException(f"Paper broker: GTT {trigger_id} is not active ({self._gtt_row(trigger_id)['status']})")
        _raise_if_accepted_then_lost("delete_gtt")
        return {"trigger_id": trigger_id}

    def get_gtts(self):
        cutoff = _utc_sql(_now() - timedelta(days=_GTT_BOOK_DAYS))
        rows = self._conn.execute(
            "SELECT * FROM paper_gtts WHERE account_id = ? AND (status = 'active' OR updated_at >= ?) ORDER BY id",
            (self._account_id, cutoff),
        ).fetchall()
        return [_gtt_dict(r) for r in rows]


# ---- the simulated exchange ----------------------------------------------------

def evaluate(account_id: int, market_data, now: datetime | None = None) -> dict:
    """Advances the paper books one step against the observed market. Its
    own connection; each change commits on its own. See the module
    docstring for the rules."""
    now = market.as_ist(now) if now is not None else _now()
    summary = {"ltp_calls": 0, "filled": 0, "triggered": 0, "expired": 0, "activated": 0}
    conn = get_connection()
    try:
        working = conn.execute(
            "SELECT * FROM paper_orders WHERE account_id = ? AND status IN (?, ?) ORDER BY id",
            (account_id, *WORKING_STATUSES),
        ).fetchall()
        gtts = conn.execute(
            "SELECT * FROM paper_gtts WHERE account_id = ? AND status = 'active' ORDER BY id", (account_id,),
        ).fetchall()
        if not working and not gtts:
            return summary

        # DAY validity needs no price: an order whose session has closed ends.
        live_orders = []
        for order in working:
            if _session_over(order, now):
                if conn.execute(
                    "UPDATE paper_orders SET status = 'CANCELLED', cancelled_at = ?, updated_at = ?, "
                    "status_message = 'Expired unfilled at the end of its session (DAY validity) — paper' "
                    "WHERE id = ? AND status = ? AND filled_quantity = ?",
                    (_utc_sql(now), _utc_sql(now), order["id"], order["status"], order["filled_quantity"]),
                ).rowcount == 1:
                    summary["expired"] += 1
                conn.commit()
            else:
                live_orders.append(order)

        if not market.is_market_open(now):
            return summary

        eligible = []
        for order in live_orders:
            if order["status"] == AMO_PENDING:
                if now.date() < date.fromisoformat(order["session_date"]):
                    continue  # waits for its session
                if conn.execute(
                    "UPDATE paper_orders SET status = 'OPEN', updated_at = ? WHERE id = ? AND status = ?",
                    (_utc_sql(now), order["id"], AMO_PENDING),
                ).rowcount == 1:
                    summary["activated"] += 1
                conn.commit()
                order = conn.execute("SELECT * FROM paper_orders WHERE id = ?", (order["id"],)).fetchone()
                if order["status"] != "OPEN":
                    continue
            eligible.append(order)

        keys = sorted({f"{r['exchange']}:{r['tradingsymbol']}" for r in [*eligible, *gtts]})
        if not keys:
            return summary
        try:
            quotes = market_data.ltp(keys)
        except Exception as e:
            # No price, no fill: nothing is advanced on an invented price.
            logger.warning("Paper exchange: LTP unavailable (%s: %s); paper orders stay pending this cycle",
                           type(e).__name__, e)
            summary["skipped"] = "ltp_unavailable"
            return summary
        summary["ltp_calls"] = 1
        prices = {}
        for key, quote in (quotes or {}).items():
            try:
                price = float((quote or {}).get("last_price") or 0)
            except (TypeError, ValueError):
                continue
            if price > 0:
                prices[key] = price

        # Orders that existed before this cycle fill first; an order a GTT
        # places below waits for the next observed price.
        # PK-22F: each order / GTT is its own transaction; one that raises
        # is rolled back alone and retried next cycle.
        for order in eligible:
            price = prices.get(f"{order['exchange']}:{order['tradingsymbol']}")
            try:
                if price is not None and _try_fill(conn, order, price, now):
                    summary["filled"] += 1
                conn.commit()
            except Exception:
                conn.rollback()
                summary["errors"] = summary.get("errors", 0) + 1
                logger.exception("event=paper.fill_failed paper_order_id=%s symbol=%s (rolled back)",
                                 order["paper_order_id"], order["tradingsymbol"])
        for gtt in gtts:
            price = prices.get(f"{gtt['exchange']}:{gtt['tradingsymbol']}")
            try:
                if price is not None and _try_trigger(conn, account_id, gtt, price, now):
                    summary["triggered"] += 1
                conn.commit()
            except Exception:
                conn.rollback()
                summary["errors"] = summary.get("errors", 0) + 1
                logger.exception("event=paper.trigger_failed gtt_id=%s symbol=%s (rolled back)",
                                 gtt["paper_gtt_id"], gtt["tradingsymbol"])
        return summary
    finally:
        conn.close()


def _next_fill_quantity(order) -> int:
    filled, quantity = order["filled_quantity"], order["quantity"]
    plan = json.loads(order["fill_plan"]) if order["fill_plan"] else None
    if not plan:
        return quantity
    for step in plan:
        if step > filled:
            return min(step, quantity)
    return quantity


def _try_fill(conn: sqlite3.Connection, order, observed: float, now: datetime) -> bool:
    side, order_type, limit = order["transaction_type"], order["order_type"], order["price"]
    if not is_fillable(side, order_type, observed, limit):
        return False
    tick = instruments.get_tick_size(order["tradingsymbol"], order["exchange"])
    price = fill_price(side, order_type, observed, limit, tick)
    filled = order["filled_quantity"]
    new_filled = _next_fill_quantity(order)
    if new_filled <= filled:
        return False
    step = new_filled - filled
    average = round((filled * (order["average_price"] or 0) + step * price) / new_filled, 4)
    status = "COMPLETE" if new_filled == order["quantity"] else "OPEN"
    at = _utc_sql(now)
    return conn.execute(
        "UPDATE paper_orders SET filled_quantity = ?, pending_quantity = ?, average_price = ?, status = ?, "
        "filled_at = ?, updated_at = ? WHERE id = ? AND status = ? AND filled_quantity = ?",
        (new_filled, order["quantity"] - new_filled, average, status, at, at, order["id"], order["status"], filled),
    ).rowcount == 1


def _fired_leg(gtt, observed: float) -> int | None:
    triggers = json.loads(gtt["trigger_values"])
    if gtt["trigger_type"] == PaperBroker.GTT_TYPE_OCO:
        lo, hi = triggers
        if observed <= lo:
            return 0
        if observed >= hi:
            return 1
        return None
    trigger, last = triggers[0], gtt["last_price"]
    # Like Kite: a single trigger fires when the price crosses it from the
    # side it was on when the GTT was placed (below it: a rise; else a fall).
    if last is not None and last < trigger:
        return 0 if observed >= trigger else None
    return 0 if observed <= trigger else None


def _try_trigger(conn: sqlite3.Connection, account_id: int, gtt, observed: float, now: datetime) -> bool:
    leg_index = _fired_leg(gtt, observed)
    if leg_index is None:
        return False
    # The compare-and-set includes the legs and triggers read above, so a
    # GTT modified meanwhile (a trailing ratchet, a PK-13 resize) is re-read
    # next cycle instead of firing on stale terms.
    if conn.execute(
        "UPDATE paper_gtts SET status = 'triggered', triggered_at = ?, updated_at = ? "
        "WHERE id = ? AND status = 'active' AND legs = ? AND trigger_values = ?",
        (_utc_sql(now), _utc_sql(now), gtt["id"], gtt["legs"], gtt["trigger_values"]),
    ).rowcount != 1:
        return False
    legs = json.loads(gtt["legs"])
    leg = legs[leg_index]
    broker = PaperBroker(conn, account_id)
    try:
        order_id = broker._insert_order(
            PaperBroker.VARIETY_REGULAR, gtt["exchange"], gtt["tradingsymbol"], leg["transaction_type"],
            leg["quantity"], leg["product"], "LIMIT", leg["price"], None, None,
            source_gtt_id=gtt["paper_gtt_id"], now=now,
        )
        placement, rejection = "success", ""
    except InputException as e:
        order_id, placement, rejection = "", "failed", str(e)
    leg = copy.deepcopy(leg)
    leg["result"] = {
        "account_id": "PAPER", "order_id": order_id, "status": placement,
        "timestamp": now.strftime(_SQL_TIME), "triggered_at": observed,
        "order_result": {"status": placement, "order_id": order_id, "rejection_reason": rejection},
    }
    legs[leg_index] = leg
    conn.execute("UPDATE paper_gtts SET legs = ? WHERE id = ?", (json.dumps(legs), gtt["id"]))
    return True
