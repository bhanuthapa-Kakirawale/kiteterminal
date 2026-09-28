import json
import logging
import sqlite3
from datetime import date

from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

from backend import config, execution, indicators, instruments, kite_chart, market, position_quantity, risk_engine
from backend.db import get_connection
from backend.strategies import STRATEGIES
from backend.watchlist import list_watchlist

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Config (strategy_configs table) — generic across every registered module
# --------------------------------------------------------------------------

def params_with_defaults(strategy_name: str, stored: dict) -> dict:
    """A stored params dict, backfilled with any key its module has since
    gained.

    Strategy parameters are persisted as a JSON blob written at the time the
    config was last saved. When a new parameter is introduced — as
    trend_pullback's `volume_multiple` was — every config saved before that
    point lacks the key, and a module reading `params["volume_multiple"]`
    raises KeyError on a live scan. The failure is silent until the nightly
    autopilot run, which is the worst possible moment to discover it.

    Backfilling on READ (rather than migrating every row) means a module can
    add a parameter without a schema migration, and an older config keeps
    working with the module's own default for anything it does not mention.
    Stored values always win; only absent keys are filled.
    """
    module = STRATEGIES.get(strategy_name)
    if module is None:
        return dict(stored)
    return {**dict(module.DEFAULT_PARAMS), **(stored or {})}


def get_config(conn: sqlite3.Connection, account_id: int, strategy_name: str) -> dict:
    row = conn.execute(
        "SELECT * FROM strategy_configs WHERE account_id = ? AND strategy_name = ?",
        (account_id, strategy_name),
    ).fetchone()
    if row is None:
        module = STRATEGIES.get(strategy_name)
        return {
            "enabled": False, "params": dict(module.DEFAULT_PARAMS) if module else {},
            "updated_at": None, "current_version": None, "last_disabled_at": None,
        }
    return {
        "enabled": bool(row["enabled"]),
        "params": params_with_defaults(strategy_name, json.loads(row["params"])),
        "updated_at": row["updated_at"],
        "current_version": row["current_version"], "last_disabled_at": row["last_disabled_at"],
    }


def save_config(conn: sqlite3.Connection, account_id: int, strategy_name: str, enabled: bool, params: dict) -> dict:
    """Phase 8: params changes are versioned (backend/db.py's
    strategy_config_versions is the full history; strategy_configs is the
    "current pointer") — but only a GENUINE params change creates a new
    version. Toggling enabled on/off alone never bumps the version, since
    version history exists to trace which parameter set produced a given
    signal, not to track the on/off switch."""
    current = conn.execute(
        "SELECT * FROM strategy_configs WHERE account_id = ? AND strategy_name = ?",
        (account_id, strategy_name),
    ).fetchone()

    was_enabled = bool(current["enabled"]) if current else False
    params_changed = current is None or json.loads(current["params"]) != params

    if params_changed:
        prev_version = current["current_version"] if current else 0
        new_version = prev_version + 1
        conn.execute(
            "UPDATE strategy_config_versions SET effective_to = datetime('now') "
            "WHERE account_id = ? AND strategy_name = ? AND effective_to IS NULL",
            (account_id, strategy_name),
        )
        conn.execute(
            """
            INSERT INTO strategy_config_versions (strategy_name, account_id, version, params_snapshot)
            VALUES (?, ?, ?, ?)
            """,
            (strategy_name, account_id, new_version, json.dumps(params)),
        )
    else:
        new_version = current["current_version"]

    just_disabled = was_enabled and not enabled
    if current is None:
        last_disabled_at = None
    elif just_disabled:
        last_disabled_at = None  # placeholder; set via datetime('now') below instead
    else:
        last_disabled_at = current["last_disabled_at"]  # unchanged

    if just_disabled:
        conn.execute(
            """
            INSERT INTO strategy_configs (account_id, strategy_name, enabled, params, current_version, last_disabled_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(account_id, strategy_name) DO UPDATE SET
                enabled = excluded.enabled, params = excluded.params, current_version = excluded.current_version,
                updated_at = datetime('now'), last_disabled_at = datetime('now')
            """,
            (account_id, strategy_name, int(enabled), json.dumps(params), new_version),
        )
    else:
        conn.execute(
            """
            INSERT INTO strategy_configs (account_id, strategy_name, enabled, params, current_version, last_disabled_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, strategy_name) DO UPDATE SET
                enabled = excluded.enabled, params = excluded.params, current_version = excluded.current_version,
                updated_at = datetime('now'), last_disabled_at = ?
            """,
            (account_id, strategy_name, int(enabled), json.dumps(params), new_version,
             last_disabled_at, last_disabled_at),
        )
    conn.commit()
    return get_config(conn, account_id, strategy_name)


