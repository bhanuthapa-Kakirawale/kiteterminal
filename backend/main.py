import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from backend import obs  # noqa: E402

logging.basicConfig(level=logging.INFO, format=obs.LOG_FORMAT)
# PK-22E: every line carries its correlation id; with KITE_TERMINAL_LOG_FILE
# set (the launcher's default) it also goes to a bounded, rotating file.
obs.use_correlation_formatter()
obs.install_file_logging()

from backend.redaction import install_log_redaction, redact_detail

# Before anything can log a token: uvicorn's access log alone would print
# /callback?request_token=... (PK-18). Also covers the file handler above.
install_log_redaction()

from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import (
    automation,
    config,
    data_jobs,
    db,
    instance_lock,
    instruments,
    kite_client,
    order_monitor,
    reconciliation,
    sector_mapping,
    startup_reconciliation,
    trailing_engine,
    version,
    workers,
)
from backend import server
from backend.accounts import get_default_account
from backend.db import init_db
from backend.obs import CorrelationIdMiddleware, log_event
from backend.security import (
    SecurityHeadersMiddleware, TerminalCredentialGuard, TerminalOriginGuard, TrustedHostGuard,
)
from backend.routes import (
    account_safety as account_safety_routes,
    auth,
    automation as automation_routes,
    dashboard_panels,
    data_health as data_health_routes,
    diagnostics as diagnostics_routes,
    dmb_inbox as dmb_inbox_routes,
    instruments as instruments_routes,
    journal as journal_routes,
    kill_switch,
    live_readiness as live_readiness_routes,
    orders,
    portfolio,
    positions as positions_routes,
    reconciliation as reconciliation_routes,
    research_scanner as research_scanner_routes,
    risk,
    smart_money_score as smart_money_score_routes,
    strategies as strategies_routes,
    tickets,
    ui_prefs,
    volume_intelligence as volume_intelligence_routes,
    watchlist as watchlist_routes,
    ws,
)
from backend.ticker import collect_instrument_tokens, ticker_manager
from backend.watchlist import watchlist_instrument_tokens

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# No Swagger/ReDoc pages: they load their JavaScript from a public CDN, which
# the Content-Security-Policy forbids (PK-18).
app = FastAPI(title="Kite Terminal", version=version.APP_VERSION, docs_url=None, redoc_url=None)


@app.exception_handler(StarletteHTTPException)
async def redacting_http_exception_handler(request, exc):
    # Many error details quote a broker exception's text; none may carry a
    # credential to the browser (PK-18). Unhandled exceptions never get here:
    # they become a bare "Internal Server Error" with the traceback only in
    # the (redacted) server log.
    exc.detail = redact_detail(exc.detail)
    return await http_exception_handler(request, exc)

# The supervisor's names; diagnostics and readiness report them by these.
_BACKGROUND_LOOPS = {
    "trailing": trailing_engine.run_trailing_loop,
    "data_jobs": data_jobs.run_job_worker,
    "reconciliation": reconciliation.run_reconciliation_loop,
    "order_monitor": order_monitor.run_order_monitor_loop,
    "automation": automation.run_automation_loop,
}


