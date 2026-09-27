import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend import (
    automation,
    data_jobs,
    instruments,
    kite_client,
    order_monitor,
    reconciliation,
    sector_mapping,
    trailing_engine,
)
from backend.accounts import get_default_account
from backend.db import init_db
from backend.security import TerminalOriginGuard
from backend.routes import (
    auth,
    automation as automation_routes,
    dashboard_panels,
    instruments as instruments_routes,
    journal as journal_routes,
    kill_switch,
    orders,
    portfolio,
    reconciliation as reconciliation_routes,
    risk,
    strategies as strategies_routes,
    tickets,
    ui_prefs,
    watchlist as watchlist_routes,
    ws,
)
from backend.ticker import collect_instrument_tokens, ticker_manager
from backend.watchlist import watchlist_instrument_tokens

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="Kite Terminal")

_BACKGROUND_LOOPS = {
    "trailing_task": trailing_engine.run_trailing_loop,
    "data_job_worker_task": data_jobs.run_job_worker,
    "reconciliation_task": reconciliation.run_reconciliation_loop,
    "order_monitor_task": order_monitor.run_order_monitor_loop,
    "automation_task": automation.run_automation_loop,
}


@app.on_event("startup")
async def on_startup():
    init_db()
    ticker_manager.set_loop(asyncio.get_running_loop())
    try:
        kite = kite_client.get_kite()
        account = get_default_account()
        tokens = set(collect_instrument_tokens(kite)) | set(watchlist_instrument_tokens(account["id"]))
        ticker_manager.start(account["api_key"], kite.access_token, list(tokens))
        instruments.refresh_instrument_cache_if_stale(kite)
        sector_mapping.seed_from_holdings(kite)
    except kite_client.NoActiveSessionError:
        logger.info("No active Kite session; ticker/instrument cache not started")
    except Exception:
        logger.exception("Failed to start ticker/instrument cache on startup")

    for name, loop in _BACKGROUND_LOOPS.items():
        setattr(app.state, name, asyncio.create_task(loop()))
    # A due autopilot run syncs market data as its first step; otherwise
    # refresh holdings/watchlist history once at startup.
    if not automation.is_due():
        app.state.startup_sync_task = asyncio.create_task(data_jobs.sync_all_symbols())


@app.on_event("shutdown")
def on_shutdown():
    ticker_manager.stop()
    for task_name in (*_BACKGROUND_LOOPS, "startup_sync_task"):
        task = getattr(app.state, task_name, None)
        if task is not None:
            task.cancel()


app.include_router(auth.router)
app.include_router(portfolio.router)
app.include_router(ws.router)
app.include_router(tickets.router)
app.include_router(orders.router)
app.include_router(instruments_routes.router)
app.include_router(risk.router)
app.include_router(ui_prefs.router)
app.include_router(dashboard_panels.router)
app.include_router(watchlist_routes.router)
app.include_router(strategies_routes.router)
app.include_router(journal_routes.router)
app.include_router(kill_switch.router)
app.include_router(reconciliation_routes.router)
app.include_router(automation_routes.router)

# Rejects cross-origin writes and WebSocket handshakes before routing (P0-8).
app.add_middleware(TerminalOriginGuard)

# Frontend is served same-origin from this app, so no CORS middleware is needed.
# Its absence is part of the P0-8 defence: a cross-origin preflight for the
# X-Kite-Terminal header gets no Access-Control-Allow-* reply and fails.
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
