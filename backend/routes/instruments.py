from fastapi import APIRouter, Depends
from kiteconnect import KiteConnect

from backend import instruments
from backend.kite_client import require_kite
from backend.instance_lock import WRITE_AUTHORITY

router = APIRouter()


@router.get("/api/instruments/search")
def search(q: str = "", kite: KiteConnect = Depends(require_kite)):
    return {"data": instruments.search_instruments(q)}


@router.post("/api/instruments/refresh", dependencies=WRITE_AUTHORITY)
def refresh(kite: KiteConnect = Depends(require_kite)):
    count = instruments.refresh_instrument_cache(kite)
    return {"data": {"count": count}}
