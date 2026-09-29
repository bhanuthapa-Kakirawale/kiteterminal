import asyncio
import logging
import threading

from kiteconnect import KiteConnect, KiteTicker

logger = logging.getLogger(__name__)

# Kite closes the ticker socket with 4403 (TokenException analogue) when the
# access token backing it is no longer valid.
_AUTH_FAILURE_CLOSE_CODES = {403, 4403}


def collect_instrument_tokens(kite: KiteConnect) -> list[int]:
    tokens = set()
    for h in kite.holdings():
        if h.get("instrument_token"):
            tokens.add(h["instrument_token"])
    positions = kite.positions()
    for p in positions.get("net", []):
        if p.get("instrument_token"):
            tokens.add(p["instrument_token"])
    return list(tokens)


class TickerManager:
    """Wraps KiteTicker and fans out simplified tick updates to any number of
    registered asyncio.Queue listeners (one per connected browser WebSocket).

    KiteTicker's own callbacks fire on its background thread, so broadcasting
    into listener queues goes through loop.call_soon_threadsafe rather than a
    direct await.
    """

    def __init__(self):
        self._kws: KiteTicker | None = None
        self._api_key: str | None = None
        self._instrument_tokens: list[int] = []
        # Per-socket outboxes (backend/tick_queue.ClientQueue). Added and
        # removed on the event loop, iterated from KiteTicker's thread and the
        # worker threads: guarded by a lock (PK-22F).
        self._listeners: set = set()
        self._listeners_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def is_connected(self) -> bool:
        return self._kws is not None and self._kws.is_connected()

    def add_listener(self, queue) -> None:
        with self._listeners_lock:
            self._listeners.add(queue)

    def remove_listener(self, queue) -> None:
        with self._listeners_lock:
            self._listeners.discard(queue)

    def listener_count(self) -> int:
        with self._listeners_lock:
            return len(self._listeners)

    def start(self, api_key: str, access_token: str, instrument_tokens: list[int]) -> None:
        self.stop()
        self._api_key = api_key
        self._instrument_tokens = instrument_tokens

        logger.info("Starting KiteTicker for %d instruments", len(instrument_tokens))
        kws = KiteTicker(api_key, access_token)

        def on_connect(ws, response):
            logger.info("KiteTicker connected: %s", response)
            ws.subscribe(self._instrument_tokens)
            ws.set_mode(ws.MODE_QUOTE, self._instrument_tokens)

        def on_ticks(ws, ticks):
            # PK-22F: a malformed tick is skipped, not allowed to drop the batch.
            simplified = [
                {
                    "instrument_token": t["instrument_token"],
                    "last_price": t.get("last_price"),
                    "close_price": (t.get("ohlc") or {}).get("close"),
                }
                for t in ticks
                if isinstance(t, dict) and t.get("instrument_token") is not None
            ]
            if simplified:
                self._broadcast({"type": "ticks", "data": simplified})

        def on_close(ws, code, reason):
            logger.warning("KiteTicker closed: %s %s", code, reason)
            if code in _AUTH_FAILURE_CLOSE_CODES:
                self._broadcast({"type": "auth_error"})

        def on_error(ws, code, reason):
            logger.error("KiteTicker error: code=%s reason=%s", code, reason)

        def on_reconnect(ws, attempts_count):
            logger.warning("KiteTicker reconnecting: attempt %s", attempts_count)

        def on_noreconnect(ws):
            logger.error("KiteTicker gave up reconnecting")

        kws.on_connect = on_connect
        kws.on_ticks = on_ticks
        kws.on_close = on_close
        kws.on_error = on_error
        kws.on_reconnect = on_reconnect
        kws.on_noreconnect = on_noreconnect

        self._kws = kws
        kws.connect(threaded=True)

    def restart_with_new_token(self, access_token: str) -> None:
        if self._api_key is None:
            return
        self.start(self._api_key, access_token, self._instrument_tokens)

    def update_instruments(self, instrument_tokens: list[int]) -> None:
        self._instrument_tokens = instrument_tokens
        if self.is_connected():
            self._kws.subscribe(instrument_tokens)
            self._kws.set_mode(self._kws.MODE_QUOTE, instrument_tokens)

    def stop(self) -> None:
        if self._kws is not None:
            try:
                self._kws.close()
            except Exception:
                logger.exception("Error closing KiteTicker")
            self._kws = None

    def _broadcast(self, message: dict) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._listeners_lock:
            listeners = list(self._listeners)
        for queue in listeners:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, message)
            except RuntimeError:
                return  # the event loop closed (shutdown): nothing left to deliver to

    def broadcast(self, message: dict) -> None:
        """Public entrypoint for broadcasters other than KiteTicker's own
        callbacks — the background loops, which call it from their worker
        threads (trailing engine, order monitor). Safe from any thread:
        _broadcast hands every message over with call_soon_threadsafe."""
        self._broadcast(message)


ticker_manager = TickerManager()
