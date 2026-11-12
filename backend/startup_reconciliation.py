"""Phase 2: startup reconciliation and the persisted entry restriction (docs/PHASE_2_DESIGN_REVIEW.md §8).

After the epoch takeover persisted RESTRICTED(STARTUP_RECONCILIATION_INCOMPLETE), the background
loops start (they do today's exposure-reducing upkeep and are never blocked by RESTRICTED) and this
module runs:

* Stage 1, read-only towards the broker: the local inventory, the session and REST-route checks,
  ONE broker read (orders, GTTs, holdings, positions, in that order, each with its timestamp, count
  and error, recorded as a broker_reads row), attribution evaluated and REPORTED only, today's
  reconciliation checks run once against that same read, cross-endpoint inconsistencies, and the
  persisted run (startup_reconciliation_runs). Retried with backoff until it passes, and re-run on
  every login and resume.
* Stage 2, recovery writes: deliberately EMPTY in Phase 2 (no protection journal is live yet). It
  exists as a named step that records that no recovery write is authorised.

The restriction only ever ADDS refusals of new live entries (live_preflight.confirm_time_blockers →
account_safety.check_live_critical_state). It never blocks an exit, a cancellation, Emergency Exit
or protection upkeep: none of those pass through the entry gates.

The state is derived from persisted facts (derive_restriction), re-derived at every confirmation and
persisted at the end of every Stage 1 and every reconciliation cycle; it is never held only in
memory, and a crash anywhere restarts RESTRICTED (the takeover writes it before anything else).
Every write here is a fenced transaction (instance_lock.fenced_transaction).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from datetime import date, datetime, timezone

from kiteconnect.exceptions import TokenException

from backend import config, instance_lock
from backend.instance_lock import (
    PREVIOUS_INSTANCE_UNCLEAN, RESUMED_AFTER_SUSPEND, STARTUP_RECONCILIATION_INCOMPLETE,
)

logger = logging.getLogger(__name__)

NORMAL, RESTRICTED = "NORMAL", "RESTRICTED"

# Restriction reasons (design §8.5). The INSTANCE_* / DATABASE_* codes come from instance_lock.
NO_SESSION = "NO_SESSION"
PROXY_UNAVAILABLE = "PROXY_UNAVAILABLE"
BROKER_READ_FAILED = "BROKER_READ_FAILED"
BROKER_DATA_INCONSISTENT = "BROKER_DATA_INCONSISTENT"
UNRESOLVED_OPERATION = "UNRESOLVED_OPERATION"
RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
CLEANUP_REQUIRED = "CLEANUP_REQUIRED"
# §8.5 NORMAL condition "the last complete broker read is no older than GTT_OBSERVATION_MAX_AGE_SECONDS"
# (not a persisted reason; derived at every evaluation).
BROKER_READ_STALE = "BROKER_READ_STALE"
# PROTECTION_GAP and LATE_GTT_APPEARED need the protection journal: Phase 3+. Never set in Phase 2.

# Persisted reasons that only a Stage 1 run can change (a later passing run clears them). Everything
# else is re-derived from local facts at every evaluation, so it clears the moment the cause does.
STICKY_REASONS = frozenset({
    STARTUP_RECONCILIATION_INCOMPLETE, PREVIOUS_INSTANCE_UNCLEAN, RESUMED_AFTER_SUSPEND, NO_SESSION,
    PROXY_UNAVAILABLE, BROKER_READ_FAILED, BROKER_DATA_INCONSISTENT,
})

PASSED, INCOMPLETE, FAILED, BLOCKED = "passed", "incomplete", "failed", "blocked"
STAGE2_NOTE = "Stage 2 (recovery writes) is empty in Phase 2: no recovery writes are authorised in this phase."

_CLEANUP_CODES = ("GTT_CANCEL_FAILED", "GTT_CANCEL_OUTCOME_UNKNOWN")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat()


def _parse(value, assume=timezone.utc) -> datetime | None:
    """ISO text (ours, login_time in IST) or 'YYYY-MM-DD HH:MM:SS' (UTC, reconciliation's)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=assume)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


# ---- the local inventory (no broker) ------------------------------------------------------------

def _unresolved(ticket: dict) -> bool:
    """unknown / needs_review: always. 'submitting': when a PREDECESSOR process claimed it (before this
    owner's takeover), always: that process can no longer resolve it and its request may have landed.
    A 'submitting' claim of THIS process is its own call in flight (the entry being confirmed right now is
    claimed just before the final gate): not counted here. Once stale, the existing LIVE-0 prerequisite
    (live_preflight.unresolved_live_submissions, before any claim) already refuses new entries for it, as
    confirm_time_blockers documents."""
    if ticket["submission_state"] != "submitting":
        return True
    submitted = _parse(ticket.get("submitted_at"))
    owner_since = _parse(instance_lock.status()["started_at"])
    if owner_since is not None:
        owner_since = owner_since.replace(microsecond=0)  # submitted_at has whole seconds
    return submitted is None or owner_since is None or submitted < owner_since


def local_inventory(conn: sqlite3.Connection, account_id: int) -> dict:
    """§8.3 step 1. Read-only."""
    from backend import execution, lifecycle, risk_engine
    rows = lambda sql, *args: [dict(r) for r in conn.execute(sql, args).fetchall()]  # noqa: E731
    inventory = {
        # an entry or exit whose broker outcome is not known yet
        "unresolved_submissions": [r for r in rows(
            "SELECT id, symbol, transaction_type, closes_ticket_id, status, submission_state, submitted_at "
            "FROM trade_tickets WHERE account_id = ? AND mode = 'live' AND status = 'confirmed' "
            "AND submission_state IN ('submitting', 'unknown', 'needs_review') ORDER BY id", account_id)
            if _unresolved(r)],
        "unconfirmed_fills": rows(
            "SELECT id, symbol, entry_fill_status FROM trade_tickets WHERE account_id = ? AND mode = 'live' "
            "AND status = 'executed' AND entry_fill_status = 'unconfirmed' ORDER BY id", account_id),
        "working_entries": rows(
            "SELECT id, symbol FROM trade_tickets WHERE account_id = ? AND mode = 'live' AND status = 'executed' "
            "AND entry_fill_status = 'pending' AND closes_ticket_id IS NULL ORDER BY id", account_id),
        "exits_in_flight": rows(
            f"SELECT id, symbol, closes_ticket_id FROM trade_tickets WHERE account_id = ? AND mode = 'live' "
            f"AND closes_ticket_id IS NOT NULL AND {execution.EXIT_IN_FLIGHT_SQL} ORDER BY id", account_id),
        "positions_quantity_unknown": [
            {"id": p["id"], "symbol": p["symbol"], "open_quantity": p["open_quantity"]}
            for p in risk_engine.list_open_positions(conn, account_id)
            if p["open_quantity"] is None or p["open_quantity"] < 0],
    }
    critical = sorted(lifecycle.CRITICAL_ISSUES)
    inventory["critical_findings"] = rows(
        f"SELECT id, issue_code, symbol FROM reconciliation_log WHERE resolved = 0 AND (issue_code IS NULL OR "
        f"issue_code IN ({','.join('?' * len(critical))})) ORDER BY id", *critical)
    inventory["cleanup_findings"] = [f for f in inventory["critical_findings"] if f["issue_code"] in _CLEANUP_CODES]
    # Phase 3 G3 (journal-aware Stage 1): the protection journal's open operations. The journal is dark (v12
    # unregistered): on a database without the table this adds nothing, so behaviour there is unchanged.
    inventory["open_protection_operations"] = rows(
        "SELECT id, position_id, kind, state FROM protection_operations WHERE account_id = ? AND state IN "
        "('intent', 'sending', 'acknowledged', 'unknown', 'needs_review') ORDER BY id", account_id) \
        if _table_exists(conn, "protection_operations") else []
    # instrument_attribution comes with Phase 7: no sticky attribution state exists yet.
    return inventory


def local_reasons(inventory: dict) -> set[str]:
    reasons = set()
    if inventory["unresolved_submissions"] or inventory.get("open_protection_operations"):
        reasons.add(UNRESOLVED_OPERATION)
    if inventory["unconfirmed_fills"] or inventory["positions_quantity_unknown"]:
        reasons.add(RECONCILIATION_REQUIRED)
    if inventory["cleanup_findings"]:
        reasons.add(CLEANUP_REQUIRED)
    return reasons


# ---- derivation ---------------------------------------------------------------------------------

def _latest_login(conn: sqlite3.Connection, account_id: int) -> datetime | None:
    from backend.trading_calendar import IST
    row = conn.execute("SELECT login_time FROM sessions WHERE is_active = 1 AND account_id = ? ORDER BY id DESC "
                       "LIMIT 1", (account_id,)).fetchone()
    return _parse(row["login_time"] if row is not None else None, assume=IST)


def _last_complete_read(conn: sqlite3.Connection, account_id: int, epoch: int | None) -> datetime | None:
    candidates = []
    if epoch is not None:
        row = conn.execute("SELECT MAX(finished_at) AS at FROM broker_reads WHERE account_id = ? AND complete = 1 "
                           "AND instance_epoch = ?", (account_id, epoch)).fetchone()
        candidates.append(_parse(row["at"]))
    from backend import reconciliation
    cycle = reconciliation.cycle_status()
    if (cycle.get("gtt_book") == "ok" and cycle.get("holdings") in ("ok", "not_needed")
            and not cycle.get("failed_checks")):
        candidates.append(_parse(cycle.get("checked_at")))
    candidates = [c for c in candidates if c is not None]
    return max(candidates) if candidates else None


def derive_restriction(conn: sqlite3.Connection, account_id: int, now: datetime | None = None,
                       sticky: set[str] | None = None) -> dict:
    """Read-only. The account's restriction state from persisted facts plus this process's authority:
    NORMAL iff this process owns the database with a current epoch; a Stage 1 run passed in this epoch
    after the last login and the last resume; the last complete broker read is fresh; and no reason
    (persisted sticky, process, or derived from local state) is set (§8.5)."""
    now = now or _utc_now()
    reasons: set[str] = set()
    detail: dict = {}
    code = instance_lock.refusal_code()
    if code is not None:
        reasons.add(code)
    reasons |= instance_lock.process_reasons()
    epoch = instance_lock.owner_epoch()
    if not _table_exists(conn, "account_restriction"):
        reasons.add(STARTUP_RECONCILIATION_INCOMPLETE)
        return {"state": RESTRICTED, "reasons": sorted(reasons), "detail": {"schema": "v11 objects missing"}}
    row = conn.execute("SELECT * FROM account_restriction WHERE account_id = ?", (account_id,)).fetchone()
    if sticky is None:
        if row is None:
            sticky = {STARTUP_RECONCILIATION_INCOMPLETE}
        else:
            try:
                sticky = set(json.loads(row["restriction_reasons"])) & STICKY_REASONS
            except (TypeError, ValueError):
                sticky = {STARTUP_RECONCILIATION_INCOMPLETE}
    reasons |= sticky

    passed = None
    if epoch is not None:
        passed = conn.execute(
            "SELECT id, started_at, finished_at FROM startup_reconciliation_runs WHERE account_id = ? "
            "AND instance_epoch = ? AND stage1_outcome = 'passed' ORDER BY id DESC LIMIT 1",
            (account_id, epoch)).fetchone()
    passed_at = _parse(passed["started_at"]) if passed is not None else None
    login_at = _latest_login(conn, account_id)
    resume_at = instance_lock.resumed_at()
    if passed_at is None or (login_at and passed_at < login_at) or (resume_at and passed_at < resume_at):
        reasons.add(STARTUP_RECONCILIATION_INCOMPLETE)
    detail.update(stage1_passed_run=passed["id"] if passed is not None else None,
                  stage1_passed_at=passed["started_at"] if passed is not None else None,
                  last_login_at=login_at.isoformat() if login_at else None,
                  resumed_at=resume_at.isoformat() if resume_at else None)

    last_read = _last_complete_read(conn, account_id, epoch)
    age = (now - last_read).total_seconds() if last_read else None
    if age is None or age > config.GTT_OBSERVATION_MAX_AGE_SECONDS or age < -60:
        reasons.add(BROKER_READ_STALE)
    detail["last_complete_read_at"] = last_read.isoformat() if last_read else None

    inventory = local_inventory(conn, account_id)
    reasons |= local_reasons(inventory)
    detail["unresolved_submission_ids"] = [r["id"] for r in inventory["unresolved_submissions"]]
    detail["persisted"] = None if row is None else {
        "state": row["restriction_state"], "reasons": row["restriction_reasons"], "since": row["restriction_since"],
        "epoch": row["restriction_epoch"], "last_startup_run_id": row["last_startup_run_id"]}
    return {"state": NORMAL if not reasons else RESTRICTED, "reasons": sorted(reasons), "detail": detail}


def describe(reasons) -> str:
    return ", ".join(reasons) if reasons else "none"


def _persist_locked(conn: sqlite3.Connection, account_id: int, sticky: set[str] | None, run_id: int | None,
                    now: datetime) -> dict:
    """Inside an open fenced transaction."""
    derived = derive_restriction(conn, account_id, now=now, sticky=sticky)
    state = derived["state"]
    reasons = [] if state == NORMAL else derived["reasons"]
    current = conn.execute("SELECT restriction_state, restriction_since, last_startup_run_id FROM account_restriction "
                           "WHERE account_id = ?", (account_id,)).fetchone()
    since = (current["restriction_since"] if current is not None and current["restriction_state"] == state
             and current["restriction_since"] else _iso(now))
    last_run = run_id if run_id is not None else (current["last_startup_run_id"] if current is not None else None)
    conn.execute(
        "INSERT INTO account_restriction (account_id, restriction_state, restriction_reasons, restriction_since, "
        "restriction_epoch, last_startup_run_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(account_id) DO UPDATE SET restriction_state = excluded.restriction_state, "
        "restriction_reasons = excluded.restriction_reasons, restriction_since = excluded.restriction_since, "
        "restriction_epoch = excluded.restriction_epoch, last_startup_run_id = excluded.last_startup_run_id, "
        "updated_at = excluded.updated_at",
        (account_id, state, json.dumps(reasons), since, instance_lock.owner_epoch(), last_run))
    return derived


def persist_state(conn: sqlite3.Connection, account_id: int, sticky: set[str] | None = None,
                  run_id: int | None = None, now: datetime | None = None) -> dict:
    with instance_lock.fenced_transaction(conn):
        return _persist_locked(conn, account_id, sticky, run_id, now or _utc_now())


def refresh_persisted_state() -> None:
    """Re-derives and persists the default account's state (end of every reconciliation cycle). Only
    the owner writes; any failure is logged and never disturbs the caller."""
    if not instance_lock.authority_ok():
        return
    from backend.accounts import get_default_account
    from backend.db import get_connection
    try:
        conn = get_connection()
        try:
            if not _table_exists(conn, "account_restriction"):
                return
            persist_state(conn, get_default_account(conn)["id"])
        finally:
            conn.close()
    except Exception:
        logger.exception("event=restriction.refresh_failed (the derived state is still evaluated at confirmation)")


def note_resume() -> None:
    """The heartbeat saw a suspend/resume gap: persist RESUMED_AFTER_SUSPEND (sticky until a Stage 1
    passes after the resume) and re-run Stage 1 now. Called on the event loop."""
    from backend.accounts import get_default_account
    from backend.db import get_connection
    try:
        conn = get_connection()
        try:
            account_id = get_default_account(conn)["id"]
            with instance_lock.fenced_transaction(conn):
                row = conn.execute("SELECT restriction_reasons FROM account_restriction WHERE account_id = ?",
                                   (account_id,)).fetchone()
                sticky = (set(json.loads(row["restriction_reasons"])) & STICKY_REASONS) if row is not None else set()
                _persist_locked(conn, account_id, sticky | {RESUMED_AFTER_SUSPEND}, None, _utc_now())
        finally:
            conn.close()
    except Exception:
        logger.exception("event=restriction.resume_persist_failed (RESUMED_AFTER_SUSPEND is still held in memory)")
    request_run("resume")


# ---- Stage 1: the broker read -------------------------------------------------------------------

_ENDPOINTS = (("orders", "orders"), ("gtts", "get_gtts"), ("holdings", "holdings"), ("positions", "positions"))


def read_broker(kite, *, clock=_utc_now, monotonic=time.monotonic) -> dict:
    """§8.3 step 3: orders → get_gtts → holdings → positions, each with its timestamp, count and error.
    A failed, partial or malformed read is recorded as such and never treated as "absent"."""
    from backend import broker_holdings
    row = {"orders_at": None, "orders_error": None, "orders_count": None, "gtts_at": None, "gtts_error": None,
           "gtts_count": None, "gtts_malformed": None, "holdings_at": None, "holdings_error": None,
           "positions_at": None, "positions_error": None, "session_expired": 0}
    data: dict = {}
    first = last = None
    for name, method in _ENDPOINTS:
        if row["session_expired"]:
            row[f"{name}_error"] = "not attempted: the Kite session expired during the read"
            continue
        started = monotonic()
        try:
            value = getattr(kite, method)()
        except TokenException as e:
            row["session_expired"] = 1
            row[f"{name}_error"] = f"TokenException: {e}"
            row[f"{name}_at"] = _iso(clock())
            continue
        except Exception as e:
            row[f"{name}_error"] = f"{type(e).__name__}: {e}"
            row[f"{name}_at"] = _iso(clock())
            continue
        first = started if first is None else first
        last = monotonic()
        row[f"{name}_at"] = _iso(clock())
        data[name] = value

    if "orders" in data:
        orders = data["orders"]
        if isinstance(orders, list) and all(isinstance(o, dict) for o in orders):
            row["orders_count"] = len(orders)
        else:
            row["orders_error"] = "malformed: orders() did not return a list of orders"
            data.pop("orders")
    if "gtts" in data:
        gtts = data["gtts"]
        if isinstance(gtts, list):
            good = [g for g in gtts if isinstance(g, dict) and g.get("id") is not None]
            row["gtts_count"], row["gtts_malformed"] = len(good), len(gtts) - len(good)
            data["gtts"] = good
        else:
            row["gtts_error"] = "malformed: get_gtts() did not return a list"
            data.pop("gtts")
    held = None
    if "holdings" in data and "positions" in data:
        try:
            held = broker_holdings.instrument_quantities(data["holdings"], data["positions"])
        except broker_holdings.BrokerDataError as e:
            row["holdings_error"] = row["positions_error"] = f"malformed broker data: {e}"
    span = (last - first) if first is not None and last is not None else None
    errors = [row[f"{n}_error"] for n, _ in _ENDPOINTS]
    complete = all(e is None for e in errors) and row["gtts_malformed"] == 0 and held is not None \
        and span is not None and span <= config.STARTUP_READ_SPAN_SECONDS
    return {"row": row, "data": data, "held": held, "span_seconds": span, "complete": complete}


def _gtt_order_ids(gtt: dict) -> list[str]:
    ids = []
    for leg in gtt.get("orders") or []:
        result = (leg or {}).get("result") or {}
        order_id = (result.get("order_result") or {}).get("order_id")
        if order_id:
            ids.append(str(order_id))
    return ids


def _ist_today() -> date:
    from backend.trading_calendar import IST
    return datetime.now(IST).date()


def _triggered_today(gtt: dict) -> bool:
    for leg in gtt.get("orders") or []:
        stamp = ((leg or {}).get("result") or {}).get("timestamp")
        parsed = _parse(stamp)
        if parsed is not None and parsed.date() == _ist_today():
            return True
    return False


def detect_inconsistencies(conn: sqlite3.Connection, account_id: int, read: dict, now: datetime) -> list[dict]:
    """§8.3 step 6: what the four endpoints cannot all be telling the truth about. Each becomes
    BROKER_DATA_INCONSISTENT; nothing is ever defaulted silently."""
    from backend import broker_holdings, risk_engine
    issues: list[dict] = []
    orders = {str(o.get("order_id")): o for o in read["data"].get("orders", []) if o.get("order_id")}
    held = read["held"] or {}
    expected: dict[tuple, int] = {}
    entry_orders: dict[tuple, list] = {}
    for pos in risk_engine.list_open_positions(conn, account_id):
        qty = pos["open_quantity"]
        if not qty or qty < 0:
            continue
        key = (broker_holdings.norm(pos["exchange"]), broker_holdings.norm(pos["symbol"]))
        expected[key] = expected.get(key, 0) + qty
        entry_orders.setdefault(key, []).append(pos["kite_order_id"])
    for key, qty in expected.items():
        instrument = f"{key[0]}:{key[1]}"
        q = held.get(key)
        if q is not None and q.problems:
            issues.append({"code": "BROKER_ROWS_UNRECONCILABLE", "instrument": instrument, "problems": q.problems})
            continue
        total = q.total if q is not None else 0
        if total >= qty:
            continue
        fills = [orders[str(o)] for o in entry_orders.get(key, []) if o is not None and str(o) in orders
                 and (orders[str(o)].get("filled_quantity") or 0) > 0]
        if not fills and q is not None:
            continue  # a plain shortfall: reconciliation's BROKER_POSITION_SHORTFALL finding, not a contradiction
        recent = [f for f in fills if (stamp := _parse(f.get("exchange_timestamp") or f.get("order_timestamp")))
                  and (now - stamp).total_seconds() <= config.FILL_VISIBILITY_TOLERANCE_SECONDS]
        if recent:
            continue  # FILL_NOT_YET_VISIBLE: within the tolerance, not yet a contradiction
        issues.append({"code": "FILL_NOT_REFLECTED" if fills else "APP_POSITION_ABSENT_AT_BROKER",
                       "instrument": instrument, "app_open_quantity": qty, "broker_total": total,
                       "filled_orders": [str(f.get("order_id")) for f in fills]})
    for gtt in read["data"].get("gtts", []):
        if gtt.get("status") != "triggered" or not _triggered_today(gtt):
            continue
        missing = [oid for oid in _gtt_order_ids(gtt) if oid not in orders]
        if missing:
            issues.append({"code": "TRIGGERED_GTT_ORDER_MISSING", "gtt_id": gtt.get("id"), "order_ids": missing})
    return issues


def evaluate_attribution(conn: sqlite3.Connection, account_id: int, read: dict) -> list[dict]:
    """§8.3 step 4, REPORT ONLY (attribution enforcement and its tables are Phase 7). Per instrument key
    the broker-observed total and the app's attributed quantity are kept apart; the app never infers it
    owns shares from a holdings surplus. No baseline exists before Phase 7, so 'exclusive' here means
    only 'the broker total equals the app's quantity and no untagged order today'."""
    from backend import broker_holdings, risk_engine
    held = read["held"]
    app: dict[tuple, int | None] = {}
    for pos in risk_engine.list_open_positions(conn, account_id):
        key = (broker_holdings.norm(pos["exchange"]), broker_holdings.norm(pos["symbol"]))
        qty = pos["open_quantity"]
        app[key] = None if qty is None or qty < 0 or app.get(key, 0) is None else app.get(key, 0) + qty
    app_order_ids = {str(r[0]) for r in conn.execute(
        "SELECT kite_order_id FROM trade_tickets WHERE account_id = ? AND kite_order_id IS NOT NULL", (account_id,))}
    app_gtt_ids = {r[0] for r in conn.execute(
        "SELECT kite_gtt_id FROM trade_tickets WHERE account_id = ? AND kite_gtt_id IS NOT NULL", (account_id,))}
    app_gtt_orders = {oid for g in read["data"].get("gtts", []) if g.get("id") in app_gtt_ids
                      for oid in _gtt_order_ids(g)}
    untagged: dict[tuple, dict] = {}
    for o in read["data"].get("orders", []):
        oid = str(o.get("order_id"))
        if oid in app_order_ids or oid in app_gtt_orders or o.get("status") in ("REJECTED", "CANCELLED"):
            continue
        key = (broker_holdings.norm(o.get("exchange")), broker_holdings.norm(o.get("tradingsymbol")))
        untagged.setdefault(key, {"BUY": [], "SELL": []}).setdefault(str(o.get("transaction_type")), []).append(oid)
    report = []
    for key in sorted(set(app) | {k for k in untagged if k in app}):
        reasons, total = [], None
        qty = app.get(key)
        q = held.get(key) if held is not None else None
        if held is None:
            state = "unknown"
            reasons.append("READ_FAILED")
        elif qty is None:
            state = "unknown"
            reasons.append("APP_QUANTITY_UNKNOWN")
        elif q is not None and q.problems:
            state = "broken"
            reasons.append("MALFORMED_ROWS")
        else:
            total = q.total if q is not None else 0
            sells, buys = untagged.get(key, {}).get("SELL", []), untagged.get(key, {}).get("BUY", [])
            if sells:
                reasons.append("UNTAGGED_SELL_TODAY")
            if total < qty:
                reasons.append("SHORTFALL")
            if buys:
                reasons.append("UNTAGGED_BUY_TODAY")
            if total > qty and not buys:
                reasons.append("UNEXPLAINED_SURPLUS")
            state = ("broken" if {"UNTAGGED_SELL_TODAY", "SHORTFALL"} & set(reasons)
                     else "shared" if reasons else "exclusive")
            if held is not None and broker_holdings.same_symbol_elsewhere(held, *key):
                reasons.append("AMBIGUOUS_IDENTITY")
                state = "broken"
        report.append({"instrument": f"{key[0]}:{key[1]}", "app_attributed_quantity": qty, "broker_observed_total":
                       total if total is not None else (q.total if q is not None and not q.problems else None),
                       "state": state, "reasons": reasons})
    return report


# ---- Stage 1 and Stage 2 --------------------------------------------------------------------------

_stage1_lock = threading.Lock()


def run_stage2(stage1_outcome: str | None) -> dict:
    """Stage 2 (§8.4). Deliberately empty in Phase 2: no recovery write of any kind."""
    return {"writes": 0, "note": STAGE2_NOTE, "stage1_outcome": stage1_outcome}


def _insert_broker_read(conn: sqlite3.Connection, account_id: int, started: datetime, read: dict) -> int:
    row = read["row"]
    cursor = conn.execute(
        "INSERT INTO broker_reads (account_id, purpose, instance_epoch, started_at, finished_at, orders_at, orders_error, "
        "orders_count, gtts_at, gtts_error, gtts_count, gtts_malformed, holdings_at, holdings_error, positions_at, "
        "positions_error, session_expired, complete) VALUES (?, 'startup', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, instance_lock.owner_epoch(), _iso(started), _iso(_utc_now()), row["orders_at"], row["orders_error"],
         row["orders_count"], row["gtts_at"], row["gtts_error"], row["gtts_count"], row["gtts_malformed"],
         row["holdings_at"], row["holdings_error"], row["positions_at"], row["positions_error"],
         row["session_expired"], 1 if read["complete"] else 0))
    return int(cursor.lastrowid)


