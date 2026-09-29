"""Historical-data integrity (PK-22D, audit P2-5 / backtester B9).

ohlc_daily stores Kite's daily candles as fetched, and only the last
REFRESH_OVERLAP_DAYS are ever re-downloaded. When a split or bonus happens,
Kite back-adjusts its history, but the older stored bars stay unadjusted: the
series then carries a false gap that corrupts SMA-200, ATR (so stop width)
and Donchian for months. This module keeps, per instrument, enough metadata
to tell whether its stored history can be trusted, and flags it when not:

  OK             nothing suspicious known.
  SUSPECT        the stored series has an open-vs-previous-close gap beyond
                 config.SUSPECT_CORPORATE_ACTION_GAP_PCT that no full
                 re-download has confirmed yet (it may be real, or a split).
  NEEDS_REFRESH  the provider's current bars for dates we already store
                 disagree with ours by a consistent factor: Kite has
                 back-adjusted the history, so ours is stale — proven.

Nothing is rewritten automatically (user decision). A flagged symbol is
excluded from backtests with its reason; a NEEDS_REFRESH symbol is also
skipped by the live entry scan. The one fix is an explicit rebuild
(ohlc.rebuild_history via the data-job queue), which replaces the symbol's
series with a fresh download in one transaction and marks it OK — any gap
that survives a fresh download is real, and is remembered as verified.

Symbols are never merged across instrument tokens. A token whose stored bars
carry a different tradingsymbol than the provider now reports records the
change in symbol_transitions; nothing else is inferred from it.
"""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone

from backend import config

logger = logging.getLogger(__name__)

OK, SUSPECT, NEEDS_REFRESH = "OK", "SUSPECT", "NEEDS_REFRESH"
EXCLUDED_FROM_BACKTEST = (SUSPECT, NEEDS_REFRESH)
PROVIDER = "kite"

# Overlap comparison: two or more already-stored dates whose provider close
# differs from ours by more than this, all by the same factor (within
# _RATIO_AGREEMENT), prove a back-adjustment. The newest stored date is left
# out: a candle stored soon after the close can still be revised by a few
# paise, and one date is never enough to call it.
_MIN_MISMATCHED_DATES = 2
_RATIO_AGREEMENT = 0.005


def _utc_now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def history_hash(rows) -> str:
    digest = hashlib.sha256()
    for r in rows:
        digest.update(f"{r['date']},{r['open']},{r['high']},{r['low']},{r['close']},{r['volume']}\n".encode())
    return digest.hexdigest()


def scan_gaps(rows) -> list[dict]:
    """The PK-21 corporate-action heuristic over a date-ordered series."""
    limit = config.SUSPECT_CORPORATE_ACTION_GAP_PCT / 100
    gaps = []
    for prev, cur in zip(rows, rows[1:]):
        if prev["close"] and prev["close"] > 0 and abs(cur["open"] / prev["close"] - 1) > limit:
            gaps.append({"date": cur["date"], "open": cur["open"], "prev_close": prev["close"],
                         "gap_pct": round((cur["open"] / prev["close"] - 1) * 100, 2)})
    return gaps


def detect_back_adjustment(stored: dict[str, float], fetched: list[dict]) -> dict | None:
    """stored: date -> close already in ohlc_daily; fetched: provider candles
    for (some of) the same dates. Returns the evidence of a back-adjustment,
    or None."""
    if not stored:
        return None
    newest = max(stored)
    ratios = []
    for candle in fetched:
        day = candle["date"].strftime("%Y-%m-%d") if hasattr(candle["date"], "strftime") else str(candle["date"])[:10]
        old = stored.get(day)
        if day == newest or not old or not candle.get("close"):
            continue
        ratio = float(candle["close"]) / float(old)
        if abs(ratio - 1) * 100 > config.HISTORY_ADJUSTMENT_TOLERANCE_PCT:
            ratios.append((day, ratio))
    if len(ratios) < _MIN_MISMATCHED_DATES:
        return None
    values = [r for _, r in ratios]
    if max(values) / min(values) - 1 > _RATIO_AGREEMENT:
        return None  # disagreements that are not one consistent factor: not a clean adjustment
    return {"ratio": round(sum(values) / len(values), 6), "dates": [d for d, _ in ratios]}


