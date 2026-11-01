"""PK-12 (P1-6): account-level safety controls.

These sit ABOVE the per-ticket risk rules. A NEW entry that passed its
strategy rules, its risk checks and confirmation-time revalidation must still
pass this gate before anything is sent to the broker. Exits, Emergency Exit,
protective GTT upkeep, reconciliation, order monitoring and UNKNOWN recovery
never come here — the gate may only ever refuse adding risk, never reducing
it.

Controls (one small function each):

  12A  trading lock            check_trading_lock        both modes
  12B  absolute max notional   check_notional            live
  12C  new entries per day     check_daily_entry_limit   live (binding copy: the claim)
  12D  price deviation / stop  check_price_sanity        live
  12E  stale strategy signal   check_stale_signal        both modes
  12F  daily realized loss     check_daily_loss          live
  12G  broker circuit breaker  check_breaker             live
  12H  market hours / AMO      decide_variety            live
  L0   live prerequisites      check_live_prerequisites  live (LIVE-0: notional + ₹ loss set,
                                                          static IP confirmed, no UNKNOWN order)
  S0   live armed for session  check_live_armed          live (SEC-1: login/logout disarms)
  S1   live critical state     check_live_critical_state live (SEC-1: reconciliation fresh and
                                                          complete, no critical finding, critical
                                                          workers healthy, paper/live isolated,
                                                          calendar covered; both orchestrators)

Two orchestrators run them, in a fixed order:

  precheck()   before the atomic claim — DB and request only, never a broker
               call; a refusal writes nothing to the ticket (except a stale
               signal, which is rejected for good).
  final_gate() after the claim and risk revalidation, immediately before
               the broker call — re-checks what can change meanwhile (lock,
               breaker, loss) and makes the one broker call (LTP) needed for
               the price checks, only if nothing failed before it.

The daily entry limit is enforced atomically by the claim itself
(daily_entry_claim_condition): a live claim is the reservation of a slot.

No FastAPI dependency here (same rule as risk_engine): routes/tickets.py
turns a failed decision into an HTTP error.

Single-account assumption: everything is keyed by account_id and belongs to
the execution-enabled trading account (accounts.get_trading_account); the
global execution mode (execution_settings) is the only unscoped input.
"""

import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from kiteconnect import KiteConnect

from backend import config, execution, market, obs, risk_engine, system_events, trading_calendar
from backend.db import get_connection
from backend.ticker import ticker_manager

logger = logging.getLogger(__name__)

TRADING_LOCKED = "TRADING_LOCKED"
DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
DAILY_ENTRY_LIMIT = "DAILY_ENTRY_LIMIT"
MAX_NOTIONAL_EXCEEDED = "MAX_NOTIONAL_EXCEEDED"
STALE_SIGNAL = "STALE_SIGNAL"
PRICE_DEVIATION_EXCEEDED = "PRICE_DEVIATION_EXCEEDED"
STOP_ALREADY_INVALID = "STOP_ALREADY_INVALID"
BROKER_CIRCUIT_OPEN = "BROKER_CIRCUIT_OPEN"
AMO_CONFIRMATION_REQUIRED = "AMO_CONFIRMATION_REQUIRED"
AMO_DISABLED = "AMO_DISABLED"
ACCOUNT_SAFETY_UNAVAILABLE = "ACCOUNT_SAFETY_UNAVAILABLE"
LIVE_PREREQUISITE_MISSING = "LIVE_PREREQUISITE_MISSING"
LIVE_STATE_UNVERIFIED = "LIVE_STATE_UNVERIFIED"
LIVE_NOT_ARMED_FOR_SESSION = "LIVE_NOT_ARMED_FOR_SESSION"
RECONCILIATION_CRITICAL = "RECONCILIATION_CRITICAL"

VARIETY_REGULAR = "regular"
VARIETY_AMO = "amo"

# Comparisons at a configured boundary: exactly AT a limit is allowed.
_EPSILON = 1e-9


@dataclass
class SafetyFailure:
    code: str
    message: str
    # True only when the ticket itself can never pass (a stale signal, a stop
    # the market has already crossed): it is rejected instead of being left
    # approvable.
    permanent: bool = False
    # 503 when the gate could not be evaluated at all (fail closed), else 409.
    http_status: int = 409


