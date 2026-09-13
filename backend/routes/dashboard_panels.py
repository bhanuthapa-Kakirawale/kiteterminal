from fastapi import APIRouter, Depends
from kiteconnect import KiteConnect
from pydantic import BaseModel, Field
from typing import Literal

from backend.accounts import get_default_account
from backend.db import get_connection
from backend.kite_client import require_kite

router = APIRouter()


class PanelConfig(BaseModel):
    panel_type: str
    display_order: int
    size: Literal["small", "medium", "large"]
    visible: bool = True


class PanelsUpdateRequest(BaseModel):
    panels: list[PanelConfig] = Field(default_factory=list)


def _serialize(row) -> dict:
    d = dict(row)
    d["visible"] = bool(d["visible"])
    return d


@router.get("/api/dashboard-panels")
def get_dashboard_panels(kite: KiteConnect = Depends(require_kite)):
    account = get_default_account()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM dashboard_panels WHERE account_id = ? ORDER BY display_order",
            (account["id"],),
        ).fetchall()
        return {"data": [_serialize(r) for r in rows]}
    finally:
        conn.close()


@router.put("/api/dashboard-panels")
def save_dashboard_panels(req: PanelsUpdateRequest, kite: KiteConnect = Depends(require_kite)):
    # Full-replace, not a per-field patch: the frontend always holds the
    # complete current layout in memory, so every interaction (drag reorder,
    # resize, hide, show, re-add) is naturally "here is the new full list" —
    # one endpoint covers every mutation instead of four fine-grained ones.
    account = get_default_account()
    conn = get_connection()
    try:
        conn.execute("DELETE FROM dashboard_panels WHERE account_id = ?", (account["id"],))
        conn.executemany(
            """
            INSERT INTO dashboard_panels (account_id, panel_type, display_order, size, visible, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            """,
            [
                (account["id"], p.panel_type, p.display_order, p.size, int(p.visible))
                for p in req.panels
            ],
        )
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM dashboard_panels WHERE account_id = ? ORDER BY display_order",
            (account["id"],),
        ).fetchall()
        return {"data": [_serialize(r) for r in rows]}
    finally:
        conn.close()
