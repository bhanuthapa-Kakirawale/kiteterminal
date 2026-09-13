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


def _symbol_exposure(conn: sqlite3.Connection, account_id: int, symbol: str, ctx: PortfolioContext) -> float:
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
    row = conn.execute(
        """
        SELECT COALESCE(SUM(entry_price * quantity), 0) AS total
        FROM trade_tickets
        WHERE account_id = ? AND symbol = ? AND status = 'executed' AND mode = 'live'
          AND closed_at IS NULL AND executed_at >= ?
        """,
        (account_id, symbol, cutoff),
    ).fetchone()
    return holdings_value + row["total"]


# Single source of truth for "what counts as an open position" — used by
# both the max_open_positions check below and Phase 5's exit scanner
# (list_open_positions), so the two can never define "open" differently.
_OPEN_POSITION_WHERE = """
    account_id = ? AND status = 'executed' AND mode = 'live' AND transaction_type = 'BUY'
      AND closed_at IS NULL
      AND id NOT IN (SELECT closes_ticket_id FROM trade_tickets WHERE closes_ticket_id IS NOT NULL)
"""


def _open_position_count(conn: sqlite3.Connection, account_id: int) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM trade_tickets WHERE {_OPEN_POSITION_WHERE}", (account_id,)
    ).fetchone()
    return row["n"]


def list_open_positions(conn: sqlite3.Connection, account_id: int) -> list:
    """Full rows (not just a count) of currently open live BUY positions, per
    the exact same definition _open_position_count uses. Used by Phase 5's
    exit scanner and breakeven check."""
    return conn.execute(
        f"SELECT * FROM trade_tickets WHERE {_OPEN_POSITION_WHERE}", (account_id,)
    ).fetchall()