@dataclass
class SafetyDecision:
    failures: list = field(default_factory=list)
    # 'regular' | 'amo' — decided once here for a live new entry and bound
    # to its execution (never re-derived from the wall clock at send time).
    variety: str | None = None

    @property
    def passed(self) -> bool:
        return not self.failures

    def add(self, failure: SafetyFailure | None) -> None:
        if failure is not None:
            self.failures.append(failure)


def now_ist() -> datetime:
    """The gate's single clock (tests pin it)."""
    return market.now_ist()


def _utc_now_sql() -> str:
    return execution.utc_now_sql()


def _to_utc_sql(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ist_day_start_utc(now: datetime) -> str:
    """Start of `now`'s IST date, in the UTC text format the ticket
    timestamps use. The daily counts reset at IST midnight — a calendar
    day, deliberately not a trading session (unchanged by PK-22)."""
    return trading_calendar.ist_day_start_utc_sql(now)


def _notify() -> None:
    ticker_manager.broadcast({"type": "account_safety_changed"})


# --------------------------------------------------------------------------
# Persistent state (lock, breaker)
# --------------------------------------------------------------------------

def get_state(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM account_safety_state WHERE account_id = ?", (account_id,)).fetchone()
    if row is None:
        # Seeded at startup for every trading account; created on demand for
        # any other, unlocked and closed.
        conn.execute("INSERT OR IGNORE INTO account_safety_state (account_id) VALUES (?)", (account_id,))
        conn.commit()
        row = conn.execute("SELECT * FROM account_safety_state WHERE account_id = ?", (account_id,)).fetchone()
    return row


def set_trading_lock(conn: sqlite3.Connection, account_id: int, locked: bool, reason: str | None) -> bool:
    """PK-12A: the explicit server-side 'no new trades' switch. Returns True
    if the lock state actually changed (then one system event is written and
    every tab is told); setting the state it is already in is a no-op."""
    get_state(conn, account_id)
    now = _utc_now_sql()
    if locked:
        changed = conn.execute(
            "UPDATE account_safety_state SET trading_locked = 1, trading_locked_at = ?, trading_lock_reason = ?, "
            "updated_at = ? WHERE account_id = ? AND trading_locked = 0",
            (now, reason, now, account_id),
        ).rowcount == 1
    else:
        changed = conn.execute(
            "UPDATE account_safety_state SET trading_locked = 0, trading_locked_at = NULL, "
            "trading_lock_reason = NULL, updated_at = ? WHERE account_id = ? AND trading_locked = 1",
            (now, account_id),
        ).rowcount == 1
    if changed:
        detail = ("Trading LOCKED — no new entries (exits stay allowed)" if locked
                  else "Trading unlocked — new entries allowed again")
        system_events.log_event(conn, account_id, "trading_locked" if locked else "trading_unlocked",
                                f"{detail}. Reason: {reason or '(none given)'}")
    conn.commit()
    if changed:
        obs.log_event(logger, "safety.trading_locked" if locked else "safety.trading_unlocked", "warning",
                      account_id=account_id, reason=reason)
        _notify()
    return changed


def reset_breaker(conn: sqlite3.Connection, account_id: int, reason: str | None) -> bool:
    """PK-12G: the only way an open breaker closes — explicit and logged. The
    failure window restarts from now, so failures before the reset never
    count again. Returns True if the breaker was open."""
    get_state(conn, account_id)
    now = _utc_now_sql()
    was_open = conn.execute(
        "UPDATE account_safety_state SET breaker_open = 0, breaker_opened_at = NULL, breaker_reason = NULL, "
        f"last_reset_at = ?, failure_watermark_id = ({_LATEST_FAILURE_ID}), updated_at = ? "
        "WHERE account_id = ? AND breaker_open = 1",
        (now, account_id, now, account_id),
    ).rowcount == 1
    if was_open:
        system_events.log_event(conn, account_id, "broker_breaker_reset",
                                f"Broker circuit breaker reset manually. Reason: {reason or '(none given)'}")
    conn.commit()
    if was_open:
        obs.log_event(logger, "safety.breaker_reset", "warning", account_id=account_id, reason=reason)
        _notify()
    return was_open


# The newest failure id of an account — the watermark a broker success or a
# manual reset sets: only failures AFTER it count toward the breaker.
_LATEST_FAILURE_ID = "SELECT COALESCE(MAX(id), 0) FROM broker_failure_log WHERE account_id = ?"


def recent_failure_count(conn: sqlite3.Connection, account_id: int, settings: sqlite3.Row, state: sqlite3.Row,
                         now_utc: datetime | None = None) -> int:
    """Failures inside the breaker window: the last
    broker_breaker_window_minutes, and after the last broker success or
    manual reset."""
    now_utc = now_utc or now_ist().astimezone(timezone.utc)
    start = _to_utc_sql(now_utc - timedelta(minutes=settings["broker_breaker_window_minutes"]))
    return conn.execute(
        "SELECT COUNT(*) AS n FROM broker_failure_log WHERE account_id = ? AND occurred_at >= ? AND id > ?",
        (account_id, start, state["failure_watermark_id"]),
    ).fetchone()["n"]


def record_broker_failure(account_id: int, source: str, detail: str) -> None:
    """PK-12G: one broker/transport failure on the order path (a network or
    token error, a 5xx, an UNKNOWN submission outcome, an unreachable LTP) —
    never a validation error, a risk refusal or a lifecycle 409. Trips the
    breaker once `broker_breaker_threshold` failures fall inside the window
    (the last `broker_breaker_window_minutes`, restarted by a broker success
    or a manual reset). Own connection and never raises: bookkeeping must not
    change the outcome of the order call that reported the failure."""
    tripped = False
    conn = None
    try:
        conn = get_connection()
        now_utc = now_ist().astimezone(timezone.utc)
        conn.execute(
            "INSERT INTO broker_failure_log (account_id, occurred_at, source, detail) VALUES (?, ?, ?, ?)",
            (account_id, _to_utc_sql(now_utc), source, (detail or "")[:500]),
        )
        state = get_state(conn, account_id)
        settings = risk_engine.get_settings(conn, account_id)
        threshold = settings["broker_breaker_threshold"] if settings is not None else None
        if threshold is not None and not state["breaker_open"]:
            count = recent_failure_count(conn, account_id, settings, state, now_utc)
            if count >= threshold:
                reason = (f"{count} broker failure(s) within {settings['broker_breaker_window_minutes']} min "
                          f"(threshold {threshold}); latest: {source}: {detail}")[:500]
                tripped = conn.execute(
                    "UPDATE account_safety_state SET breaker_open = 1, breaker_opened_at = ?, breaker_reason = ?, "
                    "updated_at = ? WHERE account_id = ? AND breaker_open = 0",
                    (_to_utc_sql(now_utc), reason, _to_utc_sql(now_utc), account_id),
                ).rowcount == 1
                if tripped:
                    system_events.log_event(conn, account_id, "broker_breaker_tripped",
                                            f"Broker circuit breaker OPEN — new entries blocked until a manual "
                                            f"reset. {reason}")
        conn.commit()
    except Exception:
        logger.exception("Account safety: could not record a broker failure (%s)", source)
        if conn is not None:
            conn.rollback()
    finally:
        if conn is not None:
            conn.close()
    if tripped:
        obs.log_event(logger, "safety.breaker_tripped", "error", account_id=account_id, source=source)
        _notify()


def record_broker_success(account_id: int) -> None:
    """A live order the broker accepted: failures before it no longer count
    toward the breaker. It does NOT close an open breaker (manual reset only)."""
    conn = None
    try:
        conn = get_connection()
        get_state(conn, account_id)
        conn.execute(
            f"UPDATE account_safety_state SET last_success_at = ?, failure_watermark_id = ({_LATEST_FAILURE_ID}) "
            "WHERE account_id = ?",
            (_to_utc_sql(now_ist().astimezone(timezone.utc)), account_id, account_id),
        )
        conn.commit()
    except Exception:
        logger.exception("Account safety: could not record a broker success")
    finally:
        if conn is not None:
            conn.close()


# --------------------------------------------------------------------------
# The controls
# --------------------------------------------------------------------------

def check_trading_lock(state: sqlite3.Row) -> SafetyFailure | None:
    """PK-12A."""
    if not state["trading_locked"]:
        return None
    return SafetyFailure(TRADING_LOCKED,
                         f"Trading is LOCKED (since {state['trading_locked_at']} UTC; reason: "
                         f"{state['trading_lock_reason'] or 'none given'}) — no new entries until it is unlocked. "
                         f"Exits are still allowed.")


def check_breaker(state: sqlite3.Row) -> SafetyFailure | None:
    """PK-12G."""
    if not state["breaker_open"]:
        return None
    return SafetyFailure(BROKER_CIRCUIT_OPEN,
                         f"The broker circuit breaker is OPEN (since {state['breaker_opened_at']} UTC: "
                         f"{state['breaker_reason']}) — no new entries until it is reset manually. Exits and order "
                         f"monitoring continue.")


def realized_live_pnl_today(conn: sqlite3.Connection, account_id: int, today: date) -> float:
    """Net realized P&L of LIVE trades whose exit filled on `today` (IST).
    closed_trades.exit_date is the UTC date of the fill; every fill happens
    inside NSE hours (03:45-10:00 UTC), where the UTC and IST dates agree.
    Dry-run trades never count."""
    return conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM closed_trades "
        "WHERE account_id = ? AND mode = 'live' AND exit_date = ?",
        (account_id, today.isoformat()),
    ).fetchone()["pnl"]


def daily_loss_limit_amount(settings: sqlite3.Row, portfolio_value: float | None) -> float | None:
    """The effective daily loss limit in rupees: the tighter of the rupee
    limit and the % of portfolio value. None if neither is set. The % part
    needs portfolio_value; without it (pre-claim) only the rupee part is
    known. Raises ValueError when the % part is set but the portfolio value is
    not usable."""
    limits = []
    if settings["max_daily_loss_amount"] is not None:
        limits.append(settings["max_daily_loss_amount"])
    if settings["max_daily_loss_pct"] is not None and portfolio_value is not None:
        if not (isinstance(portfolio_value, (int, float)) and math.isfinite(portfolio_value) and portfolio_value > 0):
            raise ValueError(f"portfolio value {portfolio_value!r} is not usable")
        limits.append(settings["max_daily_loss_pct"] / 100 * portfolio_value)
    return min(limits) if limits else None


def check_daily_loss(conn: sqlite3.Connection, account_id: int, settings: sqlite3.Row, now: datetime,
                     portfolio_value: float | None = None) -> SafetyFailure | None:
    """PK-12F: blocks new entries once today's net realized LIVE loss has
    reached the limit (loss >= limit). Realized P&L only — open MTM is not
    counted (see the PK-12 report). Fails closed if it cannot be computed."""
    if settings["max_daily_loss_amount"] is None and settings["max_daily_loss_pct"] is None:
        return None
    try:
        limit = daily_loss_limit_amount(settings, portfolio_value)
        if limit is None:
            return None
        pnl = realized_live_pnl_today(conn, account_id, market.as_ist(now).date())
    except Exception as e:
        return SafetyFailure(ACCOUNT_SAFETY_UNAVAILABLE,
                             f"The daily loss limit is enabled but today's live P&L could not be evaluated "
                             f"({type(e).__name__}: {e}) — no new entry is sent.", http_status=503)
    loss = -pnl
    if loss < limit - _EPSILON:
        return None
    return SafetyFailure(DAILY_LOSS_LIMIT,
                         f"Daily loss limit reached: today's realized live P&L is ₹{pnl:,.2f} against a limit of "
                         f"−₹{limit:,.2f} — no new entries today. Exits are still allowed.")


# A live NEW entry that reached — or may have reached — the broker on the
# given IST day: claimed and in flight, accepted, UNKNOWN (a real order may
# exist), adopted by the order monitor, or needs_review. Not counted: dry
# runs, exits (closing sells), claims withdrawn before any broker call (their
# submission fields are cleared), definite broker rejections ('rejected') and
# submissions proven never to have reached the broker ('not_found').
COUNTED_SUBMISSION_STATES = (
    execution.SUBMITTING, execution.ACKNOWLEDGED, execution.UNKNOWN, execution.ADOPTED, execution.NEEDS_REVIEW,
)
_COUNTED_ENTRY_WHERE = f"""
    counted.account_id = ? AND counted.mode = 'live' AND counted.closes_ticket_id IS NULL
      AND NOT (counted.transaction_type = 'SELL' AND counted.source = 'alert_action')
      AND counted.submitted_at >= ?
      AND counted.submission_state IN ({', '.join(repr(s) for s in COUNTED_SUBMISSION_STATES)})
      AND counted.id != ?
"""


def entries_today(conn: sqlite3.Connection, account_id: int, now: datetime, exclude_ticket_id: int = 0) -> int:
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM trade_tickets AS counted WHERE {_COUNTED_ENTRY_WHERE}",
        (account_id, ist_day_start_utc(now), exclude_ticket_id),
    ).fetchone()["n"]


