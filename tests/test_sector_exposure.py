"""PK-14 (audit P1-4): sector mapping and the unknown-sector policy.

The sector limit used to be effectively off: sector_mapping was seeded only
from holdings, and an unmapped symbol passed the check as 'skipped'. Now
every watched, held and traded symbol is seeded from SECTOR_LOOKUP (never
over a manual mapping), the risk engine resolves an unseeded symbol through
the same lookup, and a symbol with no sector at all FAILS CLOSED for a new
entry (SECTOR_MAPPING_REQUIRED). Exits never reach the check.

Portfolios here hold stocks at average price == last price, so the figures
are the same on a cost or a market-value basis.
"""

import json
from datetime import datetime, timezone

import pytest

from conftest import add_watch, displayed_mode, set_mode, ticket_row
from test_baseline_api import _insert_ticket
from test_emergency_exit_isolation import _emergency_exit, _position
from test_live_safety import _set_risk, _unknown_ticket, market_open  # noqa: F401 — market_open is a fixture

SECTOR_RULE = "max_sector_exposure_pct"


def _now_utc() -> str:
    # Never a fixed date: some exposure rules look at "today".
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _portfolio(env, cash: float, **holdings) -> None:
    """Replaces the broker's portfolio: holdings as symbol=(quantity, price)."""
    env.kite._holdings = []
    env.kite._positions = []
    for symbol, (quantity, price) in holdings.items():
        env.kite.set_holding(symbol, quantity, average_price=price, last_price=price)
    env.kite.margins_net = cash


def _ticket(env, symbol="INFY", quantity=10, entry=1500.0, stop=1450.0, target=1650.0) -> dict:
    r = env.client.post("/api/tickets", json={
        "symbol": symbol, "exchange": "NSE", "transaction_type": "BUY", "quantity": quantity,
        "entry_price": entry, "entry_order_type": "LIMIT", "stop_loss": stop, "target_1": target,
        "target_2": None, "trailing_enabled": False, "trailing_trigger": None, "trailing_distance": None,
        "product_type": "CNC", "use_gtt": True, "rationale": "PK-14 test",
    })
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _check(ticket: dict, rule: str = SECTOR_RULE) -> dict:
    return next(c for c in ticket["risk_check_results"] if c["rule"] == rule)


def _mappings(env) -> dict:
    return {m["symbol"]: m["sector"] for m in env.client.get("/api/risk/sector-mappings").json()["data"]}


def _unmap(monkeypatch, symbol: str) -> None:
    """Takes `symbol` out of the static lookup for this test only."""
    from backend import sector_mapping
    monkeypatch.delitem(sector_mapping.SECTOR_LOOKUP, symbol)


# ---------------------------------------------------------------- 1-3, 11, 12: seeding

def test_1_startup_seeds_watchlist_and_ticket_symbols_from_the_lookup(env):
    from backend import sector_mapping
    add_watch(env.db, "TCS", 2953217)
    add_watch(env.db, "RECLTD", 3930881)
    add_watch(env.db, "NOSUCHCO", 1)                 # not in SECTOR_LOOKUP
    _insert_ticket(env.db, symbol="AWL", status="cancelled")

    assert sector_mapping.seed_known_symbols() == 3
    assert _mappings(env) == {"TCS": "IT", "RECLTD": "Financial Services", "AWL": "FMCG"}


def test_2_holdings_and_todays_positions_are_seeded(env):
    from backend import sector_mapping
    _portfolio(env, 1_000_000.0, HCLTECH=(10, 1500.0))
    env.kite.set_position("LTTS", 5)
    env.kite.set_position("NIFTY25SEPFUT", 50, product="NRML", exchange="NFO")  # no sector: ignored

    assert sector_mapping.seed_from_holdings(env.kite) == 2
    assert _mappings(env) == {"HCLTECH": "IT", "LTTS": "IT"}


