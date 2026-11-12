"""Phase 3 S9: static safety checks over the dark protection engine (no database, no broker)."""

import ast
import pathlib

from backend import db, kite_client, live_entry_gate, protection

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
ENGINE = sorted(p for p in BACKEND.glob("*.py") if p.name.startswith(("protection", "emergency_exit_engine")))
# The only functions allowed to make a broker WRITE call, each inside a held fenced transaction.
ALLOWED_WRITERS = {("protection_send.py", "_call"), ("emergency_exit_engine.py", "_sell")}


def _calls(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    yield fn.name, node.func.attr


def test_S9_ST1_broker_writes_only_from_the_fenced_send_paths():
    offenders = [(p.name, fn, attr) for p in ENGINE for fn, attr in _calls(p)
                 if attr in kite_client.BROKER_WRITE_METHODS and (p.name, fn) not in ALLOWED_WRITERS]
    assert offenders == []
    found = {(p.name, fn) for p in ENGINE for fn, attr in _calls(p) if attr in kite_client.BROKER_WRITE_METHODS}
    assert found == ALLOWED_WRITERS


def test_S9_ST2_the_engine_is_not_wired_into_any_live_module():
    engine_names = {p.stem for p in ENGINE}
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if path.stem in engine_names:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[-1] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [a.name for a in node.names] + [(node.module or "").split(".")[-1]]
            if engine_names & set(names):
                offenders.append(path.relative_to(BACKEND).as_posix())
    assert offenders == []


def test_S9_ST3_engine_code_never_touches_credentials_or_builds_an_sdk_client():
    forbidden = ("create_kite_connect", ".env", "KITE_API_SECRET", "access_token", "ui_prefs", "kite_terminal.db",
                 "dotenv", "requests.", "urllib")
    for path in ENGINE:
        text = path.read_text(encoding="utf-8")
        for word in forbidden:
            assert word not in text, (path.name, word)


def test_S9_ST4_every_transition_of_the_table_is_reachable_and_nothing_leaves_a_terminal_state():
    froms = {f for f, _ in protection.ALLOWED_TRANSITIONS}
    tos = {t for _, t in protection.ALLOWED_TRANSITIONS}
    assert not froms & set(protection.TERMINAL_STATES)
    assert set(protection.STATES) - {"intent"} <= tos               # every state except the claim is a target
    rows = {r for v in protection.ALLOWED_TRANSITIONS.values() for r in v}
    assert {"2", "3", "4", "5", "5a", "6", "7", "8", "10", "11", "12", "14", "15", "15a", "15b", "16", "17", "17c",
            "18", "22", "25"} <= rows


def test_S9_ST5_production_switches_unchanged():
    import schema_builds   # the one registration pin (tests/schema_builds.py)
    assert live_entry_gate.LIVE_ENTRIES_ENABLED is False
    assert db.MIGRATIONS[-1][0] == schema_builds.expected_latest()
    assert db.LATEST_SCHEMA_VERSION == schema_builds.expected_latest()


def test_S9_ST6_every_journal_write_function_requires_the_fence():
    tree = ast.parse((BACKEND / "protection.py").read_text(encoding="utf-8"))
    writers = []
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        src = ast.unparse(fn)
        if any(w in src for w in ("UPDATE protection_operations", "INSERT INTO protection_operations",
                                  "INSERT INTO protection_operation_resolutions", "UPDATE trade_tickets SET",
                                  "INSERT INTO ticket_events")):
            writers.append(fn.name)
            assert "_require_tx(conn)" in src or fn.name == "_event", fn.name
    assert {"transition", "annotate", "claim", "supersede_open_changes", "link_gtt", "resolve_manual"} <= set(writers)