def run_stage1(trigger: str = "startup") -> dict:
    """One Stage 1 run (sync; called in a worker thread). Never calls a broker write."""
    if not instance_lock.authority_ok():
        return {"outcome": None, "skipped": instance_lock.refusal_code()}
    with _stage1_lock:
        return _run_stage1_locked(trigger)


def _run_stage1_locked(trigger: str) -> dict:
    from backend import account_safety, gtt_observations, kite_client, kite_rest_proxy, reconciliation
    from backend.accounts import get_default_account
    from backend.db import get_connection

    started = _utc_now()
    conn = get_connection()
    sticky: set[str] = set()
    findings: dict = {}
    inventory: dict = {}
    outcome = FAILED
    read_id = None
    account_id = None
    try:
        account = get_default_account(conn)
        account_id = account["id"]
        inventory = local_inventory(conn, account_id)
        conn.commit()  # no write so far; ends the implicit read transaction before the fenced ones

        kite = None
        try:
            kite = kite_client.get_kite()
        except kite_client.NoActiveSessionError:
            sticky.add(NO_SESSION)
            findings["session"] = "no fresh Kite session: log in; Stage 1 re-runs on login"
        except kite_rest_proxy.KiteRestProxyError as e:
            sticky.add(PROXY_UNAVAILABLE)
            findings["session"] = f"the Kite REST proxy cannot be used: {e}"
        route = account_safety.check_rest_route()
        if route is not None:
            sticky.add(PROXY_UNAVAILABLE)
            findings["rest_route"] = route.message
        if kite is None or route is not None:
            outcome = BLOCKED
        else:
            read = read_broker(kite)
            findings["read"] = {"span_seconds": read["span_seconds"], "complete": read["complete"],
                                "errors": {n: read["row"][f"{n}_error"] for n, _ in _ENDPOINTS
                                           if read["row"][f"{n}_error"]}}
            if read["row"]["session_expired"]:
                sticky.add(NO_SESSION)
            if read["data"].get("gtts") is not None:
                gtt_observations.record_book(read["data"]["gtts"])
            classification_failed: list[str] = []
            with reconciliation.CLASSIFICATION_LOCK, instance_lock.fenced_transaction(conn):
                reconciliation.reset_memory_if_resumed()   # Phase 3 G4 (R2-7), under CLASSIFICATION_LOCK
                read_id = _insert_broker_read(conn, account_id, started, read)
                if read["complete"] and account["account_type"] == "trading":
                    # §8.3 step 5: today's checks, once, against this same read. A single complete read
                    # is enough to REPORT at startup (no app write has started from this process).
                    snapshot = reconciliation.BrokerSnapshot(gtts=read["data"]["gtts"], held=read["held"],
                                                             held_loaded=True)
                    reconciliation._isolated(conn, "gtt_state", classification_failed,
                                             reconciliation._check_gtt_state, kite, account, snapshot)
                    reconciliation._isolated(conn, "broker_gtts", classification_failed,
                                             lambda c, a, s: reconciliation._check_broker_gtts(c, a, s, single_read=True),
                                             account, snapshot)
                    reconciliation._isolated(conn, "position_state", classification_failed,
                                             reconciliation._check_position_state, kite, account, snapshot)
            if not read["complete"]:
                sticky.add(BROKER_READ_FAILED)
                outcome = INCOMPLETE
            else:
                inconsistencies = detect_inconsistencies(conn, account_id, read, _utc_now())
                findings["inconsistencies"] = inconsistencies
                findings["attribution"] = evaluate_attribution(conn, account_id, read)
                findings["classification_failed_checks"] = classification_failed
                conn.commit()
                if inconsistencies:
                    sticky.add(BROKER_DATA_INCONSISTENT)
                    outcome = FAILED
                elif classification_failed:
                    outcome = FAILED
                else:
                    outcome = PASSED
    except instance_lock.FencingError as e:
        logger.error("event=startup_reconciliation.fenced %s", e)
        conn.close()
        return {"outcome": None, "skipped": instance_lock.refusal_code() or e.error_code}
    except Exception as e:
        logger.exception("event=startup_reconciliation.error trigger=%s", trigger)
        findings["error"] = f"{type(e).__name__}: {e}"
        outcome = FAILED
        if conn.in_transaction:
            conn.rollback()
    findings["stage2"] = run_stage2(outcome)
    if outcome != PASSED and not sticky:
        sticky.add(STARTUP_RECONCILIATION_INCOMPLETE)
    if outcome == PASSED and instance_lock.resumed_at() is not None:
        instance_lock.clear_resume(started)
    result = {"outcome": outcome, "trigger": trigger, "broker_read_id": read_id, "findings": findings,
              "run_id": None, "reasons_after": None}
    try:
        if account_id is None:
            return result
        with instance_lock.fenced_transaction(conn):
            cursor = conn.execute(
                "INSERT INTO startup_reconciliation_runs (account_id, instance_epoch, trigger, started_at, finished_at, "
                "stage1_outcome, broker_read_id, inventory, findings, reasons_after) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]')",
                (account_id, instance_lock.owner_epoch(), trigger, _iso(started), _iso(_utc_now()), outcome, read_id,
                 json.dumps(inventory, default=str), json.dumps(findings, default=str)))
            run_id = int(cursor.lastrowid)
            derived = _persist_locked(conn, account_id, sticky, run_id, _utc_now())
            conn.execute("UPDATE startup_reconciliation_runs SET reasons_after = ? WHERE id = ?",
                         (json.dumps(derived["reasons"]), run_id))
        result.update(run_id=run_id, reasons_after=derived["reasons"], state=derived["state"])
        log = logger.info if outcome == PASSED else logger.warning
        log("event=startup_reconciliation.run id=%d trigger=%s outcome=%s state=%s reasons=%s", run_id, trigger,
            outcome, derived["state"], describe(derived["reasons"]))
    except instance_lock.FencingError as e:
        logger.error("event=startup_reconciliation.not_persisted %s", e)
        result["outcome"], result["skipped"] = None, instance_lock.refusal_code() or e.error_code
    except Exception:
        logger.exception("event=startup_reconciliation.persist_failed (the account stays RESTRICTED)")
    finally:
        conn.close()
    return result


