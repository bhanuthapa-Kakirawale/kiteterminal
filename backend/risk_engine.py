import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException, TokenException

from backend import execution, position_quantity, sector_mapping

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


PORTFOLIO_EXPOSURE_UNAVAILABLE = "PORTFOLIO_EXPOSURE_UNAVAILABLE"


@dataclass
class PortfolioContext:
    """One broker snapshot of the CURRENT trading account (PK-15): every
    exposure, sector and percentage check of one approval is evaluated
    against it, so they agree with each other. Never a family/consolidated
    view — risk capital is this account's alone.

    broker_by_symbol: symbol -> {settled, t1, day_net, quantity, last_price,
    average_price}; quantity = settled + t1 + day_net (never below 0).
    portfolio_value = sum(quantity * last_price) + cash_component, where
    cash_component = margins equity.net less equity.available.collateral."""
    portfolio_value: float
    broker_by_symbol: dict
    cash_component: float = 0.0
    collateral_excluded: float = 0.0


def _unavailable(message: str) -> RiskCheckUnavailableError:
    return RiskCheckUnavailableError(
        503, PORTFOLIO_EXPOSURE_UNAVAILABLE,
        f"Portfolio exposure unavailable ({PORTFOLIO_EXPOSURE_UNAVAILABLE}): {message}")


def _number(value, what: str) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _unavailable(f"{what} is not a usable number ({value!r})")
    return float(value)


def _price(row: dict) -> float | None:
    for key in ("last_price", "close_price"):
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
            return float(value)
    return None


def broker_quantities(holdings: list, positions: dict) -> dict:
    """Shares the broker says this account owns, per symbol — the one place
    the Kite quantity fields are combined for exposure (PK-15):

      holdings.quantity       settled demat shares. A sale made today is
                              not deducted here — it shows as a negative
                              CNC position (below). Pledged shares
                              (collateral_quantity) are part of it and are
                              not added again.
      holdings.t1_quantity    bought, not yet settled (T+1). NOT inside
                              quantity, so it is added: yesterday's buy is
                              real exposure today.
      positions net, CNC,     today's delivery change: quantity less
      NSE/BSE                 overnight_quantity (overnight CNC shares are
                              the T1 shares already in holdings). MIS/NRML
                              (intraday, derivatives) are not delivery
                              exposure and are left out.

    Exit validation (get_held_quantity) and reconciliation keep their own,
    broader all-product sum on purpose — narrowing an exit path is not an
    exposure change."""
    by_symbol: dict[str, dict] = {}

    def entry(symbol: str) -> dict:
        return by_symbol.setdefault(symbol, {"settled": 0, "t1": 0, "day_net": 0, "last_price": None,
                                             "average_price": None})

    for h in holdings or []:
        e = entry(h["tradingsymbol"])
        e["settled"] += int(_number(h.get("quantity"), f"{h['tradingsymbol']} holdings quantity"))
        e["t1"] += int(_number(h.get("t1_quantity"), f"{h['tradingsymbol']} t1_quantity"))
        e["last_price"] = e["last_price"] or _price(h)
        e["average_price"] = e["average_price"] or h.get("average_price")
    for p in (positions or {}).get("net") or []:
        if p.get("product") != "CNC" or p.get("exchange", "NSE") not in ("NSE", "BSE"):
            continue
        e = entry(p["tradingsymbol"])
        e["day_net"] += int(_number(p.get("quantity"), f"{p['tradingsymbol']} position quantity")
                            - _number(p.get("overnight_quantity"), f"{p['tradingsymbol']} overnight_quantity"))
        e["last_price"] = e["last_price"] or _price(p)
        e["average_price"] = e["average_price"] or p.get("average_price")
    for e in by_symbol.values():
        e["quantity"] = max(0, e["settled"] + e["t1"] + e["day_net"])
    return by_symbol


