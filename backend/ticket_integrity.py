"""Post-claim tampering auditability: claim snapshots and tamper detection for trade tickets.

SEC-1 P1-D binds a manual / Quick-Action BUY's identity and economic fields to its human review
(account_safety.ticket_review_digest) and rejects pre-claim tampering for good. Once a ticket is
CLAIMED for execution its row is no longer editable through the API, but a write outside the app
could still change it. Before this module such a change was invisible: the broker was sent the
verified in-memory row, and the tampered database row was left as it was, unrecorded.

What this module adds (every ticket claimed through the confirm path, any source):

- At the claim, `record_claim` appends a CLAIM_SNAPSHOT: the overall digest (exactly P1-D's
  `ticket_review_digest`, so a reviewed ticket's snapshot equals its review digest) and one digest
  per economic field category. It fingerprints the row the confirmation decision was made on.
- At each trusted validation point after the claim, `check` compares the persisted row with the
  snapshot. A difference appends one TAMPER_DETECTED event (ticket, changed categories, claimed and
  current digests, stage, detection time) and the caller refuses the path:
    * `post_claim`  - the row re-read right after the claim: the claim is withdrawn and the ticket
                      REJECTED (permanent), nothing is sent;
    * `pre_broker`  - re-read immediately before the broker call: same;
    * `post_execution` - after the broker call: the broker was sent the verified values; the
                      detection is recorded and the result reports it;
    * `trailing`    - before the trailing engine re-sends a GTT built from persisted fields: the
                      modification is skipped (the existing protective GTT stays).
  A ticket with a TAMPER_DETECTED event can never be confirmed again or reverted from rejection
  (`tampered`), so no fresh review or claim is ever granted to tampered content.
- Events are append-only (database triggers) and hash-chained (`entry_hash` covers the previous
  entry's hash and every field), so `verify_chain` detects an edited, deleted or re-ordered entry.

Recorded: ids, stage, category names and SHA-256 digests only - never values, prices, quantities,
credentials or account details.

Boundary: this is application-level detection. Someone who can write the SQLite file directly can
also drop the triggers and rewrite the whole chain consistently; the chain then proves nothing. It
detects tampering by anything that goes through SQL without rewriting this log - not an attacker
with full control of the database file (docs/POST_CLAIM_TAMPERING_AUDIT.md).
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone

from backend import account_safety

logger = logging.getLogger(__name__)

CLAIM_SNAPSHOT = "CLAIM_SNAPSHOT"
TAMPER_DETECTED = "TAMPER_DETECTED"
TAMPER_CODE = "TICKET_TAMPERED_AFTER_CLAIM"
STAGES = ("claim", "post_claim", "pre_broker", "post_execution", "trailing")
GENESIS = "0" * 64

# Every field of P1-D's review digest, grouped by what it means. Together exactly the digest's
# fields (tests/test_ticket_integrity.py asserts it), so the overall digest and the categories
# can never disagree about what is covered.
CATEGORIES = {
    "identity": ("id", "account_id", "source", "closes_ticket_id", "symbol", "exchange", "instrument_token"),
    "side": ("transaction_type",),
    "quantity": ("quantity",),
    "prices": ("entry_price", "stop_loss", "target_1", "target_2"),
    "order_type": ("entry_order_type", "product_type"),
    "protection": ("use_gtt", "trailing_enabled", "trailing_distance", "trailing_trigger"),
}
_INT = frozenset(account_safety._DIGEST_INT_FIELDS)
_FLOAT = frozenset(account_safety._DIGEST_FLOAT_FIELDS)


def _value(row, name):
    v = row[name]
    if v is None:
        return None
    if name in _INT:
        return int(v)
    if name in _FLOAT:
        return float(v)
    return v


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def fingerprint(row) -> dict:
    return {"digest": account_safety.ticket_review_digest(row),
            "categories": {cat: _sha({f: _value(row, f) for f in fields}) for cat, fields in CATEGORIES.items()}}


def _now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _entry_hash(prev_hash: str, fields: dict) -> str:
    return _sha({"prev_hash": prev_hash, **fields})


_HASHED = ("account_id", "ticket_id", "event_type", "stage", "claimed_digest", "current_digest", "categories",
           "detected_at")


def _append(conn: sqlite3.Connection, **fields) -> int:
    last = conn.execute("SELECT entry_hash FROM ticket_integrity_events ORDER BY id DESC LIMIT 1").fetchone()
    prev = last["entry_hash"] if last is not None else GENESIS
    fields = {k: fields[k] for k in _HASHED}
    cur = conn.execute(
        "INSERT INTO ticket_integrity_events (account_id, ticket_id, event_type, stage, claimed_digest, "
        "current_digest, categories, detected_at, prev_hash, entry_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (*(fields[k] for k in _HASHED), prev, _entry_hash(prev, fields)))
    return cur.lastrowid


def record_claim(conn: sqlite3.Connection, account_id: int, row) -> int:
    """The snapshot of what this claim executes. Caller commits (in the claim's transaction)."""
    fp = fingerprint(row)
    return _append(conn, account_id=account_id, ticket_id=row["id"], event_type=CLAIM_SNAPSHOT, stage="claim",
                   claimed_digest=fp["digest"], current_digest=fp["digest"],
                   categories=json.dumps(fp["categories"], sort_keys=True), detected_at=_now_sql())


def _snapshot(conn: sqlite3.Connection, ticket_id: int):
    return conn.execute("SELECT * FROM ticket_integrity_events WHERE ticket_id = ? AND event_type = ? "
                        "ORDER BY id DESC LIMIT 1", (ticket_id, CLAIM_SNAPSHOT)).fetchone()


def changed_categories(snapshot, row) -> list[str]:
    claimed = json.loads(snapshot["categories"])
    current = fingerprint(row)["categories"]
    return sorted(cat for cat in CATEGORIES if claimed.get(cat) != current[cat])


def check(conn: sqlite3.Connection, account_id: int, row, stage: str) -> list[str]:
    """[] when the persisted row still matches its claim snapshot (or the ticket was never
    claimed through the confirm path); else the changed categories, after appending one
    TAMPER_DETECTED event. A repeat of the same detection (same stage, same current content) is
    not appended again. Caller commits."""
    if stage not in STAGES or stage == "claim":
        raise ValueError(f"unknown validation stage {stage!r}")
    snap = _snapshot(conn, row["id"])
    if snap is None:
        return []
    changed = changed_categories(snap, row)
    if not changed:
        return []
    current = fingerprint(row)["digest"]
    repeat = conn.execute(
        "SELECT 1 FROM ticket_integrity_events WHERE ticket_id = ? AND event_type = ? AND stage = ? "
        "AND current_digest = ? AND id > ?", (row["id"], TAMPER_DETECTED, stage, current, snap["id"])).fetchone()
    if repeat is None:
        _append(conn, account_id=account_id, ticket_id=row["id"], event_type=TAMPER_DETECTED, stage=stage,
                claimed_digest=snap["claimed_digest"], current_digest=current, categories=json.dumps(changed),
                detected_at=_now_sql())
        logger.warning("Ticket %s: economic fields changed after its claim (%s) detected at %s",
                       row["id"], ", ".join(changed), stage)
    return changed


def tampered(conn: sqlite3.Connection, ticket_id: int) -> bool:
    """Has tampering after a claim ever been detected for this ticket? Permanent."""
    return conn.execute("SELECT 1 FROM ticket_integrity_events WHERE ticket_id = ? AND event_type = ? LIMIT 1",
                        (ticket_id, TAMPER_DETECTED)).fetchone() is not None


def events(conn: sqlite3.Connection, account_id: int, ticket_id: int | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM ticket_integrity_events WHERE account_id = ?"
    args: list = [account_id]
    if ticket_id is not None:
        sql += " AND ticket_id = ?"
        args.append(ticket_id)
    rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["categories"] = json.loads(d["categories"]) if d["categories"] else None
        out.append(d)
    return out


def verify_chain(conn: sqlite3.Connection) -> dict:
    """Recomputes every entry hash in order. {ok, events, first_bad_id, problem}."""
    prev, n = GENESIS, 0
    for r in conn.execute("SELECT * FROM ticket_integrity_events ORDER BY id").fetchall():
        n += 1
        if r["prev_hash"] != prev:
            return {"ok": False, "events": n, "first_bad_id": r["id"], "problem": "chain broken (an entry is missing "
                                                                                 "or out of order)"}
        if _entry_hash(prev, {k: r[k] for k in _HASHED}) != r["entry_hash"]:
            return {"ok": False, "events": n, "first_bad_id": r["id"], "problem": "entry content changed"}
        prev = r["entry_hash"]
    return {"ok": True, "events": n, "first_bad_id": None, "problem": None}


__all__ = ["CLAIM_SNAPSHOT", "TAMPER_DETECTED", "TAMPER_CODE", "STAGES", "CATEGORIES", "fingerprint", "record_claim",
           "check", "changed_categories", "tampered", "events", "verify_chain"]
