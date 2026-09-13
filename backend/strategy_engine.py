import json
import logging
import sqlite3

from kiteconnect import KiteConnect

from backend import execution, indicators, risk_engine
from backend.db import get_connection
from backend.strategies import STRATEGIES
from backend.watchlist import list_watchlist

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Config (strategy_configs table) — generic across every registered module
# --------------------------------------------------------------------------

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
        "enabled": bool(row["enabled"]), "params": json.loads(row["params"]), "updated_at": row["updated_at"],
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

def _has_existing_strategy_signal(conn: sqlite3.Connection, account_id: int, symbol: str, strategy_name: str) -> bool:
    # Avoid re-proposing the same symbol every day while an earlier signal is
    # still pending review or already turned into an open position — not
    # explicitly required by the spec, but a direct consequence of
    # max_signals_per_day's own intent (don't flood the queue with repeats).
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM trade_tickets
        WHERE account_id = ? AND symbol = ? AND source = 'strategy' AND strategy_name = ?
          AND ((status IN ('pending_confirmation', 'risk_rejected', 'confirmed'))
               OR (status = 'executed' AND closed_at IS NULL))
        """,
        (account_id, symbol, strategy_name),
    ).fetchone()
    return row["n"] > 0


def run_entry_scan(module, conn: sqlite3.Connection, account: sqlite3.Row, kite: KiteConnect, params: dict) -> list[int]:
    """For every watchlist symbol: module.evaluate_entry(), rank passing
    candidates via module.rank_key, cap at max_signals_per_day, construct a
    ticket for each survivor, and push it through the unmodified Phase 4
    risk engine. Returns the created ticket ids."""
    candidates = []
    for w in list_watchlist(account["id"]):
        if _has_existing_strategy_signal(conn, account["id"], w["symbol"], module.STRATEGY_NAME):
            continue
        df = indicators.load_ohlc_frame(w["instrument_token"])
        result = module.evaluate_entry(df, params)
        if result["passed"]:
            candidates.append({"symbol": w["symbol"], "instrument_token": w["instrument_token"], **result})

    candidates.sort(key=module.rank_key, reverse=True)
    candidates = candidates[: params["max_signals_per_day"]]

    created_ids = []
    for c in candidates:
        ticket_id = _create_entry_ticket(module, conn, account, kite, c, params)
        if ticket_id is not None:
            created_ids.append(ticket_id)
    return created_ids


def _create_entry_ticket(
    module, conn: sqlite3.Connection, account: sqlite3.Row, kite: KiteConnect, candidate: dict, params: dict
) -> int | None:
    levels = module.compute_entry_levels(candidate, params)
    if levels is None:
        logger.info("Skipping %s (%s): could not derive valid entry levels", candidate["symbol"], module.STRATEGY_NAME)
        return None
    entry_price, stop_loss, target_1 = levels["entry_price"], levels["stop_loss"], levels["target_1"]
    trailing_distance = levels.get("trailing_distance")
    trailing_enabled = trailing_distance is not None

    settings = risk_engine.get_settings(conn, account["id"])
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

    cursor = conn.execute(
        """
        INSERT INTO trade_tickets (
            account_id, source, strategy_name, strategy_version, symbol, exchange, instrument_token,
            transaction_type, quantity, entry_price, entry_order_type,
            stop_loss, target_1, target_2,
            trailing_enabled, trailing_trigger, trailing_distance,
            product_type, use_gtt, risk_amount, risk_reward_ratio,
            rationale, status, rule_checklist
        ) VALUES (?, 'strategy', ?, ?, ?, 'NSE', ?, 'BUY', ?, ?, 'LIMIT', ?, ?, NULL,
                  ?, ?, ?, 'CNC', 1, ?, ?, ?, 'draft', ?)
        """,
        (
            account["id"], module.STRATEGY_NAME, strategy_version, candidate["symbol"], candidate["instrument_token"],
            quantity, entry_price, stop_loss, target_1,
            int(trailing_enabled), "after_target_1" if trailing_enabled else None, trailing_distance,
            metrics["risk_amount"], metrics["risk_reward_ratio"], rationale,
            json.dumps(candidate["checklist"]),
        ),
    )
    ticket_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'created', ?)",
        (ticket_id, f"Entry signal ticket created (source=strategy, {module.STRATEGY_NAME})"),
    )
    conn.commit()

    risk_engine.run_risk_checks_and_transition(conn, account, ticket_id, kite)
    return ticket_id


def _has_pending_exit(conn: sqlite3.Connection, position_ticket_id: int) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM trade_tickets
        WHERE closes_ticket_id = ? AND status IN ('pending_confirmation', 'risk_rejected', 'confirmed')
        """,
        (position_ticket_id,),
    ).fetchone()
    return row["n"] > 0


