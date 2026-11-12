"""Phase 2 CLI audit (design §7.5, §13): every database-writing entry point takes the instance lock, or
operates read-only.

    db_maintenance analyze / vacuum / migrate / rollback-schema-v11 /
                   rollback-schema-v12 / restore-backup / migrate-v12 -> the instance lock (refuse if held)
    db_maintenance status / integrity / backup                        -> read-only connection (mode=ro)
    live_preflight payload-shape                                      -> reads only; no write authority, so
                                                                         the broker fence refuses any write
    nse_calendar_monitor / trading_calendar / smart_money_validation /
    volume_research                                                   -> no app database, or mode=ro&immutable=1
"""

import ast
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import release_write_authority

REPO = Path(__file__).resolve().parent.parent
CHILD = REPO / "tests" / "instance_lock_child.py"


def _digest(path) -> str:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return json.dumps({t: sorted(map(repr, conn.execute(f'SELECT * FROM "{t}"').fetchall())) for t in tables})
    finally:
        conn.close()


@pytest.fixture
def held_by_another_process(env):
    """The terminal running in another process: a child holds the database's instance lock."""
    release_write_authority()
    child = subprocess.Popen([sys.executable, str(CHILD), "hold", json.dumps({"db": str(env.db.DB_PATH)})],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "LOCKED"
    yield env
    child.stdin.close()
    child.wait(timeout=30)


@pytest.mark.parametrize("command", ["analyze", "vacuum", "migrate"])
def test_writing_commands_refuse_while_the_terminal_holds_the_lock(held_by_another_process, capsys, command):
    from backend import db_maintenance
    env = held_by_another_process
    before = _digest(env.db.DB_PATH)
    assert db_maintenance.main([command, "--db", str(env.db.DB_PATH)]) == 3
    assert "ANOTHER_INSTANCE_RUNNING" in capsys.readouterr().err
    assert _digest(env.db.DB_PATH) == before


@pytest.mark.parametrize("argv", [["rollback-schema-v11", "--confirm"], ["rollback-schema-v12", "--confirm"],
                                  ["restore-backup", "--backup", "elsewhere.db.manual_bak", "--confirm"]],
                         ids=["rollback-schema-v11", "rollback-schema-v12", "restore-backup"])
def test_rollback_refuses_while_the_terminal_holds_the_lock(held_by_another_process, capsys, argv):
    from backend import db_maintenance
    env = held_by_another_process
    before = _digest(env.db.DB_PATH)
    assert db_maintenance.main([*argv, "--db", str(env.db.DB_PATH)]) == 3
    assert "ANOTHER_INSTANCE_RUNNING" in capsys.readouterr().err
    assert _digest(env.db.DB_PATH) == before


def test_every_writing_command_is_in_the_lock_matrix():
    from backend import db_maintenance
    assert set(db_maintenance.WRITING_COMMANDS) == {"analyze", "vacuum", "migrate", "rollback-schema-v11",
                                                    "rollback-schema-v12", "restore-backup"}


@pytest.mark.parametrize("command", ["status", "integrity", "backup"])
def test_read_only_commands_work_while_the_terminal_runs_and_write_nothing(held_by_another_process, capsys, command):
    from backend import db_maintenance
    env = held_by_another_process
    before = _digest(env.db.DB_PATH)
    assert db_maintenance.main([command, "--db", str(env.db.DB_PATH)]) == 0
    assert _digest(env.db.DB_PATH) == before


def test_the_read_only_connection_cannot_write(env):
    from backend import db_maintenance
    conn = db_maintenance._connect_readonly(Path(env.db.DB_PATH))
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE app_instance SET heartbeat_seq = heartbeat_seq + 1")
    finally:
        conn.close()


@pytest.mark.parametrize("command", ["analyze", "vacuum"])
def test_writing_commands_run_once_the_lock_is_free_and_release_it(env, capsys, command):
    from backend import db_maintenance, instance_lock
    release_write_authority()
    assert db_maintenance.main([command, "--db", str(env.db.DB_PATH)]) == 0
    fd = instance_lock.try_lock_file(instance_lock.lock_path_for(env.db.DB_PATH))   # released afterwards
    assert fd is not None
    instance_lock.release_lock_file(fd)


def test_live_preflight_payload_shape_only_reads(env, monkeypatch, capsys):
    """Run without write authority (a CLI never takes over): it reads the broker through the read-only
    wrapper and writes nothing to the database; a broker write would be refused by the fence."""
    from backend import instance_lock, live_preflight
    release_write_authority()
    before = _digest(env.db.DB_PATH)
    mark = len(env.kite.calls)
    assert live_preflight._cli(["payload-shape"]) == 0
    assert _digest(env.db.DB_PATH) == before
    assert {name for name, _ in env.kite.calls[mark:]} <= set(live_preflight.READ_ONLY_METHODS) | {"ltp"}
    assert not instance_lock.authority_ok()
    with pytest.raises(instance_lock.InstanceNotOwner):
        env.kite.place_order(variety="regular", exchange="NSE", tradingsymbol="TCS", transaction_type="BUY",
                             quantity=1, product="CNC", order_type="MARKET")


@pytest.mark.parametrize("module", ["trading_calendar", "volume_research"])
def test_the_research_clis_open_databases_read_only(module):
    source = (REPO / "backend" / f"{module}.py").read_text(encoding="utf-8")
    connects = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") == "connect"]
    assert connects and all("mode=ro" in ast.get_source_segment(source, c) for c in connects)


@pytest.mark.parametrize("module", ["nse_calendar_monitor", "smart_money_validation"])
def test_the_other_clis_never_touch_the_app_database(module):
    source = (REPO / "backend" / f"{module}.py").read_text(encoding="utf-8")
    for forbidden in ("get_connection(", "init_db(", "sqlite3.connect(", "DB_PATH"):
        assert forbidden not in source, (module, forbidden)


def test_every_backend_entry_point_is_audited():
    """A new `__main__` in backend/ must be added to this audit (and to the Phase 2 report)."""
    audited = {"db_maintenance", "live_preflight", "nse_calendar_monitor", "smart_money_validation",
               "trading_calendar", "volume_research", "server"}
    found = {p.stem for p in (REPO / "backend").rglob("*.py") if '__name__ == "__main__"' in p.read_text(encoding="utf-8")}
    assert found == audited
