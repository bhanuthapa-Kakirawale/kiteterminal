"""Per-browser-socket outbox with bounded memory (PK-22F, audit P2-7).

Before PK-22 each /ws/ticks client had an unbounded asyncio.Queue: a
backgrounded or slow tab accumulated every tick of every instrument without
limit. The same queue also carries the messages the UI must not miss
(order_update, trailing_error, tickets_changed, mode_changed, auth_error...),
so it could not simply be given a maxsize and made to drop.

ClientQueue treats the two differently:

* Market ticks are COALESCED: only the latest tick per instrument is kept,
  and all pending ticks go out as one "ticks" message. A tab that falls
  behind receives the current prices, not a backlog of stale ones. Memory is
  bounded by the number of instruments (capped at max_tokens).
* Every other message is an EVENT, delivered in order and never dropped
  silently. Events are small and rare; if a client falls max_events behind
  anyway, the queue is marked overflowed and the socket is closed (1013)
  instead of losing one. The browser reconnects and re-reads its state from
  the server (the database is the source of truth; the events are only
  prompts to refresh).
"""

import asyncio
from collections import deque

from backend import config


class QueueOverflow(Exception):
    pass


class ClientQueue:
    def __init__(self, max_events: int | None = None, max_tokens: int | None = None):
        self._events: deque = deque()
        self._ticks: dict = {}  # instrument_token -> latest tick (insertion order = age)
        self._max_events = max_events or config.WS_MAX_PENDING_EVENTS
        self._max_tokens = max_tokens or config.WS_MAX_COALESCED_TOKENS
        self._ready = asyncio.Event()
        self.overflowed = False
        self.coalesced_ticks = 0    # older ticks replaced by a newer one for the same instrument
        self.dropped_instruments = 0

    def put_nowait(self, message: dict) -> None:
        """Called on the event loop (TickerManager hands messages over with
        call_soon_threadsafe). Never raises, never blocks."""
        if isinstance(message, dict) and message.get("type") == "ticks":
            for tick in message.get("data") or []:
                token = tick.get("instrument_token") if isinstance(tick, dict) else None
                if token is None:
                    continue
                if token in self._ticks:
                    del self._ticks[token]
                    self.coalesced_ticks += 1
                elif len(self._ticks) >= self._max_tokens:
                    del self._ticks[next(iter(self._ticks))]  # the stalest instrument
                    self.dropped_instruments += 1
                self._ticks[token] = tick
        elif self.overflowed:
            pass  # the socket is being closed; the client will resync
        elif len(self._events) >= self._max_events:
            self.overflowed = True
        else:
            self._events.append(message)
        self._ready.set()

    async def get(self) -> dict:
        """Next message: queued events first (in order), then one coalesced
        ticks message. Raises QueueOverflow once events were lost."""
        while True:
            if self.overflowed:
                raise QueueOverflow()
            if self._events:
                return self._events.popleft()
            if self._ticks:
                data = list(self._ticks.values())
                self._ticks.clear()
                return {"type": "ticks", "data": data}
            self._ready.clear()
            await self._ready.wait()

    def pending_events(self) -> int:
        return len(self._events)

    def pending_ticks(self) -> int:
        return len(self._ticks)
