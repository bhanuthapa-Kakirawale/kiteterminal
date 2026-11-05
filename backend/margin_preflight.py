"""Instrument-specific margin preflight for a LIVE new entry.

Two fresh broker reads, made at the final gate and never cached:

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

Rules (no new tunable threshold; each is a property of the data):

  available  min(available.live_balance, net - available.collateral).
             live_balance is Kite's "current available balance"; net includes
             the margin from pledged holdings (collateral), which cannot fund
             a delivery (CNC) purchase, so it is taken out.
  required   Kite's order total + its charges total. For a cash-equity CNC
             BUY, the delivery margin is the full order value, so if Kite's
             total is ever below the notional the notional is used instead
             (flagged in the evidence). The notional is never ASSUMED to be
             the margin: Kite's figure is required, and missing data refuses.
  refuse     available < required                      INSUFFICIENT_FUNDS (409)
             any read error; segment not enabled; a missing, non-numeric,
             non-finite or negative funds field; an order-margin row for a
             different instrument/exchange or none at all; a total <= 0
                                                       MARGIN_DATA_UNAVAILABLE (503)

Only NSE equity BUY + CNC entries exist (PK-17: execution.SUPPORTED_ENTRY;
the instrument cache holds NSE EQ only). Derivatives margin (SPAN +
exposure, NRML/MIS) has no path here: such an entry is refused upstream, and
this module refuses anything that is not cash-equity CNC rather than guess.

Kite's own RMS check at order placement remains the final broker-side
control. The field semantics above follow Kite Connect's published docs and
are unverified against a real account.
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


def order_params(ticket, variety: str, price: float | None) -> dict:
    params = {
        "exchange": ticket["exchange"], "tradingsymbol": ticket["symbol"],
        "transaction_type": ticket["transaction_type"], "variety": variety, "product": ticket["product_type"],
        "order_type": ticket["entry_order_type"], "quantity": ticket["quantity"],
    }
    params["price"] = price if price is not None else 0
    return params


def evaluate(kite, ticket, variety: str = "regular", reference_price: float | None = None) -> dict:
    """The preflight's figures and verdict, without raising:
    {verdict: PASS | INSUFFICIENT_FUNDS | MARGIN_DATA_UNAVAILABLE | MARGIN_PREFLIGHT_UNSUPPORTED,
     available, required, order_margin, charges, order_value, notional_floor_applied, message}.
    reference_price is the price the order value is computed at (the LTP for a MARKET order)."""
    price = reference_price if reference_price is not None else ticket["entry_price"]
    order_value = float(price) * int(ticket["quantity"])
    out = {"symbol": ticket["symbol"], "exchange": ticket["exchange"], "product": ticket["product_type"],
           "quantity": ticket["quantity"], "order_value": order_value, "available": None, "required": None,
           "order_margin": None, "charges": None, "notional_floor_applied": False}
    exchanges, product = CASH_EQUITY
    if (ticket["transaction_type"], ticket["product_type"]) != ("BUY", product) or ticket["exchange"] not in exchanges:
        return {**out, "verdict": MARGIN_PREFLIGHT_UNSUPPORTED,
                "message": f"No margin model for {ticket['transaction_type']} {ticket['product_type']} on "
                           f"{ticket['exchange']}: only cash-equity CNC BUY entries are supported."}
    try:
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

        rows = kite.order_margins([order_params(ticket, variety, price if ticket["entry_order_type"] == "LIMIT"
                                                else None)])
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
        charges = _number(row.get("charges"), "total", "order margin charges") if row.get("charges") is not None \
            else 0.0
        if charges < 0:
            raise _Bad(f"the order charges total is negative ({charges:g})")
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
            "message": f"Available funds ₹{available:,.2f} cover the estimated requirement ₹{required:,.2f}."}


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