def _series(conn: sqlite3.Connection, instrument_token: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT date, open, high, low, close, volume, symbol FROM ohlc_daily WHERE instrument_token = ? ORDER BY date",
        (instrument_token,),
    ).fetchall()


def get_status(conn: sqlite3.Connection, instrument_token: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM ohlc_history_status WHERE instrument_token = ?", (instrument_token,)).fetchone()


def summarize(conn: sqlite3.Connection, instrument_token: int, symbol: str, *, full_download: bool = False,
              source: str = "refresh") -> dict:
    """Recomputes one instrument's metadata from its stored bars and flags
    any new unverified gap as SUSPECT. Never clears a flag: only a rebuild
    (mark_rebuilt) does. Caller commits."""
    rows = _series(conn, instrument_token)
    existing = get_status(conn, instrument_token)
    now = _utc_now_sql()
    verified = set(json.loads(existing["verified_gaps"])) if existing and existing["verified_gaps"] else set()
    status = existing["status"] if existing else OK
    reason = existing["status_reason"] if existing else None
    status_at = existing["status_at"] if existing else None
    unverified = [g for g in scan_gaps(rows) if g["date"] not in verified]
    if unverified and status == OK:
        status, status_at = SUSPECT, now
        reason = json.dumps({"code": "UNVERIFIED_GAP", "gaps": unverified[:5]})
        logger.warning("event=history.suspect instrument_token=%s symbol=%s gaps=%s", instrument_token, symbol,
                       [g["date"] for g in unverified[:5]])
    values = {
        "symbol": symbol,
        "first_date": rows[0]["date"] if rows else None,
        "last_date": rows[-1]["date"] if rows else None,
        "bar_count": len(rows),
        "data_hash": history_hash(rows) if rows else None,
        "status": status, "status_reason": reason, "status_at": status_at,
    }
    conn.execute(
        """
        INSERT INTO ohlc_history_status (instrument_token, symbol, provider, first_date, last_date, bar_count,
            downloaded_at, refreshed_at, data_hash, status, status_reason, status_at)
        VALUES (:token, :symbol, :provider, :first_date, :last_date, :bar_count, :downloaded_at, :refreshed_at,
                :data_hash, :status, :status_reason, :status_at)
        ON CONFLICT(instrument_token) DO UPDATE SET
            symbol = excluded.symbol, first_date = excluded.first_date, last_date = excluded.last_date,
            bar_count = excluded.bar_count, data_hash = excluded.data_hash, status = excluded.status,
            status_reason = excluded.status_reason, status_at = excluded.status_at,
            downloaded_at = COALESCE(excluded.downloaded_at, ohlc_history_status.downloaded_at),
            refreshed_at = COALESCE(excluded.refreshed_at, ohlc_history_status.refreshed_at)
        """,
        {**values, "token": instrument_token, "provider": PROVIDER,
         "downloaded_at": now if full_download else None, "refreshed_at": now if source == "refresh" else None},
    )
    return values


def mark_needs_refresh(conn: sqlite3.Connection, instrument_token: int, symbol: str, evidence: dict) -> None:
    now = _utc_now_sql()
    conn.execute(
        """
        INSERT INTO ohlc_history_status (instrument_token, symbol, provider, status, status_reason, status_at)
        VALUES (?, ?, ?, 'NEEDS_REFRESH', ?, ?)
        ON CONFLICT(instrument_token) DO UPDATE SET status = 'NEEDS_REFRESH',
            status_reason = excluded.status_reason, status_at = excluded.status_at
        """,
        (instrument_token, symbol, PROVIDER, json.dumps({"code": "PROVIDER_HISTORY_ADJUSTED", **evidence}), now),
    )
    logger.warning("event=history.needs_refresh instrument_token=%s symbol=%s ratio=%s dates=%s",
                   instrument_token, symbol, evidence.get("ratio"), evidence.get("dates"))


def mark_rebuilt(conn: sqlite3.Connection, instrument_token: int, symbol: str) -> dict:
    """After a full re-download replaced the series: OK, with every gap that
    survived it recorded as verified (real, per the provider). Caller commits."""
    rows = _series(conn, instrument_token)
    now = _utc_now_sql()
    verified = [g["date"] for g in scan_gaps(rows)]
    conn.execute(
        """
        INSERT INTO ohlc_history_status (instrument_token, symbol, provider, status, status_reason, status_at,
                                         verified_gaps, downloaded_at)
        VALUES (?, ?, ?, 'OK', ?, ?, ?, ?)
        ON CONFLICT(instrument_token) DO UPDATE SET status = 'OK', status_reason = excluded.status_reason,
            status_at = excluded.status_at, verified_gaps = excluded.verified_gaps,
            downloaded_at = excluded.downloaded_at
        """,
        (instrument_token, symbol, PROVIDER, json.dumps({"code": "REBUILT_FROM_PROVIDER"}), now,
         json.dumps(verified), now),
    )
    return summarize(conn, instrument_token, symbol, full_download=True, source="rebuild")


def record_symbol(conn: sqlite3.Connection, instrument_token: int, symbol: str, source: str) -> bool:
    """Records a tradingsymbol change for one instrument token (never a merge
    of two tokens). True if a new transition was recorded. Caller commits."""
    row = conn.execute(
        "SELECT symbol FROM ohlc_daily WHERE instrument_token = ? ORDER BY date DESC LIMIT 1", (instrument_token,)
    ).fetchone()
    if row is None or row["symbol"] == symbol:
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO symbol_transitions (instrument_token, old_symbol, new_symbol, source) VALUES (?, ?, ?, ?)",
        (instrument_token, row["symbol"], symbol, source),
    )
    if cur.rowcount:
        logger.warning("event=history.symbol_changed instrument_token=%s old_symbol=%s new_symbol=%s",
                       instrument_token, row["symbol"], symbol)
    return bool(cur.rowcount)


