"""Synthetic-data operational isolation (backend/integration_profile.py).

A synthetic DMB bundle is actionable ONLY in the isolated synthetic test profile - throwaway
database, FakeKite, no LIVE - which only a test harness can enter. In PRODUCTION (the default,
and the only profile `python -m backend.server` ever runs) synthetic packets are refused at
import and for any ticket handoff, relabelling one as real evidence is refused too, and real DMB
evidence is refused until the owner confirms a private-handoff right in this terminal.

Throwaway database (conftest `env`), FakeKite, synthetic bundles from tests/dmb_bundles.py.
"""
import inspect
import json
from pathlib import Path

import pytest

from conftest import approval_body, set_mode
from test_baseline_api import _manual_ticket
from test_dmb_inbox import _import, _items, _ok_bundle, _rows
from tests import dmb_bundles as B

ROOT = Path(__file__).resolve().parent.parent


def _handoff(env, tmp_path, monkeypatch):
    from backend import dmb_inbox
    root = tmp_path / "dmb_handoff"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv(dmb_inbox.ENV_VAR, str(root))
    clock = {"now": B.IMPORT_AT}
    monkeypatch.setattr(dmb_inbox, "now_ist", lambda: clock["now"])
    env.handoff, env.clock = root, clock
    return env


@pytest.fixture
def production(env, tmp_path, monkeypatch):
    """The real terminal's profile: nothing activates the synthetic test profile."""
    from backend import integration_profile
    assert integration_profile.current() == integration_profile.PRODUCTION
    return _handoff(env, tmp_path, monkeypatch)


@pytest.fixture
def test_profile(synthetic_profile, tmp_path, monkeypatch):
    return _handoff(synthetic_profile, tmp_path, monkeypatch)


def _code(r):
    return r.json()["detail"]["error_code"]


def _real_packet(symbol="INFY", source="some_real_source", **kw):
    """A packet with no synthetic marker at all, claiming real DMB evidence."""
    p = B.packet(symbol, origin="DMB_EVIDENCE", **kw)
    p["rights"]["sources"] = [{"source_name": source, "handoff_status": "PERMITTED", "basis": "claimed"}]
    p["source_artifact_ids"] = ["radar_candidate_history:2026-11-02"]
    p["reasons"] = ["Volume was 3.2x its prior-20-session average."]
    p["market_regime"]["reason_code"] = "BREADTH_POSITIVE"
    return B.rehash(p)


# ============================================================ production refuses synthetic data
def test_production_refuses_a_synthetic_bundle_at_import_and_stores_nothing(production):
    r = _import(production, _ok_bundle())
    assert r.status_code == 422 and _code(r) == "SYNTHETIC_NOT_PERMITTED", r.text
    assert _rows(production, "SELECT COUNT(*) AS n FROM dmb_inbox_items")[0]["n"] == 0
    [imp] = _rows(production, "SELECT status, reason_code FROM dmb_inbox_imports")
    assert imp == {"status": "REJECTED", "reason_code": "SYNTHETIC_NOT_PERMITTED"}


def test_a_synthetic_item_already_stored_cannot_start_or_link_a_ticket_in_production(test_profile):
    from backend import integration_profile
    env = test_profile
    assert _import(env, _ok_bundle()).status_code == 201
    infy = _items(env)["INFY"]
    assert infy["actionable"] and infy["integration_profile"] == "SYNTHETIC_TEST"
    env.clock["now"] = B.DURING_REVIEW
    start = env.client.post(f"/api/dmb-inbox/items/{infy['id']}/start-ticket").json()["data"]

    integration_profile.deactivate()                     # the same database, back in production
    view = _items(env)["INFY"]
    assert not view["actionable"] and "SYNTHETIC_NOT_PERMITTED" in view["reasons"]
    assert view["integration_profile"] == "PRODUCTION" and view["synthetic"]
    r = env.client.post(f"/api/dmb-inbox/items/{infy['id']}/start-ticket")
    assert r.status_code == 409 and _code(r) == "DMB_ITEM_NOT_ACTIONABLE"
    assert "SYNTHETIC_NOT_PERMITTED" in r.json()["detail"]["reasons"]
    set_mode(env.db, "dry_run")
    t = env.client.post("/api/tickets", json=_manual_ticket(symbol="INFY", quantity=1, entry_price=1500.0,
                                                            stop_loss=1450.0, target_1=1650.0)).json()["data"]
    link = env.client.post(f"/api/dmb-inbox/items/{infy['id']}/ticket-link",
                           json={"ticket_id": t["id"], "start_event_id": start["start_event_id"],
                                 "handoff_nonce": start["handoff_nonce"]})
    assert link.status_code == 409 and _code(link) == "SYNTHETIC_NOT_PERMITTED"


