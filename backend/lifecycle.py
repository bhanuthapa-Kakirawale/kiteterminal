"""PK-19: the one place that decides what state an app trade is in.

Since PK-1..PK-18 a trade's economic state is spread over several columns
and tables: ticket status, entry_fill_status, submission_state,
filled_quantity, closed_at, kite_gtt_id, the exit that owns the position
(execution.EXIT_IN_FLIGHT_SQL) and unresolved reconciliation_log rows. This
module turns them into ONE server-derived description per position, so no
route handler and no line of frontend JavaScript re-implements those rules.

Two concepts are kept apart on purpose:

  ticket_state     the ticket / broker-order view ("executed", "placed ·
                   awaiting fill", "UNKNOWN — DO NOT RESUBMIT", ...). An
                   executed exit ticket is not a closed position (PK-8).
  lifecycle        the POSITION view: what the account holds through this
                   terminal and what is happening to it — one
                   lifecycle_state plus flags (protection, review, partial
                   fills) rather than dozens of composite states.

Everything here reads persisted, app-known state only: no broker call is
ever made to render a lifecycle. build_context() loads what a batch needs in
a constant number of queries; derive() and ticket_state() are pure.

Nothing here enforces anything. allowed_actions is UX guidance — the
confirm path (PK-1/3/4/8/12/13/17) remains the enforcement at action time.
"""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from backend import config, execution, paper_broker, position_quantity
from backend import reconciliation as rc

# ---- lifecycle states ---------------------------------------------------------

PENDING_REVIEW = "PENDING_REVIEW"          # draft: its risk checks have not completed
READY_TO_APPROVE = "READY_TO_APPROVE"      # pending_confirmation
SUBMITTING = "SUBMITTING"                  # claimed; the broker call is in progress
SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"  # PK-2: the order may or may not exist — never resubmit
ENTRY_OPEN = "ENTRY_OPEN"                  # entry order at the broker, nothing filled yet
ENTRY_PARTIAL = "ENTRY_PARTIAL"            # entry order still working, some shares filled
OPEN = "OPEN"                              # shares held, no exit at the broker
EXITING = "EXITING"                        # PK-8: exit order at the broker, nothing sold yet
EXIT_PARTIAL = "EXIT_PARTIAL"              # PK-13: some shares sold, the rest still open
CLOSED = "CLOSED"                          # broker-proven flat (closed_at)
REJECTED = "REJECTED"                      # refused (by the user, the risk checks or the broker) before any fill
CANCELLED = "CANCELLED"                    # entry order cancelled with nothing filled
FAILED = "FAILED"                          # never placed / execution failed
NEEDS_REVIEW = "NEEDS_REVIEW"              # the app cannot tell the economic state from its own data

STATES = (PENDING_REVIEW, READY_TO_APPROVE, SUBMITTING, SUBMISSION_UNKNOWN, ENTRY_OPEN, ENTRY_PARTIAL, OPEN,
          EXITING, EXIT_PARTIAL, CLOSED, REJECTED, CANCELLED, FAILED, NEEDS_REVIEW)

LABELS = {
    PENDING_REVIEW: "PENDING REVIEW", READY_TO_APPROVE: "READY TO APPROVE", SUBMITTING: "SUBMITTING",
    SUBMISSION_UNKNOWN: "UNKNOWN — DO NOT RESUBMIT", ENTRY_OPEN: "ENTRY WORKING", ENTRY_PARTIAL: "ENTRY PARTIAL",
    OPEN: "OPEN", EXITING: "EXITING", EXIT_PARTIAL: "EXIT PARTIAL", CLOSED: "CLOSED", REJECTED: "REJECTED",
    CANCELLED: "CANCELLED", FAILED: "FAILED", NEEDS_REVIEW: "NEEDS REVIEW",
}

# A position exists (or may exist) at the broker in these states: it can be
# protected, reviewed, exited. Everything else is either not yet a position
# or no longer one.
POSITION_STATES = frozenset({SUBMISSION_UNKNOWN, ENTRY_OPEN, ENTRY_PARTIAL, OPEN, EXITING, EXIT_PARTIAL,
                             NEEDS_REVIEW})
# Shares are (or may be) held: what "open" means for trailing, alerts, P&L.
HOLDING_STATES = frozenset({ENTRY_PARTIAL, OPEN, EXITING, EXIT_PARTIAL, NEEDS_REVIEW})
TERMINAL_STATES = frozenset({CLOSED, REJECTED, CANCELLED, FAILED})

# ---- protection ---------------------------------------------------------------

# Judged against the CONFIRMED filled open quantity, never the ordered one: an
# accepted order is not a fill, and a GTT acts on the account's holdings, not
# on the entry (the frozen order path places it before the fill).
#
# PROTECTED needs a confirmed BROKER OBSERVATION (backend/gtt_observations.py,
# or the paper broker's own book for a paper GTT) no older than
# config.GTT_OBSERVATION_MAX_AGE_SECONDS, of the right instrument, active,
# with every leg a SELL of exactly the quantity held. The app's own record of
# its GTT (kite_gtt_id, the quantity it placed or resized to) can only LOWER
# the verdict, never raise it: no observation is UNVERIFIED, an expired one
# STALE.
PROTECTED = "PROTECTED"                    # held > 0, a fresh broker observation covers exactly what is held
NOT_PROTECTED = "NOT_PROTECTED"            # held > 0, protection missing or incorrect (protection_reason says how)
UNVERIFIED = "UNVERIFIED"                  # held > 0, the GTT has not been observed at the broker
STALE = "STALE"                            # held > 0, the last observation is too old to prove anything
PENDING_FILL = "PENDING_FILL"              # entry accepted and working, nothing filled: no position yet
PROTECTION_UNKNOWN = "UNKNOWN"             # the position's quantity or submission cannot be established
NOT_REQUESTED = "NOT_REQUESTED"            # use_gtt = 0: the ticket asked for no protective GTT
NOT_APPLICABLE = "NOT_APPLICABLE"          # pre-PK-20 dry run, closed, or not a position yet

PROTECTION_STATUSES = (PROTECTED, NOT_PROTECTED, UNVERIFIED, STALE, PENDING_FILL, PROTECTION_UNKNOWN,
                       NOT_REQUESTED, NOT_APPLICABLE)