@app.on_event("startup")
async def on_startup():
    # First: an unsafe bind or worker count aborts startup before any
    # background loop or broker session can start (PK-18).
    obs.install_file_logging()  # again, in case the server replaced its handlers
    install_log_redaction()
    server.enforce_runtime_policy()
    # Phase 2 (docs/PHASE_2_DESIGN_REVIEW.md §8.2): the OS instance lock BEFORE init_db. A process that
    # cannot take it (another instance owns the database), or whose database is not on a fixed local
    # drive, is read-only: no init_db (no migration, no invariant, no key minting), no ticker, no loops;
    # a status page, and 503 for every write (login and callback included).
    role = instance_lock.acquire(db.DB_PATH)
    if role != instance_lock.LOCKED and role != instance_lock.OWNER:
        log_event(logger, "startup.read_only", role=role, code=instance_lock.refusal_code())
        return
    # A database written by newer code is refused here, before any loop
    # starts (DATABASE_SCHEMA_UNSUPPORTED, PK-22A). Migrations now run under the lock.
    try:
        init_db()
    except (db.DatabaseMigrationAuthorizationRequired, db.DatabaseInstallationStateRefused) as e:
        # A registered migration that needs explicit operator authorization (v12) is pending, or the database of
        # an established installation is missing / empty / replaced / mid-maintenance (V12 final readiness,
        # Phase C): init_db refused before any write. Release the lock and abort startup; no takeover, loop,
        # ticker or broker session.
        instance_lock.release()
        log_event(logger, "startup.refused", severity="critical", code=e.error_code)
        logger.critical("%s", e)
        raise
    except BaseException:
        instance_lock.release()
        raise
    log_event(logger, "startup.database", schema_version=db.LATEST_SCHEMA_VERSION,
              applied_migrations=",".join(map(str, db.last_init.get("applied_migrations", []))) or "none",
              startup_row_changes=db.last_init.get("startup_row_changes"))
    # The fenced epoch takeover; it persists RESTRICTED(STARTUP_RECONCILIATION_INCOMPLETE [,
    # PREVIOUS_INSTANCE_UNCLEAN]) in the same transaction, before any loop starts. After an unclean
    # predecessor it waits out the lease (at most INSTANCE_LEASE_SECONDS).
    takeover = await asyncio.to_thread(instance_lock.takeover)
    if takeover.get("conflict"):
        log_event(logger, "startup.read_only", role=instance_lock.CONFLICT, code=instance_lock.INSTANCE_CONFLICT)
        return
    log_event(logger, "startup.instance", epoch=takeover["epoch"],
              previous_instance_unclean=takeover.get("previous_unclean"))
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

    # PK-22F: started through the supervisor — never twice in one process. They start before Stage 1
    # on purpose (§8.2): they do today's exposure-reducing upkeep, which RESTRICTED never blocks, and
    # each checks write authority at the start of every cycle.
    workers.supervisor.start(_BACKGROUND_LOOPS)
    workers.supervisor.start({"instance_heartbeat": instance_lock.run_heartbeat_loop})
    # Stage 1 (read-only) startup reconciliation, retried until it passes; new live entries stay
    # refused until it has passed and every restriction reason is clear. Stage 2 is empty in Phase 2.
    startup_reconciliation.request_run("startup")
    # A due autopilot run syncs market data as its first step; otherwise
    # refresh holdings/watchlist history once at startup.
    try:
        due = automation.is_due()
    except Exception:
        logger.exception("Could not read the autopilot schedule at startup")
        due = True  # its loop decides once it can read it; no duplicate sync meanwhile
    if not due:
        workers.supervisor.spawn("startup_sync", data_jobs.sync_all_symbols())
    log_event(logger, "startup.complete", version=version.APP_VERSION, commit=version.commit_short())


@app.on_event("shutdown")
async def on_shutdown():
    # Phase 2 §7.6: authority drops first (the fence refuses new broker writes, routes return 503).
    instance_lock.begin_shutdown()
    ticker_manager.stop()
    # Cancel AND await every loop and tracked task (bounded), so nothing
    # keeps running after the app reports it has stopped.
    await workers.supervisor.stop(timeout=config.WORKER_SHUTDOWN_TIMEOUT_SECONDS)
    # Wait (bounded) for a broker write still in flight, record the shutdown (unclean if one was),
    # then release the instance lock.
    await asyncio.to_thread(instance_lock.finish_shutdown)


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
app.include_router(positions_routes.router)
app.include_router(reconciliation_routes.router)
app.include_router(automation_routes.router)
app.include_router(account_safety_routes.router)
app.include_router(diagnostics_routes.router)
app.include_router(data_health_routes.router)
app.include_router(live_readiness_routes.router)
app.include_router(volume_intelligence_routes.router)
app.include_router(smart_money_score_routes.router)
app.include_router(research_scanner_routes.router)
app.include_router(dmb_inbox_routes.router)

# Registered innermost first; each add_middleware wraps the previous, so a
# request meets them in reverse order: security headers, then the Host check
# (DNS rebinding, reads and writes, PK-18), then the cross-origin write and
# WebSocket check (P0-8), then the per-launch terminal credential every write
# and WebSocket handshake must carry (SEC-1 P1-F). All answer before routing.
# Innermost (Phase 2): a read-only process (another instance owns the database) serves a status page
# and refuses everything else; an owner passes straight through.
app.add_middleware(instance_lock.InstanceOwnershipGuard)
app.add_middleware(TerminalCredentialGuard)
app.add_middleware(TerminalOriginGuard)
app.add_middleware(TrustedHostGuard)
app.add_middleware(SecurityHeadersMiddleware)
# Outermost (PK-22E): even a refused request gets a correlation id.
app.add_middleware(CorrelationIdMiddleware)

# Frontend is served same-origin from this app, so no CORS middleware is needed.
# Its absence is part of the P0-8 defence: a cross-origin preflight for the
# X-Kite-Terminal header gets no Access-Control-Allow-* reply and fails.
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
