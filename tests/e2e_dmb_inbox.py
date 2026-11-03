"""Browser validation of the DMB Research Inbox (headless Microsoft Edge via Playwright).

    .venv\\Scripts\\python.exe tests\\e2e_dmb_inbox.py [--port 8791] [--headed] [--workdir DIR]

Builds the same isolated environment as tests/acceptance_dmb_kite.py - DMB's real exporter on the
SYNTHETIC fixture into a scratch handoff folder, the Kite demo server in the synthetic test
profile (throwaway database, FakeKite, demo login, LIVE impossible) - then drives the real UI:
import, provenance display, idempotent re-import, dismiss, note, research watchlist, Start ticket
(only symbol / exchange / rationale prefilled), manual entry, the P1-D review in the approval
dialog, approval, and the positions list's separate broker / fill column. It asserts that
importing and opening items made no broker call at all, and that no broker WRITE happened before
the user approved. No credential is hard-coded (the demo login issues one), no personal path is
assumed (DMB is the sibling checkout or DMB_REPO). Exit code 0 only if every check passed.
Not part of the pytest run (it needs Edge and starts processes); see docs/TESTING.md.

P2-G (docs/SECURITY_P2G_AUDIT.md), in a real browser: the login lands on /#kt_token=..., the page
keeps the terminal token in its own origin's storage; a real server on ANOTHER PORT receives the
credential cookie (cookies have no port) but cannot read the token, and replaying the cookie it
captured is refused; another local hostname gets neither factor; an unexpected Host is refused;
and a logout closes the page's open tick WebSocket (4401), which the page reports.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acceptance_dmb_kite as A  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def other_port_server(port: int) -> tuple[ThreadingHTTPServer, list]:
    """Another local web server on the SAME host, different port - what R-F3 is about. It records
    the Cookie header every request brings it and serves a blank page."""
    seen: list = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Cookie") or "")
            body = b"<!doctype html><title>other local app</title><p>another port</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--workdir", default=str(Path(tempfile.gettempdir()) / "dmb_kite_browser"))
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    workdir = A._safe_workdir(Path(args.workdir))
    A.reset(workdir)
    workdir.mkdir(parents=True)
    (workdir / A.MARKER).write_text("DMB-Kite browser check scratch directory; safe to delete.\n", encoding="utf-8")
    shots = workdir / "shots"
    shots.mkdir()

    from playwright.sync_api import sync_playwright
    R = A.Run()
    errors: list[str] = []
    export = A.export_fixture(workdir)
    proc = A.start_server(workdir, args.port, keep_db=False)
    base = f"http://127.0.0.1:{args.port}"
    writes = {"X-Kite-Terminal": "1", "Origin": base}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=not args.headed)
            ctx = browser.new_context(viewport={"width": 1500, "height": 1000})
            page = ctx.new_page()
            page.clock.install(time=datetime(2026, 11, 2, 21, 0, tzinfo=IST))   # the browser's own clock, pinned
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

            def calls():
                return ctx.request.get(base + "/demo/fake-kite/calls").json()["data"]

            def auth() -> dict:
                """The terminal token the page holds (P2-G R-F3), for this harness's own API calls."""
                return {"X-Kite-Terminal-Token": page.evaluate("() => localStorage.getItem('kt.terminalToken')") or ""}

            def api(path):
                return ctx.request.get(base + path, headers=auth()).json()

            # Before the test login: the page is locked and shows no account data (P2-G R-F2), and a
            # write is refused (SEC-1 P1-F).
            page.goto(base + "/#dmb-inbox")
            page.wait_for_timeout(1200)
            R.check("F", "before login the terminal is locked: no account data shown (P2-G R-F2)",
                    page.is_visible("#terminal-locked-gate") and not page.is_visible("#app-shell"))
            r = ctx.request.post(base + "/api/dmb-inbox/imports", headers=writes,
                                 data={"session_date": A.SESSION, "bundle_id": export["bundle_id"]})
            R.check("F", "import without the test login is refused (401)", r.status == 401, r.status)
            errors.clear()                              # the refusal above is an expected 401

            page.goto(base + "/demo/kite-login")        # the test profile's Kite login stand-in, as a navigation
            page.wait_for_timeout(800)
            cookies = {c["name"]: c for c in ctx.cookies(base)}
            R.check("F", "test login sets an HttpOnly, SameSite=Strict kt_test_session cookie",
                    cookies.get("kt_test_session", {}).get("httpOnly") is True
                    and cookies["kt_test_session"].get("sameSite") == "Strict")
            token = page.evaluate("() => localStorage.getItem('kt.terminalToken')")
            R.check("F", "P2-G R-F3: the login hands the page a terminal token, stripped from the address bar",
                    bool(token) and "kt_token" not in page.url and token not in page.url, page.url)
            # P2-G R-F3 evidence: the browser sends a 127.0.0.1 cookie to EVERY port on that host.
            other = {c["name"] for c in ctx.cookies(f"http://127.0.0.1:{args.port + 1}")}
            R.check("F", "evidence R-F3: the credential cookie is visible to another port on 127.0.0.1",
                    "kt_test_session" in other, other)
            server, seen = other_port_server(args.port + 1)
            try:
                foreign = ctx.new_page()
                foreign.goto(f"http://127.0.0.1:{args.port + 1}/")
                leaked = next((c for c in seen if "kt_test_session=" in c), "")
                R.check("F", "R-F3: a real server on another port RECEIVES the credential cookie (cookies have no port)",
                        bool(leaked))
                R.check("F", "R-F3: ...but a page on that port cannot read the terminal token (origin-scoped storage)",
                        foreign.evaluate("() => localStorage.getItem('kt.terminalToken')") is None)
                stolen = leaked.split("kt_test_session=", 1)[1].split(";", 1)[0]
                replay = A.client(args.port)
                replay.cookies.set("kt_test_session", stolen, domain="127.0.0.1")
                rr, rw = replay.get("/api/dmb-inbox/items"), replay.post(
                    "/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
                R.check("F", "R-F3: replaying the captured cookie (forged Host/Origin, no token) reads and writes nothing",
                        rr.status_code == 401 and rw.status_code == 401, (rr.status_code, rw.status_code))
                fetched = foreign.evaluate("""async (url) => {
                    try { const r = await fetch(url, {credentials: 'include'}); return 'read:' + r.status; }
                    catch (e) { return 'blocked'; } }""", base + "/api/dmb-inbox/items")
                R.check("F", "R-F2: a cross-origin fetch from that page cannot read account data", fetched == "blocked",
                        fetched)
                foreign.close()
            finally:
                server.shutdown()
            alias = ctx.new_page()                      # another local hostname for the same server
            alias.goto(f"http://localhost:{args.port}/")
            alias.wait_for_timeout(1000)
            R.check("F", "R-F3: another local hostname (localhost) gets neither factor: the terminal stays locked",
                    alias.is_visible("#terminal-locked-gate")
                    and alias.evaluate("() => localStorage.getItem('kt.terminalToken')") is None
                    and "kt_test_session" not in {c["name"] for c in ctx.cookies(f"http://localhost:{args.port}")})
            alias.close()
            rebound = A.client(args.port)
            rebound.headers["Host"] = f"evil.example:{args.port}"
            R.check("F", "an unexpected Host header is refused (400)",
                    rebound.get("/api/health").status_code == 400)
            errors.clear()                              # the locked alias page's expected refusals
            assert ctx.request.post(base + "/api/demo/clock", headers={**writes, **auth()}, data={"ist": A.IMPORT_AT}).ok

            page.goto(base + "/#dmb-inbox")
            page.wait_for_selector("#dmb-items", timeout=15000)
            page.wait_for_timeout(800)
            root = page.locator("#dmb-inbox-root")
            calls_before = len(calls())
            R.check("F", "disclaimer shown", "NOT A TRADE RECOMMENDATION OR ORDER INSTRUCTION" in root.inner_text())
            R.check("F", "one bundle listed with an Import button", root.locator("[data-dmb-import]").count() == 1)
            page.screenshot(path=str(shots / "1_before_import.png"), full_page=True)
            root.locator("[data-dmb-import]").click()
            page.wait_for_selector("[data-dmb-open]", timeout=10000)
            page.wait_for_timeout(500)
            table = root.locator("#dmb-items").inner_text()
            R.check("F", "INFY and TCS imported; SYNTHETIC badge", "INFY" in table and "TCS" in table and "SYNTHETIC" in table)
            R.check("F", "INFY can start a ticket, TCS research only",
                    "can start a ticket" in table and "research only" in table, table[:300])
            root.locator("[data-dmb-import]").click()
            page.wait_for_timeout(1000)
            n = len(api("/api/dmb-inbox/items?include_superseded=true")["data"])
            R.check("F", "re-import creates no duplicate", n == 2, n)

            items = {i["symbol"]: i for i in api("/api/dmb-inbox/items")["data"]}
            infy, tcs = items["INFY"]["id"], items["TCS"]["id"]
            root.locator(f'[data-dmb-open="{infy}"]').click()
            page.wait_for_selector(".dmb-detail", timeout=5000)
            detail = root.locator(".dmb-detail").inner_text()
            for text in ("DMB packet id", "Source session", "Evidence as of", "Review session", "2026-11-03",
                         "not a trade side", "Private handoff", "PERMITTED", "MATCHED"):
                R.check("F", f"detail shows {text!r}", text in detail)
            # The demo's own background market-data sync keeps reading candles (historical_data); the
            # inbox itself builds no broker client (tests/test_dmb_inbox.py proves it in-process).
            during = calls()[calls_before:]
            R.check("F", "importing and opening items: no broker write, no call but the background candle sync",
                    set(during) <= {"historical_data"} and not [c for c in during if c in A.BROKER_WRITES], during)
            page.screenshot(path=str(shots / "2_detail.png"), full_page=True)

            root.locator("textarea[name=text]").fill("browser check note")
            root.locator(".dmb-note-form button").click()
            page.wait_for_timeout(800)
            R.check("F", "note recorded", "browser check note" in root.locator(".dmb-detail").inner_text())
            strategy_before = api("/api/watchlist")["data"]
            root.locator(f'[data-dmb-watch="{infy}"]').click()
            page.wait_for_timeout(800)
            R.check("F", "research watchlist toggled",
                    "Remove from research watchlist" in root.locator(".dmb-detail").inner_text())
            R.check("F", "strategy watchlist unchanged", api("/api/watchlist")["data"] == strategy_before)

            root.locator(f'[data-dmb-open="{tcs}"]').click()
            page.wait_for_selector(f'[data-dmb-dismiss="{tcs}"]', timeout=5000)
            page.once("dialog", lambda d: d.accept())
            root.locator(f'[data-dmb-dismiss="{tcs}"]').click()
            page.wait_for_timeout(1000)
            R.check("F", "TCS dismissed (confirmed in the browser dialog)",
                    api(f"/api/dmb-inbox/items/{tcs}")["data"]["dismissed"])

            # Start ticket during the review session: three fields only
            assert ctx.request.post(base + "/api/demo/clock", headers={**writes, **auth()}, data={"ist": A.DURING_REVIEW}).ok
            page.clock.set_fixed_time(datetime(2026, 11, 3, 11, 0, tzinfo=IST))
            assert ctx.request.post(base + "/api/demo/paper/ltp", headers={**writes, **auth()},
                                    data={"symbol": "INFY", "price": 1180.0}).ok
            settings = api("/api/risk/settings")["data"]
            keep = ("max_risk_per_trade_pct", "max_capital_per_trade_pct", "max_open_positions",
                    "max_single_stock_exposure_pct", "max_total_portfolio_risk_pct", "min_risk_reward", "starting_capital")
            # The demo portfolio is ~64% IT by design; scratch instance only (see the acceptance test).
            assert ctx.request.post(base + "/api/risk/settings", headers={**writes, **auth()},
                                    data={**{k: settings[k] for k in keep}, "max_sector_exposure_pct": 100.0}).ok
            page.goto(base + "/#dmb-inbox")
            page.wait_for_selector(f'[data-dmb-open="{infy}"]', timeout=10000)
            root.locator(f'[data-dmb-open="{infy}"]').click()
            page.wait_for_selector(f'[data-dmb-start="{infy}"]', timeout=5000)
            root.locator(f'[data-dmb-start="{infy}"]').click()
            page.wait_for_selector("#view-new-ticket:not([hidden])", timeout=5000)
            page.wait_for_timeout(600)
            form = page.evaluate("""() => { const f = document.getElementById('ticket-form');
                const v = (n) => f.querySelector(`[name="${n}"]`).value;
                return {symbol: document.getElementById('ticket-symbol').value, rationale: v('rationale'),
                        side: v('transaction_type'), order_type: v('entry_order_type'), product: v('product_type'),
                        quantity: v('quantity'), entry: v('entry_price'), stop: v('stop_loss'), target: v('target_1'),
                        valid: f.checkValidity(),
                        exchange: document.getElementById('dmb-handoff-exchange').textContent}; }""")
            R.check("F", "symbol INFY, exchange NSE, synthetic rationale prefilled",
                    form["symbol"] == "INFY" and form["exchange"] == "NSE"
                    and form["rationale"].startswith("[SYNTHETIC TEST PACKET]"), form)
            R.check("F", "side, order type, product, quantity, entry, stop, target NOT prefilled",
                    all(form[k] == "" for k in ("side", "order_type", "product", "quantity", "entry", "stop", "target")),
                    form)
            R.check("F", "the prefilled form cannot be submitted", form["valid"] is False)
            page.screenshot(path=str(shots / "3_new_ticket.png"), full_page=True)

            page.select_option("#ticket-direction", "BUY")
            page.select_option("#ticket-entry-type", "LIMIT")
            page.select_option("#ticket-product-type", "CNC")
            page.fill("#ticket-quantity", "2")
            page.fill("#ticket-entry-price", "1180")
            page.fill("#ticket-stop-loss", "1150")
            page.fill("#ticket-target-1", "1250")
            writes_before = [c for c in calls() if c in A.BROKER_WRITES]
            page.locator('#ticket-form button[type="submit"]').click()
            page.wait_for_selector("#ticket-confirm-dialog[open]", timeout=10000)
            page.wait_for_timeout(600)
            review_note = page.locator("#ticket-confirm-review").inner_text()
            R.check("F", "approval dialog opens (nothing auto-approved) with a current P1-D review",
                    "current" in review_note, review_note)
            item = api(f"/api/dmb-inbox/items/{infy}")["data"]
            tid = item["ticket_links"][0]["ticket_id"] if item["ticket_links"] else None
            R.check("F", "ticket created by the normal path and linked",
                    tid is not None and item["history"][-1]["event_type"] == "TICKET_LINKED", item["history"][-3:])
            R.check("F", "no broker write before the user approved",
                    [c for c in calls() if c in A.BROKER_WRITES] == writes_before)
            page.screenshot(path=str(shots / "4_confirm_dialog.png"), full_page=True)
            page.locator("#ticket-confirm-btn").click()
            page.wait_for_timeout(2000)
            t = api(f"/api/tickets/{tid}")["data"]
            R.check("F", "approved in DRY RUN through the paper broker",
                    t["status"] == "executed" and t["mode"] == "dry_run", (t["status"], t["mode"]))

            page.goto(base + "/#positions")
            page.wait_for_timeout(1500)
            row = page.locator(f'#positions-active-table tr[data-position-id="{tid}"]')
            cell = row.locator('td[data-field="fill"]')
            R.check("F", "positions list: Broker accepted, Fill not filled (separate facts)",
                    cell.locator('[data-acceptance="ACCEPTED"]').count() == 1
                    and cell.locator('[data-fill="AWAITING_FILL"]').count() == 1, cell.inner_text() if row.count() else "no row")
            page.screenshot(path=str(shots / "5_positions.png"), full_page=True)
            R.check("F", "no console or page errors", not errors, "; ".join(errors[:5]))

            # P2-G R-F4: a logout (here from another client of the same browser) closes the page's
            # open tick WebSocket at once; the page says the session is no longer authorised.
            closes: list = []
            page.on("console", lambda m: closes.append(m.text) if "[ws] closed" in m.text else None)
            R.check("F", "R-F4: the page's tick WebSocket is open before the logout",
                    page.evaluate("() => typeof ws !== 'undefined' && ws && ws.readyState === WebSocket.OPEN"))
            assert ctx.request.post(base + "/api/logout", headers={**writes, **auth()}).ok
            page.wait_for_timeout(1500)
            R.check("F", "R-F4: logout closed the open tick WebSocket with 4401 and the page shows why",
                    any("4401" in c for c in closes) and page.is_visible("#authorize-banner"), closes)
            R.check("F", "R-F4: the old terminal token no longer reads anything",
                    ctx.request.get(base + "/api/dmb-inbox/items", headers=auth()).status == 401)
            page.screenshot(path=str(shots / "6_after_logout.png"), full_page=True)
            browser.close()
    except Exception as e:
        R.check("!", f"browser run aborted: {type(e).__name__}", False, e)
    finally:
        A.stop_server(proc)
    print(f"\n{len(R.rows) - len(R.failed)}/{len(R.rows)} browser checks passed  (screenshots: {shots})")
    return 1 if R.failed else 0


if __name__ == "__main__":
    sys.exit(main())