def test_3_adding_a_watchlist_symbol_seeds_its_sector(env):
    r = env.client.post("/api/watchlist", json={"symbol": "RECLTD"})
    assert r.status_code == 201, r.text
    assert _mappings(env) == {"RECLTD": "Financial Services"}


def test_11_startup_seeding_is_idempotent_and_deterministic(env):
    from backend import sector_mapping
    for symbol, token in (("TCS", 2953217), ("INFY", 408065), ("SUNPHARMA", 857857)):
        add_watch(env.db, symbol, token)
    assert sector_mapping.seed_known_symbols() == 3
    first = env.client.get("/api/risk/sector-mappings").json()["data"]
    assert sector_mapping.seed_known_symbols() == 0
    assert sector_mapping.seed_from_holdings(env.kite) == 1   # RELIANCE, held in the default portfolio
    assert sector_mapping.seed_from_holdings(env.kite) == 0
    again = [m for m in env.client.get("/api/risk/sector-mappings").json()["data"] if m["symbol"] != "RELIANCE"]
    assert again == first


def test_12_a_manual_mapping_is_never_overwritten_by_any_seed(env):
    from backend import sector_mapping
    env.client.post("/api/risk/sector-mappings", json={"symbol": "TCS", "sector": "Tech Services"})
    add_watch(env.db, "TCS", 2953217)
    env.kite.set_holding("TCS", 5, average_price=3000.0, last_price=3000.0)

    sector_mapping.seed_known_symbols()
    sector_mapping.seed_from_holdings(env.kite)
    sector_mapping.seed_symbols(["TCS"])

    assert _mappings(env)["TCS"] == "Tech Services"
    assert sector_mapping.resolve_sectors(["TCS", "INFY"]) == {"TCS": "Tech Services", "INFY": "IT"}


def test_lookup_fixes_and_additions_resolve(env):
    from backend import sector_mapping
    assert sector_mapping.resolve_sectors(["INDUSTOWER", "TMPV", "BEL", "DMART", "tcs"]) == {
        "INDUSTOWER": "Telecom", "TMPV": "Auto", "BEL": "Infra", "DMART": "Consumer Services", "TCS": "IT"}
    assert "INDUS TOWERS" not in sector_mapping.SECTOR_LOOKUP


def test_every_watched_symbol_of_the_live_database_has_a_sector():
    """The 51 symbols on the live watchlist when PK-14 was written (read-only
    from the DB) plus the traded AWL: none may be left unmapped by the lookup."""
    from backend import sector_mapping
    live_watchlist = (
        "ADANIENT ADANIGREEN ADANIPORTS ADANIPOWER ASIANPAINT ATHERENERG AXISBANK BAJAJ-AUTO BAJAJFINSV BAJFINANCE "
        "BEL BHARTIARTL COALINDIA DIVISLAB DMART EICHERMOT ETERNAL FRACTAL GRASIM HAL HCLTECH HDFCBANK HINDALCO "
        "HINDUNILVR HINDZINC HYUNDAI ICICIBANK INDIGO INFY IOC ITC JSWSTEEL KOTAKBANK LTTS M&M MARUTI NESTLEIND "
        "NTPC ONGC POWERGRID RECLTD RELIANCE SHRIRAMFIN SOLARINDS SUNPHARMA TATASTEEL TCS TITAN TORNTPHARM "
        "TVSMOTOR ULTRACEMCO AWL").split()
    assert len(live_watchlist) == 52
    assert [s for s in live_watchlist if s not in sector_mapping.SECTOR_LOOKUP] == []


def test_unmapped_symbols_lists_watched_symbols_without_any_sector(env):
    add_watch(env.db, "TCS", 2953217)
    add_watch(env.db, "NOSUCHCO", 1)
    data = env.client.get("/api/risk/unmapped-symbols").json()["data"]
    assert data == ["NOSUCHCO"]  # TCS and the held RELIANCE resolve through the lookup


# ---------------------------------------------------------------- 4-6, 9, 10: the limit

