import logging
from collections.abc import Iterable

from kiteconnect import KiteConnect

from backend.db import get_connection

logger = logging.getLogger(__name__)

# Kite Connect has no sector-classification endpoint. This is a small,
# manually-maintained lookup of NSE names the terminal holds, watches or has
# traded — the one static source of sector labels. It does not cover all of
# NSE: a symbol it does not know (and nobody has mapped in Settings) has NO
# sector, and a new entry in it is refused by the risk engine until one is
# mapped (PK-14, SECTOR_MAPPING_REQUIRED).
#
# Taxonomy: broad groups, on purpose — a sector limit only protects if
# related names share one bucket. Banks are kept apart from NBFCs/insurers
# (the Nifty Bank / Nifty Financial Services split); capital goods and
# defence manufacturers are "Infra"; retail, food delivery and airlines are
# "Consumer Services".
SECTOR_LOOKUP: dict[str, str] = {
    # IT
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT", "TECHM": "IT",
    "LTIM": "IT", "MPHASIS": "IT", "COFORGE": "IT", "LTTS": "IT", "FRACTAL": "IT",
    # Banking
    "HDFCBANK": "Banking", "ICICIBANK": "Banking", "SBIN": "Banking",
    "KOTAKBANK": "Banking", "AXISBANK": "Banking", "INDUSINDBK": "Banking",
    "BANKBARODA": "Banking", "PNB": "Banking", "IDFCFIRSTB": "Banking",
    # FMCG
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG", "DABUR": "FMCG", "TATACONSUM": "FMCG",
    "MARICO": "FMCG", "GODREJCP": "FMCG", "COLPAL": "FMCG", "AWL": "FMCG",
    # Pharma
    "SUNPHARMA": "Pharma", "DRREDDY": "Pharma", "CIPLA": "Pharma",
    "DIVISLAB": "Pharma", "LUPIN": "Pharma", "AUROPHARMA": "Pharma",
    "BIOCON": "Pharma", "ALKEM": "Pharma", "TORNTPHARM": "Pharma",
    # Auto (TMPV/TMCV: the two Tata Motors companies after the demerger)
    "MARUTI": "Auto", "TATAMOTORS": "Auto", "TMPV": "Auto", "TMCV": "Auto", "M&M": "Auto",
    "BAJAJ-AUTO": "Auto", "EICHERMOT": "Auto", "HEROMOTOCO": "Auto",
    "TVSMOTOR": "Auto", "ASHOKLEY": "Auto", "HYUNDAI": "Auto", "ATHERENERG": "Auto",
    # Energy (oil & gas, power, coal)
    "RELIANCE": "Energy", "ONGC": "Energy", "NTPC": "Energy",
    "POWERGRID": "Energy", "BPCL": "Energy", "IOC": "Energy",
    "COALINDIA": "Energy", "GAIL": "Energy", "ADANIGREEN": "Energy", "ADANIPOWER": "Energy",
    # Metals
    "TATASTEEL": "Metals", "JSWSTEEL": "Metals", "HINDALCO": "Metals",
    "VEDL": "Metals", "JINDALSTEL": "Metals", "HINDZINC": "Metals",
    # Cement
    "ULTRACEMCO": "Cement", "SHREECEM": "Cement", "AMBUJACEM": "Cement",
    "ACC": "Cement", "GRASIM": "Cement",
    # Telecom
    "BHARTIARTL": "Telecom", "IDEA": "Telecom", "INDUSTOWER": "Telecom",
    # Financial Services / NBFC / insurance
    "BAJFINANCE": "Financial Services", "BAJAJFINSV": "Financial Services",
    "HDFCLIFE": "Financial Services", "SBILIFE": "Financial Services",
    "ICICIPRULI": "Financial Services", "ICICIGI": "Financial Services",
    "RECLTD": "Financial Services", "SHRIRAMFIN": "Financial Services",
    # Infra / Capital goods (including defence)
    "LT": "Infra", "ADANIPORTS": "Infra", "ADANIENT": "Infra", "SIEMENS": "Infra",
    "BEL": "Infra", "HAL": "Infra", "SOLARINDS": "Infra",
    # Consumer durables
    "TITAN": "Consumer Durables", "ASIANPAINT": "Consumer Durables",
    "HAVELLS": "Consumer Durables", "VOLTAS": "Consumer Durables",
    # Consumer services (retail, food delivery, airlines)
    "DMART": "Consumer Services", "ETERNAL": "Consumer Services", "INDIGO": "Consumer Services",
}