def flagged(conn: sqlite3.Connection, instrument_tokens, statuses=EXCLUDED_FROM_BACKTEST) -> dict[int, dict]:
    tokens = list(instrument_tokens)
    if not tokens:
        return {}
    marks = ",".join("?" * len(tokens))
    status_marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT instrument_token, symbol, status, status_reason, status_at FROM ohlc_history_status "
        f"WHERE instrument_token IN ({marks}) AND status IN ({status_marks})",
        (*tokens, *statuses),
    ).fetchall()
    return {r["instrument_token"]: dict(r) for r in rows}


def health(conn: sqlite3.Connection) -> dict:
    rows = [dict(r) for r in conn.execute(
        "SELECT instrument_token, symbol, provider, first_date, last_date, bar_count, downloaded_at, refreshed_at, "
        "data_hash, status, status_reason, status_at FROM ohlc_history_status ORDER BY status != 'NEEDS_REFRESH', "
        "status != 'SUSPECT', symbol"
    ).fetchall()]
    counts = {OK: 0, SUSPECT: 0, NEEDS_REFRESH: 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        r["status_reason"] = json.loads(r["status_reason"]) if r["status_reason"] else None
    transitions = [dict(r) for r in conn.execute(
        "SELECT instrument_token, old_symbol, new_symbol, detected_at, source FROM symbol_transitions ORDER BY id"
    ).fetchall()]
    return {"counts": counts, "instruments": rows, "symbol_transitions": transitions}


def backfill_all(conn: sqlite3.Connection) -> int:
    """Migration v4: one metadata row per instrument already stored, with the
    gap scan run once. Caller's transaction."""
    tokens = conn.execute(
        "SELECT instrument_token, MAX(date) AS last FROM ohlc_daily GROUP BY instrument_token"
    ).fetchall()
    for t in tokens:
        rows = _series(conn, t["instrument_token"])
        symbols = []
        for r in rows:
            if not symbols or symbols[-1] != r["symbol"]:
                symbols.append(r["symbol"])
        for old, new in zip(symbols, symbols[1:]):
            conn.execute(
                "INSERT OR IGNORE INTO symbol_transitions (instrument_token, old_symbol, new_symbol, source) "
                "VALUES (?, ?, ?, 'migration_scan')", (t["instrument_token"], old, new),
            )
        summarize(conn, t["instrument_token"], rows[-1]["symbol"], source="migration")
    return len(tokens)
