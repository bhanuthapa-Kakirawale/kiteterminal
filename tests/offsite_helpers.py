"""The off-machine backup record migrate-v12 requires (V12 final readiness, Phase D/E), for tests on temporary databases.

The record is made by the REAL command path (`db_maintenance backup --dest DIR`: an online backup, verified against
the database, logical digest, integrity, verification record) into a directory under the test's tmp_path. The ONLY
thing simulated is the storage class of that directory: a test machine has no second physical disk, so paths under
the "offsite disk" directory are classified as a separate removable USB disk (disk 99). Everything else (the database
itself, and every other path) is classified by the real Windows classifier.

    with offsite_disk(tmp_path / "offsite_disk"):
        record = make_record(db_path)
        db_maintenance.main(["migrate-v12", "--authorize-v12", "--offsite-record", str(record), "--db", str(db_path)])
"""

from __future__ import annotations

import io
import os
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

from backend import db as _db
from backend import storage_classification as sc

OFFSITE_DISK = 99
_REAL_CLASSIFY = sc.classify_path
# Captured at import (conftest imports this module before any test runs): a test that makes the migration's OWN
# backup fail (monkeypatching db.backup_database) still gets a real off-machine record.
_REAL_BACKUP = _db.backup_database


def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def simulated_verdict(path, root: Path, category: str = sc.REMOVABLE, disks: tuple = (OFFSITE_DISK,)):
    return sc.StorageVerdict(category, f"test: a separate {category.lower()} disk (simulated, disk {disks})",
                             str(path), str(path), volume_root=str(root), drive_type="removable"
                             if category == sc.REMOVABLE else "fixed", filesystem="NTFS", bus_type="USB",
                             physical_disks=disks)


def classifier(root: Path, category: str = sc.REMOVABLE, disks: tuple = (OFFSITE_DISK,)):
    key = _norm(root)

    def classify(path, adapter=None):
        p = _norm(path)
        if p == key or p.startswith(key + os.sep):
            return simulated_verdict(path, root, category, disks)
        return _REAL_CLASSIFY(path, adapter)
    classify.simulated_root = key
    return classify


@contextmanager
def offsite_disk(root: Path, category: str = sc.REMOVABLE, disks: tuple = (OFFSITE_DISK,)):
    """Paths under `root` are a separate removable disk for the duration (created if missing)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    previous = sc.classify_path
    sc.classify_path = classifier(root, category, disks)
    try:
        yield root
    finally:
        sc.classify_path = previous


def make_record(db_path: Path, root: Path | None = None) -> Path:
    """`backup --dest <offsite disk>` on db_path (read-only on it); returns the PASS verification record."""
    from backend import db_maintenance
    root = Path(root) if root is not None else Path(db_path).parent / "offsite_disk"
    before = set(root.glob("*.verification.json"))
    out, err = io.StringIO(), io.StringIO()
    patched, _db.backup_database = _db.backup_database, _REAL_BACKUP
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = db_maintenance.main(["backup", "--dest", str(root), "--db", str(db_path)])
    finally:
        _db.backup_database = patched
    assert code == 0, f"backup --dest failed ({code}): {out.getvalue()}{err.getvalue()}"
    new = sorted(set(root.glob("*.verification.json")) - before)
    assert len(new) == 1, new
    return new[0]


def migrate_v12_argv(db_path: Path, *extra: str, root: Path | None = None) -> list[str]:
    """Inside offsite_disk(): a fresh record for db_path's current content, and the migrate-v12 argv using it."""
    record = make_record(db_path, root)
    return ["migrate-v12", "--authorize-v12", "--offsite-record", str(record), *extra, "--db", str(db_path)]


def run_migrate_v12(db_path: Path, *extra: str) -> int:
    from backend import db_maintenance
    root = Path(db_path).parent / "offsite_disk"
    with offsite_disk(root):
        return db_maintenance.main(migrate_v12_argv(db_path, *extra, root=root))