def get_version_history(conn: sqlite3.Connection, account_id: int, strategy_name: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM strategy_config_versions
        WHERE account_id = ? AND strategy_name = ?
        ORDER BY version DESC
        """,
        (account_id, strategy_name),
    ).fetchall()
    versions = [dict(r) for r in rows]
    for v in versions:
        v["params_snapshot"] = json.loads(v["params_snapshot"])

    # Diff each version against the one immediately before it (lower
    # version number = came earlier) — changed keys only.
    by_version = {v["version"]: v["params_snapshot"] for v in versions}
    for v in versions:
        prev = by_version.get(v["version"] - 1)
        if prev is None:
            v["diff_from_previous"] = None
        else:
            v["diff_from_previous"] = {
                k: {"old": prev.get(k), "new": val}
                for k, val in v["params_snapshot"].items()
                if prev.get(k) != val
            }
    return versions


def _current_strategy_version(conn: sqlite3.Connection, account_id: int, strategy_name: str) -> int | None:
    row = conn.execute(
        "SELECT current_version FROM strategy_configs WHERE account_id = ? AND strategy_name = ?",
        (account_id, strategy_name),
    ).fetchone()
    return row["current_version"] if row else None


def list_all_configs(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    """One row per registered strategy module, seeded with defaults for any
    module that has no strategy_configs row yet — drives the frontend's
    strategy selector without requiring a prior save."""
    return [
        {"strategy_name": name, **get_config(conn, account_id, name)}
        for name in STRATEGIES
    ]


# --------------------------------------------------------------------------
# Generic entry/exit scan runners — identical for every strategy module.
# Position sizing, risk-engine validation, and ticket construction never
# vary by strategy (Phase 7 Part A.4) — only the `module` argument's pure
# evaluate_entry/evaluate_exit/compute_entry_levels/rank_key differ.
# --------------------------------------------------------------------------

def _has_existing_strategy_signal(
    conn: sqlite3.Connection, account_id: int, symbol: str, strategy_name: str, signal_date: str | None = None,
) -> bool:
    # Don't re-propose a symbol while an earlier signal is still awaiting a
    # decision or has become an open position, and never re-propose the
    # same candle's signal twice (after the user rejected it, or on a second
    # scan of an unchanged candle). A risk-rejected signal only blocks for
    # the day it was produced — it used to block that symbol/strategy
    # forever until archived.
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM trade_tickets
        WHERE account_id = ? AND symbol = ? AND source = 'strategy' AND strategy_name = ?
          AND (status IN ('draft', 'pending_confirmation', 'confirmed')
               OR (status = 'executed' AND closed_at IS NULL)
               OR (status = 'risk_rejected' AND created_at >= ?)
               OR (? IS NOT NULL AND signal_date = ?))
        """,
        (account_id, symbol, strategy_name, risk_engine._same_day_ist_cutoff_utc(), signal_date, signal_date),
    ).fetchone()
    return row["n"] > 0


def _is_stale(df, last_session: date) -> bool:
    last = date.fromisoformat(str(df["date"].iloc[-1])[:10])
    return (last_session - last).days > config.MAX_DATA_STALENESS_DAYS