def daily_entry_claim_condition(settings: sqlite3.Row, account_id: int, ticket_id: int,
                                now: datetime) -> tuple[str, tuple] | None:
    """PK-12C, the binding copy: an extra WHERE predicate for the live-entry
    claim (routes/tickets._transition). The claim's single UPDATE both counts
    today's entries and takes the slot (writes 'submitting'), under SQLite's
    one-writer lock — two approvals racing for the last slot cannot both
    win. None when the limit is disabled."""
    limit = settings["max_new_entries_per_day"]
    if limit is None:
        return None
    sql = f"(SELECT COUNT(*) FROM trade_tickets AS counted WHERE {_COUNTED_ENTRY_WHERE}) < ?"
    return sql, (account_id, ist_day_start_utc(now), ticket_id, limit)


def daily_entry_limit_failure(settings: sqlite3.Row, used: int) -> SafetyFailure:
    return SafetyFailure(DAILY_ENTRY_LIMIT,
                         f"Daily new-entry limit reached: {used} live entr{'y' if used == 1 else 'ies'} already sent "
                         f"today (IST), limit {settings['max_new_entries_per_day']}. Exits are still allowed.")


def check_daily_entry_limit(conn: sqlite3.Connection, account_id: int, settings: sqlite3.Row, ticket_id: int,
                            now: datetime) -> SafetyFailure | None:
    """PK-12C, the informative pre-check (the claim is the binding one)."""
    limit = settings["max_new_entries_per_day"]
    if limit is None:
        return None
    used = entries_today(conn, account_id, now, ticket_id)
    return daily_entry_limit_failure(settings, used) if used >= limit else None


