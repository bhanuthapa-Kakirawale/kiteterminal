"""LIVE-entry gate: the server-side refusal of every LIVE new entry while the
frozen order path places its protective GTT before the entry fills.

The defect (in backend/execution.py, frozen by the PK-25 manifest, not
changed here): _execute_live sends the entry order and, as soon as Kite
ACCEPTS it, places a sell GTT for the FULL ticket quantity. Acceptance is not
a fill. A GTT is not scoped to a position: it acts on the account's demat
holdings. A GTT that triggers before the entry fills, or that is larger than
what the entry actually bought, can sell shares the account already held.

Until a separately authorised execution.py change creates protection only
for confirmed filled quantity (docs/PROPOSAL_EXECUTION_GTT_AFTER_FILL.md),
LIVE_ENTRIES_ENABLED stays False. It is a code constant on purpose: no
environment variable, setting or route can lift it; only a reviewed commit.

Interim mitigations, which are NOT a fix and are documented as such, run
behind it (they matter only once the gate is lifted, and are tested with the
gate lifted in-test against FakeKite):
  check_market_regular_only     a LIVE entry must be a regular MARKET order
                                (no LIMIT, no AMO). Secondary only: a MARKET
                                order can still stay pending or fill partly.
  check_no_presellable_quantity no pre-existing sellable quantity of the
                                instrument anywhere in the account (holdings,
                                T1, collateral, positions, open orders, active
                                GTTs, other live app positions); fails closed.

Exits, Emergency Exit, protective-GTT upkeep, reconciliation, order
monitoring and dry run never come here: the gate refuses adding risk only.
"""

import math
import sqlite3
from datetime import datetime, timezone

from backend import execution
from backend.account_safety import SafetyFailure

# The hard gate. False until the execution.py GTT-before-fill defect is
# corrected and that change is separately authorised.
LIVE_ENTRIES_ENABLED = False

LIVE_ENTRIES_DISABLED = "LIVE_ENTRIES_DISABLED"
LIVE_ENTRY_ORDER_TYPE_UNSUPPORTED = "LIVE_ENTRY_ORDER_TYPE_UNSUPPORTED"
LIVE_AMO_UNSUPPORTED = "LIVE_AMO_UNSUPPORTED"
PRE_EXISTING_SELLABLE_QUANTITY = "PRE_EXISTING_SELLABLE_QUANTITY"
HOLDINGS_CHECK_UNAVAILABLE = "HOLDINGS_CHECK_UNAVAILABLE"

PROPOSAL_DOC = "docs/PROPOSAL_EXECUTION_GTT_AFTER_FILL.md"

REASON = (
    "LIVE entries are disabled on this terminal: the frozen order path (backend/execution.py) places the "
    "protective sell GTT for the full quantity as soon as Kite accepts the entry, before it fills, and a GTT "
    "acts on all of the account's holdings. Lifting this needs the separately authorised execution.py change "
    f"({PROPOSAL_DOC}). Nothing was executed. Dry run, exits and Emergency Exit are unaffected."
)


def entries_enabled() -> bool:
    return LIVE_ENTRIES_ENABLED is True


def is_live_entry(ticket, mode: str) -> bool:
    return mode == "live" and not execution.is_closing_sell(ticket)


def refusal(ticket, mode: str) -> SafetyFailure | None:
    """The hard gate: a LIVE new entry is refused while entries are disabled.
    Not permanent: the ticket stays approvable (in dry run, or once lifted)."""
    if not is_live_entry(ticket, mode) or entries_enabled():
        return None
    return SafetyFailure(LIVE_ENTRIES_DISABLED, REASON)


