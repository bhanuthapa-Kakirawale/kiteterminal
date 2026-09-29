"""The application's version and source commit, for diagnostics (PK-22H).

The commit is read from the .git files directly — no subprocess, no git
binary needed — and is simply "unknown" outside a checkout.
"""

from functools import lru_cache
from pathlib import Path

# The packet this code implements; bumped with each packet.
APP_VERSION = "pk22"

_REPO = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def commit() -> dict:
    git = _REPO / ".git"
    try:
        if git.is_file():  # a worktree: ".git" names the real git dir
            text = git.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                git = (_REPO / text.split(":", 1)[1].strip()).resolve()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return {"sha": head[:40], "ref": None}
        ref = head.split(":", 1)[1].strip()
        ref_file = git / ref
        if ref_file.is_file():
            return {"sha": ref_file.read_text(encoding="utf-8").strip()[:40], "ref": ref}
        common = git
        commondir = git / "commondir"
        if commondir.is_file():
            common = (git / commondir.read_text(encoding="utf-8").strip()).resolve()
            if (common / ref).is_file():
                return {"sha": (common / ref).read_text(encoding="utf-8").strip()[:40], "ref": ref}
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return {"sha": parts[0][:40], "ref": ref}
        return {"sha": None, "ref": ref}
    except OSError:
        return {"sha": None, "ref": None}


def commit_short() -> str:
    sha = commit()["sha"]
    return sha[:8] if sha else "unknown"
