"""PK-26 — the Price-Volume Research UI (frontend/research.js).

Real scanner payloads (built through the real API on FakeKite) are run through
the real frontend code in Node (tests/js/render_research.js): its filtering,
preset and sorting logic must agree with the server's exactly, and the
rendered page must carry PK-25's LIMITED status and the disclaimer, present
the score as a plain number (never a band or cue) and default to the
server's symbol order. Static checks cover the markup and CSP safety.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from backend import research_scanner as rs

from test_research_scanner import (_high_score_candles, detail, scan, varied, watch,  # noqa: F401 — fixture
                                   walk_candles)
from test_signal_detail import TCS_TOKEN, INFY_TOKEN

ROOT = Path(__file__).resolve().parents[1]
RESEARCH_JS = ROOT / "frontend" / "research.js"
HARNESS = ROOT / "tests" / "js" / "render_research.js"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed; the UI code cannot be run")

ADVICE_WORDS = ("buy", "sell", "top pick", "best", "recommend", "bullish", "bearish", "opportunit", "rocket",
                "strong", "institution", "smart money buying", "trade score", "composite score")


def run(*calls):
    r = subprocess.run([NODE, str(HARNESS)], input=json.dumps({"calls": list(calls)}), capture_output=True,
                       text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _code_only(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("//"))


def _visible(html: str) -> str:
    """Text a reader sees: the HTML emphasis tag <strong> is markup, not a word."""
    return re.sub(r"</?strong>", "", html)


# ---------------------------------------------------------------- logic parity with the server

@needs_node
def test_ui_presets_and_filters_select_exactly_the_server_rows(varied):
    data = scan(varied)
    cases = [{"preset": p["key"]} for p in data["presets"]] + [
        {"min_rvol": 1.2}, {"min_volume_percentile": 60, "obv": "rising"}, {"max_distribution_10": 0},
        {"volume": "contracting"}, {"breakout": "absent", "min_close_location": 0.3}, {"sms_min": 50},
        {"pullback": "low_volume"}, {"signal": "none"},
    ]
    calls = [["conditionsFromValues", c, data] for c in cases]
    js_conditions = run(*calls)
    for params, conditions in zip(cases, js_conditions):
        server = scan(varied, **params)
        assert conditions == server["applied"]["conditions"], params
        kept = run(*[["matches", r, conditions] for r in data["rows"]])
        assert [r["instrument_token"] for r, k in zip(data["rows"], kept) if k] == sorted(
            [r["instrument_token"] for r in server["rows"]],
            key=lambda t: [r["instrument_token"] for r in data["rows"]].index(t)), params


@needs_node
def test_ui_sort_equals_server_sort_for_every_sortable_column(varied):
    data = scan(varied)
    for key in data["sort"]["sortable"]:
        for order in ("asc", "desc"):
            js = run(["sortRows", data["rows"], key, order])[0]
            server = scan(varied, sort=key, order=order)["rows"]
            assert [r["instrument_token"] for r in js] == [r["instrument_token"] for r in server], (key, order)


def test_ui_default_sort_comes_from_the_server_not_the_score():
    code = _code_only(RESEARCH_JS.read_text(encoding="utf-8"))
    assert "state.sort = { ...state.data.sort.default }" in code
    assert not re.search(r"sort\s*=\s*\{\s*key:\s*\"sms_score\"", code)
    assert "DEFAULT_SORT" not in code and "sort.default" in code        # no client-side default of its own


# ---------------------------------------------------------------- rendered wording

@needs_node
def test_ui_shows_the_limited_status_and_the_disclaimer(varied):
    data = scan(varied)
    notice, = run(["noticeHtml", data])
    assert rs.SMS_LIMITATION in notice
    assert "Validation status: LIMITED — no incremental value beyond strategy was established." in notice
    assert "Price-volume evidence, not proof of institutional buying." in notice
    d = detail(varied, 1_000_001)
    html, = run(["detailHtml", d])
    for text in (rs.SMS_LIMITATION, rs.SMS_VALIDATION["summary"], "Price-volume evidence, not proof of institutional buying."):
        assert text in html.replace("&#39;", "'")


@needs_node
def test_ui_score_filter_carries_the_inline_warning(varied):
    data = scan(varied)
    off, on = run(["filtersHtml", data, {}, []], ["filtersHtml", data, {"sms_min": "60"}, []])
    warning = "Score v1.0 did not show independent out-of-sample predictive value."
    assert warning in off and warning in on
    assert re.search(r'class="vr-inline-warn" hidden>', off) and not re.search(r'class="vr-inline-warn" hidden>', on)
    assert "off by default" in off


@needs_node
def test_ui_high_score_is_a_plain_number_without_a_trading_label(env):
    watch(env, "TCS", TCS_TOKEN, _high_score_candles())
    data = scan(env)
    row = data["rows"][0]
    assert row["sms_score"] >= 75
    d = detail(env, TCS_TOKEN)
    table, sms_block = run(["tableHtml", data["rows"], data["sort"]["default"]],
                           ["smsHtml", d["smart_money"], d["smart_money_display"], d["row"]])
    cell = re.search(r'<td class="num vr-sms">([^<]*)</td>', table).group(1)
    assert cell == f"{row['sms_score']:.1f}"
    shown = _visible(table + sms_block.replace(d["smart_money_display"]["disclaimer"], "")).lower()
    for word in ADVICE_WORDS:
        assert word not in shown, word
    for colour in ("pnl-positive", "pnl-negative", "success", "danger", "badge"):
        assert colour not in table + sms_block, colour


@needs_node
def test_ui_detail_shows_the_signal_beside_the_evidence(env):
    from test_signal_detail import _breakout_candles, _enable
    watch(env, "TCS", TCS_TOKEN, _breakout_candles())
    _enable(env.client, "momentum_breakout")
    env.client.post("/api/strategies/scan-now")
    rs.clear_cache()
    html, = run(["detailHtml", detail(env, TCS_TOKEN)])
    assert "Signal: <strong>momentum_breakout</strong>" in html
    assert "does not combine a signal and the evidence" in html
    assert "SMS v1.0" in html and "Validation status: LIMITED" in html


@needs_node
def test_ui_unavailable_row_shows_no_fabricated_values(env):
    watch(env, "TCS", TCS_TOKEN, walk_candles(1, n=12))
    watch(env, "INFY", INFY_TOKEN, walk_candles(2))
    data = scan(env)
    table, = run(["tableHtml", data["rows"], data["sort"]["default"]])
    tcs = re.search(r'<tr class="vr-row vr-unavailable" data-token="%d".*?</tr>' % TCS_TOKEN, table, re.S).group(0)
    assert "Unavailable" in tcs and not re.search(r"\d\.\d", re.sub(r"<[^>]+>", " ", tcs).replace("TCS", ""))
    html, = run(["detailHtml", detail(env, TCS_TOKEN)])
    assert "Volume evidence is unavailable" in html and "not available" in html


@needs_node
def test_ui_escapes_every_value(env):
    watch(env, "TCS", TCS_TOKEN, walk_candles(3))
    data = scan(env)
    row = {**data["rows"][0], "symbol": "<img src=x onerror=alert(1)>", "warnings": ['"><script>x</script>']}
    table, = run(["tableHtml", [row], data["sort"]["default"]])
    assert "<img" not in table and "<script>" not in table and "&lt;img" in table


def test_ui_source_has_no_advice_wording_or_pnl_colours():
    code = _visible(_code_only(RESEARCH_JS.read_text(encoding="utf-8"))).lower()
    for word in ADVICE_WORDS:
        assert word not in code, word
    for token in ("--pnl-", "pnl-positive", "pnl-negative", "btn-success", "btn-danger"):
        assert token not in code
    css = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")
    block = css[css.index("PK-26 price-volume research"):]
    assert "--pnl-" not in block and "--success" not in block and "--danger" not in block


def test_ui_page_markup_and_csp_safety():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'data-section-id="volume-research"' in html and 'id="view-volume-research"' in html
    view = html[html.index('id="view-volume-research"'):html.index('id="view-strategies"')]
    assert "<h1>Price-Volume Research</h1>" in view
    assert "Explore price-volume evidence across the watchlist." in view
    assert "smart money scanner" not in view.lower()
    assert "onclick" not in view and "<script" not in view and "style=" not in view
    assert html.index('src="/static/research.js"') < html.index('src="/static/app.js"')
    code = _code_only(RESEARCH_JS.read_text(encoding="utf-8"))
    assert " on" + "click=" not in code and "eval(" not in code and "new Function" not in code
    app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    assert 'window.KTResearch.load(document.getElementById("volume-research-root"))' in app_js


def test_ui_makes_one_batch_request_not_one_per_symbol():
    code = _code_only(RESEARCH_JS.read_text(encoding="utf-8"))
    fetches = re.findall(r"fetch\(`([^`]*)`", code)
    assert sorted(fetches) == sorted(["/api/research/volume-scan${q}",
                                      "/api/research/volume-scan/${encodeURIComponent(token)}${q}"])
    assert code.count("fetch(") == 2          # the batch, and one detail request per opened row
    assert "/api/volume-intelligence" not in code and "/api/smart-money-score" not in code
