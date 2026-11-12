"""GTT-after-fill Phase 1: the dark protection-journal schema (backend/db.py,
docs/PROPOSAL_GTT_AFTER_FILL_LIFECYCLE_REVIEW.md §5.2 / §12,
docs/PHASE_1_DARK_SCHEMA_REPORT.md), amended by Phase 3 stage S1 to the R6
DDL of docs/PHASE_3_DESIGN_DECISIONS.md §4.1 and renumbered to v12 (still
dark, DD-2).

Every database here is a temporary file under the test's tmp_path (the `env`
fixture, or a second scratch database built the same way). The real account
database is never opened: the session guard refuses it, and the dark-schema
functions refuse it themselves (test 12).

Numbered after the Phase 1 brief:
  1 creation  2 repetition  3 definitions  4 uniqueness  5 foreign keys
  6 states  7 malformed rows  8 rollback  9 rollback keeps data
  10 failure handling  11 workflows leave the schema unused  12 no account data
and, for the R6 amendments (S1):
  13 kind / reason / direction  14 retry bound  15 sent_at and absent_after_settle
  16 overlay  17 manual resolution  18 open vs terminal  19 index partition
"""

import ast
import json
import logging
import re
import sqlite3
import uuid
from pathlib import Path

import pytest

import real_data_guard
import schema_builds
from conftest import account_id, approval_body, set_mode, ticket_row
from test_baseline_api import _insert_ticket, _manual_ticket
from test_live_safety import _monitor, _protective_oco_gtt, market_open  # noqa: F401 — market_open is a fixture

# The dark entry points run only on a v1..v11 history: env's database is created by the Phase 2 build on either
# build (tests/schema_builds.py), as it was before migration 12 was registered.
pytestmark = pytest.mark.v11_database

REPO = Path(__file__).resolve().parent.parent
NEW_OBJECTS = ("protection_operations", "protection_operation_resolutions", "idx_protection_ops_one_open_change",
               "idx_protection_ops_one_open_cancel", "idx_trade_tickets_unique_gtt")

# R6 §4.1 and DD-7, pinned here independently of backend/db.py.
_STATES = ("intent", "sending", "acknowledged", "unknown", "needs_review",
           "confirmed", "adopted", "rejected", "not_sent", "absent_after_settle", "resolved_manual")
_OPEN = ("intent", "sending", "acknowledged", "unknown", "needs_review")
_TERMINAL = ("confirmed", "adopted", "rejected", "not_sent", "absent_after_settle", "resolved_manual")
_SENT = ("sending", "acknowledged", "confirmed", "rejected", "unknown", "adopted", "absent_after_settle")
_EVENT_TYPES = ("protection_pending", "protection_submitted", "protection_confirmed",
                "protection_unknown", "protection_adopted", "protection_cancelled",
                "protection_rejected", "protection_not_sent", "protection_absent_after_settle",
                "protection_needs_review", "protection_superseded", "protection_resolved",
                "protection_late_gtt_appeared")
_REASONS = {
    "create": ("fill", "user_recreate"),
    "modify": ("fill_resize", "exit_fill", "trailing", "attribution_cap", "user_modify"),
    "cancel": ("position_closed", "supersede_cleanup", "duplicate_cleanup", "exit_prepare", "user_cancel"),
}
_DIRECTIONS = ("up", "down", "tighten", "loosen")
# A consistent (reason, direction) per kind for fixtures that do not care.
_KIND_DEFAULTS = {"create": ("fill", None), "modify": ("trailing", "tighten"), "cancel": ("position_closed", None)}
_SENT_AT = "2026-11-07 04:00:00"
_RESOLVED_AT = "2026-11-07 04:05:00"
_OVERLAY = {"superseded_at": "2026-11-07 04:01:00", "superseded_by_kind": "emergency_exit", "superseded_by_ref": "77"}


# ---------------------------------------------------------------- helpers

def _conn(env):
    return env.db.get_connection()


def _apply(env) -> dict:
    conn = _conn(env)
    try:
        return env.db.apply_dark_protection_schema(conn)
    finally:
        conn.close()


def _rollback(env) -> dict:
    conn = _conn(env)
    try:
        return env.db.rollback_dark_protection_schema(conn)
    finally:
        conn.close()


def _status(env) -> dict:
    conn = _conn(env)
    try:
        return env.db.dark_protection_schema_status(conn)
    finally:
        conn.close()