def test_4_known_sector_below_the_limit_passes_and_shows_both_figures(env):
    _portfolio(env, 850_000.0, TCS=(50, 3000.0))           # portfolio 1,000,000; IT 15%
    check = _check(_ticket(env, "INFY", 10))              # +15,000 → 16.5%
    assert check["passed"] and check["label"] == "Sector exposure · IT"
    assert check["current_value"] == pytest.approx(15.0) and check["actual_value"] == pytest.approx(16.5)
    assert check["limit_value"] == 20 and check["code"] is None
    assert "IT sector: current 15.0% → post-trade 16.5% of portfolio, limit 20.0%" in check["message"]


def test_5_worked_example_22_5_percent_is_refused(env):
    # Portfolio 10,00,000; IT holdings 1,50,000; proposed IT 75,000; limit 20%.
    _portfolio(env, 850_000.0, TCS=(50, 3000.0))
    ticket = _ticket(env, "INFY", 50)
    check = _check(ticket)
    assert ticket["status"] == "risk_rejected"
    assert not check["passed"] and check["actual_value"] == pytest.approx(22.5)
    assert [c["rule"] for c in ticket["risk_check_results"] if not c["passed"]] == [SECTOR_RULE]


def test_6_exactly_at_the_limit_passes_and_one_share_over_fails(env):
    _portfolio(env, 850_000.0, TCS=(50, 3000.0))
    at = _ticket(env, "INFY", 40, entry=1250.0, stop=1200.0, target=1400.0)       # 50,000 → exactly 20%
    assert _check(at)["actual_value"] == pytest.approx(20.0) and _check(at)["passed"]
    over = _ticket(env, "INFY", 41, entry=1250.0, stop=1200.0, target=1400.0)     # 51,250 → 20.125%
    assert not _check(over)["passed"]


def test_9_several_same_sector_holdings_aggregate(env):
    # TCS 80,000 + HCLTECH 70,000 = 15% IT; SUNPHARMA (Pharma, 100,000) never counts.
    _portfolio(env, 750_000.0, TCS=(20, 4000.0), HCLTECH=(50, 1400.0), SUNPHARMA=(50, 2000.0))
    check = _check(_ticket(env, "INFY", 40, entry=1500.0))                          # +60,000 → 21%
    assert check["current_value"] == pytest.approx(15.0) and check["actual_value"] == pytest.approx(21.0)
    assert not check["passed"]


def test_9b_open_app_positions_in_the_sector_count(env):
    # No IT holding at the broker, but an open live TCS position of the app
    # (19 x 3000 = 57,000) + an in-flight INFY entry (60 x 1500 = 90,000).
    _portfolio(env, 1_000_000.0)
    _insert_ticket(env.db, symbol="TCS", quantity=19, entry_price=3000.0, entry_fill_status="filled",
                   executed_at=_now_utc())
    _unknown_ticket(env, symbol="INFY", instrument_token=408065, quantity=60, entry_price=1500.0,
                    submitted_at=_now_utc())
    check = _check(_ticket(env, "INFY", 40, entry=1500.0))                          # +60,000
    assert check["current_value"] == pytest.approx(14.7) and check["actual_value"] == pytest.approx(20.7)
    assert not check["passed"]


def test_10_the_proposed_order_is_included(env):
    _portfolio(env, 1_000_000.0)                                  # no IT exposure at all
    _set_risk(env, max_single_stock_exposure_pct=100.0, max_capital_per_trade_pct=100.0)
    check = _check(_ticket(env, "INFY", 150, entry=1500.0))        # 225,000 alone → 22.5%
    assert check["current_value"] == 0 and check["actual_value"] == pytest.approx(22.5)
    assert not check["passed"]


# ---------------------------------------------------------------- 7, 8: unknown sector

