from fastapi import APIRouter, Depends
from kiteconnect import KiteConnect

from backend import instruments
from backend.kite_client import require_kite

router = APIRouter()


@router.get("/api/instruments/search")
def search(q: str = "", kite: KiteConnect = Depends(require_kite)):
    return {"data": instruments.search_instruments(q)}


@router.post("/api/instruments/refresh")
def refresh(kite: KiteConnect = Depends(require_kite)):
    count = instruments.refresh_instrument_cache(kite)
    return {"data": {"count": count}}
