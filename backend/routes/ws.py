import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.tick_queue import ClientQueue, QueueOverflow
from backend.ticker import ticker_manager

logger = logging.getLogger(__name__)

router = APIRouter()

# 1013 "Try Again Later": the client fell too far behind; it reconnects and
# re-reads its state (PK-22F).
_OVERFLOW_CLOSE_CODE = 1013


@router.websocket("/ws/ticks")
async def ticks_ws(websocket: WebSocket):
    await websocket.accept()
    logger.info("Tick WS client connected")
    # PK-22F: bounded — ticks coalesce to the latest per instrument; events
    # are never dropped silently (see backend/tick_queue.py).
    queue = ClientQueue()
    ticker_manager.add_listener(queue)
    try:
        while True:
            try:
                message = await queue.get()
            except QueueOverflow:
                logger.warning("event=ws.client_overflow pending_events=%d: closing so the client resyncs",
                               queue.pending_events())
                await websocket.close(code=_OVERFLOW_CLOSE_CODE, reason="Too far behind; reconnect to resync")
                break
            await websocket.send_json(message)
    except WebSocketDisconnect as e:
        logger.info("Tick WS client disconnected: code=%s reason=%s", e.code, e.reason)
    except Exception:
        logger.exception("Tick WS error")
    finally:
        ticker_manager.remove_listener(queue)
