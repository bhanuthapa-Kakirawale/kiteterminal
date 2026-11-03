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
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acceptance_dmb_kite as A  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


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
                return ctx.request.get(base + "/api/demo/fake-kite/calls").json()["data"]

            def api(path):
                return ctx.request.get(base + path).json()

            # Read-only without the credential; a write is refused (SEC-1 P1-F).
            page.goto(base + "/#dmb-inbox")
            page.wait_for_timeout(1200)
            r = ctx.request.post(base + "/api/dmb-inbox/imports", headers=writes,
                                 data={"session_date": A.SESSION, "bundle_id": export["bundle_id"]})
            R.check("F", "import without the test login is refused (401)", r.status == 401, r.status)
            errors.clear()                              # the refusal above is an expected 401

            ctx.request.get(base + "/demo/kite-login")  # the test profile's Kite login stand-in
            cookies = {c["name"]: c for c in ctx.cookies(base)}
            R.check("F", "test login sets an HttpOnly, SameSite=Strict kt_test_session cookie",
                    cookies.get("kt_test_session", {}).get("httpOnly") is True
                    and cookies["kt_test_session"].get("sameSite") == "Strict")
            # P2-G R-F3 evidence: the browser sends a 127.0.0.1 cookie to EVERY port on that host.
            other = {c["name"] for c in ctx.cookies(f"http://127.0.0.1:{args.port + 1}")}
            R.check("F", "evidence R-F3: the credential cookie is visible to another port on 127.0.0.1",
                    "kt_test_session" in other, other)
            assert ctx.request.post(base + "/api/demo/clock", headers=writes, data={"ist": A.IMPORT_AT}).ok

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
            assert ctx.request.post(base + "/api/demo/clock", headers=writes, data={"ist": A.DURING_REVIEW}).ok
            page.clock.set_fixed_time(datetime(2026, 11, 3, 11, 0, tzinfo=IST))
            assert ctx.request.post(base + "/api/demo/paper/ltp", headers=writes, data={"symbol": "INFY", "price": 1180.0}).ok
            settings = api("/api/risk/settings")["data"]
            keep = ("max_risk_per_trade_pct", "max_capital_per_trade_pct", "max_open_positions",
                    "max_single_stock_exposure_pct", "max_total_portfolio_risk_pct", "min_risk_reward", "starting_capital")
            # The demo portfolio is ~64% IT by design; scratch instance only (see the acceptance test).
            assert ctx.request.post(base + "/api/risk/settings", headers=writes,
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
            browser.close()
    except Exception as e:
        R.check("!", f"browser run aborted: {type(e).__name__}", False, e)
    finally:
        A.stop_server(proc)
    print(f"\n{len(R.rows) - len(R.failed)}/{len(R.rows)} browser checks passed  (screenshots: {shots})")
    return 1 if R.failed else 0


if __name__ == "__main__":
    sys.exit(main())
