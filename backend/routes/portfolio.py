from fastapi import APIRouter, Depends, HTTPException
from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException, TokenException

from backend import config
from backend.kite_client import require_kite

router = APIRouter()


@router.get("/api/config")
def get_config():
    return {
        "holding_loss_alert_pct": config.HOLDING_LOSS_ALERT_PCT,
        "concentration_alert_pct": config.CONCENTRATION_ALERT_PCT,
        "day_pnl_alert_pct": config.DAY_PNL_ALERT_PCT,
        "session_expiry_warning_minutes": config.SESSION_EXPIRY_WARNING_MINUTES,
        "margin_alert_threshold_inr": config.MARGIN_ALERT_THRESHOLD_INR,
    }


def _call(kite_fn):
    try:
        return {"data": kite_fn()}
    except TokenException:
        raise HTTPException(
            status_code=401,
            detail={
                "error_code": "SESSION_EXPIRED",
                "message": "Your Zerodha session has expired. Please reconnect.",
            },
        )
    except KiteException as e:
        raise HTTPException(
            status_code=502,
            detail={"error_code": "KITE_API_ERROR", "message": str(e)},
        )


@router.get("/api/profile")
def get_profile(kite: KiteConnect = Depends(require_kite)):
    return _call(kite.profile)


@router.get("/api/holdings")
def get_holdings(kite: KiteConnect = Depends(require_kite)):
    return _call(kite.holdings)


@router.get("/api/positions")
def get_positions(kite: KiteConnect = Depends(require_kite)):
    return _call(kite.positions)


@router.get("/api/mf_holdings")
def get_mf_holdings(kite: KiteConnect = Depends(require_kite)):
    return _call(kite.mf_holdings)


@router.get("/api/margins")
def get_margins(kite: KiteConnect = Depends(require_kite)):
    return _call(kite.margins)