def run_exit_scan(module, conn: sqlite3.Connection, account: sqlite3.Row, kite: KiteConnect, params: dict) -> list[int]:
    created_ids = []
    for pos in risk_engine.list_open_positions(conn, account["id"]):
        if pos["strategy_name"] != module.STRATEGY_NAME:
            continue  # only this module manages exits for positions it opened
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
    entry_price = round(result["close"], 2)
    metrics = execution.compute_risk_metrics({
        "entry_price": entry_price, "quantity": position["quantity"],
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
            rationale, status, closes_ticket_id, rule_checklist
        ) VALUES (?, 'strategy', ?, ?, ?, ?, ?, 'SELL', ?, ?, 'MARKET', ?, ?, NULL,
                  0, NULL, NULL, ?, 1, ?, ?, ?, 'draft', ?, ?)
        """,
        (
            account["id"], module.EXIT_STRATEGY_NAME, strategy_version, position["symbol"], position["exchange"], position["instrument_token"],
            position["quantity"], entry_price, position["stop_loss"], position["target_1"],
            position["product_type"], metrics["risk_amount"], metrics["risk_reward_ratio"],
            result["rationale"], position["id"], json.dumps(result["checklist"]),
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
        if p["trailing_enabled"] and p["strategy_name"] in params_by_strategy
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
        original_risk_per_share = abs(pos["entry_price"] - pos["stop_loss"])
        if original_risk_per_share <= 0:
            continue
        gain_in_r = (ltp - pos["entry_price"]) / original_risk_per_share
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
        if current_stop >= pos["entry_price"]:
            continue  # already at/above breakeven, nothing to propose

        cursor = conn.execute(
            """
            INSERT INTO stop_adjustment_proposals (ticket_id, current_stop, proposed_stop, reason)
            VALUES (?, ?, ?, ?)
            """,
            (pos["id"], current_stop, pos["entry_price"],
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
    trailing_engine._try_ratchet already uses (respecting dry_run/live mode
    exactly like every other order action), then logs a
    stop_adjusted_breakeven ticket_event on the original ticket."""
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
    mode = execution.get_execution_mode()

    if mode == "live":
        exit_side = execution.resolve_exit_side(ticket["transaction_type"])
        kite.modify_gtt(
            trigger_id=ticket["kite_gtt_id"],
            trigger_type=kite.GTT_TYPE_SINGLE,
            tradingsymbol=ticket["symbol"],
            exchange=ticket["exchange"],
            trigger_values=[proposal["proposed_stop"]],
            last_price=proposal["proposed_stop"],
            orders=[{
                "transaction_type": exit_side,
                "quantity": ticket["quantity"],
                "order_type": "LIMIT",
                "product": ticket["product_type"],
                "price": execution.buffered_stop_price(proposal["proposed_stop"], ticket["transaction_type"]),
            }],
        )

    conn.execute(
        "UPDATE trade_tickets SET current_stop_loss = ? WHERE id = ?",
        (proposal["proposed_stop"], ticket["id"]),
    )
    conn.execute(
        "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'stop_adjusted_breakeven', ?)",
        (ticket["id"], f"Stop moved to breakeven {proposal['proposed_stop']} ({mode} mode): {proposal['reason']}"),
    )
    conn.execute(
        "UPDATE stop_adjustment_proposals SET status = 'confirmed', resolved_at = datetime('now') WHERE id = ?",
        (proposal_id,),
    )
    conn.commit()
    return {"id": proposal_id, "status": "confirmed", "mode": mode}


# --------------------------------------------------------------------------
# Daily orchestration (called from data_jobs.run_daily_refresh_loop) — now
# loops every strategy_configs row with enabled=1 for this account, rather
# than calling momentum_breakout-specific functions directly.
# --------------------------------------------------------------------------

def run_daily_scans(account: sqlite3.Row, kite: KiteConnect) -> dict:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM strategy_configs WHERE account_id = ? AND enabled = 1", (account["id"],)
        ).fetchall()

        per_strategy = {}
        params_by_strategy = {}
        for row in rows:
            module = STRATEGIES.get(row["strategy_name"])
            if module is None:
                logger.warning("strategy_configs row references unknown strategy_name %r; skipping", row["strategy_name"])
                continue
            params = json.loads(row["params"])
            params_by_strategy[row["strategy_name"]] = params
            entry_ids = run_entry_scan(module, conn, account, kite, params)
            exit_ids = run_exit_scan(module, conn, account, kite, params)
            per_strategy[row["strategy_name"]] = {"entry_tickets": entry_ids, "exit_tickets": exit_ids}

        if not per_strategy:
            return {"enabled": False}

        # Breakeven check runs once across every enabled strategy's open
        # positions together (it's a position-level, not signal-level,
        # mechanism) — but each position's own strategy's breakeven_trigger_r
        # is used, via params_by_strategy, not an arbitrary shared value.
        breakeven_ids = check_breakeven_candidates(conn, account, kite, params_by_strategy)

        return {"enabled": True, "by_strategy": per_strategy, "breakeven_proposals": breakeven_ids}
    finally:
        conn.close()
