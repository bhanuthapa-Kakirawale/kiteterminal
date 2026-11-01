"""PK-16 (P1-10): GTT reconciliation in BOTH directions.

App -> Kite (existing, upgraded): each live open position's protective GTT
is active and covers exactly its PK-13 protection quantity.
Kite -> app (new): every ACTIVE broker GTT belongs to exactly one live app
position, by exact kite_gtt_id only. Anything else — an orphan, a GTT left
on a closed position, a second GTT on a protected symbol, an unlinked or
ambiguous candidate — is flagged for a human. Nothing is ever cancelled,
resized or relinked by reconciliation.

No sleeps; broker state is driven through FakeKite; nothing reaches Zerodha.
"""

import json

from kiteconnect.exceptions import InputException, NetworkException

from conftest import ticket_row
from test_baseline_api import _insert_ticket

STOP, TARGET = 2900.0, 3300.0


# ---------------------------------------------------------------- helpers

def _reconcile(times=1):
    from backend import reconciliation
    for _ in range(times):
        reconciliation._run_cycle()


def _rows(env, sql, *args):
    conn = env.db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _findings(env, code=None):
    """Every reconciliation row (reviewed or not), optionally of one issue."""
    if code is None:
        return _rows(env, "SELECT * FROM reconciliation_log ORDER BY id")
    return _rows(env, "SELECT * FROM reconciliation_log WHERE issue_code = ? ORDER BY id", code)


def _codes(env):
    return [r["issue_code"] for r in _findings(env)]


def _unresolved(env):
    r = env.client.get("/api/reconciliation/mismatches")
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _legs(quantity, *prices, side="SELL"):
    return [{"transaction_type": side, "quantity": quantity, "order_type": "LIMIT", "product": "CNC", "price": p}
            for p in prices]


def _oco_gtt(env, symbol="TCS", quantity=40, stop=STOP, target=TARGET):
    return env.kite.place_gtt(env.kite.GTT_TYPE_OCO, symbol, "NSE", [stop, target], 3000.0,
                              _legs(quantity, round(stop * 0.995, 2), target))["trigger_id"]


def _single_gtt(env, symbol="TCS", quantity=40, stop=STOP, side="SELL"):
    return env.kite.place_gtt(env.kite.GTT_TYPE_SINGLE, symbol, "NSE", [stop], 3000.0,
                              _legs(quantity, round(stop * 0.995, 2), side=side))["trigger_id"]


def _position(env, quantity=40, gtt_quantity=None, trailing=False, symbol="TCS", hold=True, gtt=True,
              **overrides) -> tuple[int, int | None]:
    """A filled live BUY `quantity` @ 3000 protected by its own GTT (single
    when trailing, OCO otherwise) and held at the broker."""
    gtt_id = None
    if gtt:
        make = _single_gtt if trailing else _oco_gtt
        gtt_id = make(env, symbol=symbol, quantity=gtt_quantity or quantity)
    fields = dict(source="strategy", strategy_name="momentum_breakout", symbol=symbol, kite_gtt_id=gtt_id,
                  quantity=quantity, executed_at="2026-09-01 04:00:00", entry_fill_status="filled",
                  filled_price=3000.0, filled_quantity=quantity, current_stop_loss=STOP,
                  stop_loss=STOP, target_1=TARGET, risk_amount=100.0 * quantity)
    if trailing:
        fields.update(trailing_enabled=1, trailing_trigger="after_target_1", trailing_distance=50.0)
    fields.update(overrides)
    buy_id = _insert_ticket(env.db, **fields)
    if hold:
        held = sum(h["quantity"] for h in env.kite._holdings if h["tradingsymbol"] == symbol)
        env.kite.set_holding(symbol, held + (overrides.get("filled_quantity") or quantity), average_price=3000.0)
    return buy_id, gtt_id


def _exit(env, buy_id, quantity, **overrides) -> int:
    fields = dict(source="strategy", strategy_name="momentum_breakout_exit", transaction_type="SELL",
                  quantity=quantity, entry_order_type="MARKET", entry_price=3100.0, status="executed",
                  mode="live", closes_ticket_id=buy_id, kite_order_id="250000000999",
                  executed_at="2026-09-01 05:00:00", rule_checklist=json.dumps({"time_exit": {"pass": True}}))
    fields.update(overrides)
    return _insert_ticket(env.db, **fields)