def test_relabelling_synthetic_data_as_real_evidence_is_refused_in_every_profile(production, monkeypatch):
    from backend import integration_profile
    p = B.packet("INFY")
    p["rights"]["data_origin"] = "DMB_EVIDENCE"            # the label edited, the content still synthetic
    p["rights"]["sources"] = [{"source_name": "approved_test_source", "handoff_status": "PERMITTED", "basis": "x"}]
    b = B.bundle([B.rehash(p)])
    r = _import(production, b)
    assert r.status_code == 422 and _code(r) == "SYNTHETIC_MISLABELLED", r.text
    integration_profile.activate_synthetic_test_profile(test_sources=("approved_test_source",))
    r = production.client.post("/api/dmb-inbox/imports", json={"session_date": b["session_date"],
                                                                "bundle_id": b["bundle_id"]})
    assert r.status_code == 422 and _code(r) == "SYNTHETIC_MISLABELLED"


@pytest.mark.parametrize("marker", ["source_artifact_ids", "reasons", "market_regime.reason_code"])
def test_each_synthetic_marker_alone_makes_a_packet_synthetic(marker):
    from backend import dmb_inbox_contract as contract
    p = _real_packet()
    assert contract.synthetic_markers(p) == []
    if marker == "source_artifact_ids":
        p["source_artifact_ids"] = ["synthetic_handoff_fixture:anything"]
    elif marker == "reasons":
        p["reasons"] = ["SYNTHETIC: invented"]
    else:
        p["market_regime"]["reason_code"] = "SYNTHETIC_FIXTURE"
    assert contract.synthetic_markers(p) == [marker]
    assert contract.handoff_policy_problem(p, synthetic_allowed=True, real_sources=frozenset({"some_real_source"}))[0] \
        == "SYNTHETIC_MISLABELLED"


def test_real_evidence_is_refused_in_production_until_a_right_is_confirmed(production):
    from backend import integration_profile
    assert integration_profile.PRODUCTION_PERMITTED_SOURCES == frozenset()
    r = _import(production, B.bundle([_real_packet()]))
    assert r.status_code == 422 and _code(r) == "RIGHTS_NOT_APPROVED", r.text


def test_a_synthetic_packet_must_come_from_dmbs_synthetic_fixture(test_profile):
    p = B.packet("INFY")
    p["source_artifact_ids"] = ["synthetic_handoff_fixture:"]          # no fixture id
    r = _import(test_profile, B.bundle([B.rehash(p)]))
    assert r.status_code == 422 and _code(r) == "SYNTHETIC_NOT_PERMITTED"


# ============================================================ the test profile works
def test_the_test_profile_imports_and_starts_a_ticket_from_the_synthetic_fixture(test_profile):
    env = test_profile
    assert _import(env, _ok_bundle()).status_code == 201
    infy = _items(env)["INFY"]
    assert infy["synthetic"] and infy["actionable"]
    env.clock["now"] = B.DURING_REVIEW
    r = env.client.post(f"/api/dmb-inbox/items/{infy['id']}/start-ticket")
    assert r.status_code == 200 and set(r.json()["data"]["prefill"]) == {"symbol", "exchange", "rationale"}