def _open_risk_sum(conn: sqlite3.Connection, account_id: int) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(risk_amount), 0) AS total FROM trade_tickets
        WHERE account_id = ? AND status = 'executed' AND mode = 'live' AND closed_at IS NULL
          AND id NOT IN (SELECT closes_ticket_id FROM trade_tickets WHERE closes_ticket_id IS NOT NULL)
        """,
        (account_id,),
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
    existing = _symbol_exposure(conn, ticket_row["account_id"], ticket_row["symbol"], ctx)
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
    existing = sum(_symbol_exposure(conn, ticket_row["account_id"], s, ctx) for s in symbols)
    pct = ((existing + capital) / ctx.portfolio_value * 100) if ctx.portfolio_value else 0
    passed = pct <= limit
    message = f"{sector} sector exposure would be {pct:.1f}% of portfolio, limit is {limit}%"
    return CheckResult("max_sector_exposure_pct", "Sector exposure", passed, False, pct, limit, "pct", message)


def _check_open_positions(conn: sqlite3.Connection, ticket_row: sqlite3.Row, settings: sqlite3.Row) -> CheckResult:
    count = _open_position_count(conn, ticket_row["account_id"])
    # Only a new BUY ticket would itself become an open position (per the
    # spec's own definition of "open" — executed BUY tickets not yet closed);
    # a SELL ticket doesn't add to this count.
    prospective = count + 1 if ticket_row["transaction_type"] == "BUY" else count
    limit = settings["max_open_positions"]
    passed = prospective <= limit
    message = f"Would have {prospective} of {limit} max open positions"
    return CheckResult("max_open_positions", "Open positions", passed, False, prospective, limit, "count", message)


def _check_total_portfolio_risk(conn: sqlite3.Connection, ticket_row: sqlite3.Row, ctx: PortfolioContext, settings: sqlite3.Row) -> CheckResult:
    existing_risk = _open_risk_sum(conn, ticket_row["account_id"])
    this_risk = abs(ticket_row["entry_price"] - ticket_row["stop_loss"]) * ticket_row["quantity"]
    pct = ((existing_risk + this_risk) / ctx.portfolio_value * 100) if ctx.portfolio_value else 0
    limit = settings["max_total_portfolio_risk_pct"]
    passed = pct <= limit
    message = f"Total portfolio risk would be {pct:.1f}%, limit is {limit}%"
    return CheckResult("max_total_portfolio_risk_pct", "Total portfolio risk", passed, False, pct, limit, "pct", message)


def _check_valid_exit_reference(conn: sqlite3.Connection, ticket_row: sqlite3.Row) -> CheckResult:
    referenced = conn.execute(
        "SELECT * FROM trade_tickets WHERE id = ? AND account_id = ?",
        (ticket_row["closes_ticket_id"], ticket_row["account_id"]),
    ).fetchone()
    valid = (
        referenced is not None
        and referenced["transaction_type"] == "BUY"
        and referenced["status"] == "executed"
        and referenced["closed_at"] is None
    )
    message = (
        "Exit ticket — position-sizing checks not applicable, closing position validated"
        if valid else
        "Exit ticket rejected: closes_ticket_id does not reference a valid open executed BUY ticket for this account"
    )
    # actual_value/limit_value are None here (this isn't a numeric
    # threshold check) — the frontend renders the message directly whenever
    # actual_value is None, the same path already used by the sector-skip case.
    return CheckResult("exit_validity", "Exit Ticket Validation", valid, False, None, None, "pct", message)


def run_checks(conn: sqlite3.Connection, account: sqlite3.Row, ticket_row: sqlite3.Row, kite: KiteConnect) -> RiskCheckReport:
    # A SELL ticket that closes an existing position is risk-REDUCING, not
    # risk-adding — the six position-opening checks below (capital,
    # single-stock/sector exposure, R:R, open-position count, total
    # portfolio risk) don't meaningfully apply to it. Scoped precisely to
    # SELL + a non-null closes_ticket_id so an ordinary manual SELL ticket
    # (no closes_ticket_id) is completely unaffected and still runs all six
    # checks exactly as before.
    if ticket_row["transaction_type"] == "SELL" and ticket_row["closes_ticket_id"] is not None:
        return RiskCheckReport(checks=[_check_valid_exit_reference(conn, ticket_row)])

    settings = get_settings(conn, account["id"])
    ctx = build_portfolio_context(kite)
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


def run_risk_checks_and_transition(
    conn: sqlite3.Connection, account: sqlite3.Row, ticket_id: int, kite: KiteConnect
) -> None:
    """Runs run_checks() against the ticket currently in the DB and
    transitions it to pending_confirmation/risk_rejected accordingly. Shared
    by manual ticket creation/editing (backend/routes/tickets.py) and the
    Phase 5 strategy engine (backend/strategy_engine.py) so the two paths
    can never drift apart. Raises RiskCheckUnavailableError (not translated
    to an HTTP error here — this module has no FastAPI dependency; callers
    that are HTTP endpoints translate it themselves) if the risk check
    itself couldn't run at all."""
    ticket = conn.execute(
        "SELECT * FROM trade_tickets WHERE id = ? AND account_id = ?", (ticket_id, account["id"])
    ).fetchone()
    if ticket is None:
        raise ValueError(f"Ticket {ticket_id} not found for account {account['id']}")

    try:
        report = run_checks(conn, account, ticket, kite)
    except RiskCheckUnavailableError as e:
        # The risk check itself couldn't run (Kite API failure) — this is
        # not the same as the ticket failing a check. Don't leave it
        # stuck in 'draft' forever as a dead row in Ticket History.
        conn.execute("UPDATE trade_tickets SET status = 'cancelled' WHERE id = ?", (ticket_id,))
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'rejected', ?)",
            (ticket_id, f"Risk check could not be evaluated: {e.message}"),
        )
        conn.commit()
        raise

    new_status = "pending_confirmation" if report.passed else "risk_rejected"
    conn.execute(
        "UPDATE trade_tickets SET status = ?, risk_check_results = ? WHERE id = ?",
        (new_status, json.dumps(report.as_list()), ticket_id),
    )
    if new_status == "risk_rejected":
        detail = "; ".join(c.message for c in report.checks if not c.passed)
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'risk_rejected', ?)",
            (ticket_id, detail),
        )
    conn.commit()