PROTECTION_LABELS = {
    PROTECTED: "PROTECTED", NOT_PROTECTED: "NOT PROTECTED", UNVERIFIED: "PROTECTION UNVERIFIED",
    STALE: "PROTECTION STALE", PENDING_FILL: "PENDING FILL", PROTECTION_UNKNOWN: "PROTECTION UNKNOWN",
    NOT_REQUESTED: "NO GTT (by choice)", NOT_APPLICABLE: "—",
}
# Every one of these is alerted; none is ever shown as protected.
PROTECTION_ALERT_STATUSES = frozenset({NOT_PROTECTED, UNVERIFIED, STALE, PENDING_FILL, PROTECTION_UNKNOWN})

# Why a position is NOT_PROTECTED (protection_reason). PENDING_FILL may carry
# GTT_BEFORE_FILL / NO_GTT as context.
NO_GTT = "NO_GTT"                                        # no protective GTT (or a broker finding says it is missing)
GTT_NOT_ACTIVE = "GTT_NOT_ACTIVE"                        # observed triggered / deleted / expired / missing
INSTRUMENT_MISMATCH = "INSTRUMENT_MISMATCH"              # the GTT is for another exchange / symbol / token
LEGS_INVALID = "LEGS_INVALID"                            # no legs, a non-SELL leg, an unreadable leg quantity
QUANTITY_EXCEEDS_POSITION = "QUANTITY_EXCEEDS_POSITION"  # it could sell more than is confirmed held
QUANTITY_BELOW_POSITION = "QUANTITY_BELOW_POSITION"      # it covers fewer shares than are held
UNRESOLVED_QUANTITY_FINDING = "UNRESOLVED_QUANTITY_FINDING"  # a resize failed / quantity finding still open
PRICE_MISMATCH = "PRICE_MISMATCH"                        # the broker trigger differs from the app's stop
GTT_BEFORE_FILL = "GTT_BEFORE_FILL"                      # PENDING_FILL: a sell GTT is already live at the broker

PROTECTION_REASON_LABELS = {
    NO_GTT: "no protective GTT", GTT_NOT_ACTIVE: "GTT not active at the broker",
    INSTRUMENT_MISMATCH: "GTT is for a different instrument", LEGS_INVALID: "GTT legs are not a valid sell",
    QUANTITY_EXCEEDS_POSITION: "GTT could sell more than is held",
    QUANTITY_BELOW_POSITION: "GTT covers fewer shares than are held",
    UNRESOLVED_QUANTITY_FINDING: "unresolved GTT quantity finding", PRICE_MISMATCH: "GTT stop price differs",
    GTT_BEFORE_FILL: "sell GTT live before the entry filled",
}

# ---- reconciliation findings ----------------------------------------------------
#
# issue_code values (PK-16's, plus the ones PK-19 adds at their write sites)
# and the short label shown for them. A row without an issue_code (written
# before PK-16, or by a path that records no code) is a generic finding —
# the frontend shows its detail text, it never interprets it.

GTT_MISSING = rc.GTT_MISSING
GTT_QUANTITY_TOO_HIGH = rc.GTT_QUANTITY_TOO_HIGH
GTT_QUANTITY_TOO_LOW = rc.GTT_QUANTITY_TOO_LOW
GTT_PRICE_MISMATCH = rc.GTT_PRICE_MISMATCH
PROTECTION_MISSING = rc.PROTECTION_MISSING
PROTECTION_QUANTITY_MISMATCH = rc.PROTECTION_QUANTITY_MISMATCH
POSITION_QUANTITY_REVIEW = rc.POSITION_QUANTITY_REVIEW
POSSIBLE_DOUBLE_SELL = rc.POSSIBLE_DOUBLE_SELL
ORPHANED_GTT_AFTER_EXIT = rc.ORPHANED_GTT_AFTER_EXIT
GTT_STAGED_BEFORE_FILL = rc.GTT_STAGED_BEFORE_FILL
GENERIC_FINDING = "RECONCILIATION_FINDING"

ISSUE_LABELS = {
    GTT_MISSING: "GTT missing at Kite",
    GTT_QUANTITY_TOO_HIGH: "GTT quantity too high",
    GTT_QUANTITY_TOO_LOW: "GTT quantity too low",
    GTT_PRICE_MISMATCH: "GTT stop price differs",
    PROTECTION_MISSING: "No active protective GTT",
    PROTECTION_QUANTITY_MISMATCH: "GTT not resized to the shares held",
    POSITION_QUANTITY_REVIEW: "Position quantity needs review",
    POSSIBLE_DOUBLE_SELL: "POSSIBLE DOUBLE SELL",
    ORPHANED_GTT_AFTER_EXIT: "Orphaned GTT after exit",
    GTT_STAGED_BEFORE_FILL: "GTT live before the entry filled",
    rc.ORPHAN_GTT: "Orphan GTT",
    rc.GTT_ON_CLOSED_POSITION: "GTT still active on closed position",
    rc.MULTIPLE_ACTIVE_GTTS: "Multiple active GTTs",
    rc.UNLINKED_POSSIBLE_GTT: "Unlinked GTT may be this stop",
    rc.AMBIGUOUS_GTT_OWNERSHIP: "Ambiguous GTT ownership",
    rc.BROKER_POSITION_SHORTFALL: "Broker quantity shortfall",
    rc.BROKER_QUANTITY_UNVERIFIABLE: "Broker quantity unverifiable",
    rc.GTT_CANCEL_FAILED: "GTT cancellation FAILED",
    rc.GTT_CANCEL_OUTCOME_UNKNOWN: "GTT cancellation outcome UNKNOWN",
    GENERIC_FINDING: "Reconciliation finding",
}

# The most dangerous findings: they can mean shares sold twice, or a stop
# that can fire against shares that are gone.
CRITICAL_ISSUES = frozenset({POSSIBLE_DOUBLE_SELL, ORPHANED_GTT_AFTER_EXIT, rc.GTT_ON_CLOSED_POSITION,
                             rc.ORPHAN_GTT, rc.MULTIPLE_ACTIVE_GTTS, rc.BROKER_POSITION_SHORTFALL,
                             # C4: a GTT the app tried to cancel may still be armed.
                             rc.GTT_CANCEL_FAILED, rc.GTT_CANCEL_OUTCOME_UNKNOWN})

# Findings recorded against no ticket but about a symbol: they concern every
# open app position in that symbol (PK-16 never assigns them to one).
# reconciliation_log has no exchange column: a symbol-scoped finding attaches to every open
# position in that symbol on any exchange (over-attaching is the conservative direction).
SYMBOL_SCOPED_ISSUES = frozenset({rc.BROKER_POSITION_SHORTFALL, rc.UNLINKED_POSSIBLE_GTT, rc.MULTIPLE_ACTIVE_GTTS,
                                  rc.AMBIGUOUS_GTT_OWNERSHIP, rc.BROKER_QUANTITY_UNVERIFIABLE})

