import json
from pathlib import Path
from threading import RLock

# Deliberately a plain local JSON file, not the SQLite DB and not browser
# storage — this is pure UI chrome (sidebar collapsed/expanded), not trading
# data, and doesn't need a migration story or per-account scoping.
PREFS_PATH = Path(__file__).parent / "ui_prefs.json"

DEFAULTS = {"sidebar_collapsed": False, "watchlist_group_by_tag": False}

# Reentrant: save_prefs() calls get_prefs() while already holding the lock,
# which would deadlock forever on a plain (non-reentrant) Lock.
_lock = RLock()


def get_prefs() -> dict:
    with _lock:
        if not PREFS_PATH.exists():
            return dict(DEFAULTS)
        try:
            return {**DEFAULTS, **json.loads(PREFS_PATH.read_text())}
        except (json.JSONDecodeError, OSError):
            return dict(DEFAULTS)


def save_prefs(**updates) -> dict:
    with _lock:
        current = get_prefs()
        current.update(updates)
        PREFS_PATH.write_text(json.dumps(current, indent=2))
        return current
