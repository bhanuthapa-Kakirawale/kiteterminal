"""PK-23: read-only Volume Intelligence for one symbol.

GET /api/volume-intelligence/{symbol}?as_of=YYYY-MM-DD&strategy=<name>&instrument_token=<int>

Stored history only: no broker call, no write, no order. The features are
descriptive (see backend/volume_intelligence.py); a flagged, stale or
missing history is reported in `data_quality` / `freshness`, never hidden.

A symbol is resolved to ONE instrument token, because the same
tradingsymbol can have both an NSE and a BSE series stored (holdings pull
either) whose volumes are not comparable. Order: the watchlist's token, then
the NSE instrument cache, then stored history preferring an NSE token.
`instrument_token` overrides the lookup.
"""

from datetime import date

from fastapi import APIRouter, HTTPException, Query

from backend import strategy_engine, volume_intelligence
from backend.accounts import get_default_account
from backend.db import get_connection
from backend.strategies import STRATEGIES

router = APIRouter()

_NSE_SEGMENT = 1  # a Kite instrument token's low byte is its exchange segment


def _prefer_nse(tokens: list[int]) -> int | None:
    if not tokens:
        return None
    nse = [t for t in tokens if t & 0xFF == _NSE_SEGMENT]
    return sorted(nse or tokens)[0]


def resolve_instrument(conn, symbol: str, instrument_token: int | None = None) -> dict | None:
    symbol = symbol.strip().upper()
    if instrument_token is not None:
        row = conn.execute(
            "SELECT symbol FROM ohlc_daily WHERE instrument_token = ? ORDER BY date DESC LIMIT 1", (instrument_token,)
        ).fetchone() or conn.execute(
            "SELECT tradingsymbol AS symbol FROM instrument_cache WHERE instrument_token = ?", (instrument_token,)
        ).fetchone()
        return None if row is None else {"instrument_token": instrument_token, "symbol": row["symbol"],
                                         "source": "instrument_token"}
    lookups = (
        ("watchlist", "SELECT DISTINCT instrument_token FROM watchlist WHERE UPPER(symbol) = ?"),
        ("instrument_cache", "SELECT instrument_token FROM instrument_cache "
                             "WHERE UPPER(tradingsymbol) = ? AND exchange = 'NSE'"),
        ("stored_history", "SELECT DISTINCT instrument_token FROM ohlc_daily WHERE UPPER(symbol) = ?"),
    )
    for source, sql in lookups:
        token = _prefer_nse([r["instrument_token"] for r in conn.execute(sql, (symbol,)).fetchall()])
        if token is not None:
            return {"instrument_token": token, "symbol": symbol, "source": source}
    return None


def _strategy_params(conn, strategy: str) -> dict:
    """The parameters the scanner would use now (the default account's saved
    config, backfilled with module defaults) — read-only."""
    try:
        account = get_default_account(conn)
    except RuntimeError:
        return dict(STRATEGIES[strategy].DEFAULT_PARAMS)
    return strategy_engine.get_config(conn, account["id"], strategy)["params"]


@router.get("/api/volume-intelligence/{symbol}")
def get_volume_intelligence(
    symbol: str,
    as_of: str | None = Query(None, description="YYYY-MM-DD; the last stored session on or before it"),
    strategy: str | None = Query(None, description="add that strategy's volume rule, side by side"),
    instrument_token: int | None = Query(None),
):
    if as_of is not None:
        try:
            as_of = date.fromisoformat(as_of).isoformat()
        except ValueError:
            raise HTTPException(422, {"error_code": "INVALID_DATE", "message": "as_of must be YYYY-MM-DD."})
    if strategy is not None and strategy not in STRATEGIES:
        raise HTTPException(422, {"error_code": "UNKNOWN_STRATEGY",
                                  "message": f"strategy must be one of {sorted(STRATEGIES)}."})
    conn = get_connection()
    try:
        resolved = resolve_instrument(conn, symbol, instrument_token)
        if resolved is None:
            raise HTTPException(404, {"error_code": "UNKNOWN_SYMBOL",
                                      "message": f"No stored history or NSE instrument for {symbol!r}."})
        params = _strategy_params(conn, strategy) if strategy else None
        features = volume_intelligence.features_for_instrument(
            conn, resolved["instrument_token"], resolved["symbol"], as_of=as_of, strategy=strategy, params=params)
    finally:
        conn.close()
    return {"data": {**features, "resolved": resolved}}
