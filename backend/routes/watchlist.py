import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from kiteconnect import KiteConnect
from pydantic import BaseModel, Field

from backend import data_jobs, instruments, watchlist
from backend.accounts import get_trading_account
from backend.kite_client import require_trading_account
from backend.ticker import collect_instrument_tokens, ticker_manager

router = APIRouter()


class WatchlistAddRequest(BaseModel):
    symbol: str
    exchange: str = "NSE"
    notes: str | None = Field(default=None)


def _resubscribe_ticker(kite: KiteConnect, account_id: int) -> None:
    tokens = set(collect_instrument_tokens(kite)) | set(watchlist.watchlist_instrument_tokens(account_id))
    ticker_manager.update_instruments(list(tokens))


@router.get("/api/watchlist")
def get_watchlist(kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    return {"data": watchlist.list_watchlist(account["id"])}


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

    try:
        row = watchlist.add_to_watchlist(
            account["id"], instrument["tradingsymbol"], instrument["instrument_token"], req.notes
        )
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, {
            "error_code": "ALREADY_WATCHED",
            "message": f"'{instrument['tradingsymbol']}' is already on your watchlist.",
        }) from e

    _resubscribe_ticker(kite, account["id"])
    await data_jobs.enqueue_backfill(instrument["instrument_token"], instrument["tradingsymbol"])

    return {"data": row}


@router.delete("/api/watchlist/{watchlist_id}")
def remove_watchlist_symbol(watchlist_id: int, kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    watchlist.remove_from_watchlist(account["id"], watchlist_id)
    _resubscribe_ticker(kite, account["id"])
    return {"data": {"deleted": True}}
