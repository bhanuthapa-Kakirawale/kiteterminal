"""Cross-repository round trip: Daily Market Byte's REAL exporter -> immutable bundle -> this
terminal's importer -> research inbox -> a user-initiated manual ticket.

DMB runs in its own interpreter (its venv), as a separate process, against a temporary output
root and the committed SYNTHETIC fixture - never its real stores. The terminal side is the
usual throwaway database + FakeKite. Skipped when the DMB checkout (sibling `daily_byte`, or
DMB_REPO) or its venv is not present.

Proves, end to end: the two independent canonicalisers agree on packet and bundle ids; only
symbol, exchange and rationale reach the ticket form; the P1-D review runs on the ticket; no
broker client is built and no broker write happens; DMB's files are never modified.
"""
import ast
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from conftest import set_mode
from test_baseline_api import _manual_ticket
from tests import dmb_bundles as B

ROOT = Path(__file__).resolve().parent.parent
DMB = Path(os.environ.get("DMB_REPO") or ROOT.parent / "daily_byte")
DMB_PY = DMB / ("venv/Scripts/python.exe" if sys.platform == "win32" else "venv/bin/python")
FIXTURE = DMB / "tests" / "fixtures" / "private_desk_handoff" / "synthetic_handoff_2026-11-02.json"
BROKER_WRITES = ("place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt")
needs_dmb = pytest.mark.skipif(not (DMB_PY.exists() and FIXTURE.exists()),
                               reason="the Daily Market Byte checkout or its venv is not present")

_EXPORT = """
import datetime as dt, json, sys
from private_desk.handoff.export import export_session
now = dt.datetime.fromisoformat(sys.argv[3])
r = export_session(dt.date.fromisoformat(sys.argv[1]), handoff_dir=sys.argv[2], fixture=sys.argv[4], now=now)
print(json.dumps(r.to_dict()))
sys.exit(r.exit_code)
"""


