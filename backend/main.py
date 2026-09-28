import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from backend.redaction import install_log_redaction, redact_detail

# Before anything can log a token: uvicorn's access log alone would print
# /callback?request_token=... (PK-18).
install_log_redaction()

from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

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
from backend import server
from backend.accounts import get_default_account
from backend.db import init_db
from backend.security import SecurityHeadersMiddleware, TerminalOriginGuard, TrustedHostGuard
from backend.routes import (
    account_safety as account_safety_routes,
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

# No Swagger/ReDoc pages: they load their JavaScript from a public CDN, which
# the Content-Security-Policy forbids (PK-18).
app = FastAPI(title="Kite Terminal", docs_url=None, redoc_url=None)


@app.exception_handler(StarletteHTTPException)
async def redacting_http_exception_handler(request, exc):
    # Many error details quote a broker exception's text; none may carry a
    # credential to the browser (PK-18). Unhandled exceptions never get here:
    # they become a bare "Internal Server Error" with the traceback only in
    # the (redacted) server log.
    exc.detail = redact_detail(exc.detail)
    return await http_exception_handler(request, exc)

_BACKGROUND_LOOPS = {
    "trailing_task": trailing_engine.run_trailing_loop,
    "data_job_worker_task": data_jobs.run_job_worker,
    "reconciliation_task": reconciliation.run_reconciliation_loop,
    "order_monitor_task": order_monitor.run_order_monitor_loop,
    "automation_task": automation.run_automation_loop,
}


@app.on_event("startup")
async def on_startup():
    # First: an unsafe bind or worker count aborts startup before any
    # background loop or broker session can start (PK-18).
    install_log_redaction()  # again, in case the server replaced its handlers
    server.enforce_runtime_policy()
    init_db()
    # PK-14: every watched/traded symbol gets its static sector before any
    # risk check runs — no Kite session needed. Never overwrites a manual one.
    try:
        sector_mapping.seed_known_symbols()
    except Exception:
        logger.exception("Sector mapping seed failed on startup")
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
app.include_router(account_safety_routes.router)

# Registered innermost first; each add_middleware wraps the previous, so a
# request meets them in reverse order: security headers, then the Host check
# (DNS rebinding, reads and writes, PK-18), then the cross-origin write and
# WebSocket check (P0-8). All three answer before routing.
app.add_middleware(TerminalOriginGuard)
app.add_middleware(TrustedHostGuard)
app.add_middleware(SecurityHeadersMiddleware)

# Frontend is served same-origin from this app, so no CORS middleware is needed.
# Its absence is part of the P0-8 defence: a cross-origin preflight for the
# X-Kite-Terminal header gets no Access-Control-Allow-* reply and fails.
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