def _set(env, ticket_id, **fields):
    conn = env.db.get_connection()
    try:
        cols = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE trade_tickets SET {cols} WHERE id = ?", (*fields.values(), ticket_id))
        conn.commit()
    finally:
        conn.close()


def _writes(env):
    return {name: len(env.kite.calls_to(name)) for name in ("delete_gtt", "modify_gtt", "place_order",
                                                           "cancel_order")}


# ---------------------------------------------------------------- 1-2 healthy

def test_1_healthy_single_gtt_has_no_finding(env):
    _position(env, trailing=True)
    _reconcile(times=2)
    assert _findings(env) == []


def test_2_healthy_oco_gtt_is_one_gtt_not_two(env):
    _, gtt_id = _position(env)
    assert len(env.kite.gtt_book[gtt_id]["orders"]) == 2
    _reconcile(times=2)
    assert _findings(env) == []


# ---------------------------------------------------------------- 3 orphan

def test_3_orphan_gtt_on_an_untraded_symbol_is_flagged_and_left_alone(env):
    gtt_id = _single_gtt(env, symbol="INFY", quantity=15, stop=1400.0)
    writes = _writes(env)
    _reconcile(times=2)
    [row] = _findings(env)
    assert row["issue_code"] == "ORPHAN_GTT"
    assert row["gtt_id"] == gtt_id and row["symbol"] == "INFY"
    assert row["broker_quantity"] == 15 and row["expected_quantity"] == 0
    assert row["severity"] == "urgent" and row["suggested_action"] == "CANCEL IN KITE"
    assert row["ticket_id"] is None and row["resolved"] == 0
    assert "1400" in row["detail"] and "1393" in row["detail"]  # trigger and limit shown
    assert env.kite.gtt_book[gtt_id]["status"] == "active"
    assert _writes(env) == writes  # nothing cancelled, resized or placed


# ---------------------------------------------------------------- 4 / 10 closed position

def test_4_closed_position_that_kept_its_gtt_is_flagged(env):
    buy_id, gtt_id = _position(env, closed_at="2026-09-02 05:00:00")
    _reconcile(times=2)
    [row] = _findings(env)
    assert row["issue_code"] == "GTT_ON_CLOSED_POSITION"
    assert row["ticket_id"] == buy_id and row["gtt_id"] == gtt_id
    assert row["position_state"] == "closed" and row["expected_quantity"] == 0 and row["broker_quantity"] == 40
    assert row["suggested_action"] == "CANCEL IN KITE"
    assert "GTT_ON_CLOSED_POSITION" not in _codes(env)[1:]


def test_4b_cancelled_entry_that_kept_its_gtt_is_flagged(env):
    buy_id, gtt_id = _position(env, hold=False, status="cancelled", entry_fill_status="cancelled",
                               filled_quantity=0)
    _reconcile(times=2)
    [row] = _findings(env)
    assert row["issue_code"] == "GTT_ON_CLOSED_POSITION"
    assert row["position_state"] == "entry cancelled" and row["ticket_id"] == buy_id


def test_10_completed_exit_whose_gtt_delete_failed_is_flagged(env):
    from backend import position_quantity
    buy_id, gtt_id = _position(env)
    exit_id = _exit(env, buy_id, 40, entry_fill_status="filled", filled_quantity=40)
    env.kite.raise_on_next("delete_gtt", InputException("GTT delete failed"))
    conn = env.db.get_connection()
    try:
        result = position_quantity.close_if_flat(conn, env.kite, buy_id, exit_id, "2026-09-01 05:00:00")
    finally:
        conn.close()
    assert result.closed and ticket_row(env.db, buy_id)["kite_gtt_id"] == gtt_id
    _reconcile(times=2)
    [row] = _findings(env, "GTT_ON_CLOSED_POSITION")
    assert row["gtt_id"] == gtt_id and row["position_state"] == "closed"


# ---------------------------------------------------------------- 5-6 quantity

def test_5_oversized_gtt_shows_expected_actual_and_difference(env):
    buy_id, gtt_id = _position(env, quantity=25, gtt_quantity=40)
    writes = _writes(env)
    _reconcile()
    [row] = _findings(env)
    assert row["issue_code"] == "GTT_QUANTITY_TOO_HIGH" and row["severity"] == "urgent"
    assert (row["expected_quantity"], row["broker_quantity"]) == (25, 40)
    assert row["ticket_id"] == buy_id and row["gtt_id"] == gtt_id
    assert "15 more" in row["detail"]
    assert _writes(env) == writes  # detection only: the GTT watcher owns resizing


