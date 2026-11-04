"""DMB Research Inbox: Daily Market Byte candidate packets, imported on request, for research.

One-way and file-based. Daily Market Byte writes immutable bundles into its handoff directory;
this module READS one of them when the user asks (`import_bundle`) and keeps every packet
version in its own tables (migration v9). There is no network connection between the two
applications, no automatic import, no automatic ticket and no broker call anywhere here: this
module never builds a Kite client and never writes `trade_tickets`.

PRIVATE RESEARCH CONTEXT - NOT A TRADE RECOMMENDATION OR ORDER INSTRUCTION. A packet carries
evidence and its provenance; it carries no side, quantity or price level, and its evidence
`direction` is never a trade side. The one bridge to trading is `start_ticket`, which only
re-checks that a packet is still actionable and hands the existing manual-ticket form a symbol,
an exchange and a research rationale. Everything else - side, quantity, prices, order type -
is entered by the user, and the ticket then goes through the unchanged manual-ticket path
(risk checks, P1-D review, live gates, confirmation).

File access (`read_bundle_file`):
- only inside the directory named by KITE_DMB_HANDOFF_DIR; unset = import disabled;
- the request names a session date and a bundle id (both pattern-checked), never a path:
  the file is `<dir>/<YYYY-MM-DD>/<64 hex>.json`;
- the directory, the session folder and the file must not be symlinks, junctions or other
  reparse points; the resolved path must stay inside the directory; a file with more than one
  hard link is refused; the opened handle must be the file that was checked (device + inode);
- read-only open, bounded read; nothing in the handoff directory is ever written or deleted.

Every import attempt is recorded (`dmb_inbox_imports`), rejected ones included. A bundle is
validated completely (backend/dmb_inbox_contract.py) before anything is written, and stored in
one transaction. Importing the same packets again creates nothing (DUPLICATE).
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

from backend import dmb_inbox_contract as contract
from backend import integration_profile, trading_calendar
from backend.db import get_connection

ENV_VAR = "KITE_DMB_HANDOFF_DIR"
DISCLAIMER = "PRIVATE RESEARCH CONTEXT — NOT A TRADE RECOMMENDATION OR ORDER INSTRUCTION."
NOTE_MAX = 1000
RATIONALE_MAX = 900
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (symlinks, junctions, mount points)


class InboxError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def now_ist() -> datetime:
    """The inbox's one clock (tests pin it)."""
    return trading_calendar.now_ist()


def policy_problem(packet: dict):
    """The integration profile's verdict on one packet (backend/integration_profile.py):
    synthetic packets only in the isolated synthetic test profile, real DMB evidence only for
    sources whose private-handoff right is confirmed in this terminal (none today)."""
    return contract.handoff_policy_problem(packet, synthetic_allowed=integration_profile.synthetic_test_active(),
                                           real_sources=integration_profile.permitted_real_sources())


def _utc_now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- the handoff directory
def handoff_root() -> Path | None:
    raw = (os.environ.get(ENV_VAR) or "").strip()
    return Path(raw) if raw else None


def _is_link(st) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:
        return False


def _checked_root() -> Path:
    root = handoff_root()
    if root is None:
        raise contract.BundleRejected("HANDOFF_NOT_CONFIGURED",
                                      f"set {ENV_VAR} to Daily Market Byte's handoff directory")
    if not root.is_absolute():
        raise contract.BundleRejected("PATH_REFUSED", f"{ENV_VAR} must be an absolute path")
    try:
        st = os.lstat(root)
    except OSError:
        raise contract.BundleRejected("HANDOFF_NOT_CONFIGURED", "the configured handoff directory does not exist")
    if _is_link(st) or not stat.S_ISDIR(st.st_mode):
        raise contract.BundleRejected("PATH_REFUSED", "the handoff directory is a link or not a directory")
    return root


def list_bundles() -> list:
    """What the user may choose to import: `<session>/<bundle id>.json` entries only, newest
    session first. Anything else in the directory (logs, temp files, links) is ignored."""
    root = _checked_root()
    out = []
    for session_dir in sorted(os.scandir(root), key=lambda e: e.name, reverse=True):
        if not contract.DATE_RE.fullmatch(session_dir.name):
            continue
        st = session_dir.stat(follow_symlinks=False)
        if _is_link(st) or not stat.S_ISDIR(st.st_mode):
            continue
        for entry in sorted(os.scandir(session_dir.path), key=lambda e: e.name):
            stem, dot, ext = entry.name.rpartition(".")
            if ext != "json" or not contract.HEX64_RE.fullmatch(stem):
                continue
            est = entry.stat(follow_symlinks=False)
            if _is_link(est) or not stat.S_ISREG(est.st_mode):
                continue
            out.append({"session_date": session_dir.name, "bundle_id": stem, "size": est.st_size})
    return out