def _schema(env) -> dict:
    """Every schema object, by (type, name) -> stored SQL."""
    conn = _conn(env)
    try:
        return {(r["type"], r["name"]): r["sql"] for r in conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex_%'")}
    finally:
        conn.close()


def _content(env, *, drop_columns=()) -> dict:
    """Every row of every table (sqlite_sequence included), ordered by rowid."""
    conn = _conn(env)
    try:
        out = {}
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        for table in tables:
            rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
            for row in rows:
                for column in drop_columns:
                    row.pop(column, None)
            out[table] = rows
        return out
    finally:
        conn.close()


def _exec(env, sql, *args):
    conn = _conn(env)
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _rows(env, sql, *args):
    conn = _conn(env)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _backups(env) -> list[str]:
    return sorted(p.name for p in env.tmp_path.glob("*_bak*"))


def _op_fields(env, pos, **overrides) -> dict:
    """A journal row consistent with its kind and state, unless overridden."""
    kind = overrides.get("kind", "create")
    state = overrides.get("state", "intent")
    reason, direction = _KIND_DEFAULTS.get(kind, ("fill", None))
    row = {
        "op_uuid": str(uuid.uuid4()), "account_id": account_id(env.db), "position_id": pos,
        "mode": "live", "kind": kind, "reason": reason, "direction": direction, "state": state,
        "basis_filled_quantity": 40, "basis_exits_filled": 0, "requested_quantity": 40,
        "request_fingerprint": json.dumps({"symbol": "TCS", "legs": [40]}, sort_keys=True),
        "request_payload": json.dumps({"type": "two-leg", "orders": []}),
        "instance_epoch": 1,
        "target_gtt_id": None if kind == "create" else 9001,
        "sent_at": _SENT_AT if state in _SENT else None,
        "result_gtt_id": 9002 if kind == "create" and state in ("acknowledged", "confirmed", "adopted") else None,
        "complete_reads_after": 2 if state == "absent_after_settle" else 0,
        "resolved_at": None if state in _OPEN else _RESOLVED_AT,
    }
    row.update(overrides)
    return row


def _insert_op(env, pos, **overrides) -> int:
    row = _op_fields(env, pos, **overrides)
    cols = ", ".join(row)
    return _exec(env, f"INSERT INTO protection_operations ({cols}) VALUES ({', '.join('?' * len(row))})",
                 *row.values())


def _position(env, **fields) -> int:
    return _insert_ticket(env.db, **fields)


def _broker_read(env, *, complete=1) -> int:
    """A broker_reads row (v11) for a resolution to cite."""
    return _exec(env, "INSERT INTO broker_reads (account_id, purpose, instance_epoch, started_at, finished_at, "
                      "gtts_malformed, complete) VALUES (?, 'resolution', 1, ?, ?, 0, ?)",
                 account_id(env.db), "2026-11-07 05:00:00", "2026-11-07 05:00:02", complete)


def _resolution_fields(env, op, **overrides) -> dict:
    outcome = overrides.get("outcome", "no_app_gtt")
    row = {"op_id": op, "outcome": outcome, "linked_gtt_id": 9003 if outcome == "linked_gtt" else None,
           "reason": "checked in Kite: no GTT for this op", "resolver": "operator", "instance_epoch": 1}
    row.update(overrides)
    if "evidence_read_id" not in row:
        row["evidence_read_id"] = _broker_read(env)
    return row


def _insert_resolution(env, op, **overrides) -> int:
    row = _resolution_fields(env, op, **overrides)
    cols = ", ".join(row)
    return _exec(env, f"INSERT INTO protection_operation_resolutions ({cols}) "
                      f"VALUES ({', '.join('?' * len(row))})", *row.values())


def _resolve_manually(env, op, *, resolved_at=_RESOLVED_AT, **overrides) -> int:
    """The Q3 sequence in one transaction: the resolution row first, then the
    op's needs_review -> resolved_manual update that points at it."""
    row = _resolution_fields(env, op, **overrides)
    conn = _conn(env)
    try:
        resolution = conn.execute(
            f"INSERT INTO protection_operation_resolutions ({', '.join(row)}) "
            f"VALUES ({', '.join('?' * len(row))})", tuple(row.values())).lastrowid
        cur = conn.execute("UPDATE protection_operations SET state = 'resolved_manual', resolution_id = ?, "
                           "resolved_at = ?, updated_at = datetime('now') WHERE id = ? AND state = 'needs_review'",
                           (resolution, resolved_at, op))
        assert cur.rowcount == 1
        conn.commit()
        return resolution
    finally:
        conn.close()


def _insert_terminal_op(env, pos, terminal, **overrides) -> int:
    """An op of a kind that can hold `terminal` (absent_after_settle: a create),
    resolved_manual reached through _resolve_manually."""
    kind = overrides.pop("kind", "create" if terminal == "absent_after_settle" else "modify")
    if terminal == "resolved_manual":
        op = _insert_op(env, pos, kind=kind, state="needs_review", **overrides)
        _resolve_manually(env, op)
        return op
    return _insert_op(env, pos, kind=kind, state=terminal, **overrides)


def _terminate(env, op, terminal):
    """Moves an open create op into `terminal` with the columns it requires."""
    if terminal == "resolved_manual":
        _exec(env, "UPDATE protection_operations SET state = 'needs_review' WHERE id = ?", op)
        _resolve_manually(env, op)
        return
    fields = _op_fields(env, None, kind="create", state=terminal)
    _exec(env, "UPDATE protection_operations SET state = ?, sent_at = ?, result_gtt_id = ?, complete_reads_after = ?, "
               "resolved_at = ? WHERE id = ?", terminal, fields["sent_at"], fields["result_gtt_id"],
          fields["complete_reads_after"], fields["resolved_at"], op)


def _seed_application_data(env) -> dict:
    """Rows in the tables Phase 1 touches (and their neighbours), so rollback
    has something to keep: positions with and without GTT ids (live and paper),
    an exit, events of several types, a closed trade and a reconciliation finding."""
    buy = _position(env, kite_gtt_id=111, filled_quantity=5, entry_fill_status="filled")
    paper = _position(env, mode="dry_run", kite_gtt_id="PAPER-G-7", kite_order_id="PAPER-O-7", filled_quantity=5)
    flat = _position(env, kite_gtt_id=None)
    sell = _position(env, transaction_type="SELL", closes_ticket_id=buy, status="cancelled")
    for ticket, event in ((buy, "created"), (buy, "executed"), (paper, "created"), (sell, "submission_unknown"),
                          (flat, "archived")):
        _exec(env, "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, ?, ?)",
              ticket, event, f"seed {event}")
    # A deleted high-id event: the AUTOINCREMENT high-water mark must survive the rebuilds.
    gone = _exec(env, "INSERT INTO ticket_events (ticket_id, event_type, detail) VALUES (?, 'modified', 'gone')", buy)
    _exec(env, "DELETE FROM ticket_events WHERE id = ?", gone)
    _exec(env, "INSERT INTO reconciliation_log (ticket_id, check_type, expected, actual, detail) "
               "VALUES (?, 'order_status', 'a', 'b', 'seed')", buy)
    return {"buy": buy, "paper": paper, "flat": flat, "sell": sell, "gone_event": gone}


# ================================================================ 1: creation

def test_1_apply_creates_every_dark_object_on_a_scratch_database(env):
    assert _status(env) == {"state": "absent", "objects": {
        "protection_operations": False, "protection_operation_resolutions": False,
        "idx_protection_ops_one_open_change": False, "idx_protection_ops_one_open_cancel": False,
        "trade_tickets.protection_state": False, "ticket_events.protection_event_types": False,
    }, "unique_gtt_index": False, "mismatched": []}

    result = _apply(env)

    assert result["action"] == "applied" and result["unique_gtt_index"] == "created"
    assert result["duplicate_gtt_ids"] == [] and result["backup"].startswith("test.db.pre_dark_protection_schema_bak")
    status = _status(env)
    assert status["state"] == "present" and status["unique_gtt_index"] and all(status["objects"].values())
    schema = _schema(env)
    assert ("table", "protection_operations") in schema
    assert ("table", "protection_operation_resolutions") in schema
    assert ("index", "idx_protection_ops_one_open_change") in schema
    assert ("index", "idx_protection_ops_one_open_cancel") in schema
    assert ("index", "idx_trade_tickets_unique_gtt") in schema
    assert not any(name == "idx_protection_ops_one_open" for _, name in schema)  # the Phase 1 index is gone
    assert "protection_state" in {r["name"] for r in _rows(env, "PRAGMA table_info(trade_tickets)")}


def test_1b_the_schema_is_dark_init_db_never_creates_it_and_the_version_is_unchanged(env):
    # init_db() of the Phase 2 build (run by the env fixture, v11_database) did not create anything.
    assert _status(env)["state"] == "absent"
    # Phase 2 registered v11 (phase2_instance_coordination). The journal is registered only by the separately
    # authorized registration diff (schema_builds.V12_REGISTERED_IN_THIS_BUILD), and then only as migration 12
    # through the late-bound wrapper: a dark function is never a MIGRATIONS entry.
    assert env.db.LATEST_SCHEMA_VERSION == schema_builds.expected_latest()
    assert [v for v, _, _ in env.db.MIGRATIONS] == list(range(1, schema_builds.expected_latest() + 1))
    assert [(v, n) for v, n, _ in env.db.MIGRATIONS if v == 11] == [(11, "phase2_instance_coordination")]
    dark = {env.db._m012_protection_journal, env.db._r012_protection_journal}
    assert not any(fn in dark for _, _, fn in env.db.MIGRATIONS)
    # M-9: renumbered; the colliding v11 names are gone.
    assert not hasattr(env.db, "_m011_protection_journal_dark")
    assert not hasattr(env.db, "_r011_protection_journal_dark")

    _apply(env)
    history = _rows(env, "SELECT version, migration_name FROM schema_version ORDER BY version")
    assert [h["version"] for h in history] == list(range(1, 12))  # no schema_version row for the dark schema
    conn = _conn(env)
    try:
        status = env.db.schema_status(conn)
        assert status["current_version"] == 11                    # the dark objects never count as migration 12
        assert status["up_to_date"] is (not schema_builds.V12_REGISTERED_IN_THIS_BUILD)
    finally:
        conn.close()


# ============================================================== 2: repetition

def test_2_applying_again_creates_nothing_and_writes_nothing(env):
    _apply(env)
    schema, content, backups = _schema(env), _content(env), _backups(env)

    for _ in range(3):
        again = _apply(env)
        assert again == {"action": "already_present", "unique_gtt_index": "present", "duplicate_gtt_ids": [],
                         "backup": None}
    assert _schema(env) == schema and _content(env) == content and _backups(env) == backups
    counts = _rows(env, "SELECT name, COUNT(*) AS n FROM sqlite_master GROUP BY name HAVING n > 1")
    assert counts == []


def test_2b_a_restart_init_db_over_the_dark_schema_changes_nothing(env):
    _apply(env)
    schema = _schema(env)
    # The restart of the build the dark schema belongs to (Phase 2). On a build registering v12 a start refuses this
    # v11 database (the authorization gate) and migrate-v12 keeps the objects (test_v12_registration_prep RP02).
    with schema_builds.phase2_build(env.db):
        env.db.init_db()
    assert env.db.last_init["applied_migrations"] == [] and _schema(env) == schema
    assert _status(env)["state"] == "present"


def test_2c_apply_rollback_apply_cycles_cleanly(env):
    for _ in range(2):
        assert _apply(env)["action"] == "applied"
        assert _rollback(env)["action"] == "rolled_back"
    assert _apply(env)["action"] == "applied" and _status(env)["state"] == "present"


def test_2d_updated_at_is_filled_by_its_default(env):
    _apply(env)
    op = _insert_op(env, _position(env))
    row = _rows(env, "SELECT created_at, updated_at FROM protection_operations WHERE id = ?", op)[0]
    assert row["updated_at"] is not None and row["updated_at"] == row["created_at"]


# ============================================================= 3: definitions

def test_3_protection_operations_columns_are_the_r6_ddl(env):
    _apply(env)
    columns = [(r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"])
               for r in _rows(env, "PRAGMA table_info(protection_operations)")]
    assert columns == [
        ("id", "INTEGER", 0, None, 1),
        ("op_uuid", "TEXT", 1, None, 0),
        ("account_id", "INTEGER", 1, None, 0),
        ("position_id", "INTEGER", 1, None, 0),
        ("mode", "TEXT", 1, None, 0),
        ("kind", "TEXT", 1, None, 0),
        ("reason", "TEXT", 1, None, 0),
        ("direction", "TEXT", 0, None, 0),
        ("state", "TEXT", 1, None, 0),
        ("basis_filled_quantity", "INTEGER", 1, None, 0),
        ("basis_exits_filled", "INTEGER", 1, None, 0),
        ("requested_quantity", "INTEGER", 1, None, 0),
        ("request_fingerprint", "TEXT", 1, None, 0),
        ("request_payload", "TEXT", 1, None, 0),
        ("target_gtt_id", "INTEGER", 0, None, 0),
        ("result_gtt_id", "INTEGER", 0, None, 0),
        ("instance_epoch", "INTEGER", 1, None, 0),
        ("attempts", "INTEGER", 1, "0", 0),
        ("complete_reads_after", "INTEGER", 1, "0", 0),
        ("superseded_at", "TEXT", 0, None, 0),
        ("superseded_by_kind", "TEXT", 0, None, 0),
        ("superseded_by_ref", "TEXT", 0, None, 0),
        ("resolution_id", "INTEGER", 0, None, 0),
        ("created_at", "TEXT", 1, "datetime('now')", 0),
        ("updated_at", "TEXT", 1, "datetime('now')", 0),
        ("sent_at", "TEXT", 0, None, 0),
        ("acknowledged_at", "TEXT", 0, None, 0),
        ("resolved_at", "TEXT", 0, None, 0),
        ("last_checked_at", "TEXT", 0, None, 0),
        ("evidence", "TEXT", 0, None, 0),
    ]
    sql = _schema(env)[("table", "protection_operations")]
    assert "AUTOINCREMENT" in sql
    foreign = {(r["from"], r["table"], r["to"]) for r in _rows(env, "PRAGMA foreign_key_list(protection_operations)")}
    assert foreign == {("account_id", "accounts", "id"), ("position_id", "trade_tickets", "id"),
                       ("resolution_id", "protection_operation_resolutions", "id")}


def test_3b_indexes_are_unique_and_partial_with_the_r6_predicates(env):
    _apply(env)
    ops = {r["name"]: r for r in _rows(env, "PRAGMA index_list(protection_operations)")}
    for name in ("idx_protection_ops_one_open_change", "idx_protection_ops_one_open_cancel"):
        assert ops[name]["unique"] == 1 and ops[name]["partial"] == 1, name
    assert [r["name"] for r in _rows(env, "PRAGMA index_info(idx_protection_ops_one_open_change)")] == ["position_id"]
    assert [r["name"] for r in _rows(env, "PRAGMA index_info(idx_protection_ops_one_open_cancel)")] == [
        "position_id", "target_gtt_id"]
    assert any(r["unique"] == 1 and r["origin"] == "u" for r in ops.values())  # op_uuid UNIQUE
    schema = _schema(env)
    open_list = "('intent', 'sending', 'acknowledged', 'unknown', 'needs_review')"
    assert re.sub(r"\s+", " ", schema[("index", "idx_protection_ops_one_open_change")]).endswith(
        f"WHERE kind IN ('create', 'modify') AND state IN {open_list}")
    assert re.sub(r"\s+", " ", schema[("index", "idx_protection_ops_one_open_cancel")]).endswith(
        f"WHERE kind = 'cancel' AND state IN {open_list}")

    tickets = {r["name"]: r for r in _rows(env, "PRAGMA index_list(trade_tickets)")}
    assert tickets["idx_trade_tickets_unique_gtt"]["unique"] == 1 and tickets["idx_trade_tickets_unique_gtt"]["partial"] == 1
    assert [r["name"] for r in _rows(env, "PRAGMA index_info(idx_trade_tickets_unique_gtt)")] == ["kite_gtt_id"]
    assert _schema(env)[("index", "idx_trade_tickets_unique_gtt")].endswith("WHERE kite_gtt_id IS NOT NULL")
    # No index was added that R6 does not define.
    assert set(ops) - {"idx_protection_ops_one_open_change", "idx_protection_ops_one_open_cancel"} == {
        n for n, r in ops.items() if r["origin"] == "u"}
    resolutions = _rows(env, "PRAGMA index_list(protection_operation_resolutions)")
    assert [r["origin"] for r in resolutions] == ["u"]  # only op_id UNIQUE


def test_3c_ticket_events_gains_exactly_the_13_protection_event_types(env):
    before = env.db._ticket_event_types(_schema(env)[("table", "ticket_events")])
    _apply(env)
    after = env.db._ticket_event_types(_schema(env)[("table", "ticket_events")])
    assert after == before + list(_EVENT_TYPES)
    assert env.db.PROTECTION_EVENT_TYPES == _EVENT_TYPES and len(_EVENT_TYPES) == 13
    assert "protection_failed" in before and "protection_failed" not in _EVENT_TYPES  # base type, not the suffix
    assert [r["name"] for r in _rows(env, "PRAGMA table_info(ticket_events)")] == list(env.db._TICKET_EVENTS_COLUMNS)
    assert {r["name"] for r in _rows(env, "PRAGMA index_list(ticket_events)")} == {"idx_ticket_events_ticket"}


def test_3d_protection_operation_resolutions_columns_and_keys_are_the_r6_ddl(env):
    _apply(env)
    columns = [(r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"])
               for r in _rows(env, "PRAGMA table_info(protection_operation_resolutions)")]
    assert columns == [
        ("id", "INTEGER", 0, None, 1),
        ("op_id", "INTEGER", 1, None, 0),
        ("outcome", "TEXT", 1, None, 0),
        ("linked_gtt_id", "INTEGER", 0, None, 0),
        ("reason", "TEXT", 1, None, 0),
        ("evidence_read_id", "INTEGER", 1, None, 0),
        ("resolver", "TEXT", 1, None, 0),
        ("instance_epoch", "INTEGER", 1, None, 0),
        ("created_at", "TEXT", 1, "datetime('now')", 0),
    ]
    assert "AUTOINCREMENT" in _schema(env)[("table", "protection_operation_resolutions")]
    foreign = {(r["from"], r["table"], r["to"])
               for r in _rows(env, "PRAGMA foreign_key_list(protection_operation_resolutions)")}
    assert foreign == {("op_id", "protection_operations", "id"), ("evidence_read_id", "broker_reads", "id")}
    (unique,) = _rows(env, "PRAGMA index_list(protection_operation_resolutions)")
    assert unique["unique"] == 1
    assert [r["name"] for r in _rows(env, f"PRAGMA index_info({unique['name']})")] == ["op_id"]


# docs/PHASE_3_DESIGN_DECISIONS.md §4.1, transcribed with its `--` comments
# removed and `<the 11 states>` written out in the order the section lists them.
_R6_SQL = {
    "protection_operations": """
CREATE TABLE protection_operations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    op_uuid               TEXT NOT NULL UNIQUE,
    account_id            INTEGER NOT NULL REFERENCES accounts(id),
    position_id           INTEGER NOT NULL REFERENCES trade_tickets(id),
    mode                  TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    kind                  TEXT NOT NULL CHECK (kind IN ('create', 'modify', 'cancel')),
    reason                TEXT NOT NULL CHECK (reason IN ('fill', 'user_recreate',
                              'fill_resize', 'exit_fill', 'trailing', 'attribution_cap', 'user_modify',
                              'position_closed', 'supersede_cleanup', 'duplicate_cleanup', 'exit_prepare',
                              'user_cancel')),
    direction             TEXT CHECK (direction IS NULL OR direction IN ('up', 'down', 'tighten', 'loosen')),
    state                 TEXT NOT NULL CHECK (state IN ('intent', 'sending', 'acknowledged', 'unknown',
                              'needs_review', 'confirmed', 'adopted', 'rejected', 'not_sent', 'absent_after_settle',
                              'resolved_manual')),
    basis_filled_quantity INTEGER NOT NULL CHECK (basis_filled_quantity >= 0),
    basis_exits_filled    INTEGER NOT NULL CHECK (basis_exits_filled >= 0),
    requested_quantity    INTEGER NOT NULL CHECK (requested_quantity >= 0),
    request_fingerprint   TEXT NOT NULL CHECK (json_valid(request_fingerprint)),
    request_payload       TEXT NOT NULL CHECK (json_valid(request_payload)),
    target_gtt_id         INTEGER,
    result_gtt_id         INTEGER,
    instance_epoch        INTEGER NOT NULL,
    attempts              INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    complete_reads_after  INTEGER NOT NULL DEFAULT 0 CHECK (complete_reads_after >= 0),
    superseded_at         TEXT,
    superseded_by_kind    TEXT CHECK (superseded_by_kind IS NULL OR
                              superseded_by_kind IN ('emergency_exit', 'exit', 'close', 'user')),
    superseded_by_ref     TEXT,
    resolution_id         INTEGER REFERENCES protection_operation_resolutions(id),
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at               TEXT,
    acknowledged_at       TEXT,
    resolved_at           TEXT,
    last_checked_at       TEXT,
    evidence              TEXT CHECK (evidence IS NULL OR json_valid(evidence)),
    CHECK ((kind = 'create' AND reason IN ('fill', 'user_recreate') AND direction IS NULL)
        OR (kind = 'modify' AND reason IN ('fill_resize', 'exit_fill', 'trailing', 'attribution_cap', 'user_modify')
                            AND direction IS NOT NULL)
        OR (kind = 'cancel' AND reason IN ('position_closed', 'supersede_cleanup', 'duplicate_cleanup',
                                           'exit_prepare', 'user_cancel') AND direction IS NULL)),
    CHECK (reason != 'fill_resize' OR direction = 'up'),
    CHECK (reason NOT IN ('exit_fill', 'attribution_cap') OR direction = 'down'),
    CHECK (reason != 'trailing' OR direction = 'tighten'),
    CHECK (attempts <= 1 OR kind = 'cancel' OR (kind = 'modify' AND direction IN ('down', 'tighten'))),
    CHECK (kind = 'create' OR target_gtt_id IS NOT NULL),
    CHECK (kind != 'create' OR state NOT IN ('acknowledged', 'confirmed', 'adopted') OR result_gtt_id IS NOT NULL),
    CHECK (state NOT IN ('intent', 'not_sent') OR sent_at IS NULL),
    CHECK (state NOT IN ('sending', 'acknowledged', 'confirmed', 'rejected', 'unknown', 'adopted',
                         'absent_after_settle') OR sent_at IS NOT NULL),
    CHECK (state != 'absent_after_settle' OR (kind = 'create' AND sent_at IS NOT NULL AND complete_reads_after >= 2)),
    CHECK ((superseded_at IS NULL) = (superseded_by_kind IS NULL)
       AND (superseded_at IS NULL) = (superseded_by_ref IS NULL)),
    CHECK (NOT (state = 'intent' AND superseded_at IS NOT NULL)),
    CHECK ((state = 'resolved_manual') = (resolution_id IS NOT NULL)),
    CHECK ((state IN ('intent', 'sending', 'acknowledged', 'unknown', 'needs_review')) = (resolved_at IS NULL))
)""",
    "idx_protection_ops_one_open_change": """
CREATE UNIQUE INDEX idx_protection_ops_one_open_change ON protection_operations(position_id)
  WHERE kind IN ('create', 'modify') AND state IN ('intent', 'sending', 'acknowledged', 'unknown', 'needs_review')""",
    "idx_protection_ops_one_open_cancel": """
CREATE UNIQUE INDEX idx_protection_ops_one_open_cancel ON protection_operations(position_id, target_gtt_id)
  WHERE kind = 'cancel' AND state IN ('intent', 'sending', 'acknowledged', 'unknown', 'needs_review')""",
    "protection_operation_resolutions": """
CREATE TABLE protection_operation_resolutions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id            INTEGER NOT NULL UNIQUE REFERENCES protection_operations(id),
    outcome          TEXT NOT NULL CHECK (outcome IN ('linked_gtt', 'no_app_gtt', 'deleted_in_kite_verified')),
    linked_gtt_id    INTEGER,
    reason           TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    evidence_read_id INTEGER NOT NULL REFERENCES broker_reads(id),
    resolver         TEXT NOT NULL CHECK (length(trim(resolver)) > 0),
    instance_epoch   INTEGER NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((outcome = 'linked_gtt') = (linked_gtt_id IS NOT NULL))
)""",
}


def _normalised(sql):
    return " ".join(sql.split())


def test_3e_the_stored_sql_is_the_r6_section_4_1_ddl_without_its_comments(env):
    _apply(env)
    stored = {name: sql for (_, name), sql in _schema(env).items() if name in _R6_SQL}
    assert set(stored) == set(_R6_SQL)
    for name, expected in _R6_SQL.items():
        assert "--" not in stored[name], name  # the explanatory comments stay in Python
        assert _normalised(stored[name]) == _normalised(expected), name
    # The enumerations behind the DDL, in the R6 order.
    assert env.db.PROTECTION_OPERATION_STATES == _STATES
    assert env.db.PROTECTION_OPEN_OPERATION_STATES == _OPEN
    assert set(_STATES) - set(_OPEN) == set(_TERMINAL)


# ============================================================== 4: uniqueness

def test_4_op_uuid_is_unique(env):
    _apply(env)
    pos = _position(env)
    _insert_op(env, pos, op_uuid="op-1", state="confirmed")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE.*op_uuid"):
        _insert_op(env, _position(env), op_uuid="op-1", state="confirmed")


@pytest.mark.parametrize("first", _OPEN)
@pytest.mark.parametrize("second", _OPEN)
def test_4b_at_most_one_open_change_per_position(env, first, second):
    _apply(env)
    for first_kind, second_kind in (("create", "modify"), ("modify", "create"), ("modify", "modify")):
        pos = _position(env)
        _insert_op(env, pos, state=first, kind=first_kind)
        with pytest.raises(sqlite3.IntegrityError, match=r"UNIQUE.*protection_operations\.position_id$"):
            _insert_op(env, pos, state=second, kind=second_kind)
        # Another position is unaffected.
        _insert_op(env, _position(env), state=second, kind=second_kind)


@pytest.mark.parametrize("terminal", _TERMINAL)
def test_4c_terminal_operations_never_block_and_resolving_frees_the_position(env, terminal):
    _apply(env)
    pos = _position(env)
    for _ in range(3):
        _insert_terminal_op(env, pos, terminal)
    op = _insert_op(env, pos, state="intent" if terminal == "not_sent" else "sending")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_op(env, pos, state="intent", kind="modify")
    _insert_op(env, pos, state="intent", kind="cancel")  # R6: a cancel coexists with an open change
    _terminate(env, op, terminal)
    _insert_op(env, pos, state="intent", kind="modify")  # the position is free again


def test_4d_a_gtt_id_protects_at_most_one_ticket(env):
    _apply(env)
    _position(env, kite_gtt_id=555)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE.*trade_tickets.kite_gtt_id"):
        _position(env, kite_gtt_id=555)
    other = _position(env)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE.*trade_tickets.kite_gtt_id"):
        _exec(env, "UPDATE trade_tickets SET kite_gtt_id = 555 WHERE id = ?", other)
    # Paper ids are unique too; NULL is never a duplicate.
    _position(env, mode="dry_run", kite_gtt_id="PAPER-G-1")
    with pytest.raises(sqlite3.IntegrityError):
        _position(env, mode="dry_run", kite_gtt_id="PAPER-G-1")
    for _ in range(3):
        _position(env, kite_gtt_id=None)


# ============================================================ 5: foreign keys

def test_5_position_and_account_must_exist(env):
    _apply(env)
    pos = _position(env)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert_op(env, 999_999)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert_op(env, pos, account_id=999_999)
    _insert_op(env, pos)  # both exist
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _exec(env, "DELETE FROM trade_tickets WHERE id = ?", pos)  # a journaled position cannot vanish


def test_5b_a_resolution_id_must_name_an_existing_resolution(env):
    _apply(env)
    pos = _position(env)
    op = _insert_op(env, pos, kind="modify", state="needs_review")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _exec(env, "UPDATE protection_operations SET state = 'resolved_manual', resolution_id = 424242, "
                   "resolved_at = ? WHERE id = ?", _RESOLVED_AT, op)
    _resolve_manually(env, op)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _exec(env, "DELETE FROM protection_operations WHERE id = ?", op)  # its resolution still cites it


# ================================================================== 6: states

@pytest.mark.parametrize("state,kind", [
    (s, k) for s in _STATES for k in ("create", "modify", "cancel")
    if not (s == "absent_after_settle" and k != "create")  # M-8: create only
])
def test_6_every_r6_state_and_kind_is_accepted(env, state, kind):
    _apply(env)
    pos = _position(env)
    if state == "resolved_manual":
        op = _insert_op(env, pos, state="needs_review", kind=kind)
        _resolve_manually(env, op)
    else:
        op = _insert_op(env, pos, state=state, kind=kind)
    assert _rows(env, "SELECT state, kind FROM protection_operations WHERE id = ?", op) == [
        {"state": state, "kind": kind}]


@pytest.mark.parametrize("overrides", [
    dict(state="pending"), dict(state="failed"), dict(state="SENDING"), dict(state=""),
    dict(state="abandoned"), dict(state="superseded"),                    # the Phase 1 enum, removed by R6
    dict(kind="delete"), dict(kind="resize"), dict(mode="paper"), dict(mode="LIVE"),
    dict(reason="cleanup"), dict(reason="other"), dict(reason="FILL"), dict(reason=""),
    dict(kind="modify", direction="neutral"), dict(kind="modify", direction="UP"),
    dict(state="sending", **{**_OVERLAY, "superseded_by_kind": "system"}),
], ids=lambda o: "-".join(f"{k}={v}" for k, v in o.items() if not k.startswith("superseded_") or k.endswith("kind")))
def test_6b_values_outside_the_r6_enumerations_are_refused(env, overrides):
    _apply(env)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_op(env, _position(env), **overrides)


def test_6c_protection_state_accepts_the_section_3_3_names_and_null_only(env):
    _apply(env)
    pos = _position(env)
    assert ticket_row(env.db, pos)["protection_state"] is None
    for name in env.db.PROTECTION_STATES:
        _exec(env, "UPDATE trade_tickets SET protection_state = ? WHERE id = ?", name, pos)
    assert len(env.db.PROTECTION_STATES) == 15
    for bad in ("PROTECTED", "protection_pending", "PROTECTION_CLOSED", "CLOSED_CLEANUP_REQUIRED", ""):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _exec(env, "UPDATE trade_tickets SET protection_state = ? WHERE id = ?", bad, pos)
    _exec(env, "UPDATE trade_tickets SET protection_state = NULL WHERE id = ?", pos)


def test_6d_the_protection_event_types_exist_only_with_the_dark_schema(env):
    pos = _position(env)
    for event in _EVENT_TYPES:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _exec(env, "INSERT INTO ticket_events (ticket_id, event_type) VALUES (?, ?)", pos, event)
    _apply(env)
    for event in _EVENT_TYPES:
        _exec(env, "INSERT INTO ticket_events (ticket_id, event_type) VALUES (?, ?)", pos, event)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _exec(env, "INSERT INTO ticket_events (ticket_id, event_type) VALUES (?, 'protection_failed_x')", pos)


# ========================================================== 7: malformed rows

@pytest.mark.parametrize("overrides,why", [
    (dict(requested_quantity=-1), "negative requested quantity"),
    (dict(basis_filled_quantity=-1), "negative fill basis"),
    (dict(basis_exits_filled=-1), "negative exit basis"),
    (dict(attempts=-1, kind="modify"), "negative attempts"),
    (dict(complete_reads_after=-1), "negative settle evidence"),
    (dict(kind="create", attempts=2), "a create is never sent twice"),
    (dict(kind="modify", target_gtt_id=None), "modify names its GTT"),
    (dict(kind="cancel", target_gtt_id=None), "cancel names its GTT"),
    (dict(state="intent", sent_at=_SENT_AT), "intent has sent nothing"),
    (dict(state="sending", sent_at=None), "sending is recorded with its send time"),
    (dict(state="unknown", sent_at=None), "unknown was sent"),
    (dict(state="rejected", sent_at=None), "rejected was sent"),
    (dict(state="acknowledged", result_gtt_id=None), "an acknowledged create knows its GTT"),
    (dict(state="confirmed", result_gtt_id=None), "a confirmed create knows its GTT"),
    (dict(state="adopted", result_gtt_id=None), "an adopted create knows its GTT"),
    (dict(request_payload="not json"), "payload is JSON"),
    (dict(request_fingerprint="{oops"), "fingerprint is JSON"),
    (dict(evidence="[1,"), "evidence is JSON"),
])
def test_7_inconsistent_rows_are_refused(env, overrides, why):
    _apply(env)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_op(env, _position(env), **overrides)


@pytest.mark.parametrize("column", ["op_uuid", "account_id", "position_id", "mode", "kind", "reason", "state",
                                    "basis_filled_quantity", "basis_exits_filled", "requested_quantity",
                                    "request_fingerprint", "request_payload", "instance_epoch", "updated_at"])
def test_7b_required_columns_are_not_null(env, column):
    _apply(env)
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        _insert_op(env, _position(env), **{column: None})


def test_7c_the_allowed_variants_of_those_rules_are_accepted(env):
    _apply(env)
    pos = _position(env)
    _insert_op(env, pos, kind="modify", direction="tighten", attempts=5, state="confirmed")  # may be retried
    _insert_op(env, pos, kind="cancel", requested_quantity=0, state="confirmed")
    _insert_op(env, pos, kind="create", state="not_sent")  # not sent
    _insert_op(env, pos, kind="create", state="absent_after_settle", complete_reads_after=2,
               evidence=json.dumps({"reads": [11, 12]}))  # absent after settle
    _insert_op(env, pos, kind="create", state="rejected", result_gtt_id=None)
    _insert_op(env, pos, kind="create", state="confirmed", result_gtt_id="PAPER-G-3", mode="dry_run")
    stored = _rows(env, "SELECT result_gtt_id, typeof(result_gtt_id) AS t FROM protection_operations "
                        "WHERE mode = 'dry_run'")
    assert stored == [{"result_gtt_id": "PAPER-G-3", "t": "text"}]


# ================================================================ 8: rollback

def test_8_rollback_restores_the_schema_byte_for_byte(env):
    before = _schema(env)
    _apply(env)
    assert _schema(env) != before
    result = _rollback(env)
    assert result["action"] == "rolled_back"
    assert result["backup"].startswith("test.db.pre_dark_protection_rollback_bak")
    assert _schema(env) == before  # every stored CREATE statement, ticket_events and trade_tickets included
    assert _status(env)["state"] == "absent"
    for name in NEW_OBJECTS:
        assert not _rows(env, "SELECT 1 FROM sqlite_master WHERE name = ?", name)
    assert _rows(env, "PRAGMA integrity_check") == [{"integrity_check": "ok"}]
    assert _rows(env, "PRAGMA foreign_key_check") == []


def test_8b_rollback_is_idempotent_and_a_no_op_on_a_database_without_the_schema(env):
    schema, content = _schema(env), _content(env)
    assert _rollback(env) == {"action": "already_absent", "backup": None}
    _apply(env)
    _rollback(env)
    assert _rollback(env) == {"action": "already_absent", "backup": None}
    assert _schema(env) == schema and _content(env) == content


def test_8c_rollback_also_removes_a_skipped_unique_gtt_index_state(env):
    _position(env, kite_gtt_id=42)
    dup = _position(env, kite_gtt_id=42)
    before = _schema(env)
    assert _apply(env)["unique_gtt_index"] == "skipped_duplicate_gtt_ids"
    assert _rollback(env)["action"] == "rolled_back"
    assert _schema(env) == before
    assert ticket_row(env.db, dup)["kite_gtt_id"] == 42


# ===================================================== 9: rollback keeps data

def test_9_apply_and_rollback_keep_every_existing_row(env):
    ids = _seed_application_data(env)
    before = _content(env)
    assert before["trade_tickets"] and before["ticket_events"] and before["reconciliation_log"]
    seq_before = {r["name"]: r["seq"] for r in before["sqlite_sequence"]}
    assert seq_before["ticket_events"] == ids["gone_event"]  # the deleted row's id is the high-water mark

    _apply(env)
    during = _content(env, drop_columns=("protection_state",))
    new_tables = {"protection_operations", "protection_operation_resolutions"}
    assert {t: rows for t, rows in during.items() if t not in new_tables} == before
    assert during["protection_operations"] == [] and during["protection_operation_resolutions"] == []
    assert all(r["protection_state"] is None for r in _rows(env, "SELECT protection_state FROM trade_tickets"))

    _rollback(env)
    assert _content(env) == before  # every table, every row, every value, sqlite_sequence included
    assert ticket_row(env.db, ids["paper"])["kite_gtt_id"] == "PAPER-G-7"
    # The next event id is still above the deleted one: no id is reused.
    new_event = _exec(env, "INSERT INTO ticket_events (ticket_id, event_type) VALUES (?, 'modified')", ids["buy"])
    assert new_event == ids["gone_event"] + 1


@pytest.mark.parametrize("data", ["journal_row", "protection_state", "resolution_row"]
                         + [f"event:{t}" for t in _EVENT_TYPES])
def test_9b_rollback_refuses_while_the_dark_schema_holds_data(env, data):
    _apply(env)
    pos = _position(env)
    if data == "journal_row":
        _insert_op(env, pos, state="not_sent")
    elif data == "protection_state":
        _exec(env, "UPDATE trade_tickets SET protection_state = 'PROTECTION_PENDING' WHERE id = ?", pos)
    elif data == "resolution_row":
        _resolve_manually(env, _insert_op(env, pos, kind="modify", state="needs_review"))
    else:
        _exec(env, "INSERT INTO ticket_events (ticket_id, event_type) VALUES (?, ?)", pos, data.split(":", 1)[1])
    schema, content, backups = _schema(env), _content(env), _backups(env)
    with pytest.raises(env.db.DarkSchemaRefused, match="holds data"):
        _rollback(env)
    assert _schema(env) == schema and _content(env) == content and _backups(env) == backups


def test_9c_rollback_refusal_counts_the_resolution_rows(env):
    _apply(env)
    op = _insert_op(env, _position(env), kind="cancel", state="needs_review")
    _resolve_manually(env, op, outcome="deleted_in_kite_verified")
    schema, content = _schema(env), _content(env)
    with pytest.raises(env.db.DarkSchemaRefused,
                       match=r"'protection_operations rows': 1, 'protection_operation_resolutions rows': 1"):
        _rollback(env)
    assert _schema(env) == schema and _content(env) == content


def test_9d_an_orphan_resolution_row_alone_still_blocks_the_rollback(env):
    _apply(env)
    conn = _conn(env)
    try:  # foreign keys off on this scratch connection only: a resolution whose op and read do not exist
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO protection_operation_resolutions (op_id, outcome, reason, evidence_read_id, "
                     "resolver, instance_epoch) VALUES (4242, 'no_app_gtt', 'orphan', 4343, 'operator', 1)")
        conn.commit()
    finally:
        conn.close()
    schema, content, backups = _schema(env), _content(env), _backups(env)
    with pytest.raises(env.db.DarkSchemaRefused,
                       match=r"'protection_operations rows': 0, 'protection_operation_resolutions rows': 1"):
        _rollback(env)
    assert _schema(env) == schema and _content(env) == content and _backups(env) == backups


# ======================================================= 10: failure handling

def _unchanged(env, schema, content):
    assert _schema(env) == schema
    assert _content(env) == content


def test_10_refuses_a_database_that_is_not_fully_migrated(env):
    _exec(env, "DELETE FROM schema_version WHERE version = 11")   # Phase 2: the latest is v11
    schema, content = _schema(env), _content(env)
    with pytest.raises(env.db.DarkSchemaRefused, match="schema version 10, not 11"):
        _apply(env)
    with pytest.raises(env.db.DarkSchemaRefused, match="schema version 10"):
        _rollback(env)
    _unchanged(env, schema, content)


def test_10b_refuses_a_database_from_newer_or_different_code(env):
    _exec(env, "INSERT INTO schema_version (version, migration_name) VALUES (12, 'from_the_future')")
    schema, content = _schema(env), _content(env)
    with pytest.raises(env.db.DatabaseSchemaUnsupported):
        _apply(env)
    _unchanged(env, schema, content)


@pytest.mark.parametrize("ddl", [
    "CREATE TABLE protection_operations (id INTEGER PRIMARY KEY)",                       # foreign form
    "CREATE VIEW protection_operations AS SELECT 1",                                      # another object type
    "CREATE INDEX idx_trade_tickets_unique_gtt ON trade_tickets(kite_gtt_id)",            # a stray Phase 1 name
    "ALTER TABLE trade_tickets ADD COLUMN protection_state TEXT",                         # half applied, foreign
    "CREATE TABLE protection_operation_resolutions (id INTEGER PRIMARY KEY)",             # foreign R6 table
    "CREATE INDEX idx_protection_ops_one_open_cancel ON trade_tickets(id)",               # a stray R6 index name
])
def test_10c_refuses_a_partial_or_foreign_form_of_the_objects(env, ddl):
    _exec(env, ddl)
    schema, content = _schema(env), _content(env)
    assert _status(env)["state"] in ("partial", "unexpected")
    with pytest.raises(env.db.DarkSchemaRefused, match="partial|unexpected"):
        _apply(env)
    with pytest.raises(env.db.DarkSchemaRefused, match="partial|unexpected"):
        _rollback(env)
    _unchanged(env, schema, content)


def test_10d_refuses_an_unexpected_ticket_events_shape(env):
    _exec(env, "ALTER TABLE ticket_events ADD COLUMN extra TEXT")
    schema, content = _schema(env), _content(env)
    with pytest.raises(env.db.DarkSchemaRefused, match="unexpected columns"):
        _apply(env)
    _unchanged(env, schema, content)


@pytest.mark.parametrize("exc", [RuntimeError("disk on fire"), KeyboardInterrupt()])
def test_10e_a_failure_mid_apply_rolls_everything_back(env, monkeypatch, exc):
    _seed_application_data(env)
    schema, content = _schema(env), _content(env)

    real = env.db._ensure_unique_gtt_index

    def fail(conn):  # runs after both tables, both indexes, the column and the ticket_events rebuild
        raise exc
    monkeypatch.setattr(env.db, "_ensure_unique_gtt_index", fail)
    with pytest.raises(type(exc)):
        _apply(env)
    _unchanged(env, schema, content)
    assert _status(env)["state"] == "absent"
    # Restored by hand: monkeypatch.undo() would also undo env's DB_PATH redirect.
    monkeypatch.setattr(env.db, "_ensure_unique_gtt_index", real)
    assert _apply(env)["action"] == "applied"  # and it still applies cleanly afterwards


def test_10f_a_failure_mid_rollback_leaves_the_schema_in_place(env, monkeypatch):
    _apply(env)
    schema, content = _schema(env), _content(env)
    real = env.db._rebuild_ticket_events

    def fail(conn, sql):  # after the drops and the DROP COLUMN
        real(conn, sql)
        raise RuntimeError("crash after the rebuild")
    monkeypatch.setattr(env.db, "_rebuild_ticket_events", fail)
    with pytest.raises(RuntimeError, match="crash after the rebuild"):
        _rollback(env)
    _unchanged(env, schema, content)
    assert _status(env)["state"] == "present"


def test_10g_refuses_a_connection_with_an_open_transaction(env):
    conn = _conn(env)
    try:
        conn.execute("UPDATE accounts SET id = id")  # opens the implicit transaction
        assert conn.in_transaction
        with pytest.raises(env.db.DarkSchemaRefused, match="no open transaction"):
            env.db.apply_dark_protection_schema(conn)
        conn.rollback()
        env.db.apply_dark_protection_schema(conn)
        conn.execute("UPDATE accounts SET id = id")
        with pytest.raises(env.db.DarkSchemaRefused, match="no open transaction"):
            env.db.rollback_dark_protection_schema(conn)
        conn.rollback()
    finally:
        conn.close()
    assert _status(env)["state"] == "present"
    assert len(_backups(env)) == 1  # only the successful apply took one


def test_10h_duplicate_gtt_ids_skip_only_the_unique_index_and_are_reported(env, caplog):
    a = _position(env, kite_gtt_id=77)
    b = _position(env, kite_gtt_id=77)
    _position(env, mode="dry_run", kite_gtt_id="PAPER-G-9")
    _position(env, mode="dry_run", kite_gtt_id="PAPER-G-9")
    with caplog.at_level(logging.WARNING, logger="backend.db"):
        result = _apply(env)
    assert result["action"] == "applied"
    assert result["unique_gtt_index"] == "skipped_duplicate_gtt_ids"
    assert result["duplicate_gtt_ids"] == [77, "PAPER-G-9"]
    assert "Not creating idx_trade_tickets_unique_gtt" in caplog.text
    status = _status(env)
    assert status["state"] == "present" and status["unique_gtt_index"] is False
    # Nothing was written to any finding table, and both tickets keep their ids.
    assert _rows(env, "SELECT COUNT(*) AS n FROM reconciliation_log") == [{"n": 0}]
    assert (ticket_row(env.db, a)["kite_gtt_id"], ticket_row(env.db, b)["kite_gtt_id"]) == (77, 77)

    again = _apply(env)  # still duplicated: still skipped, nothing else changes
    assert again["unique_gtt_index"] == "skipped_duplicate_gtt_ids" and again["action"] == "already_present"

    _exec(env, "UPDATE trade_tickets SET kite_gtt_id = NULL WHERE id = ?", b)
    _exec(env, "UPDATE trade_tickets SET kite_gtt_id = NULL WHERE kite_gtt_id = 'PAPER-G-9' AND id = "
               "(SELECT MAX(id) FROM trade_tickets WHERE kite_gtt_id = 'PAPER-G-9')")
    assert _apply(env)["unique_gtt_index"] == "created"
    assert _status(env)["unique_gtt_index"] is True


def test_10i_foreign_key_settings_and_isolation_level_are_restored(env):
    conn = _conn(env)
    try:
        isolation = conn.isolation_level
        env.db.apply_dark_protection_schema(conn)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1 and conn.isolation_level == isolation
        conn.execute("PRAGMA foreign_keys=OFF")
        env.db.rollback_dark_protection_schema(conn)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0 and conn.isolation_level == isolation
    finally:
        conn.close()


# The Phase 1 dark schema exactly as the pre-S1 code created it (db.py before
# Phase 3 S1): the 10-state table, the single open-op index, the 6-type suffix.
_PHASE1_STATES = ("'intent', 'sending', 'acknowledged', 'confirmed', 'rejected', 'unknown', 'adopted', "
                  "'abandoned', 'superseded', 'needs_review'")
_PHASE1_OPERATIONS_DDL = f"""CREATE TABLE protection_operations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    op_uuid               TEXT NOT NULL UNIQUE,
    account_id            INTEGER NOT NULL REFERENCES accounts(id),
    position_id           INTEGER NOT NULL REFERENCES trade_tickets(id),
    mode                  TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    kind                  TEXT NOT NULL CHECK (kind IN ('create', 'modify', 'cancel')),
    reason                TEXT NOT NULL,
    state                 TEXT NOT NULL CHECK (state IN ({_PHASE1_STATES})),
    basis_filled_quantity INTEGER NOT NULL CHECK (basis_filled_quantity >= 0),
    basis_exits_filled    INTEGER NOT NULL CHECK (basis_exits_filled >= 0),
    requested_quantity    INTEGER NOT NULL CHECK (requested_quantity >= 0),
    request_fingerprint   TEXT NOT NULL CHECK (json_valid(request_fingerprint)),
    request_payload       TEXT NOT NULL CHECK (json_valid(request_payload)),
    target_gtt_id         INTEGER,
    result_gtt_id         INTEGER,
    instance_epoch        INTEGER NOT NULL,
    attempts              INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    complete_reads_after  INTEGER NOT NULL DEFAULT 0 CHECK (complete_reads_after >= 0),
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at               TEXT,
    acknowledged_at       TEXT,
    resolved_at           TEXT,
    last_checked_at       TEXT,
    evidence              TEXT CHECK (evidence IS NULL OR json_valid(evidence)),
    CHECK (kind != 'create' OR attempts <= 1),
    CHECK (kind = 'create' OR target_gtt_id IS NOT NULL),
    CHECK (state != 'intent' OR sent_at IS NULL),
    CHECK (state NOT IN ('sending', 'acknowledged', 'confirmed', 'rejected', 'unknown', 'adopted') OR sent_at IS NOT NULL),
    CHECK (kind != 'create' OR state NOT IN ('acknowledged', 'confirmed', 'adopted') OR result_gtt_id IS NOT NULL)
)"""
_PHASE1_ONE_OPEN_DDL = ("CREATE UNIQUE INDEX idx_protection_ops_one_open ON protection_operations(position_id) "
                        "WHERE state IN ('intent', 'sending', 'acknowledged', 'unknown', 'needs_review')")
_PHASE1_EVENT_SUFFIX = (", 'protection_pending', 'protection_submitted', 'protection_confirmed', "
                        "'protection_unknown', 'protection_adopted', 'protection_cancelled'")


def _build_phase1_dark_schema(env):
    conn = _conn(env)
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        events_sql = env.db._ticket_events_sql(conn)
        _, list_end = env.db._event_type_list_span(events_sql)
        conn.execute(_PHASE1_OPERATIONS_DDL)
        conn.execute(_PHASE1_ONE_OPEN_DDL)
        conn.execute(f"ALTER TABLE trade_tickets ADD COLUMN {env.db._PROTECTION_STATE_COLUMN_DDL}")
        env.db._rebuild_ticket_events(conn, events_sql[:list_end] + _PHASE1_EVENT_SUFFIX + events_sql[list_end:])
        conn.execute(env.db._UNIQUE_GTT_INDEX_DDL)
        conn.execute("COMMIT")
    finally:
        conn.close()


def test_10j_a_phase_1_era_dark_schema_is_unexpected_and_never_touched(env):
    """Compatibility (M-9): a scratch database that still carries the Phase 1
    dark schema is not upgraded in place, nor removed by the R6 rollback. The
    account database never carries it (test 12), so recovery is to discard
    the scratch database."""
    _seed_application_data(env)
    _build_phase1_dark_schema(env)
    status = _status(env)
    assert status["state"] == "unexpected"
    assert status["mismatched"] == ["protection_operations", "ticket_events.protection_event_types"]
    assert status["objects"]["protection_operation_resolutions"] is False
    assert status["objects"]["idx_protection_ops_one_open_change"] is False
    schema, content, backups = _schema(env), _content(env), _backups(env)
    with pytest.raises(env.db.DarkSchemaRefused, match="unexpected"):
        _apply(env)
    with pytest.raises(env.db.DarkSchemaRefused, match="unexpected"):
        _rollback(env)
    assert _schema(env) == schema and _content(env) == content and _backups(env) == backups
    assert ("index", "idx_protection_ops_one_open") in schema  # still the Phase 1 object, untouched


# ===================================== 11: workflows leave the schema unused

_EVENT_NAMES = "|".join(t[len("protection_"):] for t in _EVENT_TYPES)
_DARK_NAMES = re.compile(
    r"protection_operations|protection_operation_resolutions|protection_state|idx_protection_ops_one_open|"
    r"idx_trade_tickets_unique_gtt|dark_protection_schema|_m012_|_r012_|PROTECTION_EVENT_TYPES|"
    r"PROTECTION_OPERATION_STATES|"
    rf"'protection_({_EVENT_NAMES})'|"
    rf"\"protection_({_EVENT_NAMES})\""
)


# Phase 3 (user decision B-D1, option A, 2026-11-11): the dark protection engine is the schema's only user. Its
# modules may name the schema; the S8 G3 journal-aware Stage 1 inventory may read it on exactly these lines (it is
# guarded by a table-exists check, so it reads nothing on a database without the journal). Every other backend
# module, and the frontend, must still leave the schema unused, and no other module may import the engine.
_DARK_ENGINE_MODULES = ("protection.py", "protection_evidence.py", "protection_send.py", "protection_runtime.py",
                        "protection_reconcile.py", "protection_exposure.py", "emergency_exit_engine.py")
_DARK_READ_ONLY_LINES = {
    "startup_reconciliation.py": {
        'inventory["open_protection_operations"] = rows(',
        '"SELECT id, position_id, kind, state FROM protection_operations WHERE account_id = ? AND state IN "',
        'if _table_exists(conn, "protection_operations") else []',
        'if inventory["unresolved_submissions"] or inventory.get("open_protection_operations"):',
    },
}


def test_11_no_application_module_refers_to_the_dark_schema():
    offenders = []
    for path in sorted((REPO / "backend").rglob("*.py")):
        if path.name == "db.py" or (path.parent.name == "backend" and path.name in _DARK_ENGINE_MODULES):
            continue
        allowed = _DARK_READ_ONLY_LINES.get(path.name, set()) if path.parent.name == "backend" else set()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _DARK_NAMES.search(line) and line.strip() not in allowed:
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    for path in sorted((REPO / "frontend").rglob("*.js")):
        if _DARK_NAMES.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == []
    # The allow-list is exact: each allowed line must still exist (a stale entry would hide nothing, but would
    # silently widen the rule if the code moved).
    for name, lines in _DARK_READ_ONLY_LINES.items():
        present = {l.strip() for l in (REPO / "backend" / name).read_text(encoding="utf-8").splitlines()}
        assert lines <= present, (name, sorted(lines - present))
    # No module outside the engine imports it: live workflows cannot reach the schema through it.
    engine = {n[:-3] for n in _DARK_ENGINE_MODULES}
    importers = []
    for path in sorted((REPO / "backend").rglob("*.py")):
        if path.parent.name == "backend" and path.name in _DARK_ENGINE_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[-1] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [a.name for a in node.names] + [(node.module or "").split(".")[-1]]
            if engine & set(names):
                importers.append(str(path.relative_to(REPO)))
    assert importers == []


def test_11b_inside_db_py_the_dark_functions_are_called_only_by_each_other():
    source = (REPO / "backend" / "db.py").read_text(encoding="utf-8")
    dark_start = source.index("# ---- GTT-after-fill Phase 1: the dark protection-journal schema")
    before_dark = source[:dark_start]
    # init_db, migrate and MIGRATIONS never reach it, except through migration 12's prepared late-bound wrapper
    # (docs/PHASE_3_V12_REGISTRATION_REMEDIATION_REPORT.md §3): exactly its one return line, nothing else.
    wrapper_line = "    return _m012_protection_journal(conn)"
    hits = [line for line in before_dark.splitlines() if _DARK_NAMES.search(line)]
    assert hits == [wrapper_line]
    tree = ast.parse(source)
    wrappers = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_migration_v12_protection_journal"]
    assert len(wrappers) == 1
    (body,) = wrappers[0].body  # one statement: return _m012_protection_journal(conn)
    assert isinstance(body, ast.Return) and isinstance(body.value, ast.Call)
    assert isinstance(body.value.func, ast.Name) and body.value.func.id == "_m012_protection_journal"
    assert [a.id for a in body.value.args] == ["conn"] and body.value.keywords == []
    assert source.index(wrapper_line) > source.index("def _migration_v12_protection_journal")
    assert "_m011_" not in source and "_r011_" not in source  # M-9: no v11-colliding dark names remain


def _assert_unused(env):
    assert _status(env)["state"] == "present"
    assert _rows(env, "SELECT COUNT(*) AS n FROM protection_operations") == [{"n": 0}]
    assert _rows(env, "SELECT COUNT(*) AS n FROM protection_operation_resolutions") == [{"n": 0}]
    assert _rows(env, "SELECT COUNT(*) AS n FROM trade_tickets WHERE protection_state IS NOT NULL") == [{"n": 0}]
    assert _rows(env, "SELECT COUNT(*) AS n FROM ticket_events WHERE event_type LIKE 'protection\\_%' ESCAPE '\\' "
                      "AND event_type != 'protection_failed'") == [{"n": 0}]
    assert _rows(env, "SELECT seq FROM sqlite_sequence WHERE name IN "
                      "('protection_operations', 'protection_operation_resolutions')") == []


def test_11c_a_live_lifecycle_runs_unchanged_and_never_touches_the_dark_schema(env, market_open):
    _apply(env)
    from backend import reconciliation, trailing_engine
    import asyncio

    # Entry through the real create -> confirm path (the frozen execution.py places the GTT, as today).
    set_mode(env.db, "live")
    env.kite.ltp_map["NSE:TCS"] = 300.0
    env.kite.auto_fill = False
    t = env.client.post("/api/tickets", json=_manual_ticket(quantity=100, entry_price=300.0, stop_loss=290.0,
                                                            target_1=330.0)).json()["data"]
    done = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"])).json()["data"]
    assert done["status"] == "executed" and done["kite_gtt_id"], done
    buy_id, order_id, gtt_id = t["id"], done["kite_order_id"], done["kite_gtt_id"]

    env.kite.set_order_state(order_id, "OPEN", filled_quantity=40, average_price=300.0)
    _monitor(env)                                                       # partial fill -> legacy modify_gtt
    env.kite.set_order_state(order_id, "COMPLETE", filled_quantity=100, average_price=300.0)
    _monitor(env)                                                       # complete -> legacy resize back up
    env.kite.set_holding("TCS", 100, average_price=300.0)
    asyncio.run(trailing_engine._tick_once())
    reconciliation._run_cycle()
    assert ticket_row(env.db, buy_id)["entry_fill_status"] == "filled"
    assert ticket_row(env.db, buy_id)["kite_gtt_id"] == gtt_id
    assert env.kite.calls_to("place_gtt") and env.kite.calls_to("modify_gtt")

    # A full exit, filled: the position closes and the legacy path deletes its GTT.
    sell_id = _insert_ticket(env.db, source="strategy", strategy_name="momentum_breakout_exit",
                             transaction_type="SELL", quantity=100, entry_order_type="MARKET", entry_price=310.0,
                             status="pending_confirmation", mode=None, closes_ticket_id=buy_id,
                             rule_checklist=json.dumps({"time_exit": {"pass": True}}))
    r = env.client.post(f"/api/tickets/{sell_id}/confirm")
    assert r.status_code == 200 and r.json()["data"]["status"] == "executed", r.text
    env.kite.set_order_state(r.json()["data"]["kite_order_id"], "COMPLETE", filled_quantity=100,
                             average_price=310.0)
    _monitor(env)
    assert ticket_row(env.db, buy_id)["closed_at"] is not None
    assert env.kite.calls_to("delete_gtt")

    _assert_unused(env)


def test_11d_dry_run_entry_and_emergency_exit_never_touch_the_dark_schema(env, market_open):
    _apply(env)
    set_mode(env.db, "dry_run")
    env.kite.ltp_map["NSE:TCS"] = 300.0
    t = env.client.post("/api/tickets", json=_manual_ticket(quantity=10, entry_price=300.0, stop_loss=290.0,
                                                            target_1=330.0)).json()["data"]
    done = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"])).json()["data"]
    assert done["status"] == "executed", done
    _monitor(env)

    # A live position too, so Emergency Exit has a broker-side GTT to work around.
    gtt_id = _protective_oco_gtt(env, quantity=5)
    _insert_ticket(env.db, source="strategy", strategy_name="momentum_breakout", kite_gtt_id=gtt_id, quantity=5,
                   executed_at="2026-09-01 04:00:00", entry_fill_status="filled", filled_price=3000.0,
                   filled_quantity=5, current_stop_loss=2900.0)
    env.kite.set_holding("TCS", 5, average_price=3000.0)
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    assert r.status_code == 200, r.text
    _assert_unused(env)


# ======================================================== 12: no account data

def test_12_the_dark_functions_refuse_the_account_database_and_its_backups(env, monkeypatch, tmp_path):
    # A stand-in for backend/kite_terminal.db: a real, fully migrated database
    # in tmp_path, made the "account database" for this test only.
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    # The databases below are created by the build the dark entry points belong to (Phase 2: exactly v11), on
    # either build; on a v12 database they refuse on the history first (test_v12_registration_prep RP01).
    schema_builds.phase2_build_patched(monkeypatch, env.db)
    monkeypatch.setattr(env.db, "_ACCOUNT_DB_FILE", account_dir / "kite_terminal.db")
    for name in ("kite_terminal.db", "kite_terminal.db.pre_schema_v10_bak", "kite_terminal.db.bak.20261107"):
        monkeypatch.setattr(env.db, "DB_PATH", account_dir / name)
        env.db.init_db()
        conn = env.db.get_connection()
        try:
            schema = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
            with pytest.raises(env.db.DarkSchemaRefused, match="account database"):
                env.db.apply_dark_protection_schema(conn)
            with pytest.raises(env.db.DarkSchemaRefused, match="account database"):
                env.db.rollback_dark_protection_schema(conn)
            assert conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
        finally:
            conn.close()
    assert not list(account_dir.glob("*pre_dark_protection*"))
    # A neighbour with a different name is just a scratch database.
    monkeypatch.setattr(env.db, "DB_PATH", account_dir / "scratch.db")
    env.db.init_db()
    conn = env.db.get_connection()
    try:
        assert env.db.apply_dark_protection_schema(conn)["action"] == "applied"
    finally:
        conn.close()


def test_12b_the_real_account_database_path_is_what_the_refusal_guards():
    from backend import db
    assert db._ACCOUNT_DB_FILE == REPO / "backend" / "kite_terminal.db"


def test_12c_apply_and_rollback_open_only_files_under_tmp_path(env):
    _seed_application_data(env)
    with real_data_guard.recording() as seen:
        _apply(env)
        _rollback(env)
    connects = [path for event, path in seen if event == "sqlite3.connect"]
    assert connects, "expected the scratch database and its backups to be opened"
    root = str(env.tmp_path.resolve()).lower()
    assert all(str(Path(p).resolve()).lower().startswith(root) for p in connects), connects
    assert not any("kite_terminal.db" in str(p) for _, p in seen)
    assert real_data_guard.unexpected_refusals() == []


# ======================================================= 13: kind / reason / direction

def test_13_kind_reason_direction_matrix(env):
    """DD-3 / DD-4: of the 3 x 12 x 5 combinations exactly 15 insert."""
    _apply(env)
    pos = _position(env)
    allowed = {("create", "fill", None), ("create", "user_recreate", None),
               ("modify", "fill_resize", "up"), ("modify", "exit_fill", "down"), ("modify", "trailing", "tighten"),
               ("modify", "attribution_cap", "down")}
    allowed |= {("modify", "user_modify", d) for d in _DIRECTIONS}
    allowed |= {("cancel", r, None) for r in _REASONS["cancel"]}
    assert len(allowed) == 15
    reasons = [r for kind in ("create", "modify", "cancel") for r in _REASONS[kind]]
    accepted, combos = set(), 0
    for kind in ("create", "modify", "cancel"):
        for reason in reasons:
            for direction in (None, *_DIRECTIONS):
                combos += 1
                try:  # not_sent: terminal, so the open indexes never interfere
                    _insert_op(env, pos, kind=kind, reason=reason, direction=direction, state="not_sent")
                except sqlite3.IntegrityError as exc:
                    assert "CHECK" in str(exc), (kind, reason, direction, exc)
                else:
                    accepted.add((kind, reason, direction))
    assert combos == 180 and accepted == allowed


@pytest.mark.parametrize("reason,direction", [
    ("fill_resize", "down"), ("fill_resize", "tighten"), ("exit_fill", "up"), ("exit_fill", "tighten"),
    ("attribution_cap", "tighten"), ("attribution_cap", "up"), ("trailing", "loosen"), ("trailing", "down"),
])
def test_13b_reasons_with_a_fixed_direction_refuse_any_other(env, reason, direction):
    _apply(env)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_op(env, _position(env), kind="modify", reason=reason, direction=direction)


# =========================================================== 14: retry bound

@pytest.mark.parametrize("kind,reason,direction,allowed", [
    ("create", "fill", None, False),
    ("create", "user_recreate", None, False),
    ("modify", "fill_resize", "up", False),
    ("modify", "user_modify", "up", False),
    ("modify", "user_modify", "loosen", False),
    ("modify", "exit_fill", "down", True),
    ("modify", "attribution_cap", "down", True),
    ("modify", "trailing", "tighten", True),
    ("modify", "user_modify", "tighten", True),
    ("cancel", "position_closed", None, True),
    ("cancel", "user_cancel", None, True),
])
def test_14_only_cancels_and_exposure_reducing_modifies_may_be_sent_twice(env, kind, reason, direction, allowed):
    _apply(env)
    pos = _position(env)
    fields = dict(kind=kind, reason=reason, direction=direction, state="rejected")
    _insert_op(env, pos, attempts=1, **fields)  # one send is always possible
    if allowed:
        _insert_op(env, pos, attempts=2, **fields)
        _insert_op(env, pos, attempts=10, **fields)
    else:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert_op(env, pos, attempts=2, **fields)


# =========================================== 15: sent_at and absent_after_settle

@pytest.mark.parametrize("state", _SENT)
def test_15_every_sent_state_carries_its_send_time(env, state):
    _apply(env)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_op(env, _position(env), state=state, sent_at=None)


@pytest.mark.parametrize("kind", ["create", "modify", "cancel"])
def test_15b_not_sent_has_sent_nothing(env, kind):
    _apply(env)
    pos = _position(env)
    _insert_op(env, pos, kind=kind, state="not_sent")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_op(env, pos, kind=kind, state="not_sent", sent_at=_SENT_AT)


@pytest.mark.parametrize("overrides,why", [
    (dict(kind="modify"), "a modify target is known: missing is needs_review (M-8)"),
    (dict(kind="cancel"), "a cancel target is known: missing is needs_review (M-8)"),
    (dict(complete_reads_after=1), "one complete read is not settle evidence"),
    (dict(complete_reads_after=0), "no read is not settle evidence"),
    (dict(sent_at=None), "an unsent create cannot be absent after settle"),
])
def test_15c_absent_after_settle_is_a_sent_create_with_two_complete_reads(env, overrides, why):
    _apply(env)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_op(env, _position(env), state="absent_after_settle", **overrides)


def test_15d_the_absent_after_settle_rules_hold_on_update_too(env):
    _apply(env)
    pos = _position(env)
    create = _insert_op(env, pos, kind="create", state="unknown")
    modify = _insert_op(env, _position(env), kind="modify", state="unknown")
    settle = ("UPDATE protection_operations SET state = 'absent_after_settle', complete_reads_after = ?, "
              "resolved_at = ? WHERE id = ?")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _exec(env, settle, 1, _RESOLVED_AT, create)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _exec(env, settle, 2, _RESOLVED_AT, modify)
    _exec(env, settle, 2, _RESOLVED_AT, create)
    _insert_op(env, pos, kind="create", state="absent_after_settle", complete_reads_after=3)
    # needs_review may or may not have been sent (an acknowledged contradiction, or a pre-send fault).
    _insert_op(env, _position(env), state="needs_review", sent_at=None)
    _insert_op(env, _position(env), state="needs_review", sent_at=_SENT_AT)


# ================================================================ 16: overlay

@pytest.mark.parametrize("keep", [
    ("superseded_at",), ("superseded_by_kind",), ("superseded_by_ref",),
    ("superseded_at", "superseded_by_kind"), ("superseded_at", "superseded_by_ref"),
    ("superseded_by_kind", "superseded_by_ref"),
])
def test_16_the_overlay_is_all_or_none(env, keep):
    _apply(env)
    partial = {k: v for k, v in _OVERLAY.items() if k in keep}
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_op(env, _position(env), state="sending", **partial)


def test_16b_an_intent_is_never_superseded_in_place(env):
    _apply(env)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_op(env, _position(env), state="intent", **_OVERLAY)
    op = _insert_op(env, _position(env), state="intent")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _exec(env, "UPDATE protection_operations SET superseded_at = ?, superseded_by_kind = ?, superseded_by_ref = ? "
                   "WHERE id = ?", *_OVERLAY.values(), op)
    # Q1 row 4: a superseded intent becomes not_sent, with the overlay.
    _exec(env, "UPDATE protection_operations SET state = 'not_sent', resolved_at = ?, superseded_at = ?, "
               "superseded_by_kind = ?, superseded_by_ref = ? WHERE id = ?", _RESOLVED_AT, *_OVERLAY.values(), op)


@pytest.mark.parametrize("state", [s for s in _STATES if s not in ("intent", "resolved_manual")])
def test_16c_the_overlay_is_accepted_on_every_other_state(env, state):
    _apply(env)
    op = _insert_op(env, _position(env), state=state, **_OVERLAY)  # the overlay never changes the state (row 19)
    assert _rows(env, "SELECT state, superseded_by_kind FROM protection_operations WHERE id = ?", op) == [
        {"state": state, "superseded_by_kind": "emergency_exit"}]


@pytest.mark.parametrize("by", ["emergency_exit", "exit", "close", "user"])
def test_16d_every_superseding_actor_is_accepted(env, by):
    _apply(env)
    _insert_op(env, _position(env), state="sending", **{**_OVERLAY, "superseded_by_kind": by})


# ==================================================== 17: manual resolution

def test_17_resolved_manual_and_resolution_id_go_together(env):
    _apply(env)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_op(env, _position(env), kind="modify", state="resolved_manual")  # no resolution_id
    op = _insert_op(env, _position(env), kind="modify", state="needs_review")
    resolution = _insert_resolution(env, op)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):  # a resolution_id outside resolved_manual
        _exec(env, "UPDATE protection_operations SET resolution_id = ? WHERE id = ?", resolution, op)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):  # a terminal state that is not resolved_manual
        _exec(env, "UPDATE protection_operations SET state = 'rejected', sent_at = ?, resolution_id = ?, "
                   "resolved_at = ? WHERE id = ?", _SENT_AT, resolution, _RESOLVED_AT, op)


@pytest.mark.parametrize("outcome", ["linked_gtt", "no_app_gtt", "deleted_in_kite_verified"])
def test_17b_the_full_resolution_sequence_succeeds_once(env, outcome):
    _apply(env)
    pos = _position(env)
    op = _insert_op(env, pos, kind="cancel", state="needs_review")
    resolution = _resolve_manually(env, op, outcome=outcome)
    row = _rows(env, "SELECT state, resolution_id, resolved_at FROM protection_operations WHERE id = ?", op)[0]
    assert row == {"state": "resolved_manual", "resolution_id": resolution, "resolved_at": _RESOLVED_AT}
    stored = _rows(env, "SELECT op_id, outcome, linked_gtt_id FROM protection_operation_resolutions")
    assert stored == [{"op_id": op, "outcome": outcome, "linked_gtt_id": 9003 if outcome == "linked_gtt" else None}]
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE.*op_id"):  # a second resolution of the same op
        _insert_resolution(env, op)
    # The position is free for a new change and a new cancel.
    _insert_op(env, pos, kind="cancel", state="intent")
    _insert_op(env, pos, kind="modify", state="intent")


@pytest.mark.parametrize("overrides,match", [
    (dict(outcome="linked_gtt", linked_gtt_id=None), "CHECK"),
    (dict(outcome="no_app_gtt", linked_gtt_id=9003), "CHECK"),
    (dict(outcome="deleted_in_kite_verified", linked_gtt_id=9003), "CHECK"),
    (dict(outcome="other"), "CHECK"),
    (dict(outcome="LINKED_GTT", linked_gtt_id=9003), "CHECK"),
    (dict(reason=""), "CHECK"),
    (dict(reason="   "), "CHECK"),
    (dict(resolver=""), "CHECK"),
    (dict(resolver="    "), "CHECK"),  # SQLite's one-argument trim() strips spaces only (R6 §4.1 as written)
    (dict(reason=None), "NOT NULL"),
    (dict(resolver=None), "NOT NULL"),
    (dict(outcome=None), "NOT NULL"),
    (dict(instance_epoch=None), "NOT NULL"),
    (dict(evidence_read_id=None), "NOT NULL"),
    (dict(evidence_read_id=987_654), "FOREIGN KEY"),
    (dict(op_id=987_654), "FOREIGN KEY"),
    (dict(op_id=None), "NOT NULL"),
])
def test_17c_a_resolution_without_its_evidence_or_with_inconsistent_fields_is_refused(env, overrides, match):
    _apply(env)
    op = _insert_op(env, _position(env), kind="modify", state="needs_review")
    with pytest.raises(sqlite3.IntegrityError, match=match):
        _insert_resolution(env, op, **overrides)
    assert _rows(env, "SELECT COUNT(*) AS n FROM protection_operation_resolutions") == [{"n": 0}]


def test_17d_the_allowed_resolution_variants_are_accepted(env):
    _apply(env)
    for outcome, linked in (("linked_gtt", 9003), ("linked_gtt", "PAPER-G-5"), ("no_app_gtt", None),
                            ("deleted_in_kite_verified", None)):
        op = _insert_op(env, _position(env), kind="modify", state="needs_review")
        _insert_resolution(env, op, outcome=outcome, linked_gtt_id=linked)
    assert _rows(env, "SELECT typeof(linked_gtt_id) AS t FROM protection_operation_resolutions "
                      "WHERE linked_gtt_id = 'PAPER-G-5'") == [{"t": "text"}]


# ===================================================== 18: open vs terminal

@pytest.mark.parametrize("state", _OPEN)
def test_18_an_open_op_has_no_resolved_at(env, state):
    _apply(env)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_op(env, _position(env), state=state, resolved_at=_RESOLVED_AT)


@pytest.mark.parametrize("state", _TERMINAL)
def test_18b_a_terminal_op_has_its_resolved_at(env, state):
    _apply(env)
    pos = _position(env)
    if state == "resolved_manual":
        op = _insert_op(env, pos, kind="modify", state="needs_review")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _resolve_manually(env, op, resolved_at=None)
        assert _rows(env, "SELECT state FROM protection_operations WHERE id = ?", op) == [{"state": "needs_review"}]
        return
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_op(env, pos, state=state, resolved_at=None)
    _insert_op(env, pos, state=state)  # with resolved_at


# ==================================================== 19: index partition

@pytest.mark.parametrize("change_kind", ["create", "modify"])
@pytest.mark.parametrize("state", _OPEN)
def test_19_an_open_cancel_coexists_with_an_open_change(env, change_kind, state):
    _apply(env)
    pos = _position(env)
    _insert_op(env, pos, kind=change_kind, state=state)
    _insert_op(env, pos, kind="cancel", state=state, target_gtt_id=9001)
    # and in the other order, on another position
    other = _position(env)
    _insert_op(env, other, kind="cancel", state=state, target_gtt_id=9001)
    _insert_op(env, other, kind=change_kind, state=state)


@pytest.mark.parametrize("first", _OPEN)
@pytest.mark.parametrize("second", _OPEN)
def test_19b_two_open_cancels_on_the_same_target_are_refused(env, first, second):
    _apply(env)
    pos = _position(env)
    _insert_op(env, pos, kind="cancel", state=first, target_gtt_id=9001)
    with pytest.raises(sqlite3.IntegrityError,
                       match=r"UNIQUE.*protection_operations\.position_id, protection_operations\.target_gtt_id"):
        _insert_op(env, pos, kind="cancel", reason="exit_prepare", state=second, target_gtt_id=9001)
    _insert_op(env, pos, kind="cancel", state=second, target_gtt_id=9005)              # another target
    _insert_op(env, _position(env), kind="cancel", state=second, target_gtt_id=9001)   # another position


def test_19c_open_cancels_on_different_targets_are_allowed_and_terminal_cancels_never_block(env):
    _apply(env)
    pos = _position(env)
    for target in (9001, 9002, 9003, "PAPER-G-4"):  # Emergency Exit cancels every known id at once (M-7)
        _insert_op(env, pos, kind="cancel", reason="exit_prepare", state="sending", target_gtt_id=target)
    for terminal in ("confirmed", "rejected", "not_sent"):
        for _ in range(2):
            _insert_op(env, pos, kind="cancel", state=terminal, target_gtt_id=9010)
    _insert_terminal_op(env, pos, "resolved_manual", kind="cancel", target_gtt_id=9010)
    open_cancel = _insert_op(env, pos, kind="cancel", state="unknown", target_gtt_id=9010)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_op(env, pos, kind="cancel", state="intent", target_gtt_id=9010)
    _exec(env, "UPDATE protection_operations SET state = 'confirmed', resolved_at = ? WHERE id = ?",
          _RESOLVED_AT, open_cancel)
    _insert_op(env, pos, kind="cancel", state="intent", target_gtt_id=9010)  # free again once it is terminal


def test_19d_the_change_index_ignores_cancels_and_the_cancel_index_ignores_changes(env):
    _apply(env)
    pos = _position(env)
    _insert_op(env, pos, kind="modify", state="unknown", target_gtt_id=9001)
    _insert_op(env, pos, kind="cancel", state="unknown", target_gtt_id=9001)  # same target as the open modify
    with pytest.raises(sqlite3.IntegrityError, match=r"protection_operations\.position_id$"):
        _insert_op(env, pos, kind="create", state="intent")                     # still one open change


def test_z5_the_live_entry_gate_constant_is_still_false():
    source = (REPO / "backend" / "live_entry_gate.py").read_text(encoding="utf-8")
    assert re.search(r"^LIVE_ENTRIES_ENABLED = False\b", source, re.MULTILINE)