def _dmb(args, tmp_path, *, script=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KITE_", "DMB_RUN"))}
    env["DAILY_BYTE_OUT"] = str(tmp_path / "dmb_out")        # never DMB's real output root
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [str(DMB_PY), "-c", script, *args] if script else [str(DMB_PY), "-m", "private_desk.handoff", *args]
    return subprocess.run(cmd, cwd=DMB, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300)


def _snapshot(folder: Path) -> dict:
    return {str(p.relative_to(folder)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.fixture
def roundtrip(synthetic_profile, tmp_path, monkeypatch):
    env = synthetic_profile          # the isolated synthetic test profile (backend/integration_profile.py)
    from backend import dmb_inbox
    handoff = tmp_path / "dmb_handoff"
    monkeypatch.setenv(dmb_inbox.ENV_VAR, str(handoff))
    clock = {"now": B.IMPORT_AT}
    monkeypatch.setattr(dmb_inbox, "now_ist", lambda: clock["now"])
    env.handoff, env.clock = handoff, clock
    return env


@needs_dmb
def test_synthetic_round_trip_from_the_dmb_exporter_to_a_reviewed_manual_ticket(roundtrip, tmp_path, monkeypatch,
                                                                                market_open):
    env = roundtrip
    # 1-2. DMB's exporter writes the immutable bundle (generation time pinned: deterministic on any date)
    r = _dmb([B.SESSION, str(env.handoff), "2026-11-02T20:00:00+05:30", str(FIXTURE)], tmp_path, script=_EXPORT)
    assert r.returncode == 0, r.stdout + r.stderr
    export = json.loads(r.stdout.strip().splitlines()[-1])
    assert export["status"] == "CREATED" and export["data_origin"] == "SYNTHETIC" and export["packet_count"] == 2
    bundle_path = Path(export["path"])
    assert bundle_path.parent == env.handoff / B.SESSION and bundle_path.name == f"{export['bundle_id']}.json"
    dmb_before = _snapshot(env.handoff)

    # 3-5. the terminal imports it, on request, authenticated; nothing builds a Kite client
    from backend import kite_client

    def no_client(*a, **k):
        raise AssertionError("a Kite client was built during the DMB handoff")
    monkeypatch.setattr(kite_client, "create_kite_connect", no_client)
    real_get_kite = kite_client.get_kite
    monkeypatch.setattr(kite_client, "get_kite", no_client)
    calls_before = len(env.kite.calls)
    listing = env.client.get("/api/dmb-inbox/bundles").json()["data"]
    assert [(b["session_date"], b["bundle_id"]) for b in listing] == [(B.SESSION, export["bundle_id"])]
    imp = env.client.post("/api/dmb-inbox/imports", json={"session_date": B.SESSION, "bundle_id": export["bundle_id"]})
    assert imp.status_code == 201, imp.text
    assert imp.json()["data"]["status"] == "IMPORTED" and imp.json()["data"]["file_sha256"] == export["file_sha256"]
    items = {i["symbol"]: i for i in env.client.get("/api/dmb-inbox/items").json()["data"]}
    assert set(items) == {"INFY", "TCS"}
    infy = items["INFY"]
    exported = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert infy["packet_id"] == next(p["packet_id"] for p in exported["packets"] if p["symbol"] == "INFY")
    assert infy["synthetic"] and infy["rights"]["handoff_status"] == "PERMITTED"
    assert infy["producer"]["system"] == "daily-market-byte" and infy["source_artifact_ids"]
    assert infy["review_session_date"] == B.REVIEW and infy["actionable"]
    assert not items["TCS"]["actionable"] and "QUALITY_REPLAY_NOT_MATCHED" in items["TCS"]["reasons"]
    # importing again is idempotent
    again = env.client.post("/api/dmb-inbox/imports", json={"session_date": B.SESSION, "bundle_id": export["bundle_id"]})
    assert again.json()["data"]["status"] == "DUPLICATE"

    # 6. a note and a dismissal, recorded
    assert env.client.post(f"/api/dmb-inbox/items/{infy['id']}/notes", json={"text": "round trip"}).status_code == 201
    assert env.client.post(f"/api/dmb-inbox/items/{items['TCS']['id']}/dismiss", json={}).status_code == 200

    # 7-8. Start ticket during the review session: only symbol, exchange and rationale
    env.clock["now"] = B.DURING_REVIEW
    start = env.client.post(f"/api/dmb-inbox/items/{infy['id']}/start-ticket")
    assert start.status_code == 200, start.text
    s = start.json()["data"]
    assert set(s["prefill"]) == {"symbol", "exchange", "rationale"}
    assert s["prefill"]["symbol"] == "INFY" and s["prefill"]["exchange"] == "NSE"
    assert "SYNTHETIC" in s["prefill"]["rationale"] and "not a trade instruction" in s["prefill"]["rationale"]
    assert env.kite.calls[calls_before:] == []                  # the inbox made no broker call at all
    conn = env.db.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 0
    finally:
        conn.close()

    # 9. the user completes the existing manual form with synthetic values (normal POST /api/tickets)
    # (the ticket path reads the portfolio for its risk checks from FakeKite, as for any manual
    # ticket; a real KiteConnect stays impossible: create_kite_connect is still refused)
    monkeypatch.setattr(kite_client, "get_kite", real_get_kite)
    set_mode(env.db, "dry_run")
    body = _manual_ticket(symbol="INFY", quantity=2, entry_price=1500.0, stop_loss=1450.0, target_1=1650.0,
                          rationale=s["prefill"]["rationale"])
    created = env.client.post("/api/tickets", json=body)
    assert created.status_code == 201, created.text
    ticket = created.json()["data"]
    # 10. the P1-D review mechanism ran on it
    assert ticket["source"] == "manual" and ticket["review"]["subject"] and ticket["review"]["current"]
    assert ticket["review"]["token"]
    refused = env.client.post(f"/api/tickets/{ticket['id']}/confirm", json={"expected_mode": "dry_run"})
    assert refused.status_code == 409 and refused.json()["detail"]["error_code"] == "TICKET_REVIEW_CHANGED"
    link = env.client.post(f"/api/dmb-inbox/items/{infy['id']}/ticket-link",
                           json={"ticket_id": ticket["id"], "start_event_id": s["start_event_id"],
                                 "handoff_nonce": s["handoff_nonce"]})
    assert link.status_code == 201, link.text
    history = [e["event_type"] for e in link.json()["data"]["history"]]
    assert history == ["IMPORTED", "NOTE_ADDED", "TICKET_STARTED", "TICKET_LINKED"]

    # 11. no broker write anywhere, and DMB's handoff files are exactly as DMB wrote them
    assert [n for n, _ in env.kite.calls if n in BROKER_WRITES] == []
    assert _snapshot(env.handoff) == dmb_before


@needs_dmb
def test_the_dmb_cli_writes_a_bundle_the_terminal_accepts_or_refuses_as_stale(roundtrip, tmp_path):
    """The real CLI, real clock: what it writes either validates here, or - once the review
    session has completed - is refused as STALE_EVIDENCE. Never anything else. Since the
    packet-expiry rule (2026-11-04) DMB refuses an already-expired packet itself
    (NOT_EXPORTABLE / PACKET_EXPIRED) and writes no bundle at all."""
    from backend import dmb_inbox_contract as contract
    out = tmp_path / "private_out"
    r = _dmb(["export", "--session", B.SESSION, "--fixture", str(FIXTURE), "--out", str(out), "--json"], tmp_path)
    result = json.loads(r.stdout)
    if datetime.now(contract.trading_calendar.IST) >= contract.expires_at(date.fromisoformat(B.REVIEW)):
        assert r.returncode == 21 and result["status"] == "NOT_EXPORTABLE", r.stdout + r.stderr
        assert result["excluded"] == {"PACKET_EXPIRED": 2} and result["bundle_id"] is None
        assert not list(out.rglob("*.json"))
        return
    assert r.returncode == 0, r.stdout + r.stderr
    assert result["status"] == "CREATED"
    raw = Path(result["path"]).read_bytes()
    generated = datetime.fromisoformat(json.loads(raw)["generated_at"])
    try:
        bundle, _ = contract.parse_and_validate(raw, generated)
        assert bundle["bundle_id"] == result["bundle_id"]
    except contract.BundleRejected as e:
        assert e.code == "STALE_EVIDENCE", e
    second = _dmb(["export", "--session", B.SESSION, "--fixture", str(FIXTURE), "--out", str(out)], tmp_path)
    assert second.returncode == 0 and "ALREADY_EXISTS" in second.stdout


@needs_dmb
def test_real_dmb_evidence_never_produces_a_bundle(tmp_path):
    """Without the fixture DMB reads its stores; with an empty temporary output root there is
    nothing to export, and the rights gate would block real evidence anyway (DMB's own tests
    prove RIGHTS_BLOCKED on a synthetic store tree)."""
    r = _dmb(["export", "--session", B.SESSION, "--out", str(tmp_path / "h"), "--json"], tmp_path)
    assert r.returncode != 0
    assert json.loads(r.stdout)["status"] in ("SESSION_UNAVAILABLE", "RIGHTS_BLOCKED", "NO_CANDIDATES")
    assert not list((tmp_path / "h").rglob("*.json"))


@needs_dmb
def test_dmb_cannot_reach_the_terminal_and_the_terminal_cannot_write_dmb():
    banned = ("requests", "urllib", "http", "socket", "subprocess", "kiteconnect", "websocket", "httpx")
    for f in sorted((DMB / "private_desk" / "handoff").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module] if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 else [])
            for n in names:
                assert not any(n == b or n.startswith(b + ".") for b in banned), (f.name, n)
        assert "127.0.0.1:8000" not in src and "localhost:8000" not in src and "/api/" not in src, f.name
    import inspect
    from backend import dmb_inbox
    src = inspect.getsource(dmb_inbox)
    assert "O_WRONLY" not in src and "O_RDWR" not in src and "O_CREAT" not in src
