import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException, TokenException

from backend import execution, sector_mapping

IST = timezone(timedelta(hours=5, minutes=30))


class RiskCheckUnavailableError(Exception):
    """Raised when the risk check itself can't run (Kite API failure) —
    distinct from a ticket failing a check. Callers translate this into an
    HTTP error rather than a risk_rejected ticket, since the ticket was
    never actually evaluated."""

    def __init__(self, status_code: int, error_code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message


@dataclass
class PortfolioContext:
    portfolio_value: float
    holdings_by_symbol: dict  # symbol -> {quantity, average_price, cost_value, last_price}


def build_portfolio_context(kite: KiteConnect) -> PortfolioContext:
    try:
        holdings = kite.holdings()
        margins = kite.margins()
    except TokenException:
        raise RiskCheckUnavailableError(
            401, "SESSION_EXPIRED", "Your Zerodha session has expired. Please reconnect."
        )
    except KiteException as e:
        raise RiskCheckUnavailableError(502, "KITE_API_ERROR", str(e))

    # Mark-to-market, matching the dashboard's existing "Portfolio Value" and
    # "Available Margin" (equity.net) figures exactly, so the risk engine's
    # numbers agree with what's already shown on screen.
    holdings_value = sum(h["quantity"] * h["last_price"] for h in holdings)
    available_margin = (margins.get("equity") or {}).get("net") or 0
    portfolio_value = holdings_value + available_margin

    holdings_by_symbol = {
        h["tradingsymbol"]: {
            "quantity": h["quantity"],
            "average_price": h["average_price"],
            # Cost basis, not mark-to-market — this is added to a new ticket's
            # own capital (entry_price * quantity, also a cost figure), so
            # mixing MTM and cost in the same sum would be inconsistent.
            "cost_value": h["quantity"] * h["average_price"],
            "last_price": h["last_price"],
        }
        for h in holdings
    }
    return PortfolioContext(portfolio_value=portfolio_value, holdings_by_symbol=holdings_by_symbol)


def get_settings(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM risk_settings WHERE account_id = ?", (account_id,)).fetchone()


def update_settings(conn: sqlite3.Connection, account_id: int, **fields) -> sqlite3.Row:
    current = get_settings(conn, account_id)
    changed = {k: v for k, v in fields.items() if current[k] != v}

    if changed:
        columns = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE risk_settings SET {columns}, updated_at = datetime('now') WHERE account_id = ?",
            (*fields.values(), account_id),
        )
        conn.executemany(
            """
            INSERT INTO risk_settings_history (account_id, field_changed, old_value, new_value)
            VALUES (?, ?, ?, ?)
            """,
            [(account_id, field, str(current[field]), str(new_value)) for field, new_value in changed.items()],
        )
        conn.commit()
    return get_settings(conn, account_id)


def get_settings_history(conn: sqlite3.Connection, account_id: int) -> list:
    rows = conn.execute(
        "SELECT * FROM risk_settings_history WHERE account_id = ? ORDER BY changed_at DESC, id DESC",
        (account_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def compute_position_size(entry_price: float, stop_loss: float, portfolio_value: float, settings: sqlite3.Row) -> dict:
    risk_per_share = abs(entry_price - stop_loss)
    if risk_per_share <= 0 or entry_price <= 0:
        return {"portfolio_value": portfolio_value, "risk_based_qty": 0, "capital_based_qty": 0, "suggested_qty": 0}

    risk_based_qty = max(math.floor((portfolio_value * settings["max_risk_per_trade_pct"] / 100) / risk_per_share), 0)
    capital_based_qty = max(math.floor((portfolio_value * settings["max_capital_per_trade_pct"] / 100) / entry_price), 0)
    return {
        "portfolio_value": portfolio_value,
        "risk_based_qty": risk_based_qty,
        "capital_based_qty": capital_based_qty,
        "suggested_qty": min(risk_based_qty, capital_based_qty),
    }


def compute_suggested_exit_quantity(
    conn: sqlite3.Connection, account_id: int, symbol: str, ctx: PortfolioContext, settings: sqlite3.Row
) -> dict:
    """For the Quick Action popup's concentration alert: how many shares to
    sell to bring this symbol back to exactly max_single_stock_exposure_pct
    of portfolio_value. Deliberately mirrors _check_single_stock_exposure's
    own math (via _symbol_exposure, which also folds in same-day-executed
    live tickets on top of holdings cost, not just bare holdings cost) so
    the suggested quantity is self-consistent with what that check will
    actually re-validate once the resulting ticket is created — using a
    DIFFERENT (e.g. mark-to-market) basis here would suggest a quantity that
    doesn't actually satisfy the real check.

    Note this basis (cost value / portfolio_value-including-margin) is NOT
    the same as the Dashboard alert banner's own trigger, which compares
    mark-to-market holding value against a holdings-only total
    (CONFIG.concentration_alert_pct, a separate, simpler config knob from
    risk_settings.max_single_stock_exposure_pct used here) — the two are
    different metrics on purpose and the frontend shows both rather than
    conflating them."""
    holding = ctx.holdings_by_symbol.get(symbol)
    quantity_held = holding["quantity"] if holding else 0
    average_price = holding["average_price"] if holding else 0.0

    current_cost = _symbol_exposure(conn, account_id, symbol, ctx)
    current_exposure_pct = (current_cost / ctx.portfolio_value * 100) if ctx.portfolio_value else 0.0
    target_pct = settings["max_single_stock_exposure_pct"]
    target_cost = target_pct / 100 * ctx.portfolio_value
    excess_cost = current_cost - target_cost

    if excess_cost <= 0 or average_price <= 0 or quantity_held <= 0:
        suggested_qty = 0
    else:
        suggested_qty = min(math.ceil(excess_cost / average_price), quantity_held)

    return {
        "quantity_held": quantity_held,
        "average_price": average_price,
        "portfolio_value": ctx.portfolio_value,
        "current_exposure_pct": current_exposure_pct,
        "target_exposure_pct": target_pct,
        "suggested_qty": suggested_qty,
    }


@dataclass
class CheckResult:
    rule: str
    label: str
    passed: bool
    skipped: bool
    actual_value: float | int | None
    limit_value: float | int | None
    unit: str  # "ratio" | "pct" | "count"
    message: str


@dataclass
class RiskCheckReport:
    checks: list

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def as_list(self) -> list:
        return [asdict(c) for c in self.checks]


def _same_day_ist_cutoff_utc() -> str:
    midnight_ist = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_ist.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# Entries claimed for execution whose broker outcome is not yet recorded as
# 'executed': in flight, UNKNOWN or needs_review (PK-2). Such an order may
# already be live at the broker, so every limit counts it as if it were open
# (PK-4) — otherwise two approvals racing each other, or an approval while an
# earlier one is unresolved, could each see room that only one of them has.
# The ticket being checked is excluded: it is 'confirmed' itself while it is
# re-validated at confirmation, and each check adds it once on its own.
_IN_FLIGHT_ENTRY_WHERE = """
    account_id = ? AND status = 'confirmed' AND mode = 'live' AND closes_ticket_id IS NULL
      AND NOT (transaction_type = 'SELL' AND source = 'alert_action')
      AND id != ?
"""


def _symbol_exposure(conn: sqlite3.Connection, account_id: int, symbol: str, ctx: PortfolioContext,
                     exclude_ticket_id: int = 0) -> float:
    # Existing holdings (any age) + same-day-executed *live* open tickets for
    # this symbol. Only same-day tickets are added on top of holdings —
    # summing ALL open tickets regardless of age would double-count every
    # position older than a day, since nothing in this app ever marks a
    # ticket "settled into holdings." This closes the same-day-stacking gap
    # (several tickets placed before T+1 settlement shows up in holdings)
    # without that double-count. mode='live' excludes dry-run "executed"
    # tickets — those are simulations with zero real capital deployed and
    # must not count against real exposure/risk limits.
    holding = ctx.holdings_by_symbol.get(symbol)
    holdings_value = holding["cost_value"] if holding else 0.0
    cutoff = _same_day_ist_cutoff_utc()
    # closes_ticket_id IS NULL: an executed exit ticket reduces exposure, so
    # its own notional must never be added on top of the position it closed.
    row = conn.execute(
        """
        SELECT COALESCE(SUM(entry_price * quantity), 0) AS total
        FROM trade_tickets
        WHERE account_id = ? AND symbol = ? AND status = 'executed' AND mode = 'live'
          AND closed_at IS NULL AND closes_ticket_id IS NULL AND executed_at >= ?
        """,
        (account_id, symbol, cutoff),
    ).fetchone()
    in_flight = conn.execute(
        f"""
        SELECT COALESCE(SUM(entry_price * quantity), 0) AS total FROM trade_tickets
        WHERE {_IN_FLIGHT_ENTRY_WHERE} AND symbol = ? AND COALESCE(submitted_at, created_at) >= ?
        """,
        (account_id, exclude_ticket_id, symbol, cutoff),
    ).fetchone()
    return holdings_value + row["total"] + in_flight["total"]


# Single source of truth for "what counts as an open position" — used by
# both the max_open_positions check below and Phase 5's exit scanner
# (list_open_positions), so the two can never define "open" differently.
# Only an exit whose FILL is proven closes a position: a pending, rejected,
# failed or cancelled exit ticket leaves it open (it used to hide the
# position from every scanner, the kill switch and the position-count limit
# forever), and so does an EXITING one — accepted by Kite, not yet filled —
# or a partially filled one (PK-8). Such a position still counts for max
# open positions, portfolio risk, Emergency Exit and reconciliation.
_CLOSED_BY_EXECUTED_EXIT = (
    "SELECT closes_ticket_id FROM trade_tickets WHERE closes_ticket_id IS NOT NULL AND status = 'executed' "
    f"AND {execution.EXIT_FILL_PROVEN_SQL}"
)
_OPEN_POSITION_WHERE = f"""
    account_id = ? AND status = 'executed' AND mode = 'live' AND transaction_type = 'BUY'
      AND closed_at IS NULL
      AND id NOT IN ({_CLOSED_BY_EXECUTED_EXIT})
"""


def _open_position_count(conn: sqlite3.Connection, account_id: int, exclude_ticket_id: int = 0) -> int:
    row = conn.execute(
        f"""
        SELECT (SELECT COUNT(*) FROM trade_tickets WHERE {_OPEN_POSITION_WHERE})
             + (SELECT COUNT(*) FROM trade_tickets WHERE {_IN_FLIGHT_ENTRY_WHERE} AND transaction_type = 'BUY') AS n
        """,
        (account_id, account_id, exclude_ticket_id),
    ).fetchone()
    return row["n"]


def list_open_positions(conn: sqlite3.Connection, account_id: int) -> list:
    """Full rows (not just a count) of currently open live BUY positions, per
    the exact same definition _open_position_count uses. Used by Phase 5's
    exit scanner and breakeven check."""
    return conn.execute(
        f"SELECT * FROM trade_tickets WHERE {_OPEN_POSITION_WHERE}", (account_id,)
    ).fetchall()


def _open_risk_sum(conn: sqlite3.Connection, account_id: int, exclude_ticket_id: int = 0) -> float:
    # Exit tickets carry a risk_amount only to satisfy the schema; counting
    # them made every executed exit add phantom risk forever.
    row = conn.execute(
        f"""
        SELECT (SELECT COALESCE(SUM(risk_amount), 0) FROM trade_tickets
                WHERE account_id = ? AND status = 'executed' AND mode = 'live' AND closed_at IS NULL
                  AND closes_ticket_id IS NULL
                  AND id NOT IN ({_CLOSED_BY_EXECUTED_EXIT}))
             + (SELECT COALESCE(SUM(risk_amount), 0) FROM trade_tickets WHERE {_IN_FLIGHT_ENTRY_WHERE}) AS total
        """,
        (account_id, account_id, exclude_ticket_id),
    ).fetchone()
    return row["total"]


def _check_risk_reward(metrics: dict, settings: sqlite3.Row) -> CheckResult:
    rr = metrics["risk_reward_ratio"]
    min_rr = settings["min_risk_reward"]
    passed = rr is not None and rr >= min_rr
    message = (
        f"Risk:Reward is 1:{rr:.2f}, minimum required is 1:{min_rr}"
        if rr is not None else "Risk:Reward could not be computed"
    )
    return CheckResult("min_risk_reward", "Risk:Reward", passed, False, rr, min_rr, "ratio", message)


def _check_capital_per_trade(ticket_row: sqlite3.Row, ctx: PortfolioContext, settings: sqlite3.Row) -> CheckResult:
    capital = ticket_row["entry_price"] * ticket_row["quantity"]
    pct = (capital / ctx.portfolio_value * 100) if ctx.portfolio_value else 0
    limit = settings["max_capital_per_trade_pct"]
    passed = pct <= limit
    message = f"Capital would be {pct:.1f}% of portfolio, limit is {limit}%"
    return CheckResult("max_capital_per_trade_pct", "Capital", passed, False, pct, limit, "pct", message)


def _check_single_stock_exposure(conn: sqlite3.Connection, ticket_row: sqlite3.Row, ctx: PortfolioContext, settings: sqlite3.Row) -> CheckResult:
    capital = ticket_row["entry_price"] * ticket_row["quantity"]
    existing = _symbol_exposure(conn, ticket_row["account_id"], ticket_row["symbol"], ctx, ticket_row["id"])
    pct = ((existing + capital) / ctx.portfolio_value * 100) if ctx.portfolio_value else 0
    limit = settings["max_single_stock_exposure_pct"]
    passed = pct <= limit
    message = f"{ticket_row['symbol']} exposure would be {pct:.1f}% of portfolio, limit is {limit}%"
    return CheckResult("max_single_stock_exposure_pct", "Single-stock exposure", passed, False, pct, limit, "pct", message)


def _check_sector_exposure(conn: sqlite3.Connection, ticket_row: sqlite3.Row, ctx: PortfolioContext, settings: sqlite3.Row) -> CheckResult:
    limit = settings["max_sector_exposure_pct"]
    sector = sector_mapping.get_sector(ticket_row["symbol"])
    if sector is None:
        message = "unknown sector, exposure check skipped (no mapping for this symbol)"
        return CheckResult("max_sector_exposure_pct", "Sector exposure", True, True, None, limit, "pct", message)

    capital = ticket_row["entry_price"] * ticket_row["quantity"]
    symbols = sector_mapping.symbols_in_sector(sector)
    existing = sum(_symbol_exposure(conn, ticket_row["account_id"], s, ctx, ticket_row["id"]) for s in symbols)
    pct = ((existing + capital) / ctx.portfolio_value * 100) if ctx.portfolio_value else 0
    passed = pct <= limit
    message = f"{sector} sector exposure would be {pct:.1f}% of portfolio, limit is {limit}%"
    return CheckResult("max_sector_exposure_pct", "Sector exposure", passed, False, pct, limit, "pct", message)


def _check_open_positions(conn: sqlite3.Connection, ticket_row: sqlite3.Row, settings: sqlite3.Row) -> CheckResult:
    count = _open_position_count(conn, ticket_row["account_id"], ticket_row["id"])
    # Only a new BUY ticket would itself become an open position (per the
    # spec's own definition of "open" — executed BUY tickets not yet closed);
    # a SELL ticket doesn't add to this count.
    prospective = count + 1 if ticket_row["transaction_type"] == "BUY" else count
    limit = settings["max_open_positions"]
    passed = prospective <= limit
    message = f"Would have {prospective} of {limit} max open positions"
    return CheckResult("max_open_positions", "Open positions", passed, False, prospective, limit, "count", message)


def _check_total_portfolio_risk(conn: sqlite3.Connection, ticket_row: sqlite3.Row, ctx: PortfolioContext, settings: sqlite3.Row) -> CheckResult:
    existing_risk = _open_risk_sum(conn, ticket_row["account_id"], ticket_row["id"])
    this_risk = abs(ticket_row["entry_price"] - ticket_row["stop_loss"]) * ticket_row["quantity"]
    pct = ((existing_risk + this_risk) / ctx.portfolio_value * 100) if ctx.portfolio_value else 0
    limit = settings["max_total_portfolio_risk_pct"]
    passed = pct <= limit
    message = f"Total portfolio risk would be {pct:.1f}%, limit is {limit}%"
    return CheckResult("max_total_portfolio_risk_pct", "Total portfolio risk", passed, False, pct, limit, "pct", message)


def get_held_quantity(kite: KiteConnect, symbol: str) -> int:
    """Currently held quantity for a symbol per the broker's actual
    holdings+positions data — reuses the exact formula
    reconciliation.py:_check_position_state already established: unsettled
    (T+1) delivery quantity sits in t1_quantity, not quantity, and a
    same-day CNC buy shows up in positions() net before it settles into
    holdings() the next day, so both sources are summed."""
    holdings = kite.holdings()
    positions = kite.positions().get("net", [])
    qty = sum(
        (h.get("quantity") or 0) + (h.get("t1_quantity") or 0)
        for h in holdings if h["tradingsymbol"] == symbol
    )
    qty += sum(p.get("quantity") or 0 for p in positions if p["tradingsymbol"] == symbol)
    return qty


@dataclass
class ExitProblem:
    code: str
    message: str
    # False only while another exit of the same position is claimed and its
    # outcome is unresolved — this exit may become valid again if that one
    # ends failed/not_found. Every other problem can never go away.
    permanent: bool


def linked_exit_problem(conn: sqlite3.Connection, ticket_row) -> ExitProblem | None:
    """Why an exit ticket (closes_ticket_id set) cannot close its position
    right now, or None if it can (PK-4). Re-reads the position — never
    trusts what was true when the exit was created: the position may since
    have been closed by a GTT, Emergency Exit or another exit ticket, or
    another exit may already own it. DB-only, no broker call. Mode is not
    checked here: confirmation checks it first (routes/tickets._claim_mode).

    Quantity: the exit must equal the position's quantity — the best
    representation of the open quantity available until partial fills are
    modelled (PK-13). A larger exit would oversell; a smaller one is not
    modelled either (its fill would delete the whole position's GTT and
    journal the full quantity as closed)."""
    position_id = ticket_row["closes_ticket_id"]
    position = conn.execute(
        "SELECT * FROM trade_tickets WHERE id = ? AND account_id = ?", (position_id, ticket_row["account_id"])
    ).fetchone()
    if (position is None or position["transaction_type"] != "BUY" or position["closes_ticket_id"] is not None
            or position["status"] != "executed"):
        state = "does not exist for this account" if position is None else (
            f"is a {position['transaction_type']} ticket in status '{position['status']}'"
            + (" that itself closes a position" if position["closes_ticket_id"] is not None else ""))
        return ExitProblem("EXIT_POSITION_INVALID",
                           f"Exit ticket {ticket_row['id']} references ticket {position_id}, which {state} — "
                           f"not an open executed BUY position.", True)

    owner = conn.execute(
        """
        SELECT id, status, submission_state, entry_fill_status FROM trade_tickets
        WHERE closes_ticket_id = ? AND id != ? AND status IN ('confirmed', 'executed')
        ORDER BY status = 'executed' DESC, id LIMIT 1
        """,
        (position_id, ticket_row["id"]),
    ).fetchone()
    owner_filled = owner is not None and owner["status"] == "executed" and owner["entry_fill_status"] in (None, "filled")
    if owner_filled:
        return ExitProblem("EXIT_POSITION_CLOSED",
                           f"Position {position_id} was already exited by ticket {owner['id']}.", True)
    if position["closed_at"] is not None:
        return ExitProblem("EXIT_POSITION_CLOSED",
                           f"Position {position_id} is already closed (at {position['closed_at']}).", True)
    if owner is not None and owner["status"] == "executed":
        # PK-8: EXITING (or partially filled) — the owning exit is at the
        # broker but its fill is not proven. Not permanent: if it is rejected
        # or cancelled unfilled, this exit may close the position after all.
        state = ("awaiting its broker fill (EXITING)" if owner["entry_fill_status"] == "pending"
                 else f"{owner['entry_fill_status']} — needs review")
        return ExitProblem("EXIT_ALREADY_IN_PROGRESS",
                           f"Exit ticket {owner['id']} for position {position_id} is already at the broker, "
                           f"{state}. DO NOT RESUBMIT.", False)
    if owner is not None:
        return ExitProblem("EXIT_ALREADY_IN_PROGRESS",
                           f"Exit ticket {owner['id']} for position {position_id} is already being executed "
                           f"(submission {owner['submission_state'] or 'in progress'}) — its broker outcome must be "
                           f"resolved first. DO NOT RESUBMIT.", False)
    if ticket_row["quantity"] != position["quantity"]:
        return ExitProblem("EXIT_QUANTITY_MISMATCH",
                           f"Exit quantity {ticket_row['quantity']} does not match the {position['quantity']} shares "
                           f"of position {position_id}"
                           + (" — it would sell more than the position holds." if ticket_row["quantity"] > position["quantity"]
                              else " — partial exits of a tracked position are not supported."), True)
    return None


def _check_exit_or_reduction(conn: sqlite3.Connection, ticket_row: sqlite3.Row, kite: KiteConnect) -> CheckResult:
    """Runs instead of the six position-opening checks for any ticket
    execution.is_closing_sell() considers a closing sell. Two ways a SELL
    ticket can validly skip those checks:
    (a) closes_ticket_id references a specific tracked open executed BUY —
        Phase 5 strategy exits, the kill switch's emergency exit.
    (b) closes_ticket_id is NULL but source == 'alert_action' (the Quick
        Action popup's sell action) — validated instead against the
        broker's actual currently-held quantity for the symbol, since many
        real holdings were never opened via a trade_ticket in this app at
        all (pre-existing positions, or bought directly in Kite)."""
    if ticket_row["closes_ticket_id"] is not None:
        # The same validation confirmation re-runs (PK-4), so a ticket is
        # never approvable here yet refused there for a reason already true.
        problem = linked_exit_problem(conn, ticket_row)
        valid = problem is None
        message = (
            "Exit ticket — closes a specific tracked position, position-sizing checks not applicable"
            if valid else f"Exit ticket rejected: {problem.message}"
        )
    else:
        held_qty = get_held_quantity(kite, ticket_row["symbol"])
        valid = 0 < ticket_row["quantity"] <= held_qty
        message = (
            f"Reduces a held position not tracked by a ticket — {ticket_row['quantity']} of {held_qty} "
            f"shares currently held, position-sizing checks not applicable"
            if valid else
            f"Rejected: requested quantity {ticket_row['quantity']} exceeds currently held quantity {held_qty}"
        )
    # actual_value/limit_value are None here (this isn't a numeric
    # threshold check) — the frontend renders the message directly whenever
    # actual_value is None, the same path already used by the sector-skip case.
    return CheckResult("exit_or_reduction", "Exit / Reduction Validation", valid, False, None, None, "pct", message)


def run_checks(
    conn: sqlite3.Connection, account: sqlite3.Row, ticket_row: sqlite3.Row, kite: KiteConnect,
    ctx: PortfolioContext | None = None,
) -> RiskCheckReport:
    # A SELL ticket that closes/reduces an existing position is risk-
    # REDUCING, not risk-adding — the six position-opening checks below
    # (capital, single-stock/sector exposure, R:R, open-position count,
    # total portfolio risk) don't meaningfully apply to it.
    # execution.is_closing_sell() is the single source of truth for which
    # SELL tickets this applies to (closes_ticket_id set, OR
    # source='alert_action' — see that function's docstring); an ordinary
    # manual SELL ticket (source='manual', no closes_ticket_id) is
    # completely unaffected and still runs all six checks exactly as before.
    if execution.is_closing_sell(ticket_row):
        return RiskCheckReport(checks=[_check_exit_or_reduction(conn, ticket_row, kite)])

    settings = get_settings(conn, account["id"])
    # A batch caller (the strategy scan) passes one context for the whole
    # batch — holdings/margins can't change mid-scan since nothing executes.
    ctx = ctx or build_portfolio_context(kite)
    metrics = execution.compute_risk_metrics(dict(ticket_row))

    # All six checks always run and are always evaluated — never
    # short-circuited on the first failure — so a rejected ticket's breakdown
    # shows the full picture, not one rule at a time.
    checks = [
        _check_risk_reward(metrics, settings),
        _check_capital_per_trade(ticket_row, ctx, settings),
        _check_single_stock_exposure(conn, ticket_row, ctx, settings),
        _check_sector_exposure(conn, ticket_row, ctx, settings),
        _check_open_positions(conn, ticket_row, settings),
        _check_total_portfolio_risk(conn, ticket_row, ctx, settings),
    ]
    return RiskCheckReport(checks=checks)


def _transition_from_draft(conn: sqlite3.Connection, ticket_id: int, to_status: str,
                           risk_check_results: str | None = None) -> bool:
    """Applies a risk-check outcome only if the ticket is still 'draft'. If
    not, someone else moved it while the checks ran: its current state
    stands, and one event says the result was discarded."""
    if risk_check_results is None:
        cursor = conn.execute(
            "UPDATE trade_tickets SET status = ? WHERE id = ? AND status = 'draft'", (to_status, ticket_id))
    else:
        cursor = conn.execute(
            "UPDATE trade_tickets SET status = ?, risk_check_results = ? WHERE id = ? AND status = 'draft'",
            (to_status, risk_check_results, ticket_id),
        )
    if cursor.rowcount == 1:
        return True
    conn.rollback()
    current = conn.execute("SELECT status FROM trade_tickets WHERE id = ?", (ticket_id,)).fetchone()
    if current is not None:
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'modified', ?)",
            (ticket_id, f"Risk check finished after the ticket had become '{current['status']}'; its result "
                        f"({to_status}) was discarded and the ticket left unchanged."),
        )
        conn.commit()
    return False


def run_risk_checks_and_transition(
    conn: sqlite3.Connection, account: sqlite3.Row, ticket_id: int, kite: KiteConnect,
    ctx: PortfolioContext | None = None,
) -> None:
    """Runs run_checks() against the ticket currently in the DB and
    transitions it to pending_confirmation/risk_rejected accordingly. Shared
    by manual ticket creation/editing (backend/routes/tickets.py) and the
    Phase 5 strategy engine (backend/strategy_engine.py) so the two paths
    can never drift apart. Raises RiskCheckUnavailableError (not translated
    to an HTTP error here — this module has no FastAPI dependency; callers
    that are HTTP endpoints translate it themselves) if the risk check
    itself couldn't run at all.

    Both writes are compare-and-set on status='draft' (PK-4): the checks
    call Kite and take time, and the ticket may be cancelled/rejected
    meanwhile (Cancel All Pending, reject). A result that arrives after that
    is discarded — it must never turn a withdrawn ticket back into an
    approvable one."""
    ticket = conn.execute(
        "SELECT * FROM trade_tickets WHERE id = ? AND account_id = ?", (ticket_id, account["id"])
    ).fetchone()
    if ticket is None:
        raise ValueError(f"Ticket {ticket_id} not found for account {account['id']}")

    try:
        report = run_checks(conn, account, ticket, kite, ctx=ctx)
    except RiskCheckUnavailableError as e:
        # The risk check itself couldn't run (Kite API failure) — this is
        # not the same as the ticket failing a check. Don't leave it
        # stuck in 'draft' forever as a dead row in Ticket History.
        if _transition_from_draft(conn, ticket_id, "cancelled"):
            conn.execute(
                "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'rejected', ?)",
                (ticket_id, f"Risk check could not be evaluated: {e.message}"),
            )
            conn.commit()
        raise

    new_status = "pending_confirmation" if report.passed else "risk_rejected"
    if not _transition_from_draft(conn, ticket_id, new_status, json.dumps(report.as_list())):
        return
    if new_status == "risk_rejected":
        detail = "; ".join(c.message for c in report.checks if not c.passed)
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'risk_rejected', ?)",
            (ticket_id, detail),
        )
    conn.commit()
