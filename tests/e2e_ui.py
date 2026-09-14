"""Browser end-to-end check of the UI against tests/demo_server.py (FakeKite
+ throwaway database), driven through Playwright with the locally installed
Microsoft Edge. Saves screenshots and a JSON result file to --out.

    .venv/Scripts/python.exe tests/e2e_ui.py --base http://127.0.0.1:8765 --out <dir>
"""

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SECTIONS = ["dashboard", "approvals", "todays-actions", "trade-queue", "orders-trades", "new-ticket",
            "watchlist", "strategies", "journal", "ticket-history", "settings"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []

    def check(name: str, ok, detail: str = "") -> None:
        results.append({"check": name, "ok": bool(ok), "detail": str(detail)[:300]})
        print("PASS" if ok else "FAIL", name, detail if detail else "", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console: {m.text} @ {m.location.get('url', '')}")
                if m.type == "error" else None)

        def nav(section: str) -> None:
            page.click(f'.nav-item[data-section-id="{section}"]')
            page.wait_for_selector(f"#view-{section}:not([hidden])")

        page.goto(args.base + "/")
        page.wait_for_selector("#app-shell:not([hidden])", timeout=20000)
        page.wait_for_selector("#nav-badge-approvals:not([hidden])", timeout=20000)
        check("app loads; approvals badge shows pending count", True, page.inner_text("#nav-badge-approvals"))
        check("dashboard callout points to approvals", page.is_visible("#dash-approvals-callout"))
        page.wait_for_selector("#autopilot-pill:not([hidden])", timeout=20000)
        check("autopilot pill shows next run", "next" in page.inner_text("#autopilot-pill"), page.inner_text("#autopilot-pill"))
        page.wait_for_timeout(700)
        page.screenshot(path=str(out / "01-dashboard-dark.png"))

        for section in SECTIONS:
            nav(section)
            check(f"section '{section}' renders and routes", page.evaluate("location.hash") == f"#{section}")

        nav("approvals")
        page.wait_for_selector("#ap-entries .approval-card")
        entries = page.locator("#ap-entries .approval-card").count()
        check("inbox lists entry signals", entries == 2, entries)
        check("inbox lists the exit signal", page.locator("#ap-exits .approval-card").count() == 1)
        check("inbox lists the stop adjustment", page.locator("#ap-adjustments .approval-card").count() == 1)
        check("risk-blocked signal listed separately", page.locator("#ap-blocked .approval-card").count() == 1)
        check("autopilot card shows pipeline", "You approve or reject" in page.inner_text("#autopilot-card"))
        page.screenshot(path=str(out / "02-approvals.png"), full_page=True)

        card = page.locator("#ap-entries .approval-card").first
        symbol = card.locator(".ac-symbol").inner_text()
        card.locator('button[data-action="approve"]').click()
        page.wait_for_selector("#app-confirm-dialog[open]")
        check("approve asks for confirmation with execution note", "DRY RUN" in page.inner_text("#app-confirm-body"))
        page.screenshot(path=str(out / "03-approve-confirm.png"))
        page.click("#app-confirm-ok")
        page.wait_for_selector(".toast.success", timeout=15000)
        check("approval reports its outcome in a toast", symbol in page.inner_text(".toast.success"),
              page.inner_text(".toast.success"))
        page.wait_for_function("document.querySelectorAll('#ap-entries .approval-card').length === 1", timeout=15000)
        check("approved signal leaves the inbox", True)

        page.locator('#ap-entries button[data-action="reject"]').first.click()
        page.wait_for_function("document.querySelectorAll('#ap-entries .approval-card').length === 0", timeout=15000)
        check("rejected signal leaves the inbox", True)

        page.locator('#ap-adjustments button[data-action="approve-adj"]').click()
        page.wait_for_selector("#app-confirm-dialog[open]")
        page.click("#app-confirm-ok")
        page.wait_for_function("document.querySelectorAll('#ap-adjustments .approval-card').length === 0", timeout=15000)
        check("stop adjustment approved from the inbox", True)

        page.locator('#ap-exits button[data-action="details"]').click()
        page.wait_for_selector("#ticket-confirm-dialog[open]")
        check("details dialog shows risk-check summary",
              "risk checks" in page.inner_text("#ticket-risk-check-body").lower())
        page.screenshot(path=str(out / "04-exit-details.png"))
        page.keyboard.press("Escape")

        nav("trade-queue")
        page.click('.tab-btn[data-tq-tab="executed"]')
        check("approved ticket appears under Executed", symbol in page.inner_text("#tq-executed-table"))
        page.screenshot(path=str(out / "05-trade-queue.png"))

        nav("new-ticket")
        page.fill("#ticket-symbol", "ITC")
        page.wait_for_timeout(700)
        page.select_option("#ticket-entry-type", "LIMIT")
        page.fill("#ticket-entry-price", "415")
        page.fill("#ticket-stop-loss", "404")
        page.fill("#ticket-target-1", "440")
        page.fill("#ticket-rationale", "E2E manual ticket")
        page.wait_for_timeout(900)
        qty = page.input_value("#ticket-quantity")
        check("position size auto-filled from risk settings", qty.isdigit() and int(qty) > 0, qty)
        check("live capital/risk preview shown", page.is_visible("#ticket-preview"), page.inner_text("#ticket-preview"))
        page.screenshot(path=str(out / "06-new-ticket.png"))
        page.click('#ticket-form button[type="submit"]')
        page.wait_for_selector("#ticket-confirm-dialog[open]", timeout=15000)
        title = page.inner_text("#ticket-confirm-title")
        check("new ticket goes straight to the approval dialog", title in ("Approve trade", "Blocked by risk checks"), title)
        page.screenshot(path=str(out / "07-ticket-dialog.png"))
        page.keyboard.press("Escape")

        nav("strategies")
        check("strategy description shown", len(page.inner_text("#strategy-info")) > 40)
        page.click("#scan-now-btn")
        page.wait_for_function("document.querySelector('#scan-now-result').innerText.includes('evaluated')", timeout=30000)
        check("scan summary explains what was evaluated", True, page.inner_text("#scan-now-result"))
        page.screenshot(path=str(out / "08-strategies.png"))

        nav("journal")
        page.wait_for_selector("#journal-equity-curve svg", timeout=15000)
        check("journal equity curve renders", True)
        page.screenshot(path=str(out / "09-journal.png"))

        nav("orders-trades")
        page.wait_for_timeout(600)
        check("GTT book lists protective GTTs", page.locator("#gtts-table tbody tr").count() >= 2)
        page.screenshot(path=str(out / "10-orders.png"))

        nav("dashboard")
        page.click("#theme-toggle-btn")
        check("light theme applied", page.evaluate("document.documentElement.dataset.theme") == "light")
        page.wait_for_timeout(300)
        page.screenshot(path=str(out / "11-dashboard-light.png"))
        nav("approvals")
        page.screenshot(path=str(out / "12-approvals-light.png"), full_page=True)
        page.click("#theme-toggle-btn")

        page.evaluate("location.hash = '#journal'")
        page.wait_for_selector("#view-journal:not([hidden])", timeout=10000)
        check("changing the URL hash switches section", True)
        page.evaluate("location.hash = '#strategies'")
        page.reload()
        page.wait_for_selector("#view-strategies:not([hidden])", timeout=20000)
        check("deep link #strategies survives a reload", True)
        check("theme choice persisted across reload", page.evaluate("document.documentElement.dataset.theme") == "dark")

        nav("approvals")
        page.click('[data-ap-action="run-now"]')
        page.wait_for_selector(".toast-title:has-text('Autopilot run')", timeout=90000)
        check("autopilot run triggered from UI completes", True,
              page.locator(".toast-title:has-text('Autopilot run')").last.inner_text())
        page.wait_for_timeout(1200)
        runs_text = page.eval_on_selector("#ap-runs", "e => e.textContent")
        check("run log lists the manual run", "manual" in runs_text, " ".join(runs_text.split())[:140])

        page.click("#emergency-exit-all-btn")
        page.wait_for_selector("#emergency-exit-dialog[open]")
        check("emergency exit gated on typed phrase", page.is_disabled("#emergency-exit-confirm-btn"))
        page.fill("#emergency-exit-phrase", "EXIT ALL")
        check("emergency exit enabled after phrase", page.is_enabled("#emergency-exit-confirm-btn"))
        page.click("#emergency-exit-cancel-btn")

        page.set_viewport_size({"width": 820, "height": 1000})
        nav("dashboard")
        page.wait_for_timeout(300)
        page.screenshot(path=str(out / "13-narrow.png"))
        overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
        check("no horizontal page overflow at 820px", not overflow)

        check("no JavaScript errors", not errors, "; ".join(errors[:5]))
        browser.close()

    (out / "e2e_results.json").write_text(json.dumps(results, indent=2))
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
