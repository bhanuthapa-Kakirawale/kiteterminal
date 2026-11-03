"""DMB -> Kite isolated synthetic acceptance test (docs/DMB_KITE_ACCEPTANCE_TEST.md).

One command, from the kite-terminal folder:

    .venv\\Scripts\\python.exe tests\\acceptance_dmb_kite.py            # run all 20 stages, then stop
    .venv\\Scripts\\python.exe tests\\acceptance_dmb_kite.py --keep-running   # leave the UI up afterwards
    .venv\\Scripts\\python.exe tests\\acceptance_dmb_kite.py --serve-only     # isolated UI for a manual walk-through
    .venv\\Scripts\\python.exe tests\\acceptance_dmb_kite.py --reset          # delete the scratch workdir only

What it touches - and nothing else:
- a scratch workdir (default %TEMP%\\dmb_kite_acceptance), recreated on every run, marked with a
  `.dmb_kite_acceptance` file; `--reset` deletes only a folder carrying that marker;
- Daily Market Byte's REAL exporter, run in DMB's own venv as a separate process, reading the
  committed SYNTHETIC fixture and writing ONLY into <workdir>\\handoff (DAILY_BYTE_OUT points at
  <workdir>\\dmb_out, never DMB's real output root);
- Kite Terminal's demo server (tests/demo_server.py) in the isolated SYNTHETIC TEST PROFILE:
  a throwaway database in <workdir>\\kite, FakeKite, the demo login, LIVE impossible.

It never opens the real account database or its backups (it only stat()s them, before and
after, to prove they did not change), never uses broker credentials, never contacts Zerodha and
never runs a DMB production job. The clocks are pinned, so it runs the same on any date:
the bundle is generated 2026-11-02 20:00 IST, imported 21:00 IST, and the ticket is started
during the review session, 2026-11-03 11:00 IST.

Exit code 0 only if every stage passed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DMB = Path(os.environ.get("DMB_REPO") or ROOT.parent / "daily_byte")
DMB_PY = DMB / ("venv/Scripts/python.exe" if sys.platform == "win32" else "venv/bin/python")
KITE_PY = ROOT / (".venv/Scripts/python.exe" if sys.platform == "win32" else ".venv/bin/python")
FIXTURE = DMB / "tests" / "fixtures" / "private_desk_handoff" / "synthetic_handoff_2026-11-02.json"
FIXTURE_SHA256 = "c6361bcb2276f1bd0345df25ad9d9bf7ab660ff4d066437046d856912a292776"
MARKER = ".dmb_kite_acceptance"
SESSION, REVIEW = "2026-11-02", "2026-11-03"
GENERATED_AT = "2026-11-02T20:00:00+05:30"
IMPORT_AT, DURING_REVIEW = "2026-11-02T21:00:00+05:30", "2026-11-03T11:00:00+05:30"
REAL_DB_DIR = ROOT / "backend"
BROKER_WRITES = ("place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt")

_EXPORT = """
import datetime as dt, json, sys
from private_desk.handoff.export import export_session
r = export_session(dt.date.fromisoformat(sys.argv[1]), handoff_dir=sys.argv[2], fixture=sys.argv[4],
                   now=dt.datetime.fromisoformat(sys.argv[3]))
print(json.dumps(r.to_dict()))
sys.exit(r.exit_code)
"""


class Run:
    def __init__(self):
        self.rows: list[tuple[str, str, bool, str]] = []

    def check(self, stage: str, name: str, ok, detail="") -> bool:
        ok = bool(ok)
        self.rows.append((stage, name, ok, "" if ok else str(detail)[:300]))
        print(f"  [{'PASS' if ok else 'FAIL'}] {stage:>3} {name}" + ("" if ok else f"  -- {str(detail)[:300]}"),
              flush=True)
        return ok

    @property
    def failed(self):
        return [r for r in self.rows if not r[2]]


# ---------------------------------------------------------------- isolation helpers
def _real_db_fingerprint() -> dict:
    """stat() only - size and mtime of the real database and every backup; never opened."""
    out = {}
    for entry in sorted(os.scandir(REAL_DB_DIR), key=lambda e: e.name):
        if entry.name.lower().startswith("kite_terminal.db"):
            st = entry.stat()
            out[entry.name] = (st.st_size, st.st_mtime_ns)
    return out


def _safe_workdir(path: Path) -> Path:
    """Never inside the real database folder, a repository, or DMB's output root."""
    path = path.resolve()
    for forbidden in (REAL_DB_DIR, ROOT, DMB):
        f = forbidden.resolve()
        if path == f or f in path.parents:
            raise SystemExit(f"Refusing workdir {path}: it is inside {f}. Use a scratch folder such as %TEMP%.")
    return path