def run_entry_scan(
    module, conn: sqlite3.Connection, account: sqlite3.Row, kite: KiteConnect, params: dict,
    ctx: risk_engine.PortfolioContext | None = None, diagnostics: dict | None = None,
) -> list[int]:
    """For every watchlist symbol: module.evaluate_entry(), rank passing
    candidates via module.rank_key, cap at max_signals_per_day, construct a
    ticket for each survivor, and push it through the unmodified Phase 4
    risk engine. Returns the created ticket ids; `diagnostics` (if given) is
    filled with why symbols were not evaluated."""
    diag = diagnostics if diagnostics is not None else {}
    diag.update({"evaluated": 0, "insufficient_history": [], "stale_data": [], "already_signalled": [],
                 "qualified": 0, "kept": 0})
    last_session = market.last_complete_session_date()
    min_history = max(params.get("trend_filter_period", 2), 2)

    candidates = []
    for w in list_watchlist(account["id"]):
        df = indicators.load_ohlc_frame(w["instrument_token"])
        if len(df) < min_history:
            diag["insufficient_history"].append(w["symbol"])
            continue
        if _is_stale(df, last_session):
            diag["stale_data"].append(w["symbol"])
            continue
        diag["evaluated"] += 1
        result = module.evaluate_entry(df, params)
        if not result["passed"]:
            continue
        signal_date = str(result["date"])[:10]
        if _has_existing_strategy_signal(conn, account["id"], w["symbol"], module.STRATEGY_NAME, signal_date):
            diag["already_signalled"].append(w["symbol"])
            continue
        candidates.append({
            "symbol": w["symbol"], "instrument_token": w["instrument_token"], "signal_date": signal_date, **result,
        })

    # How competitive the day was is only knowable HERE — once the list is
    # truncated, "this signal barely made the cut" versus "it was the only
    # candidate all day" is unrecoverable. Both readings matter to someone
    # deciding whether to approve, so the surviving candidates carry their
    # own rank and the size of the field they beat.
    qualified_count = len(candidates)
    cap = params["max_signals_per_day"]
    candidates.sort(key=module.rank_key, reverse=True)
    candidates = candidates[:cap]
    for position, candidate in enumerate(candidates, start=1):
        candidate["rank_position"] = position
        candidate["rank_value"] = module.rank_key(candidate)
        candidate["qualified_count"] = qualified_count
        candidate["max_signals_per_day"] = cap
    diag["qualified"] = qualified_count
    diag["kept"] = len(candidates)

    created_ids = []
    for c in candidates:
        ticket_id = _create_entry_ticket(module, conn, account, kite, c, params, ctx)
        if ticket_id is not None:
            created_ids.append(ticket_id)
    return created_ids


def _build_entry_evaluation(
    module, candidate: dict, params: dict, strategy_version: int | None,
    levels_raw: dict, tick: float, sizing: dict, quantity: int,
) -> dict:
    """The structured record persisted to trade_tickets.entry_evaluation.

    Everything in it comes from the evaluation that actually produced this
    signal — the conditions list and marker bars are taken straight off the
    module's own evaluate_entry result, never re-derived, so the Signal
    Detail view cannot end up explaining a different evaluation than the one
    that fired. The params snapshot is included because a signal approved
    three days after a parameter change must still be explainable against
    the thresholds it was actually judged by.

    `levels_raw` and `tick` are captured specifically so the detail view can
    show WHERE a number changed: the stored levels are already snapped onto
    the exchange tick grid, and without the pre-snap values there is no way
    to tell afterwards whether a stop moved by a paisa or by a rupee. Same
    reasoning for `sizing`, which holds the two competing quantity limits
    before the floor() that produced the final integer.
    """
    return {
        "strategy_name": module.STRATEGY_NAME,
        "strategy_label": getattr(module, "STRATEGY_LABEL", module.STRATEGY_NAME),
        "strategy_version": strategy_version,
        "signal_date": candidate.get("signal_date"),
        "signal_close": candidate.get("close"),
        "atr_14": candidate.get("atr_14"),
        "conditions": candidate.get("conditions", []),
        "markers": candidate.get("markers", []),
        "rank": {
            "position": candidate.get("rank_position"),
            "value": candidate.get("rank_value"),
            "qualified_count": candidate.get("qualified_count"),
            "max_signals_per_day": candidate.get("max_signals_per_day", params.get("max_signals_per_day")),
        },
        "sizing": {
            "portfolio_value": sizing.get("portfolio_value"),
            "risk_based_qty": sizing.get("risk_based_qty"),
            "capital_based_qty": sizing.get("capital_based_qty"),
            "quantity": quantity,
            "entry_price_buffer_pct": config.ENTRY_PRICE_BUFFER_PCT,
        },
        "levels_raw": levels_raw,
        "tick_size": tick,
        "params": dict(params),
    }


