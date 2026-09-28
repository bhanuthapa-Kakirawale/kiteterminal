"""Secret redaction for logs and client-facing error text (PK-18, audit P1-12).

Two layers, applied together by ``redact_text``:

- **By name.** The value after a secret-bearing key is replaced wherever it
  appears as ``key=value`` (query strings, env lines), ``key: value`` or
  ``"key": "value"`` (JSON, headers). Only these exact key names are matched,
  on word boundaries, so ``instrument_token`` and similar fields are untouched.
  Kite's ``Authorization: token api_key:access_token`` form is also covered.
- **By value.** Every registered secret (each Kite access token as it is
  obtained) and the current ``KITE_API_SECRET`` and encryption key from the
  environment are replaced verbatim wherever they appear, whatever surrounds
  them.

``SecretRedactingFilter`` applies this to log records. It redacts the format
string and each argument separately rather than collapsing them, because
uvicorn's access formatter unpacks ``record.args`` positionally. Tracebacks
are rendered and redacted into ``record.exc_text``, which formatters reuse.
"""

import logging
import os
import re
import threading

REDACTED = "[REDACTED]"

_SECRET_KEYS = (
    "access_token", "request_token", "refresh_token", "public_token", "enctoken",
    "api_secret", "api_secret_enc", "access_token_enc", "password", "checksum",
    "kite_api_secret", "kite_terminal_encryption_key",
)
_KEY_ALT = "|".join(sorted(_SECRET_KEYS, key=len, reverse=True))
# key, optional closing quote, separator, optional opening quote, then the
# value. The lookahead keeps it idempotent: a record passes through both a
# logger's and a handler's filter, and "[REDACTED]" must not be re-matched.
_BY_KEY = re.compile(
    rf"(?i)(?<![A-Za-z0-9_])((?:{_KEY_ALT})[\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED\])([^\s\"'&,;}}\]]+)")
# Whole-line header values: Authorization, Cookie and Set-Cookie.
_BY_HEADER = re.compile(r"(?i)(?<![A-Za-z0-9_-])((?:authorization|set-cookie|cookie)[\"']?\s*[:=]\s*[\"']?)([^\r\n\"']+)")
# Kite's auth scheme outside a header: "token <api_key>:<access_token>".
_KITE_AUTH = re.compile(r"(?i)\btoken\s+[A-Za-z0-9]+:[A-Za-z0-9]+")

_ENV_SECRETS = ("KITE_API_SECRET", "KITE_TERMINAL_ENCRYPTION_KEY")
_MIN_SECRET_LEN = 8
_MAX_REGISTERED = 64

_lock = threading.Lock()
_registered: dict[str, None] = {}  # insertion-ordered set, oldest evicted first


def register_secret(value: str | None) -> None:
    """Redact this exact value from every later log line and error message."""
    if not value or len(value) < _MIN_SECRET_LEN:
        return
    with _lock:
        _registered.pop(value, None)
        _registered[value] = None
        while len(_registered) > _MAX_REGISTERED:
            _registered.pop(next(iter(_registered)))


def _known_secrets() -> list[str]:
    with _lock:
        values = set(_registered)
    for name in _ENV_SECRETS:
        value = os.environ.get(name)
        if value and len(value) >= _MIN_SECRET_LEN:
            values.add(value)
    return sorted(values, key=len, reverse=True)


def redact_text(text):
    """``text`` with secrets replaced by ``[REDACTED]``; non-strings pass through."""
    if not isinstance(text, str) or not text:
        return text
    text = _BY_HEADER.sub(lambda m: m.group(1) + REDACTED, text)
    text = _BY_KEY.sub(lambda m: m.group(1) + REDACTED, text)
    text = _KITE_AUTH.sub("token " + REDACTED, text)
    for secret in _known_secrets():
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def redact_detail(detail):
    """An HTTPException detail with every string inside it redacted."""
    if isinstance(detail, str):
        return redact_text(detail)
    if isinstance(detail, dict):
        return {k: redact_detail(v) for k, v in detail.items()}
    if isinstance(detail, (list, tuple)):
        return [redact_detail(v) for v in detail]
    return detail


def _redact_arg(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, BaseException):
        # An exception passed as a %s argument is rendered by str(); render
        # it here so a secret in its message cannot slip through.
        return redact_text(str(value))
    return value


class SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact_text(record.msg)
            elif isinstance(record.msg, BaseException):
                record.msg = redact_text(str(record.msg))
            if isinstance(record.args, tuple):
                record.args = tuple(_redact_arg(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: _redact_arg(v) for k, v in record.args.items()}
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact_text(record.exc_text)
            if record.stack_info:
                record.stack_info = redact_text(record.stack_info)
        except Exception:  # redaction must never drop a log line
            pass
        return True


_FILTER = SecretRedactingFilter()
_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")


def install_log_redaction() -> None:
    """Attach the filter to the root and uvicorn loggers and their handlers.

    Handler filters catch records propagated from any child logger; logger
    filters catch records logged directly to a logger whose handler was added
    later. Idempotent, so it is safe to call at import and again at startup.
    """
    for name in _LOGGERS:
        target = logging.getLogger(name)
        if _FILTER not in target.filters:
            target.addFilter(_FILTER)
        for handler in target.handlers:
            if _FILTER not in handler.filters:
                handler.addFilter(_FILTER)
