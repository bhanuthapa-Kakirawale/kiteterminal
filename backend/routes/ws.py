import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend import security
from backend.tick_queue import ClientQueue, QueueOverflow
from backend.ticker import ticker_manager

logger = logging.getLogger(__name__)

router = APIRouter()

# 1013 "Try Again Later": the client fell too far behind; it reconnects and
# re-reads its state (PK-22F).
_OVERFLOW_CLOSE_CODE = 1013


# Session expiry: how often an open socket re-checks its credential's deadlines at most. Expiry
# revokes the credential, and the revocation closes the socket (4401) like a logout.
_EXPIRY_CHECK_MAX_SECONDS = 1.0


async def _until_expired() -> None:
    """Returns once the credential is gone (expired here, or revoked elsewhere). The inactivity
    deadline can move while the socket is open, so the remaining time is re-read on every pass."""
    while True:
        remaining = security.credential_seconds_remaining()
        if remaining is None:
            return
        if remaining <= 0:
            security.expire_terminal_credential_if_due()
            return
        await asyncio.sleep(min(remaining, _EXPIRY_CHECK_MAX_SECONDS))


async def _until_disconnect(websocket: WebSocket):
    """Returns the close code once the client disconnects — or the server
    closes the socket on shutdown. Anything the client sends is ignored."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return message.get("code")


@router.websocket("/ws/ticks")
async def ticks_ws(websocket: WebSocket):
    # P2-G R-F4: a socket lives only as long as the credential it was opened under. The listener is
    # registered BEFORE the credential is re-checked here, so a logout or a new login that lands
    # between the handshake guard and this point is never missed: any issue or revoke closes it.
    loop = asyncio.get_running_loop()
    revoked = asyncio.Event()

    def _on_credential_change():
        try:
            loop.call_soon_threadsafe(revoked.set)
        except RuntimeError:        # the loop is already closed: the socket is gone with it
            pass

    security.add_credential_listener(_on_credential_change)
    try:
        offered = websocket.scope.get("subprotocols") or []
        await websocket.accept(subprotocol=security.WS_SUBPROTOCOL if security.WS_SUBPROTOCOL in offered else None)
        if revoked.is_set() or security.credential_rejection_reason(websocket.scope) is not None:
            await websocket.close(code=security.WS_REVOKED_CLOSE_CODE, reason="Terminal session revoked")
            return
        await _serve_ticks(websocket, revoked)
    finally:
        security.remove_credential_listener(_on_credential_change)


async def _serve_ticks(websocket: WebSocket, revoked: asyncio.Event):
    logger.info("Tick WS client connected")
    # PK-22F: bounded — ticks coalesce to the latest per instrument; events
    # are never dropped silently (see backend/tick_queue.py).
    queue = ClientQueue()
    ticker_manager.add_listener(queue)
    # LIVE-0: the socket is also watched for a disconnect. Waiting on the
    # queue alone never noticed a closed tab — or the server's own shutdown
    # close — while no tick flowed (market closed), so a graceful shutdown
    # waited on this handler until the browser went away, and a second
    # Ctrl+C then force-quit it mid-await.
    disconnected = asyncio.ensure_future(_until_disconnect(websocket))
    revocation = asyncio.ensure_future(revoked.wait())
    # Session expiry: at the credential's deadline it is revoked, which sets `revoked` above.
    expiry = asyncio.ensure_future(_until_expired())
    try:
        while True:
            next_message = asyncio.ensure_future(queue.get())
            await asyncio.wait({next_message, disconnected, revocation}, return_when=asyncio.FIRST_COMPLETED)
            if disconnected.done():
                if next_message.done():
                    next_message.exception()   # retrieved: the client is gone, nothing is sent
                else:
                    next_message.cancel()
                logger.info("Tick WS client disconnected: code=%s", disconnected.result())
                break
            if revocation.done():
                # Logout, expiry, restart-equivalent revoke or a new login: nothing more is sent on
                # this socket.
                if next_message.done():
                    next_message.exception()
                else:
                    next_message.cancel()
                logger.info("event=ws.credential_revoked: closing the tick socket")
                await websocket.close(code=security.WS_REVOKED_CLOSE_CODE, reason="Terminal session revoked")
                break
            try:
                message = next_message.result()
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
        disconnected.cancel()
        revocation.cancel()
        expiry.cancel()
        ticker_manager.remove_listener(queue)