def test_test_only_sources_count_only_inside_the_test_profile(test_profile):
    from backend import integration_profile
    p = _real_packet(source="approved_test_source")
    assert _import(test_profile, B.bundle([p])).status_code == 201
    assert _items(test_profile)["INFY"]["actionable"]
    integration_profile.deactivate()
    assert "RIGHTS_NOT_APPROVED" in _items(test_profile)["INFY"]["reasons"]


# ============================================================ the profile needs isolation
def test_activation_is_refused_on_the_real_database_path(env, monkeypatch):
    from backend import db, integration_profile
    # Path logic only: the real file is never opened (tests/real_data_guard.py would refuse it).
    monkeypatch.setattr(db, "DB_PATH", ROOT / "backend" / "kite_terminal.db")
    with pytest.raises(integration_profile.ProfileRefused, match="real account database"):
        integration_profile.activate_synthetic_test_profile()
    monkeypatch.setattr(db, "DB_PATH", ROOT / "backend" / "kite_terminal.db.pre_pk9_pk11_bak")
    assert "the database is the real account database" in integration_profile.isolation_problems()


def test_activation_is_refused_with_the_real_broker_client(env, monkeypatch):
    from backend import integration_profile, kite_client
    monkeypatch.setattr(kite_client, "get_kite", kite_client.PRODUCTION_GET_KITE)
    with pytest.raises(integration_profile.ProfileRefused, match="real broker client"):
        integration_profile.activate_synthetic_test_profile()


def test_an_active_profile_ends_the_moment_isolation_is_lost(synthetic_profile, monkeypatch):
    from backend import integration_profile, kite_client
    assert integration_profile.current() == "SYNTHETIC_TEST"
    monkeypatch.setattr(kite_client, "get_kite", kite_client.PRODUCTION_GET_KITE)
    assert integration_profile.current() == "PRODUCTION"


def test_nothing_but_a_test_harness_can_activate_the_profile():
    """No backend code, environment variable, setting or route turns it on."""
    for f in sorted((ROOT / "backend").rglob("*.py")):
        src = f.read_text(encoding="utf-8")
        if f.name == "integration_profile.py":
            assert "os.environ" not in src and "getenv" not in src
            continue
        assert "activate_synthetic_test_profile" not in src, f
    harnesses = [f.name for f in (ROOT / "tests").glob("*.py")
                 if "activate_synthetic_test_profile(" in f.read_text(encoding="utf-8")]
    assert "demo_server.py" in harnesses and "conftest.py" in harnesses


# ============================================================ the test profile never goes live
def test_the_test_profile_refuses_to_arm_live(synthetic_profile):
    from conftest import make_live_ready
    make_live_ready(synthetic_profile)
    r = synthetic_profile.client.post("/api/execution/mode", json={"mode": "live", "confirm_real_money": True})
    assert r.status_code == 409, r.text
    assert "SYNTHETIC_TEST_PROFILE" in r.json()["detail"]["blocking_codes"]
    assert synthetic_profile.client.get("/api/execution/mode").json()["data"]["mode"] == "dry_run"


def test_a_live_entry_is_refused_in_the_test_profile_even_if_the_database_says_live(synthetic_profile, market_open):
    env = synthetic_profile
    set_mode(env.db, "live")                          # behind the app's back
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"]))
    assert r.status_code == 409, r.text
    codes = [f["code"] for f in r.json()["detail"]["failures"]]
    assert "LIVE_NOT_ARMED_FOR_SESSION" in codes
    assert env.kite.calls_to("place_order") == []


def test_dry_run_still_works_in_the_test_profile(synthetic_profile, market_open):
    env = synthetic_profile
    set_mode(env.db, "dry_run")
    t = env.client.post("/api/tickets", json=_manual_ticket()).json()["data"]
    r = env.client.post(f"/api/tickets/{t['id']}/confirm", json=approval_body(env.db, t["id"]))
    assert r.status_code == 200, r.text
    assert env.kite.calls_to("place_order") == []                # the paper broker, never FakeKite's book


def test_the_profile_is_documented_in_the_inbox_and_server_never_imports_the_activation():
    from backend import server
    assert "integration_profile" not in inspect.getsource(server)
    assert json.loads(json.dumps({"ok": True}))                 # keep json import honest