def read_bundle_file(session_date: str, bundle_id: str) -> tuple:
    """`(raw bytes, sha256 hex, size)` of `<root>/<session_date>/<bundle_id>.json`, or
    BundleRejected. Never accepts a path from the caller; never writes."""
    if not isinstance(session_date, str) or not contract.DATE_RE.fullmatch(session_date):
        raise contract.BundleRejected("PATH_REFUSED", "session_date must be YYYY-MM-DD")
    if not isinstance(bundle_id, str) or not contract.HEX64_RE.fullmatch(bundle_id):
        raise contract.BundleRejected("PATH_REFUSED", "bundle_id must be 64 lower-case hex characters")
    root = _checked_root()
    root_real = os.path.realpath(root)
    session_dir = root / session_date
    target = session_dir / f"{bundle_id}.json"
    try:
        dst = os.lstat(session_dir)
        fst = os.lstat(target)
    except FileNotFoundError:
        raise contract.BundleRejected("BUNDLE_NOT_FOUND", f"no bundle {bundle_id[:12]}… for {session_date}")
    if _is_link(dst) or not stat.S_ISDIR(dst.st_mode):
        raise contract.BundleRejected("PATH_REFUSED", "the session folder is a link or not a directory")
    if _is_link(fst) or not stat.S_ISREG(fst.st_mode):
        raise contract.BundleRejected("PATH_REFUSED", "the bundle is a link or not a regular file")
    if fst.st_nlink > 1:
        raise contract.BundleRejected("FILE_SUBSTITUTION", "the bundle file has more than one hard link")
    if fst.st_size > contract.MAX_BUNDLE_BYTES:
        raise contract.BundleRejected("BUNDLE_TOO_LARGE", f"{fst.st_size} bytes")
    real = os.path.realpath(target)
    expected = os.path.join(root_real, session_date, f"{bundle_id}.json")
    if not _inside(real, root_real) or os.path.normcase(real) != os.path.normcase(expected):
        raise contract.BundleRejected("PATH_REFUSED", "the bundle resolves outside the handoff directory")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags)
    try:
        ost = os.fstat(fd)
        if (ost.st_dev, ost.st_ino) != (fst.st_dev, fst.st_ino) or not stat.S_ISREG(ost.st_mode):
            raise contract.BundleRejected("FILE_SUBSTITUTION", "the bundle changed while it was being opened")
        chunks, total = [], 0
        while total <= contract.MAX_BUNDLE_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > contract.MAX_BUNDLE_BYTES:
        raise contract.BundleRejected("BUNDLE_TOO_LARGE", f"more than {contract.MAX_BUNDLE_BYTES} bytes")
    import hashlib
    return raw, hashlib.sha256(raw).hexdigest(), len(raw)


# ---------------------------------------------------------------- import
def _record_rejection(account_id: int, session_id: int | None, session_date, bundle_id,
                      exc: contract.BundleRejected, sha: str | None, size: int | None) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO dmb_inbox_imports (account_id, requested_session_date, requested_bundle_id, status, "
            "reason_code, reason_detail, file_sha256, file_size, session_id) VALUES (?, ?, ?, 'REJECTED', ?, ?, ?, ?, ?)",
            (account_id, str(session_date)[:16], str(bundle_id)[:64], exc.code, exc.detail[:500], sha, size,
             session_id))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _event(conn, account_id: int, event_type: str, *, item_id=None, import_id=None, detail=None,
           session_id=None) -> int:
    cur = conn.execute(
        "INSERT INTO dmb_inbox_events (account_id, item_id, import_id, event_type, detail, session_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, item_id, import_id, event_type,
         json.dumps(detail, sort_keys=True) if detail is not None else None, session_id))
    return cur.lastrowid


def _content(packet: dict) -> bytes:
    return contract.canonical_bytes({k: v for k, v in packet.items() if k != "generated_at"})


