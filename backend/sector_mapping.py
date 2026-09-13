from kiteconnect import KiteConnect

from backend.db import get_connection

# Kite Connect has no sector-classification endpoint. This is a small,
# manually-maintained lookup of well-known NSE large-caps, deliberately not
# meant to cover all of NSE — see backend/db.py's sector_mapping table and
# the risk engine's "unknown sector, exposure check skipped" fallback for
# symbols not covered here.
SECTOR_LOOKUP: dict[str, str] = {
    # IT
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT", "TECHM": "IT",
    "LTIM": "IT", "MPHASIS": "IT", "COFORGE": "IT",
    # Banking
    "HDFCBANK": "Banking", "ICICIBANK": "Banking", "SBIN": "Banking",
    "KOTAKBANK": "Banking", "AXISBANK": "Banking", "INDUSINDBK": "Banking",
    "BANKBARODA": "Banking", "PNB": "Banking", "IDFCFIRSTB": "Banking",
    # FMCG
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG", "DABUR": "FMCG", "TATACONSUM": "FMCG",
    "MARICO": "FMCG", "GODREJCP": "FMCG", "COLPAL": "FMCG",
    # Pharma
    "SUNPHARMA": "Pharma", "DRREDDY": "Pharma", "CIPLA": "Pharma",
    "DIVISLAB": "Pharma", "LUPIN": "Pharma", "AUROPHARMA": "Pharma",
    "BIOCON": "Pharma", "ALKEM": "Pharma",
    # Auto
    "MARUTI": "Auto", "TATAMOTORS": "Auto", "M&M": "Auto",
    "BAJAJ-AUTO": "Auto", "EICHERMOT": "Auto", "HEROMOTOCO": "Auto",
    "TVSMOTOR": "Auto", "ASHOKLEY": "Auto",
    # Energy
    "RELIANCE": "Energy", "ONGC": "Energy", "NTPC": "Energy",
    "POWERGRID": "Energy", "BPCL": "Energy", "IOC": "Energy",
    "COALINDIA": "Energy", "GAIL": "Energy", "ADANIGREEN": "Energy",
    # Metals
    "TATASTEEL": "Metals", "JSWSTEEL": "Metals", "HINDALCO": "Metals",
    "VEDL": "Metals", "JINDALSTEL": "Metals",
    # Cement
    "ULTRACEMCO": "Cement", "SHREECEM": "Cement", "AMBUJACEM": "Cement",
    "ACC": "Cement",
    # Telecom
    "BHARTIARTL": "Telecom", "IDEA": "Telecom", "INDUS TOWERS": "Telecom",
    # Financial Services / NBFC
    "BAJFINANCE": "Financial Services", "BAJAJFINSV": "Financial Services",
    "HDFCLIFE": "Financial Services", "SBILIFE": "Financial Services",
    "ICICIPRULI": "Financial Services", "ICICIGI": "Financial Services",
    # Infra / Capital goods
    "LT": "Infra", "ADANIPORTS": "Infra", "ADANIENT": "Infra", "SIEMENS": "Infra",
    # Consumer durables
    "TITAN": "Consumer Durables", "ASIANPAINT": "Consumer Durables",
    "HAVELLS": "Consumer Durables", "VOLTAS": "Consumer Durables",
}


def seed_from_holdings(kite: KiteConnect) -> int:
    holdings = kite.holdings()
    symbols = {h["tradingsymbol"] for h in holdings}

    conn = get_connection()
    try:
        existing = {r["symbol"] for r in conn.execute("SELECT symbol FROM sector_mapping")}
        to_insert = [
            (symbol, SECTOR_LOOKUP[symbol])
            for symbol in symbols
            if symbol not in existing and symbol in SECTOR_LOOKUP
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO sector_mapping (symbol, sector) VALUES (?, ?)", to_insert
        )
        conn.commit()
        return len(to_insert)
    finally:
        conn.close()


def get_sector(symbol: str) -> str | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT sector FROM sector_mapping WHERE symbol = ?", (symbol,)).fetchone()
        return row["sector"] if row else None
    finally:
        conn.close()


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
    holdings_symbols = {h["tradingsymbol"] for h in kite.holdings()}
    positions_symbols = {p["tradingsymbol"] for p in kite.positions().get("net", [])}
    candidates = holdings_symbols | positions_symbols

    conn = get_connection()
    try:
        mapped = {r["symbol"] for r in conn.execute("SELECT symbol FROM sector_mapping")}
    finally:
        conn.close()
    return sorted(candidates - mapped)