_MISSING_CODES = frozenset({GTT_MISSING, PROTECTION_MISSING})
_QUANTITY_CODES = frozenset({GTT_QUANTITY_TOO_HIGH, GTT_QUANTITY_TOO_LOW, PROTECTION_QUANTITY_MISMATCH})

# ---- allowed actions (UX guidance only) -----------------------------------------

EXIT = "EXIT"                              # create a linked exit ticket for approval
REVIEW_EXIT = "REVIEW_EXIT"                # an exit ticket already awaits approval: open it
EMERGENCY_EXIT = "EMERGENCY_EXIT"          # covered by Emergency Exit All
CANCEL_ORDER = "CANCEL_ORDER"              # a working broker order can be cancelled (Orders page)
VIEW_RECONCILIATION = "VIEW_RECONCILIATION"
VIEW_JOURNAL = "VIEW_JOURNAL"
VIEW_DETAIL = "VIEW_DETAIL"

# Exit tickets not yet sent that still stand for this position.
_PENDING_EXIT_STATUSES = ("draft", "pending_confirmation", "risk_rejected")

_SQL_TIME = "%Y-%m-%d %H:%M:%S"


# ---- context ----------------------------------------------------------------------

@dataclass
class Context:
    """Everything derive() needs for a batch, loaded once."""
    tickets: dict[int, sqlite3.Row] = field(default_factory=dict)
    exits: dict[int, list] = field(default_factory=lambda: defaultdict(list))
    issues_by_ticket: dict[int, list] = field(default_factory=lambda: defaultdict(list))
    issues_by_symbol: dict[str, list] = field(default_factory=lambda: defaultdict(list))
    journal: dict[int, sqlite3.Row] = field(default_factory=dict)
    # PK-20: the paper broker's own order book, by paper order id — the
    # app's persisted simulated broker, so still no broker call.
    paper_orders: dict[str, sqlite3.Row] = field(default_factory=dict)
    # The paper broker's GTT book, by paper GTT id: for a paper position it IS
    # the broker's state of record, so reading it here is a broker observation.
    paper_gtts: dict[str, dict] = field(default_factory=dict)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def build_context(conn: sqlite3.Connection, account_id: int) -> Context:
    """Five queries, whatever the number of positions: every ticket of the
    account, the unresolved reconciliation findings, the journal and (PK-20)
    the paper order and GTT books. (The
    reconciliation log has no account column — the app has one trading
    account; its rows are matched to this account's tickets and symbols.)"""
    ctx = Context()
    for row in conn.execute("SELECT * FROM trade_tickets WHERE account_id = ? ORDER BY id", (account_id,)):
        ctx.tickets[row["id"]] = row
        if row["closes_ticket_id"] is not None:
            ctx.exits[row["closes_ticket_id"]].append(row)
    for row in conn.execute("SELECT * FROM reconciliation_log WHERE resolved = 0 ORDER BY id"):
        if row["ticket_id"] is not None:
            if row["ticket_id"] in ctx.tickets:
                ctx.issues_by_ticket[row["ticket_id"]].append(row)
        elif row["symbol"] and row["issue_code"] in SYMBOL_SCOPED_ISSUES:
            ctx.issues_by_symbol[row["symbol"]].append(row)
    for row in conn.execute("SELECT * FROM closed_trades WHERE account_id = ?", (account_id,)):
        ctx.journal[row["buy_ticket_id"]] = row
    for row in conn.execute(
        "SELECT paper_order_id, status, quantity, filled_quantity, average_price FROM paper_orders "
        "WHERE account_id = ?", (account_id,),
    ):
        ctx.paper_orders[row["paper_order_id"]] = row
    for row in conn.execute("SELECT * FROM paper_gtts WHERE account_id = ? AND paper_gtt_id IS NOT NULL",
                            (account_id,)):
        ctx.paper_gtts[row["paper_gtt_id"]] = paper_broker.gtt_dict(row)
    return ctx


def _paper_order(row, active_exit: dict | None, ctx: Context) -> dict | None:
    """PK-20: the paper order that matters for this position now — its
    working exit's, else its entry's — with the paper broker's own status.
    PARTIAL is how a working order with some shares filled is shown (Kite,
    and so the paper broker, report it as OPEN)."""
    order_id = active_exit["kite_order_id"] if active_exit and active_exit["kite_order_id"] else row["kite_order_id"]
    order = ctx.paper_orders.get(order_id) if paper_broker.is_paper_id(order_id) else None
    if order is None:
        return None
    status = order["status"]
    if status == "OPEN" and 0 < order["filled_quantity"] < order["quantity"]:
        status = "PARTIAL"
    elif status == paper_broker.AMO_PENDING:
        status = "AMO"
    return {"order_id": order_id, "side": "exit" if active_exit and order_id != row["kite_order_id"] else "entry",
            "status": status, "filled_quantity": order["filled_quantity"], "quantity": order["quantity"],
            "average_price": order["average_price"] or None}


# ---- predicates -------------------------------------------------------------------

def is_position_ticket(row) -> bool:
    """An entry: a ticket that opens (or would open) a position. Closing
    sells — linked exits, GTT closures, Quick Action reductions — are not."""
    return row["closes_ticket_id"] is None and not execution.is_closing_sell(row)


def exit_in_flight(row) -> bool:
    """The Python twin of execution.EXIT_IN_FLIGHT_SQL: the exit's order is
    still in flight and it owns its position (PK-4/PK-13)."""
    return row["status"] == "confirmed" or (
        row["status"] == "executed" and (row["entry_fill_status"] or "") in ("pending", "unconfirmed"))


def _submission_stale(row, now: datetime) -> bool:
    """A claim still 'submitting' long after any request could be in flight:
    the process died mid-submission (order_monitor treats it as unknown)."""
    if row["submission_state"] != execution.SUBMITTING or not row["submitted_at"]:
        return False
    try:
        at = datetime.strptime(row["submitted_at"][:19], _SQL_TIME).replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return now - at > timedelta(seconds=config.SUBMISSION_STALE_SECONDS)


def _submission_unknown(row, now: datetime) -> bool:
    return row["status"] == "confirmed" and (
        row["submission_state"] in (execution.UNKNOWN, execution.NEEDS_REVIEW) or _submission_stale(row, now))


def _legacy_closed(exits: list) -> bool:
    """risk_engine._CLOSED_BY_EXECUTED_EXIT for one position: an executed
    exit from before fill tracking closed it."""
    return any(x["status"] == "executed" and x["entry_fill_status"] is None for x in exits)


