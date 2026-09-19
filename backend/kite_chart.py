"""Kite / TradingView chart links for a ticket's symbol.

Resolution happens SERVER-SIDE at ticket-creation time (see
strategy_engine._create_entry_ticket) rather than when the link is clicked:
a signal generated at 18:22 whose symbol can't be resolved should say so at
18:22, not at 09:15 the next morning when the user is trying to act on it.
The outcome is stored on the ticket so a dead link can be disabled with a
stated reason instead of rendered and left to 404.

Only the primary Kite URL and the resolution status are persisted. The
ChartIQ variant and the public TradingView fallback are pure string
transforms of the same (exchange, tradingsymbol, instrument_token) triple,
so `fallback_chain` derives them on read — three stored columns could drift
out of agreement with each other, one cannot.
"""

import logging
from urllib.parse import quote

from backend import config, instruments, system_events
from backend.db import get_connection

logger = logging.getLogger(__name__)

# Kite renders charts through two different vendors and which one a given
# account gets is a per-user Kite profile setting, not something this app can
# detect. Both URLs are offered rather than guessing.
_KITE_BASE = "https://kite.zerodha.com/chart/ext"
_TRADINGVIEW_PUBLIC = "https://www.tradingview.com/chart/?symbol="

STATUS_OK = "ok"
STATUS_STALE_CACHE = "stale_cache"
STATUS_UNRESOLVED = "unresolved"

_STATUS_NOTES = {
    STATUS_OK: None,
    STATUS_STALE_CACHE: (
        f"The instrument list hasn't refreshed in over {config.INSTRUMENT_CACHE_MAX_AGE_HOURS}h, "
        f"so this symbol's token may be out of date — the chart could open the wrong instrument "
        f"if it was renamed by a corporate action."
    ),
    STATUS_UNRESOLVED: (
        "This symbol could not be resolved to a Kite instrument token, so a Kite chart link "
        "cannot be built. Refresh the instrument list in Settings, or use the public "
        "TradingView chart instead."
    ),
}


def build_kite_url(exchange: str, tradingsymbol: str, instrument_token: int, vendor: str = "tvc") -> str:
    """https://kite.zerodha.com/chart/ext/{vendor}/{EXCHANGE}/{SYMBOL}/{TOKEN}

    The tradingsymbol is URL-encoded rather than inserted raw: NSE series
    suffixes put characters like the '-' in 'BIRET-RR' and 'IRBINVIT-IV' into
    the path, and quote() leaves those untouched while still escaping the '&'
    a few instrument names carry. Stripping the suffix would point the link at
    a different instrument entirely, so it is always preserved verbatim.
    """
    return f"{_KITE_BASE}/{vendor}/{exchange.upper()}/{quote(tradingsymbol, safe='')}/{int(instrument_token)}"


def build_tradingview_url(exchange: str, tradingsymbol: str) -> str:
    """Public TradingView chart — works logged out, which makes it the useful
    universal fallback when the user has no active Kite web session."""
    return f"{_TRADINGVIEW_PUBLIC}{quote(f'{exchange.upper()}:{tradingsymbol}', safe='')}"