def _normalise(symbols: Iterable[str]) -> list[str]:
    return sorted({s.strip().upper() for s in symbols if s and s.strip()})


def seed_symbols(symbols: Iterable[str]) -> int:
    """Stores SECTOR_LOOKUP's sector for each of `symbols` that has no row yet.
    Never overwrites a row — a sector set by hand in Settings always wins.
    Deterministic and idempotent. Returns the number of rows added."""
    rows = [(s, SECTOR_LOOKUP[s]) for s in _normalise(symbols) if s in SECTOR_LOOKUP]
    if not rows:
        return 0
    conn = get_connection()
    try:
        before = conn.total_changes
        conn.executemany("INSERT OR IGNORE INTO sector_mapping (symbol, sector) VALUES (?, ?)", rows)
        added = conn.total_changes - before
        conn.commit()
        return added
    finally:
        conn.close()


def seed_known_symbols() -> int:
    """Seeds every symbol the terminal already knows without asking Kite:
    the watchlist and every ticket ever created. Run at startup."""
    conn = get_connection()
    try:
        symbols = [r["symbol"] for r in conn.execute(
            "SELECT symbol FROM watchlist UNION SELECT symbol FROM trade_tickets")]
    finally:
        conn.close()
    return seed_symbols(symbols)


def seed_from_holdings(kite: KiteConnect) -> int:
    """Seeds the symbols held at the broker: holdings and today's positions."""
    symbols = {h["tradingsymbol"] for h in kite.holdings()}
    try:
        symbols |= {p["tradingsymbol"] for p in kite.positions().get("net", [])}
    except Exception:
        logger.exception("Could not read positions() for sector seeding; holdings were still seeded")
    return seed_symbols(symbols)


def resolve_sectors(symbols: Iterable[str]) -> dict[str, str | None]:
    """The sector of each symbol: its sector_mapping row (seeded, or set by
    hand — which wins), else SECTOR_LOOKUP, else None (unmapped). One read,
    so a symbol never seeded yet (a strategy candidate) still resolves."""
    wanted = _normalise(symbols)
    conn = get_connection()
    try:
        stored = {r["symbol"]: r["sector"] for r in conn.execute("SELECT symbol, sector FROM sector_mapping")}
    finally:
        conn.close()
    return {s: stored.get(s) or SECTOR_LOOKUP.get(s) for s in wanted}


def get_sector(symbol: str) -> str | None:
    return resolve_sectors([symbol]).get(symbol.strip().upper())


def upsert_sector(symbol: str, sector: str) -> dict:
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO sector_mapping (symbol, sector) VALUES (?, ?)
            ON CONFLICT(symbol) DO UPDATE SET sector = excluded.sector
            """,
            (symbol, sector),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM sector_mapping WHERE symbol = ?", (symbol,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_mappings() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM sector_mapping ORDER BY symbol").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def symbols_in_sector(sector: str) -> list[str]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT symbol FROM sector_mapping WHERE sector = ?", (sector,)).fetchall()
        return [r["symbol"] for r in rows]
    finally:
        conn.close()


def delete_sector(symbol: str) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM sector_mapping WHERE symbol = ?", (symbol,))
        conn.commit()
    finally:
        conn.close()


def list_sectors() -> list[str]:
    """Distinct sector names already in use, unioned with the static
    SECTOR_LOOKUP values — so the suggestion list is useful even before
    anything's been mapped, and existing spellings win over new ones
    (e.g. "IT" doesn't silently fork into "Information Technology")."""
    conn = get_connection()
    try:
        stored = {r["sector"] for r in conn.execute("SELECT DISTINCT sector FROM sector_mapping")}
    finally:
        conn.close()
    return sorted(stored | set(SECTOR_LOOKUP.values()))


def find_unmapped_symbols(kite: KiteConnect) -> list[str]:
    """Held, position or watchlist symbols with no sector at all — exactly
    the ones a new entry would be refused for (SECTOR_MAPPING_REQUIRED)."""
    holdings_symbols = {h["tradingsymbol"] for h in kite.holdings()}
    positions_symbols = {p["tradingsymbol"] for p in kite.positions().get("net", [])}
    conn = get_connection()
    try:
        watched = {r["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM watchlist")}
    finally:
        conn.close()
    sectors = resolve_sectors(holdings_symbols | positions_symbols | watched)
    return sorted(s for s, sector in sectors.items() if sector is None)
