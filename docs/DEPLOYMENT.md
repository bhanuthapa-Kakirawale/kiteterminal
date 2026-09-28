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

## Checklist before going live on a VM

- [ ] The firewall allows only SSH or VPN inbound, and port 8000 is not publicly reachable. Check from outside, for example with an online port check against the static IP.
- [ ] The startup `Security summary:` shows `workers=1 reload=no` and the expected `bind`/`allowed_hosts`.
- [ ] The Kite redirect URL matches the host the browser uses.
- [ ] `.env` and the DB are readable only by the service user, and the backups are protected.
- [ ] The process runs under a supervisor with restart-on-crash, as a single instance.
