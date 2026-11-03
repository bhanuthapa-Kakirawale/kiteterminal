"""Synthetic Daily Market Byte handoff bundles for the DMB Research Inbox tests.

Built from the wire contract with this repository's own id functions
(backend/dmb_inbox_contract.py) - no DMB code is imported. Every value is invented; the
symbols are the ones conftest seeds into the instrument cache. tests/test_dmb_handoff_roundtrip.py
uses DMB's real exporter instead.
"""
import copy
import json
import os
from datetime import datetime, timedelta, timezone

from backend import dmb_inbox_contract as contract

IST = timezone(timedelta(hours=5, minutes=30))
SESSION = "2026-11-02"            # a Monday, regular NSE session
REVIEW = "2026-11-03"             # the next regular session
IMPORT_AT = datetime(2026, 11, 2, 21, 0, tzinfo=IST)          # the evening after the close
DURING_REVIEW = datetime(2026, 11, 3, 11, 0, tzinfo=IST)
AFTER_REVIEW = datetime(2026, 11, 3, 15, 30, tzinfo=IST)      # the review session has completed


def packet(symbol="INFY", *, session=SESSION, review=REVIEW, evidence_as_of="2026-11-02T19:45:00+05:30",
           generated_at="2026-11-02T20:00:00+05:30", replay="MATCHED", direction="ALIGNED_POSITIVE",
           freshness_ok=True, radar_coverage_ok=True, close=1534.5, origin="SYNTHETIC") -> dict:
    sources = ([{"source_name": contract.SYNTHETIC_SOURCE, "handoff_status": "PERMITTED",
                 "basis": "synthetic data generated for integration tests"}] if origin == "SYNTHETIC" else
               [{"source_name": "approved_test_source", "handoff_status": "PERMITTED", "basis": "test"}])
    p = {
        "schema_version": contract.PACKET_SCHEMA, "packet_id": "", "session_date": session,
        "listing_exchange": "NSE", "symbol": symbol, "company": f"Synthetic {symbol}",
        "sector": "Information Technology",
        "radar_state": {"attention_level": "HIGH_INTEREST", "novelty_type": "NEW", "appearance": "FIRST_RECORDED",
                        "families": ["VOLUME", "STRUCTURE"], "direction": direction,
                        "persistence": "NEW", "selected_for_story": False},
        "detector_ids": ["VOLUME_UNUSUAL", "STRUCTURE_BREAK_ABOVE_20D_RANGE"],
        "reasons": ["SYNTHETIC: volume was 3.2x its prior-20-session average.",
                    "SYNTHETIC: the close was above the prior 20-session range."],
        "metrics": {"price_change_pct": 2.4, "relative_volume": 3.2, "vs_nifty_5d_pp": 1.8,
                    "vs_nifty_20d_pp": 3.5, "close": close, "replay_status": replay},
        "market_context": {"advances": 120, "declines": 70, "breadth_denominator": 200, "nifty_change_pct": 0.42},
        "sector_context": {"sector": "Information Technology", "constituents": 15, "advances": 11, "declines": 4},
        "official_events": [{"kind": "ASM", "status": "LISTED", "source_date": session}],
        "source_artifact_ids": ["synthetic_handoff_fixture:test"],
        "evidence_as_of": evidence_as_of, "review_session_date": review,
        "quality": {"freshness_ok": freshness_ok, "warnings": [] if replay == "MATCHED" else [f"REPLAY_{replay}"],
                    "replay_status": replay, "radar_coverage_ok": radar_coverage_ok},
        "producer": dict(contract.PRODUCER),
        "rights": {"research_classification": "PRIVATE_RESEARCH_ONLY", "classification_is_permission": False,
                   "public_publication": "BLOCK", "data_origin": origin, "handoff_status": "PERMITTED",
                   "handoff_basis": "every source explicitly permits private handoff", "sources": sources},
        "generated_at": generated_at,
        "notes": ["Radar candidate = attention item, not a trade recommendation."],
        "market_regime": {"label": "NEUTRAL", "session_date": session, "calculation_version": "1.0",
                          "dimensions": {"TREND": "POSITIVE", "FLOWS": "UNAVAILABLE"}, "reason_code": "SYNTHETIC"},
    }
    return rehash(p)


def date_of(text: str):
    from datetime import date
    return date.fromisoformat(text)


def rehash(p: dict) -> dict:
    p["packet_id"] = contract.packet_id_for(p)
    return p


def bundle(packets, *, session=SESSION, generated_at="2026-11-02T20:00:00+05:30") -> dict:
    packets = sorted(copy.deepcopy(packets), key=lambda p: (p["symbol"], p["packet_id"]))
    return {"schema_version": contract.BUNDLE_SCHEMA,
            "bundle_id": contract.bundle_id_for(session, [p["packet_id"] for p in packets]),
            "session_date": session, "generated_at": generated_at, "packet_count": len(packets),
            "packets": packets}


def write(root, b: dict, raw: bytes | None = None, name: str | None = None) -> str:
    folder = os.path.join(str(root), b.get("session_date") or SESSION)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name or f"{b['bundle_id']}.json")
    with open(path, "wb") as fh:
        fh.write(raw if raw is not None else json.dumps(b, indent=2).encode("utf-8"))
    return path