def _apply_supersession(conn, account_id: int, logical_key: str, session_id) -> None:
    """The newest version (evidence_as_of, then generated_at, then import order) is current;
    every older version not yet marked is marked superseded by it - once, never undone."""
    rows = conn.execute(
        "SELECT id, superseded_by_item_id FROM dmb_inbox_items WHERE account_id = ? AND logical_key = ? "
        "ORDER BY evidence_as_of_utc, generated_at_utc, id", (account_id, logical_key)).fetchall()
    if len(rows) < 2:
        return
    current = rows[-1]["id"]
    for r in rows[:-1]:
        if r["superseded_by_item_id"] is None:
            conn.execute("UPDATE dmb_inbox_items SET superseded_by_item_id = ?, superseded_at = ? WHERE id = ?",
                         (current, _utc_now_sql(), r["id"]))
            _event(conn, account_id, "SUPERSEDED", item_id=r["id"], detail={"superseded_by_item_id": current},
                   session_id=session_id)


def import_bundle(account_id: int, session_id: int | None, session_date: str, bundle_id: str) -> dict:
    """Validate the whole bundle, then store it in one transaction. Always records the attempt."""
    now = now_ist()
    sha = size = None
    try:
        raw, sha, size = read_bundle_file(session_date, bundle_id)
        bundle, facts = contract.parse_and_validate(raw, now)
        if bundle["bundle_id"] != bundle_id or bundle["session_date"] != session_date:
            raise contract.BundleRejected("FILE_SUBSTITUTION", "the file name does not match the bundle it holds")
        for p in bundle["packets"]:
            problem = policy_problem(p)
            if problem is not None:
                raise contract.BundleRejected(*problem)
    except contract.BundleRejected as exc:
        import_id = _record_rejection(account_id, session_id, session_date, bundle_id, exc, sha, size)
        return {"status": "REJECTED", "import_id": import_id, "reason_code": exc.code, "reason": exc.detail}

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        new, conflict = [], None
        for p, f in zip(bundle["packets"], facts):
            row = conn.execute("SELECT packet_json FROM dmb_inbox_items WHERE account_id = ? AND packet_id = ?",
                               (account_id, p["packet_id"])).fetchone()
            if row is None:
                new.append((p, f))
            elif _content(json.loads(row["packet_json"])) != _content(p):
                conflict = p["packet_id"]
                break
        if conflict:
            conn.rollback()
            exc = contract.BundleRejected("CONFLICTING_PACKET",
                                          f"packet {conflict[:12]} is already stored with different content")
            import_id = _record_rejection(account_id, session_id, session_date, bundle_id, exc, sha, size)
            return {"status": "REJECTED", "import_id": import_id, "reason_code": exc.code, "reason": exc.detail}
        status = "IMPORTED" if new else "DUPLICATE"
        cur = conn.execute(
            "INSERT INTO dmb_inbox_imports (account_id, requested_session_date, requested_bundle_id, status, "
            "file_sha256, file_size, bundle_schema, bundle_generated_at, packet_count, new_item_count, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, session_date, bundle_id, status, sha, size, bundle["schema_version"],
             bundle["generated_at"], len(bundle["packets"]), len(new), session_id))
        import_id = cur.lastrowid
        keys = set()
        for p, f in new:
            key = contract.logical_key(p)
            keys.add(key)
            cur = conn.execute(
                "INSERT INTO dmb_inbox_items (account_id, packet_id, logical_key, symbol, listing_exchange, "
                "session_date, review_session_date, evidence_as_of, evidence_as_of_utc, generated_at, "
                "generated_at_utc, data_origin, rights_status, bundle_id, import_id, packet_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, p["packet_id"], key, p["symbol"], p["listing_exchange"], p["session_date"],
                 p["review_session_date"], p["evidence_as_of"], contract.utc_sql(f["evidence_as_of"]),
                 p["generated_at"], contract.utc_sql(f["generated_at"]), p["rights"]["data_origin"],
                 p["rights"]["handoff_status"], bundle_id, import_id,
                 json.dumps(p, sort_keys=True, ensure_ascii=False)))
            _event(conn, account_id, "IMPORTED", item_id=cur.lastrowid, import_id=import_id,
                   detail={"bundle_id": bundle_id, "file_sha256": sha}, session_id=session_id)
        for key in sorted(keys):
            _apply_supersession(conn, account_id, key, session_id)
        conn.commit()
        return {"status": status, "import_id": import_id, "packet_count": len(bundle["packets"]),
                "new_item_count": len(new), "bundle_id": bundle_id, "file_sha256": sha}
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def list_imports(account_id: int, limit: int = 50) -> list:
    conn = get_connection()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM dmb_inbox_imports WHERE account_id = ? ORDER BY id DESC LIMIT ?",
            (account_id, limit)).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------- items and actionability
