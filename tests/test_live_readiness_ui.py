"""LIVE-0 — the Live Readiness page (frontend/readiness.js).

Real pre-flight payloads (built by the real backend on FakeKite) are run
through the page's own view-model code in Node (tests/js/render_readiness.js):
NOT READY reasons are listed, WARN is kept apart from BLOCK, the disclaimer is
shown, and the first-live-order checklist appears only when READY — never
pre-ticked. Static checks cover the markup, CSP safety and the page's calls.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_live_preflight import _evaluate, ready  # noqa: F401 — fixture

ROOT = Path(__file__).resolve().parents[1]
READINESS_JS = ROOT / "frontend" / "readiness.js"
HARNESS = ROOT / "tests" / "js" / "render_readiness.js"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed; the UI code cannot be run")


def run(*calls):
    r = subprocess.run([NODE, str(HARNESS)], input=json.dumps({"calls": list(calls)}), capture_output=True,
                       text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _code_only(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("//"))


def _model(data):
    return run(["viewModel", json.loads(json.dumps(data, default=str))])[0]


@needs_node
def test_ready_shows_ready_disclaimer_and_an_unticked_checklist(ready):
    model = _model(_evaluate())
    assert model["verdict"]["title"] == "READY"
    assert model["disclaimer"] == "Operational readiness only. It does not assess investment merit."
    assert model["showChecklist"] is True
    assert [c["text"] for c in model["checklist"]] == [
        "Static IP verified in the Kite developer app", "Correct trading account confirmed",
        "Max order notional reviewed", "Daily loss limit reviewed", "Trading lock intentionally disabled",
        "Order quantity reviewed", "BUY + CNC confirmed", "Stop and target reviewed", "No unresolved broker order",
        "No critical reconciliation warning"]
    assert not any(c["checked"] for c in model["checklist"])        # never ticked for the user


@needs_node
def test_not_ready_lists_every_blocking_reason_and_hides_the_checklist(ready):
    _exec = __import__("test_live_preflight")._exec
    _exec(ready, "UPDATE risk_settings SET max_order_notional = NULL, max_daily_loss_amount = NULL")
    _exec(ready, "DELETE FROM live_readiness")
    model = _model(_evaluate())
    assert model["verdict"]["title"] == "NOT READY" and "3 blocking items" in model["verdict"]["text"]
    assert {b["code"] for b in model["blocking"]} == {"MAX_ORDER_NOTIONAL_SET", "DAILY_LOSS_AMOUNT_SET", "STATIC_IP"}
    assert {b["label"] for b in model["blocking"]} == {"FAIL", "ACTION REQUIRED"}
    assert model["showChecklist"] is False
    static_ip = next(s for s in model["sections"] if s["key"] == "static_ip")
    assert static_ip["rows"][0]["label"] == "ACTION REQUIRED" and static_ip["worst"] == "FAIL"
    safety = next(s for s in model["sections"] if s["key"] == "account_safety")
    notional = next(r for r in safety["rows"] if r["code"] == "MAX_ORDER_NOTIONAL_SET")
    assert notional["link"] == {"href": "#settings", "text": "Open Settings"}


@needs_node
def test_warnings_are_kept_apart_from_blockers(ready):
    model = _model(_evaluate())                     # the ticker is never connected in tests: a warning
    assert model["blocking"] == []
    assert "TICKER_CONNECTED" in {w["code"] for w in model["warnings"]}
    market = next(s for s in model["sections"] if s["key"] == "market_data")
    ticker = next(r for r in market["rows"] if r["code"] == "TICKER_CONNECTED")
    assert ticker["label"] == "WARN" and ticker["kind"] == "Advisory" and ticker["failing"] is False


@needs_node
def test_lock_only_verdict_says_to_unlock_deliberately(ready):
    from backend import account_safety
    from conftest import account_id
    conn = ready.db.get_connection()
    try:
        account_safety.set_trading_lock(conn, account_id(ready.db), True, "review")
    finally:
        conn.close()
    model = _model(_evaluate())
    assert model["verdict"]["tone"] == "lock" and "Unlock trading deliberately" in model["verdict"]["text"]
    assert model["showChecklist"] is False


@needs_node
def test_status_labels():
    assert run(["statusLabel", "USER_ACTION"], ["statusLabel", "PASS"], ["statusLabel", "FAIL"],
               ["statusLabel", "WARN"]) == ["ACTION REQUIRED", "PASS", "FAIL", "WARN"]


def test_page_markup_and_csp_safety():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'data-section-id="live-readiness"' in html and 'id="view-live-readiness"' in html
    view = html[html.index('id="view-live-readiness"'):html.index('id="view-settings"')]
    assert "<h1>Live Readiness</h1>" in view and 'id="live-readiness-root"' in view
    assert "onclick" not in view and "<script" not in view and "style=" not in view
    assert html.index('src="/static/readiness.js"') < html.index('src="/static/app.js"')
    diagnostics = html[html.index('id="view-diagnostics"'):html.index('id="view-live-readiness"')]
    assert 'href="#live-readiness"' in diagnostics
    code = _code_only(READINESS_JS.read_text(encoding="utf-8"))
    assert "innerHTML" not in code and "eval(" not in code and "new Function" not in code
    assert " on" + "click=" not in code and "https://" not in code
    app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    assert 'window.KTReadiness.load(document.getElementById("live-readiness-root"))' in app_js


def test_page_calls_only_the_readiness_endpoints_and_never_an_order_route():
    code = _code_only(READINESS_JS.read_text(encoding="utf-8"))
    fetched = set(re.findall(r"fetch\(`([^`$?]*)", code))
    posted = set(re.findall(r'post\("([^"]+)"', code))
    assert fetched == {"/api/live-readiness"}
    assert posted == {"/api/live-readiness/static-ip", "/api/live-readiness/static-ip/clear",
                      "/api/live-readiness/egress-check"}
    for forbidden in ("/api/tickets", "/confirm", "/api/execution/mode", "trading-lock", "/api/orders"):
        assert forbidden not in code


def test_cautious_profile_never_saves_or_invents_an_amount():
    app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    start = app_js.index('getElementById("first-live-profile-btn")')
    handler = app_js[start:app_js.index("el.sectorMappingForm.addEventListener", start)]
    assert "confirmAction(" in handler and "if (!ok) return;" in handler
    assert "saveRiskSettings" not in handler and "requestSubmit" not in handler and ".submit(" not in handler
    assert '[name="max_new_entries_per_day"]\').value = 1' in handler
    assert not re.search(r'max_order_notional"\]\'\)\.value\s*=', handler)
    assert not re.search(r'max_daily_loss_amount"\]\'\)\.value\s*=', handler)


# ---------------------------------------------------------------- NET-1: Kite REST route

@needs_node
def test_net1_route_is_shown_in_the_header_and_as_its_own_section(ready, monkeypatch):
    monkeypatch.setenv("KITE_REST_PROXY", "http://10.77.0.1:3128")
    model = _model(_evaluate())
    assert model["restRoute"] == {"mode": "PROXY", "endpoint": "10.77.0.1:3128"}
    assert model["meta"].endswith("· REST via PROXY 10.77.0.1:3128")
    section = next(s for s in model["sections"] if s["key"] == "network")
    assert section["title"] == "Kite REST Route" and section["worst"] == "PASS"
    assert [(r["code"], r["status"]) for r in section["rows"]] == [("KITE_REST_ROUTE", "PASS")]
    assert model["staticIp"]["egress_route"] == "PROXY" and model["staticIp"]["egress_proxy"] == "10.77.0.1:3128"


@needs_node
def test_net1_direct_and_invalid_route_texts():
    assert run(["restRouteText", {"mode": "DIRECT", "endpoint": None}],
               ["restRouteText", {"mode": "PROXY", "endpoint": "10.77.0.1:3128"}],
               ["restRouteText", {"mode": "INVALID", "endpoint": None}],
               ["restRouteText", None]) == ["REST DIRECT — LIVE disabled (proxy required)",
                                            "REST via PROXY 10.77.0.1:3128",
                                            "REST proxy setting INVALID", ""]


def test_net1_outbound_ip_button_names_the_proxy_route():
    code = _code_only(READINESS_JS.read_text(encoding="utf-8"))
    assert 'detail.egress_route === "PROXY"' in code
    assert "Verify outbound IP via the Kite REST proxy" in code