def _create_entry_ticket(
    module, conn: sqlite3.Connection, account: sqlite3.Row, kite: KiteConnect, candidate: dict, params: dict,
    ctx: risk_engine.PortfolioContext | None = None,
) -> int | None:
    levels = module.compute_entry_levels(candidate, params)
    if levels is None:
        logger.info("Skipping %s (%s): could not derive valid entry levels", candidate["symbol"], module.STRATEGY_NAME)
        return None

    # Snap every level onto the exchange tick grid here, so the ticket the
    # user approves shows exactly the prices that will be sent to Kite.
    tick = instruments.get_tick_size(candidate["symbol"], "NSE")
    entry_price = market.round_to_tick(levels["entry_price"], tick)
    stop_loss = market.round_to_tick(levels["stop_loss"], tick)
    target_1 = market.round_to_tick(levels["target_1"], tick)
    if not (0 < stop_loss < entry_price < target_1):
        logger.info("Skipping %s (%s): levels collapse on the %.2f tick grid", candidate["symbol"],
                    module.STRATEGY_NAME, tick)
        return None
    trailing_distance = levels.get("trailing_distance")
    if trailing_distance is not None:
        trailing_distance = max(market.round_to_tick(trailing_distance, tick), tick)
    trailing_enabled = trailing_distance is not None

    settings = risk_engine.get_settings(conn, account["id"])
    if ctx is None:
        try:
            ctx = risk_engine.build_portfolio_context(kite)
        except risk_engine.RiskCheckUnavailableError:
            logger.info("Skipping entry scan for %s: portfolio context unavailable", candidate["symbol"])
            return None
    sizing = risk_engine.compute_position_size(entry_price, stop_loss, ctx.portfolio_value, settings)
    quantity = sizing["suggested_qty"]
    if quantity <= 0:
        logger.info("Skipping %s: computed quantity is 0 (risk/capital limits too tight for this stop)", candidate["symbol"])
        return None

    rationale = module.build_entry_rationale(candidate, params)
    metrics = execution.compute_risk_metrics({
        "entry_price": entry_price, "quantity": quantity,
        "stop_loss": stop_loss, "target_1": target_1, "target_2": None,
    })
    strategy_version = _current_strategy_version(conn, account["id"], module.STRATEGY_NAME)
    entry_evaluation = _build_entry_evaluation(
        module, candidate, params, strategy_version, levels, tick, sizing, quantity)
    chart = kite_chart.resolve_and_log(
        conn, account["id"], candidate["symbol"], "NSE", candidate["instrument_token"],
    )

    cursor = conn.execute(
        """
        INSERT INTO trade_tickets (
            account_id, source, strategy_name, strategy_version, symbol, exchange, instrument_token,
            transaction_type, quantity, entry_price, entry_order_type,
            stop_loss, target_1, target_2,
            trailing_enabled, trailing_trigger, trailing_distance,
            product_type, use_gtt, risk_amount, risk_reward_ratio,
            rationale, status, rule_checklist, signal_date,
            entry_evaluation, kite_chart_url, kite_chart_status
        ) VALUES (?, 'strategy', ?, ?, ?, 'NSE', ?, 'BUY', ?, ?, 'LIMIT', ?, ?, NULL,
                  ?, ?, ?, 'CNC', 1, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)
        """,
        (
            account["id"], module.STRATEGY_NAME, strategy_version, candidate["symbol"], candidate["instrument_token"],
            quantity, entry_price, stop_loss, target_1,
            int(trailing_enabled), "after_target_1" if trailing_enabled else None, trailing_distance,
            metrics["risk_amount"], metrics["risk_reward_ratio"], rationale,
            json.dumps(candidate["checklist"]), candidate.get("signal_date"),
            json.dumps(entry_evaluation), chart["url"], chart["status"],
        ),
    )
    ticket_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'created', ?)",
        (ticket_id, f"Entry signal ticket created (source=strategy, {module.STRATEGY_NAME})"),
    )
    conn.commit()

    risk_engine.run_risk_checks_and_transition(conn, account, ticket_id, kite, ctx=ctx)
    return ticket_id