# ---- supervision --------------------------------------------------------------------------------

_task: asyncio.Task | None = None


async def run_supervised(trigger: str = "startup", *, delays=None) -> dict | None:
    """Stage 1 until it passes: the first attempt carries `trigger`, later ones 'retry', after
    30 s, 60 s, 120 s, then every 300 s. Stops when this process has no authority."""
    delays = tuple(delays or config.STARTUP_RECONCILIATION_RETRY_SECONDS)
    attempt = 0
    while True:
        if not instance_lock.loop_may_run("startup_reconciliation"):
            return None
        try:
            result = await asyncio.to_thread(run_stage1, trigger if attempt == 0 else "retry")
        except Exception:
            logger.exception("event=startup_reconciliation.crashed")
            result = {"outcome": FAILED}
        if result.get("outcome") == PASSED or result.get("skipped"):
            return result
        delay = delays[min(attempt, len(delays) - 1)]
        attempt += 1
        logger.warning("event=startup_reconciliation.retry_scheduled in_s=%s outcome=%s", delay, result.get("outcome"))
        await asyncio.sleep(delay)


def request_run(trigger: str) -> asyncio.Task | None:
    """Start Stage 1 now (startup, login, resume), replacing any pending retry. Event loop only."""
    global _task
    from backend import workers
    previous = _task
    if previous is not None and not previous.done() and not previous.get_loop().is_closed():
        previous.cancel()
    try:
        _task = workers.supervisor.spawn("startup_reconciliation", run_supervised(trigger))
    except RuntimeError:  # no running event loop (a sync caller): the next trigger or retry runs it
        logger.warning("event=startup_reconciliation.not_scheduled trigger=%s (no event loop)", trigger)
        _task = None
    return _task


def status(account_id: int | None = None) -> dict:
    """Read-only, for diagnostics: the derived state, the persisted row and the last run."""
    from backend.accounts import get_default_account
    from backend.db import get_connection
    conn = get_connection()
    try:
        if not _table_exists(conn, "account_restriction"):
            return {"state": RESTRICTED, "reasons": [STARTUP_RECONCILIATION_INCOMPLETE], "last_run": None}
        account_id = account_id or get_default_account(conn)["id"]
        derived = derive_restriction(conn, account_id)
        run = conn.execute("SELECT id, instance_epoch, trigger, started_at, finished_at, stage1_outcome, broker_read_id, "
                           "reasons_after FROM startup_reconciliation_runs WHERE account_id = ? ORDER BY id DESC LIMIT 1",
                           (account_id,)).fetchone()
        derived["last_run"] = dict(run) if run is not None else None
        return derived
    finally:
        conn.close()

