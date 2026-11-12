"""Instrument-specific margin preflight for a LIVE new entry.

Fresh broker reads, made at the final gate and never cached:

  kite.quote([key])        MARKET orders only (M3): GET /quote, for the
                           instrument's last price and upper circuit limit.

  kite.margins("equity")   GET /user/margins/equity: the account's funds.
  kite.order_margins([..]) POST /margins/orders: Kite's own calculation of the
                           margin this exact order (instrument, exchange,
                           side, product, variety, order type, quantity, price)
                           would block, considering existing positions and
                           open orders. It places nothing; it is a POST only
                           because the order travels in the body. With
                           KITE_TERMINAL_BROKER_WRITES=disabled it is refused
                           locally, like every non-GET, and the gate then
                           fails closed (no entry could be sent anyway).

Pricing (M3). The order the margin is computed for is never priced at 0:

  LIMIT    the ticket's limit price, which must be a finite positive number.
  MARKET   a MARKET BUY can fill at any price up to the instrument's upper
           circuit limit (NSE/BSE refuse trades above it; for a stock with a
           dynamic price band the quote reports the band's current upper
           limit). The margin request is therefore sized as a hypothetical
           LIMIT BUY at the upper circuit, from a fresh quote: for a CNC
           delivery BUY the margin blocked is the order value and the charges
           are value-based, so that is an upper bound on what the MARKET
           order can block. A missing, non-numeric or non-positive last price
           or circuit, or a circuit below the last price, refuses. This is
           deliberately conservative: an account with funds between the
           expected and the worst-case value is refused.

Rules (no new tunable threshold; each is a property of the data):

  available  min(available.live_balance, net - available.collateral).
             live_balance is Kite's "current available balance"; net includes
             the margin from pledged holdings (collateral), which cannot fund
             a delivery (CNC) purchase, so it is taken out.
  required   Kite's order total + its charges total. For a cash-equity CNC
             BUY, the delivery margin is the full order value, so if Kite's
             total is ever below the notional (at the margin price above) the
             notional is used instead (flagged in the evidence). The notional
             is never ASSUMED to be the margin: Kite's figure is required, and
             missing data refuses.
  charges    Kite's charges.total is REQUIRED and must be > 0: a delivery BUY
             always carries STT and stamp duty, so a missing or zero figure
             means the data is incomplete. It is never defaulted to zero.
  refuse     available < required                      INSUFFICIENT_FUNDS (409)
             any read error; segment not enabled; a missing, non-numeric,
             non-finite or negative funds field; an unusable price, quote or
             circuit; an order-margin row for a different instrument/exchange
             or none at all; a total <= 0; missing or non-positive charges
                                                       MARGIN_DATA_UNAVAILABLE (503)

Only NSE equity BUY + CNC entries exist (PK-17: execution.SUPPORTED_ENTRY;
the instrument cache holds NSE EQ only). Derivatives margin (SPAN +
exposure, NRML/MIS) has no path here: such an entry is refused upstream, and
this module refuses anything that is not cash-equity CNC rather than guess.

Writes stay off. order_margins is a POST: with KITE_TERMINAL_BROKER_WRITES
disabled, or without the Kite REST proxy, it is refused locally like every
write, and the gate fails closed. Nothing here, or anywhere, re-enables a
write to make the calculation work.

Remaining assumptions (documented, not verified against a real account):
Kite's field semantics (live_balance, collateral, net, order total,
charges.total) follow Kite Connect's published docs; the upper circuit (or
dynamic band) reported by the quote is the highest price a MARKET BUY can fill
at; Kite's margin for a LIMIT CNC BUY equals what a MARKET one of the same
value blocks. Kite's own RMS check at order placement remains the final
broker-side control.
"""

import math

from backend.account_safety import SafetyFailure

INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
MARGIN_DATA_UNAVAILABLE = "MARGIN_DATA_UNAVAILABLE"
MARGIN_PREFLIGHT_UNSUPPORTED = "MARGIN_PREFLIGHT_UNSUPPORTED"

CASH_EQUITY = (("NSE", "BSE"), "CNC")


class _Bad(ValueError):
    pass


def _number(container, name: str, where: str) -> float:
    if not isinstance(container, dict):
        raise _Bad(f"{where} is missing")
    value = container.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _Bad(f"{where}.{name} is missing or not a number ({value!r})")
    return float(value)