def _instrument_resolved(conn, symbol: str, exchange: str) -> bool:
    """The LOCAL instrument cache only: never a broker call to validate a symbol."""
    return conn.execute("SELECT 1 FROM instrument_cache WHERE tradingsymbol = ? AND exchange = ?",
                        (symbol, exchange)).fetchone() is not None


def _state(conn, item_id: int) -> dict:
    events = [dict(r) for r in conn.execute(
        "SELECT id, event_type, detail, created_at FROM dmb_inbox_events WHERE item_id = ? ORDER BY id",
        (item_id,)).fetchall()]
    for e in events:
        e["detail"] = json.loads(e["detail"]) if e["detail"] else None
    watch = None
    for e in events:
        if e["event_type"] in ("WATCHLIST_ADDED", "WATCHLIST_REMOVED"):
            watch = e["event_type"] == "WATCHLIST_ADDED"
    links = [dict(r) for r in conn.execute(
        "SELECT id, ticket_id, start_event_id, linked_at FROM dmb_inbox_ticket_links WHERE item_id = ? ORDER BY id",
        (item_id,)).fetchall()]
    return {"events": events,
            "dismissed": any(e["event_type"] == "DISMISSED" for e in events),
            "notes": [{"text": e["detail"]["text"], "at": e["created_at"]} for e in events
                      if e["event_type"] == "NOTE_ADDED"],
            "on_research_watchlist": bool(watch), "ticket_links": links}


def actionability(conn, item, packet: dict, state: dict, now: datetime) -> dict:
    """Re-evaluated on every read and again at Start ticket - never cached."""
    reasons = []
    if item["superseded_by_item_id"] is not None:
        reasons.append("SUPERSEDED")
    if state["dismissed"]:
        reasons.append("DISMISSED")
    if contract.packet_id_for(packet) != item["packet_id"]:
        reasons.append("INTEGRITY_MISMATCH")
    if not contract.rights_permitted(packet.get("rights") or {}):
        reasons.append("RIGHTS_NOT_APPROVED")
    problem = policy_problem(packet)
    if problem is not None and problem[0] not in reasons:
        reasons.append(problem[0])
    session = contract.parse_date(item["session_date"], "session_date")
    review = contract.parse_date(item["review_session_date"], "review_session_date")
    try:
        contract.check_sessions(session, review, now)
    except contract.BundleRejected as exc:
        reasons.append(exc.code)
    try:
        expires = contract.expires_at(review)
    except contract.BundleRejected as exc:          # the calendar cannot establish it: fail closed
        expires = None
        if exc.code not in reasons:
            reasons.append(exc.code)
    if expires is None or now >= expires:
        reasons.append("EXPIRED")
    q = packet.get("quality") or {}
    if not q.get("freshness_ok"):
        reasons.append("QUALITY_FRESHNESS")
    if not q.get("radar_coverage_ok"):
        reasons.append("QUALITY_RADAR_COVERAGE")
    if q.get("replay_status") != "MATCHED":
        reasons.append("QUALITY_REPLAY_NOT_MATCHED")
    if not _instrument_resolved(conn, item["symbol"], item["listing_exchange"]):
        reasons.append("INSTRUMENT_UNRESOLVED")
    return {"actionable": not reasons, "reasons": reasons,
            "expires_at": expires.isoformat() if expires is not None else None}