# ---- the position lifecycle -------------------------------------------------------

def _entry_state(row, exits: list, open_qty, now: datetime) -> tuple[str, dict]:
    """(lifecycle_state, extra detail) for an entry ticket."""
    status, sub, efs = row["status"], row["submission_state"], row["entry_fill_status"]
    if status == "draft":
        return PENDING_REVIEW, {}
    if status == "pending_confirmation":
        return READY_TO_APPROVE, {}
    if status in ("rejected", "risk_rejected"):
        return REJECTED, {"reason": "risk checks" if status == "risk_rejected" else "rejected"}
    if status == "cancelled":
        if efs == "rejected":
            return REJECTED, {"reason": "broker rejected the entry order"}
        return CANCELLED, {"reason": "entry order cancelled with nothing filled"}
    if status == "failed":
        if sub == execution.REJECTED:
            return REJECTED, {"reason": "broker rejected the order"}
        if sub == execution.NOT_FOUND:
            return FAILED, {"reason": "not placed — no broker order was found"}
        return FAILED, {"reason": "execution failed"}
    if status == "confirmed":
        if _submission_unknown(row, now):
            return SUBMISSION_UNKNOWN, {"unknown_scope": "entry"}
        return SUBMITTING, {}

    # status == 'executed': the order reached the broker.
    if row["closed_at"] is not None or _legacy_closed(exits):
        return CLOSED, {}
    owner = next((x for x in exits if exit_in_flight(x)), None)
    if owner is not None and _submission_unknown(owner, now):
        return SUBMISSION_UNKNOWN, {"unknown_scope": "exit"}
    if efs == "unconfirmed":
        return NEEDS_REVIEW, {"reason": "the entry order left the order book before its fill was confirmed"}
    if owner is not None and owner["status"] == "executed" and owner["entry_fill_status"] == "unconfirmed":
        return NEEDS_REVIEW, {"reason": "the exit order left the order book before its fill was confirmed"}
    if efs == "pending":
        return (ENTRY_PARTIAL if (position_quantity.filled(row) or 0) > 0 else ENTRY_OPEN), {}
    if open_qty is None:
        return NEEDS_REVIEW, {"reason": "open quantity unknown (a partial fill was never recorded)"}
    if open_qty < 0:
        return NEEDS_REVIEW, {"reason": f"exits sold more than the entry bought (open {open_qty})"}
    if owner is not None:
        return (EXIT_PARTIAL if (position_quantity.filled(owner) or 0) > 0 else EXITING), {}
    if open_qty == 0:
        return NEEDS_REVIEW, {"reason": "every share is sold but the position is not marked closed"}
    if (position_quantity.exits_filled_from_rows(exits) or 0) > 0:
        return EXIT_PARTIAL, {}
    return OPEN, {}


def _active_exit(exits: list, now: datetime) -> dict | None:
    owner = next((x for x in exits if exit_in_flight(x)), None)
    if owner is None:
        return None
    sold = position_quantity.filled(owner)
    if owner["status"] == "confirmed":
        phase = "UNKNOWN" if _submission_unknown(owner, now) else "SUBMITTING"
    elif owner["entry_fill_status"] == "unconfirmed":
        phase = "UNCONFIRMED"
    else:
        phase = "PARTIAL" if (sold or 0) > 0 else "WORKING"
    return {
        "ticket_id": owner["id"], "phase": phase, "source": owner["source"],
        "strategy_name": owner["strategy_name"],
        "requested_quantity": owner["quantity"], "filled_quantity": sold,
        "remaining_quantity": None if sold is None else owner["quantity"] - sold,
        "kite_order_id": owner["kite_order_id"], "order_variety": owner["order_variety"],
        "order_tag": owner["order_tag"], "submission_state": owner["submission_state"],
    }


def _issue(row) -> dict:
    code = row["issue_code"] or GENERIC_FINDING
    return {
        "id": row["id"], "code": code, "label": ISSUE_LABELS.get(code, code.replace("_", " ").capitalize()),
        "severity": "critical" if code in CRITICAL_ISSUES else (row["severity"] or "urgent"),
        "detail": row["detail"], "suggested_action": row["suggested_action"], "checked_at": row["checked_at"],
        "expected_quantity": row["expected_quantity"], "broker_quantity": row["broker_quantity"],
    }


def _sql_utc(text) -> datetime | None:
    try:
        return datetime.strptime(str(text)[:19], _SQL_TIME).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def gtt_observation(row, ctx: Context | None = None) -> tuple[dict | None, str | None]:
    """(observation, source) for the position's GTT: what a broker READ last
    reported (backend/gtt_observations.py), or for a paper GTT the paper
    broker's own book (its state of record, read for this context). (None,
    None) when the GTT has never been observed: the app's record of it is not
    an observation."""
    from backend import gtt_observations
    gtt_id = row["kite_gtt_id"]
    if gtt_id is None:
        return None, None
    if paper_broker.is_paper_id(gtt_id):
        if ctx is None:
            return None, None
        now = ctx.now.strftime(_SQL_TIME)
        if gtt_id in ctx.paper_gtts:
            return gtt_observations.observation(ctx.paper_gtts[gtt_id], now), "paper_broker"
        # The whole paper book was read for this context: the GTT is not in it.
        return {"status": gtt_observations.MISSING, "exchange": None, "tradingsymbol": None,
                "instrument_token": None, "legs": [], "quantity": None, "observed_at": now}, "paper_broker"
    seen = gtt_observations.get(gtt_id)
    return (seen, "broker") if seen is not None else (None, None)


def _observation_age(seen: dict, now: datetime) -> float | None:
    at = _sql_utc(seen.get("observed_at"))
    return None if at is None else max(0.0, (now - at).total_seconds())


def gtt_view(row, desired, ctx: Context | None = None) -> dict:
    """What is known about the position's GTT, keeping the broker's observation and the
    app's own record apart: gtt_status / gtt_quantity come ONLY from an observation
    (None when unobserved); gtt_recorded_quantity is what the app placed or resized it to."""
    recorded = desired if row["kite_gtt_id"] is not None else None
    seen, source = gtt_observation(row, ctx)
    now = ctx.now if ctx is not None else datetime.now(timezone.utc)
    if seen is None:
        return {"gtt_status": None, "gtt_quantity": None, "gtt_observed_at": None, "gtt_source": None,
                "gtt_observation_age_seconds": None, "gtt_recorded_quantity": recorded, "_seen": None}
    age = _observation_age(seen, now)
    return {"gtt_status": seen["status"], "gtt_quantity": seen["quantity"], "gtt_observed_at": seen["observed_at"],
            "gtt_source": source, "gtt_observation_age_seconds": None if age is None else int(age),
            "gtt_recorded_quantity": recorded, "_seen": seen}