def reset(workdir: Path) -> None:
    if workdir.exists():
        if not (workdir / MARKER).exists():
            raise SystemExit(f"Refusing to delete {workdir}: it has no {MARKER} marker (not an acceptance workdir).")
        def writable_then_retry(func, target, _exc):
            # DMB writes its bundles read-only (immutable); this is OUR scratch copy.
            os.chmod(target, stat.S_IWRITE)
            func(target)
        shutil.rmtree(workdir, onexc=writable_then_retry)
    print(f"Scratch workdir reset: {workdir}")


# ---------------------------------------------------------------- processes
def export_fixture(workdir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KITE_", "DMB_RUN"))}
    env["DAILY_BYTE_OUT"] = str(workdir / "dmb_out")
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([str(DMB_PY), "-c", _EXPORT, SESSION, str(workdir / "handoff"), GENERATED_AT, str(FIXTURE)],
                       cwd=DMB, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"DMB export failed ({r.returncode}): {r.stdout[-500:]} {r.stderr[-500:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def start_server(workdir: Path, port: int, keep_db: bool) -> subprocess.Popen:
    args = [str(KITE_PY), str(ROOT / "tests" / "demo_server.py"), "--port", str(port), "--workdir",
            str(workdir / "kite"), "--review-controls", "--paper-controls", "--dmb-synthetic-profile",
            "--dmb-handoff-dir", str(workdir / "handoff")]
    if keep_db:
        args.append("--keep-db")
    env = {k: v for k, v in os.environ.items() if not k.startswith("KITE_")}
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(workdir / f"kite_server_{'restart' if keep_db else 'first'}.log", "w", encoding="utf-8")
    proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    import httpx
    deadline = time.time() + 120
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"the Kite test server exited ({proc.returncode}); see {log.name}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/session/status", timeout=2).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError(f"the Kite test server did not answer on port {port}")


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()


def client(port: int):
    import httpx
    base = f"http://127.0.0.1:{port}"
    return httpx.Client(base_url=base, headers={"X-Kite-Terminal": "1", "Origin": base}, timeout=60)


TOKEN_HEADER = "X-Kite-Terminal-Token"


def login(c) -> None:
    """The test profile's stand-in for a Kite login: a real session replacement with a fake
    KiteConnect, then this launch's credential cookie (kt_test_session) and - P2-G R-F3 - the
    port-isolated terminal token, which a browser keeps in localStorage and this client sends as
    the X-Kite-Terminal-Token header."""
    r = c.get("/demo/kite-login")
    r.raise_for_status()
    c.headers[TOKEN_HEADER] = r.json()["data"]["terminal_token"]


def cookie(c, name: str = "kt_test_session") -> str | None:
    """The most recently set value of a cookie in this client's jar (httpx may hold one per domain key)."""
    values = [ck.value for ck in c.cookies.jar if ck.name == name]
    return values[-1] if values else None


def pin(c, ist: str) -> None:
    c.post("/api/demo/clock", json={"ist": ist}).raise_for_status()


# ---------------------------------------------------------------- the stages
def run(workdir: Path, port: int, keep_running: bool) -> int:
    from backend import dmb_inbox_contract as contract
    R = Run()
    before = _real_db_fingerprint()
    procs: list[subprocess.Popen] = []
    try:
        print(f"\nDMB -> Kite synthetic acceptance  (workdir {workdir}, UI http://127.0.0.1:{port})\n")
        # 1. isolated environment
        R.check("1", "DMB checkout, DMB venv and Kite venv present", DMB_PY.exists() and KITE_PY.exists(), DMB_PY)
        R.check("1", "approved synthetic fixture unchanged (SHA-256)",
                hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256, FIXTURE)

        # 2-3. export with DMB's real exporter, verify ids and hash
        export = export_fixture(workdir)
        bundle_path = Path(export["path"])
        raw = bundle_path.read_bytes()
        bundle = json.loads(raw)
        R.check("2", "DMB exported the SYNTHETIC fixture into the scratch handoff dir",
                export["status"] == "CREATED" and export["data_origin"] == "SYNTHETIC"
                and bundle_path.parent == (workdir / "handoff" / SESSION), export)
        R.check("3", "bundle id recomputed from its packet ids",
                contract.bundle_id_for(SESSION, [p["packet_id"] for p in bundle["packets"]]) == export["bundle_id"])
        R.check("3", "every packet id recomputed from its content",
                all(contract.packet_id_for(p) == p["packet_id"] for p in bundle["packets"]))
        R.check("3", "file SHA-256 matches the export report", hashlib.sha256(raw).hexdigest() == export["file_sha256"])
        dmb_files_before = {p: p.read_bytes() for p in (workdir / "handoff").rglob("*") if p.is_file()}

        proc = start_server(workdir, port, keep_db=False)
        procs.append(proc)
        c = client(port)
        r = c.post("/api/demo/clock", json={"ist": IMPORT_AT})
        R.check("1", "writes refused before the test login (401 TERMINAL_SESSION_REQUIRED)",
                r.status_code == 401 and r.json()["detail"]["error_code"] == "TERMINAL_SESSION_REQUIRED", r.text)
        login(c)
        pin(c, IMPORT_AT)
        strategy_before = c.get("/api/watchlist").json()["data"]

        # 4-6. import, provenance, idempotency
        listing = c.get("/api/dmb-inbox/bundles").json()
        R.check("4", "the bundle is listed for import (nothing auto-imported)",
                [(b["session_date"], b["bundle_id"], b["imported"]) for b in listing["data"]]
                == [(SESSION, export["bundle_id"], False)], listing)
        imp = c.post("/api/dmb-inbox/imports", json={"session_date": SESSION, "bundle_id": export["bundle_id"]})
        R.check("4", "import accepted (IMPORTED) with the exported file hash",
                imp.status_code == 201 and imp.json()["data"]["status"] == "IMPORTED"
                and imp.json()["data"]["file_sha256"] == export["file_sha256"], imp.text)
        items = {i["symbol"]: i for i in c.get("/api/dmb-inbox/items").json()["data"]}
        infy, tcs = items.get("INFY", {}), items.get("TCS", {})
        R.check("5", "INFY and TCS imported", set(items) == {"INFY", "TCS"}, list(items))
        R.check("5", "provenance: producer, artifact ids, PERMITTED synthetic rights, review session",
                infy.get("producer", {}).get("system") == "daily-market-byte" and infy.get("source_artifact_ids")
                and infy.get("synthetic") and infy.get("rights", {}).get("handoff_status") == "PERMITTED"
                and infy.get("review_session_date") == REVIEW, infy)
        R.check("5", "runs in the SYNTHETIC_TEST integration profile",
                infy.get("integration_profile") == "SYNTHETIC_TEST", infy.get("integration_profile"))
        R.check("5", "INFY actionable; TCS research only (replay not matched)",
                infy.get("actionable") and not tcs.get("actionable")
                and "QUALITY_REPLAY_NOT_MATCHED" in tcs.get("reasons", []), (infy.get("reasons"), tcs.get("reasons")))
        again = c.post("/api/dmb-inbox/imports", json={"session_date": SESSION, "bundle_id": export["bundle_id"]})
        count = len(c.get("/api/dmb-inbox/items", params={"include_superseded": True}).json()["data"])
        R.check("6", "duplicate import is idempotent (DUPLICATE, still 2 items)",
                again.json()["data"]["status"] == "DUPLICATE" and count == 2, again.text)

        # 7-10. note, dismiss, research watchlist, strategy universe unchanged
        R.check("7", "note added", c.post(f"/api/dmb-inbox/items/{infy['id']}/notes",
                                          json={"text": "acceptance note"}).status_code == 201)
        R.check("8", "TCS dismissed", c.post(f"/api/dmb-inbox/items/{tcs['id']}/dismiss", json={}).status_code == 200)
        w = c.post(f"/api/dmb-inbox/items/{infy['id']}/research-watchlist", json={"watched": True})
        R.check("9", "INFY on the separate research watchlist",
                w.status_code == 200 and w.json()["data"]["on_research_watchlist"], w.text)
        R.check("10", "strategy scan universe (watchlist table) unchanged",
                c.get("/api/watchlist").json()["data"] == strategy_before)

        # 11-13. Start ticket: only symbol, exchange, rationale; the user types the rest
        pin(c, DURING_REVIEW)
        start = c.post(f"/api/dmb-inbox/items/{infy['id']}/start-ticket")
        s = start.json().get("data", {})
        R.check("11", "Start ticket accepted during the review session", start.status_code == 200, start.text)
        R.check("12", "prefill is exactly symbol, exchange, rationale",
                set(s.get("prefill", {})) == {"symbol", "exchange", "rationale"}
                and s["prefill"]["symbol"] == "INFY" and s["prefill"]["exchange"] == "NSE"
                and s["prefill"]["rationale"].startswith("[SYNTHETIC TEST PACKET]"), s)
        # Synthetic market for the manual ticket: FakeKite's INFY last price set to 1180 (demo control).
        c.post("/api/demo/paper/ltp", json={"symbol": "INFY", "price": 1180.0}).raise_for_status()
        # The demo portfolio is ~64% IT by design and both fixture packets are IT stocks: on THIS
        # scratch instance only, the sector limit is raised through the normal settings API (every
        # other limit, and every account-safety limit, is sent back unchanged or left out).
        current = c.get("/api/risk/settings").json()["data"]
        keep = ("max_risk_per_trade_pct", "max_capital_per_trade_pct", "max_open_positions",
                "max_single_stock_exposure_pct", "max_total_portfolio_risk_pct", "min_risk_reward",
                "starting_capital")
        lifted = c.post("/api/risk/settings", json={**{k: current[k] for k in keep}, "max_sector_exposure_pct": 100.0})
        R.check("13", "scratch instance: sector-exposure limit raised for the IT-heavy demo portfolio (only change)",
                lifted.status_code == 200 and all(lifted.json()["data"][k] == current[k] for k in keep), lifted.text)
        bare = c.post("/api/tickets", json={"symbol": "INFY", "exchange": "NSE", "rationale": s["prefill"]["rationale"]})
        R.check("13", "a ticket without side/quantity/entry/stop/target is refused (manual input required)",
                bare.status_code == 422, bare.status_code)
        body = {"symbol": "INFY", "exchange": "NSE", "transaction_type": "BUY", "quantity": 2, "entry_price": 1180.0,
                "entry_order_type": "LIMIT", "stop_loss": 1150.0, "target_1": 1250.0, "product_type": "CNC",
                "use_gtt": True, "trailing_enabled": False, "rationale": s["prefill"]["rationale"]}
        created = c.post("/api/tickets", json=body)
        t = created.json().get("data", {})
        failed_checks = [(x.get("rule"), x.get("message")) for x in (t.get("risk_check_results") or [])
                         if isinstance(x, dict) and not x.get("passed") and not x.get("skipped")]
        R.check("13", "manual ticket created with user-entered synthetic values, awaiting approval",
                created.status_code == 201 and t.get("source") == "manual" and t.get("quantity") == 2
                and t.get("status") == "pending_confirmation", f"{t.get('status')} {failed_checks}")

        # 14. P1-D review and token binding
        review = t.get("review", {})
        R.check("14", "P1-D review stamped and current, with a token",
                review.get("subject") and review.get("current") and review.get("token"), review)
        stale = c.post(f"/api/tickets/{t['id']}/confirm", json={"expected_mode": "dry_run"})
        R.check("14", "approval without the shown review token refused (TICKET_REVIEW_CHANGED)",
                stale.status_code == 409 and stale.json()["detail"]["error_code"] == "TICKET_REVIEW_CHANGED", stale.text)
        link = c.post(f"/api/dmb-inbox/items/{infy['id']}/ticket-link",
                      json={"ticket_id": t["id"], "start_event_id": s["start_event_id"],
                            "handoff_nonce": s["handoff_nonce"]})
        R.check("14", "ticket linked to the inbox item", link.status_code == 201, link.text)
        live = c.post("/api/execution/mode", json={"mode": "live", "confirm_real_money": True})
        R.check("14", "LIVE cannot be armed in the synthetic test profile",
                live.status_code == 409 and "SYNTHETIC_TEST_PROFILE" in live.json()["detail"].get("blocking_codes", []),
                live.text)
        ok = c.post(f"/api/tickets/{t['id']}/confirm", json={"expected_mode": "dry_run",
                                                              "expected_review_token": review.get("token")})
        R.check("14", "approval with the shown token accepted (dry run, paper broker)",
                ok.status_code == 200 and ok.json()["data"]["mode"] == "dry_run", ok.text)

        # 15. positions list: acceptance vs fill, through partial to full
        def progress():
            pos = c.get("/api/app-positions", params={"view": "active"}).json()["data"]
            p = next((x for x in pos if x["position_id"] == t["id"]), None)
            return p and (p["entry_progress"]["acceptance"], p["entry_progress"]["fill"],
                          p["entry_progress"]["filled_quantity"], p["entry_fill_price"])
        R.check("15", "accepted, not filled", (progress() or ())[:3] == ("ACCEPTED", "AWAITING_FILL", 0), progress())
        order_id = ok.json()["data"]["kite_order_id"]
        c.post("/api/demo/paper/fill-plan", json={"order_id": order_id, "plan": [1, 2]}).raise_for_status()
        c.post("/api/demo/paper/ltp", json={"symbol": "INFY", "price": 1179.0}).raise_for_status()   # crosses the LIMIT
        c.post("/api/demo/paper/cycle").raise_for_status()
        R.check("15", "partially filled 1/2", (progress() or ())[:3] == ("ACCEPTED", "PARTIAL", 1), progress())
        c.post("/api/demo/paper/cycle").raise_for_status()
        R.check("15", "fully filled 2/2 with an average price",
                (progress() or ())[:3] == ("ACCEPTED", "FILLED", 2) and progress()[3] is not None, progress())

        # 16. post-claim tampering audit
        t2 = c.post("/api/tickets", json={**body, "quantity": 1}).json()["data"]
        c.post(f"/api/demo/tamper-after-claim/{t2['id']}").raise_for_status()
        tampered = c.post(f"/api/tickets/{t2['id']}/confirm",
                          json={"expected_mode": "dry_run", "expected_review_token": t2["review"]["token"]})
        audit = c.get(f"/api/tickets/{t2['id']}/integrity").json()["data"]
        R.check("16", "change after the claim refused (TICKET_TAMPERED_AFTER_CLAIM), nothing sent",
                tampered.status_code == 409 and tampered.json()["detail"]["error_code"] == "TICKET_TAMPERED_AFTER_CLAIM"
                and c.get(f"/api/tickets/{t2['id']}").json()["data"]["kite_order_id"] is None, tampered.text)
        R.check("16", "audit: snapshot + TAMPER_DETECTED(quantity), hash chain verifies",
                audit["tampered"] and [e["event_type"] for e in audit["events"]] == ["TAMPER_DETECTED", "CLAIM_SNAPSHOT"]
                and audit["events"][0]["categories"] == ["quantity"] and audit["chain"]["ok"], audit)
        revert = c.post(f"/api/tickets/{t2['id']}/revert-rejection")
        R.check("16", "a tampered ticket can never be reverted",
                revert.status_code == 409 and revert.json()["detail"]["error_code"] == "TICKET_TAMPERED_AFTER_CLAIM")

        # 17-18. restart on the same database: old credential dead, fresh login required
        old_cookie, old_token = cookie(c), c.headers[TOKEN_HEADER]
        stop_server(proc)
        procs.remove(proc)
        proc = start_server(workdir, port, keep_db=True)
        procs.append(proc)
        c2 = client(port)
        c2.cookies.set("kt_test_session", old_cookie, domain="127.0.0.1")
        c2.headers[TOKEN_HEADER] = old_token
        status = c2.get("/api/session/status").json()
        # P2-G R-F2 (deliberate change): account reads, like writes, now need this launch's credential.
        stale_read = c2.get("/api/dmb-inbox/items")
        R.check("17", "after the restart: terminal not authorised, account reads refused (P2-G R-F2)",
                status["terminal_authorized"] is False and status["user_name"] is None
                and stale_read.status_code == 401
                and stale_read.json()["detail"]["error_code"] == "TERMINAL_SESSION_REQUIRED", status)
        r = c2.post(f"/api/dmb-inbox/items/{infy['id']}/notes", json={"text": "after restart"})
        R.check("18", "old credential refused for writes (401 TERMINAL_SESSION_REQUIRED)",
                r.status_code == 401 and r.json()["detail"]["error_code"] == "TERMINAL_SESSION_REQUIRED", r.text)

        # 19. Emergency Exit: refused before re-login, works after (FakeKite, live demo positions)
        mark = len(_broker_calls(c2))
        ee = c2.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
        R.check("19", "Emergency Exit before re-login refused (401), no broker write",
                ee.status_code == 401 and _broker_calls(c2)[mark:] == [], ee.text)
        login(c2)
        R.check("18", "fresh login issues a new credential", cookie(c2) not in (None, old_cookie))
        R.check("18", "fresh login issues a new terminal token; the old one is dead (P2-G R-F3)",
                c2.headers[TOKEN_HEADER] != old_token
                and c2.get("/api/dmb-inbox/items", headers={TOKEN_HEADER: old_token}).status_code == 401)
        replay = client(port)                                # the cookie alone, as another port would collect it
        replay.cookies.set("kt_test_session", cookie(c2), domain="127.0.0.1")
        r_read, r_write = replay.get("/api/dmb-inbox/items"), replay.post(
            "/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
        R.check("18", "the current cookie WITHOUT the terminal token reads and writes nothing (P2-G R-F3)",
                r_read.status_code == 401 and r_write.status_code == 401 and _broker_calls(c2)[mark:] == [],
                (r_read.status_code, r_write.status_code))
        ee = c2.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
        data = ee.json().get("data", {})
        R.check("19", "Emergency Exit after fresh login submits an exit for every live position (FakeKite)",
                ee.status_code == 200 and data.get("count", 0) >= 1
                and all(x["status"] in ("exit_submitted", "already_exiting", "closed") for x in data.get("results", []))
                and _broker_calls(c2)[mark:], ee.text[:400])

        # 20. nothing real was touched
        R.check("20", "the DMB handoff files are exactly as DMB wrote them",
                {p: p.read_bytes() for p in (workdir / "handoff").rglob("*") if p.is_file()} == dmb_files_before)
        R.check("20", "the test server ran on the scratch database with FakeKite",
                f"db: {workdir / 'kite' / 'demo.db'}" in (workdir / "kite_server_first.log").read_text(encoding="utf-8")
                and "SYNTHETIC TEST PROFILE active" in (workdir / "kite_server_first.log").read_text(encoding="utf-8"))
        R.check("20", "the real account database and backups unchanged (size + mtime, never opened)",
                _real_db_fingerprint() == before)
        if keep_running:
            print(f"\nThe isolated test UI stays up at http://localhost:{port}/  (open /demo/kite-login first). "
                  f"Ctrl+C to stop.")
            try:
                proc.wait()
            except KeyboardInterrupt:
                pass
    except Exception as e:                              # a crashed stage is a failure, never a pass
        R.check("!", f"run aborted: {type(e).__name__}", False, e)
    finally:
        for p in procs:
            stop_server(p)
    print(f"\n{len(R.rows) - len(R.failed)}/{len(R.rows)} checks passed")
    (workdir / "acceptance_result.json").write_text(json.dumps(
        [{"stage": s, "check": n, "passed": ok, "detail": d} for s, n, ok, d in R.rows], indent=2), encoding="utf-8")
    if R.failed:
        print("FAILED:")
        for s, n, _, d in R.failed:
            print(f"  {s} {n}: {d}")
        return 1
    print("LOCAL SYNTHETIC INTEGRATION ACCEPTANCE: PASS (FakeKite, scratch database, no real broker or data)")
    return 0


def serve_only(workdir: Path, port: int) -> int:
    """Manual walk-through: the same isolated environment, left running for a person to use.
    The server clock is pinned inside the review session (2026-11-03 11:00 IST), where the
    synthetic bundle is importable and Start ticket is allowed."""
    export = export_fixture(workdir)
    proc = start_server(workdir, port, keep_db=False)
    c = client(port)
    try:
        login(c)
        pin(c, DURING_REVIEW)
        c.post("/api/demo/paper/ltp", json={"symbol": "INFY", "price": 1180.0}).raise_for_status()
        current = c.get("/api/risk/settings").json()["data"]
        keep = ("max_risk_per_trade_pct", "max_capital_per_trade_pct", "max_open_positions",
                "max_single_stock_exposure_pct", "max_total_portfolio_risk_pct", "min_risk_reward", "starting_capital")
        c.post("/api/risk/settings", json={**{k: current[k] for k in keep}, "max_sector_exposure_pct": 100.0}
               ).raise_for_status()
        print(f"""
ISOLATED SYNTHETIC TEST INSTANCE RUNNING (FakeKite, scratch DB, LIVE impossible)
  bundle      {export['bundle_id']}  (session {SESSION}, {export['packet_count']} packets)
  bundle file {export['path']}
  server log  {workdir / 'kite_server_first.log'}

  1. Open a SEPARATE browser profile (e.g. an Edge InPrivate window).
  2. Go to  http://localhost:{port}/demo/authorize   (the test login; opens the terminal)
  3. Open "DMB Research Inbox" in the left menu and follow docs/DMB_KITE_ACCEPTANCE_TEST.md, part B.
Ctrl+C here stops the instance.""", flush=True)
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop_server(proc)
    return 0


def _broker_calls(c) -> list:
    """The broker write calls the test server's FakeKite has received so far (demo-only read)."""
    r = c.get("/demo/fake-kite/calls")
    r.raise_for_status()
    return [n for n in r.json()["data"] if n in BROKER_WRITES]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workdir", default=str(Path(tempfile.gettempdir()) / "dmb_kite_acceptance"))
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--keep-running", action="store_true")
    ap.add_argument("--reset", action="store_true", help="delete the scratch workdir and exit")
    ap.add_argument("--serve-only", action="store_true",
                    help="no automated checks: export, start the isolated instance and leave it up for a manual run")
    args = ap.parse_args()
    workdir = _safe_workdir(Path(args.workdir))
    reset(workdir)
    if args.reset:
        return 0
    workdir.mkdir(parents=True)
    (workdir / MARKER).write_text("DMB-Kite synthetic acceptance scratch directory; safe to delete.\n", encoding="utf-8")
    if args.serve_only:
        return serve_only(workdir, args.port)
    return run(workdir, args.port, args.keep_running)


if __name__ == "__main__":
    sys.exit(main())