def check_notional(ticket: sqlite3.Row, settings: sqlite3.Row) -> SafetyFailure | None:
    """PK-12B: an absolute rupee cap per order, independent of the broker-
    derived portfolio value every % limit scales with."""
    cap = settings["max_order_notional"]
    if cap is None:
        return None
    notional = ticket["entry_price"] * ticket["quantity"]
    if notional <= cap + _EPSILON:
        return None
    return SafetyFailure(MAX_NOTIONAL_EXCEEDED,
                         f"Order value ₹{notional:,.2f} ({ticket['quantity']} × ₹{ticket['entry_price']:,.2f}) exceeds "
                         f"the absolute per-order cap of ₹{cap:,.2f}.")


def check_live_prerequisites(conn: sqlite3.Connection, account_id: int, settings: sqlite3.Row,
                             now: datetime) -> SafetyFailure | None:
    """LIVE-0: what must hold before ANY live new entry, checked locally (no
    broker call): an absolute per-order notional cap and a rupee daily loss
    limit are set (NULL is no longer 'disabled' for a live entry), the Kite
    static IP is confirmed and not contradicted, and no earlier live order
    has an unknown outcome that may be real, unaccounted exposure. The same
    helpers back the Live Readiness pre-flight (backend/live_preflight.py)."""
    from backend import live_preflight
    missing = []
    if settings["max_order_notional"] is None:
        missing.append("max order notional is not set")
    if settings["max_daily_loss_amount"] is None:
        missing.append("the rupee daily loss limit is not set")
    ip = live_preflight.static_ip_status(conn, account_id)
    if ip["status"] == live_preflight.STATIC_IP_FAILED:
        missing.append(f"static IP check FAILED ({ip['message']})")
    elif ip["status"] not in live_preflight.STATIC_IP_ACCEPTABLE:
        missing.append("the Kite static IP has not been confirmed on the Live Readiness page")
    unknown = live_preflight.unresolved_live_submissions(conn, account_id, now.astimezone(timezone.utc))
    if unknown:
        ids = ", ".join(f"#{u['id']}" for u in unknown[:5])
        missing.append(f"{len(unknown)} live order(s) have an unknown broker outcome ({ids}) — check Kite first")
    if not missing:
        return None
    return SafetyFailure(LIVE_PREREQUISITE_MISSING,
                         "Live entry refused — live-trading prerequisite(s) missing: " + "; ".join(missing)
                         + ". See Live Readiness. Nothing was executed; exits are still allowed.")