def _norm(value) -> str | None:
    text = str(value).strip().upper() if value is not None else ""
    return text or None


def _instrument_matches(row, seen: dict) -> bool:
    if (_norm(seen.get("exchange")) != _norm(row["exchange"])
            or _norm(seen.get("tradingsymbol")) != _norm(row["symbol"])):
        return False
    token, mine = seen.get("instrument_token"), row["instrument_token"]
    if token and mine and int(token) != int(mine):
        return False
    for leg in seen.get("legs") or []:
        for name, expected in (("exchange", row["exchange"]), ("tradingsymbol", row["symbol"])):
            if leg.get(name) is not None and _norm(leg[name]) != _norm(expected):
                return False
    return True


def _protection(row, state: str, ticket_issue_codes: set, desired, held=None, gtt: dict | None = None,
                now: datetime | None = None) -> tuple[str, str | None]:
    """(protection_status, protection_reason). `held` is the CONFIRMED filled open quantity
    (bought minus sold); `desired` the app's record of what its GTT covers. PROTECTED only
    when shares are held and a fresh broker observation of an active GTT on this instrument
    has every leg a SELL of exactly `held`. The app's record never makes a position
    protected: it can only lower the verdict."""
    if state not in POSITION_STATES or not position_quantity.broker_tracked(row):
        # A dry-run position from before PK-20 never had a GTT; a paper
        # position's paper GTT is judged exactly like a live one's.
        return NOT_APPLICABLE, None
    if state == SUBMISSION_UNKNOWN:
        return PROTECTION_UNKNOWN, None
    if not row["use_gtt"]:
        return NOT_REQUESTED, None
    if held is None or held < 0:
        return PROTECTION_UNKNOWN, None
    gtt = gtt if gtt is not None else gtt_view(row, desired)
    seen = gtt.get("_seen")
    active = seen is not None and seen.get("status") == "active"
    if held == 0:
        if row["entry_fill_status"] == "pending":
            # An accepted, working entry with nothing filled: not a position yet, so never
            # reported as an unprotected one. A sell GTT already live can still sell other
            # holdings: GTT_BEFORE_FILL says so.
            return PENDING_FILL, (GTT_BEFORE_FILL if row["kite_gtt_id"] is not None else NO_GTT)
        if active and (seen.get("quantity") or 0) > 0:
            return NOT_PROTECTED, QUANTITY_EXCEEDS_POSITION   # nothing held, yet a sell is armed
        return PROTECTION_UNKNOWN, None

    now = now or datetime.now(timezone.utc)
    if row["kite_gtt_id"] is None or ticket_issue_codes & _MISSING_CODES:
        return NOT_PROTECTED, NO_GTT
    if seen is None:
        return UNVERIFIED, None
    if not active:
        # Kite GTT statuses other than 'active' never revert: true however old the observation.
        return NOT_PROTECTED, GTT_NOT_ACTIVE
    age = _observation_age(seen, now)
    if age is None or age > config.GTT_OBSERVATION_MAX_AGE_SECONDS:
        return STALE, None
    if not _instrument_matches(row, seen):
        return NOT_PROTECTED, INSTRUMENT_MISMATCH
    legs = seen.get("legs") or []
    if not legs or any(leg.get("transaction_type") != "SELL" or leg.get("quantity") is None
                       or leg["quantity"] <= 0 for leg in legs):
        return NOT_PROTECTED, LEGS_INVALID
    quantities = {leg["quantity"] for leg in legs}
    if max(quantities) > held:
        return NOT_PROTECTED, QUANTITY_EXCEEDS_POSITION
    if min(quantities) < held:
        return NOT_PROTECTED, QUANTITY_BELOW_POSITION
    if ticket_issue_codes & _QUANTITY_CODES:
        return NOT_PROTECTED, UNRESOLVED_QUANTITY_FINDING
    if GTT_PRICE_MISMATCH in ticket_issue_codes:
        return NOT_PROTECTED, PRICE_MISMATCH
    return PROTECTED, None


