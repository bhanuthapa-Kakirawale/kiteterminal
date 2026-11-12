"""Off-machine backup verification (V12 final readiness, Phase D): what makes a backup valid, the verification record,
and the live re-verification migrate-v12 relies on. Temporary databases only; only the storage CLASS of a simulated
external disk is faked (tests/offsite_helpers.py), every SQLite step is real.
"""

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

import offsite_helpers
from backend import backup_verification as bv
from backend import storage_classification as sc

SENTINEL = "ZZ_SENTINEL_ACCOUNT_VALUE_7f3a"


def _main(*argv):
    from backend import db_maintenance
    return db_maintenance.main(list(argv))


def _db(env) -> Path:
    return Path(env.db.DB_PATH)


def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _offsite(env):
    return offsite_helpers.offsite_disk(env.tmp_path / "offsite_disk")


def _backup_to(env, root) -> tuple[Path, dict]:
    record_path = offsite_helpers.make_record(_db(env), root)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    return record_path, record


def _seed_sentinel(env):
    conn = env.db.get_connection()
    try:
        conn.execute("INSERT INTO instrument_cache (instrument_token, tradingsymbol, name, exchange, segment) "
                     "VALUES (999001, ?, ?, 'NSE', 'NSE')", (SENTINEL, SENTINEL))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- the record

def test_b01_backup_dest_writes_a_verified_backup_and_a_pass_record_with_every_required_field(env, capsys):
    with _offsite(env) as root:
        record_path, record = _backup_to(env, root)
    assert record["result"] == bv.PASS and record["record_type"] == bv.RECORD_TYPE
    for key in ("verified_at", "verified_at_epoch", "tool", "backup", "source", "failure_domain", "problems",
                "operator_attestation_required"):
        assert key in record, key
    b = record["backup"]
    assert b["integrity_check"] == ["ok"] and b["schema_newest"] == [12, "phase3_protection_journal"]
    assert b["sha256"] == _sha(b["path"]) and b["size"] == Path(b["path"]).stat().st_size
    assert record["source"]["matches_backup"] is True and record["source"]["path_category"] == sc.FIXED_LOCAL
    assert record["failure_domain"]["separate_physical_storage"] is True
    assert set(record["tool"]) >= {"python", "sqlite", "git_head", "command"}
    assert record_path.parent == root


def test_b02_the_record_and_the_output_never_contain_database_contents(env, capsys):
    _seed_sentinel(env)
    with _offsite(env) as root:
        record_path, _record = _backup_to(env, root)
        assert _main("verify-backup", "--backup", json.loads(record_path.read_text())["backup"]["path"],
                     "--db", str(_db(env))) == 0
    out = capsys.readouterr()
    for text in (record_path.read_text(encoding="utf-8"), out.out, out.err,
                 *[p.read_text(encoding="utf-8") for p in env.tmp_path.glob("test.db.backup_verification_*.json")]):
        assert SENTINEL not in text and "test_api_key" not in text


def test_b03_rows_committed_only_in_the_wal_are_part_of_the_source_digest_and_of_the_backup(env):
    path = _db(env)
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    try:
        writer.execute("INSERT INTO instrument_cache (instrument_token, tradingsymbol, name, exchange, segment) "
                       "VALUES (999002, 'WALONLY', 'WALONLY', 'NSE', 'NSE')")
        writer.commit()
        with _offsite(env) as root:
            _path, record = _backup_to(env, root)
        immutable = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
        try:
            main_file_only = bv.logical_digest(immutable)
        finally:
            immutable.close()
    finally:
        writer.close()
    assert record["source"]["matches_backup"] and record["result"] == bv.PASS
    assert record["source"]["logical_digest"] != main_file_only    # the WAL row is in the digest


def test_b04_a_backup_on_the_same_physical_disk_as_the_database_is_never_off_machine(env, capsys):
    """The real classifier: env's database and this backup are on the same disk (this machine has one)."""
    local = env.tmp_path / "second_folder"
    local.mkdir()
    assert _main("backup", "--dest", str(local), "--db", str(_db(env))) == 1
    err = capsys.readouterr().err
    assert "NOT valid as an off-machine copy" in err
    record = json.loads(next(local.glob("*.verification.json")).read_text(encoding="utf-8"))
    if record["backup"]["storage"]["category"] == sc.FIXED_LOCAL:
        assert record["result"] == bv.FAIL_SAME_DOMAIN and record["failure_domain"]["same_physical_disk"] is True
    else:
        assert record["result"] != bv.PASS