def _has_pending_exit(conn: sqlite3.Connection, position_ticket_id: int) -> bool:
    # An executed exit still in flight (EXITING, PK-8) counts too: the
    # position is still open, but its exit is at the broker. One that ended
    # after a partial fill does not (PK-13): what it left may be exited.
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM trade_tickets
        WHERE closes_ticket_id = ? AND (status IN ('pending_confirmation', 'risk_rejected')
                                        OR {execution.EXIT_IN_FLIGHT_SQL})
        """,
        (position_ticket_id,),
    ).fetchone()
    return row["n"] > 0


def run_exit_scan(module, conn: sqlite3.Connection, account: sqlite3.Row, kite: KiteConnect, params: dict) -> list[int]:
    created_ids = []
    for pos in risk_engine.list_open_positions(conn, account["id"]):
        if pos["strategy_name"] != module.STRATEGY_NAME:
            continue  # only this module manages exits for positions it opened
        if pos["entry_fill_status"] == "pending":
            continue  # entry hasn't filled — nothing to exit yet
        if not pos["open_quantity"] or pos["open_quantity"] < 0:
            continue  # nothing broker-confirmed to sell, or unknown (PK-13)
        if _has_pending_exit(conn, pos["id"]):
            continue
        df = indicators.load_ohlc_frame(pos["instrument_token"])
        if len(df) < 2:
            continue
        result = module.evaluate_exit(dict(pos), df, params)
        if not result["passed"]:
            continue
        ticket_id = _create_exit_ticket(module, conn, account, kite, pos, result)
        if ticket_id is not None:
            created_ids.append(ticket_id)
    return created_ids


def _create_exit_ticket(
    module, conn: sqlite3.Connection, account: sqlite3.Row, kite: KiteConnect, position: sqlite3.Row, result: dict
) -> int:
    # entry_price/stop_loss/target_1 are NOT NULL CHECK(>0) columns on
    # trade_tickets but are not actionable levels for a closing MARKET
    # order — entry_price is today's close (a reference/expected fill
    # price), stop_loss/target_1 are carried over informationally from the
    # position being closed, purely to satisfy the schema.
    tick = instruments.get_tick_size(position["symbol"], position["exchange"])
    entry_price = market.round_to_tick(result["close"], tick)
    # PK-13: sells what the position actually holds, never what it requested.
    quantity = position_quantity.open_quantity(conn, position["id"])
    metrics = execution.compute_risk_metrics({
        "entry_price": entry_price, "quantity": quantity,
        "stop_loss": position["stop_loss"], "target_1": position["target_1"], "target_2": None,
    })
    # Looked up against the ENTRY strategy_name — strategy_configs has no
    # row for the "_exit" name, only the entry module's own name.
    strategy_version = _current_strategy_version(conn, account["id"], module.STRATEGY_NAME)

    cursor = conn.execute(
        """
        INSERT INTO trade_tickets (
            account_id, source, strategy_name, strategy_version, symbol, exchange, instrument_token,
            transaction_type, quantity, entry_price, entry_order_type,
            stop_loss, target_1, target_2,
            trailing_enabled, trailing_trigger, trailing_distance,
            product_type, use_gtt, risk_amount, risk_reward_ratio,
            rationale, status, closes_ticket_id, rule_checklist, mode
        ) VALUES (?, 'strategy', ?, ?, ?, ?, ?, 'SELL', ?, ?, 'MARKET', ?, ?, NULL,
                  0, NULL, NULL, ?, 1, ?, ?, ?, 'draft', ?, ?, ?)
        """,
        (
            account["id"], module.EXIT_STRATEGY_NAME, strategy_version, position["symbol"], position["exchange"], position["instrument_token"],
            quantity, entry_price, position["stop_loss"], position["target_1"],
            position["product_type"], metrics["risk_amount"], metrics["risk_reward_ratio"],
            # An exit inherits the position's mode, never the global one (PK-3).
            result["rationale"], position["id"], json.dumps(result["checklist"]), position["mode"],
        ),
    )
    ticket_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'created', ?)",
        (ticket_id, f"Exit signal ticket created (source=strategy, {module.EXIT_STRATEGY_NAME}): {result['rationale']}"),
    )
    conn.commit()

    risk_engine.run_risk_checks_and_transition(conn, account, ticket_id, kite)
    return ticket_id


def expire_stale_signals(conn: sqlite3.Connection, account_id: int, current_signal_date: str) -> list[int]:
    """Cancels strategy ENTRY signals still awaiting approval that were
    computed from an older candle than the one about to be scanned — their
    limit price and stop come from a close that is no longer current.
    Signals from the same candle are left alone. Exit signals never expire
    (they protect capital; the exit scanner won't duplicate them)."""
    rows = conn.execute(
        """
        SELECT id, signal_date, created_at FROM trade_tickets
        WHERE account_id = ? AND source = 'strategy' AND closes_ticket_id IS NULL
          AND status IN ('draft', 'pending_confirmation')
          AND COALESCE(signal_date, substr(created_at, 1, 10)) < ?
        """,
        (account_id, current_signal_date),
    ).fetchall()
    expired = []
    for r in rows:
        from_date = r["signal_date"] or r["created_at"][:10]
        # Conditional (PK-4): a signal approved since the SELECT is being
        # executed — possibly with an UNKNOWN broker outcome — and must never
        # be overwritten with 'cancelled'.
        if conn.execute(
            "UPDATE trade_tickets SET status = 'cancelled' WHERE id = ? AND status IN ('draft', 'pending_confirmation')",
            (r["id"],),
        ).rowcount != 1:
            continue
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'rejected', ?)",
            (r["id"], f"Expired automatically: entry signal from the {from_date} candle was not approved before "
                      f"the {current_signal_date} scan — its price levels are stale."),
        )
        expired.append(r["id"])
    conn.commit()
    return expired


