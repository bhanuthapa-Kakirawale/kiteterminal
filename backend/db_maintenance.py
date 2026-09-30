"""Database maintenance commands (PK-22G). Nothing here runs automatically.

    python -m backend.db_maintenance status      schema version, pending migrations, size
    python -m backend.db_maintenance integrity   PRAGMA integrity_check + foreign_key_check
    python -m backend.db_maintenance backup      online backup to kite_terminal.db.manual_<time>_bak
    python -m backend.db_maintenance analyze     refresh query-planner statistics (ANALYZE)
    python -m backend.db_maintenance vacuum      compact the file — STOP THE APP FIRST
    python -m backend.db_maintenance migrate     apply pending migrations — STOP THE APP FIRST

Only `migrate` changes the schema, and it runs exactly what an app start
runs (backend/db.init_db, with its own verified pre-migration backup), minus
the server and workers (LIVE-0). `--db PATH` points any command at another
file (for example a copy).
"""

import argparse
import sys
import time
from pathlib import Path

from backend import db


def _connect(path: Path):
    import sqlite3
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def migrate(path: Path) -> int:
    """LIVE-0: bring a database current deliberately, without starting the
    server, the workers or the ticker — exactly the application's own
    initialisation (backend/db.init_db: the verified pre-migration backup,
    the ordered migrations, the startup invariants). STOP THE APP FIRST.

    The encryption key must come from .env: init_db() would otherwise mint a
    new key, and the stored (encrypted) Kite token could never be read again.
    """
    import os

    from dotenv import load_dotenv

    from backend import crypto
    load_dotenv(crypto.ENV_PATH)
    if not os.environ.get(crypto.ENV_VAR):
        print(f"{crypto.ENV_VAR} is not set (no .env at {crypto.ENV_PATH}): refusing to migrate — a new key "
              f"would make the stored Kite token unreadable.", file=sys.stderr)
        return 2
    db.DB_PATH = path
    conn = _connect(path)
    try:
        before = db.schema_status(conn)
    finally:
        conn.close()
    if not before["supported"]:
        print(f"refused: {before['error']}", file=sys.stderr)
        return 1
    db.init_db()
    print(f"database:            {path}")
    print(f"schema:              v{before['current_version']} -> v{db.last_init['schema_version']}")
    print(f"applied migrations:  {db.last_init['applied_migrations'] or 'none'}")
    print(f"startup row changes: {db.last_init['startup_row_changes']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.db_maintenance")
    parser.add_argument("command", choices=["status", "integrity", "backup", "analyze", "vacuum", "migrate"])
    parser.add_argument("--db", type=Path, default=db.DB_PATH, help="database file (default: the app's)")
    args = parser.parse_args(argv)
    path: Path = args.db
    if not path.exists():
        print(f"No database at {path}", file=sys.stderr)
        return 2
    if args.command == "migrate":
        return migrate(path)
    conn = _connect(path)
    try:
        if args.command == "status":
            status = db.schema_status(conn)
            print(f"database:  {path} ({path.stat().st_size / 1_048_576:.1f} MB)")
            print(f"schema:    v{status['current_version']} of v{status['latest_version']}"
                  f"{'' if status['supported'] else '  UNSUPPORTED: ' + str(status['error'])}")
            pending = ", ".join("{} {}".format(p["version"], p["name"]) for p in status["pending"])
            print(f"pending:   {pending or 'none'}")
            for h in status["history"]:
                print(f"  applied v{h['version']} {h['migration_name']} at {h['applied_at']} ({h['duration_ms']} ms)")
            return 0 if status["supported"] else 1
        if args.command == "integrity":
            result = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            print(f"integrity_check: {', '.join(result[:10])}")
            print(f"foreign_key_check: {len(fk)} violation(s)")
            return 0 if result == ["ok"] and not fk else 1
        if args.command == "backup":
            target = path.with_name(f"{path.name}.manual_{time.strftime('%Y%m%d%H%M%S')}_bak")
            written = db.backup_database(conn, target)
            print(f"backup written and verified: {written}")
            return 0
        if args.command == "analyze":
            conn.execute("ANALYZE")
            conn.commit()
            print("ANALYZE done.")
            return 0
        if args.command == "vacuum":
            print("VACUUM rewrites the whole file; the app must be stopped. Running...")
            conn.execute("VACUUM")
            print(f"VACUUM done: {path.stat().st_size / 1_048_576:.1f} MB")
            return 0
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
