from fastapi import APIRouter, Depends, HTTPException
from kiteconnect import KiteConnect

from backend import automation, workers
from backend.kite_client import require_trading_account
from backend.instance_lock import WRITE_AUTHORITY

router = APIRouter()


@router.get("/api/automation/status")
def automation_status(kite: KiteConnect = Depends(require_trading_account)):
    return {"data": automation.get_status()}


@router.post("/api/automation/run-now", status_code=202, dependencies=WRITE_AUTHORITY)
async def automation_run_now(kite: KiteConnect = Depends(require_trading_account)):
    if automation.is_running():
        raise HTTPException(409, {"error_code": "ALREADY_RUNNING", "message": "The autopilot is already running."})
    workers.supervisor.spawn("autopilot_manual", automation.run_pipeline("manual"))
    return {"data": {"started": True}}