def derive(row, ctx: Context) -> dict:
    """The lifecycle of the position an ENTRY ticket opens (or opened)."""
    exits = ctx.exits.get(row["id"], [])
    executed = row["status"] == "executed"
    closed = executed and (row["closed_at"] is not None or _legacy_closed(exits))
    bought = position_quantity.filled(row)
    if not executed:
        open_qty = None
    elif closed:
        open_qty = 0
    else:
        open_qty = position_quantity.open_quantity_from_rows(row, exits)
    sold = position_quantity.exits_filled_from_rows(exits) if executed else None

    state, extra = _entry_state(row, exits, open_qty, ctx.now)
    active_exit = _active_exit(exits, ctx.now) if executed and not closed else None
    pending_exit = next((x for x in reversed(exits)
                         if x["status"] in _PENDING_EXIT_STATUSES and not x["archived"]), None)

    ticket_issues = [_issue(r) for r in ctx.issues_by_ticket.get(row["id"], [])]
    symbol_issues = ([_issue(r) for r in ctx.issues_by_symbol.get(row["symbol"], [])]
                     if state in POSITION_STATES else [])
    issues = ticket_issues + symbol_issues
    ticket_codes = {i["code"] for i in ticket_issues}

    desired = (position_quantity.protection_quantity_from_rows(row, exits)
               if executed and not closed else None)
    gtt = gtt_view(row, desired, ctx)
    protection, protection_reason = _protection(row, state, ticket_codes, desired, open_qty, gtt, ctx.now)

    reasons = []
    if state == SUBMISSION_UNKNOWN:
        reasons.append("SUBMISSION_UNKNOWN")
    if state == NEEDS_REVIEW:
        reasons.append("NEEDS_REVIEW")
    if protection in PROTECTION_ALERT_STATUSES:
        reasons.append(f"PROTECTION_{protection}")
    reasons.extend(sorted({i["code"] for i in issues}))
    review_required = bool(reasons)

    if (state in (SUBMISSION_UNKNOWN, NEEDS_REVIEW)
            or protection == NOT_PROTECTED
            or any(i["severity"] == "critical" for i in issues)):
        severity = "critical"
    elif review_required or state in (ENTRY_PARTIAL, EXITING, EXIT_PARTIAL):
        severity = "warning"
    elif state in (PENDING_REVIEW, READY_TO_APPROVE, SUBMITTING, ENTRY_OPEN):
        severity = "info"
    elif state == OPEN:
        severity = "ok"
    else:
        severity = "muted"

    journal = ctx.journal.get(row["id"]) if closed else None
    actions = _allowed_actions(row, state, open_qty, active_exit, pending_exit, issues, journal)

    return {
        "position_id": row["id"],
        "symbol": row["symbol"], "exchange": row["exchange"], "instrument_token": row["instrument_token"],
        "strategy_name": row["strategy_name"], "source": row["source"], "product_type": row["product_type"],
        # PK-3: the position's own, immutable mode — never the global switch.
        "mode": row["mode"],
        # PK-20: this position's orders and GTT are the paper broker's.
        "paper": paper_broker.is_paper_id(row["kite_order_id"]),
        "paper_order": _paper_order(row, active_exit, ctx),
        "lifecycle_state": state, "lifecycle_label": LABELS[state], "severity": severity,
        "state_reason": extra.get("reason"), "unknown_scope": extra.get("unknown_scope"),
        "is_position": state in POSITION_STATES, "is_holding": state in HOLDING_STATES,
        "is_closed": state == CLOSED, "is_terminal": state in TERMINAL_STATES,
        # Quantities (PK-13), never recomputed by the frontend.
        "requested_quantity": row["quantity"], "filled_quantity": bought,
        "open_quantity": open_qty, "exited_quantity": sold,
        "entry_partial": bool(executed and row["entry_fill_status"] == "partial"
                              and bought is not None and bought < row["quantity"]),
        "exit_requested_quantity": active_exit["requested_quantity"] if active_exit else None,
        "exit_filled_quantity": active_exit["filled_quantity"] if active_exit else None,
        "remaining_quantity": active_exit["remaining_quantity"] if active_exit else None,
        "entry_fill_price": row["filled_price"] if executed and (bought or 0) > 0 else None,
        "entry_reference_price": row["entry_price"],
        "current_stop": row["current_stop_loss"] or row["stop_loss"], "target": row["target_1"],
        "trailing_enabled": bool(row["trailing_enabled"]), "trailing_active": bool(row["trailing_active"]),
        "protection_status": protection, "protection_label": PROTECTION_LABELS[protection],
        "protection_reason": protection_reason,
        "protection_reason_label": PROTECTION_REASON_LABELS.get(protection_reason) if protection_reason else None,
        "protection_quantity": desired, "kite_gtt_id": row["kite_gtt_id"],
        # The GTT as last OBSERVED at the broker (None when it never was), the app's own
        # record of it, and what is confirmed held: the three are never conflated.
        "gtt_status": gtt["gtt_status"], "gtt_quantity": gtt["gtt_quantity"],
        "gtt_observed_at": gtt["gtt_observed_at"], "gtt_source": gtt["gtt_source"],
        "gtt_observation_age_seconds": gtt["gtt_observation_age_seconds"],
        "gtt_observation_max_age_seconds": config.GTT_OBSERVATION_MAX_AGE_SECONDS,
        "gtt_recorded_quantity": gtt["gtt_recorded_quantity"],
        "confirmed_filled_quantity": bought, "protection_alert": protection in PROTECTION_ALERT_STATUSES,
        "kite_order_id": row["kite_order_id"], "order_tag": row["order_tag"],
        "submission_state": row["submission_state"],
        "active_exit": active_exit,
        "pending_exit_ticket_id": pending_exit["id"] if pending_exit else None,
        "review_required": review_required, "attention_reasons": reasons,
        "reconciliation_issues": issues,
        "opened_at": row["filled_at"] or row["executed_at"], "closed_at": row["closed_at"],
        "closed_trade": _closed_trade(journal),
        "allowed_actions": actions,
        # SEC-1 P1-E: the entry order's broker acceptance and actual fill,
        # stated separately (an accepted order is not a filled one).
        "entry_progress": order_progress(row),
    }


def _allowed_actions(row, state: str, open_qty, active_exit, pending_exit, issues, journal) -> list[str]:
    actions = []
    exitable = (state in (OPEN, EXIT_PARTIAL) and active_exit is None and open_qty is not None and open_qty > 0
                and row["mode"] in ("live", "dry_run"))
    if exitable and pending_exit is None:
        actions.append(EXIT)
    if pending_exit is not None and state in POSITION_STATES:
        actions.append(REVIEW_EXIT)
    # Emergency Exit covers live positions it would act on: not one already
    # exiting (already_exiting) or of unknown outcome (exit_outcome_unknown).
    if (row["mode"] == "live" and active_exit is None
            and state in (ENTRY_OPEN, ENTRY_PARTIAL, OPEN, EXIT_PARTIAL)):
        actions.append(EMERGENCY_EXIT)
    # PK-20: a working paper order is cancelled through the paper broker,
    # exactly like a live one through Kite (the monitor still resolves it).
    entry_working = state in (ENTRY_OPEN, ENTRY_PARTIAL) and position_quantity.broker_tracked(row)
    exit_working = (active_exit is not None and active_exit["phase"] in ("WORKING", "PARTIAL")
                    and (row["mode"] == "live" or paper_broker.is_paper_id(active_exit["kite_order_id"])))
    if entry_working or exit_working:
        actions.append(CANCEL_ORDER)
    if issues:
        actions.append(VIEW_RECONCILIATION)
    if journal is not None:
        actions.append(VIEW_JOURNAL)
    actions.append(VIEW_DETAIL)
    return actions


def _closed_trade(journal) -> dict | None:
    if journal is None:
        return None
    return {
        "journal_id": journal["id"], "quantity": journal["quantity"], "entry_price": journal["entry_price"],
        "average_exit_price": journal["exit_price"], "realized_pnl": journal["realized_pnl"],
        "realized_pnl_pct": journal["realized_pnl_pct"], "r_multiple": journal["r_multiple_realized"],
        "exit_reason": journal["exit_reason"], "entry_date": journal["entry_date"],
        "exit_date": journal["exit_date"], "sell_ticket_id": journal["sell_ticket_id"],
    }


def for_ticket(row, ctx: Context) -> dict | None:
    """The position lifecycle a ticket belongs to: an entry's own, an exit's
    position's. None for a reduction of a holding the app never opened."""
    if is_position_ticket(row):
        return derive(row, ctx)
    position = ctx.tickets.get(row["closes_ticket_id"]) if row["closes_ticket_id"] is not None else None
    return derive(position, ctx) if position is not None else None


