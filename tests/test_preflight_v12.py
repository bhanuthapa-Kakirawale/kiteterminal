"""The read-only v12 registration preflight (backend/db_maintenance.py preflight-v12; the R2-4 predicate approved
2026-11-11). Every database here is a pytest temporary database or a backup of one; the live database is never
named except as a refused file name inside tmp_path."""

import hashlib
import inspect
import re
import sqlite3

import pytest

from backend import db, db_maintenance, risk_engine
from protection_helpers import conn_of, make_position

# The preflight runs on a v11 operator backup (two tickets sharing a GTT id are possible only before v12's unique
# index): env's database is created at exactly v11 by the Phase 2 build on either build (tests/schema_builds.py).
pytestmark = pytest.mark.v11_database


def _backup(env, name="test.db.manual_20261111000000_bak"):
    conn = conn_of(env)
    try:
        return db.backup_database(conn, env.tmp_path / name)
    finally:
        conn.close()


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(capsys, *argv):
    code = db_maintenance.main(["preflight-v12", *argv])
    out = capsys.readouterr()
    return code, out.out + out.err


def _sql(env, sql, *args):
    conn = conn_of(env)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _null_mode(env, pid):
    _sql(env, "UPDATE trade_tickets SET mode = NULL WHERE id = ?", pid)


def test_PF01_a_clean_v11_backup_passes_and_is_left_byte_identical(env, capsys):
    make_position(env)
    backup = _backup(env)
    siblings = lambda: {p.name: _sha(p) for p in backup.parent.iterdir()  # noqa: E731
                        if p.is_file() and p.name.startswith(backup.name)}
    before = siblings()
    code, out = _run(capsys, "--db", str(backup))
    assert code == 0 and "result:" in out and "PASS" in out
    assert "DD-6 duplicate GTT ids:           0" in out and "R2-4 open executed BUY, NULL mode: 0" in out
    assert siblings() == before, (before, siblings())     # not a byte changed, and no -wal/-shm sidecar created


def test_PF02_duplicate_gtt_ids_fail_and_are_named(env, capsys):
    make_position(env, gtt_id=7001)
    make_position(env, symbol="INFY", token=408065, gtt_id=7001)
    code, out = _run(capsys, "--db", str(_backup(env)))
    assert code == 1 and "DD-6 duplicate GTT ids:           1  [7001]" in out and "FAIL" in out


def test_PF03_an_open_executed_buy_with_null_mode_fails_and_is_listed(env, capsys):
    pid = make_position(env)
    _null_mode(env, pid)
    code, out = _run(capsys, "--db", str(_backup(env)))
    assert code == 1 and "R2-4 open executed BUY, NULL mode: 1" in out and f"ticket {pid} " in out


def test_PF04_closed_live_sell_or_unexecuted_rows_do_not_count_and_the_diagnostic_never_gates(env, capsys):
    closed = make_position(env, symbol="A", token=1)
    _null_mode(env, closed)
    _sql(env, "UPDATE trade_tickets SET closed_at = datetime('now') WHERE id = ?", closed)
    by_exit = make_position(env, symbol="B", token=2, closed=True)          # an executed exit closes it...
    _sql(env, "UPDATE trade_tickets SET closed_at = NULL, mode = NULL WHERE id = ?", by_exit)   # ...closed_at unset
    _sql(env, "UPDATE trade_tickets SET entry_fill_status = NULL WHERE closes_ticket_id = ?", by_exit)
    make_position(env, symbol="C", token=3, mode="live")                    # live: not NULL mode
    confirmed = make_position(env, symbol="D", token=4)
    _sql(env, "UPDATE trade_tickets SET status = 'confirmed', mode = NULL WHERE id = ?", confirmed)
    code, out = _run(capsys, "--db", str(_backup(env)))
    assert code == 0, out
    assert "R2-4 open executed BUY, NULL mode: 0" in out
    assert f"review ticket {by_exit} " in out                               # shown to a human, not gating


def test_PF05_the_count_is_exactly_the_applications_open_position_definition_over_null_mode(env):
    """Drift guard: the approved predicate equals risk_engine._OPEN_POSITION_WHERE with mode IS NULL (all
    accounts), evaluated on the same rows."""
    for i, (mode, closed, status) in enumerate([(None, False, "executed"), (None, True, "executed"),
                                                ("live", False, "executed"), (None, False, "executed")]):
        pid = make_position(env, symbol=f"S{i}", token=100 + i, mode=mode or "live", closed=closed)
        if mode is None:
            _null_mode(env, pid)
    reference = re.sub(r"account_id = \? AND\s*", "", risk_engine._OPEN_POSITION_WHERE).replace(
        "mode = 'live'", "mode IS NULL")
    conn = conn_of(env)
    try:
        expected = [r[0] for r in conn.execute(f"SELECT id FROM trade_tickets WHERE {reference} ORDER BY id")]
        got = [r["id"] for r in conn.execute(db_maintenance._R2_4_OPEN_NULL_MODE_SQL)]
    finally:
        conn.close()
    assert got == expected and len(got) == 2


def test_PF06_a_database_that_is_not_v11_of_this_code_fails(env, capsys):
    backup = _backup(env)
    raw = sqlite3.connect(backup)
    try:
        raw.execute("INSERT INTO schema_version (version, migration_name, applied_at, duration_ms) "
                    "VALUES (12, 'phase3_protection_journal', datetime('now'), 0)")
        raw.commit()
    finally:
        raw.close()
    code, out = _run(capsys, "--db", str(backup))
    assert code == 1 and "schema v11 of this code:          NO" in out


def test_PF07_the_live_database_name_is_refused_before_any_filesystem_access(env, capsys, monkeypatch):
    touched = []
    real_exists = db_maintenance.Path.exists
    monkeypatch.setattr(db_maintenance.Path, "exists", lambda self: (touched.append(self), real_exists(self))[1])
    ghost = env.tmp_path / "nowhere" / "kite_terminal.db"                 # does not exist anywhere
    code, out = _run(capsys, "--db", str(ghost))
    assert code == 2 and "never on the live database" in out and touched == []
    decoy = env.tmp_path / "kite_terminal.db"
    decoy.write_bytes(b"not a database")
    code, out = _run(capsys, "--db", str(decoy))
    assert code == 2 and decoy.read_bytes() == b"not a database" and touched == []


def test_PF08_db_must_be_explicit_and_a_missing_file_is_refused(env, capsys):
    code, out = _run(capsys)
    assert code == 2 and "needs --db" in out
    code, out = _run(capsys, "--db", str(env.tmp_path / "missing.db"))
    assert code == 2 and "No database" in out


def test_PF09_the_preflight_connection_cannot_write(env):
    backup = _backup(env)
    conn = db_maintenance._connect_preflight(backup)
    try:
        conn.execute("PRAGMA query_only = ON")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE trade_tickets SET mode = 'live'")
    finally:
        conn.close()


def test_PF10_the_preflight_code_contains_no_write_statement():
    src = inspect.getsource(db_maintenance.preflight_v12) + inspect.getsource(db_maintenance.preflight_v12_counts) \
        + db_maintenance._R2_4_OPEN_NULL_MODE_SQL + db_maintenance._R2_4_DIAGNOSTIC_SQL
    for word in ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "REPLACE", "VACUUM", "ANALYZE", "commit("):
        assert word not in src, word
    assert "preflight-v12" not in db_maintenance.WRITING_COMMANDS          # no instance lock, no write connection


def test_PF11_other_commands_still_default_to_the_apps_database(env, capsys):
    assert db_maintenance.main(["status"]) == 0
    assert str(db.DB_PATH) in capsys.readouterr().out