def _positive(value, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise _Bad(f"{what} is missing or not a positive number ({value!r})")
    return float(value)


def margin_price(kite, ticket) -> tuple[float, str, dict]:
    """(price, basis, evidence): the price the margin request is sized at. Never 0."""
    order_type = ticket["entry_order_type"]
    if order_type == "LIMIT":
        return _positive(ticket["entry_price"], "the LIMIT price"), "limit_price", {}
    if order_type != "MARKET":
        raise _Bad(f"order type {order_type!r} has no pricing rule")
    key = f"{ticket['exchange']}:{ticket['symbol']}"
    quotes = kite.quote([key])
    quote = quotes.get(key) if isinstance(quotes, dict) else None
    if not isinstance(quote, dict):
        raise _Bad(f"the quote for {key} is missing")
    last = _positive(quote.get("last_price"), f"{key} last_price")
    upper = _positive(quote.get("upper_circuit_limit"), f"{key} upper_circuit_limit")
    if upper < last:
        raise _Bad(f"{key} upper circuit {upper:g} is below its last price {last:g}")
    return upper, "upper_circuit", {"quote_last_price": last, "upper_circuit_limit": upper}


def order_params(ticket, variety: str, price: float) -> dict:
    """The order Kite's margin calculation is asked about: the ticket's own instrument, side,
    product, variety and quantity, priced at `price` (never 0). A MARKET entry is asked about as
    the LIMIT at its worst possible fill price (the upper circuit), see margin_price()."""
    return {
        "exchange": ticket["exchange"], "tradingsymbol": ticket["symbol"],
        "transaction_type": ticket["transaction_type"], "variety": variety, "product": ticket["product_type"],
        "order_type": "LIMIT", "quantity": ticket["quantity"], "price": _positive(price, "the margin price"),
    }


def evaluate(kite, ticket, variety: str = "regular", reference_price: float | None = None) -> dict:
    """The preflight's figures and verdict, without raising:
    {verdict: PASS | INSUFFICIENT_FUNDS | MARGIN_DATA_UNAVAILABLE | MARGIN_PREFLIGHT_UNSUPPORTED,
     available, required, order_margin, charges, order_value, notional_floor_applied, message}.
    reference_price is the caller's fresh LTP, kept in the evidence only: the margin is sized at
    margin_price() (the LIMIT price, or for MARKET the upper circuit from a fresh quote)."""
    out = {"symbol": ticket["symbol"], "exchange": ticket["exchange"], "product": ticket["product_type"],
           "quantity": ticket["quantity"], "order_type": ticket["entry_order_type"], "reference_price": reference_price,
           "margin_price": None, "margin_price_basis": None, "order_value": None, "available": None,
           "required": None, "order_margin": None, "charges": None, "notional_floor_applied": False}
    exchanges, product = CASH_EQUITY
    if (ticket["transaction_type"], ticket["product_type"]) != ("BUY", product) or ticket["exchange"] not in exchanges:
        return {**out, "verdict": MARGIN_PREFLIGHT_UNSUPPORTED,
                "message": f"No margin model for {ticket['transaction_type']} {ticket['product_type']} on "
                           f"{ticket['exchange']}: only cash-equity CNC BUY entries are supported."}
    try:
        quantity = ticket["quantity"]
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise _Bad(f"the quantity is not a positive whole number ({quantity!r})")
        price, basis, price_evidence = margin_price(kite, ticket)
        order_value = price * quantity
        out.update(margin_price=price, margin_price_basis=basis, order_value=order_value, **price_evidence)
        funds = kite.margins("equity")
        if isinstance(funds, dict) and "equity" in funds and "net" not in funds:
            funds = funds["equity"]  # some clients return every segment
        if not isinstance(funds, dict):
            raise _Bad("equity funds payload is not an object")
        if funds.get("enabled") is False:
            raise _Bad("the equity segment is not enabled for this account")
        available_block = funds.get("available")
        live_balance = _number(available_block, "live_balance", "equity.available")
        collateral = _number(available_block, "collateral", "equity.available")
        net = _number(funds, "net", "equity")
        if collateral < 0:
            raise _Bad(f"equity.available.collateral is negative ({collateral:g})")
        available = min(live_balance, net - collateral)

        rows = kite.order_margins([order_params(ticket, variety, price)])
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise _Bad("the order-margin reply is not exactly one row")
        row = rows[0]
        if (str(row.get("tradingsymbol", "")).upper() != str(ticket["symbol"]).upper()
                or str(row.get("exchange", "")).upper() != str(ticket["exchange"]).upper()):
            raise _Bad(f"the order-margin reply is for {row.get('exchange')}:{row.get('tradingsymbol')}, not "
                       f"{ticket['exchange']}:{ticket['symbol']}")
        total = _number(row, "total", "order margin")
        if total <= 0:
            raise _Bad(f"the order margin total is not positive ({total:g})")
        # Required, never defaulted: a delivery BUY always carries STT and stamp duty.
        charges = _number(row.get("charges"), "total", "order margin charges")
        if charges <= 0:
            raise _Bad(f"the order charges total is not positive ({charges:g}); a delivery BUY always carries "
                       f"charges")
    except Exception as e:
        return {**out, "verdict": MARGIN_DATA_UNAVAILABLE,
                "message": f"Margin data for {ticket['symbol']} is unavailable or inconsistent "
                           f"({type(e).__name__}: {e})."}
    floor_applied = total < order_value
    margin = max(total, order_value)
    required = margin + charges
    out.update(available=available, required=required, order_margin=total, charges=charges,
               notional_floor_applied=floor_applied)
    if available + 1e-9 < required:
        return {**out, "verdict": INSUFFICIENT_FUNDS,
                "message": f"Available funds ₹{available:,.2f} are below the estimated requirement ₹{required:,.2f} "
                           f"(margin ₹{margin:,.2f} + charges ₹{charges:,.2f}) for {ticket['quantity']} "
                           f"{ticket['symbol']}."}
    return {**out, "verdict": "PASS",
            "message": f"Available funds ₹{available:,.2f} cover the estimated requirement ₹{required:,.2f}"
                       f"{' (sized at the upper circuit, the worst price a MARKET order can fill at)' if basis == 'upper_circuit' else ''}."}


def check(kite, ticket, variety: str, reference_price: float | None) -> tuple[list[SafetyFailure], dict]:
    """The final-gate form: (failures, evidence)."""
    result = evaluate(kite, ticket, variety, reference_price)
    verdict = result["verdict"]
    if verdict == "PASS":
        return [], result
    if verdict == INSUFFICIENT_FUNDS:
        return [SafetyFailure(INSUFFICIENT_FUNDS, result["message"] + " Nothing was executed.")], result
    return [SafetyFailure(verdict, result["message"] + " Without it the entry cannot be checked against the "
                                                      "account's funds, so nothing was executed.",
                          http_status=503)], result
