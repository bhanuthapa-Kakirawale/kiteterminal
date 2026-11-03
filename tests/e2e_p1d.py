"""Browser validation of SEC-1 P1-D (manual-ticket review) against
tests/demo_server.py run with --review-controls (FakeKite + a throwaway
database; not --lifecycle-fixtures, whose ten open positions fill
max_open_positions and block every new entry), driven through Playwright with the locally
installed Microsoft Edge. Never touches a real account, database or broker.

    .venv/Scripts/python.exe tests/demo_server.py --port 8765 --workdir <scratch> --review-controls
    .venv/Scripts/python.exe tests/e2e_p1d.py --base http://127.0.0.1:8765 --out <dir>

Both clocks are controlled: the server's trading clock through the demo-only
POST /api/demo/clock, the browser's through Playwright's page.clock. The
market-window rule itself is untouched.

Every brief check gets an explicit PASS/FAIL line. A refused approval (HTTP
409) is part of the expected behaviour: each one is declared before the
action that triggers it, matched against the response actually observed, and
the browser's "Failed to load resource ... 409" console line is accepted only
for those declared responses. Any other console error or page error fails.
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

CHECKS = {
    1: "manual ticket creation and review",
    2: "Quick-Action BUY review",
    3: "approval dialog displays current review information",
    4: "current token approves when all other gates pass",
    5: "second-tab re-review invalidates the first tab's token",
    6: "stale dialog gets the expected refusal and the UI explains it",
    7: "re-review restores approval eligibility",
    8: "tampered ticket is permanently rejected",
    9: "Kite logout/login invalidates the old review",
    10: "Quick-Action SELL, ordinary exit and Emergency Exit remain available",
    11: "trading lock and dry-run behaviour remain correct",
    12: "market-window transition invalidates an old review",
    13: "P1-F / P2-G: reads and writes need the per-launch credential (HttpOnly cookie + terminal token)",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base = args.base
    write_headers = {"Origin": base, "X-Kite-Terminal": "1"}

    results: dict[int, list[tuple[str, bool, str]]] = {n: [] for n in CHECKS}
    expected_409: list[str] = []      # declared error codes, consumed as observed
    observed_409: list[dict] = []
    unexpected_http: list[str] = []
    # Codes the page itself read from refusal bodies a listener could not
    # read: (url suffix, code), matched to observed refusals at the end.
    page_read_codes: list[tuple[str, str]] = []

    def check(n: int, name: str, ok, detail="") -> None:
        results[n].append((name, bool(ok), str(detail)[:240]))
        print(f"  [{n:>2}] {'PASS' if ok else 'FAIL'} {name} {detail if not ok else ''}".rstrip(), flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        console_errors: list[str] = []
        page_errors: list[str] = []

        def on_response(response):
            if response.status in (401, 409) and "/api/" in response.url:
                try:
                    code = response.json()["detail"]["error_code"]
                except Exception:
                    code = None      # body not retrievable here; confirm_from_page fills in what the page read
                observed_409.append({"url": response.url, "code": code})
            elif response.status >= 400 and "/api/" in response.url:
                unexpected_http.append(f"{response.status} {response.url}")

        def watch(pg):
            pg.on("pageerror", lambda e: page_errors.append(str(e)))
            pg.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            pg.on("response", on_response)

        def auth() -> dict:
            """The terminal token the page keeps in localStorage (P2-G R-F3), for the harness's own calls."""
            return {"X-Kite-Terminal-Token": page.evaluate("() => localStorage.getItem('kt.terminalToken')") or ""}

        def demo(path: str, body: dict | None = None) -> dict:
            r = context.request.post(base + path, data=json.dumps(body or {}),
                                     headers={**write_headers, **auth(), "Content-Type": "application/json"})
            assert r.ok, f"{path}: {r.status} {r.text()}"
            return r.json()["data"]

        def api_get(path: str) -> dict:
            return context.request.get(base + path, headers=auth()).json()

        def set_clock(pages, **body) -> datetime:
            now = datetime.fromisoformat(demo("/api/demo/clock", body)["now_ist"])
            for pg in pages:
                pg.clock.set_fixed_time(now)
            return now

        def expect_409(code: str) -> None:
            expected_409.append(code)

        page = context.new_page()
        watch(page)

        def nav(pg, section: str) -> None:
            pg.click(f'.nav-item[data-section-id="{section}"]')
            pg.wait_for_selector(f"#view-{section}:not([hidden])")

        def boot(pg, section: str = "dashboard") -> None:
            pg.goto(f"{base}/#{section}")
            pg.reload()
            pg.wait_for_selector("#app-shell:not([hidden])", timeout=20000)
            pg.wait_for_selector(f"#view-{section}:not([hidden])", timeout=20000)
            pg.wait_for_timeout(1200)

        def close_toasts(pg) -> None:
            pg.evaluate("() => document.querySelectorAll('.toast').forEach((t) => t.remove())")

        def create_manual(pg, symbol="ITC", qty="10", entry="415", stop="404", target="440") -> int:
            nav(pg, "new-ticket")
            pg.fill("#ticket-symbol", symbol)
            pg.wait_for_timeout(700)
            pg.select_option("#ticket-entry-type", "LIMIT")
            pg.fill("#ticket-entry-price", entry)
            pg.fill("#ticket-stop-loss", stop)
            pg.fill("#ticket-target-1", target)
            pg.fill("#ticket-rationale", "P1-D browser validation")
            pg.wait_for_timeout(700)
            pg.fill("#ticket-quantity", qty)
            pg.wait_for_timeout(300)
            pg.click('#ticket-form button[type="submit"]')
            pg.wait_for_selector("#ticket-confirm-dialog[open]", timeout=15000)
            return api_get("/api/tickets?source=manual")["data"][0]["id"]

        def ticket(tid: int) -> dict:
            return api_get(f"/api/tickets/{tid}")["data"]

        def dialog_state(pg) -> dict:
            return {
                "open": pg.is_visible("#ticket-confirm-dialog"),
                "note": pg.inner_text("#ticket-confirm-review") if pg.is_visible("#ticket-confirm-review") else "",
                "approve": pg.is_visible("#ticket-confirm-btn"),
                "rereview": pg.is_visible("#ticket-rereview-btn"),
            }

        def approve_in_dialog(pg) -> None:
            if pg.is_visible("#ticket-confirm-amo"):
                pg.check("#ticket-confirm-amo-checkbox")
            pg.click("#ticket-confirm-btn")

        def confirm_from_page(pg, tid: int, body: dict) -> tuple[int, str | None]:
            """A confirm sent by the page's own fetch (same origin), as a stale
            or scripted client would; the body is read so its code is known."""
            status, code = pg.evaluate("""async ([id, body]) => {
                const r = await fetch(`/api/tickets/${id}/confirm`, {method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-Kite-Terminal': '1'}, body: JSON.stringify(body)});
                const j = await r.json();
                return [r.status, (j.detail && j.detail.error_code) || null];
            }""", [tid, body])
            page_read_codes.append((f"/api/tickets/{tid}/confirm", code))
            return status, code

        def open_card_dialog(pg, tid: int) -> None:
            boot(pg, "approvals")
            card = pg.locator(f'.approval-card[data-ticket-id="{tid}"]')
            card.locator('button[data-action="details"]').click()
            pg.wait_for_selector("#ticket-confirm-dialog[open]", timeout=10000)

        # ---------------------------------------------------------------- P1-F: before authorization
        # The demo seeds a Kite session, but this browser has no terminal
        # credential yet (as after an app restart): since P2-G R-F2 the UI is
        # locked - no account data - and writes are refused.
        page.goto(base + "/")
        page.wait_for_selector("#terminal-locked-gate:not([hidden])", timeout=20000)
        page.wait_for_timeout(800)
        check(13, "unauthorized: the UI is locked, shows no account data and offers only the login",
              page.is_visible("#unlock-btn") and not page.is_visible("#app-shell"))
        expect_409("TERMINAL_SESSION_REQUIRED")
        status, code = page.evaluate("""async () => {
            const r = await fetch('/api/ui-prefs', {method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-Kite-Terminal': '1'}, body: '{"theme": "dark"}'});
            const j = await r.json();
            return [r.status, (j.detail && j.detail.error_code) || null];
        }""")
        page_read_codes.append(("/api/ui-prefs", code))
        check(13, "unauthorized: a same-origin write with the right headers is refused (401 TERMINAL_SESSION_REQUIRED)",
              (status, code) == (401, "TERMINAL_SESSION_REQUIRED"), (status, code))
        authorized = context.request.get(base + "/demo/authorize", max_redirects=0, headers={"Accept": "text/html"})
        set_cookie = authorized.headers.get("set-cookie", "")
        check(13, "the authorization response sets an HttpOnly, SameSite=Strict credential cookie",
              "kt_session=" in set_cookie and "httponly" in set_cookie.lower()
              and "samesite=strict" in set_cookie.lower(), set_cookie[:0])
        landing = authorized.headers.get("location", "")
        check(13, "it lands on the UI with the terminal token in the fragment only (P2-G R-F3)",
              landing.startswith("/#kt_token=") and "kt_token" not in set_cookie, landing[:12])
        page.goto(base + landing)
        page.wait_for_timeout(300)
        check(13, "the page moved the token into its storage and out of the address bar",
              "kt_token" not in page.url and bool(page.evaluate("() => localStorage.getItem('kt.terminalToken')")))
        page.goto(base + "/")
        page.wait_for_selector("#app-shell:not([hidden])", timeout=20000)
        page.wait_for_timeout(800)
        check(13, "authorized: the banner is gone and page scripts cannot read the credential",
              not page.is_visible("#authorize-banner") and "kt_session" not in page.evaluate("() => document.cookie"))

        # ---------------------------------------------------------------- setup
        start = set_clock([page], time="11:00")   # a weekday, market open, demo signals current
        print(f"Server and browser clocks pinned to {start.isoformat()}")
        boot(page)

        # ---------------------------------------------------------------- 1, 3, 4, 11 (dry run)
        tid = create_manual(page)
        d = dialog_state(page)
        t = ticket(tid)
        check(1, "a new manual ticket is stamped with a review", t["review_generation"] and t["review"]["current"], t["review"])
        check(3, "the dialog shows the review as current with the creation time",
              "Created" in d["note"] and "current" in d["note"] and d["approve"] and not d["rereview"], d)
        page.screenshot(path=str(out / "p1d-01-fresh-dialog.png"))
        approve_in_dialog(page)
        page.wait_for_selector(".toast.success", timeout=15000)
        page.wait_for_timeout(600)
        t = ticket(tid)
        check(4, "approving with the current review token executes", t["status"] == "executed", t["status"])
        check(11, "dry run: the approval is a PAPER order, nothing sent to the broker",
              t["mode"] == "dry_run" and str(t["kite_order_id"] or "").startswith("PAPER"), t["kite_order_id"])
        close_toasts(page)

        # ---------------------------------------------------------------- 2: Quick-Action BUY
        demo("/api/demo/holding-loss", {"symbol": "HDFCBANK", "pct": 12})   # raises the loss alert
        boot(page)
        qa_tid = None
        chips = page.locator(".alert-chip")
        for i in range(chips.count()):
            chips.nth(i).click()
            page.wait_for_timeout(600)
            if page.is_visible("#quick-action-dialog") and page.is_visible("#qa-buy-section"):
                page.fill("#qa-buy-qty", "1")
                page.fill("#qa-buy-entry-price", page.input_value("#qa-buy-entry-price") or "1400")
                page.fill("#qa-buy-stop-loss", str(round(float(page.input_value("#qa-buy-entry-price")) * 0.95, 2)))
                page.fill("#qa-buy-target-1", str(round(float(page.input_value("#qa-buy-entry-price")) * 1.15, 2)))
                page.fill("#qa-buy-rationale", "P1-D browser validation (Quick Action buy)")
                page.click("#qa-buy-submit")
                page.wait_for_selector("#ticket-confirm-dialog[open]", timeout=15000)
                qa_tid = api_get("/api/tickets?source=alert_action")["data"][0]["id"]
                break
            if page.is_visible("#quick-action-dialog"):
                page.keyboard.press("Escape")
        if qa_tid is None:
            check(2, "a Quick-Action BUY could be created from an alert chip", False,
                  "no breach_loss alert in the demo data")
        else:
            d = dialog_state(page)
            q = ticket(qa_tid)
            check(2, "the Quick-Action BUY carries a current review and its dialog shows it",
                  q["source"] == "alert_action" and q["review"]["current"] and "current" in d["note"]
                  and d["approve"], {"review": q["review"], "dialog": d})
            page.screenshot(path=str(out / "p1d-02-quick-action-buy.png"))
            page.keyboard.press("Escape")

        # ---------------------------------------------------------------- 9: Kite logout / login
        tid9 = create_manual(page, qty="5")
        page.keyboard.press("Escape")
        page.click("#logout-btn")
        page.wait_for_timeout(400)
        if page.is_visible("#app-confirm-dialog"):
            page.click("#app-confirm-ok")
        page.wait_for_selector("#connect-gate:not([hidden])", timeout=10000)
        check(9, "logout shows the connect gate", True)
        page.goto(base + "/demo/kite-login")   # real session replacement + new credential, as a navigation
        page.wait_for_timeout(500)
        boot(page, "approvals")
        card = page.locator(f'.approval-card[data-ticket-id="{tid9}"]')
        check(9, "after a new Kite login the ticket's card says 'needs re-review'",
              card.count() == 1 and "needs re-review" in card.inner_text(), card.inner_text()[:160] if card.count() else "")
        card.locator('button[data-action="approve"]').click()
        page.wait_for_selector("#ticket-confirm-dialog[open]", timeout=10000)
        d = dialog_state(page)
        check(9, "the dialog explains the earlier login and offers Re-review instead of Approve",
              "earlier Kite login" in d["note"] and d["rereview"] and not d["approve"], d)
        page.screenshot(path=str(out / "p1d-09-relogin-stale.png"))
        page.keyboard.press("Escape")

        # ---------------------------------------------------------------- 5, 6, 7: two tabs
        tab2 = context.new_page()
        watch(tab2)
        tab2.clock.set_fixed_time(start)
        open_card_dialog(page, tid9)
        open_card_dialog(tab2, tid9)
        check(5, "both tabs show the stale ticket in re-review state",
              dialog_state(page)["rereview"] and dialog_state(tab2)["rereview"])
        page.click("#ticket-rereview-btn")                     # tab 1 re-reviews
        page.wait_for_selector(".toast.success", timeout=15000)
        page.wait_for_timeout(500)
        token_tab1 = ticket(tid9)["review"]["token"]
        d1 = dialog_state(page)
        check(7, "after Re-review the dialog is current and offers Approve", "current" in d1["note"] and d1["approve"], d1)
        tab2.click("#ticket-rereview-btn")                     # tab 2 re-reviews, same second
        tab2.wait_for_selector(".toast.success", timeout=15000)
        tab2.wait_for_timeout(500)
        token_tab2 = ticket(tid9)["review"]["token"]
        check(5, "the second tab's re-review issued a new token (same second, new generation)",
              token_tab2 != token_tab1, (token_tab1[:12], token_tab2[:12]))
        close_toasts(page)
        expect_409("TICKET_REVIEW_CHANGED")
        approve_in_dialog(page)                                # tab 1 approves with its older token
        page.wait_for_selector(".toast.warning", timeout=15000)
        warning = page.inner_text(".toast.warning")
        check(6, "tab 1's approval is refused and the toast explains the ticket changed",
              "changed since you looked" in warning and ticket(tid9)["status"] == "pending_confirmation", warning)
        page.wait_for_selector("#ticket-confirm-dialog[open]", timeout=10000)
        d1 = dialog_state(page)
        check(6, "the refused dialog reopens on the current review", d1["approve"] and "current" in d1["note"], d1)
        page.screenshot(path=str(out / "p1d-06-stale-tab-refused.png"))
        close_toasts(page)
        approve_in_dialog(page)
        page.wait_for_selector(".toast.success", timeout=15000)
        page.wait_for_timeout(600)
        check(7, "the re-reviewed ticket is approved from the reopened dialog", ticket(tid9)["status"] == "executed",
              ticket(tid9)["status"])
        tab2.keyboard.press("Escape")
        tab2.close()
        close_toasts(page)

        # ---------------------------------------------------------------- 8: tampering
        tid8 = create_manual(page, qty="6")
        token8 = ticket(tid8)["review"]["token"]
        page.keyboard.press("Escape")
        demo(f"/api/demo/tamper/{tid8}")
        open_card_dialog(page, tid8)
        d = dialog_state(page)
        check(8, "a tampered ticket's dialog offers neither Approve nor Re-review",
              "cannot be approved" in d["note"] and not d["approve"] and not d["rereview"], d)
        page.screenshot(path=str(out / "p1d-08-tampered.png"))
        page.keyboard.press("Escape")
        expect_409("TICKET_EVIDENCE_MISMATCH")
        status, code = confirm_from_page(page, tid8, {"expected_mode": "dry_run", "expected_review_token": token8})
        check(8, "approving it anyway is refused (TICKET_EVIDENCE_MISMATCH) and the ticket is rejected for good",
              (status, code) == (409, "TICKET_EVIDENCE_MISMATCH") and ticket(tid8)["status"] == "rejected",
              (status, code, ticket(tid8)["status"]))

        # ---------------------------------------------------------------- 11: trading lock
        tid11 = create_manual(page, qty="4")
        page.keyboard.press("Escape")
        boot(page)
        page.click("#trading-lock-btn")
        page.wait_for_selector("#app-confirm-dialog[open]", timeout=5000)
        if page.is_visible("#trading-lock-reason"):
            page.fill("#trading-lock-reason", "P1-D browser validation")
        page.click("#app-confirm-ok")
        page.wait_for_selector("#trading-lock-pill:not([hidden])", timeout=10000)
        open_card_dialog(page, tid11)
        expect_409("TRADING_LOCKED")
        approve_in_dialog(page)
        page.wait_for_selector(".toast.warning", timeout=15000)
        warning = page.inner_text(".toast.warning")
        t11 = ticket(tid11)
        check(11, "with trading locked the approval is refused, explained, and nothing is sent",
              "locked" in warning.lower() and t11["status"] == "pending_confirmation", warning)
        check(11, "the lock does not invalidate the review", t11["review"]["current"], t11["review"])
        close_toasts(page)

        # ---------------------------------------------------------------- 10: exits stay available (lock ON)
        boot(page)
        sell_ok = False
        chips = page.locator(".alert-chip")
        for i in range(chips.count()):
            chips.nth(i).click()
            page.wait_for_timeout(600)
            if page.is_visible("#quick-action-dialog") and page.is_visible("#qa-sell-section"):
                page.fill("#qa-sell-qty", "1")
                page.fill("#qa-sell-rationale", "P1-D browser validation (Quick Action sell)")
                page.click("#qa-sell-submit")
                page.wait_for_selector("#ticket-confirm-dialog[open]", timeout=15000)
                d = dialog_state(page)
                approve_in_dialog(page)
                page.wait_for_selector(".toast.success", timeout=15000)
                sell = api_get("/api/tickets?source=alert_action")["data"][0]
                sell_ok = sell["transaction_type"] == "SELL" and sell["status"] == "executed" and not d["note"]
                break
            if page.is_visible("#quick-action-dialog"):
                page.keyboard.press("Escape")
        check(10, "a Quick-Action SELL is approved while trading is locked (no review asked)", sell_ok)
        close_toasts(page)
        boot(page, "positions")
        exit_btn = page.locator('button[data-action="position-exit"]').first
        if exit_btn.count():
            exit_btn.click()
            page.wait_for_selector("#ticket-confirm-dialog[open]", timeout=15000)
            d = dialog_state(page)
            approve_in_dialog(page)
            page.wait_for_selector(".toast.success", timeout=15000)
            check(10, "an ordinary position exit is approved while trading is locked (no review asked)",
                  not d["note"], d)
        else:
            check(10, "an ordinary position exit is approved while trading is locked", False, "no open position")
        close_toasts(page)
        boot(page)
        page.click("#emergency-exit-all-btn")
        page.wait_for_selector("#emergency-exit-dialog[open]", timeout=10000)
        page.fill("#emergency-exit-phrase", "EXIT ALL")
        page.click("#emergency-exit-confirm-btn")
        page.wait_for_selector(".toast", timeout=20000)
        page.wait_for_timeout(800)
        toasts = " | ".join(page.locator(".toast").all_inner_texts())
        check(10, "Emergency Exit All runs while trading is locked and reviews are stale",
              ("exit" in toasts.lower()) and not re.search(r"locked|re-review|TICKET_", toasts, re.I),
              toasts[:200])
        close_toasts(page)

        boot(page)
        page.click("#trading-lock-btn")                        # unlock
        page.wait_for_timeout(400)
        if page.is_visible("#app-confirm-dialog"):
            if page.is_visible("#trading-lock-reason"):
                page.fill("#trading-lock-reason", "P1-D browser validation done")
            page.click("#app-confirm-ok")
        page.wait_for_selector("#trading-lock-pill", state="hidden", timeout=10000)
        open_card_dialog(page, tid11)
        approve_in_dialog(page)
        page.wait_for_selector(".toast.success", timeout=15000)
        page.wait_for_timeout(600)
        t11 = ticket(tid11)
        check(11, "after unlocking, the same (still current) review approves as a paper order",
              t11["status"] == "executed" and str(t11["kite_order_id"] or "").startswith("PAPER"), t11["status"])
        close_toasts(page)

        # ---------------------------------------------------------------- 12: market window
        tid12 = create_manual(page, qty="3")
        page.keyboard.press("Escape")
        later = set_clock([page], time="15:31")
        boot(page, "approvals")
        card = page.locator(f'.approval-card[data-ticket-id="{tid12}"]')
        check(12, f"after the 15:30 close ({later.time()}) the card says 'needs re-review'",
              card.count() == 1 and "needs re-review" in card.inner_text())
        card.locator('button[data-action="details"]').click()
        page.wait_for_selector("#ticket-confirm-dialog[open]", timeout=10000)
        d = dialog_state(page)
        check(12, "the dialog explains the session window and offers Re-review",
              "session" in d["note"] and d["rereview"] and not d["approve"], d)
        page.screenshot(path=str(out / "p1d-12-window.png"))
        page.keyboard.press("Escape")
        expect_409("TICKET_REVIEW_REQUIRED")
        status, code = confirm_from_page(page, tid12, {"expected_mode": "dry_run", "allow_amo": True,
                                                        "expected_review_token": ticket(tid12)["review"]["token"]})
        check(12, "an approval sent anyway is refused (TICKET_REVIEW_REQUIRED) and nothing changes",
              (status, code) == (409, "TICKET_REVIEW_REQUIRED") and ticket(tid12)["status"] == "pending_confirmation",
              (status, code))
        set_clock([page], ist=None)

        browser.close()

    # ---------------------------------------------------------------- expected refusals / errors
    for suffix, code in page_read_codes:
        for entry in observed_409:
            if entry["code"] is None and entry["url"].endswith(suffix):
                entry["code"] = code
                break
    observed_codes = [o["code"] for o in observed_409]
    unexpected = list(observed_codes)
    for code in expected_409:
        if code in unexpected:
            unexpected.remove(code)
    missing = [c for c in expected_409 if c not in observed_codes]
    resource_409 = [e for e in console_errors if "status of 409" in e or "status of 401" in e]
    other_console = [e for e in console_errors if "status of 409" not in e and "status of 401" not in e]
    print()
    print(f"Expected refusals (401/409) declared: {expected_409}")
    print(f"Refusals observed:                     {observed_codes}")
    ok_http = not unexpected and not missing and len(resource_409) <= len(observed_409)
    print(f"{'PASS' if ok_http else 'FAIL'} every refusal was declared and observed "
          f"(unexpected {unexpected}, missing {missing}, console 401/409 lines {len(resource_409)})")
    ok_errors = not other_console and not page_errors and not unexpected_http
    print(f"{'PASS' if ok_errors else 'FAIL'} no other console error, page error or HTTP error "
          f"{(other_console + page_errors + unexpected_http)[:4]}")

    print()
    summary = {}
    for n, name in CHECKS.items():
        entries = results[n]
        passed = bool(entries) and all(ok for _, ok, _ in entries)
        summary[n] = passed
        print(f"Check {n:>2}: {'PASS' if passed else 'FAIL'}  {name}  ({sum(ok for _, ok, _ in entries)}/{len(entries)})")
    (out / "e2e_p1d_results.json").write_text(json.dumps({
        "checks": {n: {"name": CHECKS[n], "passed": summary[n], "assertions": results[n]} for n in CHECKS},
        "expected_409": expected_409, "observed_409": observed_409, "http_ok": ok_http, "errors_ok": ok_errors,
    }, indent=2), encoding="utf-8")
    print(f"{sum(summary.values())}/{len(CHECKS)} checks passed")
    sys.exit(0 if all(summary.values()) and ok_http and ok_errors else 1)


if __name__ == "__main__":
    main()