# --------------------------------------------------------------------------
# Breakeven stop-tightening proposals (not a trade_ticket) — fully
# strategy-agnostic, unchanged from Phase 5: runs once across every open
# position regardless of which module opened it.
# --------------------------------------------------------------------------

def check_breakeven_candidates(
    conn: sqlite3.Connection, account: sqlite3.Row, kite: KiteConnect, params_by_strategy: dict[str, dict]
) -> list[int]:
    # Restricted to trailing_enabled positions (see Phase 5 plan judgment
    # call #7) — a non-trailing position's GTT is GTT_TYPE_OCO with two
    # legs (stop + target); safely moving just the stop leg via modify_gtt
    # requires re-supplying both legs' current values correctly, a
    # materially different and riskier code path than what's specced.
    #
    # `params_by_strategy` maps each enabled strategy_name to its own params
    # dict, so a position's breakeven_trigger_r is always read from the
    # SAME strategy that opened it — not an arbitrary "first enabled
    # strategy" shared across every position regardless of origin.
    created_ids = []
    positions = [
        p for p in risk_engine.list_open_positions(conn, account["id"])
        if p["trailing_enabled"] and p["strategy_name"] in params_by_strategy and p["entry_fill_status"] != "pending"
    ]
    if not positions:
        return created_ids

    keys = [f"{p['exchange']}:{p['symbol']}" for p in positions]
    try:
        ltp_data = kite.ltp(keys)
    except Exception:
        logger.exception("Breakeven check: could not fetch LTPs")
        return created_ids

    for pos in positions:
        key = f"{pos['exchange']}:{pos['symbol']}"
        ltp = ltp_data.get(key, {}).get("last_price")
        if ltp is None:
            continue
        # Breakeven is the ACTUAL fill when the order monitor has recorded it.
        entry = pos["filled_price"] or pos["entry_price"]
        breakeven = market.round_to_tick(entry, instruments.get_tick_size(pos["symbol"], pos["exchange"]), "up")
        original_risk_per_share = abs(entry - pos["stop_loss"])
        if original_risk_per_share <= 0:
            continue
        gain_in_r = (ltp - entry) / original_risk_per_share
        trigger_r = params_by_strategy[pos["strategy_name"]]["breakeven_trigger_r"]
        if gain_in_r < trigger_r:
            continue

        already_pending = conn.execute(
            "SELECT COUNT(*) AS n FROM stop_adjustment_proposals WHERE ticket_id = ? AND status = 'pending'",
            (pos["id"],),
        ).fetchone()["n"]
        if already_pending:
            continue

        current_stop = pos["current_stop_loss"] or pos["stop_loss"]
        if current_stop >= breakeven:
            continue  # already at/above breakeven, nothing to propose

        cursor = conn.execute(
            """
            INSERT INTO stop_adjustment_proposals (ticket_id, current_stop, proposed_stop, reason)
            VALUES (?, ?, ?, ?)
            """,
            (pos["id"], current_stop, breakeven,
             f"Position up {gain_in_r:.2f}R (trigger {trigger_r}R), tightening stop to breakeven"),
        )
        created_ids.append(cursor.lastrowid)
    conn.commit()
    return created_ids


