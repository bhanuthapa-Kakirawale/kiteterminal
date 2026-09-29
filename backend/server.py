"""Supported way to start the terminal (PK-18): ``python -m backend.server``.

Safe defaults, and every widening of exposure is an explicit opt-in:

- **Bind** ``127.0.0.1``. A non-loopback bind (``0.0.0.0``, ``::``, a LAN or
  VM address, a hostname) is refused unless
  ``KITE_TERMINAL_ALLOW_REMOTE_BIND=1``. The app has no authentication, so a
  remote bind is only safe behind a firewall/VPN/TLS proxy (docs/DEPLOYMENT.md).
- **Reload** off. ``--dev-reload`` / ``KITE_TERMINAL_DEV_RELOAD=1`` turns it
  on for development only; reload restarts the process on a file save, which
  can land between an order placement and its protective GTT.
- **Workers** exactly 1. Every worker would start its own order monitor,
  trailing engine, reconciliation, automation and data-job loops against the
  same database, so ``KITE_TERMINAL_WORKERS`` other than 1 is refused.
- **Proxy headers** ignored unless ``KITE_TERMINAL_TRUSTED_PROXY_IPS`` lists
  the reverse proxy's address. (uvicorn's own default trusts 127.0.0.1.)

The same rules are re-checked inside the app at startup
(``enforce_runtime_policy``), so ``uvicorn backend.main:app --host 0.0.0.0``
or ``--workers 2`` fails to start even without this launcher. That check
reads the values the launcher exported, or, for a bare uvicorn start,
parses the command line best-effort.
"""

import argparse
import ipaddress
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping, Sequence

logger = logging.getLogger(__name__)

ENV_HOST = "KITE_TERMINAL_HOST"
ENV_PORT = "KITE_TERMINAL_PORT"
ENV_ALLOW_REMOTE = "KITE_TERMINAL_ALLOW_REMOTE_BIND"
ENV_DEV_RELOAD = "KITE_TERMINAL_DEV_RELOAD"
ENV_WORKERS = "KITE_TERMINAL_WORKERS"
ENV_TRUSTED_PROXY_IPS = "KITE_TERMINAL_TRUSTED_PROXY_IPS"
# Exported by the launcher so the app process (and a reload child) can report
# and re-check exactly what it was started with.
RUNTIME_PREFIX = "KITE_TERMINAL_RUNTIME_"
DEFAULT_LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "kite_terminal.log"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_RELOAD = False
REQUIRED_WORKERS = 1

_TRUE = frozenset({"1", "true", "yes", "on"})


class RuntimeConfigError(ValueError):
    """The requested runtime would expose the terminal unsafely."""


@dataclass(frozen=True)
class RuntimeConfig:
    host: str
    port: int
    reload: bool
    workers: int
    allow_remote: bool
    trusted_proxy_ips: tuple[str, ...]
    source: str = "launcher"

    @property
    def remote_bind(self) -> bool:
        return not is_loopback_host(self.host)


def is_loopback_host(host: str) -> bool:
    name = host.strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False  # 0.0.0.0/:: parse but are not loopback; any other name is remote


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE


def _parse_port(value) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise RuntimeConfigError(f"port must be an integer, got {value!r}")
    if not 1 <= port <= 65535:
        raise RuntimeConfigError(f"port must be 1-65535, got {port}")
    return port


