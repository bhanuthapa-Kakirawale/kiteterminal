"""What the broker says the account holds, per instrument, without double counting.

One pure function over a holdings list and a positions payload, as Kite's
GET /portfolio/holdings and GET /portfolio/positions return them. Shared by
the reconciliation cycle (broker quantity vs the app's open positions) and
Emergency Exit (may the app's open quantity be sold now?). No broker call is
made here.

Identity is the exchange plus the trading symbol, never the symbol alone:
NSE:XYZ and BSE:XYZ are separate keys. A demat holding is fungible across
exchanges, but this module does not bridge them (it has no ISIN for the app's
positions): a holding listed under another exchange is reported
(`same_symbol_elsewhere`) so the caller can tell the user, and never counted
for the app's instrument. Where the broker gives an instrument_token it is
kept, and two different tokens on one key are a problem.

Per key (EXCHANGE, TRADINGSYMBOL):

  settled   holdings.quantity       settled delivery shares (start of day)
  t1        holdings.t1_quantity    bought, not yet settled (T+1)
  day_net   positions.net, product CNC only: quantity - overnight_quantity
            The day's delivery buys minus sells. A CNC position carried from
            the previous session (overnight_quantity) is already in holdings
            as T1, so only the day's change is added: no double counting.
  total     settled + t1 + day_net  what can be sold for delivery now

Not counted: holdings.used_quantity (today's sells already appear in
positions), collateral / authorised fields (they describe the same shares),
and every non-CNC position (MIS / NRML are intraday or derivatives, never a
delivery holding; they are listed under `other_products`).

Fails closed per key: a missing or non-numeric quantity, a negative holding,
a key listed twice, conflicting tokens or a negative total puts a reason in
`problems`; the caller must then not trust that key's figures. A payload that
is not a list / not a positions object raises BrokerDataError.
"""

import math
from dataclasses import dataclass, field


class BrokerDataError(ValueError):
    """The holdings or positions payload is not usable at all."""


@dataclass
class InstrumentQuantity:
    exchange: str
    tradingsymbol: str
    settled: int = 0
    t1: int = 0
    day_net: int = 0
    tokens: set = field(default_factory=set)
    other_products: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    _holding_rows: int = 0
    _position_rows: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.settled + self.t1 + self.day_net

    @property
    def key(self) -> tuple[str, str]:
        return self.exchange, self.tradingsymbol

    def describe(self) -> str:
        return (f"{self.exchange}:{self.tradingsymbol} settled {self.settled}, T1 {self.t1}, "
                f"today's CNC net {self.day_net:+d} = {self.total}")


def norm(value) -> str:
    return str(value or "").strip().upper()


def _int(row: dict, name: str, required: bool) -> int | None:
    value = row.get(name)
    if value is None:
        return None if required else 0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or value != int(value):
        return None
    return int(value)


def _token(row: dict) -> int | None:
    value = row.get("instrument_token")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value:
        return None
    return int(value)


def instrument_quantities(holdings, positions) -> dict[tuple[str, str], InstrumentQuantity]:
    if not isinstance(holdings, list):
        raise BrokerDataError("holdings is not a list")
    if not isinstance(positions, dict) or not isinstance(positions.get("net"), list):
        raise BrokerDataError("positions has no 'net' list")
    out: dict[tuple[str, str], InstrumentQuantity] = {}

    def entry(row: dict, where: str) -> InstrumentQuantity | None:
        exchange, symbol = norm(row.get("exchange")), norm(row.get("tradingsymbol"))
        if not exchange or not symbol:
            raise BrokerDataError(f"a {where} row has no exchange or trading symbol")
        q = out.setdefault((exchange, symbol), InstrumentQuantity(exchange, symbol))
        token = _token(row)
        if token is not None:
            q.tokens.add(token)
            if len(q.tokens) > 1:
                q.problems.append(f"conflicting instrument tokens {sorted(q.tokens)}")
        return q

    for h in holdings:
        if not isinstance(h, dict):
            raise BrokerDataError("a holdings row is not an object")
        q = entry(h, "holdings")
        q._holding_rows += 1
        if q._holding_rows > 1:
            q.problems.append("the holding is listed more than once")
        settled, t1 = _int(h, "quantity", True), _int(h, "t1_quantity", False)
        if settled is None or t1 is None:
            q.problems.append(f"holding quantity/t1_quantity unreadable ({h.get('quantity')!r}, "
                              f"{h.get('t1_quantity')!r})")
            continue
        if settled < 0 or t1 < 0:
            q.problems.append(f"negative holding quantity ({settled}, T1 {t1})")
            continue
        q.settled += settled
        q.t1 += t1

    for p in positions["net"]:
        if not isinstance(p, dict):
            raise BrokerDataError("a positions row is not an object")
        q = entry(p, "positions")
        product = norm(p.get("product"))
        quantity, overnight = _int(p, "quantity", True), _int(p, "overnight_quantity", False)
        if product != "CNC":
            q.other_products.append(f"{product or '?'} {p.get('quantity')!r}")
            continue
        q._position_rows[product] = q._position_rows.get(product, 0) + 1
        if q._position_rows[product] > 1:
            q.problems.append("the CNC position is listed more than once")
        if quantity is None or overnight is None:
            q.problems.append(f"CNC position quantity/overnight_quantity unreadable ({p.get('quantity')!r}, "
                              f"{p.get('overnight_quantity')!r})")
            continue
        q.day_net += quantity - overnight

    for q in out.values():
        if not q.problems and q.total < 0:
            q.problems.append(f"the broker figures add up to a negative quantity ({q.describe()})")
    return out


def same_symbol_elsewhere(quantities: dict, exchange: str, symbol: str) -> list[InstrumentQuantity]:
    """Rows for the same trading symbol under another exchange: possibly the same
    security, never counted for this one."""
    exchange, symbol = norm(exchange), norm(symbol)
    return [q for (ex, sym), q in quantities.items() if sym == symbol and ex != exchange]
