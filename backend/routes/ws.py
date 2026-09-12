import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.ticker import ticker_manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/ticks")
async def ticks_ws(websocket: WebSocket):
    await websocket.accept()
    logger.info("Tick WS client connected")
    queue: asyncio.Queue = asyncio.Queue()
    ticker_manager.add_listener(queue)
    try:
        while True:
            message = await queue.get()
            await websocket.send_json(message)
    except WebSocketDisconnect as e:
        logger.info("Tick WS client disconnected: code=%s reason=%s", e.code, e.reason)
    except Exception:
        logger.exception("Tick WS error")
    finally:
        ticker_manager.remove_listener(queue)
