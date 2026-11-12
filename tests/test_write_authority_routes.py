"""Phase 2: every write route depends on instance_lock.require_write_authority (design §7.2, §13).

Completeness is checked against the app's own route table, like the CSRF and credential inventories
(test_csrf_protection.py, test_terminal_credential.py): a new mutating route without the dependency
fails here. Without write authority (no ownership grant, another instance owns the database, authority
lost, shutting down) every write route answers 503 and nothing reaches the broker."""

import pytest
from fastapi.routing import APIRoute

from backend import instance_lock

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
# GETs that write (session, the encrypted token, a cached chart URL): guarded like writes.
WRITING_GETS = {"/login", "/callback", "/api/tickets/{ticket_id}/chart"}


def _routes():
    from backend.main import app
    return [r for r in app.routes if isinstance(r, APIRoute)]


def _guarded(route) -> bool:
    return any(dep.dependency is instance_lock.require_write_authority for dep in route.dependencies)


def _write_routes():
    return sorted((method, r.path) for r in _routes() for method in r.methods
                  if method in MUTATING or (method == "GET" and r.path in WRITING_GETS))


def test_every_mutating_route_and_writing_get_requires_write_authority():
    missing = [(sorted(r.methods), r.path) for r in _routes()
               if (r.methods & MUTATING or r.path in WRITING_GETS) and not _guarded(r)]
    assert missing == []


def test_the_inventory_covers_the_safety_critical_writes():
    paths = {path for _, path in _write_routes()}
    for path in ("/api/tickets/{ticket_id}/confirm", "/api/kill-switch/emergency-exit-confirm",
                 "/api/orders/{order_id}/cancel", "/api/gtts/{gtt_id}/cancel", "/api/app-positions/{position_id}/exit",
                 "/api/strategies/stop-adjustments/{proposal_id}/confirm", "/api/execution/mode", "/login",
                 "/callback", "/api/logout"):
        assert path in paths, path
    assert len(_write_routes()) >= 48


def test_read_routes_do_not_require_write_authority():
    """Reads keep working for a process that lost authority (it shows a banner; it writes nothing)."""
    reads = [r for r in _routes() if r.methods <= {"GET", "HEAD"} and r.path not in WRITING_GETS]
    assert reads and not any(_guarded(r) for r in reads)


def _concrete(path: str) -> str:
    return (path.replace("{ticket_id}", "1").replace("{order_id}", "1").replace("{gtt_id}", "1")
            .replace("{position_id}", "1").replace("{proposal_id}", "1").replace("{symbol}", "TCS")
            .replace("{watchlist_id}", "1").replace("{mismatch_id}", "1").replace("{instrument_token}", "1")
            .replace("{strategy_name}", "momentum_breakout").replace("{item_id}", "1").replace("{run_id}", "1"))


@pytest.mark.parametrize("method,path", _write_routes())
def test_without_authority_every_write_route_is_refused_with_503(env, method, path):
    """The fail-closed default: this process never took the lock (the state of a test without the
    ownership grant, a CLI, or a process whose startup did not complete)."""
    instance_lock.reset_for_tests()
    mark = len(env.kite.calls)
    r = env.client.request(method, _concrete(path), json={})
    assert r.status_code == 503, (method, path, r.status_code, r.text[:300])
    assert r.json()["detail"]["error_code"] == "INSTANCE_NOT_OWNER"
    assert env.kite.calls[mark:] == []


@pytest.mark.parametrize("state,code", [("lost", "INSTANCE_LOCK_LOST"), ("conflict", "INSTANCE_CONFLICT"),
                                        ("non_owner", "ANOTHER_INSTANCE_RUNNING"), ("shutting_down", "INSTANCE_NOT_OWNER")])
def test_each_loss_of_authority_has_its_own_refusal_code(env, monkeypatch, state, code):
    if state == "shutting_down":
        instance_lock.begin_shutdown()
    elif state == "lost":
        instance_lock.lose_authority(instance_lock.INSTANCE_LOCK_LOST, "test")
    else:
        monkeypatch.setattr(instance_lock._state, "role", state)
        monkeypatch.setattr(instance_lock._state, "reason", code)
    r = env.client.post("/api/kill-switch/emergency-exit-confirm", json={"confirmation_phrase": "EXIT ALL"})
    assert r.status_code == 503 and r.json()["detail"]["error_code"] == code


def test_login_and_callback_are_refused_for_a_non_owner(env, monkeypatch):
    from backend import kite_client
    exchanged = []
    monkeypatch.setattr(kite_client, "exchange_request_token", lambda *a, **k: exchanged.append(a))
    instance_lock.reset_for_tests()
    assert env.client.get("/login", follow_redirects=False).status_code == 503
    r = env.client.get("/callback?request_token=abc&status=success", follow_redirects=False)
    assert r.status_code == 503 and exchanged == []


def test_a_login_by_the_owner_requests_a_stage1_run(env, monkeypatch):
    from conftest import start_kite_login
    from backend import kite_client, security
    monkeypatch.setattr(kite_client, "exchange_request_token", lambda *a, **k: {"access_token": "x"})
    state = start_kite_login(env.client)
    r = env.client.get(f"/callback?request_token=abc&status=success&{security.LOGIN_STATE_PARAM}={state}",
                       follow_redirects=False)
    assert r.status_code in (302, 307), r.text
    assert env.stage1_requests == ["login"]


def test_with_the_ownership_grant_write_routes_work_as_before(env):
    r = env.client.post("/api/watchlist", json={"symbol": "TCS"})
    assert r.status_code != 503, r.text