def resolve_stop_adjustment(
    conn: sqlite3.Connection, kite: KiteConnect, proposal_id: int, confirm: bool
) -> dict:
    """Confirm or reject a pending breakeven proposal. Confirm calls
    kite.modify_gtt with the SAME single-trigger/single-leg shape
    trailing_engine._try_ratchet uses, then logs a stop_adjusted_breakeven
    ticket_event on the original ticket. Any condition that would make the
    broker reject the modification is raised as ValueError (-> HTTP 409 with
    a readable message) instead of an unhandled 500.

    The position's OWN mode decides (PK-3), never the global toggle: a live
    position's broker GTT is always modified, so the app never shows a stop
    the broker does not have; a dry-run position only has its recorded stop
    moved and no broker call is made."""
    proposal = conn.execute(
        "SELECT * FROM stop_adjustment_proposals WHERE id = ?", (proposal_id,)
    ).fetchone()
    if proposal is None:
        raise ValueError(f"Stop adjustment proposal {proposal_id} not found")
    if proposal["status"] != "pending":
        raise ValueError(f"Proposal {proposal_id} is already {proposal['status']}")

    if not confirm:
        conn.execute(
            "UPDATE stop_adjustment_proposals SET status = 'rejected', resolved_at = datetime('now') WHERE id = ?",
            (proposal_id,),
        )
        conn.commit()
        return {"id": proposal_id, "status": "rejected"}

    ticket = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (proposal["ticket_id"],)).fetchone()
    if ticket is None or ticket["status"] != "executed" or ticket["closed_at"] is not None:
        raise ValueError("The position this proposal belongs to is no longer open — reject the proposal.")
    mode = ticket["mode"]
    tick = instruments.get_tick_size(ticket["symbol"], ticket["exchange"])
    new_stop = market.round_to_tick(proposal["proposed_stop"], tick)

    if mode == "live":
        if ticket["kite_gtt_id"] is None:
            raise ValueError(f"{ticket['symbol']} has no active protective GTT to move — place the stop in Kite.")
        key = f"{ticket['exchange']}:{ticket['symbol']}"
        try:
            ltp = kite.ltp(key)[key]["last_price"]
        except Exception as e:
            raise ValueError(f"Could not fetch the current price for {ticket['symbol']}: {e}")
        exit_side = execution.resolve_exit_side(ticket["transaction_type"])
        if (exit_side == "SELL" and ltp <= new_stop) or (exit_side == "BUY" and ltp >= new_stop):
            raise ValueError(
                f"{ticket['symbol']} is at ₹{ltp:,.2f}, already through the proposed stop ₹{new_stop:,.2f} — "
                f"exit the position instead of moving the stop."
            )
        # PK-13: the GTT keeps covering exactly the shares held — read under
        # the position's protection lock, so a concurrent fill resize is
        # never overwritten with a stale quantity.
        with position_quantity.protection_lock(ticket["id"]):
            fresh = conn.execute("SELECT * FROM trade_tickets WHERE id = ?", (ticket["id"],)).fetchone()
            quantity = position_quantity.protection_quantity(conn, fresh)
            if not quantity or quantity < 0:
                raise ValueError(f"{ticket['symbol']}: the position's open quantity is {quantity} — its stop cannot be "
                                 f"moved safely; check it in Kite.")
            try:
                kite.modify_gtt(
                    trigger_id=ticket["kite_gtt_id"],
                    trigger_type=kite.GTT_TYPE_SINGLE,
                    tradingsymbol=ticket["symbol"],
                    exchange=ticket["exchange"],
                    trigger_values=[new_stop],
                    last_price=ltp,
                    orders=[{
                        "transaction_type": exit_side,
                        "quantity": quantity,
                        "order_type": "LIMIT",
                        "product": ticket["product_type"],
                        "price": execution.buffered_stop_price(new_stop, ticket["transaction_type"], tick),
                    }],
                )
            except KiteException as e:
                raise ValueError(f"Kite rejected the stop update: {e}")

    conn.execute(
        "UPDATE trade_tickets SET current_stop_loss = ? WHERE id = ?",
        (new_stop, ticket["id"]),
    )
    conn.execute(
        "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'stop_adjusted_breakeven', ?)",
        (ticket["id"], f"Stop moved to breakeven {new_stop} ({mode} mode): {proposal['reason']}"),
    )
    conn.execute(
        "UPDATE stop_adjustment_proposals SET status = 'confirmed', resolved_at = datetime('now') WHERE id = ?",
        (proposal_id,),
    )
    conn.commit()
    return {"id": proposal_id, "status": "confirmed", "mode": mode}