def _view(conn, item, now: datetime) -> dict:
    packet = json.loads(item["packet_json"])
    state = _state(conn, item["id"])
    act = actionability(conn, item, packet, state, now)
    return {
        "id": item["id"], "packet_id": item["packet_id"], "logical_key": item["logical_key"],
        "symbol": item["symbol"], "exchange": item["listing_exchange"],
        "session_date": item["session_date"], "review_session_date": item["review_session_date"],
        "evidence_as_of": item["evidence_as_of"], "generated_at": item["generated_at"],
        "imported_at": item["created_at"], "import_id": item["import_id"], "bundle_id": item["bundle_id"],
        "data_origin": item["data_origin"],
        "synthetic": item["data_origin"] == "SYNTHETIC" or bool(contract.synthetic_markers(packet)),
        "integration_profile": integration_profile.current(),
        "superseded_by_item_id": item["superseded_by_item_id"], "superseded_at": item["superseded_at"],
        "company": packet.get("company"), "sector": packet.get("sector"),
        "rationale": list(packet.get("reasons") or []), "detector_ids": list(packet.get("detector_ids") or []),
        "radar_state": packet.get("radar_state"), "metrics": packet.get("metrics"),
        "market_context": packet.get("market_context"), "sector_context": packet.get("sector_context"),
        "market_regime": packet.get("market_regime"), "official_events": packet.get("official_events"),
        "quality": packet.get("quality"), "rights": packet.get("rights"),
        "source_artifact_ids": packet.get("source_artifact_ids"), "producer": packet.get("producer"),
        "notes_from_dmb": packet.get("notes"),
        **{k: state[k] for k in ("dismissed", "notes", "on_research_watchlist", "ticket_links")},
        "history": [{"event_type": e["event_type"], "at": e["created_at"], "detail": e["detail"]}
                    for e in state["events"]],
        **act,
    }


def list_items(account_id: int, include_superseded: bool = False, watchlist_only: bool = False) -> list:
    now = now_ist()
    conn = get_connection()
    try:
        sql = "SELECT * FROM dmb_inbox_items WHERE account_id = ?"
        if not include_superseded:
            sql += " AND superseded_by_item_id IS NULL"
        rows = conn.execute(sql + " ORDER BY session_date DESC, symbol, id DESC", (account_id,)).fetchall()
        views = [_view(conn, r, now) for r in rows]
    finally:
        conn.close()
    if watchlist_only:
        views = [v for v in views if v["on_research_watchlist"]]
    return views


def _item_or_404(conn, account_id: int, item_id: int):
    row = conn.execute("SELECT * FROM dmb_inbox_items WHERE id = ? AND account_id = ?",
                       (item_id, account_id)).fetchone()
    if row is None:
        raise InboxError(404, "NOT_FOUND", "Inbox item not found")
    return row


def get_item(account_id: int, item_id: int) -> dict:
    conn = get_connection()
    try:
        item = _item_or_404(conn, account_id, item_id)
        view = _view(conn, item, now_ist())
        view["versions"] = [{"id": r["id"], "packet_id": r["packet_id"], "evidence_as_of": r["evidence_as_of"],
                             "generated_at": r["generated_at"], "superseded_by_item_id": r["superseded_by_item_id"]}
                            for r in conn.execute(
                                "SELECT * FROM dmb_inbox_items WHERE account_id = ? AND logical_key = ? "
                                "ORDER BY evidence_as_of_utc, generated_at_utc, id",
                                (account_id, item["logical_key"])).fetchall()]
        return view
    finally:
        conn.close()


# ---------------------------------------------------------------- user actions
def _act(account_id: int, item_id: int, session_id, event_type: str, detail=None) -> dict:
    conn = get_connection()
    try:
        _item_or_404(conn, account_id, item_id)
        _event(conn, account_id, event_type, item_id=item_id, detail=detail, session_id=session_id)
        conn.commit()
    finally:
        conn.close()
    return get_item(account_id, item_id)


def dismiss(account_id: int, item_id: int, session_id, reason: str | None) -> dict:
    conn = get_connection()
    try:
        _item_or_404(conn, account_id, item_id)
        if _state(conn, item_id)["dismissed"]:
            raise InboxError(409, "ALREADY_DISMISSED", "This inbox item is already dismissed.")
    finally:
        conn.close()
    return _act(account_id, item_id, session_id, "DISMISSED", {"reason": (reason or "").strip()[:NOTE_MAX] or None})