def test_7_unknown_sector_fails_closed_at_creation(env, monkeypatch):
    _unmap(monkeypatch, "INFY")
    ticket = _ticket(env, "INFY", 10)
    check = _check(ticket)
    assert ticket["status"] == "risk_rejected"
    assert (check["passed"], check["skipped"], check["code"]) == (False, False, "SECTOR_MAPPING_REQUIRED")
    assert check["label"] == "Sector exposure · UNMAPPED"
    assert "UNMAPPED — BLOCKED" in check["message"] and "INFY" in check["message"]
    # Every other check passed: the mapping alone blocks it.
    assert [c["rule"] for c in ticket["risk_check_results"] if not c["passed"]] == [SECTOR_RULE]


def test_7b_unknown_sector_fails_closed_at_confirmation(env, market_open, monkeypatch):
    ticket = _ticket(env, "INFY", 10)
    assert ticket["status"] == "pending_confirmation"
    _unmap(monkeypatch, "INFY")                                    # the mapping is gone before approval
    set_mode(env.db, "live")

    r = env.client.post(f"/api/tickets/{ticket['id']}/confirm", json=displayed_mode(env.db))

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error_code"] == "RISK_REVALIDATION_FAILED"
    assert detail["failed_checks"] == [SECTOR_RULE] and detail["failed_codes"] == ["SECTOR_MAPPING_REQUIRED"]
    assert ticket_row(env.db, ticket["id"])["status"] == "risk_rejected"
    assert env.kite.calls_to("place_order") == []


def test_7c_unknown_sector_also_applies_to_dry_run(env, monkeypatch):
    # The mode is only claimed at confirmation, so creation-time checks are
    # mode-agnostic: a dry run simulates exactly what live would refuse.
    set_mode(env.db, "dry_run")
    _unmap(monkeypatch, "INFY")
    assert _ticket(env, "INFY", 10)["status"] == "risk_rejected"


def test_7d_mapping_the_symbol_by_hand_unblocks_it(env, monkeypatch):
    _unmap(monkeypatch, "INFY")
    env.client.post("/api/risk/sector-mappings", json={"symbol": "INFY", "sector": "IT"})
    assert _ticket(env, "INFY", 10)["status"] == "pending_confirmation"


def test_8_emergency_exit_of_an_unmapped_symbol_is_unaffected(env, market_open, monkeypatch):
    position = _position(env, "TCS")
    _unmap(monkeypatch, "TCS")
    set_mode(env.db, "live")

    r = _emergency_exit(env.client)

    assert r.status_code == 200, r.text
    [result] = r.json()["data"]["results"]
    assert result["status"] == "exit_submitted"
    exit_ticket = ticket_row(env.db, result["ticket_id"])
    assert exit_ticket["closes_ticket_id"] == position
    assert [c["rule"] for c in json.loads(exit_ticket["risk_check_results"])] == ["exit_or_reduction"]


def test_8b_alert_action_sell_of_an_unmapped_holding_is_unaffected(env, monkeypatch):
    _unmap(monkeypatch, "TCS")
    env.kite.set_holding("TCS", 10, average_price=3000.0, last_price=2600.0)
    r = env.client.post("/api/tickets/alert-action/sell", json={
        "symbol": "TCS", "quantity": 10, "entry_order_type": "MARKET", "entry_price": 2600.0,
        "rationale": "cut loss", "alert_type": "breach_loss",
    })
    assert r.status_code == 201, r.text
    assert r.json()["data"]["status"] == "pending_confirmation"


def test_unmapped_holding_is_named_but_never_blocks_another_sector(env, monkeypatch):
    _unmap(monkeypatch, "SUNPHARMA")
    _portfolio(env, 900_000.0, SUNPHARMA=(50, 2000.0))
    check = _check(_ticket(env, "INFY", 10))
    assert check["passed"] and "not counted, no sector mapped: SUNPHARMA" in check["message"]


def test_dry_run_executed_tickets_never_count_toward_sector_exposure(env):
    _portfolio(env, 1_000_000.0)
    _insert_ticket(env.db, symbol="TCS", quantity=60, entry_price=3000.0, mode="dry_run",
                   entry_fill_status="filled", executed_at=_now_utc())   # 180,000 simulated
    check = _check(_ticket(env, "INFY", 40))
    assert check["current_value"] == 0 and check["passed"]