def _parse_workers(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RuntimeConfigError(f"worker count must be an integer, got {value!r}")


def _parse_trusted_proxies(raw: str | None) -> tuple[str, ...]:
    entries = tuple(part.strip() for part in (raw or "").split(",") if part.strip())
    for entry in entries:
        if "*" in entry:
            raise RuntimeConfigError(f"{ENV_TRUSTED_PROXY_IPS} must list proxy addresses, not {entry!r}")
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            raise RuntimeConfigError(f"{ENV_TRUSTED_PROXY_IPS} entry {entry!r} is not an IP address or network")
    return entries


def validate(config: RuntimeConfig) -> RuntimeConfig:
    """Raise RuntimeConfigError for a combination that must not start."""
    if config.workers != REQUIRED_WORKERS:
        raise RuntimeConfigError(
            f"workers={config.workers}: exactly one application worker is supported. Each worker runs its "
            "own order monitor, trailing engine and reconciliation loop against the same account.")
    if config.remote_bind and not config.allow_remote:
        raise RuntimeConfigError(
            f"bind address {config.host!r} is not loopback. The terminal has no login of its own; "
            f"set {ENV_ALLOW_REMOTE}=1 only behind a firewall, VPN/SSH tunnel or TLS proxy "
            "(see docs/DEPLOYMENT.md).")
    if config.remote_bind and config.reload:
        raise RuntimeConfigError("auto-reload is a development feature and cannot be combined with a remote bind")
    return config


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m backend.server", description="Start the Kite Terminal.")
    p.add_argument("--host", help=f"bind address (default {DEFAULT_HOST}; env {ENV_HOST})")
    p.add_argument("--port", help=f"port (default {DEFAULT_PORT}; env {ENV_PORT})")
    p.add_argument("--dev-reload", action="store_true",
                   help=f"development only: restart on file changes (env {ENV_DEV_RELOAD})")
    return p


def resolve_runtime(env: Mapping[str, str], argv: Sequence[str]) -> RuntimeConfig:
    """The launcher's runtime from its command line and environment."""
    args = _parser().parse_args(list(argv))
    config = RuntimeConfig(
        host=(args.host or env.get(ENV_HOST) or DEFAULT_HOST).strip(),
        port=_parse_port(args.port or env.get(ENV_PORT) or DEFAULT_PORT),
        reload=bool(args.dev_reload) or _truthy(env.get(ENV_DEV_RELOAD)) or DEFAULT_RELOAD,
        workers=_parse_workers(env.get(ENV_WORKERS) or REQUIRED_WORKERS),
        allow_remote=_truthy(env.get(ENV_ALLOW_REMOTE)),
        trusted_proxy_ips=_parse_trusted_proxies(env.get(ENV_TRUSTED_PROXY_IPS)),
    )
    return validate(config)


def export_runtime(config: RuntimeConfig, env: MutableMapping[str, str]) -> None:
    env[RUNTIME_PREFIX + "HOST"] = config.host
    env[RUNTIME_PREFIX + "PORT"] = str(config.port)
    env[RUNTIME_PREFIX + "RELOAD"] = "1" if config.reload else "0"
    env[RUNTIME_PREFIX + "WORKERS"] = str(config.workers)
    env[RUNTIME_PREFIX + "TRUSTED_PROXY_IPS"] = ",".join(config.trusted_proxy_ips)


def _cli_option(argv: Sequence[str], name: str) -> str | None:
    """Value of ``--name X`` or ``--name=X`` in a uvicorn command line."""
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def detect_runtime(env: Mapping[str, str], argv: Sequence[str]) -> RuntimeConfig:
    """What this process was started with: the launcher's exported values, or
    a best-effort reading of a bare ``uvicorn backend.main:app ...`` command
    line (uvicorn's defaults: 127.0.0.1, port 8000, one worker, no reload).
    A multiprocessing-spawned worker or reload child inherits the parent's
    argv, so it sees the same flags."""
    allow_remote = _truthy(env.get(ENV_ALLOW_REMOTE))
    if RUNTIME_PREFIX + "HOST" in env:
        return RuntimeConfig(
            host=env[RUNTIME_PREFIX + "HOST"],
            port=_parse_port(env.get(RUNTIME_PREFIX + "PORT", DEFAULT_PORT)),
            reload=env.get(RUNTIME_PREFIX + "RELOAD") == "1",
            workers=_parse_workers(env.get(RUNTIME_PREFIX + "WORKERS", REQUIRED_WORKERS)),
            allow_remote=allow_remote,
            trusted_proxy_ips=_parse_trusted_proxies(env.get(RUNTIME_PREFIX + "TRUSTED_PROXY_IPS")),
            source="launcher",
        )
    workers = _cli_option(argv, "--workers") or env.get("WEB_CONCURRENCY") or REQUIRED_WORKERS
    return RuntimeConfig(
        host=_cli_option(argv, "--host") or DEFAULT_HOST,
        port=_parse_port(_cli_option(argv, "--port") or DEFAULT_PORT),
        reload="--reload" in argv,
        workers=_parse_workers(workers),
        allow_remote=allow_remote,
        trusted_proxy_ips=(),
        source="unverified",
    )


def security_summary(config: RuntimeConfig, allowed_hosts) -> str:
    mode = "private-remote" if config.remote_bind else "local"
    return (
        f"Security summary: bind={config.host} port={config.port} "
        f"allowed_hosts={','.join(sorted(allowed_hosts))} "
        f"remote_exposure={'yes' if config.remote_bind else 'no'} "
        f"reload={'yes' if config.reload else 'no'} workers={config.workers} "
        f"trusted_proxies={','.join(config.trusted_proxy_ips) or 'none'} "
        f"mode={mode} started_by={config.source}"
    )


def runtime_warnings(config: RuntimeConfig, allowed_hosts) -> list[str]:
    warnings = []
    if config.source != "launcher":
        warnings.append("Started outside `python -m backend.server`: bind, reload and worker settings were read "
                        "best-effort from the command line. Use the launcher for enforced safe defaults.")
    if config.reload:
        warnings.append("Auto-reload is ON. It is a development feature: a file save restarts the server, "
                        "possibly between an order and its protective GTT. Never trade live with reload on.")
    if config.remote_bind:
        warnings.append(f"REMOTE BIND on {config.host}: the terminal is reachable beyond this machine. It has NO "
                        "login, and provides no TLS. Keep it behind a firewall and a VPN/SSH tunnel or TLS "
                        "reverse proxy; never expose it to the public internet.")
        if not (set(allowed_hosts) - {"127.0.0.1", "localhost", "::1"}):
            warnings.append("Remote bind with only loopback allowed hosts: remote browsers will get INVALID_HOST. "
                            "Set KITE_TERMINAL_ALLOWED_HOSTS to the name the browser uses.")
    return warnings


def enforce_runtime_policy(env: Mapping[str, str] | None = None, argv: Sequence[str] | None = None,
                           allowed_hosts=None) -> RuntimeConfig:
    """Called first at app startup: refuse an unsafe runtime, log the summary."""
    from backend.security import allowed_hostnames

    env = os.environ if env is None else env
    argv = sys.argv if argv is None else argv
    allowed = allowed_hostnames() if allowed_hosts is None else allowed_hosts
    config = detect_runtime(env, argv)
    try:
        validate(config)
    except RuntimeConfigError as e:
        logger.critical("Refusing to start: %s", e)
        raise
    logger.info(security_summary(config, allowed))
    for warning in runtime_warnings(config, allowed):
        logger.warning(warning)
    return config


def main(argv: Sequence[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    try:
        config = resolve_runtime(os.environ, sys.argv[1:] if argv is None else argv)
    except RuntimeConfigError as e:
        print(f"Refusing to start: {e}", file=sys.stderr)
        return 2
    export_runtime(config, os.environ)
    # PK-22E: the launcher logs to a bounded, rotating file unless told
    # otherwise (KITE_TERMINAL_LOG_FILE=off, or another path).
    os.environ.setdefault("KITE_TERMINAL_LOG_FILE", str(DEFAULT_LOG_FILE))

    import uvicorn

    options = {
        "host": config.host, "port": config.port, "reload": config.reload, "workers": 1,
        "proxy_headers": bool(config.trusted_proxy_ips), "server_header": False,
    }
    if config.trusted_proxy_ips:
        options["forwarded_allow_ips"] = ",".join(config.trusted_proxy_ips)
    uvicorn.run("backend.main:app", **options)
    return 0


if __name__ == "__main__":
    sys.exit(main())
