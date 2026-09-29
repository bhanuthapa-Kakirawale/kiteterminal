"""Observability (PK-22E, audit P2-9): correlation ids, structured event
lines and bounded file logging — built on the PK-18 redaction layer.

* Correlation id. Every HTTP request and WebSocket gets one (echoed back as
  X-Request-ID); every background-worker cycle sets its own (om-…, tr-…,
  rc-…). It lives in a ContextVar, which asyncio.to_thread copies into the
  worker thread, so everything a request or cycle logs carries the same id.
  A confirmation is then traceable across HTTP request -> claim -> broker
  submission by that id, and on to the order monitor by ticket_id and the
  order id (the monitor's cycle has its own id).
* Structured events. log_event(logger, "order.submitted", ticket_id=…, …)
  writes one stable `event=… key=value … cid=…` line: grep-able, and still
  plain text through the same handlers and redaction filter. Only the
  critical flows use it; other log lines are unchanged.
* File logging. When KITE_TERMINAL_LOG_FILE is set (the launcher sets it by
  default), a RotatingFileHandler bounds disk use: LOG_FILE_MAX_BYTES x
  (LOG_FILE_BACKUP_COUNT + 1). Bare uvicorn and tests stay stdout-only.
"""

import logging
import os
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path

from backend import config
from backend.redaction import redact_text

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(cid)s]: %(message)s"
FILE_HANDLER_NAME = "kite_terminal_file"

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")

# A client may pass its own X-Request-ID; only a plain token is kept.
_CLIENT_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

# Fields always written in this order when present; the rest follow sorted.
_FIELD_ORDER = ("account_id", "ticket_id", "position_id", "exit_ticket_id", "symbol", "mode", "order_id",
                "paper_order_id", "order_tag", "gtt_id", "status", "reason", "severity")


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@contextmanager
def correlation(cid: str):
    token = correlation_id.set(cid)
    try:
        yield cid
    finally:
        correlation_id.reset(token)


def _value(v) -> str:
    text = "-" if v is None else str(v)
    if text == "" or any(c in text for c in ' "=\n\r\t'):
        text = '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'
    return text


def format_event(event: str, **fields) -> str:
    keys = [k for k in _FIELD_ORDER if k in fields] + sorted(k for k in fields if k not in _FIELD_ORDER)
    parts = [f"event={event}"] + [f"{k}={_value(fields[k])}" for k in keys] + [f"cid={correlation_id.get()}"]
    return redact_text(" ".join(parts))


def log_event(logger: logging.Logger, event: str, severity: str = "info", **fields) -> None:
    level = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
             "error": logging.ERROR, "critical": logging.CRITICAL}.get(severity, logging.INFO)
    logger.log(level, "%s", format_event(event, **fields),
               extra={"kt_event": event, "kt_fields": {k: v for k, v in fields.items()}})


class CorrelationFormatter(logging.Formatter):
    """Tolerates records that no filter stamped with a cid."""

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "cid"):
            record.cid = correlation_id.get()
        return super().format(record)


class CorrelationIdMiddleware:
    """Pure ASGI: one correlation id per HTTP request / WebSocket, echoed as
    X-Request-ID on HTTP responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        supplied = None
        for name, value in scope.get("headers") or []:
            if name == b"x-request-id":
                candidate = value.decode("latin-1")
                supplied = candidate if _CLIENT_ID.match(candidate) else None
                break
        cid = supplied or new_id("rq-")

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", cid.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        with correlation(cid):
            return await self.app(scope, receive, send_with_id if scope["type"] == "http" else send)


def use_correlation_formatter() -> None:
    """Root handlers (basicConfig's stream handler) get the cid format."""
    for handler in logging.getLogger().handlers:
        if not isinstance(handler.formatter, CorrelationFormatter):
            handler.setFormatter(CorrelationFormatter(LOG_FORMAT))


def install_file_logging(path: str | None = None) -> RotatingFileHandler | None:
    """Idempotent. Attaches one bounded, rotating file handler to the root
    and uvicorn loggers when KITE_TERMINAL_LOG_FILE (or `path`) names a file;
    'off'/'0'/'' disables it. Returns the handler, or None."""
    path = path if path is not None else os.environ.get("KITE_TERMINAL_LOG_FILE", "")
    if not path or path.strip().lower() in ("off", "0", "none", "false"):
        return None
    targets = [logging.getLogger(name) for name in ("", "uvicorn", "uvicorn.access")]
    for logger in targets:
        for handler in logger.handlers:
            if handler.get_name() == FILE_HANDLER_NAME:
                return handler
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=config.LOG_FILE_MAX_BYTES,
                                  backupCount=config.LOG_FILE_BACKUP_COUNT, encoding="utf-8", delay=True)
    handler.set_name(FILE_HANDLER_NAME)
    handler.setFormatter(CorrelationFormatter(LOG_FORMAT))
    for logger in targets:
        logger.addHandler(handler)
    return handler


def file_logging_status() -> dict:
    for handler in logging.getLogger().handlers:
        if handler.get_name() == FILE_HANDLER_NAME:
            return {"enabled": True, "file": Path(handler.baseFilename).name,
                    "max_bytes": handler.maxBytes, "backup_count": handler.backupCount}
    return {"enabled": False, "file": None, "max_bytes": None, "backup_count": None}