# ---- the ticket / order view ------------------------------------------------------

_STATUS_LABELS = {"pending_confirmation": "awaiting approval", "risk_rejected": "risk rejected"}


def ticket_state(row, now: datetime | None = None) -> dict:
    """What happened to THIS ticket and its broker order — distinct from the
    position lifecycle. {code, label, tone, css, hint}: the frontend renders
    it, it never decides it. Needs only the row."""
    now = now or datetime.now(timezone.utc)
    status, sub, efs = row["status"], row["submission_state"], row["entry_fill_status"]
    filled = position_quantity.filled(row)

    def state(code, label, tone, css, hint=""):
        return {"code": code, "label": label, "tone": tone, "css": css, "hint": hint}

    if status == "confirmed" and sub == execution.NEEDS_REVIEW:
        return state("NEEDS_REVIEW", "NEEDS REVIEW — CHECK KITE", "critical", "submission_unknown",
                     "Broker orders for this ticket are ambiguous — resolve it in Kite. Nothing will be resubmitted.")
    if status == "confirmed" and (sub == execution.UNKNOWN or _submission_stale(row, now)):
        return state("UNKNOWN", "UNKNOWN — DO NOT RESUBMIT", "critical", "submission_unknown",
                     "The order may or may not exist at the broker. The app is searching Kite for it by its order "
                     "tag.")
    if status == "failed" and sub == execution.NOT_FOUND:
        return state("NOT_PLACED", "not placed · no broker order", "muted", "failed",
                     "No broker order with this ticket's tag appeared — nothing was placed.")
    exit_state = execution.exit_state(row)
    if exit_state == "exiting":
        if (filled or 0) > 0:
            return state("EXIT_WORKING_PARTIAL", f"EXITING · {filled}/{row['quantity']} sold", "warning", "exiting",
                         "The exit order is still working and has sold part of its quantity. The rest of the "
                         "position stays open and protected.")
        return state("EXITING", "EXITING · awaiting fill", "warning", "exiting",
                     "Exit order placed with the broker but not filled. The position stays open and keeps its "
                     "protective GTT until Kite reports the fill.")
    if exit_state == "exit_partial":
        return state("EXIT_PARTIAL", f"PARTIAL EXIT · {filled if filled is not None else '?'}/{row['quantity']} sold",
                     "warning", "submission_unknown",
                     "The exit order ended after a partial fill. Those shares are sold; the rest of the position is "
                     "still OPEN and protected, and can be exited again. Nothing will be resubmitted "
                     "automatically.")
    if exit_state == "exit_unconfirmed":
        return state("EXIT_UNCONFIRMED", "EXIT UNCONFIRMED — NEEDS REVIEW", "critical", "submission_unknown",
                     "The exit's fill could not be confirmed. The position is NOT closed — check Kite. Nothing "
                     "will be resubmitted.")
    if exit_state == "exited":
        return state("EXIT_FILLED", "exit filled", "muted", "executed", "The exit order filled in full.")
    if status == "executed" and efs == "pending" and (filled or 0) > 0:
        return state("ORDER_PARTIAL", f"PARTIAL FILL · {filled}/{row['quantity']} · working", "warning", "exiting",
                     "The order is still working at the broker and has filled part of its quantity. The position "
                     "holds only the filled shares; its protective GTT covers exactly those.")
    if status == "executed" and efs == "pending":
        return state("ORDER_WORKING", "placed · awaiting fill", "info", "awaiting_fill",
                     "Order placed with the broker — waiting for the fill")
    if status == "executed" and efs == "unconfirmed":
        return state("FILL_UNCONFIRMED", "FILL UNCONFIRMED — NEEDS REVIEW", "critical", "submission_unknown",
                     "The order left the broker's order book before its fill was confirmed — verify it in Kite.")
    if status == "executed" and efs == "partial" and row["closes_ticket_id"] is None:
        return state("PARTIAL_FILL", f"PARTIAL FILL · {filled if filled is not None else '?'}/{row['quantity']}",
                     "warning", "exiting",
                     "The order ended after a partial fill. The position is the filled shares only; the rest will "
                     "not be bought.")
    label = _STATUS_LABELS.get(status, status.replace("_", " "))
    tone = {"executed": "ok", "pending_confirmation": "info", "draft": "info", "confirmed": "info"}.get(status, "muted")
    return state(status.upper(), label, tone, status)


# ---- SEC-1 P1-E: broker acceptance vs actual fill ---------------------------------
#
# A ticket's stored status 'executed' has always meant "the broker ACCEPTED the
# order" (PK-8): it says nothing about how much of it filled. order_progress()
# states the two separately, derived only from what is persisted — the
# submission bookkeeping (PK-1/PK-2), the broker order id, and the fill the
# order monitor recorded from the broker's own order history (PK-8/PK-13). It
# acquires nothing, changes nothing and never infers a fill from an
# acknowledgement; where the record cannot say, it says so.

# Was the order accepted by the broker?
ACC_NOT_SUBMITTED = "NOT_SUBMITTED"      # never sent (awaiting approval, refused, withdrawn)
ACC_SUBMITTING = "SUBMITTING"            # claimed, broker call in flight
ACC_UNKNOWN = "UNKNOWN"                  # outcome unknown — may exist at the broker; never resubmitted
ACC_NEEDS_REVIEW = "NEEDS_REVIEW"        # contradictory broker evidence — resolve in Kite
ACC_ACCEPTED = "ACCEPTED"                # the broker holds (or held) this order
ACC_REJECTED = "REJECTED"                # definitively rejected at submission
ACC_NOT_FOUND = "NOT_FOUND"              # proven never to have reached the broker
ACC_NOT_RECORDED = "NOT_RECORDED"        # a legacy row with no submission record

# How much of it filled — broker-reported only.
FILL_NONE = "NONE"                       # nothing was sent, so nothing can fill
FILL_UNKNOWN = "UNKNOWN"                 # the order may exist; its fill is not known
FILL_AWAITING = "AWAITING_FILL"          # accepted, working, 0 filled so far
FILL_PARTIAL = "PARTIAL"                 # part filled (still working, or ended)
FILL_FILLED = "FILLED"                   # the whole quantity filled
FILL_CANCELLED = "CANCELLED"             # accepted, then cancelled with nothing filled
FILL_REJECTED = "REJECTED"               # accepted, then rejected by the exchange/RMS
FILL_UNCONFIRMED = "UNCONFIRMED"         # left the order book before its fill was confirmed
FILL_NOT_RECORDED = "NOT_RECORDED"       # a legacy row with no fill record

