"""Backup verification without exposing database contents (V12 final readiness, Phase D).

A backup counts for migrate-v12 only when ALL of these hold, checked live, never taken from a stale report:
  * it is a closed, self-contained SQLite file: no non-empty -wal / -journal of its own (an open or crashed database
    is not a backup), opened immutable + read-only so not a byte (and no sidecar) is written;
  * PRAGMA integrity_check is ok, and its migration history is one this build accepts;
  * its LOGICAL DIGEST (below) equals the logical digest of the database it claims to back up, so it is a copy of
    exactly that content, whatever the page layout or WAL state;
  * it lies in a SEPARATE PHYSICAL FAILURE DOMAIN from the database: another physical disk (a different disk number,
    a removable disk) or a network share on another host. A second partition of the same disk (this machine's E:,
    which is on the same NVMe disk 0 as D:) is NOT separate.
What software cannot prove: that the device is then disconnected or kept away from this machine (theft, fire,
ransomware). That is an operator attestation in the GO/NO-GO checklist.

The verification record (JSON) holds storage categories, sizes, hashes, the schema version name, the integrity result
and tool versions. It never holds a row, a table's contents, an account id, a token or a symbol.

The logical digest: sha256 over every schema object (type, name, table, SQL) in name order and every row of every
table in rowid order (WITHOUT ROWID tables: primary-key order), each value encoded with its SQLite type. sqlite_stat*
tables (planner statistics, rewritten by ANALYZE) are excluded. The digest reveals nothing about the contents.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from backend import storage_classification as sc

RECORD_TYPE = "kite_terminal_backup_verification"
RECORD_FORMAT = 1
PASS, FAIL, FAIL_SAME_DOMAIN = "PASS", "FAIL", "FAIL_SAME_FAILURE_DOMAIN"
MAX_RECORD_AGE_SECONDS = 12 * 3600  # migrate-v12 refuses an older record (and re-verifies a fresh one live anyway)

OPERATOR_ATTESTATION = (
    "Software verified the backup's integrity, content digest and that it is on separate physical storage. It cannot "
    "verify custody: the operator must confirm the device was disconnected / is kept away from this machine (or the "
    "share is on another physical host) and record it in the GO/NO-GO checklist.")


def _connect_offline(path: Path):
    """mode=ro + immutable: no lock, no sidecar, not a byte written (an offline copy no process uses)."""
    conn = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro&immutable=1", uri=True, timeout=10)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _connect_source(path: Path):
    """mode=ro (it reads the WAL, so committed transactions not yet checkpointed are included), query_only."""
    conn = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA query_only = ON")
    return conn


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _encode(value) -> bytes:
    if value is None:
        return b"N"
    if isinstance(value, int):
        text = str(value).encode("ascii")
        return b"I" + struct.pack("<I", len(text)) + text
    if isinstance(value, float):
        text = value.hex().encode("ascii")
        return b"F" + struct.pack("<I", len(text)) + text
    if isinstance(value, str):
        data = value.encode("utf-8", "surrogatepass")
        return b"T" + struct.pack("<I", len(data)) + data
    data = bytes(value)
    return b"B" + struct.pack("<I", len(data)) + data


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def logical_digest(conn) -> str:
    """The content digest of the database behind `conn` (read-only use). Never returns or logs any value."""
    digest = hashlib.sha256(b"kite-terminal-logical-digest-v1\x00")
    objects = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_stat%' "
        "ORDER BY type, name").fetchall()
    for obj in objects:
        digest.update(b"O" + b"".join(_encode(v) for v in obj))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_stat%' ORDER BY name")]
    for table in tables:
        q = _quoted(table)
        try:
            conn.execute(f"SELECT rowid FROM {q} LIMIT 0")
            order = "rowid"
            select = f"SELECT rowid, * FROM {q} ORDER BY rowid"
        except sqlite3.OperationalError:  # WITHOUT ROWID: its primary key orders it
            pk = [r[1] for r in sorted(conn.execute(f"PRAGMA table_info({q})"), key=lambda r: r[5]) if r[5]]
            order = ", ".join(_quoted(c) for c in pk) or "1"
            select = f"SELECT * FROM {q} ORDER BY {order}"
        digest.update(b"T" + _encode(table))
        count = 0
        for row in conn.execute(select):
            digest.update(b"R" + b"".join(_encode(v) for v in row))
            count += 1
        digest.update(b"C" + _encode(count))
    return digest.hexdigest()


def _schema_newest(conn) -> list | None:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'").fetchone():
        return None
    row = conn.execute("SELECT version, migration_name FROM schema_version ORDER BY version DESC LIMIT 1").fetchone()
    return [row[0], row[1]] if row else None


def _history(conn) -> list:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'").fetchone():
        return []
    return [{"version": r[0], "migration_name": r[1]}
            for r in conn.execute("SELECT version, migration_name FROM schema_version ORDER BY version")]


def git_head(repo: Path | None = None) -> str | None:
    """The repository HEAD read from .git (no git process, nothing written)."""
    repo = repo or Path(__file__).resolve().parents[1]
    try:
        head = (repo / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = head[5:]
            ref_file = repo / ".git" / ref
            if ref_file.exists():
                return ref_file.read_text(encoding="utf-8").strip()
            packed = repo / ".git" / "packed-refs"
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0]
            return None
        return head
    except OSError:
        return None


def tool_info(command: str) -> dict:
    return {"command": command, "module": "backend.backup_verification", "record_format": RECORD_FORMAT,
            "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version, "git_head": git_head()}


def failure_domain(source: sc.StorageVerdict | None, destination: sc.StorageVerdict) -> dict:
    """Is the destination in a separate physical failure domain from the source database?"""
    cat = destination.category
    same = sc.same_physical_disk(source, destination) if source is not None else None
    out = {"destination_category": cat, "source_category": source.category if source else None,
           "same_physical_disk": same, "separate_physical_storage": False, "reason": ""}
    if cat in sc.NETWORK_CATEGORIES:
        if destination.loopback:
            out["reason"] = "a network path back to THIS machine (loopback): the same physical storage"
        elif cat == sc.NETWORK_MAPPED and not (destination.details or {}).get("remote"):
            out["reason"] = "a mapped drive whose share (host) could not be established: separation not established"
        elif cat == sc.NETWORK_MAPPED or cat == sc.NETWORK_UNC:
            out.update(separate_physical_storage=True,
                       reason="a network share on another host (the operator attests the host is a separate machine)")
        else:
            out.update(separate_physical_storage=True, reason="network block storage")
        return out
    if cat == sc.REMOVABLE:
        if same:
            out["reason"] = "the removable volume shares a physical disk with the database"
        else:
            out.update(separate_physical_storage=True, reason="a separate removable disk")
        return out
    if cat == sc.FIXED_LOCAL:
        if same is None:
            out["reason"] = "the physical disks could not be compared: separation cannot be established"
        elif same:
            out["reason"] = (f"the destination is on the SAME physical disk as the database (disk "
                             f"{', '.join(map(str, destination.physical_disks))}): another partition or folder of "
                             "the same disk is not an off-machine copy")
        else:
            out.update(separate_physical_storage=True,
                       reason="a different internal physical disk (separate disk; still the same machine)")
        return out
    out["reason"] = f"the destination storage is {cat} ({destination.reason}): separation cannot be established"
    return out


def verify_backup_file(backup, *, source_db=None, command: str = "verify-backup", classify=None) -> dict:
    """The verification record for `backup` (and, with `source_db`, whether it is a copy of exactly that database).
    Read-only on both. Never raises for a bad backup: the record says FAIL and why."""
    classify = classify or sc.classify_path
    backup = Path(backup)
    now = time.time()
    rec = {"record_type": RECORD_TYPE, "format": RECORD_FORMAT,
           "verified_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
           "verified_at_epoch": now, "tool": tool_info(command), "problems": [],
           "operator_attestation_required": OPERATOR_ATTESTATION}
    dest_verdict = classify(backup.parent if not backup.exists() else backup)
    info = {"file_name": backup.name, "path": str(backup.resolve()) if backup.exists() else str(backup),
            "storage": dest_verdict.as_record()}
    rec["backup"] = info
    if not backup.is_file():
        rec["problems"].append("no backup file at the given path")
        rec["result"] = FAIL
        return rec
    sidecars = {s: Path(f"{backup}{s}").stat().st_size for s in ("-wal", "-journal", "-shm")
                if Path(f"{backup}{s}").exists()}
    info["sidecars"] = sidecars
    if sidecars.get("-wal") or sidecars.get("-journal"):
        rec["problems"].append(f"the backup has non-empty sidecar(s) {sorted(k for k, v in sidecars.items() if v)}: "
                               "it is not a closed, self-contained file")
    info["size"] = backup.stat().st_size
    info["sha256"] = file_sha256(backup)
    try:
        conn = _connect_offline(backup)
        try:
            integrity = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
            info["integrity_check"] = integrity[:10]
            info["foreign_key_violations"] = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            info["schema_newest"] = _schema_newest(conn)
            history = _history(conn)
            info["logical_digest"] = logical_digest(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        rec["problems"].append(f"the backup is not a readable SQLite database ({type(e).__name__}: {e})")
        rec["result"] = FAIL
        return rec
    if info["integrity_check"] != ["ok"]:
        rec["problems"].append(f"integrity_check {info['integrity_check']}")
    from backend import db
    try:
        if not history:
            raise db.DatabaseSchemaUnsupported("the backup has no schema history")
        db._check_history(history)
    except db.DatabaseSchemaUnsupported as e:
        rec["problems"].append(f"schema history not accepted by this build: {e}")
    src_verdict = None
    if source_db is not None:
        source_db = Path(source_db)
        src_verdict = classify(source_db)
        src = {"path_category": src_verdict.category, "storage": src_verdict.as_record(),
               "database_name": source_db.name}
        rec["source"] = src
        if not source_db.is_file():
            rec["problems"].append("the source database does not exist")
        else:
            try:
                conn = _connect_source(source_db)
                try:
                    src["schema_newest"] = _schema_newest(conn)
                    src["logical_digest"] = logical_digest(conn)
                finally:
                    conn.close()
            except sqlite3.DatabaseError as e:
                rec["problems"].append(f"the source database cannot be read ({type(e).__name__})")
            src["matches_backup"] = src.get("logical_digest") == info.get("logical_digest")
            if not src["matches_backup"]:
                rec["problems"].append("the backup's logical digest differs from the source database's: it is not "
                                       "a copy of the database as it is now")
    rec["failure_domain"] = failure_domain(src_verdict, dest_verdict)
    if rec["problems"]:
        rec["result"] = FAIL
    elif not rec["failure_domain"]["separate_physical_storage"]:
        rec["result"] = FAIL_SAME_DOMAIN if rec["failure_domain"]["same_physical_disk"] or \
            (dest_verdict.loopback and dest_verdict.category in sc.NETWORK_CATEGORIES) else FAIL
        rec["problems"].append(f"not in a separate failure domain: {rec['failure_domain']['reason']}")
    else:
        rec["result"] = PASS
    return rec


def write_record(record: dict, out: Path) -> Path:
    """Written exclusively (never over an existing file) and read back to prove it is readable."""
    out = Path(out)
    text = json.dumps(record, indent=2, default=str)
    with open(out, "x", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    if json.loads(out.read_text(encoding="utf-8")).get("result") != record.get("result"):
        raise OSError(f"the verification record {out} did not read back")
    return out


def load_record(path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("record_type") != RECORD_TYPE or data.get("format") != RECORD_FORMAT:
        raise ValueError("not a Kite Terminal backup verification record of this format")
    return data


def check_offsite_record(record_path, target_db, *, now: float | None = None, classify=None,
                         max_age: int = MAX_RECORD_AGE_SECONDS) -> dict:
    """migrate-v12's off-machine backup gate. Re-verifies the recorded backup LIVE against the target database and
    returns {"ok": bool, "problems": [...], "record": ..., "live": ..., "target_logical_digest": ...}. Any ambiguity
    (unreadable record, stale, future-dated, hash or digest mismatch, same failure domain) is a problem."""
    now = time.time() if now is None else now
    out = {"ok": False, "problems": [], "record_file": str(record_path)}
    try:
        record = load_record(record_path)
    except (OSError, ValueError) as e:
        out["problems"].append(f"the off-machine backup record cannot be used ({type(e).__name__}: {e})")
        return out
    out["record"] = {k: record.get(k) for k in ("verified_at", "result")}
    if record.get("result") != PASS:
        out["problems"].append(f"the record's result is {record.get('result')}, not PASS")
    epoch = record.get("verified_at_epoch")
    if not isinstance(epoch, (int, float)):
        out["problems"].append("the record has no verification time")
    elif epoch > now + 300:
        out["problems"].append("the record is dated in the future")
    elif now - epoch > max_age:
        out["problems"].append(f"the record is {int((now - epoch) // 60)} minutes old; at most {max_age // 3600} h is "
                               "accepted (verify the off-machine backup again, today)")
    backup = (record.get("backup") or {}).get("path")
    if not backup:
        out["problems"].append("the record names no backup file")
        return out
    live = verify_backup_file(backup, source_db=target_db, command="migrate-v12 offsite re-verification",
                              classify=classify)
    out["live"] = {"result": live["result"], "problems": live["problems"],
                   "sha256": live["backup"].get("sha256"), "failure_domain": live.get("failure_domain")}
    out["target_logical_digest"] = (live.get("source") or {}).get("logical_digest")
    if live["result"] != PASS:
        out["problems"].append(f"the live re-verification of {backup} gives {live['result']}: {live['problems']}")
    if live["backup"].get("sha256") != (record.get("backup") or {}).get("sha256"):
        out["problems"].append("the backup file's SHA-256 differs from the record's (changed or replaced since)")
    if live["backup"].get("logical_digest") != (record.get("backup") or {}).get("logical_digest"):
        out["problems"].append("the backup's logical digest differs from the record's")
    out["ok"] = not out["problems"]
    return out