def test_6_undersized_gtt_names_the_unprotected_shares(env):
    _position(env, quantity=40, gtt_quantity=25)
    _reconcile()
    [row] = _findings(env)
    assert row["issue_code"] == "GTT_QUANTITY_TOO_LOW"
    assert (row["expected_quantity"], row["broker_quantity"]) == (40, 25)
    assert "15 share(s) UNPROTECTED" in row["detail"]


# ---------------------------------------------------------------- 7-8, 21 PK-13 quantities

def test_7_21_partially_filled_entry_expects_its_filled_quantity(env):
    _position(env, quantity=100, gtt_quantity=40, filled_quantity=40, entry_fill_status="partial")
    _reconcile()
    assert _findings(env) == []  # 40 held, 40 protected: the requested 100 is never used


def test_7b_21_gtt_sized_for_the_requested_quantity_is_oversized(env):
    _position(env, quantity=100, gtt_quantity=100, filled_quantity=40, entry_fill_status="partial")
    _reconcile()
    [row] = _findings(env)
    assert row["issue_code"] == "GTT_QUANTITY_TOO_HIGH"
    assert (row["expected_quantity"], row["broker_quantity"]) == (40, 100)


def test_8_partial_exit_residual_is_the_expected_quantity(env):
    buy_id, gtt_id = _position(env, quantity=40, gtt_quantity=25)
    _exit(env, buy_id, 40, entry_fill_status="partial", filled_quantity=15)
    _reconcile()
    assert _findings(env) == []


def test_8b_gtt_still_covering_the_pre_exit_quantity_is_oversized(env):
    buy_id, _ = _position(env, quantity=40)
    _exit(env, buy_id, 40, entry_fill_status="partial", filled_quantity=15)
    _reconcile()
    [row] = _findings(env)
    assert row["issue_code"] == "GTT_QUANTITY_TOO_HIGH"
    assert (row["expected_quantity"], row["broker_quantity"]) == (25, 40)


# ---------------------------------------------------------------- 9 EXITING

def test_9_exiting_position_with_its_gtt_active_is_healthy(env):
    buy_id, gtt_id = _position(env)
    _exit(env, buy_id, 40, entry_fill_status="pending", filled_quantity=0)
    _reconcile(times=2)
    assert _findings(env) == []


def test_9b_position_state_reports_exiting(env):
    buy_id, _ = _position(env)
    _exit(env, buy_id, 40, entry_fill_status="pending", filled_quantity=0)
    _single_gtt(env, quantity=40)  # an extra GTT on the same symbol
    _reconcile(times=2)
    [row] = _findings(env)
    assert row["issue_code"] == "MULTIPLE_ACTIVE_GTTS" and row["position_state"] == "EXITING"


# ---------------------------------------------------------------- 11 manual delete

def test_11_manually_deleted_gtt_is_caught_by_the_forward_check(env):
    buy_id, gtt_id = _position(env)
    env.kite.set_gtt_status(gtt_id, "deleted")
    _reconcile()
    [row] = _findings(env)
    assert row["issue_code"] == "GTT_MISSING" and row["ticket_id"] == buy_id
    assert row["suggested_action"] == "CHECK KITE"
    t = ticket_row(env.db, buy_id)
    assert t["closed_at"] is None and t["kite_gtt_id"] == gtt_id  # reconciliation never mutates the position


# ---------------------------------------------------------------- 12-14, 20 ownership

def test_12_extra_manual_gtt_on_a_protected_position_is_multiple(env):
    buy_id, gtt_id = _position(env)
    extra = _single_gtt(env, quantity=40, stop=2850.0)
    _reconcile(times=2)
    [row] = _findings(env)
    assert row["issue_code"] == "MULTIPLE_ACTIVE_GTTS" and row["gtt_id"] == extra
    assert row["suggested_action"] == "CANCEL IN KITE"
    assert str(gtt_id) in row["detail"]  # names the GTT that already protects it
    assert ticket_row(env.db, buy_id)["kite_gtt_id"] == gtt_id


def test_13_two_candidate_gtts_for_an_unprotected_position_are_ambiguous(env):
    buy_id, _ = _position(env, gtt=False)
    a = _single_gtt(env, quantity=40)
    b = _single_gtt(env, quantity=40, stop=2850.0)
    _reconcile(times=2)
    rows = _findings(env)
    assert {r["issue_code"] for r in rows} == {"AMBIGUOUS_GTT_OWNERSHIP"}
    assert {r["gtt_id"] for r in rows} == {a, b}
    assert ticket_row(env.db, buy_id)["kite_gtt_id"] is None