def build_portfolio_context(kite: KiteConnect) -> PortfolioContext:
    """One holdings(), positions() and margins() call each. Fails closed —
    a new entry is never approved on a figure that could not be computed."""
    try:
        holdings = kite.holdings()
        positions = kite.positions()
        margins = kite.margins()
    except TokenException:
        raise RiskCheckUnavailableError(
            401, "SESSION_EXPIRED", "Your Zerodha session has expired. Please reconnect."
        )
    except KiteException as e:
        raise RiskCheckUnavailableError(502, "KITE_API_ERROR", str(e))

    try:
        broker = broker_quantities(holdings, positions)
        equity = (margins or {}).get("equity") or {}
        # Kite's equity.net already includes the margin from pledged
        # holdings (available.collateral); those holdings are counted at
        # market value below, so their collateral is taken out once here.
        net = _number(equity.get("net"), "margins equity.net")
        collateral = max(0.0, _number((equity.get("available") or {}).get("collateral"),
                                      "margins equity.available.collateral"))
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise _unavailable(f"the broker's portfolio data is malformed ({type(e).__name__}: {e})")

    holdings_value = 0.0
    for symbol, e in broker.items():
        if e["quantity"] == 0:
            continue
        if e["last_price"] is None:
            raise _unavailable(f"{symbol} is held ({e['quantity']} shares) but has no market price")
        holdings_value += e["quantity"] * e["last_price"]
    cash = net - collateral
    portfolio_value = holdings_value + cash
    if not math.isfinite(portfolio_value) or portfolio_value <= 0:
        raise _unavailable(f"portfolio value {portfolio_value:,.2f} is not positive")
    return PortfolioContext(portfolio_value=portfolio_value, broker_by_symbol=broker,
                            cash_component=cash, collateral_excluded=collateral)


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
    own math (the same account_exposures figure: broker shares — settled,
    T1 and today's — or the app's own, counted once, at market value, plus
    entries still in flight) so the suggested quantity is self-consistent
    with what that check will re-validate. The shares to sell are sized at
    the market price, the same basis the exposure is measured on (PK-15;
    it was cost before).

    Note this is NOT the Dashboard alert banner's own trigger, which
    compares holding value against a holdings-only total
    (CONFIG.concentration_alert_pct, a separate, simpler config knob from
    risk_settings.max_single_stock_exposure_pct used here) — the two are
    different metrics on purpose and the frontend shows both rather than
    conflating them."""
    broker = ctx.broker_by_symbol.get(symbol) or {}
    quantity_held = broker.get("quantity", 0)
    last_price = broker.get("last_price") or 0.0

    exposure = account_exposures(conn, account_id, ctx).get(symbol)
    current_value = exposure.value if exposure else 0.0
    current_exposure_pct = current_value / ctx.portfolio_value * 100
    target_pct = settings["max_single_stock_exposure_pct"]
    excess_value = current_value - target_pct / 100 * ctx.portfolio_value

    if excess_value <= 0 or last_price <= 0 or quantity_held <= 0:
        suggested_qty = 0
    else:
        suggested_qty = min(math.ceil(excess_value / last_price), quantity_held)

    return {
        "quantity_held": quantity_held,
        "average_price": broker.get("average_price") or 0.0,
        "last_price": last_price,
        "exposure_basis": "market_value",
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
    code: str | None = None             # a machine-readable refusal reason (e.g. SECTOR_MAPPING_REQUIRED)
    current_value: float | None = None  # the exposure before this ticket, same unit as actual_value


SECTOR_MAPPING_REQUIRED = "SECTOR_MAPPING_REQUIRED"
# Exposure exactly AT a limit passes; this absorbs float noise in the
# percentage (e.g. 20.000000000000004 for a true 20%).
_PCT_EPSILON = 1e-9


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


@dataclass
class SymbolExposure:
    symbol: str
    broker_quantity: int     # settled + T1 + today's CNC change, per the broker (PortfolioContext)
    app_held_quantity: int   # shares this app's open live positions hold (PK-13 open quantity)
    pending_quantity: int    # may still be bought: in-flight/UNKNOWN entries, unfilled rest of working ones
    held_quantity: int       # max(broker, app_held): one economic position, counted once
    price: float | None      # broker market price; the app's entry price only if the broker has none
    value: float             # held_quantity * price + pending_quantity at their planned entry prices


def account_exposures(conn: sqlite3.Connection, account_id: int, ctx: PortfolioContext,
                      exclude_ticket_id: int = 0) -> dict:
    """Current exposure per symbol for the new-entry limits (PK-15): the
    shares the account holds, counted ONCE, plus what may still be bought.

    The broker is authoritative for shares held — including ones bought
    outside this app, yesterday's (T1) and today's (positions). The app's
    open positions (their PK-13 open quantity) cover the same shares, so the
    two are never added: held = max(broker, app). The app side still
    matters when the broker has not caught up yet (a fill it has not
    reflected). An unknown open quantity counts as what it may hold.

    On top of that, conservatively, everything that may still be bought:
    every in-flight/UNKNOWN/needs_review live entry at its full quantity,
    whatever its age (PK-4/PK-12 — it may already be live at the broker),
    and the unfilled remainder of a working entry (PK-13 decision (a)). A
    fill the monitor has not recorded yet may be counted twice for that
    moment; it is never counted as zero.

    Valuation — current market value: held shares at the broker's last
    price (the app's average entry price only if the broker has none);
    shares not yet bought at their planned entry price. Dry-run tickets
    never count (mode='live'). The ticket being checked is excluded; each
    check adds it once itself."""
    rq = position_quantity.risk_quantity_sql()
    held = f"MAX(0, COALESCE({position_quantity.open_quantity_sql()}, {rq}))"
    rows = conn.execute(
        f"""
        SELECT symbol, SUM(held) AS held, SUM(held * entry_price) AS held_cost,
               SUM(pending) AS pending, SUM(pending * entry_price) AS pending_value
        FROM (
            SELECT symbol, entry_price, {held} AS held, MAX(0, {rq} - {held}) AS pending
            FROM trade_tickets WHERE {_OPEN_POSITION_WHERE}
            UNION ALL
            SELECT symbol, entry_price, 0 AS held, quantity AS pending
            FROM trade_tickets WHERE {_IN_FLIGHT_ENTRY_WHERE}
        )
        GROUP BY symbol
        """,
        (account_id, account_id, exclude_ticket_id),
    ).fetchall()
    app = {r["symbol"]: r for r in rows}

    exposures = {}
    for symbol in sorted(set(app) | {s for s, b in ctx.broker_by_symbol.items() if b["quantity"] > 0}):
        broker = ctx.broker_by_symbol.get(symbol) or {}
        broker_quantity = broker.get("quantity", 0)
        row = app.get(symbol)
        app_held = int(row["held"] or 0) if row else 0
        pending = int(row["pending"] or 0) if row else 0
        held_quantity = max(broker_quantity, app_held)
        price = broker.get("last_price") or (row["held_cost"] / app_held if app_held else None)
        value = (held_quantity * price if held_quantity else 0.0) + (float(row["pending_value"] or 0) if row else 0.0)
        exposures[symbol] = SymbolExposure(symbol, broker_quantity, app_held, pending, held_quantity, price, value)
    return exposures


def _symbol_exposure(conn: sqlite3.Connection, account_id: int, symbol: str, ctx: PortfolioContext,
                     exclude_ticket_id: int = 0) -> float:
    """One symbol's account_exposures value (0 if it has none)."""
    exposure = account_exposures(conn, account_id, ctx, exclude_ticket_id).get(symbol)
    return exposure.value if exposure else 0.0


# Single source of truth for "what counts as an open position" — used by
# both the max_open_positions check below and Phase 5's exit scanner
# (list_open_positions), so the two can never define "open" differently.
# Only an exit whose FILL is proven closes a position: a pending, rejected,
# failed or cancelled exit ticket leaves it open (it used to hide the
# position from every scanner, the kill switch and the position-count limit
# forever), and so does an EXITING one — accepted by Kite, not yet filled —
# or a partially filled one (PK-8). Such a position still counts for max
# open positions, portfolio risk, Emergency Exit and reconciliation.
#
# PK-13: a proven fill no longer means "closed" on its own — an exit may
# sell only part of what the position holds (the entry filled further after
# the exit was sized). Every proven close stamps closed_at (since PK-8), so
# the exit-based fallback is kept only for exits from before fill tracking
# (entry_fill_status NULL), whose positions predate that stamping.
_CLOSED_BY_EXECUTED_EXIT = (
    "SELECT closes_ticket_id FROM trade_tickets WHERE closes_ticket_id IS NOT NULL AND status = 'executed' "
    "AND entry_fill_status IS NULL"
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
    exit scanner and breakeven check. PK-13: each row also carries
    open_quantity (shares actually held: filled minus sold; NULL if
    unknown) and risk_quantity (position_quantity.py)."""
    return conn.execute(
        f"SELECT *, {position_quantity.open_quantity_sql()} AS open_quantity, "
        f"{position_quantity.risk_quantity_sql()} AS risk_quantity "
        f"FROM trade_tickets WHERE {_OPEN_POSITION_WHERE}", (account_id,)
    ).fetchall()


def _risk_fraction_sql() -> str:
    # Exactly 1 for a position at its requested size, so the usual case
    # sums risk_amount unchanged.
    rq = position_quantity.risk_quantity_sql()
    return f"(CASE WHEN {rq} = quantity THEN 1.0 ELSE {rq} * 1.0 / quantity END)"


def _open_risk_sum(conn: sqlite3.Connection, account_id: int, exclude_ticket_id: int = 0) -> float:
    # Exit tickets carry a risk_amount only to satisfy the schema; counting
    # them made every executed exit add phantom risk forever.
    # PK-13: risk_amount was sized for the requested quantity; each position
    # risks it pro rata to the shares it can actually hold (risk_quantity).
    row = conn.execute(
        f"""
        SELECT (SELECT COALESCE(SUM(risk_amount * {_risk_fraction_sql()}), 0)
                FROM trade_tickets
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


def _check_single_stock_exposure(ticket_row: sqlite3.Row, ctx: PortfolioContext, settings: sqlite3.Row,
                                 exposures: dict) -> CheckResult:
    capital = ticket_row["entry_price"] * ticket_row["quantity"]
    own = exposures.get(ticket_row["symbol"])
    existing = own.value if own else 0.0
    current = (existing / ctx.portfolio_value * 100) if ctx.portfolio_value else 0
    pct = ((existing + capital) / ctx.portfolio_value * 100) if ctx.portfolio_value else 0
    limit = settings["max_single_stock_exposure_pct"]
    passed = pct <= limit + _PCT_EPSILON
    message = (f"{ticket_row['symbol']}: current {current:.1f}% → post-trade {pct:.1f}% of portfolio, "
               f"limit {limit}%")
    return CheckResult("max_single_stock_exposure_pct", "Single-stock exposure", passed, False, pct, limit, "pct",
                       message, current_value=current)


def _check_sector_exposure(ticket_row: sqlite3.Row, ctx: PortfolioContext, settings: sqlite3.Row,
                           exposures: dict) -> CheckResult:
    """PK-14: the sector limit over every symbol that shares the ticket's
    sector, plus the proposed order. A symbol with no sector FAILS CLOSED
    (SECTOR_MAPPING_REQUIRED): it used to pass as 'skipped', which let
    unmapped names stack up in one sector unchecked. Only position-opening
    tickets reach here — closing sells (exits, Emergency Exit) never do.
    Per-symbol exposure is PK-15's (account_exposures): broker shares incl.
    T1 and today's, counted once with the app's, at market value."""
    limit = settings["max_sector_exposure_pct"]
    symbol = ticket_row["symbol"]
    held = {s for s, e in exposures.items() if e.value > 0}
    sectors = sector_mapping.resolve_sectors(held | {symbol})
    sector = sectors.get(symbol.strip().upper())
    if sector is None:
        message = (f"Sector: UNMAPPED — BLOCKED, a sector mapping is required for {symbol} before a new entry "
                   f"(Settings → Sector mappings) [{SECTOR_MAPPING_REQUIRED}]")
        return CheckResult("max_sector_exposure_pct", "Sector exposure · UNMAPPED", False, False, None, limit, "pct",
                           message, code=SECTOR_MAPPING_REQUIRED)

    capital = ticket_row["entry_price"] * ticket_row["quantity"]
    existing = sum(exposures[s].value for s in sorted(held) if sectors.get(s) == sector)
    current = (existing / ctx.portfolio_value * 100) if ctx.portfolio_value else 0
    pct = ((existing + capital) / ctx.portfolio_value * 100) if ctx.portfolio_value else 0
    passed = pct <= limit + _PCT_EPSILON
    message = f"{sector} sector: current {current:.1f}% → post-trade {pct:.1f}% of portfolio, limit {limit}%"
    unmapped = sorted(s for s, sec in sectors.items() if sec is None and s != symbol)
    if unmapped:
        message += f" (not counted, no sector mapped: {', '.join(unmapped)})"
    return CheckResult("max_sector_exposure_pct", f"Sector exposure · {sector}", passed, False, pct, limit, "pct",
                       message, current_value=current)


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

    Quantity (PK-13): the exit must equal the position's OPEN quantity —
    the shares the broker actually filled for it, less those its earlier
    exits already sold — never the requested quantity. A larger exit would
    oversell; a smaller one (a user-chosen partial exit) is not supported:
    an exit closes whatever the position still holds. An unknown or zero
    open quantity cannot be exited from here."""
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

    legacy_exit = conn.execute(
        "SELECT id FROM trade_tickets WHERE closes_ticket_id = ? AND id != ? AND status = 'executed' "
        "AND entry_fill_status IS NULL LIMIT 1",
        (position_id, ticket_row["id"]),
    ).fetchone()
    if legacy_exit is not None:
        return ExitProblem("EXIT_POSITION_CLOSED",
                           f"Position {position_id} was already exited by ticket {legacy_exit['id']}.", True)
    if position["closed_at"] is not None:
        return ExitProblem("EXIT_POSITION_CLOSED",
                           f"Position {position_id} is already closed (at {position['closed_at']}).", True)
    # PK-13: only an exit whose order is still in flight owns the position.
    owner = conn.execute(
        f"""
        SELECT id, status, submission_state, entry_fill_status FROM trade_tickets
        WHERE closes_ticket_id = ? AND id != ? AND {execution.EXIT_IN_FLIGHT_SQL}
        ORDER BY status = 'executed' DESC, id LIMIT 1
        """,
        (position_id, ticket_row["id"]),
    ).fetchone()
    if owner is not None and owner["status"] == "executed":
        # PK-8: EXITING — the owning exit is at the broker but has not
        # finished. Not permanent: once it ends (rejected, cancelled, or
        # filled only in part) this exit may close what is left.
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
    held = position_quantity.open_quantity(conn, position_id)
    if held is None:
        return ExitProblem("EXIT_QUANTITY_UNKNOWN",
                           f"Position {position_id}'s open quantity is unknown (a partial fill was never recorded) — "
                           f"it cannot be sized safely; check it in Kite.", True)
    if held == 0 and position_quantity.entry_ended(position):
        # Flat: earlier exits already sold everything it bought.
        return ExitProblem("EXIT_POSITION_CLOSED",
                           f"Position {position_id} was already exited in full "
                           f"({position_quantity.describe(position, held)}).", True)
    if held <= 0:
        return ExitProblem("EXIT_NOTHING_OPEN",
                           f"Position {position_id} holds no broker-confirmed shares to sell "
                           f"({position_quantity.describe(position, held)}).", True)
    if ticket_row["quantity"] != held:
        return ExitProblem("EXIT_QUANTITY_MISMATCH",
                           f"Exit quantity {ticket_row['quantity']} does not match the {held} shares position "
                           f"{position_id} holds ({position_quantity.describe(position, held)})"
                           + (" — it would sell more than the position holds." if ticket_row["quantity"] > held
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
    # source='alert_action' — see that function's docstring). Any other SELL
    # would open a short, which PK-17 refuses just below.
    if execution.is_closing_sell(ticket_row):
        return RiskCheckReport(checks=[_check_exit_or_reduction(conn, ticket_row, kite)])
    # PK-17: a new position the terminal cannot manage (anything but BUY +
    # CNC) fails here, before any broker call or sizing/exposure work —
    # whether it came from a strategy, a reverted rejection or an older row.
    unsupported = execution.unsupported_new_position(ticket_row)
    if unsupported is not None:
        return RiskCheckReport(checks=[CheckResult(
            "supported_lifecycle", "Supported Trade Lifecycle", False, False, None, None, "pct", unsupported,
            code=execution.UNSUPPORTED_MANUAL_TRADE,
        )])

    settings = get_settings(conn, account["id"])
    # A batch caller (the strategy scan) passes one context for the whole
    # batch — holdings/margins can't change mid-scan since nothing executes.
    ctx = ctx or build_portfolio_context(kite)
    if not (isinstance(ctx.portfolio_value, (int, float)) and math.isfinite(ctx.portfolio_value)
            and ctx.portfolio_value > 0):
        # Every percentage limit divides by it: never approve on 0% of nothing.
        raise _unavailable(f"portfolio value {ctx.portfolio_value!r} is not positive")
    metrics = execution.compute_risk_metrics(dict(ticket_row))
    # One exposure map (PK-15) feeds both exposure checks, from the same
    # broker snapshot as the portfolio value they divide by.
    exposures = account_exposures(conn, account["id"], ctx, ticket_row["id"])

    # All six checks always run and are always evaluated — never
    # short-circuited on the first failure — so a rejected ticket's breakdown
    # shows the full picture, not one rule at a time.
    checks = [
        _check_risk_reward(metrics, settings),
        _check_capital_per_trade(ticket_row, ctx, settings),
        _check_single_stock_exposure(ticket_row, ctx, settings, exposures),
        _check_sector_exposure(ticket_row, ctx, settings, exposures),
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