def check_market_regular_only(ticket, mode: str, variety: str | None) -> list[SafetyFailure]:
    """Interim: a LIVE entry must be a regular-session MARKET order. A LIMIT
    entry can rest unfilled for the whole session, and an AMO overnight, each
    with a full-quantity GTT already live. Not proof of safety: a MARKET order
    can still be pending or fill partly."""
    if not is_live_entry(ticket, mode):
        return []
    failures = []
    if ticket["entry_order_type"] != "MARKET":
        failures.append(SafetyFailure(
            LIVE_ENTRY_ORDER_TYPE_UNSUPPORTED,
            f"LIVE entries must be MARKET orders while the protective GTT is placed before the fill "
            f"(this ticket is {ticket['entry_order_type']}). A LIMIT entry can rest unfilled with a full-quantity "
            f"GTT live. Nothing was executed; change the ticket or use dry run."))
    if variety == "amo":
        failures.append(SafetyFailure(
            LIVE_AMO_UNSUPPORTED,
            "LIVE After-Market Order entries are not allowed while the protective GTT is placed before the fill: "
            "an AMO waits overnight with a full-quantity GTT live. Approve during market hours. Nothing was "
            "executed."))
    return failures


# ---- pre-existing sellable quantity ------------------------------------------------------
#
# A protective GTT placed for a NEW entry is a sell order on the instrument, not on the entry. If
# any quantity of that instrument is already sellable in the account, the GTT can sell it — before
# the entry fills, or beyond what the entry bought. So a LIVE entry is refused whenever anything of
# the instrument is already there, whoever owns it (an app position too), and whenever that cannot
# be established from fresh, consistent broker data.

# Kite holdings fields that are, or can become, sellable quantity of the demat holding. Pledged
# (collateral) and T1 (bought, not yet settled) shares count: both can be sold.
_HOLDING_QUANTITY_FIELDS = ("quantity", "t1_quantity", "collateral_quantity", "realised_quantity",
                            "opening_quantity", "authorised_quantity", "used_quantity")
_POSITION_QUANTITY_FIELDS = ("quantity", "overnight_quantity")
_TERMINAL_ORDER_STATUSES = {"COMPLETE", "CANCELLED", "REJECTED"}


class _Inconsistent(ValueError):
    pass


def _norm(symbol) -> str:
    return str(symbol or "").strip().upper()


def _quantity(row: dict, name: str, required: bool, where: str) -> float:
    value = row.get(name)
    if value is None:
        if required:
            raise _Inconsistent(f"{where}: '{name}' is missing")
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _Inconsistent(f"{where}: '{name}' is not a number ({value!r})")
    return float(value)


def _rows(payload, where: str) -> list[dict]:
    if not isinstance(payload, list) or not all(isinstance(r, dict) for r in payload):
        raise _Inconsistent(f"{where} is not a list of records")
    return payload


def _holdings_matches(holdings: list[dict], symbol: str) -> list[str]:
    found = []
    by_symbol = [h for h in holdings if _norm(h.get("tradingsymbol")) == symbol]
    # The same demat holding can be listed under another exchange's symbol: match its ISIN too.
    isins = {str(h["isin"]).strip().upper() for h in by_symbol if h.get("isin")}
    related = [h for h in holdings if h in by_symbol or (h.get("isin") and str(h["isin"]).strip().upper() in isins)]
    seen = set()
    for h in related:
        where = f"holding {h.get('exchange')}:{h.get('tradingsymbol')}"
        key = (_norm(h.get("exchange")), _norm(h.get("tradingsymbol")))
        if key in seen:
            raise _Inconsistent(f"{where} is listed more than once")
        seen.add(key)
        for name in _HOLDING_QUANTITY_FIELDS:
            qty = _quantity(h, name, name == "quantity", where)
            if qty < 0:
                raise _Inconsistent(f"{where}: '{name}' is negative ({qty:g})")
            if qty > 0:
                found.append(f"{where} {name}={qty:g}")
    return found


def _positions_matches(positions, symbol: str) -> list[str]:
    if not isinstance(positions, dict) or "net" not in positions:
        raise _Inconsistent("positions has no 'net' list")
    found = []
    for bucket in ("net", "day"):
        for p in _rows(positions.get(bucket) or [], f"positions.{bucket}"):
            if _norm(p.get("tradingsymbol")) != symbol:
                continue
            where = f"{bucket} position {p.get('exchange')}:{p.get('tradingsymbol')} {p.get('product')}"
            for name in _POSITION_QUANTITY_FIELDS:
                qty = _quantity(p, name, name == "quantity", where)
                if qty != 0:
                    found.append(f"{where} {name}={qty:g}")
    return found