def test_b05_verify_backup_is_read_only_on_the_backup_and_on_the_database(env, capsys):
    with _offsite(env) as root:
        record_path, record = _backup_to(env, root)
        backup = Path(record["backup"]["path"])
        conn = sqlite3.connect(_db(env))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        db_before, backup_before = _sha(_db(env)), _sha(backup)
        assert _main("verify-backup", "--backup", str(backup), "--db", str(_db(env))) == 0
    assert _sha(_db(env)) == db_before and _sha(backup) == backup_before
    assert not list(backup.parent.glob(f"{backup.name}-*"))         # no sidecar created beside the backup


# ---------------------------------------------------------------- what is not a valid backup

def test_b06_a_tampered_backup_fails_the_live_re_verification(env):
    with _offsite(env) as root:
        record_path, record = _backup_to(env, root)
        backup = Path(record["backup"]["path"])
        data = bytearray(backup.read_bytes())
        data[len(data) // 2] ^= 0xFF
        backup.write_bytes(bytes(data))
        check = bv.check_offsite_record(record_path, _db(env))
    assert not check["ok"] and any("SHA-256" in p for p in check["problems"])


def test_b07_a_truncated_backup_is_not_a_backup(env):
    with _offsite(env) as root:
        _record_path, record = _backup_to(env, root)
        backup = Path(record["backup"]["path"])
        backup.write_bytes(backup.read_bytes()[: backup.stat().st_size // 3])
        live = bv.verify_backup_file(backup, source_db=_db(env))
    assert live["result"] == bv.FAIL and live["problems"]


def test_b08_a_backup_with_a_non_empty_wal_of_its_own_is_not_self_contained(env):
    with _offsite(env) as root:
        _record_path, record = _backup_to(env, root)
        backup = Path(record["backup"]["path"])
        Path(f"{backup}-wal").write_bytes(b"x" * 100)
        live = bv.verify_backup_file(backup, source_db=_db(env))
    assert live["result"] == bv.FAIL and any("sidecar" in p for p in live["problems"])


def test_b09_a_backup_of_older_content_does_not_match_the_database_any_more(env):
    with _offsite(env) as root:
        record_path, _record = _backup_to(env, root)
        _seed_sentinel(env)                                          # the database changes after the backup
        check = bv.check_offsite_record(record_path, _db(env))
    assert not check["ok"] and any("not a copy of the database as it is now" in p for p in check["problems"])


def test_b10_a_missing_backup_file_fails(env):
    with _offsite(env) as root:
        record_path, record = _backup_to(env, root)
        Path(record["backup"]["path"]).unlink()
        check = bv.check_offsite_record(record_path, _db(env))
    assert not check["ok"]


# ---------------------------------------------------------------- the record itself: fresh, PASS, this format

def _rewrite(record_path: Path, **changes) -> Path:
    data = json.loads(record_path.read_text(encoding="utf-8"))
    data.update(changes)
    out = record_path.with_name(record_path.stem + f"_edited_{len(changes)}_{time.monotonic_ns()}.json")
    out.write_text(json.dumps(data), encoding="utf-8")
    return out


@pytest.mark.parametrize("changes, expected", [
    ({"verified_at_epoch": time.time() - 13 * 3600}, "minutes old"),
    ({"verified_at_epoch": time.time() + 3600}, "future"),
    ({"verified_at_epoch": None}, "no verification time"),
    ({"result": bv.FAIL}, "not PASS"),
    ({"record_type": "something_else"}, "cannot be used"),
    ({"format": 99}, "cannot be used"),
])
def test_b11_a_stale_future_failed_or_foreign_record_is_refused(env, changes, expected):
    with _offsite(env) as root:
        record_path, _record = _backup_to(env, root)
        check = bv.check_offsite_record(_rewrite(record_path, **changes), _db(env))
    assert not check["ok"] and any(expected in p for p in check["problems"]), check["problems"]


def test_b12_an_unreadable_record_is_refused(env, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json")
    check = bv.check_offsite_record(bad, _db(env))
    assert not check["ok"] and not check.get("live")


def test_b13_a_record_naming_another_backup_with_a_different_hash_is_refused(env):
    with _offsite(env) as root:
        record_path, record = _backup_to(env, root)
        second_path, second = _backup_to(env, root)
        forged = _rewrite(record_path, backup=dict(record["backup"], path=second["backup"]["path"]))
        # Same content, different file: its SHA-256 matches the second backup, not the forged record's.
        if second["backup"]["sha256"] != record["backup"]["sha256"]:
            check = bv.check_offsite_record(forged, _db(env))
            assert not check["ok"] and any("SHA-256" in p for p in check["problems"])


def test_b14_the_record_is_written_exclusively_and_read_back(env, tmp_path):
    out = tmp_path / "exists.json"
    out.write_text("{}")
    with pytest.raises(FileExistsError):
        bv.write_record({"result": bv.PASS}, out)


# ---------------------------------------------------------------- failure domains

def _verdict(category, disks=(), loopback=False):
    return sc.StorageVerdict(category, "test", "x", physical_disks=tuple(disks), loopback=loopback)


@pytest.mark.parametrize("dest, separate", [
    (_verdict(sc.FIXED_LOCAL, (0,)), False),                    # another partition of the same disk (this E:)
    (_verdict(sc.FIXED_LOCAL, (1,)), True),                     # a second internal disk
    (_verdict(sc.FIXED_LOCAL, ()), False),                      # cannot compare: not established
    (_verdict(sc.REMOVABLE, (2,)), True),
    (_verdict(sc.REMOVABLE, (0,)), False),
    (_verdict(sc.NETWORK_UNC, (), loopback=False), True),
    (_verdict(sc.NETWORK_UNC, (), loopback=True), False),       # \\localhost\... is this machine
    (sc.StorageVerdict(sc.NETWORK_MAPPED, "t", "x", details={"remote": r"\\nas\share"}), True),
    (sc.StorageVerdict(sc.NETWORK_MAPPED, "t", "x", details={"remote": None}), False),   # host unknown
    (sc.StorageVerdict(sc.NETWORK_MAPPED, "t", "x", loopback=True, details={"remote": r"\\localhost\s"}), False),
    (_verdict(sc.CLOUD_SYNCED, ()), False),                     # sync completion cannot be verified
    (_verdict(sc.UNKNOWN, ()), False),
    (_verdict(sc.UNAVAILABLE, ()), False),
])
def test_b15_failure_domain_rules(dest, separate):
    source = _verdict(sc.FIXED_LOCAL, (0,))
    assert bv.failure_domain(source, dest)["separate_physical_storage"] is separate


def test_b16_a_backup_on_a_share_back_to_this_machine_is_same_failure_domain(env):
    with offsite_helpers.offsite_disk(env.tmp_path / "offsite_disk") as root:
        record_path, record = _backup_to(env, root)
    backup = Path(record["backup"]["path"])
    loop = lambda path, adapter=None: (sc.StorageVerdict(sc.NETWORK_UNC, "loopback", str(path), loopback=True)  # noqa: E731
                                       if str(path).startswith(str(root)) else offsite_helpers._REAL_CLASSIFY(path))
    live = bv.verify_backup_file(backup, source_db=_db(env), classify=loop)
    assert live["result"] == bv.FAIL_SAME_DOMAIN


def test_b17_logical_digest_changes_with_any_value_and_ignores_planner_statistics(env):
    path = _db(env)
    conn = sqlite3.connect(path)
    try:
        before = bv.logical_digest(conn)
        conn.execute("ANALYZE")
        conn.commit()
        assert bv.logical_digest(conn) == before                    # sqlite_stat* excluded
        conn.execute("UPDATE instrument_cache SET name = name || 'x' WHERE rowid = (SELECT MIN(rowid) FROM "
                     "instrument_cache)")
        conn.commit()
        assert bv.logical_digest(conn) != before
    finally:
        conn.close()


def test_b18_verify_backup_needs_backup_before_any_filesystem_access(env, capsys):
    assert _main("verify-backup", "--db", str(_db(env))) == 2


def test_b19_storage_check_reports_the_database_and_the_destination(env, capsys):
    with _offsite(env) as root:
        assert _main("storage-check", "--db", str(_db(env)), "--backup", str(root)) == 0
    local = env.tmp_path / "same_disk"
    local.mkdir()
    assert _main("storage-check", "--db", str(_db(env)), "--backup", str(local)) == 1
    assert "NOT OK" in capsys.readouterr().out