def add_note(account_id: int, item_id: int, session_id, text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise InboxError(400, "VALIDATION_ERROR", "A note cannot be empty.")
    return _act(account_id, item_id, session_id, "NOTE_ADDED", {"text": text[:NOTE_MAX]})


def set_research_watch(account_id: int, item_id: int, session_id, watched: bool) -> dict:
    """The inbox's OWN research watchlist (events on the item). Deliberately not the Kite
    `watchlist` table: that table is the strategy engine's scan universe, and an inbox action
    must never change what the strategies scan or trade."""
    return _act(account_id, item_id, session_id, "WATCHLIST_ADDED" if watched else "WATCHLIST_REMOVED")


def _rationale(view: dict) -> str:
    prefix = "[SYNTHETIC TEST PACKET] " if view["synthetic"] else ""
    head = (f"{prefix}DMB research context (not a trade instruction): packet {view['packet_id'][:12]}, "
            f"session {view['session_date']}, evidence as of {view['evidence_as_of']}. ")
    body = " ".join(view["rationale"])
    text = head + body
    return text if len(text) <= RATIONALE_MAX else text[:RATIONALE_MAX - 1] + "…"


def start_ticket(account_id: int, item_id: int, session_id) -> dict:
    """Re-checks the packet is still actionable, records the user's explicit request, and
    returns ONLY what the manual-ticket form may be prefilled with: symbol, exchange and a
    research rationale. Creates no ticket and calls no broker."""
    view = get_item(account_id, item_id)
    if not view["actionable"]:
        raise InboxError(409, "DMB_ITEM_NOT_ACTIONABLE",
                         "This research item cannot start a ticket: " + ", ".join(view["reasons"]) + ".",
                         reasons=view["reasons"])
    nonce = secrets.token_hex(16)
    conn = get_connection()
    try:
        event_id = _event(conn, account_id, "TICKET_STARTED", item_id=item_id, detail={"nonce": nonce},
                          session_id=session_id)
        conn.commit()
    finally:
        conn.close()
    return {"start_event_id": event_id, "handoff_nonce": nonce,
            "prefill": {"symbol": view["symbol"], "exchange": view["exchange"], "rationale": _rationale(view)},
            "disclaimer": DISCLAIMER}


def link_ticket(account_id: int, item_id: int, session_id, ticket_id: int, start_event_id: int,
                handoff_nonce: str) -> dict:
    """Associates a manual ticket the user created after Start ticket with the inbox item.
    Reads trade_tickets, never writes it."""
    conn = get_connection()
    try:
        item = _item_or_404(conn, account_id, item_id)
        problem = policy_problem(json.loads(item["packet_json"]))
        if problem is not None:
            raise InboxError(409, problem[0], "This research item cannot be linked to a ticket here: " + problem[1] + ".")
        start = conn.execute(
            "SELECT * FROM dmb_inbox_events WHERE id = ? AND account_id = ? AND item_id = ? AND event_type = 'TICKET_STARTED'",
            (start_event_id, account_id, item_id)).fetchone()
        detail = json.loads(start["detail"]) if start is not None and start["detail"] else {}
        if start is None or not secrets.compare_digest(str(detail.get("nonce", "")), str(handoff_nonce or "")):
            raise InboxError(409, "DMB_LINK_REFUSED", "That ticket was not started from this research item.")
        ticket = conn.execute("SELECT id, account_id, source, symbol, exchange, created_at FROM trade_tickets "
                              "WHERE id = ? AND account_id = ?", (ticket_id, account_id)).fetchone()
        if ticket is None:
            raise InboxError(404, "NOT_FOUND", "Ticket not found")
        if (ticket["source"] != "manual" or ticket["symbol"] != item["symbol"]
                or ticket["exchange"] != item["listing_exchange"] or ticket["created_at"] < start["created_at"]):
            raise InboxError(409, "DMB_LINK_REFUSED",
                             "Only a manual ticket for the same symbol, created after Start ticket, can be linked.")
        try:
            cur = conn.execute(
                "INSERT INTO dmb_inbox_ticket_links (account_id, item_id, ticket_id, start_event_id) VALUES (?, ?, ?, ?)",
                (account_id, item_id, ticket_id, start_event_id))
        except sqlite3.IntegrityError:
            raise InboxError(409, "DMB_LINK_REFUSED", "This ticket or this Start ticket is already linked.")
        _event(conn, account_id, "TICKET_LINKED", item_id=item_id,
               detail={"ticket_id": ticket_id, "link_id": cur.lastrowid, "start_event_id": start_event_id},
               session_id=session_id)
        conn.commit()
    finally:
        conn.close()
    return get_item(account_id, item_id)


__all__ = ["ENV_VAR", "DISCLAIMER", "InboxError", "handoff_root", "list_bundles", "read_bundle_file",
           "import_bundle", "list_imports", "list_items", "get_item", "actionability", "dismiss",
           "add_note", "set_research_watch", "start_ticket", "link_ticket", "now_ist"]