def test_13b_14_single_plausible_gtt_is_reported_never_relinked(env):
    buy_id, _ = _position(env, gtt=False)
    candidate = _single_gtt(env, quantity=40)
    _reconcile(times=3)
    [row] = _findings(env)
    assert row["issue_code"] == "UNLINKED_POSSIBLE_GTT" and row["gtt_id"] == candidate
    assert row["expected_quantity"] == 40 and row["suggested_action"] == "REVIEW POSITION"
    assert f"#{buy_id}" in row["detail"]
    assert ticket_row(env.db, buy_id)["kite_gtt_id"] is None  # no fuzzy reassignment, ever


def test_14b_same_gtt_id_on_two_positions_is_ambiguous(env):
    a_id, gtt_id = _position(env, quantity=20)
    b_id, _ = _position(env, quantity=20, gtt=False, kite_gtt_id=gtt_id)
    _reconcile(times=2)
    rows = _findings(env, "AMBIGUOUS_GTT_OWNERSHIP")
    assert len(rows) == 1 and rows[0]["gtt_id"] == gtt_id
    assert f"#{a_id}" in rows[0]["detail"] and f"#{b_id}" in rows[0]["detail"]


def test_20_same_symbol_positions_never_cross_link(env):
    a_id, a_gtt = _position(env, quantity=40)
    b_id, b_gtt = _position(env, quantity=25, gtt_quantity=30)
    _reconcile()
    [row] = _findings(env)
    # Only B's own GTT is wrong: A's 40-share GTT is not "B's oversized GTT",
    # and B's is not compared with A's quantity.
    assert row["issue_code"] == "GTT_QUANTITY_TOO_HIGH" and row["ticket_id"] == b_id and row["gtt_id"] == b_gtt
    assert (row["expected_quantity"], row["broker_quantity"]) == (25, 30)


# ---------------------------------------------------------------- 15-16 unavailable data

def test_15_gtt_book_unavailable_writes_nothing_and_says_so(env):
    buy_id, gtt_id = _position(env)
    _single_gtt(env, symbol="INFY", quantity=5)
    env.kite.raise_on_next("get_gtts", NetworkException("Kite unreachable"))
    _reconcile()
    assert _findings(env) == []
    assert ticket_row(env.db, buy_id)["kite_gtt_id"] == gtt_id
    status = env.client.get("/api/reconciliation/status").json()["data"]
    assert status["gtt_book"] == "unavailable" and "Kite unreachable" in status["gtt_error"]
    _reconcile(times=2)  # the book is back: the orphan is found, the position is healthy
    assert _codes(env) == ["ORPHAN_GTT"]
    assert env.client.get("/api/reconciliation/status").json()["data"]["gtt_book"] == "ok"


def test_16_holdings_unavailable_gtt_checks_still_run(env):
    _position(env)
    orphan = _single_gtt(env, symbol="INFY", quantity=5)
    for _ in range(2):  # holdings down on both cycles an orphan needs
        env.kite.raise_on_next("holdings", NetworkException("holdings down"))
        _reconcile()
    assert [(r["issue_code"], r["gtt_id"]) for r in _findings(env)] == [("ORPHAN_GTT", orphan)]
    status = env.client.get("/api/reconciliation/status").json()["data"]
    assert status["holdings"] == "unavailable" and status["gtt_book"] == "ok"


def test_one_broker_snapshot_per_cycle(env):
    _position(env)
    _position(env, symbol="INFY", quantity=10)
    _single_gtt(env, symbol="SUNPHARMA", quantity=3)
    before = {n: len(env.kite.calls_to(n)) for n in ("get_gtts", "holdings", "positions")}
    _reconcile()
    after = {n: len(env.kite.calls_to(n)) for n in ("get_gtts", "holdings", "positions")}
    assert {n: after[n] - before[n] for n in after} == {"get_gtts": 1, "holdings": 1, "positions": 1}


# ---------------------------------------------------------------- confirmation across cycles

def test_kite_to_app_finding_needs_two_consecutive_cycles(env):
    _single_gtt(env, symbol="INFY", quantity=5)
    _reconcile()
    assert _findings(env) == []
    _reconcile()
    assert _codes(env) == ["ORPHAN_GTT"]