def check_live_armed(conn: sqlite3.Connection, account_id: int) -> SafetyFailure | None:
    """SEC-1 backstop: LIVE is armed for one broker session (live_arming.
    set_mode, after Live Readiness passed). A login or logout
    disarms it; should any path have skipped that, a live new entry is still
    refused while the armed session is not the active one."""
    try:
        from backend import live_arming
        if live_arming.live_armed_for_active_session(conn, account_id):
            return None
    except Exception as e:
        return SafetyFailure(LIVE_STATE_UNVERIFIED, f"Whether LIVE is armed for this session could not be read "
                                                    f"({type(e).__name__}: {e}) — no new live entry is sent.",
                             http_status=503)
    return SafetyFailure(LIVE_NOT_ARMED_FOR_SESSION,
                         "LIVE mode is not armed for the current Kite session (a new login resets it): switch to "
                         "LIVE again once Live Readiness passes with its broker check. Nothing was executed; exits "
                         "are still allowed.")


def check_live_critical_state(conn: sqlite3.Connection, account_id: int, now: datetime) -> list[SafetyFailure]:
    """SEC-1: the live state a NEW entry's safety rests on, re-evaluated at
    confirmation and not just when the Live Readiness page is displayed:
    - the last reconciliation compared the app with Kite completely and
      recently;
    - no critical reconciliation finding is unresolved (a broker position
      shortfall, an orphaned or duplicate GTT, a possible double sell);
    - the order monitor, trailing and reconciliation loops are healthy;
    - paper and live ids are isolated;
    - the NSE calendar covers today.

    Local only, no broker call. Anything stale, missing or inconsistent
    fails closed (503). A known-bad finding is 409 and never permanent: the
    ticket stays approvable once the finding is resolved and reviewed.
    Exits never come here."""
    from backend import live_preflight
    try:
        blockers = live_preflight.confirm_time_blockers(conn, account_id, trading_now=now)
    except Exception as e:
        return [SafetyFailure(LIVE_STATE_UNVERIFIED,
                              f"The live trading state could not be evaluated ({type(e).__name__}: {e}) — no new "
                              f"live entry is sent. Exits are still allowed.", http_status=503)]
    failures = []
    critical = [b for b in blockers if b["code"] in live_preflight.CONFIRM_TIME_CRITICAL_CODES]
    unverified = [b for b in blockers if b["code"] not in live_preflight.CONFIRM_TIME_CRITICAL_CODES]
    if critical:
        failures.append(SafetyFailure(RECONCILIATION_CRITICAL,
                                      "Live entry refused — " + " ".join(b["message"] for b in critical)
                                      + " Nothing was executed; exits are still allowed."))
    if unverified:
        failures.append(SafetyFailure(LIVE_STATE_UNVERIFIED,
                                      "Live entry refused — the terminal's view of the broker cannot be trusted "
                                      "right now: " + " ".join(b["message"] for b in unverified)
                                      + " Nothing was executed; exits are still allowed. See Live Readiness.",
                                      http_status=503))
    return failures


