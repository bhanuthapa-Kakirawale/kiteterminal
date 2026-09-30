# Deploying Kite Terminal safely

Kite Terminal is a **single-user application with no login of its own**. The
app does not authenticate anyone who can reach its port, so that person can
read your account and place trades. Every deployment therefore relies on the
network to keep other people out. This page lists the two supported ways to
run it, and what each one requires.

**Unsupported: exposing the terminal to the public internet** (for example
opening port 8000 on a VM's public IP), with or without TLS. That setup would
need real authentication, which the terminal does not have.

## What the app enforces (PK-18)

| Protection | Behaviour |
|---|---|
| Bind address | `python -m backend.server` binds `127.0.0.1` by default. A non-loopback bind (`0.0.0.0`, `::`, a LAN/VM address or a hostname) is **refused** unless `KITE_TERMINAL_ALLOW_REMOTE_BIND=1`. |
| Host header | Every request and WebSocket must carry `Host: localhost`, `127.0.0.1` or `[::1]` (any port), or a name listed in `KITE_TERMINAL_ALLOWED_HOSTS`. Anything else gets `400 INVALID_HOST` before any route runs. This blocks DNS-rebinding pages from *reading* account data. |
| Cross-origin writes | Every POST/PUT/DELETE needs `X-Kite-Terminal: 1` and, if it has an Origin/Referer, a terminal origin (PK-5). This check runs after the Host check. |
| WebSocket | `/ws/ticks` needs an allowed Host and, if an Origin is present, a terminal origin. It carries no credentials in the URL. |
| Proxy headers | `X-Forwarded-Host` and `Forwarded` are never used. uvicorn honours `X-Forwarded-Proto`/`-For` only when `KITE_TERMINAL_TRUSTED_PROXY_IPS` names the proxy. |
| Reload | Off. `--dev-reload` / `KITE_TERMINAL_DEV_RELOAD=1` is for development only and is refused together with a remote bind. |
| Workers | Exactly one. Any other value of `KITE_TERMINAL_WORKERS` is refused. The app also re-checks this at startup, so `uvicorn backend.main:app --workers 2` or `--host 0.0.0.0` without the opt-in **fails to start**. |
| Browser | CSP `script-src 'self'`, `frame-ancestors 'none'` plus `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`, and `Cache-Control: no-store` on `/api/*`. All JavaScript is served locally from `frontend/vendor/` with SRI hashes. |
| Secrets | The Kite access token is stored only Fernet-encrypted. It is never sent to the browser or returned by any API, and it is redacted from logs and error messages, including `request_token` in the access log. |

At startup the log shows one line that you can check:

```
Security summary: bind=127.0.0.1 port=8000 allowed_hosts=127.0.0.1,::1,localhost remote_exposure=no reload=no workers=1 trusted_proxies=none mode=local started_by=launcher
```

The line never contains credentials. `started_by=unverified` means the server
was started some way other than `python -m backend.server`. In that case the
settings were read from the command line on a best-effort basis, so use the
launcher instead.

## Configuration

All settings are environment variables, which can go in `.env`; see
`.env.example`. The defaults are safe for Mode A.

| Variable | Default | Meaning |
|---|---|---|
| `KITE_TERMINAL_HOST` | `127.0.0.1` | Bind address (`--host`). |
| `KITE_TERMINAL_PORT` | `8000` | Port (`--port`). |
| `KITE_TERMINAL_ALLOW_REMOTE_BIND` | `0` | Must be `1` for any non-loopback bind. |
| `KITE_TERMINAL_ALLOWED_HOSTS` | *(empty)* | Extra hostnames or IPs the browser may use, comma-separated. Wildcards are refused. |
| `KITE_TERMINAL_TRUSTED_PROXY_IPS` | *(empty)* | IP addresses or networks of a TLS reverse proxy whose `X-Forwarded-Proto`/`-For` may be trusted. |
| `KITE_TERMINAL_DEV_RELOAD` | `0` | Development only: restart on file changes (`--dev-reload`). |
| `KITE_TERMINAL_WORKERS` | `1` | Must be 1. |
| `KITE_TERMINAL_LOG_FILE` | `logs/kite_terminal.log` (launcher) | PK-22: rotating log file, 10 MB × 11 files at most. `off` disables it (console only). Bare `uvicorn` and the tests log to the console unless it is set. |
| `KITE_TERMINAL_ENCRYPTION_KEY` | generated | Fernet key for the stored secret and token. The app appends a generated key to `.env` if none is set. For a VM, supply it from the service environment instead. A value already in the environment is never overridden by `.env`. |

## Mode A — Local desktop (default)

```
python -m backend.server
```

- The server binds `127.0.0.1` and you use a browser on the same machine at
  `http://127.0.0.1:8000`.
- TLS is not needed, because traffic never leaves the machine.
- The Kite app's redirect URL is `http://127.0.0.1:8000/callback`.

## Mode B — Private cloud VM

Use Mode B when the terminal has to run on a VM, for example to give Kite a
fixed static IP for order placement.

### Static outbound IP is not inbound exposure

Kite's static-IP rule applies to **outbound** API calls from the terminal to
Kite. It does not require anyone to be able to **reach** the terminal from
outside. A VM can have a reserved static external IP for outbound traffic
while its firewall allows no inbound traffic except SSH or a VPN.

```
Browser (your laptop)                                   Kite API
      │  SSH tunnel / VPN (encrypted, authenticated)        ▲
      ▼                                                     │ outbound, from the VM's
 VM: 127.0.0.1:8000  ── Kite Terminal ──────────────────────┘ static IP
 (firewall: inbound SSH/VPN only; port 8000 never opened)
```

### B1 — Recommended: loopback bind with an SSH tunnel or VPN

On the VM, keep the defaults:

```
python -m backend.server          # binds 127.0.0.1:8000 on the VM
```

From your laptop:

```
ssh -N -L 8000:127.0.0.1:8000 you@vm-address
```

Then browse to `http://127.0.0.1:8000`.

- No remote bind or extra allowed host is needed, because the browser sees
  `127.0.0.1` exactly as in Mode A.
- SSH provides the encryption and authentication.
- The Kite redirect URL stays `http://127.0.0.1:8000/callback`, and the login
  round trip completes through the tunnel.

A mesh VPN such as WireGuard or Tailscale works the same way: point the
browser at the tunnel's local end, or bind to the VPN interface as in B2.

### B2 — Explicit remote bind (private network only)

Use B2 only if a tunnel is impractical.

```
KITE_TERMINAL_HOST=10.8.0.2               # the VM's private/VPN interface, not 0.0.0.0
KITE_TERMINAL_ALLOW_REMOTE_BIND=1
KITE_TERMINAL_ALLOWED_HOSTS=10.8.0.2      # the exact name/IP the browser uses
```

Requirements:

- **Private network.** The VM firewall must allow the port only from the VPN
  or private network. Nothing may be reachable from the public internet.
- **Name the interface.** Bind to the specific private interface rather than
  `0.0.0.0`.
- **TLS** whenever browser traffic crosses a network you do not fully control.
  Put a reverse proxy such as Caddy or nginx in front, bind the terminal to
  `127.0.0.1`, and set:
  - `KITE_TERMINAL_TRUSTED_PROXY_IPS=127.0.0.1`, so the proxy's
    `X-Forwarded-Proto: https` is honoured and the browser's `https://` Origin
    matches;
  - `KITE_TERMINAL_ALLOWED_HOSTS=terminal.internal`, the name in the TLS
    certificate.

  The proxy must pass the original `Host` header through unchanged.
- **Warning.** The startup log always warns about a remote bind. That is
  expected, and it is a reminder that the network is the only thing protecting
  the terminal.

## Single-worker requirement

Each server process starts its own copy of five background loops:

- the order monitor
- the trailing engine
- reconciliation
- the autopilot
- the data-job worker

Two processes would place duplicate protective orders and race each other on
the same tickets. Therefore:

- run exactly **one** worker in **one** process;
- run no second copy of the terminal against the same database, not even on
  another port;
- under a supervisor (systemd, NSSM, Task Scheduler), configure restart on
  crash, not multiple instances.

## OAuth (Kite login)

- `/login` and `/callback` are ordinary GET routes. They pass the Host check
  only on an allowed host.
- The redirect URL registered in the Kite developer console must therefore use
  an allowed host, for example `http://127.0.0.1:8000/callback` (Mode A and
  B1) or `https://terminal.internal/callback` (B2 with TLS).
- The `request_token` in the callback URL is redacted from the access log.
- `Referrer-Policy: same-origin` keeps it from being sent to any other site.
- The callback has no `state` parameter yet (login CSRF, low impact locally).
  This is deferred.

## Files, permissions and backups

| File | Holds | Protect by |
|---|---|---|
| `.env` | `KITE_API_SECRET`, possibly the encryption key | git-ignored. On Linux, `chmod 600`, owned by the service user. On Windows, keep it in a user-only folder. On a VM, prefer the service's environment over this file. |
| `backend/kite_terminal.db` | trade history, the encrypted secret and token | git-ignored. Keep it on an encrypted disk, readable only by the service user. |
| `backend/kite_terminal.db.*_bak` | copies of the DB | Same as the DB. **Backups taken before PK-18 contain plaintext session tokens.** Those tokens expire daily, but treat the files as secret. |

Anyone who can read both the `.env` and the database can decrypt the stored
secret. Keeping the encryption key in the service environment, outside the
application directory, separates the two.

## Operations (PK-22)

### Health, readiness and diagnostics

| Endpoint | Meaning |
|---|---|
| `GET /api/health` | Liveness: the process serves requests and the database answers. Always 200 while up. |
| `GET /api/ready` | Readiness to trade: 200 when ready, **503** with `blockers` otherwise. The market does not have to be open. Blockers: schema not current or unsupported, database integrity failure, a critical worker (order monitor, trailing, reconciliation) not running, no valid Kite session for today, trading lock, open circuit breaker. |
| `GET /api/diagnostics` | Everything the **System Diagnostics** page shows. Read-only; no broker call; no credential. |
| `GET /api/live-readiness` | LIVE-0 **Live Readiness** pre-flight: `READY`/`NOT_READY` for a live new entry, with every check (`PASS`/`FAIL`/`WARN`/`USER_ACTION`, blocking or not). Adds to `/api/ready`: worker staleness, max order notional and ₹ daily loss set, static IP confirmed, no UNKNOWN live order, no critical reconciliation finding, strategy history current. `?broker=true` adds one read-only Kite snapshot (profile, holdings, positions, margins, orders, GTTs, one LTP). Never writes, switches mode or unlocks. |

All three answer only on an allowed Host, like every other route. A supervisor
or uptime check can poll `/api/health`; check `/api/ready` before trading.

### Logs

The launcher writes `logs/kite_terminal.log` (git-ignored) and rotates it at
10 MB, keeping 10 older files, so disk use is bounded at about 110 MB. The
PK-18 redaction filter applies to the file exactly as to the console. Critical
flows write one structured line each (`event=order.submitting ticket_id=…
order_tag=… cid=…`); `cid` is the correlation id of the HTTP request (echoed
in the `X-Request-ID` response header) or of the background cycle (`om-…`,
`tr-…`, `rc-…`). To trace one approval: grep its `cid`, then its `ticket_id`
and `order_id` for what the order monitor did with it later.

### Database migrations and backups

The schema is versioned (`schema_version` table). On start, the app applies
only migrations it has not applied before, each in its own transaction, and
first copies the database to `kite_terminal.db.pre_schema_v<N>_bak` (checked
with `integrity_check`). A database written by a **newer** version is refused
with `DATABASE_SCHEMA_UNSUPPORTED` and nothing is changed: run the newer code,
or restore the matching backup.

Maintenance (stop the app first for `vacuum`):

```
python -m backend.db_maintenance status      # schema version, pending migrations, size
python -m backend.db_maintenance integrity   # PRAGMA integrity_check + foreign_key_check
python -m backend.db_maintenance backup      # online backup to kite_terminal.db.manual_<time>_bak
python -m backend.db_maintenance analyze     # refresh query-planner statistics
python -m backend.db_maintenance vacuum      # compact the file (app stopped!)
python -m backend.db_maintenance migrate     # apply pending migrations without starting the server (app stopped!)
```

`migrate` (LIVE-0) runs exactly the application's own initialisation — the
verified pre-migration backup, the migrations, the startup invariants — with
no server, workers or ticker, so a migration can be done and checked
deliberately. It loads `.env` first and refuses to run without
`KITE_TERMINAL_ENCRYPTION_KEY`, because a newly generated key would make the
stored Kite token unreadable. Run it twice: the second run must report
`applied migrations: none` and `startup row changes: 0`.

Nothing runs `VACUUM` automatically; `PRAGMA optimize` runs only after a
migration was applied.

### NSE holiday calendar — yearly update

`backend/calendar_data/nse_equity_holidays.json` lists NSE's weekday trading
holidays per year from the official "Trading holidays for the calendar year"
circular (published each December) and its amendments. Each December:

1. Add the new year with its circular reference, from nseindia.com.
2. Add any mid-year amendment circular (for example an election holiday).
3. Validate, and cross-check against stored candles on a **copy** of the DB:
   `python -m backend.trading_calendar check --verify-db <copy.db>`.

If a year is missing, weekdays are assumed to trade (as before PK-22) and
readiness reports the warning `CALENDAR_YEAR_UNCOVERED`. Special sessions
(Muhurat trading, a weekend budget session) are listed but their hours are not
modelled: the app treats the market as closed during them.

### Historical price data

A symbol whose stored history Kite has since back-adjusted (a split or bonus)
is flagged `NEEDS_REFRESH`; an unexplained gap of more than 25% is `SUSPECT`.
Both are excluded from backtests with the reason, and `NEEDS_REFRESH` symbols
are skipped by the live scan. Nothing is rewritten automatically: rebuild a
flagged symbol from **System Diagnostics → Historical price data → Rebuild
history** (needs a Kite session), which re-downloads its whole history and
replaces it in one transaction.

## Checklist before going live on a VM

- [ ] The firewall allows only SSH or VPN inbound, and port 8000 is not publicly reachable. Check from outside, for example with an online port check against the static IP.
- [ ] The startup `Security summary:` shows `workers=1 reload=no` and the expected `bind`/`allowed_hosts`.
- [ ] The Kite redirect URL matches the host the browser uses.
- [ ] `.env` and the DB are readable only by the service user, and the backups are protected.
- [ ] The process runs under a supervisor with restart-on-crash, as a single instance.
- [ ] `GET /api/ready` returns 200 (after logging in to Kite), and System Diagnostics shows no blockers.
- [ ] **Live Readiness** (with *Run broker check*) shows nothing blocking except, deliberately, the trading lock; the static IP there is the one registered for the Kite app. Then follow [FIRST_LIVE_TRADE.md](FIRST_LIVE_TRADE.md).
- [ ] `logs/` is on a disk with room for ~110 MB, and readable only by the service user.
