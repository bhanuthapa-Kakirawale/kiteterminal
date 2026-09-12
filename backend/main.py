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

from backend import kite_client
from backend.accounts import get_default_account
from backend.db import init_db
from backend.routes import auth, portfolio, ws
from backend.ticker import collect_instrument_tokens, ticker_manager

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="Kite Terminal")


@app.on_event("startup")
async def on_startup():
    init_db()
    ticker_manager.set_loop(asyncio.get_running_loop())
    try:
        kite = kite_client.get_kite()
        account = get_default_account()
        tokens = collect_instrument_tokens(kite)
        ticker_manager.start(account["api_key"], kite.access_token, tokens)
    except kite_client.NoActiveSessionError:
        logger.info("No active Kite session; ticker not started")
    except Exception:
        logger.exception("Failed to start ticker on startup")


@app.on_event("shutdown")
def on_shutdown():
    ticker_manager.stop()


app.include_router(auth.router)
app.include_router(portfolio.router)
app.include_router(ws.router)

# Frontend is served same-origin from this app, so no CORS middleware is needed.
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