def check_stale_signal(ticket: sqlite3.Row, now: datetime) -> SafetyFailure | None:
    """PK-12E: a STRATEGY entry is valid only while its signal candle is the
    latest completed session (the rule expire_stale_signals applies at the
    next scan). Manual and alert-action entries keep their existing semantics.
    Weekends are handled; NSE holidays are not (PK-22)."""
    if ticket["source"] != "strategy":
        return None
    raw = ticket["signal_date"] or (ticket["created_at"] or "")[:10]
    session = market.last_complete_session_date(now)
    try:
        signal_day = date.fromisoformat(str(raw)[:10])
    except ValueError:
        return SafetyFailure(STALE_SIGNAL, f"Strategy signal date {raw!r} cannot be read — the signal cannot be "
                                           f"shown to be current. Nothing was executed.", permanent=True)
    if signal_day >= session:
        return None
    return SafetyFailure(STALE_SIGNAL,
                         f"Stale signal: computed from the {signal_day.isoformat()} candle, but the {session.isoformat()} "
                         f"session has completed since — its price levels are no longer current. Nothing was "
                         f"executed; the ticket is rejected (it is not refreshed automatically).", permanent=True)


def decide_variety(now: datetime, allow_amo: bool) -> tuple[str | None, SafetyFailure | None]:
    """PK-12H: market open → a regular order. Market closed → an AMO only
    with the approver's explicit consent (allow_amo), never by default."""
    if market.is_market_open(now):
        return VARIETY_REGULAR, None
    if not config.USE_AMO_WHEN_MARKET_CLOSED:
        return None, SafetyFailure(AMO_DISABLED, "The market is closed and After-Market Orders are disabled in "
                                                 "configuration — approve during market hours. Nothing was executed.")
    if allow_amo:
        return VARIETY_AMO, None
    return None, SafetyFailure(AMO_CONFIRMATION_REQUIRED,
                               "The market is closed. An entry now would be queued overnight as an After-Market "
                               "Order (a paper AMO in dry run) — approve again with 'Send as AMO' ticked to do that. "
                               "Nothing was executed.")


