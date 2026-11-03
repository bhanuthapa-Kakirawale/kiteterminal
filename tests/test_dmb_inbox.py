"""DMB Research Inbox: migration v9, authenticated import, path safety, idempotency,
revisions, user actions and the manual-ticket handoff (backend/dmb_inbox.py).

Throwaway database (conftest `env`), FakeKite, synthetic bundles from tests/dmb_bundles.py in a
temporary handoff directory. No real database, broker, credential or DMB file is touched.
"""
import hashlib
import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from conftest import TERMINAL_HEADERS, TERMINAL_ORIGIN, approval_body, reauthorize, set_mode, terminal_credential
from test_baseline_api import _manual_ticket
from tests import dmb_bundles as B

BROKER_WRITES = ("place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "delete_gtt")
INBOX_TABLES = ("dmb_inbox_imports", "dmb_inbox_items", "dmb_inbox_events", "dmb_inbox_ticket_links")


@pytest.fixture
def inbox(env, tmp_path, monkeypatch):
    from backend import dmb_inbox
    root = tmp_path / "dmb_handoff"
    root.mkdir()
    monkeypatch.setenv(dmb_inbox.ENV_VAR, str(root))
    clock = {"now": B.IMPORT_AT}
    monkeypatch.setattr(dmb_inbox, "now_ist", lambda: clock["now"])
    env.handoff, env.clock = root, clock
    return env


def _ok_bundle():
    return B.bundle([B.packet("INFY"), B.packet("TCS", replay="NOT_REPLAYED")])


def _import(env, b, client=None, **override):
    if "path" not in override:
        B.write(env.handoff, b)
    body = {"session_date": b["session_date"], "bundle_id": b["bundle_id"], **override.get("body", {})}
    return (client or env.client).post("/api/dmb-inbox/imports", json=body)


def _rows(env, sql, *args):
    conn = env.db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _items(env, **params):
    r = env.client.get("/api/dmb-inbox/items", params=params)
    assert r.status_code == 200, r.text
    return {i["symbol"]: i for i in r.json()["data"]}


def _digest(env, tables) -> str:
    h = hashlib.sha256()
    conn = env.db.get_connection()
    try:
        for t in tables:
            for row in conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall():
                h.update(repr(tuple(row)).encode())
    finally:
        conn.close()
    return h.hexdigest()


TRADING_TABLES = ("trade_tickets", "ticket_events", "watchlist", "strategy_configs", "strategy_config_versions",
                  "execution_settings", "risk_settings", "paper_orders", "paper_gtts", "closed_trades",
                  "account_safety_state")


# ============================================================ migration v9
def test_migration_v9_creates_the_inbox_tables(env):
    assert env.db.LATEST_SCHEMA_VERSION == 9
    assert env.db.MIGRATIONS[-1][:2] == (9, "dmb_research_inbox")
    for t in INBOX_TABLES:
        assert _rows(env, "SELECT name FROM sqlite_master WHERE type='table' AND name=?", t), t
    conn = env.db.get_connection()
    try:
        env.db._m009_dmb_research_inbox(conn)          # idempotent: re-running changes nothing
        conn.commit()
    finally:
        conn.close()


def test_migration_v9_leaves_trading_tables_unchanged(env):
    conn = env.db.get_connection()
    try:
        tickets_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='trade_tickets'").fetchone()[0]
        watch_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='watchlist'").fetchone()[0]
    finally:
        conn.close()
    assert "dmb" not in tickets_sql.lower() and "dmb" not in watch_sql.lower()
    assert "dmb" not in env.db.SCHEMA.lower()


def test_imports_and_events_are_append_only_and_items_immutable(inbox):
    assert _import(inbox, _ok_bundle()).status_code == 201
    conn = inbox.db.get_connection()
    try:
        for sql in ("UPDATE dmb_inbox_imports SET status='REJECTED'", "DELETE FROM dmb_inbox_imports",
                    "UPDATE dmb_inbox_events SET event_type='DISMISSED'", "DELETE FROM dmb_inbox_events",
                    "DELETE FROM dmb_inbox_items", "UPDATE dmb_inbox_items SET packet_json='{}'",
                    "UPDATE dmb_inbox_items SET symbol='TCS'",
                    "UPDATE dmb_inbox_items SET superseded_by_item_id = NULL"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        a, b = [r["id"] for r in conn.execute("SELECT id FROM dmb_inbox_items ORDER BY id")]
        conn.execute("UPDATE dmb_inbox_items SET superseded_by_item_id = ?, superseded_at = 'x' WHERE id = ?", (b, a))
        with pytest.raises(sqlite3.IntegrityError):              # set once, never re-pointed
            conn.execute("UPDATE dmb_inbox_items SET superseded_by_item_id = ? WHERE id = ?", (a, a))
        conn.rollback()
    finally:
        conn.close()


# ============================================================ authentication
def _bare(credential=None, origin=TERMINAL_ORIGIN, header=True):
    from backend.main import app
    from backend import security
    headers = {"X-Kite-Terminal": "1"} if header else {}
    if origin is not None:
        headers["Origin"] = origin
    c = TestClient(app, base_url=TERMINAL_ORIGIN, headers=headers)
    if credential:
        c.cookies.set(security.CREDENTIAL_COOKIE, credential)
    return c


@pytest.mark.parametrize("case", ["no_credential", "wrong_credential", "foreign_origin", "no_origin", "no_header"])
def test_import_requires_the_p1f_credential_and_origin(inbox, case):
    client = {"no_credential": _bare(None), "wrong_credential": _bare("x" * 43),
              "foreign_origin": _bare(terminal_credential(), origin="https://evil.example"),
              "no_origin": _bare(terminal_credential(), origin=None),
              "no_header": _bare(terminal_credential(), header=False)}[case]
    r = _import(inbox, _ok_bundle(), client=client)
    assert r.status_code in (401, 403), (case, r.status_code, r.text)
    assert _rows(inbox, "SELECT * FROM dmb_inbox_imports") == []
    assert _rows(inbox, "SELECT * FROM dmb_inbox_items") == []


def test_import_requires_an_active_kite_login(inbox):
    inbox.client.post("/api/logout")
    reauthorize(inbox.client)              # a valid credential, but no Kite session any more
    r = _import(inbox, _ok_bundle())
    assert r.status_code == 401 and r.json()["detail"]["error_code"] == "NO_SESSION"
    assert _rows(inbox, "SELECT * FROM dmb_inbox_items") == []


def test_every_inbox_action_requires_a_kite_login(inbox):
    _import(inbox, _ok_bundle())
    item = _items(inbox)["INFY"]["id"]
    inbox.client.post("/api/logout")
    reauthorize(inbox.client)
    for path, body in ((f"/api/dmb-inbox/items/{item}/dismiss", {}),
                       (f"/api/dmb-inbox/items/{item}/notes", {"text": "x"}),
                       (f"/api/dmb-inbox/items/{item}/research-watchlist", {"watched": True}),
                       (f"/api/dmb-inbox/items/{item}/start-ticket", {}),
                       (f"/api/dmb-inbox/items/{item}/ticket-link",
                        {"ticket_id": 1, "start_event_id": 1, "handoff_nonce": "x"})):
        r = inbox.client.post(path, json=body)
        assert r.status_code == 401 and r.json()["detail"]["error_code"] == "NO_SESSION", path


def test_a_non_trading_account_is_refused(inbox):
    conn = inbox.db.get_connection()
    try:
        conn.execute("UPDATE accounts SET account_type = 'investment'")
        conn.commit()
    finally:
        conn.close()
    assert _import(inbox, _ok_bundle()).status_code == 403
    assert inbox.client.get("/api/dmb-inbox/items").status_code == 403


def test_no_get_route_or_page_exposes_the_credential(inbox):
    _import(inbox, _ok_bundle())
    token = terminal_credential()
    for url in ("/api/dmb-inbox/items", "/api/dmb-inbox/imports", "/api/dmb-inbox/bundles",
                f"/api/dmb-inbox/items/{_items(inbox)['INFY']['id']}", "/"):
        assert token not in inbox.client.get(url).text, url


# ============================================================ import
def test_a_valid_bundle_imports_with_provenance(inbox):
    b = _ok_bundle()
    r = _import(inbox, b)
    assert r.status_code == 201, r.text
    d = r.json()["data"]
    assert d["status"] == "IMPORTED" and d["new_item_count"] == 2 and d["bundle_id"] == b["bundle_id"]
    raw = open(os.path.join(inbox.handoff, B.SESSION, f"{b['bundle_id']}.json"), "rb").read()
    assert d["file_sha256"] == hashlib.sha256(raw).hexdigest()
    [imp] = _rows(inbox, "SELECT * FROM dmb_inbox_imports")
    assert imp["status"] == "IMPORTED" and imp["file_sha256"] == d["file_sha256"] and imp["session_id"]
    items = _items(inbox)
    infy = items["INFY"]
    assert infy["exchange"] == "NSE" and infy["packet_id"] == b["packets"][0]["packet_id"]
    assert infy["session_date"] == B.SESSION and infy["review_session_date"] == B.REVIEW
    assert infy["evidence_as_of"] == "2026-11-02T19:45:00+05:30" and infy["synthetic"] is True
    assert infy["rights"]["handoff_status"] == "PERMITTED" and infy["quality"]["replay_status"] == "MATCHED"
    assert infy["rationale"] and infy["radar_state"]["direction"] == "ALIGNED_POSITIVE"
    assert infy["source_artifact_ids"] and infy["imported_at"] and infy["superseded_by_item_id"] is None
    assert infy["actionable"] is True and infy["reasons"] == []
    assert items["TCS"]["actionable"] is False and "QUALITY_REPLAY_NOT_MATCHED" in items["TCS"]["reasons"]
    assert [e["event_type"] for e in infy["history"]] == ["IMPORTED"]
    assert "NOT A TRADE RECOMMENDATION" in inbox.client.get("/api/dmb-inbox/items").json()["disclaimer"]


def test_importing_creates_no_ticket_order_or_trading_change(inbox):
    before = _digest(inbox, TRADING_TABLES)
    calls_before = len(inbox.kite.calls)
    assert _import(inbox, _ok_bundle()).status_code == 201
    assert _digest(inbox, TRADING_TABLES) == before
    assert inbox.kite.calls[calls_before:] == []
    assert _rows(inbox, "SELECT * FROM trade_tickets") == []
    assert inbox.broadcasts == [] or all(m.get("type") != "tickets_changed" for m in inbox.broadcasts)


def test_no_broker_client_is_ever_built_by_the_inbox(inbox, monkeypatch):
    from backend import kite_client

    def refuse(*a, **k):
        raise AssertionError("the inbox built a Kite client")
    monkeypatch.setattr(kite_client, "get_kite", refuse)
    monkeypatch.setattr(kite_client, "create_kite_connect", refuse)
    assert _import(inbox, _ok_bundle()).status_code == 201
    item = _items(inbox)["INFY"]["id"]
    for path, body in ((f"/api/dmb-inbox/items/{item}/notes", {"text": "n"}),
                       (f"/api/dmb-inbox/items/{item}/research-watchlist", {"watched": True}),
                       (f"/api/dmb-inbox/items/{item}/start-ticket", {}),
                       (f"/api/dmb-inbox/items/{item}/dismiss", {})):
        assert inbox.client.post(path, json=body).status_code in (200, 201), path
    for url in ("/api/dmb-inbox/items", "/api/dmb-inbox/imports", "/api/dmb-inbox/bundles"):
        assert inbox.client.get(url).status_code == 200


def test_a_duplicate_import_is_idempotent(inbox):
    b = _ok_bundle()
    assert _import(inbox, b).json()["data"]["status"] == "IMPORTED"
    r = _import(inbox, b)
    assert r.status_code == 201 and r.json()["data"]["status"] == "DUPLICATE"
    assert r.json()["data"]["new_item_count"] == 0
    assert len(_rows(inbox, "SELECT * FROM dmb_inbox_items")) == 2
    assert [i["status"] for i in _rows(inbox, "SELECT status FROM dmb_inbox_imports ORDER BY id")] == \
        ["IMPORTED", "DUPLICATE"]
    # the same packets in another bundle add nothing either
    other = B.bundle([B.packet("INFY")])
    assert _import(inbox, other).json()["data"]["status"] == "DUPLICATE"
    assert len(_rows(inbox, "SELECT * FROM dmb_inbox_items")) == 2


def test_a_revised_packet_is_kept_and_the_older_version_superseded(inbox):
    v1 = B.packet("INFY", replay="NOT_REPLAYED")
    _import(inbox, B.bundle([v1]))
    v2 = B.packet("INFY", evidence_as_of="2026-11-02T20:10:00+05:30", generated_at="2026-11-02T20:15:00+05:30")
    assert _import(inbox, B.bundle([v2], generated_at="2026-11-02T20:15:00+05:30")).status_code == 201
    rows = _rows(inbox, "SELECT * FROM dmb_inbox_items ORDER BY id")
    assert [r["packet_id"] for r in rows] == [v1["packet_id"], v2["packet_id"]]
    assert rows[0]["superseded_by_item_id"] == rows[1]["id"] and rows[1]["superseded_by_item_id"] is None
    current = _items(inbox)["INFY"]
    assert current["packet_id"] == v2["packet_id"] and current["actionable"]
    every = inbox.client.get("/api/dmb-inbox/items", params={"include_superseded": True}).json()["data"]
    old = next(i for i in every if i["packet_id"] == v1["packet_id"])
    assert old["actionable"] is False and "SUPERSEDED" in old["reasons"]
    assert [e["event_type"] for e in old["history"]] == ["IMPORTED", "SUPERSEDED"]
    detail = inbox.client.get(f"/api/dmb-inbox/items/{current['id']}").json()["data"]
    assert [v["packet_id"] for v in detail["versions"]] == [v1["packet_id"], v2["packet_id"]]


def test_an_older_version_imported_late_is_superseded_at_once(inbox):
    newer = B.packet("INFY", evidence_as_of="2026-11-02T20:10:00+05:30", generated_at="2026-11-02T20:15:00+05:30")
    older = B.packet("INFY", replay="NOT_REPLAYED")
    _import(inbox, B.bundle([newer], generated_at="2026-11-02T20:15:00+05:30"))
    _import(inbox, B.bundle([older]))
    assert _items(inbox)["INFY"]["packet_id"] == newer["packet_id"]
    rows = {r["packet_id"]: r for r in _rows(inbox, "SELECT * FROM dmb_inbox_items")}
    assert rows[older["packet_id"]]["superseded_by_item_id"] == rows[newer["packet_id"]]["id"]
    assert rows[newer["packet_id"]]["superseded_by_item_id"] is None


def test_a_stored_packet_id_with_other_content_is_refused(inbox):
    p = B.packet("INFY")
    _import(inbox, B.bundle([p]))
    conn = inbox.db.get_connection()          # simulate a stored row whose content was altered
    try:
        conn.execute("DROP TRIGGER dmb_inbox_items_immutable")
        conn.execute("UPDATE dmb_inbox_items SET packet_json = replace(packet_json, '1534.5', '1.0')")
        conn.commit()
    finally:
        conn.close()
    r = _import(inbox, B.bundle([p, B.packet("TCS")]))
    assert r.status_code == 422 and r.json()["detail"]["error_code"] == "CONFLICTING_PACKET"
    assert len(_rows(inbox, "SELECT * FROM dmb_inbox_items")) == 1     # nothing from that bundle
    assert "INTEGRITY_MISMATCH" in _items(inbox)["INFY"]["reasons"]


@pytest.mark.parametrize("mutate,code", [
    (lambda b: b["packets"][0]["sector_context"].update(nested={"positionSize": 1}), "FORBIDDEN_FIELD"),
    (lambda b: b["packets"][0].update(order_id="1"), "FORBIDDEN_FIELD"),
    (lambda b: b["packets"][0]["quality"].update(extra=True), "UNKNOWN_KEY"),
    (lambda b: b["packets"][0]["rights"].update(handoff_status="BLOCKED"), "RIGHTS_NOT_APPROVED"),
    (lambda b: b["packets"][0]["metrics"].update(close=2.0), "PACKET_ID_MISMATCH"),
    (lambda b: b.update(schema_version="dmb-candidate-bundle-9"), "UNSUPPORTED_BUNDLE_SCHEMA"),
])
def test_a_malicious_bundle_is_refused_whole_and_recorded(inbox, mutate, code):
    b = _ok_bundle()
    mutate(b)
    r = _import(inbox, b)
    assert r.status_code == 422 and r.json()["detail"]["error_code"] == code, r.text
    [imp] = _rows(inbox, "SELECT * FROM dmb_inbox_imports")
    assert imp["status"] == "REJECTED" and imp["reason_code"] == code and imp["file_sha256"]
    assert _rows(inbox, "SELECT * FROM dmb_inbox_items") == []
    assert _rows(inbox, "SELECT * FROM dmb_inbox_events") == []


def test_a_stale_bundle_is_refused(inbox):
    inbox.clock["now"] = B.AFTER_REVIEW
    r = _import(inbox, _ok_bundle())
    assert r.status_code == 422 and r.json()["detail"]["error_code"] == "STALE_EVIDENCE"


def test_corrupt_json_is_refused(inbox):
    b = _ok_bundle()
    B.write(inbox.handoff, b, raw=b'{"schema_version": "dmb-candidate-bundle-1", ')
    r = _import(inbox, b, path=True)
    assert r.status_code == 422 and r.json()["detail"]["error_code"] == "CORRUPT_JSON"


def test_a_file_whose_name_does_not_match_its_bundle_is_refused(inbox):
    good, other = _ok_bundle(), B.bundle([B.packet("RELIANCE")])
    B.write(inbox.handoff, other, name=f"{good['bundle_id']}.json")      # file substituted under a known name
    r = _import(inbox, good, path=True)
    assert r.status_code == 422 and r.json()["detail"]["error_code"] == "FILE_SUBSTITUTION"


def test_import_is_disabled_until_the_handoff_directory_is_configured(inbox, monkeypatch):
    from backend import dmb_inbox
    monkeypatch.delenv(dmb_inbox.ENV_VAR)
    r = _import(inbox, _ok_bundle(), path=True)
    assert r.status_code == 422 and r.json()["detail"]["error_code"] == "HANDOFF_NOT_CONFIGURED"
    listing = inbox.client.get("/api/dmb-inbox/bundles").json()
    assert listing["configured"] is False and listing["data"] == []


# ============================================================ paths
@pytest.mark.parametrize("session_date,bundle_id", [
    ("../../../x", "a" * 64), ("2026-11-02", "../" + "a" * 61), ("2026-11-02", "A" * 64),
    ("2026-11-02", "a" * 63 + "/"), ("2026-11-02", "..\\" + "a" * 61), ("C:/Windows", "a" * 64),
    ("2026/11/02", "a" * 64),
])
def test_path_traversal_is_refused_before_any_file_access(inbox, session_date, bundle_id):
    r = inbox.client.post("/api/dmb-inbox/imports", json={"session_date": session_date, "bundle_id": bundle_id})
    assert r.status_code == 422
    detail = r.json()["detail"]
    if isinstance(detail, dict):
        assert detail["error_code"] == "PATH_REFUSED"


def test_listing_shows_only_bundle_files(inbox):
    b = _ok_bundle()
    B.write(inbox.handoff, b)
    (inbox.handoff / "export_log.jsonl").write_text("{}\n")
    (inbox.handoff / B.SESSION / ".bundle-x.tmp").write_text("x")
    (inbox.handoff / "notes").mkdir()
    listing = inbox.client.get("/api/dmb-inbox/bundles").json()["data"]
    assert [(x["session_date"], x["bundle_id"]) for x in listing] == [(B.SESSION, b["bundle_id"])]
    assert listing[0]["imported"] is False
    _import(inbox, b, path=True)
    assert inbox.client.get("/api/dmb-inbox/bundles").json()["data"][0]["imported"] is True


def _symlink_or_skip(target, link, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"symlinks unavailable here: {e}")


def test_a_symlinked_bundle_is_refused(inbox, tmp_path):
    b = _ok_bundle()
    outside = tmp_path / "outside.json"
    outside.write_bytes(json.dumps(b).encode())
    (inbox.handoff / B.SESSION).mkdir()
    _symlink_or_skip(outside, inbox.handoff / B.SESSION / f"{b['bundle_id']}.json")
    r = _import(inbox, b, path=True)
    assert r.status_code == 422 and r.json()["detail"]["error_code"] == "PATH_REFUSED"


def test_a_symlinked_session_folder_is_refused(inbox, tmp_path):
    b = _ok_bundle()
    elsewhere = tmp_path / "elsewhere"
    B.write(elsewhere, b)
    _symlink_or_skip(elsewhere / B.SESSION, inbox.handoff / B.SESSION, directory=True)
    r = _import(inbox, b, path=True)
    assert r.status_code == 422 and r.json()["detail"]["error_code"] == "PATH_REFUSED"


def test_a_junction_session_folder_is_refused(inbox, tmp_path):
    try:
        import _winapi
    except ImportError:
        pytest.skip("junctions are a Windows feature")
    b = _ok_bundle()
    elsewhere = tmp_path / "elsewhere"
    B.write(elsewhere, b)
    _winapi.CreateJunction(str(elsewhere / B.SESSION), str(inbox.handoff / B.SESSION))
    r = _import(inbox, b, path=True)
    assert r.status_code == 422 and r.json()["detail"]["error_code"] == "PATH_REFUSED"
    assert inbox.client.get("/api/dmb-inbox/bundles").json()["data"] == []


def test_a_hard_linked_bundle_is_refused(inbox, tmp_path):
    b = _ok_bundle()
    outside = tmp_path / "outside.json"
    outside.write_bytes(json.dumps(b).encode())
    (inbox.handoff / B.SESSION).mkdir()
    os.link(outside, inbox.handoff / B.SESSION / f"{b['bundle_id']}.json")
    r = _import(inbox, b, path=True)
    assert r.status_code == 422 and r.json()["detail"]["error_code"] == "FILE_SUBSTITUTION"


def test_a_symlinked_handoff_root_is_refused(inbox, tmp_path, monkeypatch):
    from backend import dmb_inbox
    real = tmp_path / "real_root"
    B.write(real, _ok_bundle())
    link = tmp_path / "linked_root"
    _symlink_or_skip(real, link, directory=True)
    monkeypatch.setenv(dmb_inbox.ENV_VAR, str(link))
    r = _import(inbox, _ok_bundle(), path=True)
    assert r.status_code == 422 and r.json()["detail"]["error_code"] == "PATH_REFUSED"


def test_the_import_never_modifies_dmb_files(inbox):
    b = _ok_bundle()
    path = B.write(inbox.handoff, b)
    os.chmod(path, 0o444)
    before = {p: (open(p, "rb").read(), os.stat(p).st_mtime_ns) for p in [path]}
    listing_before = sorted(os.listdir(inbox.handoff / B.SESSION))
    _import(inbox, b, path=True)
    _import(inbox, b, path=True)
    assert {p: (open(p, "rb").read(), os.stat(p).st_mtime_ns) for p in [path]} == before
    assert sorted(os.listdir(inbox.handoff / B.SESSION)) == listing_before
    assert sorted(os.listdir(inbox.handoff)) == [B.SESSION]


def test_the_inbox_has_no_write_into_the_handoff_directory():
    """Static: the module opens files read-only and never removes, renames or writes them."""
    import ast
    import inspect
    from backend import dmb_inbox
    tree = ast.parse(inspect.getsource(dmb_inbox))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("remove", "unlink", "rename", "replace", "rmdir",
                                                            "write_bytes", "write_text", "chmod", "makedirs"):
            pytest.fail(f"dmb_inbox calls {node.attr}")
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open":
            pytest.fail("dmb_inbox uses open(); it must use os.open(..., O_RDONLY)")
    src = inspect.getsource(dmb_inbox)
    assert "O_WRONLY" not in src and "O_RDWR" not in src and "O_CREAT" not in src


# ============================================================ user actions
def test_dismiss_note_and_research_watchlist_are_recorded(inbox):
    _import(inbox, _ok_bundle())
    infy = _items(inbox)["INFY"]["id"]
    tcs = _items(inbox)["TCS"]["id"]
    watch_before = _digest(inbox, ("watchlist", "strategy_configs", "strategy_config_versions"))
    r = inbox.client.post(f"/api/dmb-inbox/items/{infy}/notes", json={"text": "  check the sector  "})
    assert r.status_code == 201 and r.json()["data"]["notes"][0]["text"] == "check the sector"
    r = inbox.client.post(f"/api/dmb-inbox/items/{infy}/research-watchlist", json={"watched": True})
    assert r.json()["data"]["on_research_watchlist"] is True
    assert list(_items(inbox, watchlist=True)) == ["INFY"]
    r = inbox.client.post(f"/api/dmb-inbox/items/{tcs}/dismiss", json={"reason": "not interesting"})
    assert r.json()["data"]["dismissed"] is True and "DISMISSED" in r.json()["data"]["reasons"]
    assert inbox.client.post(f"/api/dmb-inbox/items/{tcs}/dismiss", json={}).status_code == 409
    events = [e["event_type"] for e in _rows(inbox, "SELECT event_type FROM dmb_inbox_events ORDER BY id")]
    assert events == ["IMPORTED", "IMPORTED", "NOTE_ADDED", "WATCHLIST_ADDED", "DISMISSED"]
    assert all(r["session_id"] for r in _rows(inbox, "SELECT session_id FROM dmb_inbox_events"))
    # the research watchlist is not the strategy universe
    assert _digest(inbox, ("watchlist", "strategy_configs", "strategy_config_versions")) == watch_before
    assert _rows(inbox, "SELECT * FROM watchlist") == []
    inbox.client.post(f"/api/dmb-inbox/items/{infy}/research-watchlist", json={"watched": False})
    assert _items(inbox, watchlist=True) == {}


def test_actions_on_another_accounts_or_a_missing_item_are_404(inbox):
    _import(inbox, _ok_bundle())
    assert inbox.client.post("/api/dmb-inbox/items/999/notes", json={"text": "x"}).status_code == 404
    assert inbox.client.get("/api/dmb-inbox/items/999").status_code == 404
    assert inbox.client.post("/api/dmb-inbox/items/1/notes", json={"text": ""}).status_code == 422


# ============================================================ start ticket
def _start(env, symbol="INFY"):
    item = _items(env)[symbol]["id"]
    return item, env.client.post(f"/api/dmb-inbox/items/{item}/start-ticket")


def test_start_ticket_prefills_only_symbol_exchange_and_rationale(inbox):
    _import(inbox, _ok_bundle())
    before = _digest(inbox, TRADING_TABLES)
    item, r = _start(inbox)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert set(d["prefill"]) == {"symbol", "exchange", "rationale"}
    assert d["prefill"]["symbol"] == "INFY" and d["prefill"]["exchange"] == "NSE"
    assert d["prefill"]["rationale"].startswith("[SYNTHETIC TEST PACKET] DMB research context (not a trade instruction)")
    text = json.dumps(d).lower()
    for word in ("buy", "sell", "quantity", "stop_loss", "target", "entry_price", "order_type", "product", "leverage"):
        assert f'"{word}"' not in text, word
    assert "aligned_positive" not in text                # evidence direction never reaches the form
    assert _digest(inbox, TRADING_TABLES) == before         # no ticket, nothing changed
    assert [e["event_type"] for e in _items(inbox)["INFY"]["history"]] == ["IMPORTED", "TICKET_STARTED"]


@pytest.mark.parametrize("why", ["degraded", "stale", "superseded", "dismissed", "unresolved", "uncovered"])
def test_start_ticket_refuses_a_non_actionable_item(inbox, why, monkeypatch):
    if why == "unresolved":
        _import(inbox, B.bundle([B.packet("NOTCACHED")]))
        symbol = "NOTCACHED"
    else:
        _import(inbox, _ok_bundle())
        symbol = "TCS" if why == "degraded" else "INFY"
    if why == "stale":
        inbox.clock["now"] = B.AFTER_REVIEW
    if why == "superseded":
        _import(inbox, B.bundle([B.packet("INFY", evidence_as_of="2026-11-02T20:10:00+05:30",
                                          generated_at="2026-11-02T20:15:00+05:30")],
                                generated_at="2026-11-02T20:15:00+05:30"))
        item = _rows(inbox, "SELECT id FROM dmb_inbox_items WHERE superseded_by_item_id IS NOT NULL")[0]["id"]
    if why == "dismissed":
        inbox.client.post(f"/api/dmb-inbox/items/{_items(inbox)['INFY']['id']}/dismiss", json={})
    if why == "uncovered":
        from backend import trading_calendar
        monkeypatch.setattr(trading_calendar, "coverage", lambda year: "UNCOVERED")
    if why != "superseded":
        item = _rows(inbox, "SELECT id FROM dmb_inbox_items WHERE symbol = ?", symbol)[0]["id"]
    r = inbox.client.post(f"/api/dmb-inbox/items/{item}/start-ticket")
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "DMB_ITEM_NOT_ACTIONABLE", r.text
    expected = {"degraded": "QUALITY_REPLAY_NOT_MATCHED", "stale": "EXPIRED", "superseded": "SUPERSEDED",
                "dismissed": "DISMISSED", "unresolved": "INSTRUMENT_UNRESOLVED",
                "uncovered": "CALENDAR_UNCOVERED"}[why]
    assert expected in r.json()["detail"]["reasons"]
    assert not any(e["event_type"] == "TICKET_STARTED" for e in
                   _rows(inbox, "SELECT event_type FROM dmb_inbox_events"))


def test_evidence_direction_never_sets_a_side(inbox):
    _import(inbox, B.bundle([B.packet("INFY", direction="ALIGNED_NEGATIVE")]))
    _, r = _start(inbox)
    assert r.status_code == 200 and set(r.json()["data"]["prefill"]) == {"symbol", "exchange", "rationale"}
    assert "NEGATIVE" not in r.json()["data"]["prefill"]["rationale"]


def _create_manual_ticket(env, symbol="INFY", **over):
    fields = {"entry_price": 1500.0, "stop_loss": 1450.0, "target_1": 1650.0, "quantity": 3,
              "rationale": "typed by the user", **over}
    if symbol == "TCS":
        fields.update(entry_price=3000.0, stop_loss=2900.0, target_1=3300.0)
    body = _manual_ticket(symbol=symbol, **fields)
    r = env.client.post("/api/tickets", json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_the_manual_ticket_goes_through_p1d_and_is_linked_without_being_changed(inbox, market_open):
    set_mode(inbox.db, "dry_run")
    _import(inbox, _ok_bundle())
    item, r = _start(inbox)
    start = r.json()["data"]
    ticket = _create_manual_ticket(inbox, rationale=start["prefill"]["rationale"])
    # the existing P1-D creation path ran: the review was stamped from the stored row
    row = _rows(inbox, "SELECT * FROM trade_tickets WHERE id = ?", ticket["id"])[0]
    assert row["source"] == "manual" and row["reviewed_at"] and row["review_digest"] and row["reviewed_session_id"]
    assert ticket["review"]["subject"] is True and ticket["review"]["current"] is True
    before = _digest(inbox, ("trade_tickets", "ticket_events"))
    r = inbox.client.post(f"/api/dmb-inbox/items/{item}/ticket-link",
                          json={"ticket_id": ticket["id"], "start_event_id": start["start_event_id"],
                                "handoff_nonce": start["handoff_nonce"]})
    assert r.status_code == 201, r.text
    assert r.json()["data"]["ticket_links"][0]["ticket_id"] == ticket["id"]
    assert _digest(inbox, ("trade_tickets", "ticket_events")) == before
    # P1-D still guards the approval: a confirm without the review token is refused, nothing sent
    r = inbox.client.post(f"/api/tickets/{ticket['id']}/confirm", json={"expected_mode": "dry_run"})
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "TICKET_REVIEW_CHANGED", r.text
    assert [n for n, _ in inbox.kite.calls if n in BROKER_WRITES] == []
    # and the normal approval works exactly as for any manual ticket (dry run, paper broker)
    r = inbox.client.post(f"/api/tickets/{ticket['id']}/confirm", json=approval_body(inbox.db, ticket["id"]))
    assert r.status_code == 200, r.text
    assert [n for n, _ in inbox.kite.calls if n in BROKER_WRITES] == []


@pytest.mark.parametrize("case", ["wrong_nonce", "other_symbol", "created_before", "double", "other_item"])
def test_link_ticket_refuses_anything_not_started_from_the_item(inbox, case):
    _import(inbox, _ok_bundle())
    if case == "created_before":
        early = _create_manual_ticket(inbox)
    item, r = _start(inbox)
    start = r.json()["data"]
    ticket = early if case == "created_before" else _create_manual_ticket(
        inbox, symbol="TCS" if case == "other_symbol" else "INFY")
    if case == "created_before":
        conn = inbox.db.get_connection()
        try:   # the ticket existed before Start ticket
            conn.execute("UPDATE trade_tickets SET created_at = datetime('now', '-1 hour') WHERE id = ?", (early["id"],))
            conn.commit()
        finally:
            conn.close()
    body = {"ticket_id": ticket["id"], "start_event_id": start["start_event_id"],
            "handoff_nonce": "0" * 32 if case == "wrong_nonce" else start["handoff_nonce"]}
    target = _items(inbox)["TCS"]["id"] if case == "other_item" else item
    if case == "double":
        assert inbox.client.post(f"/api/dmb-inbox/items/{item}/ticket-link", json=body).status_code == 201
    r = inbox.client.post(f"/api/dmb-inbox/items/{target}/ticket-link", json=body)
    assert r.status_code == 409, r.text
    assert len(_rows(inbox, "SELECT * FROM dmb_inbox_ticket_links")) == (1 if case == "double" else 0)


def test_deleting_a_link_never_touches_the_ticket(inbox, market_open):
    _import(inbox, _ok_bundle())
    item, r = _start(inbox)
    start = r.json()["data"]
    ticket = _create_manual_ticket(inbox)
    inbox.client.post(f"/api/dmb-inbox/items/{item}/ticket-link",
                      json={"ticket_id": ticket["id"], "start_event_id": start["start_event_id"],
                            "handoff_nonce": start["handoff_nonce"]})
    before = _digest(inbox, ("trade_tickets", "ticket_events"))
    conn = inbox.db.get_connection()
    try:
        conn.execute("DELETE FROM dmb_inbox_ticket_links")
        conn.commit()
    finally:
        conn.close()
    assert _digest(inbox, ("trade_tickets", "ticket_events")) == before
    assert inbox.client.get(f"/api/tickets/{ticket['id']}").status_code == 200
    # and a ticket stays deletable/editable by its own rules: no foreign key points at it
    fks = _rows(inbox, "SELECT * FROM pragma_foreign_key_list('dmb_inbox_ticket_links')")
    assert "trade_tickets" not in {f["table"] for f in fks}


def test_the_inbox_never_touches_live_mode_or_strategy_config(inbox):
    before = _digest(inbox, ("execution_settings", "strategy_configs", "strategy_config_versions", "risk_settings"))
    _import(inbox, _ok_bundle())
    item, _ = _start(inbox)
    inbox.client.post(f"/api/dmb-inbox/items/{item}/research-watchlist", json={"watched": True})
    assert _digest(inbox, ("execution_settings", "strategy_configs", "strategy_config_versions",
                           "risk_settings")) == before


def test_the_inbox_module_imports_no_broker_or_execution_code():
    import ast
    import inspect
    from backend import dmb_inbox, dmb_inbox_contract
    from backend.routes import dmb_inbox as routes
    banned = ("kiteconnect", "backend.execution", "backend.risk_engine", "backend.strategy_engine",
              "backend.paper_broker", "backend.order_monitor", "backend.live_arming", "backend.watchlist",
              "requests", "urllib", "http.client", "socket", "subprocess")
    for module in (dmb_inbox, dmb_inbox_contract, routes):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for n in names:
                assert not any(n == b or n.startswith(b + ".") for b in banned), (module.__name__, n)
        assert "get_kite" not in inspect.getsource(module), module.__name__


def test_a_link_reported_by_lstat_is_refused_without_symlink_privileges(inbox, monkeypatch):
    """The symlink branch, exercised where creating a real symlink needs admin rights."""
    import stat as st_mod
    from backend import dmb_inbox
    b = _ok_bundle()
    target = B.write(inbox.handoff, b)
    real_lstat = os.lstat

    class LinkStat:
        def __init__(self, base):
            self._b = base
            self.st_mode = st_mod.S_IFLNK | 0o777

        def __getattr__(self, name):
            return getattr(self._b, name)

    monkeypatch.setattr(dmb_inbox.os, "lstat",
                        lambda p, *a, **k: LinkStat(real_lstat(p)) if os.fspath(p) == target else real_lstat(p))
    r = _import(inbox, b, path=True)
    assert r.status_code == 422 and r.json()["detail"]["error_code"] == "PATH_REFUSED"
