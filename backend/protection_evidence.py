"""Phase 3 (DARK): broker evidence for the protection engine (docs/PHASE_3_DESIGN_DECISIONS.md).

One Evidence object is one broker read: the same four endpoints Stage 1 reads (orders, GTTs, holdings,
positions; startup_reconciliation.read_broker), parsed into normalised GTT and order views and recorded as a
v11 broker_reads row (purpose 'prewrite', 'reconciliation', 'emergency_exit' or 'resolution') in a fenced
transaction. Every engine decision names the broker_reads.id it relied on.

What this module never does:
- conclude "absent" from a failed, partial or malformed read (complete is False and nothing is concluded);
- treat a GTT whose id or status it cannot parse as harmless: it is kept in `unparsed` and makes the GTT
  set ambiguous for every consumer (R3-2, B-32);
- merge two different GTT ids (B-29) or guess an id.

Freshness (DD-8): a read is usable for a send only if (i) it is complete, (ii) its wall-clock age is at most
PREWRITE_MAX_AGE_SECONDS, and (iii) it started after the last resume and after the current takeover. The
monotonic clock is never used alone for read age.

Nothing here talks to Kite: the broker object is passed in (FakeKite in every Phase 3 test).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from backend import config, instance_lock

logger = logging.getLogger(__name__)

PURPOSES = ("prewrite", "reconciliation", "emergency_exit", "resolution", "startup")

# B-32: the only statuses the engine understands. Only 'active' is armed; only an explicit 'deleted' or
# 'cancelled' confirms a removal. Anything else is an unknown status (never proof of anything).
GTT_ACTIVE = "active"
GTT_TRIGGERED = "triggered"
GTT_REMOVED_STATUSES = frozenset({"deleted", "cancelled"})
GTT_KNOWN_STATUSES = frozenset({"active", "triggered", "deleted", "cancelled", "disabled", "expired", "rejected"})

# Order statuses (Kite vocabulary as modelled by FakeKite; B-32). Terminal = cannot execute further.
ORDER_TERMINAL = frozenset({"COMPLETE", "CANCELLED", "REJECTED"})
ORDER_WORKING = frozenset({"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED", "PUT ORDER REQ RECEIVED",
                           "VALIDATION PENDING", "OPEN PENDING", "MODIFY PENDING", "MODIFY VALIDATION PENDING",
                           "CANCEL PENDING", "UPDATE"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat()


def parse_time(value) -> datetime | None:
    """ISO text (assumed UTC when naive) as an instant; never compared as a string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalise_gtt_id(value) -> str | None:
    """The canonical identity of a broker GTT id (R3-2): Kite ids are integers, paper ids 'PAPER-G-<n>'.
    Booleans, floats, blanks and anything else are not ids (None: ambiguous, fail closed)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() and int(text) > 0:
            return str(int(text))
        if text.startswith("PAPER-G-") and text[len("PAPER-G-"):].isdigit():
            return text
    return None


def _int_or_none(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


@dataclass(frozen=True)
class Leg:
    transaction_type: str | None
    quantity: int | None
    product: str | None
    order_type: str | None
    price: float | None
    order_id: str | None          # the triggered order linked from this leg's result, if any
    triggered: bool               # this leg carries a result (it fired), whatever the placement outcome
    placement: str | None         # result.order_result.status ('success' / 'failed' / None)


@dataclass(frozen=True)
class GttView:
    id: str
    status: str                   # lower-cased; '' when absent (unknown status)
    exchange: str | None
    tradingsymbol: str | None
    trigger_type: str | None
    trigger_values: tuple
    legs: tuple

    @property
    def status_known(self) -> bool:
        return self.status in GTT_KNOWN_STATUSES

    def instrument(self) -> tuple[str, str]:
        return (str(self.exchange or "").upper(), str(self.tradingsymbol or "").upper())

    def sell_legs(self) -> list[Leg]:
        return [leg for leg in self.legs if leg.transaction_type == "SELL"]

    def has_sell(self) -> bool:
        return bool(self.sell_legs())


@dataclass(frozen=True)
class OrderView:
    id: str
    status: str
    quantity: int | None
    filled_quantity: int | None
    pending_quantity: int | None
    cancelled_quantity: int | None
    transaction_type: str | None
    exchange: str | None
    tradingsymbol: str | None


def parse_gtt(raw) -> GttView | None:
    if not isinstance(raw, dict):
        return None
    gid = normalise_gtt_id(raw.get("id"))
    if gid is None:
        return None
    cond = raw.get("condition") if isinstance(raw.get("condition"), dict) else {}
    legs = []
    for leg in raw.get("orders") or []:
        if not isinstance(leg, dict):
            legs.append(Leg(None, None, None, None, None, None, False, None))
            continue
        result = leg.get("result") if isinstance(leg.get("result"), dict) else None
        order_result = (result or {}).get("order_result") if isinstance((result or {}).get("order_result"), dict) else {}
        order_id = (order_result or {}).get("order_id") or (result or {}).get("order_id") or None
        price = leg.get("price")
        legs.append(Leg(
            transaction_type=leg.get("transaction_type") if isinstance(leg.get("transaction_type"), str) else None,
            quantity=_int_or_none(leg.get("quantity")),
            product=leg.get("product") if isinstance(leg.get("product"), str) else None,
            order_type=leg.get("order_type") if isinstance(leg.get("order_type"), str) else None,
            price=float(price) if isinstance(price, (int, float)) and not isinstance(price, bool) else None,
            order_id=str(order_id) if order_id else None,
            triggered=result is not None,
            placement=(order_result or {}).get("status") if result is not None else None,
        ))
    status = raw.get("status")
    return GttView(
        id=gid, status=str(status).strip().lower() if isinstance(status, str) else "",
        exchange=cond.get("exchange"), tradingsymbol=cond.get("tradingsymbol"),
        trigger_type=raw.get("type") if isinstance(raw.get("type"), str) else None,
        trigger_values=tuple(cond.get("trigger_values") or ()), legs=tuple(legs),
    )


def parse_order(raw) -> OrderView | None:
    if not isinstance(raw, dict) or not raw.get("order_id"):
        return None
    status = raw.get("status")
    return OrderView(
        id=str(raw.get("order_id")), status=str(status).strip().upper() if isinstance(status, str) else "",
        quantity=_int_or_none(raw.get("quantity")), filled_quantity=_int_or_none(raw.get("filled_quantity")),
        pending_quantity=_int_or_none(raw.get("pending_quantity")),
        cancelled_quantity=_int_or_none(raw.get("cancelled_quantity")),
        transaction_type=raw.get("transaction_type"), exchange=raw.get("exchange"),
        tradingsymbol=raw.get("tradingsymbol"),
    )


@dataclass
class Evidence:
    """One broker read. `complete` is the startup_reconciliation definition (every endpoint ok, no
    malformed GTT row, holdings and positions parsed, span within STARTUP_READ_SPAN_SECONDS) AND every GTT
    row parsed into a view (an unparsable row makes the GTT set ambiguous: complete is False)."""
    read_id: int | None
    account_id: int
    purpose: str
    epoch: int | None
    started_at: datetime
    gtts_at: datetime | None
    finished_at: datetime | None
    complete: bool
    gtts: dict[str, GttView] = field(default_factory=dict)
    orders: dict[str, OrderView] = field(default_factory=dict)
    held: dict | None = None
    unparsed_gtts: int = 0
    duplicate_ids: tuple = ()
    started_mono: float | None = None
    gtts_mono: float | None = None
    detail: str = ""
    raw: dict | None = None        # the read_broker() result (attribution needs its orders and holdings)

    def gtt(self, gtt_id) -> GttView | None:
        key = normalise_gtt_id(gtt_id)
        return None if key is None else self.gtts.get(key)

    def sell_gtts_on(self, exchange: str, tradingsymbol: str) -> list[GttView]:
        key = (str(exchange or "").upper(), str(tradingsymbol or "").upper())
        return [g for g in self.gtts.values() if g.instrument() == key and g.has_sell()]

    def ref(self) -> dict:
        return {"read_id": self.read_id, "purpose": self.purpose, "started_at": iso(self.started_at),
                "gtts_at": iso(self.gtts_at) if self.gtts_at else None, "complete": self.complete}


def from_read(read: dict, *, account_id: int, purpose: str, epoch: int | None, started_at: datetime,
              finished_at: datetime | None, read_id: int | None = None, started_mono: float | None = None,
              gtts_mono: float | None = None) -> Evidence:
    """Builds Evidence from a startup_reconciliation.read_broker() result."""
    data = read.get("data") or {}
    gtts: dict[str, GttView] = {}
    unparsed = 0
    duplicates = []
    for raw in data.get("gtts") or []:
        view = parse_gtt(raw)
        if view is None:
            unparsed += 1
            continue
        if view.id in gtts:
            duplicates.append(view.id)     # the same id listed twice: ambiguous listing
            continue
        gtts[view.id] = view
    orders: dict[str, OrderView] = {}
    for raw in data.get("orders") or []:
        view = parse_order(raw)
        if view is not None:
            orders[view.id] = view
    complete = bool(read.get("complete")) and unparsed == 0 and not duplicates
    row = read.get("row") or {}
    return Evidence(
        read_id=read_id, account_id=account_id, purpose=purpose, epoch=epoch, started_at=started_at,
        gtts_at=parse_time(row.get("gtts_at")), finished_at=finished_at, complete=complete, gtts=gtts,
        orders=orders, held=read.get("held"), unparsed_gtts=unparsed, duplicate_ids=tuple(duplicates),
        started_mono=started_mono, gtts_mono=gtts_mono, raw=read,
        detail="" if complete else "incomplete read: " + json.dumps({k: row.get(k) for k in row if k.endswith("_error")
                                                                     and row.get(k)} | {"unparsed": unparsed}),
    )


def capture(conn, kite, account_id: int, purpose: str, *, clock=utc_now, monotonic=time.monotonic) -> Evidence:
    """One broker read, recorded as a broker_reads row in a fenced transaction (the read itself runs outside
    any transaction). A process without authority cannot record a read: FencingError propagates."""
    from backend import startup_reconciliation
    if purpose not in PURPOSES:
        raise ValueError(f"unknown read purpose {purpose!r}")
    started, started_mono = clock(), monotonic()
    read = startup_reconciliation.read_broker(kite, clock=clock, monotonic=monotonic)
    gtts_mono = monotonic()
    finished = clock()
    row = read["row"]
    with instance_lock.fenced_transaction(conn):
        epoch = instance_lock.owner_epoch()
        evidence = from_read(read, account_id=account_id, purpose=purpose, epoch=epoch, started_at=started,
                             finished_at=finished, started_mono=started_mono, gtts_mono=gtts_mono)
        read_id = conn.execute(
            "INSERT INTO broker_reads (account_id, purpose, instance_epoch, started_at, finished_at, orders_at, "
            "orders_error, orders_count, gtts_at, gtts_error, gtts_count, gtts_malformed, holdings_at, holdings_error, "
            "positions_at, positions_error, session_expired, complete) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?)",
            (account_id, purpose, epoch, iso(started), iso(finished), row["orders_at"], row["orders_error"],
             row["orders_count"], row["gtts_at"], row["gtts_error"], row["gtts_count"],
             row["gtts_malformed"] if row["gtts_malformed"] is not None else (0 if read["complete"] else None),
             row["holdings_at"], row["holdings_error"], row["positions_at"], row["positions_error"],
             row["session_expired"], 1 if evidence.complete else 0)).lastrowid
    evidence.read_id = read_id
    return evidence


def takeover_at(conn) -> datetime | None:
    """When the current epoch's owner started (app_instance.started_at): DD-8 rule iii's takeover bound."""
    row = conn.execute("SELECT started_at FROM app_instance WHERE id = 1").fetchone()
    return parse_time(row[0]) if row is not None else None


def freshness_problem(evidence: Evidence | None, *, now: datetime, takeover: datetime | None,
                      resumed: datetime | None = None, max_age: float | None = None,
                      epoch: int | None = None) -> str | None:
    """DD-8 rules i-iii. None = usable; otherwise why not. Never trusts the monotonic clock alone."""
    if evidence is None:
        return "NO_READ"
    if not evidence.complete or evidence.read_id is None:
        return "READ_INCOMPLETE"
    if epoch is not None and evidence.epoch != epoch:
        return "READ_FROM_ANOTHER_EPOCH"
    limit = config.PREWRITE_MAX_AGE_SECONDS if max_age is None else max_age
    age = (now - evidence.started_at).total_seconds()
    if age > limit or age < -60:
        return "READ_STALE"
    if takeover is None or evidence.started_at < takeover:
        return "READ_BEFORE_TAKEOVER"
    resumed = instance_lock.resumed_at() if resumed is None else resumed
    if resumed is not None and evidence.started_at < resumed:
        return "READ_BEFORE_RESUME"
    return None