def fetch_ltp(kite: KiteConnect, ticket: sqlite3.Row) -> float:
    key = f"{ticket['exchange']}:{ticket['symbol']}"
    price = kite.ltp(key)[key]["last_price"]
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        raise ValueError(f"unusable last price {price!r} for {key}")
    return float(price)


def check_price_sanity(ticket: sqlite3.Row, ltp: float, settings: sqlite3.Row) -> list[SafetyFailure]:
    """PK-12D: refuses — never rewrites — an entry whose planned price is far
    from the market, or whose stop the market has already crossed."""
    failures = []
    stop = ticket["stop_loss"]
    if (ticket["transaction_type"] == "BUY" and ltp <= stop) or (ticket["transaction_type"] == "SELL" and ltp >= stop):
        side = "at or below" if ticket["transaction_type"] == "BUY" else "at or above"
        failures.append(SafetyFailure(STOP_ALREADY_INVALID,
                                      f"{ticket['symbol']} last traded at ₹{ltp:,.2f}, {side} the planned stop "
                                      f"₹{stop:,.2f} — the setup is already invalid. Nothing was executed; the ticket "
                                      f"is rejected.", permanent=True))
    limit = settings["max_entry_price_deviation_pct"]
    if limit is not None:
        reference = ticket["entry_price"]
        deviation = abs(ltp - reference) / reference * 100
        if deviation > limit + _EPSILON:
            kind = "limit price" if ticket["entry_order_type"] == "LIMIT" else "reference price"
            failures.append(SafetyFailure(PRICE_DEVIATION_EXCEEDED,
                                          f"{ticket['symbol']} last traded at ₹{ltp:,.2f}, {deviation:.2f}% from the "
                                          f"planned {kind} ₹{reference:,.2f} (max {limit}%). Nothing was executed — "
                                          f"review the ticket against the current market."))
    return failures


# --------------------------------------------------------------------------
# Orchestrators
# --------------------------------------------------------------------------

def precheck(conn: sqlite3.Connection, account_id: int, ticket: sqlite3.Row, mode: str, allow_amo: bool,
             now: datetime | None = None) -> SafetyDecision:
    """Before the atomic claim. DB and request only — never a broker call.
    Returns every failure it finds, in the documented order."""
    now = now or now_ist()
    state = get_state(conn, account_id)
    settings = risk_engine.get_settings(conn, account_id)
    decision = SafetyDecision()
    decision.add(check_trading_lock(state))
    live = mode == "live"
    if live:
        decision.add(check_live_armed(conn, account_id))
        decision.add(check_live_prerequisites(conn, account_id, settings, now))
        for failure in check_live_critical_state(conn, account_id, now):
            decision.add(failure)
        decision.add(check_daily_loss(conn, account_id, settings, now))
        decision.add(check_daily_entry_limit(conn, account_id, settings, ticket["id"], now))
        decision.add(check_notional(ticket, settings))
    decision.add(check_stale_signal(ticket, now))
    if live:
        decision.add(check_breaker(state))
    # PK-20: a dry-run entry is a paper ORDER, subject to the same session
    # rules (PK-12H): a regular order only while the market is open, an AMO
    # only with the approver's explicit consent. The breaker and the daily
    # loss / entry / notional limits above stay live-only — paper activity
    # never consumes or trips them.
    variety, failure = decide_variety(now, allow_amo)
    decision.add(failure)
    decision.variety = variety
    return decision