def _cache_age_hours() -> float | None:
    """None when the cache is empty — treated as stale by every caller."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT (julianday('now') - julianday(MAX(refreshed_at))) * 24 AS age_hours FROM instrument_cache"
        ).fetchone()
    finally:
        conn.close()
    return row["age_hours"] if row is not None else None


def resolve(symbol: str, exchange: str = "NSE", instrument_token: int | None = None) -> dict:
    """Resolve one symbol to its Kite chart URL and a resolution status.

    Resolution goes through instruments.get_instrument_by_symbol — the app's
    authoritative symbol->token resolver — for the ticket's OWN exchange, so
    a symbol listed on both NSE and BSE always resolves on the exchange the
    ticket was actually created for rather than whichever row happened to be
    found first. Note the instrument cache is currently NSE-equity-only
    (instruments.refresh_instrument_cache filters segment == 'NSE'), so a BSE
    ticket legitimately resolves to 'unresolved' rather than silently
    borrowing the NSE token and charting the wrong exchange.

    `instrument_token` is the token already stored on the ticket. It is used
    only as a fallback when the cache has no row for the symbol at all — a
    symbol renamed by a corporate action leaves exactly that state behind
    (the app's own ohlc_daily still holds candles under the old token while
    the refreshed cache no longer lists it). The link is still built in that
    case, but flagged, because the token may now point somewhere else.
    """
    symbol = (symbol or "").strip().upper()
    exchange = (exchange or "NSE").strip().upper()

    instrument = instruments.get_instrument_by_symbol(symbol, exchange) if symbol else None
    age_hours = _cache_age_hours()
    cache_is_stale = age_hours is None or age_hours > config.INSTRUMENT_CACHE_MAX_AGE_HOURS

    if instrument is not None:
        token = instrument["instrument_token"]
        status = STATUS_STALE_CACHE if cache_is_stale else STATUS_OK
    elif instrument_token:
        # Not in the cache, but the ticket carries a token from when it was
        # created. Build the link so the user is not left with nothing, and
        # flag it — this is the corporate-action rename case.
        token = instrument_token
        status = STATUS_STALE_CACHE
    else:
        token = None
        status = STATUS_UNRESOLVED

    return {
        "symbol": symbol,
        "exchange": exchange,
        "instrument_token": token,
        "status": status,
        "note": _STATUS_NOTES[status],
        "url": None if token is None else build_kite_url(exchange, symbol, token),
        "cache_age_hours": None if age_hours is None else round(age_hours, 2),
    }


def fallback_chain(symbol: str, exchange: str, instrument_token: int | None, status: str) -> list[dict]:
    """The four options offered in the UI's dropdown, in preference order.

    Surfaced as an explicit list the user chooses from rather than silently
    substituting one for another: which Kite chart vendor works depends on a
    per-account Kite profile setting this app cannot read, so quietly picking
    one and having it fail would look like the app is broken.

    The two Kite tiers come back `enabled: False` with a stated reason when
    the token could not be resolved, so the UI disables them with a tooltip
    instead of rendering a link that goes nowhere.
    """
    symbol = (symbol or "").strip().upper()
    exchange = (exchange or "NSE").strip().upper()
    kite_enabled = bool(instrument_token) and status != STATUS_UNRESOLVED
    disabled_reason = _STATUS_NOTES[STATUS_UNRESOLVED] if not kite_enabled else None

    return [
        {
            "key": "kite_tvc", "label": "Kite chart (TradingView)",
            "url": build_kite_url(exchange, symbol, instrument_token, "tvc") if kite_enabled else None,
            "enabled": kite_enabled, "reason": disabled_reason,
            "note": "Needs an active Kite web session in this browser.",
        },
        {
            "key": "kite_ciq", "label": "Kite chart (ChartIQ)",
            "url": build_kite_url(exchange, symbol, instrument_token, "ciq") if kite_enabled else None,
            "enabled": kite_enabled, "reason": disabled_reason,
            "note": "Use this if your Kite profile is set to ChartIQ rather than TradingView.",
        },
        {
            "key": "tradingview_public", "label": "TradingView (public)",
            "url": build_tradingview_url(exchange, symbol),
            "enabled": True, "reason": None,
            "note": "Works without any login, but shows no position levels.",
        },
        {
            "key": "copy_symbol", "label": f"Copy “{symbol}” to clipboard",
            "url": None, "enabled": True, "reason": None,
            "note": "Paste it into whichever charting tool you prefer.",
        },
    ]


def resolve_and_log(conn, account_id: int, symbol: str, exchange: str, instrument_token: int | None) -> dict:
    """resolve(), plus a system_events row whenever resolution did not come
    back clean — consistent with how other notable-but-non-fatal conditions
    (mode switches, batch cancels, emergency exits) are recorded. Caller
    commits; a chart link is never important enough to fail ticket creation,
    so any unexpected error is swallowed into an 'unresolved' result.
    """
    try:
        result = resolve(symbol, exchange, instrument_token)
    except Exception:
        logger.exception("Kite chart resolution failed for %s:%s", exchange, symbol)
        result = {
            "symbol": symbol, "exchange": exchange, "instrument_token": None,
            "status": STATUS_UNRESOLVED, "note": _STATUS_NOTES[STATUS_UNRESOLVED],
            "url": None, "cache_age_hours": None,
        }

    if result["status"] != STATUS_OK:
        system_events.log_event(
            conn, account_id, "kite_chart_unresolved",
            f"{exchange}:{symbol} chart link {result['status']} "
            f"(instrument cache age {result['cache_age_hours']}h): {result['note']}",
        )
    return result
