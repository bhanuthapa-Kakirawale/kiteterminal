import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from kiteconnect import KiteConnect
from pydantic import BaseModel, Field

from backend import data_jobs, instruments, sector_mapping, watchlist
from backend.accounts import get_trading_account
from backend.kite_client import require_trading_account
from backend.ticker import collect_instrument_tokens, ticker_manager

router = APIRouter()


class WatchlistAddRequest(BaseModel):
    symbol: str
    exchange: str = "NSE"
    notes: str | None = Field(default=None)
    tag: str | None = Field(default=None, max_length=50)


class WatchlistTagRequest(BaseModel):
    tag: str | None = Field(default=None, max_length=50)


def _resubscribe_ticker(kite: KiteConnect, account_id: int) -> None:
    tokens = set(collect_instrument_tokens(kite)) | set(watchlist.watchlist_instrument_tokens(account_id))
    ticker_manager.update_instruments(list(tokens))


@router.get("/api/watchlist")
def get_watchlist(kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    rows = watchlist.list_watchlist(account["id"])
    return {"data": watchlist.seed_live_quotes(kite, rows)}


@router.post("/api/watchlist", status_code=201)
async def add_watchlist_symbol(req: WatchlistAddRequest, kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()

    instrument = instruments.get_instrument_by_symbol(req.symbol, req.exchange)
    if instrument is None:
        raise HTTPException(400, {
            "error_code": "INSTRUMENT_NOT_FOUND",
            "message": f"Could not resolve '{req.symbol}' on {req.exchange}. "
                       f"Check spelling or refresh the instrument list.",
        })

    tag = req.tag.strip() or None if req.tag else None
    try:
        row = watchlist.add_to_watchlist(
            account["id"], instrument["tradingsymbol"], instrument["instrument_token"], req.notes, tag
        )
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, {
            "error_code": "ALREADY_WATCHED",
            "message": f"'{instrument['tradingsymbol']}' is already on your watchlist.",
        }) from e

    # PK-14: a newly watched symbol gets its static sector right away (a
    # manual mapping is never overwritten); an unknown one shows up in
    # Settings → unmapped symbols, and new entries in it are refused.
    sector_mapping.seed_symbols([instrument["tradingsymbol"]])
    _resubscribe_ticker(kite, account["id"])
    await data_jobs.enqueue_backfill(instrument["instrument_token"], instrument["tradingsymbol"])

    return {"data": row}


@router.delete("/api/watchlist/{watchlist_id}")
def remove_watchlist_symbol(watchlist_id: int, kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    watchlist.remove_from_watchlist(account["id"], watchlist_id)
    _resubscribe_ticker(kite, account["id"])
    return {"data": {"deleted": True}}


@router.get("/api/watchlist/tags")
def get_watchlist_tags(kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    return {"data": watchlist.distinct_tags(account["id"])}


@router.put("/api/watchlist/{watchlist_id}/tag")
def update_watchlist_tag(
    watchlist_id: int, req: WatchlistTagRequest, kite: KiteConnect = Depends(require_trading_account)
):
    account = get_trading_account()
    tag = req.tag.strip() or None if req.tag else None
    row = watchlist.set_tag(account["id"], watchlist_id, tag)
    if row is None:
        raise HTTPException(404, {"error_code": "NOT_FOUND", "message": "Watchlist entry not found"})
    return {"data": row}
