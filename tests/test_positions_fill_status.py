"""Positions list: broker acceptance and actual fill, shown separately (SEC-1 P1-E, UI).

Real positions built through the real create -> confirm -> order-monitor flows on FakeKite, read
from GET /api/app-positions (`entry_progress`, `entry_fill_price`), rendered by the real
frontend/lifecycle.js in Node. An acknowledgement is never shown as a fill, and a quantity or
price the server does not report is never invented.
"""
import re

import pytest

from test_baseline_api import _insert_ticket
from test_lifecycle_ui import INDEX, LIFECYCLE_JS, _position, _render, needs_node
from test_live_safety import _monitor, market_open  # noqa: F401 - fixture
from test_partial_fills import PRICE, _fill, _place_entry

pytestmark = needs_node


def _cell(row_html: str) -> str:
    m = re.search(r'<td data-field="fill">(.*?)</td>', row_html, re.S)
    assert m, row_html
    return m.group(1)


def _fill_attrs(html: str) -> tuple[str, str]:
    return (re.search(r'data-acceptance="([A-Z_]+)"', html).group(1), re.search(r'data-fill="([A-Z_]+)"', html).group(1))


def test_accepted_but_not_filled_says_so_and_shows_no_price(env, market_open):
    tid, order_id, _ = _place_entry(env, quantity=100)
    p = _position(env, tid)
    assert p["entry_progress"]["acceptance"] == "ACCEPTED" and p["entry_progress"]["fill"] == "AWAITING_FILL"
    [row] = _render(["activeRowCells", p, None])
    cell = _cell(row)
    assert _fill_attrs(cell) == ("ACCEPTED", "AWAITING_FILL")
    assert "Broker: <strong>accepted</strong>" in cell and "accepted, not filled" in cell
    assert "0/100" in cell and "avg" not in cell and "order working" in cell
    assert "fully filled" not in cell


def test_partially_filled_shows_the_broker_quantity_and_average_price(env, market_open):
    tid, order_id, _ = _place_entry(env, quantity=100)
    _fill(env, order_id, 40, status="OPEN", price=PRICE - 1.5)
    _monitor(env)
    p = _position(env, tid)
    assert p["entry_progress"]["fill"] == "PARTIAL" and p["entry_progress"]["filled_quantity"] == 40
    [row] = _render(["activeRowCells", p, None])
    cell = _cell(row)
    assert _fill_attrs(cell) == ("ACCEPTED", "PARTIAL")
    assert "partly filled" in cell and "40/100" in cell
    # The average is shown only when the server reports one; a working order's isn't recorded yet.
    if p["entry_fill_price"] is None:
        assert "avg price not reported" in cell
    else:
        assert f"avg ₹{p['entry_fill_price']:,.2f}" in cell


def test_fully_filled(env, market_open):
    tid, order_id, _ = _place_entry(env, quantity=10)
    _fill(env, order_id, 10, status="COMPLETE")
    _monitor(env)
    p = _position(env, tid)
    [row] = _render(["activeRowCells", p, None])
    cell = _cell(row)
    assert _fill_attrs(cell) == ("ACCEPTED", "FILLED")
    assert p["entry_fill_price"] == PRICE
    assert "fully filled" in cell and "10/10" in cell and f"avg ₹{PRICE:,.2f}" in cell


@pytest.mark.parametrize("status,fill", [("CANCELLED", "CANCELLED"), ("REJECTED", "REJECTED")])
def test_cancelled_or_rejected_after_acceptance(env, market_open, status, fill):
    tid, order_id, _ = _place_entry(env, quantity=10)
    _fill(env, order_id, 0, status=status)
    _monitor(env)
    from conftest import ticket_row
    row_db = ticket_row(env.db, tid)
    from backend import lifecycle
    progress = lifecycle.order_progress(row_db)
    assert (progress["acceptance"], progress["fill"]) == ("ACCEPTED", fill)
    lc = {"entry_progress": progress, "entry_fill_price": None}
    [cell] = _render(["entryProgressCell", lc])
    assert _fill_attrs(cell) == ("ACCEPTED", fill) and "avg" not in cell and "fully filled" not in cell


def test_unknown_submission_is_never_shown_as_filled(env, market_open):
    tid = _insert_ticket(env.db, status="confirmed", mode="live", submission_state="unknown", order_tag="KT1x",
                         submitted_at="2026-09-01 04:00:00", kite_order_id=None)
    p = _position(env, tid)
    assert p["entry_progress"]["acceptance"] == "UNKNOWN" and p["entry_progress"]["filled_quantity"] is None
    [row] = _render(["activeRowCells", p, None])
    cell = _cell(row)
    assert _fill_attrs(cell) == ("UNKNOWN", "UNKNOWN")
    assert "do not resubmit" in cell and "fill unknown" in cell and "/" not in re.sub(r"<[^>]+>", "", cell)


def test_missing_progress_is_shown_as_unavailable_never_guessed():
    [cell] = _render(["entryProgressCell", {"entry_progress": None, "entry_fill_price": 1234.5}])
    assert _fill_attrs(cell) == ("UNAVAILABLE", "UNAVAILABLE") and "1,234" not in cell
    [cell] = _render(["entryProgressCell", {"entry_progress": {"acceptance": "ACCEPTED"}, "entry_fill_price": 1.0}])
    assert "unavailable" in cell


def test_a_filled_quantity_without_a_reported_price_says_so():
    p = {"acceptance": "ACCEPTED", "fill": "PARTIAL", "filled_quantity": 4, "requested_quantity": 10,
         "working": False, "simulated": False, "fill_evidence": "BROKER"}
    [cell] = _render(["entryProgressCell", {"entry_progress": p, "entry_fill_price": None}])
    assert "avg price not reported" in cell and "4/10" in cell


def test_paper_and_legacy_records_are_labelled():
    base = {"acceptance": "ACCEPTED", "fill": "FILLED", "filled_quantity": 5, "requested_quantity": 5,
            "working": False}
    [paper, legacy] = _render(
        ["entryProgressCell", {"entry_progress": {**base, "simulated": True, "fill_evidence": "BROKER"},
                               "entry_fill_price": 100.0}],
        ["entryProgressCell", {"entry_progress": {**base, "simulated": False, "fill_evidence": "LEGACY_RECORD"},
                               "entry_fill_price": 100.0}])
    assert "Paper broker:" in paper and "legacy record, not broker-verified" in legacy


def test_every_server_value_has_a_label_and_the_column_exists_in_both_tables():
    from backend import lifecycle
    acc = {v for k, v in vars(lifecycle).items() if k.startswith("ACC_")}
    fill = {v for k, v in vars(lifecycle).items() if k.startswith("FILL_")}
    src = LIFECYCLE_JS.read_text(encoding="utf-8")
    for v in acc | fill:
        assert re.search(rf"\b{v}:", src), v
    html = INDEX.read_text(encoding="utf-8")
    assert html.count(">Broker / fill</th>") == 2


def test_the_closed_table_shows_the_fill_too(env, market_open):
    tid, order_id, _ = _place_entry(env, quantity=10)
    _fill(env, order_id, 10, status="COMPLETE")
    _monitor(env)
    p = _position(env, tid)
    [row] = _render(["closedRowCells", p])
    assert _fill_attrs(_cell(row)) == ("ACCEPTED", "FILLED")