def test_gtt_linked_between_cycles_is_never_flagged(env):
    """An entry's GTT exists a moment before the confirming request stores
    its kite_gtt_id: a cycle in that gap sees an unlinked GTT once."""
    gtt_id = _single_gtt(env, quantity=40)
    _reconcile()
    _position(env, gtt=False, trailing=True, kite_gtt_id=gtt_id)
    _reconcile(times=3)
    assert _findings(env) == []


def test_close_then_delete_gap_is_never_flagged(env):
    """closed_at is committed a moment before the GTT is deleted."""
    buy_id, gtt_id = _position(env, closed_at="2026-09-02 05:00:00")
    _reconcile()
    env.kite.set_gtt_status(gtt_id, "deleted")
    _set(env, buy_id, kite_gtt_id=None)
    _reconcile(times=3)
    assert _findings(env) == []


def test_a_changed_fingerprint_restarts_confirmation(env):
    orphan = _single_gtt(env, symbol="INFY", quantity=5)
    _reconcile()
    env.kite.gtt_book[orphan]["orders"][0]["quantity"] = 6
    _reconcile()
    assert _findings(env) == []  # two different states, each seen once
    _reconcile()
    assert [r["broker_quantity"] for r in _findings(env)] == [6]


# ---------------------------------------------------------------- 17-19 dedup and review

def test_17_an_unchanged_finding_is_logged_once(env):
    _single_gtt(env, symbol="INFY", quantity=5)
    _position(env, quantity=25, gtt_quantity=40)
    _reconcile(times=3)
    assert sorted(_codes(env)) == ["GTT_QUANTITY_TOO_HIGH", "ORPHAN_GTT"]


def test_18_a_changed_finding_gets_a_new_row(env):
    orphan = _single_gtt(env, symbol="INFY", quantity=5, stop=1400.0)
    buy_id, gtt_id = _position(env, quantity=25, gtt_quantity=40)
    _reconcile(times=2)
    g = env.kite.gtt_book[orphan]
    g["orders"][0]["quantity"] = 8                      # changed by hand in Kite
    env.kite.gtt_book[gtt_id]["orders"][0]["quantity"] = 35
    env.kite.gtt_book[gtt_id]["orders"][1]["quantity"] = 35
    _reconcile(times=2)
    orphans = _findings(env, "ORPHAN_GTT")
    assert [r["broker_quantity"] for r in orphans] == [5, 8]
    assert [r["broker_quantity"] for r in _findings(env, "GTT_QUANTITY_TOO_HIGH")] == [40, 35]


def test_19_reviewed_orphan_stays_silent_until_it_changes(env):
    orphan = _single_gtt(env, symbol="INFY", quantity=5, stop=1400.0)
    _reconcile(times=2)
    [row] = _unresolved(env)
    r = env.client.post(f"/api/reconciliation/{row['id']}/mark-reviewed", json={"reason": "checked in Kite"})
    assert r.status_code == 200 and r.json()["data"]["resolved"] is True
    _reconcile(times=3)
    assert _unresolved(env) == [] and len(_findings(env)) == 1
    env.kite.gtt_book[orphan]["condition"]["trigger_values"] = [1350.0]  # trigger moved in Kite
    _reconcile(times=2)
    [again] = _unresolved(env)
    assert again["issue_code"] == "ORPHAN_GTT" and "1350" in again["actual"]


def test_19b_orphan_becomes_a_new_finding_when_the_app_opens_that_symbol(env):
    orphan = _single_gtt(env, quantity=40)
    _reconcile(times=2)
    [row] = _unresolved(env)
    env.client.post(f"/api/reconciliation/{row['id']}/mark-reviewed", json={"reason": "checked in Kite"})
    _position(env)  # the app now holds TCS with its own GTT: the old one is a second GTT
    _reconcile(times=2)
    [again] = _unresolved(env)
    assert again["issue_code"] == "MULTIPLE_ACTIVE_GTTS" and again["gtt_id"] == orphan


def test_19c_forward_finding_review_keeps_existing_behaviour(env):
    _position(env, quantity=25, gtt_quantity=40)
    _reconcile()
    [row] = _unresolved(env)
    env.client.post(f"/api/reconciliation/{row['id']}/mark-reviewed", json={"reason": "checked in Kite"})
    _reconcile()
    [again] = _unresolved(env)  # a live position's wrong protection reappears while it lasts
    assert again["issue_code"] == "GTT_QUANTITY_TOO_HIGH" and again["id"] != row["id"]


# ---------------------------------------------------------------- 22-23 scope

