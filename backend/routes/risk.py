from fastapi import APIRouter, Depends, HTTPException, Query
from kiteconnect import KiteConnect

from backend import risk_engine, sector_mapping
from backend.accounts import get_trading_account
from backend.db import get_connection
from backend.kite_client import require_trading_account
from backend.models import RiskSettingsUpdateRequest, SectorMappingUpsertRequest

router = APIRouter()


@router.get("/api/risk/settings")
def get_settings(kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        return {"data": dict(risk_engine.get_settings(conn, account["id"]))}
    finally:
        conn.close()


@router.post("/api/risk/settings")
def save_settings(req: RiskSettingsUpdateRequest, kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        updated = risk_engine.update_settings(conn, account["id"], **req.model_dump())
        return {"data": dict(updated)}
    finally:
        conn.close()


@router.get("/api/risk/sector-mappings")
def list_sector_mappings(kite: KiteConnect = Depends(require_trading_account)):
    return {"data": sector_mapping.list_mappings()}


@router.post("/api/risk/sector-mappings")
def save_sector_mapping(req: SectorMappingUpsertRequest, kite: KiteConnect = Depends(require_trading_account)):
    return {"data": sector_mapping.upsert_sector(req.symbol.strip().upper(), req.sector.strip())}


@router.delete("/api/risk/sector-mappings/{symbol}")
def delete_sector_mapping(symbol: str, kite: KiteConnect = Depends(require_trading_account)):
    sector_mapping.delete_sector(symbol.strip().upper())
    return {"data": {"deleted": True}}


@router.get("/api/risk/sectors")
def list_sectors(kite: KiteConnect = Depends(require_trading_account)):
    return {"data": sector_mapping.list_sectors()}


@router.get("/api/risk/unmapped-symbols")
def unmapped_symbols(kite: KiteConnect = Depends(require_trading_account)):
    return {"data": sector_mapping.find_unmapped_symbols(kite)}


@router.get("/api/risk/settings/history")
def settings_history(kite: KiteConnect = Depends(require_trading_account)):
    account = get_trading_account()
    conn = get_connection()
    try:
        return {"data": risk_engine.get_settings_history(conn, account["id"])}
    finally:
        conn.close()


@router.get("/api/risk/position-size")
def position_size(
    entry_price: float = Query(gt=0),
    stop_loss: float = Query(gt=0),
    kite: KiteConnect = Depends(require_trading_account),
):
    account = get_trading_account()
    conn = get_connection()
    try:
        settings = risk_engine.get_settings(conn, account["id"])
        try:
            ctx = risk_engine.build_portfolio_context(kite)
        except risk_engine.RiskCheckUnavailableError as e:
            raise HTTPException(status_code=e.status_code, detail={"error_code": e.error_code, "message": e.message})
        return {"data": risk_engine.compute_position_size(entry_price, stop_loss, ctx.portfolio_value, settings)}
    finally:
        conn.close()


@router.get("/api/risk/position-reduction")
def position_reduction(
    symbol: str = Query(min_length=1),
    kite: KiteConnect = Depends(require_trading_account),
):
    # Quick Action popup's concentration-alert default sell quantity: how
    # many shares to sell to bring `symbol` back to exactly
    # max_single_stock_exposure_pct — see
    # risk_engine.compute_suggested_exit_quantity for the basis/formula.
    account = get_trading_account()
    conn = get_connection()
    try:
        settings = risk_engine.get_settings(conn, account["id"])
        try:
            ctx = risk_engine.build_portfolio_context(kite)
        except risk_engine.RiskCheckUnavailableError as e:
            raise HTTPException(status_code=e.status_code, detail={"error_code": e.error_code, "message": e.message})
        return {"data": risk_engine.compute_suggested_exit_quantity(conn, account["id"], symbol.strip().upper(), ctx, settings)}
    finally:
        conn.close()