# Where the filled quantity comes from.
EVIDENCE_BROKER = "BROKER"               # the order monitor's record of the broker's order history
EVIDENCE_LEGACY = "LEGACY_RECORD"        # a pre-fill-tracking row (migration backfill rule), not re-verified


def order_progress(row, now: datetime | None = None) -> dict:
    """{acceptance, fill, filled_quantity, requested_quantity, working,
    simulated, fill_evidence} for one ticket row. filled_quantity is None
    whenever the record cannot say (an unknown submission, a legacy row with
    no fill record) — never assumed from an acknowledgement."""
    now = now or datetime.now(timezone.utc)
    status, sub, efs = row["status"], row["submission_state"], row["entry_fill_status"]
    order_id = row["kite_order_id"]
    simulated = row["mode"] == "dry_run" or paper_broker.is_paper_id(order_id)

    def progress(acceptance, fill, filled, working=False, evidence=None):
        return {"acceptance": acceptance, "fill": fill, "filled_quantity": filled,
                "requested_quantity": row["quantity"], "working": working, "simulated": simulated,
                "fill_evidence": evidence}

    # Not (yet) at the broker.
    if status in ("draft", "pending_confirmation", "risk_rejected"):
        return progress(ACC_NOT_SUBMITTED, FILL_NONE, 0)
    if status == "confirmed":
        if sub == execution.NEEDS_REVIEW:
            return progress(ACC_NEEDS_REVIEW, FILL_UNKNOWN, None)
        if sub == execution.UNKNOWN or _submission_stale(row, now):
            return progress(ACC_UNKNOWN, FILL_UNKNOWN, None)
        if sub == execution.SUBMITTING:
            return progress(ACC_SUBMITTING, FILL_UNKNOWN, None)
        return progress(ACC_NOT_RECORDED, FILL_UNKNOWN, None)
    if sub == execution.NOT_FOUND:
        return progress(ACC_NOT_FOUND, FILL_NONE, 0)
    if order_id is None:
        if status in ("rejected", "cancelled") and sub is None:
            return progress(ACC_NOT_SUBMITTED, FILL_NONE, 0)            # withdrawn before any broker call
        if sub == execution.REJECTED:
            return progress(ACC_REJECTED, FILL_NONE, 0)
        if status == "failed":
            return progress(ACC_NOT_RECORDED, FILL_NOT_RECORDED, None)    # legacy failure, outcome unrecorded
        return progress(ACC_NOT_SUBMITTED, FILL_NONE, 0)

    # The broker accepted it (it has an order id): what filled is what the
    # order monitor recorded from the broker's order history.
    filled = row["filled_quantity"]
    if efs == "pending":
        if (filled or 0) > 0:
            return progress(ACC_ACCEPTED, FILL_PARTIAL, filled, working=True, evidence=EVIDENCE_BROKER)
        return progress(ACC_ACCEPTED, FILL_AWAITING, 0, working=True, evidence=EVIDENCE_BROKER)
    if efs == "partial":
        return progress(ACC_ACCEPTED, FILL_PARTIAL, filled, evidence=EVIDENCE_BROKER)
    if efs == "filled":
        return progress(ACC_ACCEPTED, FILL_FILLED, filled if filled is not None else row["quantity"],
                        evidence=EVIDENCE_BROKER)
    if efs == "unconfirmed":
        return progress(ACC_ACCEPTED, FILL_UNCONFIRMED, filled, evidence=EVIDENCE_BROKER)
    if efs == "cancelled":
        return progress(ACC_ACCEPTED, FILL_CANCELLED, filled or 0, evidence=EVIDENCE_BROKER)
    if efs == "rejected":
        return progress(ACC_ACCEPTED, FILL_REJECTED, filled or 0, evidence=EVIDENCE_BROKER)
    # A row from before fill tracking (no entry_fill_status).
    if filled is not None:
        fill = FILL_FILLED if filled == row["quantity"] else FILL_PARTIAL
        return progress(ACC_ACCEPTED, fill, filled, evidence=EVIDENCE_LEGACY)
    return progress(ACC_ACCEPTED, FILL_NOT_RECORDED, None)


# ---- batch helpers for routes ------------------------------------------------------

def serialize_fields(row, ctx: Context) -> dict:
    """The PK-19 fields every ticket payload carries."""
    return {"ticket_state": ticket_state(row, ctx.now), "lifecycle": for_ticket(row, ctx)}


def positions(ctx: Context, view: str) -> list[dict]:
    """App-managed positions for the Positions view. 'active': every entry
    that is (or may be) a position at the broker — waiting to fill, UNKNOWN,
    open, exiting, needs review. 'closed': broker-proven closed ones, newest
    first. Tickets that never became a position (drafts, approvals,
    rejections) are not positions and appear in neither."""
    out = []
    for row in ctx.tickets.values():
        if not is_position_ticket(row) or row["status"] not in ("confirmed", "executed"):
            continue
        lc = derive(row, ctx)
        if view == "closed" and lc["lifecycle_state"] == CLOSED:
            out.append(lc)
        elif view == "active" and lc["lifecycle_state"] in POSITION_STATES | {SUBMITTING}:
            out.append(lc)
    if view == "closed":
        out.sort(key=lambda p: (p["closed_at"] or "", p["position_id"]), reverse=True)
    else:
        rank = {"critical": 0, "warning": 1, "info": 2, "ok": 3, "muted": 4}
        out.sort(key=lambda p: (rank[p["severity"]], p["symbol"], p["position_id"]))
    return out


def summary(active: list[dict]) -> dict:
    """Header counts for the active view."""
    # Disjoint buckets: a position whose exit order is working counts as
    # exiting, not also as open.
    return {
        "open": sum(p["lifecycle_state"] in (OPEN, EXIT_PARTIAL) and p["active_exit"] is None for p in active),
        "waiting_to_fill": sum(p["lifecycle_state"] in (SUBMITTING, ENTRY_OPEN, ENTRY_PARTIAL) for p in active),
        "exiting": sum(p["lifecycle_state"] in (EXITING, EXIT_PARTIAL) and p["active_exit"] is not None
                       for p in active),
        "needs_attention": sum(p["review_required"] for p in active),
        "unprotected": sum(p["protection_status"] == NOT_PROTECTED for p in active),
        "unverified": sum(p["protection_status"] in (UNVERIFIED, STALE) for p in active),
        "protection_attention": sum(p["protection_status"] in PROTECTION_ALERT_STATUSES for p in active),
        "total": len(active),
    }