def test_22_dry_run_tickets_neither_own_nor_get_checked(env):
    gtt_id = _single_gtt(env, quantity=40)
    _position(env, gtt=False, hold=False, mode="dry_run", kite_gtt_id=gtt_id)
    _position(env, gtt=False, hold=False, mode="dry_run", kite_gtt_id=424242, symbol="INFY")
    _reconcile(times=2)
    assert [(r["issue_code"], r["gtt_id"], r["ticket_id"]) for r in _findings(env)] == [("ORPHAN_GTT", gtt_id, None)]


def test_23_another_accounts_gtt_is_ignored(env):
    other = _single_gtt(env, symbol="INFY", quantity=5)
    mine = _single_gtt(env, symbol="SUNPHARMA", quantity=5)
    env.kite.gtt_book[other]["user_id"] = "ZZ9999"
    env.kite.gtt_book[mine]["user_id"] = "DT0001"   # the trading account's own session user
    _reconcile(times=2)
    assert [(r["issue_code"], r["gtt_id"]) for r in _findings(env)] == [("ORPHAN_GTT", mine)]


def test_buy_side_gtt_the_app_never_placed_is_ignored(env):
    _single_gtt(env, symbol="INFY", quantity=5, side="BUY")  # the user's own buy-the-dip GTT
    _reconcile(times=2)
    assert _findings(env) == []


def test_inactive_gtts_are_never_orphans(env):
    for status in ("triggered", "cancelled", "deleted", "expired", "disabled"):
        env.kite.set_gtt_status(_single_gtt(env, symbol="INFY", quantity=5), status)
    _reconcile(times=2)
    assert _findings(env) == []


# ---------------------------------------------------------------- price

def test_price_mismatch_needs_two_consecutive_cycles(env):
    buy_id, gtt_id = _position(env, trailing=True, current_stop_loss=2950.0)
    _reconcile()
    assert _findings(env) == []
    _reconcile()
    [row] = _findings(env)
    assert row["issue_code"] == "GTT_PRICE_MISMATCH" and row["ticket_id"] == buy_id
    assert "2950" in row["detail"] and "2900" in row["detail"]
    _reconcile()
    assert len(_findings(env)) == 1


def test_price_mismatch_on_the_oco_stop_leg(env):
    _, gtt_id = _position(env)
    env.kite.gtt_book[gtt_id]["condition"]["trigger_values"] = [2800.0, TARGET]  # stop moved by hand
    _reconcile(times=2)
    [row] = _findings(env)
    assert row["issue_code"] == "GTT_PRICE_MISMATCH"


def test_sub_tick_difference_is_not_a_price_mismatch(env):
    _position(env, trailing=True, current_stop_loss=2900.02)  # rounds to the 2900.0 trigger
    _reconcile(times=2)
    assert _findings(env) == []


def test_transient_price_difference_is_not_flagged(env):
    buy_id, _ = _position(env, trailing=True, current_stop_loss=2950.0)
    _reconcile()
    _set(env, buy_id, current_stop_loss=STOP)  # e.g. a ratchet committed between cycles
    _reconcile(times=2)
    assert _findings(env) == []


# ---------------------------------------------------------------- broker shortfall

def test_shortfall_is_structured_and_names_the_armed_gtt(env):
    buy_id, gtt_id = _position(env)
    env.kite.set_holding("TCS", 0)  # sold in Kite by hand
    _reconcile()
    [row] = _findings(env, "BROKER_POSITION_SHORTFALL")
    assert row["check_type"] == "position_state" and row["symbol"] == "TCS"
    assert (row["expected_quantity"], row["broker_quantity"]) == (40, 0)
    assert f"GTT {gtt_id}" in row["detail"]
    assert ticket_row(env.db, buy_id)["closed_at"] is None  # never closed on a holdings figure


# ---------------------------------------------------------------- schema

def test_migration_is_idempotent(env):
    from backend import db
    db.init_db()
    db.init_db()
    cols = {r["name"] for r in _rows(env, "PRAGMA table_info(reconciliation_log)")}
    assert {"issue_code", "severity", "symbol", "gtt_id", "expected_quantity", "broker_quantity",
            "position_state", "suggested_action"} <= cols


def test_status_before_any_cycle(env, monkeypatch):
    from conftest import clear_healthy_live_state
    clear_healthy_live_state(monkeypatch)  # a fresh process (env seeds a reconciled one, SEC-1)
    r = env.client.get("/api/reconciliation/status")
    assert r.status_code == 200 and r.json()["data"]["gtt_book"] == "not_run"
