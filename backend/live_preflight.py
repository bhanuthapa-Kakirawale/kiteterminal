"""LIVE-0: the live trading pre-flight.

One question, one authoritative answer: "is this terminal safe enough RIGHT
NOW to allow a live NEW-entry order?" -> READY or NOT_READY, with every
reason. It is operational readiness only; it never assesses a trade.

evaluate() is strictly read-only:

  * no database write (not even the on-demand rows other readers create),
  * no execution-mode switch and no trading unlock — ever,
  * broker access only when the caller asks for it (broker=True), and then
    only through ReadOnlyKite: one snapshot of profile, holdings, positions,
    margins, orders, GTTs and one LTP, reused for every check. Any other
    broker attribute (place_order, modify_gtt, ...) raises.

It builds on the PK-22 readiness pieces (schema status, integrity, worker
health, session state) and adds what a first live order needs on top:
worker staleness, the absolute notional and rupee daily-loss limits, the
static-IP prerequisite, unresolved UNKNOWN submissions, critical
reconciliation findings, strategy-history freshness and a real broker read.

NET-1: when KITE_REST_PROXY is configured, the same broker snapshot proves
the Windows -> proxy -> Kite HTTPS route (KITE_REST_ROUTE). A configured
proxy that does not reach Kite blocks readiness, whatever the static-IP
attestation says. The deliberate outbound-IP check goes through that proxy
too, so it observes the IP Kite sees for REST calls.

Research analytics (Volume Research / scores) are never a prerequisite:
their history staleness is reported as a warning only.

The narrow confirm-time gate (account_safety.check_live_prerequisites)
shares static_ip_status() and unresolved_live_submissions() with this
module, so the page and the gate cannot disagree about them.

CLI (read-only, sanitized):
    python -m backend.live_preflight payload-shape [--out FILE]
"""

import argparse
import ipaddress
import json
import logging
import sqlite3
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

from backend import config, db, execution, kite_rest_proxy, obs, trading_calendar

logger = logging.getLogger(__name__)

READY = "READY"
NOT_READY = "NOT_READY"

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
USER_ACTION = "USER_ACTION"

# Static-IP prerequisite states.
STATIC_IP_VERIFIED = "VERIFIED"                     # attested, and this machine's outbound IP matches it
STATIC_IP_USER_CONFIRMED = "USER_CONFIRMED"         # attested by the user; outbound IP not observed recently
STATIC_IP_USER_VERIFICATION_REQUIRED = "USER_VERIFICATION_REQUIRED"  # never attested
STATIC_IP_FAILED = "FAILED"                         # a recent outbound IP differs from the attested one
STATIC_IP_ACCEPTABLE = (STATIC_IP_VERIFIED, STATIC_IP_USER_CONFIRMED)

DISCLAIMER = "Operational readiness only. It does not assess investment merit."

CRITICAL_LIVE_WORKERS = ("order_monitor", "trailing", "reconciliation")

# Broker methods a pre-flight may call. Everything else is refused.
READ_ONLY_METHODS = frozenset({"profile", "holdings", "positions", "margins", "orders", "get_gtts", "ltp"})

# Broker order statuses that are still working at the exchange.
_WORKING_ORDER_STATUSES = frozenset({"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED", "OPEN PENDING",
                                     "VALIDATION PENDING", "PUT ORDER REQ RECEIVED", "MODIFY PENDING"})


class PreflightReadOnlyViolation(RuntimeError):
    """The pre-flight tried to use a broker method that is not read-only."""


