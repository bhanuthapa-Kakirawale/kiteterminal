"""PK-22I: deterministic restart validation.

Each scenario builds real state through the API, then "restarts" the way a
process restart does — the database survives, process memory does not:
init_db() twice (the startup migration and its no-op second run), fresh
process memory (reconciliation sightings, worker health, paper hooks,
diagnostics cache). Then the background cycles run again (order monitor,
paper exchange, GTT watcher / trailing, reconciliation) and the scenario
checks that state was preserved and nothing was duplicated or reset:
no second broker order or GTT, no second journal row or finding, no
safety state cleared. Worker de-duplication itself is covered in
tests/test_platform_pk22.py (supervisor and app-startup tests).
"""

import pytest
import requests

from conftest import approval_body, account_id, set_mode, ticket_row

BROKER_WRITES = ("place_order", "place_gtt", "modify_gtt", "delete_gtt", "cancel_order")
PRICE, STOP, TARGET = 1500.0, 1450.0, 1600.0


# ---------------------------------------------------------------- helpers

def _rows(env, sql, params=()):
    conn = env.db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _exec(env, sql, params=()):
    conn = env.db.get_connection()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _writes(env) -> dict:
    return {name: len(env.kite.calls_to(name)) for name in BROKER_WRITES}


def _restart(env):
    from backend import diagnostics, paper_broker, reconciliation, worker_health
    env.db.init_db()
    env.db.init_db()
    assert env.db.last_init["applied_migrations"] == [] and env.db.last_init["startup_row_changes"] == 0
    reconciliation.reset_cycle_memory()
    paper_broker.reset_test_hooks()
    worker_health.reset()
    diagnostics.reset_cache()


def _cycles(env, times: int = 2, paper: bool = False):
    from backend import order_monitor, reconciliation, trailing_engine
    for _ in range(times):
        order_monitor.check_pending_orders(kite=env.kite)
        if paper:
            order_monitor.check_paper_orders(market_data=env.kite)
        trailing_engine._tick_sync()
        reconciliation._run_cycle()


def _entry(env, quantity=10, order_type="LIMIT", mode="live", symbol="INFY"):
    set_mode(env.db, mode)
    body = {"symbol": symbol, "exchange": "NSE", "transaction_type": "BUY", "quantity": quantity,
            "entry_price": PRICE, "entry_order_type": order_type, "stop_loss": STOP, "target_1": TARGET,
            "product_type": "CNC", "rationale": "PK-22 restart scenario"}
    r = env.client.post("/api/tickets", json=body)
    assert r.status_code in (200, 201), r.text
    tid = r.json()["data"]["id"]
    r = env.client.post(f"/api/tickets/{tid}/confirm", json=approval_body(env.db, tid, expected_mode=mode))
    return tid, r


def _journal(env, buy_id):
    return _rows(env, "SELECT * FROM closed_trades WHERE buy_ticket_id = ?", (buy_id,))


def _findings(env):
    return _rows(env, "SELECT id, issue_code, ticket_id, resolved FROM reconciliation_log ORDER BY id")


# ---------------------------------------------------------------- scenarios

def test_1_2_clean_database_restarts_without_schema_churn(env):
    import sqlite3
    env.db.init_db()  # absorb the fixture's pre-PK-18 plaintext session token
    before = sqlite3.connect(env.db.DB_PATH).execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    _restart(env)
    after = sqlite3.connect(env.db.DB_PATH).execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    assert before == after
    assert [r["version"] for r in _rows(env, "SELECT version FROM schema_version")] == \
        [v for v, _, _ in env.db.MIGRATIONS]


def test_3_pending_live_order_survives_and_is_never_resubmitted(env, market_open):
    env.kite.auto_fill = False
    tid, r = _entry(env)
    assert r.status_code == 200, r.text
    order_id = ticket_row(env.db, tid)["kite_order_id"]
    writes = _writes(env)
    assert writes["place_order"] == 1 and writes["place_gtt"] == 1

    _restart(env)
    _cycles(env)
    row = ticket_row(env.db, tid)
    assert (row["status"], row["entry_fill_status"], row["kite_order_id"]) == ("executed", "pending", order_id)
    assert _writes(env) == writes                              # nothing placed again

    env.kite.set_order_state(order_id, "COMPLETE", filled_quantity=10, average_price=PRICE)
    _cycles(env)
    row = ticket_row(env.db, tid)
    assert (row["entry_fill_status"], row["filled_quantity"]) == ("filled", 10)
    assert _writes(env) == writes


def test_4_unknown_submission_survives_and_is_adopted_not_resubmitted(env, market_open):
    env.kite.auto_fill = False
    env.kite.accept_then_raise("place_order", requests.ReadTimeout("Read timed out. (read timeout=7)"))
    tid, r = _entry(env)
    assert r.status_code < 500
    row = ticket_row(env.db, tid)
    assert (row["status"], row["submission_state"]) == ("confirmed", "unknown")
    [broker_order] = list(env.kite.order_book)

    _restart(env)
    _cycles(env)
    row = ticket_row(env.db, tid)
    assert (row["status"], row["submission_state"], row["kite_order_id"]) == ("executed", "adopted", broker_order)
    assert len(env.kite.calls_to("place_order")) == 1            # the one accepted order, never a second


