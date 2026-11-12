"""PK-19: the frontend DISPLAYS the server's lifecycle and decides nothing.

Real API payloads (built through the real flows on FakeKite) are rendered by
the real frontend code — frontend/lifecycle.js, plus the ticket rendering
functions extracted verbatim from frontend/app.js — in Node
(tests/js/render_lifecycle.js), and the HTML is asserted. Static checks keep
app.js from growing its own lifecycle rules again.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import approval_body, set_mode
from test_baseline_api import _insert_ticket
from test_live_safety import _monitor, market_open  # noqa: F401 — market_open: fixture
from test_partial_fills import _fill, _held_position, _place_entry, _place_exit, _reconcile

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "frontend" / "app.js"
LIFECYCLE_JS = ROOT / "frontend" / "lifecycle.js"
INDEX = ROOT / "frontend" / "index.html"
HARNESS = ROOT / "tests" / "js" / "render_lifecycle.js"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed; the renderers cannot be run")

# app.js functions/constants that render tickets — extracted verbatim.
_APP_FUNCTIONS = ("esc", "fmt", "statusBadgeHtml", "positionChipHtml", "quantityCellHtml", "positionDetailHtml",
                  "isSubmissionUnresolved")
_APP_CONSTS = ("_ESC", "STATUS_LABELS")


def _app_prelude() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    parts = []
    for name in _APP_CONSTS:
        m = re.search(rf"^const {name} = .*?;$", source, re.M | re.S)
        assert m, f"const {name} not found in app.js"
        parts.append(m.group(0))
    for name in _APP_FUNCTIONS:
        m = re.search(rf"^function {name}\(.*?^}}$", source, re.M | re.S)
        assert m, f"function {name} not found in app.js"
        parts.append(m.group(0))
    return "\n".join(parts)


def _render(*calls):
    r = subprocess.run([NODE, str(HARNESS)], input=json.dumps({"prelude": _app_prelude(), "calls": list(calls)}),
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _position(env, pid, view="active"):
    data = env.client.get(f"/api/app-positions?view={view}").json()["data"]
    return next(p for p in data if p["position_id"] == pid)


def _ticket(env, tid):
    return env.client.get(f"/api/tickets/{tid}").json()["data"]


def _buttons(html) -> list[str]:
    return re.findall(r'data-action="([^"]+)"', html)


# ================================================================ badges

@needs_node
def test_open_badge_and_exit_button(env, market_open):
    buy_id, order_id, _ = _place_entry(env, 100)
    _fill(env, order_id, 100, status="COMPLETE")
    _monitor(env)
    p = _position(env, buy_id)
    badge, buttons, prot = _render(["lifecycleBadge", p], ["actionButtons", p], ["protectionBadge", p])
    assert 'data-lifecycle="OPEN"' in badge and ">OPEN<" in badge
    assert "EXECUTED" not in badge.upper()
    assert _buttons(buttons) == ["position-exit", "position-detail"]
    assert 'data-protection="PROTECTED"' in prot


@needs_node
def test_exiting_badge_never_says_closed(env, market_open):
    buy_id, _ = _held_position(env, 40)
    sell_id, _ = _place_exit(env, buy_id, 40)
    p = _position(env, buy_id)
    badge, buttons, exit_cell = _render(["lifecycleBadge", p], ["actionButtons", p], ["activeExitCell", p])
    assert 'data-lifecycle="EXITING"' in badge and "CLOSED" not in badge
    assert "position-exit" not in _buttons(buttons)
    assert f"Exit #{sell_id}" in exit_cell and 'data-active-exit="WORKING"' in exit_cell
    # The exit TICKET: its own state and its position's, never "executed"/"closed".
    t = _ticket(env, sell_id)
    [ts, chip] = _render(["statusBadgeHtml", t["status"], t], ["positionChipHtml", t])
    assert "EXITING" in ts and ">executed<" not in ts.lower()
    assert 'data-lifecycle="EXITING"' in chip


@needs_node
def test_unknown_warning_says_do_not_resubmit(env, market_open):
    import requests
    set_mode(env.db, "live")
    from test_baseline_api import _manual_ticket
    from conftest import displayed_mode
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    env.kite.raise_on_next("place_order", requests.ReadTimeout("Read timed out."))
    env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t['id']))
    p = _position(env, t["id"])
    badge, buttons, row = _render(["lifecycleBadge", p], ["actionButtons", p], ["activeRowCells", p, None])
    assert "DO NOT RESUBMIT" in badge and "lc-sev-critical" in badge and "⛔" in badge
    assert _buttons(buttons) == ["position-detail"]
    [ts, unresolved] = _render(["statusBadgeHtml", "confirmed", _ticket(env, t["id"])],
                               ["isSubmissionUnresolved", _ticket(env, t["id"])])
    assert "UNKNOWN — DO NOT RESUBMIT" in ts and unresolved is True


@needs_node
def test_partial_entry_quantity(env, market_open):
    buy_id, order_id, _ = _place_entry(env, 100)
    _fill(env, order_id, 40)
    _monitor(env)
    working = _position(env, buy_id)
    _fill(env, order_id, 40, status="CANCELLED")
    _monitor(env)
    ended = _position(env, buy_id)
    q_working, q_ended, cell = _render(["quantityCell", working], ["quantityCell", ended],
                                       ["quantityCellHtml", _ticket(env, buy_id)])
    assert "40 / 100 filled" in q_working and "order still working" in q_working
    assert 'data-qty="open">40<' in q_ended and "40 / 100 filled" in q_ended
    assert "Req 100 · Filled 40 · Open 40" in cell


@needs_node
def test_partial_exit_quantity(env, market_open):
    buy_id, _ = _held_position(env, 40)
    sell_id, order_id = _place_exit(env, buy_id, 40)
    _fill(env, order_id, 15, price=3100.0)
    _monitor(env)
    p = _position(env, buy_id)
    qty, exit_qty = _render(["quantityCell", p], ["quantityCellHtml", _ticket(env, sell_id)])
    assert "25 remaining" in qty and "15 exited" in qty
    assert "Exit 40 · Filled 15 · Remaining 25" in exit_qty


@needs_node
def test_unprotected_state_is_text_and_icon_not_just_colour(env):
    buy_id, _ = _held_position(env, 40, kite_gtt_id=None)
    p = _position(env, buy_id)
    prot, review = _render(["protectionBadge", p], ["reviewCell", p])
    assert "NOT PROTECTED" in prot and "⛔" in prot and 'data-protection="NOT_PROTECTED"' in prot
    assert 'data-protection-reason="NO_GTT"' in prot and "no protective GTT" in prot
    assert "NEEDS REVIEW" in review


@needs_node
def test_closed_history_row(env, market_open):
    buy_id, _ = _held_position(env, 40)
    _, order_id = _place_exit(env, buy_id, 40)
    _fill(env, order_id, 40, status="COMPLETE", price=3150.0)
    _monitor(env)
    p = _position(env, buy_id, "closed")
    [row] = _render(["closedRowCells", p])
    assert 'data-lifecycle="CLOSED"' in row and "3,150.00" in row
    assert "+₹6,000.00" in row  # (3150 - 3000) x 40, the journal's realised P&L
    assert _buttons(row) == ["position-journal", "position-detail"]


@needs_node
def test_buttons_follow_allowed_actions_exactly(env, market_open):
    buy_id, order_id, _ = _place_entry(env, 100)
    _fill(env, order_id, 30)
    _monitor(env)
    p = _position(env, buy_id)  # ENTRY_PARTIAL: cancel the rest, no Exit
    [buttons] = _render(["actionButtons", p])
    rendered = set(_buttons(buttons))
    mapping = {"EXIT": "position-exit", "REVIEW_EXIT": "position-review-exit", "CANCEL_ORDER": "position-orders",
               "VIEW_RECONCILIATION": "position-reconciliation", "VIEW_JOURNAL": "position-journal",
               "VIEW_DETAIL": "position-detail"}
    assert rendered == {mapping[a] for a in p["allowed_actions"] if a in mapping}
    assert "position-exit" not in rendered and "position-orders" in rendered


@needs_node
def test_mode_is_displayed_from_the_position(env):
    dry_id = _insert_ticket(env.db, mode="dry_run", entry_fill_status="filled", filled_quantity=5,
                            filled_price=3000.0, kite_gtt_id=None)
    set_mode(env.db, "live")  # the global switch says LIVE ...
    [row] = _render(["activeRowCells", _position(env, dry_id), 3100.0])
    assert 'data-mode="dry_run"' in row and "DRY RUN" in row and 'data-mode="live"' not in row  # ... the row does not


@needs_node
def test_reconciliation_issue_is_visible(env):
    buy_id, _ = _held_position(env, 40, gtt_quantity=100)
    _reconcile()
    p = _position(env, buy_id)
    review, prot, detail = _render(["reviewCell", p], ["protectionBadge", p],
                                   ["positionDetailHtml", _ticket(env, buy_id)])
    assert "GTT quantity too high" in review and 'data-issue="GTT_QUANTITY_TOO_HIGH"' in review
    assert "NOT PROTECTED" in prot and "GTT could sell more than is held" in prot
    assert "covers 100; confirmed filled 40, held 40" in prot
    assert "Position" in detail and "NEEDS REVIEW" in detail


@needs_node
def test_unrealised_pnl_uses_the_open_quantity_and_the_fill_price(env, market_open):
    buy_id, _ = _held_position(env, 40)
    sell_id, order_id = _place_exit(env, buy_id, 40)
    _fill(env, order_id, 15, status="CANCELLED", price=3100.0)
    _monitor(env)
    p = _position(env, buy_id)  # 25 open, filled at 3000
    [pnl] = _render(["unrealisedPnl", p, 3010.0])
    assert pnl == pytest.approx(250.0)
    working = _insert_ticket(env.db, entry_fill_status="pending", filled_quantity=0, kite_order_id="W1")
    [none] = _render(["unrealisedPnl", _position(env, working), 3010.0])
    assert none is None  # nothing held yet: no P&L


# ============================================================ static checks

def test_index_loads_lifecycle_js_before_app_js_from_self():
    html = INDEX.read_text(encoding="utf-8")
    lc = html.index('<script src="/static/lifecycle.js"></script>')
    assert lc < html.index('<script src="/static/app.js"></script>')
    assert (ROOT / "frontend" / "lifecycle.js").exists()


# Raw columns a lifecycle rule would read. The server decides from them;
# the frontend must not (kite_gtt_id may still be DISPLAYED as an id).
_RAW_LIFECYCLE_FIELDS = ("entry_fill_status", "exit_state", "submission_state", "closed_at",
                         "exit_in_flight_ticket_id")


def _code(path: Path) -> str:
    """The script without its comments (which may name the fields)."""
    text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
    return "\n".join(line.split("//")[0] if line.lstrip().startswith("//") else line for line in text.splitlines())


@pytest.mark.parametrize("field", _RAW_LIFECYCLE_FIELDS)
def test_app_js_no_longer_derives_lifecycle_from_raw_columns(field):
    assert field not in _code(APP_JS), f"app.js reads {field}"


@pytest.mark.parametrize("field", ("entry_fill_status", "exit_state", "submission_state", "exit_in_flight"))
def test_lifecycle_js_only_renders_server_fields(field):
    assert field not in _code(LIFECYCLE_JS)


def test_status_badge_is_the_servers_ticket_state():
    body = re.search(r"^function statusBadgeHtml\(.*?^}$", APP_JS.read_text(encoding="utf-8"), re.M | re.S).group(0)
    assert "LC.ticketStateBadge(ticket.ticket_state)" in body
    assert "status ===" not in body and ".status ==" not in body