class ReadOnlyKite:
    """Wraps a Kite client so only READ_ONLY_METHODS are reachable."""

    def __init__(self, kite):
        object.__setattr__(self, "_kite", kite)

    def __getattr__(self, name):
        if name not in READ_ONLY_METHODS:
            raise PreflightReadOnlyViolation(f"live pre-flight refused broker method {name!r}: read-only access only")
        return getattr(object.__getattribute__(self, "_kite"), name)

    def __setattr__(self, name, value):
        raise PreflightReadOnlyViolation("live pre-flight broker access is read-only")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sql_utc(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_utc(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _describe(exc: BaseException) -> str:
    from backend.redaction import redact_text
    return f"{type(exc).__name__}: {redact_text(str(exc))[:200]}"


def mask_identifier(value: str | None) -> str | None:
    """'AB1234' -> 'AB••34'. Enough to recognise one's own account, not to
    reuse it."""
    if not value:
        return None
    value = str(value)
    if len(value) <= 4:
        return value[0] + "•" * (len(value) - 1)
    return value[:2] + "•" * (len(value) - 4) + value[-2:]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _check(code: str, section: str, status: str, message: str, blocking: bool, action: str | None = None,
           detail: dict | None = None) -> dict:
    return {"code": code, "section": section, "status": status, "blocking": blocking, "message": message,
            "action_link": action, "detail": detail}


# --------------------------------------------------------------------------
# Static IP (shared with the confirm gate)
# --------------------------------------------------------------------------

def _readiness_row(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
    if not _has_table(conn, "live_readiness"):
        return None
    return conn.execute("SELECT * FROM live_readiness WHERE account_id = ?", (account_id,)).fetchone()


def static_ip_status(conn: sqlite3.Connection, account_id: int, now: datetime | None = None) -> dict:
    """The static-IP prerequisite. Kite's API does not expose the IP
    whitelisted on the developer console, so it rests on the user's
    attestation, strengthened (VERIFIED) or contradicted (FAILED) by a
    recent, deliberately requested observation of this machine's outbound IP."""
    now = now or _utc_now()
    row = _readiness_row(conn, account_id)
    static_ip = row["static_ip"] if row is not None else None
    base = {
        "static_ip": static_ip,
        "confirmed_at": row["static_ip_confirmed_at"] if row is not None else None,
        "egress_ip": row["egress_ip"] if row is not None else None,
        "egress_checked_at": row["egress_checked_at"] if row is not None else None,
        "egress_error": row["egress_error"] if row is not None else None,
        "attested": bool(static_ip and row["static_ip_confirmed_at"]),
    }
    if not base["attested"]:
        return {**base, "status": STATIC_IP_USER_VERIFICATION_REQUIRED,
                "message": "The static IP whitelisted in the Kite developer console has not been confirmed here."}
    checked = _parse_utc(base["egress_checked_at"])
    recent = checked is not None and now - checked <= timedelta(hours=config.EGRESS_OBSERVATION_MAX_AGE_HOURS)
    if recent and base["egress_ip"]:
        if _same_ip(base["egress_ip"], static_ip):
            return {**base, "status": STATIC_IP_VERIFIED,
                    "message": f"Confirmed static IP {static_ip} matches this machine's outbound IP "
                               f"(observed {base['egress_checked_at']} UTC)."}
        return {**base, "status": STATIC_IP_FAILED,
                "message": f"This machine's outbound IP {base['egress_ip']} (observed {base['egress_checked_at']} UTC) "
                           f"differs from the confirmed static IP {static_ip}: Kite would reject every order."}
    return {**base, "status": STATIC_IP_USER_CONFIRMED,
            "message": f"Static IP {static_ip} confirmed by the user at {base['confirmed_at']} UTC; this machine's "
                       f"outbound IP has not been observed in the last {config.EGRESS_OBSERVATION_MAX_AGE_HOURS} h."}


def _same_ip(a: str, b: str) -> bool:
    try:
        return ipaddress.ip_address(a.strip()) == ipaddress.ip_address(b.strip())
    except ValueError:
        return False


def validate_public_ip(value: str) -> str:
    """The canonical text of a public (globally routable) IP, or ValueError."""
    try:
        ip = ipaddress.ip_address((value or "").strip())
    except ValueError:
        raise ValueError(f"{value!r} is not an IP address")
    if not ip.is_global:
        raise ValueError(f"{ip} is not a public IP address: Kite whitelists the public (static) outbound IP")
    return str(ip)


def _ensure_row(conn: sqlite3.Connection, account_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO live_readiness (account_id) VALUES (?)", (account_id,))


def confirm_static_ip(conn: sqlite3.Connection, account_id: int, ip: str) -> dict:
    """The user's explicit statement that `ip` is whitelisted in the Kite
    developer console. Commits."""
    canonical = validate_public_ip(ip)
    now = _sql_utc(_utc_now())
    _ensure_row(conn, account_id)
    conn.execute("UPDATE live_readiness SET static_ip = ?, static_ip_confirmed_at = ?, updated_at = ? "
                 "WHERE account_id = ?", (canonical, now, now, account_id))
    conn.commit()
    obs.log_event(logger, "live_readiness.static_ip_confirmed", "warning", account_id=account_id)
    return static_ip_status(conn, account_id)


def clear_static_ip(conn: sqlite3.Connection, account_id: int) -> dict:
    now = _sql_utc(_utc_now())
    _ensure_row(conn, account_id)
    conn.execute("UPDATE live_readiness SET static_ip = NULL, static_ip_confirmed_at = NULL, updated_at = ? "
                 "WHERE account_id = ?", (now, account_id))
    conn.commit()
    obs.log_event(logger, "live_readiness.static_ip_cleared", "warning", account_id=account_id)
    return static_ip_status(conn, account_id)


def _fetch_egress_ip() -> str:
    """One HTTPS GET to the configured echo service. Only ever called from
    check_egress_ip(), which only a deliberate user action reaches.

    NET-1: with KITE_REST_PROXY set it goes through that proxy (an explicit
    ProxyHandler, which ignores environment proxies), so it observes the IP
    Kite sees for REST calls, never silently the direct route."""
    request = urllib.request.Request(config.EGRESS_IP_ECHO_URL, headers={"User-Agent": "kite-terminal-preflight"})
    proxy = kite_rest_proxy.from_env()
    if proxy is None:
        opener = urllib.request.urlopen
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxy.requests_proxies)).open
    with opener(request, timeout=config.EGRESS_IP_TIMEOUT_SECONDS) as response:  # noqa: S310
        return response.read(64).decode("ascii", "replace").strip()


def check_egress_ip(conn: sqlite3.Connection, account_id: int) -> dict:
    """Observe this machine's outbound public IP and store it (or the error).
    Commits. Never raises for a network failure."""
    now = _sql_utc(_utc_now())
    _ensure_row(conn, account_id)
    try:
        observed = str(ipaddress.ip_address(_fetch_egress_ip()))
        conn.execute("UPDATE live_readiness SET egress_ip = ?, egress_checked_at = ?, egress_error = NULL, "
                     "updated_at = ? WHERE account_id = ?", (observed, now, now, account_id))
    except Exception as e:
        conn.execute("UPDATE live_readiness SET egress_ip = NULL, egress_checked_at = ?, egress_error = ?, "
                     "updated_at = ? WHERE account_id = ?", (now, _describe(e), now, account_id))
    conn.commit()
    status = static_ip_status(conn, account_id)
    obs.log_event(logger, "live_readiness.egress_checked", "info", account_id=account_id, result=status["status"])
    return status


# --------------------------------------------------------------------------
# Unresolved live submissions (shared with the confirm gate)
# --------------------------------------------------------------------------

def unresolved_live_submissions(conn: sqlite3.Connection, account_id: int, now: datetime | None = None) -> list[dict]:
    """Live tickets whose broker outcome is not known: UNKNOWN, needs_review,
    a claim stuck in 'submitting' past SUBMISSION_STALE_SECONDS, or an entry
    fill that could not be confirmed. Any of these may be a real order the
    app is not accounting for, so no NEW live entry may be added on top."""
    now = now or _utc_now()
    stale_before = _sql_utc(now - timedelta(seconds=config.SUBMISSION_STALE_SECONDS))
    rows = conn.execute(
        "SELECT id, symbol, transaction_type, quantity, submission_state, entry_fill_status, submitted_at "
        "FROM trade_tickets WHERE account_id = ? AND mode = 'live' AND ("
        "  (status = 'confirmed' AND (submission_state IN (?, ?) OR (submission_state = ? AND submitted_at <= ?)))"
        "  OR (status = 'executed' AND entry_fill_status = 'unconfirmed')"
        ") ORDER BY id",
        (account_id, execution.UNKNOWN, execution.NEEDS_REVIEW, execution.SUBMITTING, stale_before),
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# The one broker snapshot
# --------------------------------------------------------------------------

@dataclass
class BrokerSnapshot:
    taken_at: str
    profile: dict | None = None
    holdings: list | None = None
    positions: dict | None = None
    margins: dict | None = None
    orders: list | None = None
    gtts: list | None = None
    ltp_key: str | None = None
    ltp: float | None = None
    errors: dict = field(default_factory=dict)
    # NET-1: reads that failed in transport (requests: proxy, connect,
    # timeout) rather than with a Kite API error.
    transport_errors: set = field(default_factory=set)
    session_error: str | None = None   # no usable session: nothing was read


def _reference_ltp_key(conn: sqlite3.Connection, account_id: int) -> str:
    row = conn.execute("SELECT symbol FROM watchlist WHERE account_id = ? ORDER BY symbol LIMIT 1",
                       (account_id,)).fetchone()
    return f"NSE:{row['symbol']}" if row is not None else "NSE:NIFTY 50"


def take_snapshot(kite, ltp_key: str) -> BrokerSnapshot:
    """Each read once; a failed read is None plus its error, never an empty
    list (an empty book and an unreadable one are different facts)."""
    ro = kite if isinstance(kite, ReadOnlyKite) else ReadOnlyKite(kite)
    snap = BrokerSnapshot(taken_at=_sql_utc(_utc_now()), ltp_key=ltp_key)
    for name in ("profile", "holdings", "positions", "margins", "orders", "gtts"):
        method = "get_gtts" if name == "gtts" else name
        try:
            setattr(snap, name, getattr(ro, method)())
        except PreflightReadOnlyViolation:
            raise
        except Exception as e:
            snap.errors[name] = _describe(e)
            if isinstance(e, requests.RequestException):
                snap.transport_errors.add(name)
    try:
        price = ro.ltp(ltp_key)[ltp_key]["last_price"]
        if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
            raise ValueError(f"unusable last price {price!r}")
        snap.ltp = float(price)
    except PreflightReadOnlyViolation:
        raise
    except Exception as e:
        snap.errors["ltp"] = _describe(e)
        if isinstance(e, requests.RequestException):
            snap.transport_errors.add("ltp")
    return snap


_cache_lock = threading.Lock()
_cache: dict = {}


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()


def broker_snapshot(conn: sqlite3.Connection, account_id: int, refresh: bool = False) -> BrokerSnapshot:
    """The cached snapshot (PREFLIGHT_BROKER_SNAPSHOT_CACHE_SECONDS), or a new
    one. A missing session is reported, not raised."""
    from backend import kite_client
    with _cache_lock:
        cached = _cache.get(account_id)
        if (cached is not None and not refresh
                and time.monotonic() - cached[0] < config.PREFLIGHT_BROKER_SNAPSHOT_CACHE_SECONDS):
            return cached[1]
    ltp_key = _reference_ltp_key(conn, account_id)
    try:
        kite = kite_client.get_kite(account_id)
    except kite_client.NoActiveSessionError:
        return BrokerSnapshot(taken_at=_sql_utc(_utc_now()), ltp_key=ltp_key,
                              session_error="No usable Kite session for today: log in first.")
    except kite_rest_proxy.KiteRestProxyError as e:
        return BrokerSnapshot(taken_at=_sql_utc(_utc_now()), ltp_key=ltp_key,
                              session_error=f"Kite REST proxy configuration invalid ({e}): no broker call was made.")
    snap = take_snapshot(kite, ltp_key)
    with _cache_lock:
        _cache[account_id] = (time.monotonic(), snap)
    return snap


# --------------------------------------------------------------------------
# Real-payload semantics (PK-14/15 assumption check)
# --------------------------------------------------------------------------

# What risk_engine.broker_quantities / build_portfolio_context read.
HOLDING_FIELDS_USED = ("tradingsymbol", "quantity", "t1_quantity", "average_price", "last_price")
HOLDING_FIELDS_OBSERVED = ("collateral_quantity", "collateral_type", "used_quantity", "realised_quantity",
                           "opening_quantity", "close_price", "exchange", "product")
POSITION_FIELDS_USED = ("tradingsymbol", "product", "exchange", "quantity", "overnight_quantity", "average_price",
                        "last_price")
POSITION_FIELDS_OBSERVED = ("day_buy_quantity", "day_sell_quantity", "close_price", "multiplier")


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _presence(rows: list, fields) -> dict:
    """For each field: in how many of the rows it is present / numeric."""
    out = {}
    for f in fields:
        present = sum(1 for r in rows if isinstance(r, dict) and f in r)
        numeric = sum(1 for r in rows if isinstance(r, dict) and _is_number(r.get(f)))
        out[f] = {"present": present, "numeric": numeric}
    return out


def payload_semantics(snap: BrokerSnapshot) -> dict:
    """Field presence only — no symbol, identifier or amount leaves here.
    `problems` lists what would make the frozen PK-14/15 exposure maths
    silently wrong (e.g. a missing numeric field it reads as 0)."""
    problems: list[str] = []
    notes: list[str] = []
    review: list[str] = []   # holding shapes PK-14/15 does not model: shown as a warning, never a block
    holdings = snap.holdings if isinstance(snap.holdings, list) else []
    net = (snap.positions or {}).get("net") if isinstance(snap.positions, dict) else None
    net = net if isinstance(net, list) else []
    equity = (snap.margins or {}).get("equity") if isinstance(snap.margins, dict) else None

    h = _presence(holdings, HOLDING_FIELDS_USED + HOLDING_FIELDS_OBSERVED)
    for f in ("quantity", "t1_quantity"):
        if holdings and h[f]["numeric"] < len(holdings):
            problems.append(f"holdings.{f} missing or non-numeric in {len(holdings) - h[f]['numeric']} row(s): "
                            f"the exposure maths would count it as 0")
    priced = sum(1 for r in holdings if _is_number(r.get("last_price")) or _is_number(r.get("close_price")))
    if holdings and priced < len(holdings):
        problems.append(f"{len(holdings) - priced} holding(s) carry no last/close price")
    pledged = sum(1 for r in holdings if _is_number(r.get("collateral_quantity")) and r["collateral_quantity"] > 0)
    if pledged:
        review.append(f"{pledged} holding(s) report collateral_quantity > 0 (pledged). PK-14/15 assumes pledged "
                      f"shares are already inside `quantity`")
    mtf = sum(1 for r in holdings if isinstance(r.get("mtf"), dict) and _is_number(r["mtf"].get("quantity"))
              and r["mtf"]["quantity"] > 0)
    if mtf:
        review.append(f"{mtf} holding(s) carry Margin Trading Facility (MTF) quantity, which PK-14/15 does not model")
    short = sum(1 for r in holdings if _is_number(r.get("short_quantity")) and r["short_quantity"] > 0)
    if short:
        review.append(f"{short} holding(s) report short_quantity > 0, which PK-14/15 does not model")
    discrepant = sum(1 for r in holdings if r.get("discrepancy") is True)
    if discrepant:
        review.append(f"{discrepant} holding(s) are flagged with a discrepancy by Kite")
    t1 = sum(1 for r in holdings if _is_number(r.get("t1_quantity")) and r["t1_quantity"] > 0)
    if t1:
        notes.append(f"{t1} holding(s) have T1 quantity (counted as held, as PK-14/15 assumes).")

    p = _presence(net, POSITION_FIELDS_USED + POSITION_FIELDS_OBSERVED)
    cnc = [r for r in net if isinstance(r, dict) and r.get("product") == "CNC"]
    for f in ("quantity", "overnight_quantity"):
        missing = sum(1 for r in cnc if not _is_number(r.get(f)))
        if missing:
            problems.append(f"positions.net.{f} missing or non-numeric in {missing} CNC row(s)")

    margins = {"equity_present": isinstance(equity, dict)}
    if isinstance(equity, dict):
        available = equity.get("available") if isinstance(equity.get("available"), dict) else {}
        utilised = equity.get("utilised") if isinstance(equity.get("utilised"), dict) else {}
        margins.update({
            "net_numeric": _is_number(equity.get("net")),
            "available_cash_numeric": _is_number(available.get("cash")),
            "available_live_balance_numeric": _is_number(available.get("live_balance")),
            "available_collateral_numeric": _is_number(available.get("collateral")),
            "utilised_debits_numeric": _is_number(utilised.get("debits")),
            "available_keys": sorted(available.keys()),
            "utilised_keys": sorted(utilised.keys()),
        })
        if not margins["net_numeric"]:
            problems.append("margins.equity.net missing or non-numeric: the portfolio value would count cash as 0")
    elif snap.margins is not None:
        problems.append("margins has no 'equity' segment: the portfolio value would count cash as 0")

    return {
        "holdings_rows": len(holdings), "holdings_fields": h,
        "net_position_rows": len(net), "cnc_position_rows": len(cnc), "position_fields": p,
        "margins": margins, "problems": problems, "review": review, "notes": notes,
    }


def _shape(value, depth: int = 0):
    """Keys and type names only (sanitized structure)."""
    if isinstance(value, dict):
        if depth >= 3:
            return "dict"
        return {k: _shape(v, depth + 1) for k, v in sorted(value.items())}
    if isinstance(value, list):
        keys: dict = {}
        for item in value:
            if isinstance(item, dict):
                for k, v in item.items():
                    keys.setdefault(k, set()).add(type(v).__name__)
        return {"_rows": len(value), "_row_keys": {k: sorted(t) for k, t in sorted(keys.items())}}
    return type(value).__name__


def payload_shape(snap: BrokerSnapshot) -> dict:
    """Sanitized structural report of the real payloads: keys, types, row
    counts and the semantics check. No values, symbols or identifiers."""
    return {
        "taken_at": snap.taken_at,
        "errors": {k: v.split(":", 1)[0] for k, v in snap.errors.items()},
        "profile_keys": sorted(snap.profile.keys()) if isinstance(snap.profile, dict) else None,
        "holdings": _shape(snap.holdings) if snap.holdings is not None else None,
        "positions": ({k: _shape(v) for k, v in snap.positions.items()} if isinstance(snap.positions, dict) else None),
        "margins": _shape(snap.margins) if snap.margins is not None else None,
        "orders": _shape(snap.orders) if snap.orders is not None else None,
        "gtts": _shape(snap.gtts) if snap.gtts is not None else None,
        "ltp_ok": snap.ltp is not None,
        "semantics": payload_semantics(snap),
    }


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------

def _database_checks(conn: sqlite3.Connection) -> list[dict]:
    from backend import diagnostics
    out = []
    schema = db.schema_status(conn)
    if not schema["supported"]:
        out.append(_check("DB_SCHEMA_CURRENT", "database", FAIL, schema["error"] or "Unsupported schema.", True))
    elif not schema["up_to_date"]:
        pending = ", ".join(f"v{p['version']}" for p in schema["pending"])
        out.append(_check("DB_SCHEMA_CURRENT", "database", FAIL,
                          f"Schema v{schema['current_version']} of v{schema['latest_version']}: migration(s) {pending} "
                          f"pending. Restart the application to apply them.", True))
    else:
        out.append(_check("DB_SCHEMA_CURRENT", "database", PASS,
                          f"Schema v{schema['current_version']} is current; no migration pending.", True))
    integrity = diagnostics.integrity_status()
    if integrity["ok"]:
        out.append(_check("DB_INTEGRITY", "database", PASS,
                          f"SQLite integrity check ok (checked {integrity['checked_at']} UTC).", True))
    else:
        out.append(_check("DB_INTEGRITY", "database", FAIL,
                          "SQLite integrity check failed: " + "; ".join(integrity["result"]), True, "#diagnostics"))
    return out


def _security_checks() -> list[dict]:
    import os
    from backend import server
    from backend.security import allowed_hostnames
    out = []
    try:
        runtime = server.validate(server.detect_runtime(os.environ, sys.argv))
    except Exception as e:
        return [_check("RUNTIME_POLICY", "security", FAIL, f"Unsafe runtime configuration: {e}", True, "#diagnostics")]
    summary = (f"bind {runtime.host}:{runtime.port}, workers={runtime.workers}, reload={'on' if runtime.reload else 'off'}, "
               f"{'remote' if runtime.remote_bind else 'loopback'} bind")
    if runtime.reload:
        out.append(_check("RUNTIME_POLICY", "security", FAIL,
                          f"Auto-reload is ON ({summary}): a file save restarts the server, possibly between an order "
                          f"and its protective GTT. Restart with `python -m backend.server`.", True, "#diagnostics"))
    elif runtime.source != "launcher":
        out.append(_check("RUNTIME_POLICY", "security", WARN,
                          f"Started outside `python -m backend.server`: {summary} was read best-effort from the command "
                          f"line. Use the launcher for enforced single-worker, reload-off defaults.", False, "#diagnostics"))
    else:
        out.append(_check("RUNTIME_POLICY", "security", PASS, f"Started by the launcher: {summary}.", True))
    hosts = sorted(allowed_hostnames())
    extra = [h for h in hosts if h not in ("127.0.0.1", "localhost", "::1")]
    if runtime.remote_bind and not extra:
        out.append(_check("ALLOWED_HOSTS", "security", FAIL,
                          "Remote bind with only loopback allowed hosts: remote browsers would be refused. Set "
                          "KITE_TERMINAL_ALLOWED_HOSTS (see docs/DEPLOYMENT.md).", True))
    else:
        out.append(_check("ALLOWED_HOSTS", "security", PASS, f"Allowed hosts: {', '.join(hosts)}.", True))
    return out


def _session_row(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sessions WHERE is_active = 1 AND account_id = ? ORDER BY id DESC LIMIT 1",
                        (account_id,)).fetchone()


def _broker_checks(conn: sqlite3.Connection, account: sqlite3.Row, snap: BrokerSnapshot | None) -> list[dict]:
    from backend import kite_client
    out = []
    row = _session_row(conn, account["id"])
    stored = row is not None and bool(account["access_token_enc"]) and kite_client.is_session_fresh(row)
    if account["account_type"] != "trading":
        out.append(_check("ACCOUNT_IDENTITY", "broker", FAIL,
                          "The default account is not the execution-enabled trading account.", True))
    if not stored:
        out.append(_check("BROKER_SESSION", "broker", FAIL,
                          "No Kite session for today (Kite tokens expire daily): log in before trading.", True))
        return out
    if snap is None:
        out.append(_check("BROKER_SESSION", "broker", USER_ACTION,
                          "A stored session exists for today. Run the broker check to prove it with a read-only "
                          "call.", True))
        return out
    if snap.session_error or snap.profile is None:
        reason = snap.session_error or snap.errors.get("profile", "profile read failed")
        out.append(_check("BROKER_SESSION", "broker", FAIL,
                          f"Kite did not accept the session ({reason}): log in again.", True))
        return out
    out.append(_check("BROKER_SESSION", "broker", PASS,
                      f"Kite accepted the session (read-only profile call at {snap.taken_at} UTC).", True))
    live_user = snap.profile.get("user_id")
    stored_user = row["user_id"]
    if account["account_type"] == "trading":
        if not live_user or (stored_user and live_user != stored_user):
            out.append(_check("ACCOUNT_IDENTITY", "broker", FAIL,
                              f"Kite reports account {mask_identifier(live_user)}, but this terminal's session belongs "
                              f"to {mask_identifier(stored_user)}. Log out and in with the intended trading account.",
                              True))
        else:
            out.append(_check("ACCOUNT_IDENTITY", "broker", PASS,
                              f"Trading account {mask_identifier(live_user)} is the account Kite reports.", True,
                              detail={"account": mask_identifier(live_user)}))
    missing = [k for k in ("holdings", "positions", "margins", "orders", "gtts") if k in snap.errors]
    if missing:
        out.append(_check("BROKER_READS", "broker", FAIL,
                          "Read-only broker calls failed: " + "; ".join(f"{k}: {snap.errors[k]}" for k in missing),
                          True))
        return out
    semantics = payload_semantics(snap)
    if semantics["problems"]:
        out.append(_check("BROKER_PAYLOAD_SHAPE", "broker", FAIL,
                          "The broker payload does not match what the exposure maths reads: "
                          + "; ".join(semantics["problems"]), True))
    else:
        out.append(_check("BROKER_PAYLOAD_SHAPE", "broker", PASS,
                          f"Holdings ({semantics['holdings_rows']}), positions ({semantics['net_position_rows']}) and "
                          f"margins carry every field the exposure maths reads.", True))
    if semantics["review"]:
        out.append(_check("HOLDINGS_NEED_REVIEW", "broker", WARN,
                          "; ".join(semantics["review"]) + ". Exposure limits may over- or under-count these symbols: "
                          "review them in Kite before the first live trade.", False))
    return out


_SNAPSHOT_READS = ("profile", "holdings", "positions", "margins", "orders", "gtts")


def _route_answered(snap: BrokerSnapshot) -> bool:
    """Kite answered through the route: some read returned data, or failed
    with a Kite API error (a reply from Kite itself), not in transport."""
    if snap.ltp is not None or any(getattr(snap, name) is not None for name in _SNAPSHOT_READS):
        return True
    return any(name not in snap.transport_errors for name in snap.errors)


def _rest_route_checks(snap: BrokerSnapshot | None, route: dict) -> list[dict]:
    """NET-1: is the Kite REST route this terminal is configured for usable?
    Proven by the one LIVE-0 broker snapshot (taken by the same
    proxy-configured client every order uses), never by a port probe or an
    external IP-echo service."""
    detail = {"mode": route["mode"], "endpoint": route["endpoint"]}
    if route["mode"] == kite_rest_proxy.INVALID:
        return [_check("KITE_REST_ROUTE", "network", FAIL,
                       f"{route['error']} Every Kite REST call is refused until it is fixed (never sent directly).",
                       True, detail=detail)]
    if route["mode"] == kite_rest_proxy.DIRECT:
        return [_check("KITE_REST_ROUTE", "network", WARN,
                       f"No REST proxy configured ({kite_rest_proxy.ENV_VAR} not set): Kite REST calls go directly "
                       f"from this machine. Live orders work only if this machine's own outbound IP is the "
                       f"whitelisted static IP.", False, detail=detail)]
    via = f"proxy {route['endpoint']}"
    if snap is None:
        return [_check("KITE_REST_ROUTE", "network", USER_ACTION,
                       f"Kite REST is configured through {via}. Run the broker check to prove the route reaches Kite "
                       f"with a read-only call.", True, detail=detail)]
    if snap.session_error:
        return [_check("KITE_REST_ROUTE", "network", USER_ACTION,
                       f"Kite REST is configured through {via}, but the route could not be proven: "
                       f"{snap.session_error} (Login also goes through the proxy.)", True, detail=detail)]
    if _route_answered(snap):
        return [_check("KITE_REST_ROUTE", "network", PASS,
                       f"Kite REST via {via}: read-only Kite calls answered at {snap.taken_at} UTC.", True,
                       detail=detail)]
    first = next(iter(sorted(snap.errors)), None)
    reason = snap.errors[first] if first else "no reply"
    return [_check("KITE_REST_ROUTE", "network", FAIL,
                   f"Kite REST is configured through {via}, but the proxy route is unreachable ({reason}). Calls are "
                   f"never retried directly: fix the tunnel/proxy, then re-run the broker check.", True,
                   detail=detail)]


def _static_ip_checks(conn: sqlite3.Connection, account_id: int, now: datetime) -> list[dict]:
    s = static_ip_status(conn, account_id, now)
    detail = {k: s[k] for k in ("static_ip", "confirmed_at", "egress_ip", "egress_checked_at", "egress_error")}
    detail["state"] = s["status"]
    route = kite_rest_proxy.route_status()
    detail["egress_route"] = route["mode"]
    detail["egress_proxy"] = route["endpoint"]
    if s["status"] == STATIC_IP_VERIFIED:
        return [_check("STATIC_IP", "static_ip", PASS, s["message"], True, detail=detail)]
    if s["status"] == STATIC_IP_USER_CONFIRMED:
        return [_check("STATIC_IP", "static_ip", PASS,
                       s["message"] + " Optional: check this machine's outbound IP.", True, detail=detail)]
    if s["status"] == STATIC_IP_FAILED:
        return [_check("STATIC_IP", "static_ip", FAIL, s["message"], True, detail=detail)]
    return [_check("STATIC_IP", "static_ip", USER_ACTION,
                   s["message"] + " Kite rejects API orders from any other IP (the cause of the earlier rejected live "
                                  "attempts). Enter the IP and confirm it.", True, detail=detail)]


def _mode_and_lock_checks(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    out = []
    from backend import live_arming
    mode = conn.execute("SELECT mode FROM execution_settings WHERE id = 1").fetchone()["mode"]
    if mode == "live" and live_arming.live_armed_for_active_session(conn, account_id):
        out.append(_check("EXECUTION_MODE", "trading_lock", PASS,
                          "Global execution mode is LIVE, armed for the current Kite session.", False,
                          detail={"mode": mode, "armed": True}))
    elif mode == "live":
        out.append(_check("EXECUTION_MODE", "trading_lock", USER_ACTION,
                          "Global execution mode is LIVE but not armed for the current Kite session: live new "
                          "entries are refused. Switch to DRY RUN and back to LIVE once this page passes.", False,
                          detail={"mode": mode, "armed": False}))
    else:
        out.append(_check("EXECUTION_MODE", "trading_lock", USER_ACTION,
                          "Global execution mode is DRY RUN. The pre-flight never switches it: switch to LIVE "
                          "deliberately, and only for the first controlled trade. Switching needs this page to "
                          "pass with its broker check (the trading lock may stay ON), and every new Kite login "
                          "switches back to DRY RUN.", False, detail={"mode": mode}))
    state = conn.execute("SELECT * FROM account_safety_state WHERE account_id = ?", (account_id,)).fetchone()
    if state is not None and state["trading_locked"]:
        out.append(_check("TRADING_LOCK", "trading_lock", USER_ACTION,
                          f"Trading lock is ON (since {state['trading_locked_at']} UTC"
                          f"{'; ' + state['trading_lock_reason'] if state['trading_lock_reason'] else ''}). Keep it on "
                          f"while reviewing; unlock deliberately only when every other check passes.", True,
                          "#settings", detail={"locked": True}))
    else:
        out.append(_check("TRADING_LOCK", "trading_lock", PASS, "Trading lock is OFF: new entries can be approved.",
                          True, "#settings", detail={"locked": False}))
    return out


def _account_safety_checks(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    out = []
    s = conn.execute("SELECT * FROM risk_settings WHERE account_id = ?", (account_id,)).fetchone()
    if s is None:
        return [_check("RISK_SETTINGS", "account_safety", FAIL, "No risk settings row for the trading account.", True,
                       "#settings")]

    def required(code: str, column: str, label: str, fmt) -> None:
        value = s[column]
        if value is None:
            out.append(_check(code, "account_safety", FAIL,
                              f"{label} is NOT SET. It must be set explicitly before the first live trade.", True,
                              "#settings"))
        else:
            out.append(_check(code, "account_safety", PASS, f"{label}: {fmt(value)}.", True, "#settings",
                              detail={column: value}))

    required("MAX_ORDER_NOTIONAL_SET", "max_order_notional", "Max order notional", lambda v: f"₹{v:,.2f}")
    required("DAILY_LOSS_AMOUNT_SET", "max_daily_loss_amount", "Daily loss limit (₹)", lambda v: f"₹{v:,.2f}")
    required("DAILY_ENTRY_LIMIT_SET", "max_new_entries_per_day", "Max new live entries per day", str)
    required("PRICE_DEVIATION_GUARD_SET", "max_entry_price_deviation_pct", "Entry price deviation guard",
             lambda v: f"{v}%")
    out.append(_check("STALE_SIGNAL_GUARD", "account_safety", PASS,
                      "Stale strategy signals are refused at approval (built in, holiday-aware).", False))
    out.append(_check("EXPOSURE_LIMITS", "account_safety", PASS,
                      f"Max open positions {s['max_open_positions']}, max stock exposure "
                      f"{s['max_single_stock_exposure_pct']}%, max sector exposure {s['max_sector_exposure_pct']}% — "
                      f"review before the first live trade.", False, "#settings",
                      detail={"max_open_positions": s["max_open_positions"],
                              "max_single_stock_exposure_pct": s["max_single_stock_exposure_pct"],
                              "max_sector_exposure_pct": s["max_sector_exposure_pct"]}))
    state = conn.execute("SELECT * FROM account_safety_state WHERE account_id = ?", (account_id,)).fetchone()
    if state is not None and state["breaker_open"]:
        out.append(_check("CIRCUIT_BREAKER", "account_safety", FAIL,
                          f"Broker circuit breaker is OPEN (since {state['breaker_opened_at']} UTC: "
                          f"{state['breaker_reason']}). Investigate, then reset it manually.", True, "#settings"))
    else:
        out.append(_check("CIRCUIT_BREAKER", "account_safety", PASS, "Broker circuit breaker is closed.", True))
    return out


# SEC-1: a timestamp this far ahead of the wall clock is not "fresh" — the
# clock or the recorded state is inconsistent, so it fails closed.
CLOCK_SKEW_TOLERANCE_SECONDS = 120

_WORKER_LABELS = {"order_monitor": "Order monitor", "trailing": "Trailing / GTT monitor",
                  "reconciliation": "Reconciliation"}


def _evaluate_worker(snapshot: dict, name: str, blocking: bool, code: str, label: str, now: datetime) -> dict:
    from backend import workers
    entry = snapshot.get(name) or {"task_state": "not_started", "last_ok_at": None, "failing": False,
                                   "consecutive_failures": 0, "last_error": None}
    loop_name = "order_monitor" if name == "order_monitor_paper" else name
    alive = workers.supervisor.is_running(loop_name)
    threshold = config.WORKER_STALE_AFTER_SECONDS[name]
    last_ok = _parse_utc(entry.get("last_ok_at"))
    bad = FAIL if blocking else WARN
    if not alive:
        return _check(code, "workers", bad, f"{label} is {entry['task_state'].replace('_', ' ')}: it is not "
                                            f"running in this process.", blocking, "#diagnostics")
    if last_ok is None:
        return _check(code, "workers", bad, f"{label} is running but has not completed a cycle yet.",
                      blocking, "#diagnostics")
    age = (now - last_ok).total_seconds()
    if age < -CLOCK_SKEW_TOLERANCE_SECONDS:
        return _check(code, "workers", bad, f"{label} reports a last successful cycle in the future "
                                            f"({entry.get('last_ok_at')} UTC): its health cannot be trusted.",
                      blocking, "#diagnostics")
    if age > threshold:
        return _check(code, "workers", bad, f"{label} is stalled: last successful cycle {int(age)} s ago "
                                            f"(limit {threshold} s).", blocking, "#diagnostics")
    if entry.get("failing"):
        return _check(code, "workers", bad, f"{label} is failing: {entry['consecutive_failures']} consecutive "
                                            f"failed cycles; last error: {entry.get('last_error')}.",
                      blocking, "#diagnostics")
    return _check(code, "workers", PASS, f"{label} healthy: last successful cycle {max(int(age), 0)} s ago.",
                  blocking)


def _critical_worker_checks(now: datetime) -> list[dict]:
    """The order monitor, trailing/GTT monitor and reconciliation loops:
    blocking for live, and shared with the confirm-time gate (SEC-1)."""
    from backend import worker_health
    snapshot = worker_health.snapshot()
    return [_evaluate_worker(snapshot, name, True, f"WORKER_{name.upper()}", _WORKER_LABELS[name], now)
            for name in CRITICAL_LIVE_WORKERS]


def _worker_checks(now: datetime) -> list[dict]:
    from backend import ticker, worker_health
    out = _critical_worker_checks(now)
    out.append(_evaluate_worker(worker_health.snapshot(), "order_monitor_paper", False, "WORKER_PAPER",
                                "Paper exchange (dry run)", now))
    if ticker.ticker_manager.is_connected():
        out.append(_check("TICKER_CONNECTED", "market_data", PASS, "Live tick stream connected.", False))
    else:
        out.append(_check("TICKER_CONNECTED", "market_data", WARN,
                          "Live tick stream not connected: on-screen prices may lag. Approval still fetches a fresh "
                          "LTP from Kite before any order.", False))
    return out


def _reconciliation_cycle_check(now: datetime) -> dict:
    """Did the last reconciliation cycle of THIS process compare the app with
    Kite completely, and recently? SEC-1: a cycle whose checks raised and were
    rolled back (failed_checks), or whose timestamp lies in the future, is not
    proof that nothing is wrong."""
    from backend import reconciliation
    cycle = reconciliation.cycle_status()
    checked = _parse_utc(cycle.get("checked_at"))
    threshold = config.WORKER_STALE_AFTER_SECONDS["reconciliation"]
    if checked is None:
        return _check("RECON_LAST_CYCLE", "reconciliation", FAIL,
                      "Reconciliation has not compared the app with Kite in this process yet.", True, "#diagnostics")
    age = (now - checked).total_seconds()
    if age < -CLOCK_SKEW_TOLERANCE_SECONDS:
        return _check("RECON_LAST_CYCLE", "reconciliation", FAIL,
                      f"The last reconciliation is stamped in the future ({cycle['checked_at']} UTC): the clock or "
                      f"the recorded state is inconsistent.", True, "#diagnostics")
    if cycle.get("gtt_book") != "ok" or cycle.get("holdings") == "unavailable":
        return _check("RECON_LAST_CYCLE", "reconciliation", FAIL,
                      f"The last reconciliation could not read Kite (GTT book: {cycle.get('gtt_book')}, holdings: "
                      f"{cycle.get('holdings')}).", True, "#diagnostics")
    if cycle.get("failed_checks"):
        return _check("RECON_LAST_CYCLE", "reconciliation", FAIL,
                      f"The last reconciliation did not complete: check(s) {', '.join(cycle['failed_checks'])} failed "
                      f"and were rolled back.", True, "#diagnostics")
    if age > threshold:
        return _check("RECON_LAST_CYCLE", "reconciliation", FAIL,
                      f"The last reconciliation with Kite is {int(age)} s old (limit {threshold} s).", True,
                      "#diagnostics")
    return _check("RECON_LAST_CYCLE", "reconciliation", PASS,
                  f"Reconciled with Kite at {cycle['checked_at']} UTC ({cycle.get('active_gtts') or 0} active "
                  f"GTT(s)).", True)


def _critical_findings_check(conn: sqlite3.Connection) -> dict:
    from backend import lifecycle
    critical = sorted(lifecycle.CRITICAL_ISSUES)
    marks = ",".join("?" * len(critical))
    rows = conn.execute(
        f"SELECT id, issue_code, symbol, ticket_id FROM reconciliation_log WHERE resolved = 0 "
        f"AND (issue_code IS NULL OR issue_code IN ({marks})) ORDER BY id", critical,
    ).fetchall()
    if rows:
        listed = ", ".join(f"#{r['id']} {r['issue_code'] or 'NEEDS_REVIEW'} {r['symbol'] or ''}".strip() for r in rows[:5])
        return _check("RECON_CRITICAL_UNRESOLVED", "reconciliation", FAIL,
                      f"{len(rows)} unresolved critical reconciliation finding(s): {listed}. Inspect Kite, resolve, "
                      f"then mark them reviewed.", True, "#positions")
    return _check("RECON_CRITICAL_UNRESOLVED", "reconciliation", PASS,
                  "No unresolved critical reconciliation finding.", True)


def _isolation_check(conn: sqlite3.Connection, account_id: int) -> dict:
    mixed = conn.execute(
        "SELECT COUNT(*) AS n FROM trade_tickets WHERE account_id = ? AND ("
        " (mode = 'live' AND (kite_order_id LIKE 'PAPER-%' OR kite_gtt_id LIKE 'PAPER-%'))"
        " OR (mode = 'dry_run' AND ((kite_order_id IS NOT NULL AND kite_order_id NOT LIKE 'PAPER-%')"
        "      OR (kite_gtt_id IS NOT NULL AND CAST(kite_gtt_id AS TEXT) NOT LIKE 'PAPER-%'))))",
        (account_id,),
    ).fetchone()["n"]
    if mixed:
        return _check("PAPER_LIVE_ISOLATION", "reconciliation", FAIL,
                      f"{mixed} ticket(s) mix paper and live broker ids: paper/live isolation is broken.", True,
                      "#diagnostics")
    return _check("PAPER_LIVE_ISOLATION", "reconciliation", PASS,
                  "Paper and live orders are isolated (no ticket carries the other mode's broker ids).", True)


def _reconciliation_checks(conn: sqlite3.Connection, account_id: int, now: datetime) -> list[dict]:
    from backend import lifecycle
    out = [_reconciliation_cycle_check(now), _critical_findings_check(conn)]
    critical = sorted(lifecycle.CRITICAL_ISSUES)
    marks = ",".join("?" * len(critical))
    other = conn.execute(
        f"SELECT COUNT(*) AS n FROM reconciliation_log WHERE resolved = 0 AND issue_code IS NOT NULL "
        f"AND issue_code NOT IN ({marks})", critical,
    ).fetchone()["n"]
    if other:
        out.append(_check("RECON_NONCRITICAL_UNRESOLVED", "reconciliation", WARN,
                          f"{other} unresolved non-critical reconciliation finding(s): review them on the Positions "
                          f"page.", False, "#positions"))

    unknown = unresolved_live_submissions(conn, account_id, now)
    if unknown:
        listed = ", ".join(f"#{u['id']} {u['symbol']} ({u['submission_state'] or u['entry_fill_status']})"
                           for u in unknown[:5])
        out.append(_check("UNKNOWN_LIVE_ORDERS", "reconciliation", FAIL,
                          f"{len(unknown)} live order(s) with an unknown broker outcome: {listed}. A real order may "
                          f"exist — check Kite's order book; never resubmit.", True, "#positions"))
    else:
        out.append(_check("UNKNOWN_LIVE_ORDERS", "reconciliation", PASS,
                          "No live order with an unknown or unreviewed broker outcome.", True))

    out.append(_isolation_check(conn, account_id))
    return out


def _broker_order_checks(conn: sqlite3.Connection, account_id: int, snap: BrokerSnapshot | None) -> list[dict]:
    if snap is None or snap.orders is None:
        return []
    known = {str(r["kite_order_id"]) for r in conn.execute(
        "SELECT kite_order_id FROM trade_tickets WHERE account_id = ? AND kite_order_id IS NOT NULL", (account_id,))}
    working = [o for o in snap.orders if isinstance(o, dict) and o.get("status") in _WORKING_ORDER_STATUSES]
    unlinked = [o for o in working if str(o.get("order_id")) not in known]
    if unlinked:
        return [_check("BROKER_OPEN_ORDERS_UNLINKED", "reconciliation", WARN,
                       f"{len(unlinked)} working order(s) at Kite were not placed by this terminal. They are not "
                       f"counted in its exposure; review them in Kite.", False)]
    return [_check("BROKER_OPEN_ORDERS_UNLINKED", "reconciliation", PASS,
                   f"{len(working)} working broker order(s), all placed by this terminal.", False)]


def _calendar_check(now: datetime) -> tuple[dict, dict]:
    calendar = trading_calendar.calendar_status(now)
    year = trading_calendar.as_ist(now).year
    if calendar["uncovered_years"] or year not in calendar["covered_years"]:
        return _check("CALENDAR_YEAR", "market_data", FAIL,
                      f"NSE holiday calendar does not cover {calendar['uncovered_years'] or [year]}: session "
                      f"dates (stale-signal rule, AMO timing) cannot be trusted. Update "
                      f"backend/calendar_data/nse_equity_holidays.json.", True, "#diagnostics"), calendar
    return _check("CALENDAR_YEAR", "market_data", PASS,
                  f"NSE holiday calendar covers {year}; latest completed session "
                  f"{calendar['latest_completed_session']}.", True), calendar


def _market_checks(conn: sqlite3.Connection, account_id: int, snap: BrokerSnapshot | None, now: datetime) -> list[dict]:
    from backend import automation, ohlc_integrity
    out = []
    if snap is None:
        out.append(_check("MARKET_DATA_LTP", "market_data", USER_ACTION,
                          "Run the broker check to prove a current price can be read from Kite.", True))
    elif snap.ltp is None:
        out.append(_check("MARKET_DATA_LTP", "market_data", FAIL,
                          f"Kite returned no usable price for {snap.ltp_key} "
                          f"({snap.session_error or snap.errors.get('ltp', 'no data')}).", True))
    else:
        out.append(_check("MARKET_DATA_LTP", "market_data", PASS,
                          f"Kite price read OK ({snap.ltp_key} at {snap.taken_at} UTC).", True))

    calendar_check, calendar = _calendar_check(now)
    out.append(calendar_check)

    session = calendar["latest_completed_session"]
    fresh = automation.data_freshness(account_id, session)
    if fresh["watchlist_size"] == 0:
        out.append(_check("STRATEGY_HISTORY_FRESH", "market_data", WARN,
                          "The watchlist is empty: no strategy can generate an entry.", False, "#watchlist"))
    elif fresh["stale"]:
        listed = ", ".join(fresh["stale"][:8]) + (" …" if len(fresh["stale"]) > 8 else "")
        out.append(_check("STRATEGY_HISTORY_FRESH", "market_data", FAIL,
                          f"{len(fresh['stale'])} of {fresh['watchlist_size']} watchlist symbol(s) lack the {session} "
                          f"candle ({listed}): strategy signals would be computed on stale history. Refresh "
                          f"history (log in, or Run now on the autopilot).", True, "#diagnostics"))
    else:
        out.append(_check("STRATEGY_HISTORY_FRESH", "market_data", PASS,
                          f"All {fresh['watchlist_size']} watchlist symbols have the {session} candle.", True))

    if _has_table(conn, "ohlc_history_status"):
        watch_tokens = {r["instrument_token"] for r in conn.execute(
            "SELECT instrument_token FROM watchlist WHERE account_id = ?", (account_id,))}
        flagged = ohlc_integrity.flagged(conn, watch_tokens, (ohlc_integrity.NEEDS_REFRESH,))
        if flagged:
            names = ", ".join(sorted(f["symbol"] for f in flagged.values())[:8])
            out.append(_check("HISTORY_NEEDS_REFRESH", "market_data", WARN,
                              f"{len(flagged)} watchlist symbol(s) need a history rebuild after a corporate action "
                              f"({names}). Strategy scans skip them until rebuilt; do not trade them manually on "
                              f"stored levels.", False, "#diagnostics"))
        other = conn.execute(
            "SELECT COUNT(*) AS n FROM ohlc_history_status WHERE last_date < ? AND instrument_token NOT IN "
            "(SELECT instrument_token FROM watchlist WHERE account_id = ?)", (session, account_id),
        ).fetchone()["n"]
        if other:
            out.append(_check("RESEARCH_HISTORY_STALE", "market_data", WARN,
                              f"{other} non-watchlist symbol(s) used only by research views lack the {session} candle. "
                              f"Research analytics are informational and never a live-trading prerequisite.", False))
    return out


SECTION_TITLES = {
    "database": "Database",
    "security": "Security",
    "broker": "Broker",
    "network": "Kite REST Route",
    "static_ip": "Static IP",
    "market_data": "Market Data",
    "workers": "Workers",
    "account_safety": "Account Safety",
    "reconciliation": "Reconciliation",
    "trading_lock": "Trading Lock",
}


def is_blocking_failure(check: dict) -> bool:
    return check["blocking"] and check["status"] in (FAIL, USER_ACTION)


# --------------------------------------------------------------------------
# SEC-1: the confirm-time subset (shared with account_safety)
# --------------------------------------------------------------------------

# The state the terminal's own view of the broker depends on. Stale, missing
# or inconsistent means it cannot be trusted right now (fail closed, 503).
CONFIRM_TIME_UNVERIFIED_CODES = frozenset({
    "RECON_LAST_CYCLE", "WORKER_ORDER_MONITOR", "WORKER_TRAILING", "WORKER_RECONCILIATION", "CALENDAR_YEAR",
})
# Known-bad state that a human must resolve first (409).
CONFIRM_TIME_CRITICAL_CODES = frozenset({"RECON_CRITICAL_UNRESOLVED", "PAPER_LIVE_ISOLATION"})


def confirm_time_blockers(conn: sqlite3.Connection, account_id: int, trading_now: datetime | None = None,
                          now: datetime | None = None) -> list[dict]:
    """The blocking failures among the checks a live NEW entry must pass at
    confirmation (account_safety.check_live_critical_state). They are built
    by the same functions the Live Readiness page uses, so the two cannot
    disagree.

    Strictly local: no broker call and no database write. Freshness is judged
    on the wall clock (`now`, UTC), because worker and reconciliation
    timestamps are wall-clock. The calendar is judged on the trading clock
    (`trading_now`). Unresolved UNKNOWN submissions are not repeated here:
    the LIVE-0 prerequisite check already refuses them."""
    now = now or _utc_now()
    checks = [_reconciliation_cycle_check(now), _critical_findings_check(conn), _isolation_check(conn, account_id)]
    checks += _critical_worker_checks(now)
    checks.append(_calendar_check(trading_now or now)[0])
    return [c for c in checks if is_blocking_failure(c)]


def evaluate(broker: bool = False, refresh: bool = False, now: datetime | None = None) -> dict:
    """The pre-flight. Read-only; broker reads only when broker=True."""
    from backend.accounts import get_default_account
    now = now or _utc_now()
    route = kite_rest_proxy.route_status()
    conn = db.get_connection()
    try:
        account = get_default_account(conn)
        account_id = account["id"]
        snap = broker_snapshot(conn, account_id, refresh=refresh) if broker else None
        checks: list[dict] = []
        checks += _database_checks(conn)
        checks += _security_checks()
        checks += _broker_checks(conn, account, snap)
        checks += _rest_route_checks(snap, route)
        checks += _static_ip_checks(conn, account_id, now)
        checks += _market_checks(conn, account_id, snap, now)
        checks += _worker_checks(now)
        checks += _account_safety_checks(conn, account_id)
        checks += _reconciliation_checks(conn, account_id, now)
        checks += _broker_order_checks(conn, account_id, snap)
        checks += _mode_and_lock_checks(conn, account_id)
        mode = conn.execute("SELECT mode FROM execution_settings WHERE id = 1").fetchone()["mode"]
    finally:
        conn.close()
    blocking = [c for c in checks if is_blocking_failure(c)]
    order = list(SECTION_TITLES)
    checks.sort(key=lambda c: order.index(c["section"]))
    return {
        "overall_status": READY if not blocking else NOT_READY,
        "ready_except_trading_lock": bool(blocking) and {c["code"] for c in blocking} == {"TRADING_LOCK"},
        "blocking_codes": [c["code"] for c in blocking],
        "warning_codes": [c["code"] for c in checks if c["status"] == WARN],
        "execution_mode": mode,
        "broker_checked": snap is not None,
        "broker_snapshot_at": snap.taken_at if snap is not None else None,
        "rest_route": {"mode": route["mode"], "endpoint": route["endpoint"]},
        "checked_at": _sql_utc(now),
        "disclaimer": DISCLAIMER,
        "sections": [{"key": k, "title": t} for k, t in SECTION_TITLES.items()],
        "checks": checks,
    }


# --------------------------------------------------------------------------
# CLI: sanitized real-payload structure (read-only)
# --------------------------------------------------------------------------

def _cli(argv: list[str] | None = None) -> int:
    import os
    parser = argparse.ArgumentParser(prog="python -m backend.live_preflight")
    parser.add_argument("command", choices=["payload-shape"])
    parser.add_argument("--out", help="write the JSON report here instead of stdout")
    args = parser.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv()
    if not os.environ.get("KITE_TERMINAL_ENCRYPTION_KEY"):
        print("KITE_TERMINAL_ENCRYPTION_KEY is not set (.env not found): refusing to run.", file=sys.stderr)
        return 2
    from backend import kite_client
    from backend.accounts import get_default_account
    conn = db.get_connection()
    try:
        account_id = get_default_account(conn)["id"]
        ltp_key = _reference_ltp_key(conn, account_id)
    finally:
        conn.close()
    try:
        kite = kite_client.get_kite(account_id)
    except kite_client.NoActiveSessionError:
        print("No Kite session for today: log in through the terminal first.", file=sys.stderr)
        return 1
    report = payload_shape(take_snapshot(kite, ltp_key))
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"sanitized payload structure written to {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
