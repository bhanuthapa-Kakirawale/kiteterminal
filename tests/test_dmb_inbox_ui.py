"""DMB Research Inbox UI (frontend/dmb_inbox.js) run in Node, plus the static wiring in
index.html / app.js: the disclaimer is always on screen, every value is escaped, the evidence
direction is labelled as evidence, nothing scores or ranks, and Start ticket prefills only the
symbol, the exchange and the rationale."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import dmb_bundles as B

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "js" / "render_dmb_inbox.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed; the UI code cannot be run")
APP = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "frontend" / "dmb_inbox.js").read_text(encoding="utf-8")


def run(*calls):
    r = subprocess.run([NODE, str(HARNESS)], input=json.dumps({"calls": list(calls)}), capture_output=True,
                       text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _item(**over):
    p = B.packet("INFY")
    item = {"id": 7, "packet_id": p["packet_id"], "symbol": "INFY", "exchange": "NSE", "session_date": B.SESSION,
            "review_session_date": B.REVIEW, "evidence_as_of": p["evidence_as_of"], "generated_at": p["generated_at"],
            "imported_at": "2026-11-02 15:31:00", "bundle_id": "a" * 64, "synthetic": True,
            "superseded_by_item_id": None, "company": p["company"], "sector": p["sector"],
            "rationale": p["reasons"], "detector_ids": p["detector_ids"], "radar_state": p["radar_state"],
            "metrics": p["metrics"], "market_context": p["market_context"], "sector_context": p["sector_context"],
            "market_regime": p["market_regime"], "official_events": p["official_events"], "quality": p["quality"],
            "rights": p["rights"], "source_artifact_ids": p["source_artifact_ids"], "producer": p["producer"],
            "dismissed": False, "notes": [{"text": "<b>mine</b>", "at": "x"}], "on_research_watchlist": False,
            "ticket_links": [], "history": [{"event_type": "IMPORTED", "at": "x"}], "actionable": True, "reasons": [],
            "expires_at": "2026-11-03T15:30:00+05:30", "versions": []}
    item.update(over)
    return item


@needs_node
def test_detail_shows_evidence_provenance_and_the_disclaimer():
    [html] = run(["detailHtml", _item()])
    for text in ("PRIVATE RESEARCH CONTEXT — NOT A TRADE RECOMMENDATION OR ORDER INSTRUCTION.", "INFY", "NSE",
                 B.SESSION, B.REVIEW, "2026-11-02T19:45:00+05:30", "ALIGNED_POSITIVE", "not a trade side",
                 "SYNTHETIC: volume was 3.2x", "Information Technology", "MATCHED", "synthetic_handoff_fixture",
                 "PRIVATE_RESEARCH_ONLY", "a classification, not a permission", "PERMITTED", "SYNTHETIC",
                 "current version", "Start ticket", "Dismiss", "Add note", "research watchlist",
                 "not the strategy watchlist", "2026-11-02 15:31:00", "Market context", "Sector context",
                 "Historical evidence", "Evidence as of", "Review session", "Imported", "Supersession"):
        assert text in html, text
    assert "<b>mine</b>" not in html and "&lt;b&gt;mine&lt;/b&gt;" in html          # escaped
    assert not re.search(r"score|probability|expected return|\brank", html, re.I)


@needs_node
def test_a_non_actionable_item_cannot_start_a_ticket_and_says_why():
    [html, table] = run(["detailHtml", _item(actionable=False, reasons=["EXPIRED", "QUALITY_REPLAY_NOT_MATCHED"],
                                              superseded_by_item_id=9)],
                        ["itemsTableHtml", [_item(actionable=False, reasons=["SUPERSEDED"])]])
    assert re.search(r'data-dmb-start="7"\s+disabled', html)
    assert "the review session has completed" in html and "detector replay did not match" in html
    assert "superseded by item #9" in html
    assert "research only" in table and "a newer version" in table


@needs_node
def test_hostile_packet_text_is_escaped_everywhere():
    evil = "<img src=x onerror=alert(1)>"
    [html, table] = run(["detailHtml", _item(company=evil, rationale=[evil], symbol=evil)],
                        ["itemsTableHtml", [_item(symbol=evil)]])
    assert evil not in html and evil not in table


@needs_node
def test_import_listing_needs_an_explicit_click():
    [off, listed] = run(["bundlesHtml", {"configured": False, "data": [], "message": "set it"}],
                        ["bundlesHtml", {"configured": True, "data": [{"session_date": B.SESSION, "bundle_id": "b" * 64,
                                                                         "size": 4096, "imported": False}]}])
    assert "Import is off" in off and "data-dmb-import" not in off
    assert f'data-dmb-import="{B.SESSION}|{"b" * 64}"' in listed


def test_the_view_is_wired_and_the_disclaimer_is_static():
    assert 'data-section-id="dmb-inbox"' in HTML and 'id="view-dmb-inbox"' in HTML
    assert '<script src="/static/dmb_inbox.js"></script>' in HTML
    assert HTML.index("dmb_inbox.js") < HTML.index('src="/static/app.js"')
    assert '"dmb-inbox"' in APP and "KTDmbInbox.load" in APP
    assert "PRIVATE RESEARCH CONTEXT" in HTML      # the New Ticket handoff banner


def test_start_ticket_prefills_only_symbol_exchange_and_rationale():
    body = APP[APP.index("async function openTicketFromDmb"):APP.index("async function linkDmbTicket")]
    assert set(re.findall(r"\[name=\"(\w+)\"\]'\)\.value = start", body)) == {"rationale"}
    assert "el.ticketSymbolInput.value = start.prefill.symbol" in body
    assert len(re.findall(r"\.value = start\.", body)) == 2              # symbol + rationale, nothing else
    assert 'textContent = start.prefill.exchange' in body                 # exchange shown, NSE by contract
    # direction, order type and product start on an unselected placeholder that `required` refuses
    assert 'DMB_PLACEHOLDER_FIELDS = ["transaction_type", "entry_order_type", "product_type"]' in APP
    assert "select.insertBefore(placeholder, select.firstChild)" in body and 'select.value = ""' in body
    for name in ("transaction_type", "entry_order_type", "product_type"):
        assert re.search(rf'<select id="[^"]+" name="{name}" required>', HTML), name
    assert "state.quantityManuallyEdited = true" in body                 # no position-size auto-fill
    assert "radar_state" not in body and "direction" not in body          # evidence direction never used


def test_the_ticket_is_created_by_the_normal_path_then_linked():
    submit = APP[APP.index('el.ticketForm.addEventListener("submit"'):]
    submit = submit[:submit.index("\n});")]
    assert "API.createTicket(body)" in submit
    assert submit.index("API.createTicket(body)") < submit.index("linkDmbTicket(ticket)")
    assert "/api/dmb-inbox/" not in submit                  # the ticket body itself never touches the inbox


def test_the_inbox_ui_never_names_the_credential_or_places_orders():
    assert "kt_session" not in JS and "document.cookie" not in JS
    assert "/api/tickets" not in JS