# --------------------------------------------------------------------------
# Daily orchestration (called by backend/automation.py's pipeline and the
# Scan Now button) — loops every strategy_configs row with enabled=1.
# --------------------------------------------------------------------------

def run_daily_scans(account: sqlite3.Row, kite: KiteConnect) -> dict:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM strategy_configs WHERE account_id = ? AND enabled = 1", (account["id"],)
        ).fetchall()
        enabled = []
        for row in rows:
            module = STRATEGIES.get(row["strategy_name"])
            if module is None:
                logger.warning("strategy_configs row references unknown strategy_name %r; skipping", row["strategy_name"])
                continue
            enabled.append((module, params_with_defaults(row["strategy_name"], json.loads(row["params"]))))
        if not enabled:
            return {"enabled": False}

        # One portfolio snapshot for the whole scan — nothing executes
        # mid-scan, so holdings/margins can't change, and fetching them per
        # candidate (twice) risked hitting Kite's rate limit.
        # RiskCheckUnavailableError propagates: nothing can be sized without it.
        ctx = risk_engine.build_portfolio_context(kite)

        per_strategy = {}
        params_by_strategy = {}
        for module, params in enabled:
            params_by_strategy[module.STRATEGY_NAME] = params
            diagnostics: dict = {}
            entry_ids = run_entry_scan(module, conn, account, kite, params, ctx=ctx, diagnostics=diagnostics)
            exit_ids = run_exit_scan(module, conn, account, kite, params)
            per_strategy[module.STRATEGY_NAME] = {
                "entry_tickets": entry_ids, "exit_tickets": exit_ids, "diagnostics": diagnostics,
            }

        # Breakeven check runs once across every enabled strategy's open
        # positions together (it's a position-level, not signal-level,
        # mechanism) — but each position's own strategy's breakeven_trigger_r
        # is used, via params_by_strategy, not an arbitrary shared value.
        breakeven_ids = check_breakeven_candidates(conn, account, kite, params_by_strategy)

        return {"enabled": True, "by_strategy": per_strategy, "breakeven_proposals": breakeven_ids}
    finally:
        conn.close()
