"""DMB Research Inbox API (backend/dmb_inbox.py).

Every write here is a user action and is protected exactly like every other write: Host,
Origin and the per-launch terminal credential are checked by the global middleware (SEC-1
P0-A, P1-F) before a route runs, and each write additionally requires the trading account
and an active Kite login (`require_inbox_session`) - importing included. The dependency
reads the session row only: no route here builds a Kite client or calls the broker.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import dmb_inbox, kite_client
from backend.accounts import get_trading_account
from backend.instance_lock import WRITE_AUTHORITY

router = APIRouter()


def _trading_account_id() -> int:
    try:
        return get_trading_account()["id"]
    except PermissionError:
        raise HTTPException(403, {"error_code": "NOT_TRADING_ACCOUNT",
                                  "message": "This account is not enabled for trading."})


def require_inbox_reader() -> int:
    return _trading_account_id()


def require_inbox_session() -> dict:
    account_id = _trading_account_id()
    row = kite_client.get_active_session(account_id)
    if not kite_client.is_session_fresh(row):
        raise HTTPException(401, {"error_code": "NO_SESSION",
                                  "message": "Log in to Zerodha to change the research inbox."})
    return {"account_id": account_id, "session_id": row["id"]}


def _run(fn, *args):
    try:
        return fn(*args)
    except dmb_inbox.InboxError as e:
        raise HTTPException(e.status, {"error_code": e.code, "message": e.message, **e.extra})


class ImportRequest(BaseModel):
    session_date: str = Field(min_length=10, max_length=10)
    bundle_id: str = Field(min_length=64, max_length=64)


class DismissRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=dmb_inbox.NOTE_MAX)


class NoteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=dmb_inbox.NOTE_MAX)


class WatchRequest(BaseModel):
    watched: bool


class LinkTicketRequest(BaseModel):
    ticket_id: int = Field(gt=0)
    start_event_id: int = Field(gt=0)
    handoff_nonce: str = Field(min_length=1, max_length=64)


@router.get("/api/dmb-inbox/bundles")
def get_bundles(account_id: int = Depends(require_inbox_reader)):
    from backend import dmb_inbox_contract as contract
    try:
        bundles = dmb_inbox.list_bundles()
    except contract.BundleRejected as e:
        return {"data": [], "configured": False, "error_code": e.code, "message": e.detail}
    imported = {(i["requested_session_date"], i["requested_bundle_id"])
                for i in dmb_inbox.list_imports(account_id, limit=1000) if i["status"] != "REJECTED"}
    for b in bundles:
        b["imported"] = (b["session_date"], b["bundle_id"]) in imported
    return {"data": bundles, "configured": True}


@router.get("/api/dmb-inbox/imports")
def get_imports(account_id: int = Depends(require_inbox_reader)):
    return {"data": dmb_inbox.list_imports(account_id)}


@router.post("/api/dmb-inbox/imports", status_code=201, dependencies=WRITE_AUTHORITY)
def post_import(req: ImportRequest, s: dict = Depends(require_inbox_session)):
    result = dmb_inbox.import_bundle(s["account_id"], s["session_id"], req.session_date, req.bundle_id)
    if result["status"] == "REJECTED":
        raise HTTPException(422, {"error_code": result["reason_code"], "message": result["reason"],
                                  "import_id": result["import_id"]})
    return {"data": result}


@router.get("/api/dmb-inbox/items")
def get_items(include_superseded: bool = False, watchlist: bool = False,
              account_id: int = Depends(require_inbox_reader)):
    return {"data": dmb_inbox.list_items(account_id, include_superseded, watchlist),
            "disclaimer": dmb_inbox.DISCLAIMER}


@router.get("/api/dmb-inbox/items/{item_id}")
def get_item(item_id: int, account_id: int = Depends(require_inbox_reader)):
    return {"data": _run(dmb_inbox.get_item, account_id, item_id), "disclaimer": dmb_inbox.DISCLAIMER}


@router.post("/api/dmb-inbox/items/{item_id}/dismiss", dependencies=WRITE_AUTHORITY)
def post_dismiss(item_id: int, req: DismissRequest, s: dict = Depends(require_inbox_session)):
    return {"data": _run(dmb_inbox.dismiss, s["account_id"], item_id, s["session_id"], req.reason)}


@router.post("/api/dmb-inbox/items/{item_id}/notes", status_code=201, dependencies=WRITE_AUTHORITY)
def post_note(item_id: int, req: NoteRequest, s: dict = Depends(require_inbox_session)):
    return {"data": _run(dmb_inbox.add_note, s["account_id"], item_id, s["session_id"], req.text)}


@router.post("/api/dmb-inbox/items/{item_id}/research-watchlist", dependencies=WRITE_AUTHORITY)
def post_watch(item_id: int, req: WatchRequest, s: dict = Depends(require_inbox_session)):
    return {"data": _run(dmb_inbox.set_research_watch, s["account_id"], item_id, s["session_id"], req.watched)}


@router.post("/api/dmb-inbox/items/{item_id}/start-ticket", dependencies=WRITE_AUTHORITY)
def post_start_ticket(item_id: int, s: dict = Depends(require_inbox_session)):
    return {"data": _run(dmb_inbox.start_ticket, s["account_id"], item_id, s["session_id"])}


@router.post("/api/dmb-inbox/items/{item_id}/ticket-link", status_code=201, dependencies=WRITE_AUTHORITY)
def post_ticket_link(item_id: int, req: LinkTicketRequest, s: dict = Depends(require_inbox_session)):
    return {"data": _run(dmb_inbox.link_ticket, s["account_id"], item_id, s["session_id"], req.ticket_id,
                         req.start_event_id, req.handoff_nonce)}