def test_5_open_paper_order_survives_and_fills_exactly_once(env, market_open):
    env.kite.ltp_map["NSE:INFY"] = PRICE + 20                   # above the BUY limit: no fill yet
    tid, r = _entry(env, mode="dry_run")
    assert r.status_code == 200, r.text
    paper_id = ticket_row(env.db, tid)["kite_order_id"]
    assert paper_id.startswith("PAPER-O-")

    _restart(env)
    _cycles(env, paper=True)
    assert _rows(env, "SELECT status FROM paper_orders")[0]["status"] == "OPEN"
    env.kite.ltp_map["NSE:INFY"] = PRICE - 5
    _cycles(env, times=3, paper=True)
    orders = _rows(env, "SELECT paper_order_id, status, filled_quantity FROM paper_orders")
    assert orders == [{"paper_order_id": paper_id, "status": "COMPLETE", "filled_quantity": 10}]
    assert ticket_row(env.db, tid)["entry_fill_status"] == "filled"
    assert _writes(env) == {name: 0 for name in BROKER_WRITES}  # dry run never touched Kite


def test_6_partial_position_keeps_its_quantity_and_protection(env, market_open):
    env.kite.auto_fill = False
    tid, _ = _entry(env)
    row = ticket_row(env.db, tid)
    env.kite.set_order_state(row["kite_order_id"], "OPEN", filled_quantity=4, average_price=PRICE)
    _cycles(env, times=1)
    assert ticket_row(env.db, tid)["filled_quantity"] == 4
    gtt = env.kite.gtt_book[row["kite_gtt_id"]]
    assert all(leg["quantity"] == 4 for leg in gtt["orders"])
    writes = _writes(env)

    _restart(env)
    _cycles(env)
    assert ticket_row(env.db, tid)["filled_quantity"] == 4
    assert all(leg["quantity"] == 4 for leg in env.kite.gtt_book[row["kite_gtt_id"]]["orders"])
    assert _writes(env) == writes                               # no re-resize, no second GTT


def test_7_gtt_reconciliation_finding_is_preserved_not_duplicated(env, market_open):
    tid, _ = _entry(env)
    env.kite.set_holding("INFY", 10)                            # the broker holds the position
    _cycles(env, times=1)                                       # entry filled
    gtt_id = ticket_row(env.db, tid)["kite_gtt_id"]
    # The protective GTT disappears at the broker (deleted in Kite by hand):
    # an urgent finding the app must never "repair" by itself.
    del env.kite.gtt_book[gtt_id]
    _cycles(env, times=2)
    # The GTT watcher and reconciliation each record it (under their own
    # wording); what matters is that a restart adds none and resolves none.
    findings = _findings(env)
    assert findings and all(f["ticket_id"] == tid and f["resolved"] == 0 for f in findings)
    writes = _writes(env)

    _restart(env)
    _cycles(env, times=2)
    assert _findings(env) == findings                           # the same unresolved findings, no duplicate
    assert ticket_row(env.db, tid)["closed_at"] is None         # still an open position
    assert _writes(env) == writes                               # no GTT placed behind the user's back


def test_8_open_circuit_breaker_survives_a_restart(env, market_open):
    _exec(env, "UPDATE account_safety_state SET breaker_open = 1, breaker_opened_at = datetime('now'), "
               "breaker_reason = 'three broker failures'")
    _restart(env)
    state = _rows(env, "SELECT breaker_open, breaker_reason FROM account_safety_state")[0]
    assert state == {"breaker_open": 1, "breaker_reason": "three broker failures"}
    tid, r = _entry(env)
    assert r.status_code in (409, 503) and "BROKER_CIRCUIT_OPEN" in r.text
    assert len(env.kite.calls_to("place_order")) == 0
    assert "BREAKER_OPEN" in {b["code"] for b in env.client.get("/api/ready").json()["data"]["blockers"]}


def test_9_trading_lock_survives_a_restart(env, market_open):
    from backend import account_safety
    conn = env.db.get_connection()
    try:
        account_safety.set_trading_lock(conn, account_id(env.db), True, "end of day")
    finally:
        conn.close()
    _restart(env)
    assert _rows(env, "SELECT trading_locked, trading_lock_reason FROM account_safety_state")[0] == \
        {"trading_locked": 1, "trading_lock_reason": "end of day"}
    tid, r = _entry(env)
    # SEC-1: right after a restart nothing has been reconciled yet, so the
    # live-state gate refuses too (503); the lock must still be among the
    # reasons — exactly like the breaker in test_8.
    assert r.status_code in (409, 503) and "TRADING_LOCKED" in r.text
    assert len(env.kite.calls_to("place_order")) == 0


def test_exiting_position_is_journaled_exactly_once_across_restarts(env, market_open):
    tid, _ = _entry(env)
    _cycles(env, times=1)
    assert ticket_row(env.db, tid)["entry_fill_status"] == "filled"
    env.kite.auto_fill = False
    r = env.client.post(f"/api/app-positions/{tid}/exit")
    assert r.status_code == 201, r.text
    exit_id = r.json()["data"]["id"]
    r = env.client.post(f"/api/tickets/{exit_id}/confirm")
    assert r.status_code == 200, r.text
    sell_order = ticket_row(env.db, exit_id)["kite_order_id"]
    writes = _writes(env)

    _restart(env)
    _cycles(env)
    assert ticket_row(env.db, tid)["closed_at"] is None and _journal(env, tid) == []   # still EXITING
    env.kite.set_order_state(sell_order, "COMPLETE", filled_quantity=10, average_price=PRICE + 30)
    _cycles(env)
    assert ticket_row(env.db, tid)["closed_at"] is not None
    assert len(_journal(env, tid)) == 1

    _restart(env)
    _cycles(env)
    assert len(_journal(env, tid)) == 1                         # never journaled twice
    assert _writes(env)["place_order"] == writes["place_order"]