def _orders_matches(orders: list[dict], symbol: str) -> list[str]:
    found = []
    for o in orders:
        if _norm(o.get("tradingsymbol")) != symbol:
            continue
        status = str(o.get("status") or "").upper()
        if not status:
            raise _Inconsistent(f"order {o.get('order_id')} has no status")
        if status not in _TERMINAL_ORDER_STATUSES:
            found.append(f"open order {o.get('order_id')} {o.get('transaction_type')} {o.get('quantity')} ({status})")
    return found


def _gtts_matches(gtts: list[dict], symbol: str) -> list[str]:
    found = []
    for g in gtts:
        condition = g.get("condition")
        if not isinstance(condition, dict):
            raise _Inconsistent(f"GTT {g.get('id')} has no condition")
        if _norm(condition.get("tradingsymbol")) != symbol:
            continue
        status = str(g.get("status") or "").lower()
        if not status:
            raise _Inconsistent(f"GTT {g.get('id')} has no status")
        if status == "active":
            found.append(f"active GTT {g.get('id')}")
    return found


def _app_matches(conn: sqlite3.Connection, account_id: int, ticket) -> list[str]:
    rows = conn.execute(
        "SELECT id, status, submission_state, entry_fill_status FROM trade_tickets "
        "WHERE account_id = ? AND mode = 'live' AND UPPER(symbol) = ? AND id != ? AND closed_at IS NULL "
        "AND (status = 'confirmed' OR (status = 'executed' AND NOT (transaction_type = 'SELL' AND "
        "(closes_ticket_id IS NOT NULL OR source = 'alert_action')))) ORDER BY id",
        (account_id, _norm(ticket["symbol"]), ticket["id"]),
    ).fetchall()
    return [f"app ticket #{r['id']} ({r['status']}, submission {r['submission_state']}, fill {r['entry_fill_status']})"
            for r in rows]


def check_no_presellable_quantity(conn: sqlite3.Connection, account_id: int, ticket, kite,
                                  mode: str) -> tuple[list[SafetyFailure], dict]:
    """Interim: refuses a LIVE entry when any quantity of its instrument is already sellable in the
    account, or could become sellable before its GTT exists. Four fresh broker reads, made here and
    nowhere cached (holdings, positions, orders, GTTs), plus the app's own live tickets. Any read
    error, malformed payload, missing or non-numeric quantity, negative quantity or duplicate row
    fails closed. Returns (failures, evidence)."""
    if not is_live_entry(ticket, mode):
        return [], {}
    symbol = _norm(ticket["symbol"])
    evidence = {"symbol": symbol, "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
    try:
        holdings = _rows(kite.holdings(), "holdings")
        positions = kite.positions()
        orders = _rows(kite.orders(), "orders")
        gtts = _rows(kite.get_gtts(), "GTT list")
        found = (_holdings_matches(holdings, symbol) + _positions_matches(positions, symbol)
                 + _orders_matches(orders, symbol) + _gtts_matches(gtts, symbol)
                 + _app_matches(conn, account_id, ticket))
    except Exception as e:
        evidence["error"] = f"{type(e).__name__}: {e}"
        return [SafetyFailure(
            HOLDINGS_CHECK_UNAVAILABLE,
            f"Whether {symbol} is already held could not be established from the broker ({type(e).__name__}: "
            f"{e}). A protective GTT could sell pre-existing shares, so nothing was executed.",
            http_status=503)], evidence
    evidence["found"] = found
    if not found:
        return [], evidence
    return [SafetyFailure(
        PRE_EXISTING_SELLABLE_QUANTITY,
        f"{symbol} already has sellable or pending quantity in this account ({'; '.join(found[:6])}). The "
        f"protective GTT placed for a new entry is a sell on the instrument, not on the entry, and could sell "
        f"those shares. Nothing was executed.")], evidence