def final_gate(conn: sqlite3.Connection, account_id: int, ticket: sqlite3.Row, kite: KiteConnect,
               portfolio_value: float | None, now: datetime | None = None) -> SafetyDecision:
    """After the claim and risk revalidation, immediately before the broker
    call. Re-checks what may have changed meanwhile, then — only if nothing
    failed — makes the single LTP call for the price checks. An LTP that
    cannot be obtained fails closed."""
    now = now or now_ist()
    state = get_state(conn, account_id)
    decision = SafetyDecision()
    decision.add(check_trading_lock(state))
    if ticket["mode"] != "live":
        return decision
    settings = risk_engine.get_settings(conn, account_id)
    decision.add(check_breaker(state))
    decision.add(check_live_armed(conn, account_id))
    decision.add(check_daily_loss(conn, account_id, settings, now, portfolio_value))
    # SEC-1: re-evaluated here as well: reconciliation can raise a critical
    # finding, or a worker can stall, between the pre-check and this point.
    for failure in check_live_critical_state(conn, account_id, now):
        decision.add(failure)
    if not decision.passed:
        return decision  # no broker call after a hard failure
    try:
        ltp = fetch_ltp(kite, ticket)
    except Exception as e:
        if not isinstance(e, (ValueError, *execution.DEFINITE_REJECTIONS)):
            record_broker_failure(account_id, "ltp", f"{type(e).__name__}: {e}")
        decision.add(SafetyFailure(ACCOUNT_SAFETY_UNAVAILABLE,
                                   f"The current price of {ticket['symbol']} could not be fetched "
                                   f"({type(e).__name__}: {e}) — without it the entry cannot be checked against the "
                                   f"market, so nothing was executed.", http_status=503))
        return decision
    for failure in check_price_sanity(ticket, ltp, settings):
        decision.add(failure)
    return decision


def log_refusal(conn: sqlite3.Connection, account_id: int, ticket: sqlite3.Row, mode: str, phase: str,
                failures: list) -> None:
    """One system event per hard refusal (routine passes are not logged).
    Caller commits."""
    reasons = "; ".join(f"{f.code}: {f.message}" for f in failures)
    system_events.log_event(
        conn, account_id, "account_safety_refusal",
        f"Ticket {ticket['id']} {ticket['transaction_type']} {ticket['quantity']} {ticket['symbol']} ({mode}, "
        f"{phase}) refused by account safety — {reasons}",
    )


def status(conn: sqlite3.Connection, account_id: int) -> dict:
    """Everything the UI shows about the account-safety layer. Read-only,
    no broker call."""
    now = now_ist()
    state = get_state(conn, account_id)
    settings = risk_engine.get_settings(conn, account_id)
    pnl = realized_live_pnl_today(conn, account_id, now.date())
    try:
        amount_limit = daily_loss_limit_amount(settings, None)
    except ValueError:
        amount_limit = None
    loss_hit = amount_limit is not None and -pnl >= amount_limit - _EPSILON
    used = entries_today(conn, account_id, now)
    failures = recent_failure_count(conn, account_id, settings, state, now.astimezone(timezone.utc))
    market_open = market.is_market_open(now)
    return {
        "trading_locked": bool(state["trading_locked"]),
        "trading_locked_at": state["trading_locked_at"],
        "trading_lock_reason": state["trading_lock_reason"],
        "breaker_open": bool(state["breaker_open"]),
        "breaker_opened_at": state["breaker_opened_at"],
        "breaker_reason": state["breaker_reason"],
        "breaker_recent_failures": failures,
        "breaker_threshold": settings["broker_breaker_threshold"],
        "breaker_window_minutes": settings["broker_breaker_window_minutes"],
        "realized_live_pnl_today": pnl,
        "max_daily_loss_amount": settings["max_daily_loss_amount"],
        "max_daily_loss_pct": settings["max_daily_loss_pct"],
        # The % part needs the broker's portfolio value, so it is only
        # evaluated at confirmation; this flag reflects the rupee limit.
        "daily_loss_limit_hit": loss_hit,
        "entries_today": used,
        "max_new_entries_per_day": settings["max_new_entries_per_day"],
        "max_order_notional": settings["max_order_notional"],
        "max_entry_price_deviation_pct": settings["max_entry_price_deviation_pct"],
        "market_open": market_open,
        "amo_confirmation_required": not market_open,
        "trading_date_ist": now.date().isoformat(),
        # PK-22C: the browser only knows weekends and hours; the server knows
        # NSE holidays, so the header chip defers to these.
        "session_state": trading_calendar.session_state(now),
        "holiday_name": trading_calendar.holiday_name(now.date()),
    }
