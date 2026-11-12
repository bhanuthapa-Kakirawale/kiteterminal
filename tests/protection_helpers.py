"""Phase 3 S4-S9 test helpers: the dark journal on env's tmp database (never the account database), positions,
FakeKite GTTs and evidence. Test setup may write protection_operations directly (force_state); the backend
may not (only backend/protection.py does; tests/test_protection_s4.py checks it statically)."""

import json
import uuid

from backend import db, instance_lock, protection
from backend.protection_evidence import capture, iso, utc_now

SL_LEG = {"transaction_type": "SELL", "product": "CNC", "order_type": "LIMIT", "price": 2900.0}


def journal(env):
    """The protection journal on env's tmp database. On a v12 database (a build that registers migration 12
    creates env at v12) the journal already exists: it is checked to be exactly v12's and left alone, since the
    dark entry points refuse a v12 history by design. On a v11 database (today's build) the dark protection
    schema is applied (which refuses the account database by itself)."""
    conn = env.db.get_connection()
    try:
        newest = [(h["version"], h["migration_name"]) for h in db.migration_history(conn)][-1]
        if newest == (12, "phase3_protection_journal"):
            assert db.v12_schema_problems(conn, empty_journal=False) == []
            return
        result = db.apply_dark_protection_schema(conn)
        assert result["action"] in ("applied", "already_present")
    finally:
        conn.close()


def conn_of(env):
    return env.db.get_connection()


def account_id(conn):
    return conn.execute("SELECT id FROM accounts WHERE is_default = 1").fetchone()["id"]


def make_position(env, *, symbol="TCS", token=2953217, qty=10, mode="live", closed=False, gtt_id=None,
                  exchange="NSE"):
    conn = conn_of(env)
    try:
        aid = account_id(conn)
        cur = conn.execute(
            "INSERT INTO trade_tickets (account_id, symbol, exchange, instrument_token, transaction_type, quantity, "
            "entry_price, entry_order_type, stop_loss, target_1, product_type, rationale, status, mode, kite_order_id, "
            "kite_gtt_id, executed_at, filled_quantity, entry_fill_status, closed_at) VALUES (?, ?, ?, ?, 'BUY', ?, "
            "3000, 'MARKET', 2900, 3300, 'CNC', 'test', 'executed', ?, ?, ?, datetime('now'), ?, 'filled', ?)",
            (aid, symbol, exchange, token, qty, mode, f"ORD-{uuid.uuid4().hex[:8]}", gtt_id, qty,
             None if not closed else "2026-11-11 10:00:00"))
        pid = cur.lastrowid
        if closed:  # a full exit of the whole quantity (open quantity 0)
            conn.execute(
                "INSERT INTO trade_tickets (account_id, symbol, exchange, instrument_token, transaction_type, quantity, "
                "entry_price, entry_order_type, stop_loss, target_1, product_type, rationale, status, mode, "
                "closes_ticket_id, filled_quantity, entry_fill_status) VALUES (?, ?, ?, ?, 'SELL', ?, 3000, 'MARKET', "
                "2900, 3300, 'CNC', 'exit', 'executed', ?, ?, ?, 'filled')", (aid, symbol, exchange, token, qty, mode, pid, qty))
        conn.commit()
        return pid
    finally:
        conn.close()


def request(qty=10, *, legs=1, trigger=2910.0, price=2900.0, symbol="TCS", exchange="NSE"):
    if legs == 1:
        return {"trigger_type": "single", "tradingsymbol": symbol, "exchange": exchange, "trigger_values": [trigger],
                "last_price": 3000.0, "orders": [dict(SL_LEG, quantity=qty, price=price)]}
    return {"trigger_type": "two-leg", "tradingsymbol": symbol, "exchange": exchange,
            "trigger_values": [trigger, 3300.0], "last_price": 3000.0,
            "orders": [dict(SL_LEG, quantity=qty, price=price), dict(SL_LEG, quantity=qty, price=3290.0)]}


def gtt(env, req=None, *, status="active"):
    """A GTT placed in env's FakeKite WITHOUT going through the recorded/fenced call path. Returns its id."""
    req = req or request()
    gid = env.kite._new_gtt(req["trigger_type"], req["tradingsymbol"], req["exchange"], req["trigger_values"],
                            req["last_price"], req["orders"])
    env.kite.gtt_book[gid]["status"] = status
    return gid


def read(env, purpose="prewrite"):
    conn = conn_of(env)
    try:
        return capture(conn, env.kite, account_id(conn), purpose)
    finally:
        conn.close()


def force_state(env, op_id, state, **cols):
    """Test setup only: puts an op into `state` with the columns its CHECKs need, bypassing the engine."""
    sets = {"state": state, **cols}
    if state in protection.TERMINAL_STATES and "resolved_at" not in sets:
        sets["resolved_at"] = iso(utc_now())
    if state in protection.OPEN_STATES:
        sets.setdefault("resolved_at", None)
    if state in ("sending", "acknowledged", "confirmed", "rejected", "unknown", "adopted", "absent_after_settle"):
        sets.setdefault("sent_at", iso(utc_now()))
        sets.setdefault("attempts", 1)
    conn = conn_of(env)
    try:
        conn.execute(f"UPDATE protection_operations SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                     (*sets.values(), op_id))
        conn.commit()
    finally:
        conn.close()


def claim(env, position_id, kind="create", reason="fill", *, req=None, evidence=None, target=None, direction=None,
          automatic=False, dx=None, may_increase=True):
    conn = conn_of(env)
    try:
        if kind == "create" and evidence is None:
            evidence = read(env)
        with protection.journal_transaction(conn):
            op = protection.claim(conn, position_id=position_id, kind=kind, reason=reason,
                                  request=req if req is not None else (request() if kind != "cancel" else {}),
                                  direction=direction, target_gtt_id=target, automatic=automatic, dx=dx,
                                  evidence=evidence, may_increase_exposure=may_increase)
        return op
    finally:
        conn.close()


def op(env, op_id):
    conn = conn_of(env)
    try:
        return protection.get_op(conn, op_id)
    finally:
        conn.close()


def events(env, position_id, prefix="protection_"):
    conn = conn_of(env)
    try:
        return [(r["event_type"], json.loads(r["detail"])) for r in conn.execute(
            "SELECT event_type, detail FROM ticket_events WHERE ticket_id = ? AND event_type LIKE ? ORDER BY id",
            (position_id, prefix + "%"))]
    finally:
        conn.close()


def epoch():
    return instance_lock.owner_epoch()
