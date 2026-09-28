# OPUS55 PK-18 Implementation Report: local and private deployment security hardening

| | |
|---|---|
| Packet | **PK-18** only (audit **P1-12** plus the PK-5 report's deferred items §17/§18). PK-19 and later were not started. |
| Baseline before | **809 passed**, 0 failed, 0 xfailed, 0 xpassed; the tree was clean at `44688df`. |
| Result after | **986 passed, 0 failed, 0 xfailed, 0 xpassed** on every one of three consecutive full runs. That is 809 + 177 new tests in `tests/test_security_hardening.py`. One PK-5 parametrised case moved between files (see *PK-5 Interaction*). |
| Backups taken first | Tag `backup-pre-pk18` and branch `backup/pre-pk18`, both at `44688df`. `backend/kite_terminal.db.pre_pk18_bak` was taken with the SQLite online-backup API (`integrity_check` = ok). The live DB's SHA-256 was recorded before the first edit. |
| Live DB | **Not touched.** Its SHA-256 is identical before and after all the work (`1378f8b4…dacf`). The one data migration was validated on a copy (see *Database Validation*). It will run on the live DB the next time the app is started. |
| Committed | Yes, after every validation gate was clean. It is one commit, `fix: harden local and private deployment security`, on `feature/signal-detail-and-strategy-revision`, directly after `44688df`. PK-19 was not started. |

# Executive Summary

PK-5 made cross-origin **writes** impossible. PK-18 closes the rest of the local and private deployment surface without inventing an authentication system.

1. **Host validation (DNS rebinding) protects reads as well as writes.**
   - A new outermost-but-one ASGI middleware, `TrustedHostGuard`, refuses every HTTP request and WebSocket handshake whose `Host` is missing, malformed, duplicated or not an allowed name. It answers **400 `INVALID_HOST`** (or closes the WebSocket with 1008) before any route, broker call or DB access.
   - The allowed names are `localhost`, `127.0.0.1` and `::1`, plus `KITE_TERMINAL_ALLOWED_HOSTS`.
   - Forwarded headers are never read.
   - Before PK-18, a rebinding page could `GET /api/holdings`, tickets, the journal and settings. It now gets 400. This was proven in a real Edge browser, with `evil.example` mapped to 127.0.0.1.
2. **Safe runtime by construction.**
   - There is a new supported launcher, `python -m backend.server`. By default it binds loopback, turns reload off, runs one worker, ignores proxy headers and sends no `Server` header.
   - A non-loopback bind needs `KITE_TERMINAL_ALLOW_REMOTE_BIND=1`.
   - Any worker count other than 1, and reload combined with a remote bind, are refused.
   - The same policy is **re-checked inside the app at startup**. `uvicorn backend.main:app --host 0.0.0.0` or `--workers 2` therefore aborts before `init_db` or any background loop runs.
   - Startup logs a one-line security summary that contains no secrets.
3. **Secrets.**
   - The Kite access token is now stored **only encrypted**. `get_kite()` reads `accounts.access_token_enc`, and new logins write `''` to `sessions.access_token`.
   - An idempotent migration backfills the encrypted copy if it is missing, then blanks the old plaintext column. Nobody is logged out.
   - A reusable redactor (`backend/redaction.py`) is installed on the root and uvicorn loggers. It covers messages, arguments and tracebacks, including uvicorn's `/callback?request_token=` access line.
   - Every `HTTPException` detail is redacted before it reaches the browser.
   - A route-table crawl proves that no GET endpoint returns any credential.
   - `.env.example` has been added, and `.gitignore` is broadened.
4. **Frontend integrity.**
   - Both CDN libraries are vendored at exact versions. Each was verified against its npm registry `dist.integrity` and is byte-identical to the CDN copy used before.
   - They are served from `/static/vendor/` with SRI.
   - The inline theme script became a file.
   - The page now runs under `script-src 'self'`, with no inline or external script.
5. **Headers.**
   - CSP, including `frame-ancestors 'none'`.
   - `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`, `Permissions-Policy`, COOP and CORP.
   - `Cache-Control: no-store` on `/api/*`, `/login` and `/callback`. The UI page gets `no-cache`, and static assets keep their ETag caching.
   - Swagger and ReDoc are disabled, because they load CDN JavaScript.
6. **Documentation.**
   - The new `docs/DEPLOYMENT.md` defines Mode A (local) and Mode B (private VM: loopback bind plus an SSH tunnel or VPN, or an explicit private bind with a TLS proxy).
   - It separates the static *outbound* IP from *inbound* exposure.
   - It states that public exposure is unsupported, because there is no application authentication.

No trading, risk, execution, exit or reconciliation logic changed.

# Threat Model

| Threat | Attacker | Before PK-18 | After PK-18 |
|---|---|---|---|
| DNS rebinding **read** | A web page whose hostname resolves to 127.0.0.1 | Could read holdings, positions, tickets, journal, settings, GTT ids | 400 `INVALID_HOST` for every HTTP path and WebSocket |
| DNS rebinding **write** | Same | Blocked by PK-5 (loopback Origin allow-list) | Blocked twice: Host first, then Origin |
| Hostile / injected Host | A client or proxy sending an odd `Host` | Accepted | Strict parser rejects userinfo, paths, whitespace, CR/LF, non-ASCII, a trailing dot, bad ports and duplicates |
| Forwarded-header spoofing | A client sending `X-Forwarded-Host` or `Forwarded` | Not used (no Host policy existed) | Never read. `X-Forwarded-Proto`/`-For` are honoured only when the launcher is given `KITE_TERMINAL_TRUSTED_PROXY_IPS` |
| Accidental LAN or public exposure | The operator (`--host 0.0.0.0`) | Allowed silently | Refused without an explicit opt-in, by both the launcher and in-app startup. A remote bind always logs a warning |
| Duplicate background workers | The operator (`--workers N`, `WEB_CONCURRENCY`) | N copies of the order monitor, trailing, reconciliation and autopilot | Refused at startup |
| Reload during live trading | The operator (`--reload` in the USER_GUIDE) | Recommended by the docs | Off by default; `--dev-reload` only; refused with a remote bind; warned on bare `uvicorn --reload` |
| Token theft from DB or backups | Anyone who reads the DB file | Plaintext `sessions.access_token` | Plaintext removed. Only Fernet ciphertext remains; the key is in the environment or `.env`. Pre-PK-18 backups are documented as sensitive |
| Token leakage through logs | Log reader | `request_token` in the access log; no redaction | Redacted by name and by value, in messages, arguments and tracebacks |
| Credential in an API response or error | Browser or page script | None found (audit), but not guarded | A crawl test covers every GET route; `HTTPException` details are redacted; a 500 gives a bare "Internal Server Error" |
| Compromised CDN script | CDN or supply chain | Two unpinned-SRI CDN scripts ran with full same-origin power | Vendored, SRI-checked, and CSP `script-src 'self'` |
| Clickjacking | A hostile site framing the terminal | Possible | `frame-ancestors 'none'` plus `X-Frame-Options: DENY`; proven in the browser |
| Cached account data | Shared or intermediary caches, browser disk | Default caching | `no-store` on every `/api/*` and the auth redirects |

**Out of scope, deliberately.** Malware on the same host, which can read the DB and `.env`. An operator exposing the port publicly despite the refusal and warnings. Login CSRF on `/callback`. These are listed under *Deferred Security Work*.

# PK-18A Host / DNS Rebinding Protection

- **Where.** The check lives in `backend/security.py`: `parse_host`, `allowed_hostnames`, `host_rejection_reason` and `TrustedHostGuard`. It is registered in `backend/main.py`.
- **Order, outermost first.** `SecurityHeadersMiddleware`, then `TrustedHostGuard`, then `TerminalOriginGuard` (PK-5), then the router. The Host check therefore runs before the Origin and write policy, and before routing.
- **Parser.**
  - `hostname[:port]` or `[ipv6][:port]`, ASCII only.
  - Labels are letters, digits and hyphens: no empty label, no leading or trailing hyphen, no trailing dot, at most 253 characters.
  - The port must be 1–65535.
  - IPv6 literals are normalised, so `[0:0:0:0:0:0:0:1]` is treated as `::1`.
  - Zero Host headers, or more than one, are rejected.
- **Allowed hostnames.**
  - `localhost`, `127.0.0.1` and `::1` are always allowed. `KITE_TERMINAL_ALLOWED_HOSTS` adds more: hostnames, bare IPs or bracketed IPv6.
  - Wildcards and invalid entries are ignored with a warning.
  - The setting is read on every request, cached on the raw string.
  - **Any port** is accepted for an allowed name. Rebinding is defeated by the *name*: the attacker controls the hostname, not which port the server listens on. The Origin check still ties a write's Origin port to the Host port.
- **Forwarded headers.** Neither guard reads `X-Forwarded-Host`, `Forwarded` or `X-Forwarded-Proto`. The CSP echoes only the validated Host header.
- **Origin policy.** `is_terminal_origin` now uses the same allowed set instead of the fixed loopback set. With the default configuration the two sets are identical, so PK-5 behaviour is unchanged. With `KITE_TERMINAL_ALLOWED_HOSTS=terminal.lan`, writes from `http://terminal.lan:PORT` work (tested).
- **Rejection.** HTTP gets `400 {"detail":{"error_code":"INVALID_HOST",…}}`, carrying the security headers and `no-store`. A WebSocket gets `websocket.close(1008)` before accept, which the server turns into HTTP 403. The warning log line uses `repr()` of the value, truncated to 100 characters, so it cannot be used for log injection.
- **OAuth.** `/login` and `/callback` are checked like every other path. They work on an approved Host and give 400 on a hostile one (both tested). The Kite redirect URL must use an allowed host; see *OAuth Interaction*.

# PK-18B Runtime Bind Safety

**`backend/server.py` (new)** holds the launcher and the in-app policy.

- **`resolve_runtime(env, argv)`** is pure and fully tested. It reads the following settings, with `--host`, `--port` and `--dev-reload` overriding the environment:

  | Setting | Default |
  |---|---|
  | `KITE_TERMINAL_HOST` | `127.0.0.1` |
  | `KITE_TERMINAL_PORT` | 8000 |
  | `KITE_TERMINAL_ALLOW_REMOTE_BIND` | 0 |
  | `KITE_TERMINAL_DEV_RELOAD` | 0 |
  | `KITE_TERMINAL_WORKERS` | 1 |
  | `KITE_TERMINAL_TRUSTED_PROXY_IPS` | empty |

- **`validate()`** refuses:
  - a worker count other than 1;
  - a non-loopback bind without the opt-in. Loopback means `localhost` or `ipaddress.is_loopback`, so `0.0.0.0`, `::`, a LAN or VM IP and any other hostname count as remote;
  - reload combined with a remote bind;
  - a bad port, and a wildcard or non-IP proxy entry.
- **`main()`**
  - calls `load_dotenv()` and resolves the runtime; an unsafe one gets "Refusing to start: …" and exit code 2;
  - exports the result as `KITE_TERMINAL_RUNTIME_*`;
  - calls `uvicorn.run("backend.main:app", host, port, reload, workers=1, proxy_headers=<only with trusted IPs>, server_header=False)`.
- **`enforce_runtime_policy()`** is the first statement of `on_startup`, even before `init_db()`.
  - It reads the launcher's exported values. If there are none, it reads the command line on a best-effort basis: `--host`, `--port`, `--workers`, `WEB_CONCURRENCY` and `--reload`. A multiprocessing-spawned worker inherits argv, so it sees the same flags.
  - An unsafe runtime raises, and uvicorn prints "Application startup failed" and exits. This was proven for real with `uvicorn backend.main:app --host 0.0.0.0`: exit code 3, and the scratch DB was never created.
  - It logs the summary line and any warnings: a start without the launcher, reload being on, and a remote bind. The remote-bind warning adds another if only loopback hosts are allowed, since remote browsers would then get `INVALID_HOST`.
- **Reload.** Off by default. The USER_GUIDE no longer recommends `--reload`.
- **Workers.** Refused unless exactly 1. No distributed lock was built, as instructed. A second *separate* instance against the same DB is documented as unsupported but is not detected.

# PK-18C Secrets / Token Handling

**Inventory.** Every use of `api_key`, `api_secret`, `access_token`, `request_token`, `public_token`, `KITE_API_*` and the encryption key was traced in `backend/`, `frontend/`, `tests/`, `docs/`, `.gitignore` and the start instructions.

| Secret | Where it was | Where it is now |
|---|---|---|
| API key | `.env`, `accounts.api_key` | Unchanged. It is a public identifier. The frontend never receives it. |
| API secret | `.env` (seed only), `accounts.api_secret_enc` (Fernet) | Unchanged at rest. It is registered with the redactor whenever it is decrypted, and is also redacted by value from `KITE_API_SECRET`. |
| Access token | `sessions.access_token` **plaintext** (read by `get_kite`), plus `accounts.access_token_enc` (unused) | **Only** `accounts.access_token_enc`. `get_kite()` decrypts it; if it is missing or cannot be decrypted, the result is `NoActiveSessionError` and a log line without the value. The session row stores `''`. Logout still sets the encrypted copy to NULL. Every token obtained is registered with the redactor. |
| Request token | Callback query string; uvicorn access log | Redacted in logs by name. `Referrer-Policy: same-origin`. It is registered with the redactor during the exchange. |
| Public token | `sessions.public_token` | Unchanged; it is a public value. It is redacted by name in logs. |
| Encryption key | `.env` (auto-generated) or the environment | Unchanged; supplying it from the environment is documented. It is redacted by value. |

**Migration `_scrub_plaintext_session_tokens`** is in `backend/db.py` and is called from `init_db`. It is data only, with **no schema change**.

1. For each account whose `access_token_enc` IS NULL, it encrypts the token of the newest active session that has a non-empty token into `access_token_enc`, so that a user who logged in today stays logged in.
2. It runs `UPDATE sessions SET access_token = '' WHERE access_token != ''`.

A second run finds nothing to do.

**Redaction (`backend/redaction.py`).**

- **By name.** Handles the `key=value`, `key: value` and `"key": "value"` forms for `access_token`, `request_token`, `refresh_token`, `public_token`, `enctoken`, `api_secret`, `api_secret_enc`, `access_token_enc`, `password`, `checksum`, `kite_api_secret` and `kite_terminal_encryption_key`. Names match on word boundaries only, so `instrument_token` is untouched (tested).
- **Header values.** Covers `Authorization`, `Cookie` and `Set-Cookie`, and Kite's `token key:secret` scheme.
- **By value.** Replaces any registered secret (up to 64, with a minimum length of 8) and the current `KITE_API_SECRET` and encryption key.
- **Idempotent.** A record passes through both a logger filter and a handler filter. The real-server smoke test found `request_token=[REDACTED]]` on a double pass. This was fixed with a lookahead, and a regression test was added.
- **`SecretRedactingFilter`.** Redacts `msg`, each argument separately, exception arguments, `exc_text` and `stack_info`. Arguments are redacted one by one, not collapsed, because uvicorn's `AccessFormatter` unpacks `args` positionally.
- **`install_log_redaction()`.** Attaches the filter to the root, `uvicorn`, `uvicorn.error` and `uvicorn.access` loggers and all their handlers. It runs at `main.py` import (uvicorn configures logging before importing the app) and again at startup.
- **API errors.** `main.py` registers a Starlette `HTTPException` handler that redacts every string in `detail`, then delegates to FastAPI's default handler. This covers the roughly 20 places where a broker exception's text is quoted in an error. Unhandled exceptions become Starlette's bare `Internal Server Error`, with no traceback or path (tested, `app.debug` is False); the traceback goes only to the redacted log.

**Repository hygiene.**

- `.gitignore` now also covers `.env.*` (except `!.env.example`), `*.pem`, `*.key`, `secrets/`, every `*.db`/`-wal`/`-shm`/`-journal`, `*.sqlite*`, `*.bak`, `*_bak`, `captures/` and `*.har`.
- `.env.example` has been added with placeholders only.
- Before the change, the only tracked non-source files were `.gitignore`, `pytest.ini` and the requirements files, so nothing tracked needed to be removed.
- A test scans every tracked and un-ignored file for Fernet-key-shaped values, non-placeholder `KITE_API_SECRET=` or encryption-key assignments, and 32-character `access_token` literals.

# PK-18D Frontend Dependency Integrity

Before PK-18, `index.html` loaded two scripts from CDNs, with exact versions but **no SRI**:

- cdnjs Sortable 1.15.2;
- jsDelivr lightweight-charts 5.2.1.

It also had one inline theme script.

**Now:**

- **Vendored libraries.** `frontend/vendor/Sortable-1.15.2.min.js` and `frontend/vendor/lightweight-charts-5.2.1.standalone.production.js`, with their licence files and a `vendor/README.md` recording provenance, SHA-256, SHA-384 and the upgrade steps.
  - Each file was extracted from its npm registry tarball after the tarball was checked against the registry's `dist.integrity` (sha512).
  - Both files are **byte-identical** to the CDN copies previously used.
  - Neither package ships a NOTICE file.
- **SRI.** `index.html` loads them from `/static/vendor/…` with `integrity="sha384-…"`, so a tampered local file is refused too. A test recomputes the hashes.
- **Theme script.** The inline script moved to `frontend/theme-init.js`, loaded synchronously in `<head>`, so the theme still applies before first paint.
- **Chart fallback.** The `app.js` fallback text no longer mentions a CDN.
- **CSP.** The policy is `script-src 'self'`, with no `unsafe-inline` or `unsafe-eval` for script. `style-src` keeps `'unsafe-inline'`, because `app.js` renders `style=""` attributes through `innerHTML`; removing that would mean a broad frontend rewrite, so it is a documented limitation. No inline event handlers or `javascript:` URLs exist (tested).

# PK-18E WebSocket Security

`/ws/ticks` is guarded by both middlewares before `accept()`:

- **Host** must be allowed.
- **Origin**, if present, must be a terminal origin.
- **No Origin with an approved Host** is accepted. This is the documented policy for local non-browser clients, since browsers always send Origin; the stream is read-only market data.
- **No credentials** are required or accepted in the URL. `app.js` builds `${protocol}//${location.host}/ws/ticks` with no query string, and a test enforces it.
- **Hostile Host.** Refused (1008, or HTTP 403 on the wire) with no Origin, with a *loopback* Origin (which the PK-5 check alone would accept), with a rebinding Origin, and with `X-Forwarded-Host: 127.0.0.1`.
- **Browser proof.** In the real browser, the terminal's socket connects, and a rebinding page's socket is refused.

# PK-18F Security Headers / Error Handling

`SecurityHeadersMiddleware` is pure ASGI and wraps `send`. It adds these headers to every HTTP response, including the 400 and 403 rejections:

| Header | Value |
|---|---|
| `Content-Security-Policy` | `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self' ws://HOST wss://HOST; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'` (HOST only if validated) |
| `X-Frame-Options` | `DENY` |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `same-origin` |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=(), payment=(), usb=()` |
| `Cross-Origin-Opener-Policy` / `-Resource-Policy` | `same-origin` |
| `Cache-Control` | `no-store` on `/api/*`, `/login`, `/callback`; `no-cache` on `/`; unchanged on `/static/*` (ETag caching kept, tested) |

- **Why the WebSocket origin is listed explicitly.** `connect-src` names the WebSocket origin because older browsers do not treat `ws:` as covered by `'self'`.
- **What is not sent.** HSTS is not sent, since the app speaks plain HTTP on loopback; a TLS proxy should add it.
- **Server header.** The launcher suppresses uvicorn's `Server` header.
- **Browser check.** Zero CSP violations were recorded across the full UI (see *Browser Validation*).
- **Swagger.** FastAPI's Swagger UI and ReDoc are disabled (`docs_url=None, redoc_url=None`), because they pull JavaScript from a CDN, which the CSP forbids. `/openapi.json` is still served; it is same-origin JSON only.
- **Errors.** See PK-18C. The terminal returns error codes and messages, redacted. It never returns tracebacks.

# PK-18G Deployment Modes

`docs/DEPLOYMENT.md` (new) documents:

- the enforced protections and the configuration table;
- the startup summary line;
- **Mode A**, local desktop;
- **Mode B1**, a VM with a loopback bind, reached over an SSH tunnel or VPN (recommended);
- **Mode B2**, an explicit private-interface bind, with a TLS reverse proxy, trusted proxy IPs and allowed hosts;
- the single-worker rule, OAuth, file permissions and backups, and a pre-live checklist.

`docs/USER_GUIDE.md` §2 now starts the app with `python -m backend.server`. It warns against `--reload` and `--workers` and links to DEPLOYMENT.md. `docs/TESTING.md` §2.1 checks the summary line.

**Authentication decision.** The app has no application authentication. Following the packet, no weak password system was invented. The default is local-only, remote use requires network-layer protection, and **direct public exposure is documented as unsupported**.

# Local Deployment

`python -m backend.server`, then browse to `http://127.0.0.1:8000` or `http://localhost:8000`. No configuration is needed.

The summary line reads `bind=127.0.0.1 … remote_exposure=no reload=no workers=1 … mode=local started_by=launcher`. This was verified by booting the real launcher against a scratch DB.

# Private VM Deployment

**Recommended (B1).**

- Keep the defaults on the VM, and run `ssh -N -L 8000:127.0.0.1:8000 you@vm`.
- The browser sees `127.0.0.1`, so the Host, Origin and OAuth settings are identical to local use.
- The VM firewall allows only SSH or the VPN.

**B2**, only if a tunnel is impractical:

- `KITE_TERMINAL_HOST=<private IP>`, `KITE_TERMINAL_ALLOW_REMOTE_BIND=1`, `KITE_TERMINAL_ALLOWED_HOSTS=<name>`.
- TLS through a reverse proxy, with `KITE_TERMINAL_TRUSTED_PROXY_IPS=127.0.0.1` so that `https://` Origins match.
- The app always warns about a remote bind.

# Static Outbound IP vs Inbound Exposure

Kite's static-IP rule concerns **outbound** API calls from the terminal. The VM can hold a reserved external IP for egress while its firewall allows no inbound traffic except SSH or the VPN. Port 8000 never needs to be publicly reachable. DEPLOYMENT.md includes the diagram:

- browser → tunnel → VM loopback;
- VM → static IP → Kite.

No VPN or SSH tooling was implemented.

# Single-Worker Requirement

- Each process starts five loops: the order monitor, trailing, reconciliation, the autopilot and the data-job worker. Several workers would duplicate protective orders.
- The launcher always passes `workers=1` and refuses `KITE_TERMINAL_WORKERS≠1`.
- In-app startup refuses `--workers N` and `WEB_CONCURRENCY=N` from a bare uvicorn command line.
- A second separate instance on another port is documented as unsupported; there is no lock, as instructed.

# OAuth Interaction

- `/login` and `/callback` are GET routes, so the PK-5 write policy still never applies to them. They are subject to the Host check.
- The Kite developer-console redirect URL must use an allowed host, for example `http://127.0.0.1:8000/callback`, or the proxy's HTTPS name in B2.
- In tests, a fake `KiteConnect` stood in for the network, and:
  - the callback on an approved Host exchanges the token and redirects to `/`, storing it only encrypted;
  - the same callback on a hostile Host is refused before the exchange;
  - the failure path is unchanged.
- The live launcher smoke test hit `/callback?request_token=…&status=cancelled`, which makes no Kite call. The access log showed `request_token=[REDACTED]`.
- **Not done: login CSRF.** There is no `state` parameter, so a site could log the terminal into another Kite session; the impact is low locally. See *Deferred Security Work*.

# PK-5 Interaction

The Host check comes first. The PK-5 write, header and Origin protection still applies unchanged after a Host is accepted:

- all 126 PK-5 tests pass;
- the 31-route inventory still returns `CSRF_PROTECTION_FAILED` for a foreign Origin on an approved Host;
- the live-safety P0-8 tests pass.

One parametrised case moved:

- **Old.** `test_e_non_terminal_origins_are_rejected[http://evil.example:8000-http://evil.example:8000]` sent Host `evil.example:8000`, so it now receives `INVALID_HOST` before the Origin check.
- **New home.** It lives in `test_security_hardening.py::test_dns_rebinding_write_is_rejected_by_host`.
- **Origin layer still tested.** The Origin layer's own verdict on the same pair was added to `test_e_is_terminal_origin`, so the PK-5 "Origin == Host" mutant is still caught.

The PK-5 net count is unchanged.

`is_terminal_origin` now uses the configurable allowed set, which by default equals the old loopback set.

The PK-5 report's "Loopback-only writes" and "HTTPS behind a proxy" limitations are now handled by configuration: `KITE_TERMINAL_ALLOWED_HOSTS` and `KITE_TERMINAL_TRUSTED_PROXY_IPS`.

# Files Changed

| File | Change |
|---|---|
| `backend/security.py` | Rewritten around the PK-5 core: the Host parser and policy, `TrustedHostGuard`, `SecurityHeadersMiddleware`, CSP and cache policy, and the configurable allowed set shared with the Origin check. PK-5 logic is otherwise unchanged. |
| `backend/redaction.py` | **New.** `redact_text`, `redact_detail`, `register_secret`, `SecretRedactingFilter` and `install_log_redaction`. |
| `backend/server.py` | **New.** The launcher and the runtime policy (`resolve_runtime`, `validate`, `detect_runtime`, `enforce_runtime_policy`, `security_summary`). |
| `backend/main.py` | Installs redaction at import. Adds the redacting `HTTPException` handler and turns off docs and ReDoc. Registers three middlewares in order. `enforce_runtime_policy()` is now the first statement of startup. |
| `backend/kite_client.py` | Encrypted-only token: `_access_token_decrypted`, `''` written into sessions, `get_kite` decrypts, and secrets are registered with the redactor. |
| `backend/db.py` | `_scrub_plaintext_session_tokens` and its call in `init_db`. |
| `frontend/index.html` | Vendored scripts with SRI; the inline script is replaced by `theme-init.js`. |
| `frontend/theme-init.js`, `frontend/vendor/*` | **New.** Two libraries, two licence files and a README. |
| `frontend/app.js` | One string: the chart-fallback message no longer says "CDN". |
| `.gitignore`, `.env.example` (new) | Repository hygiene. |
| `.gitattributes` (new) | Marks `frontend/vendor/*.js` and `*.txt` as `-text`. This repository checks out with `core.autocrlf`, so without it a fresh checkout would rewrite the vendored files' LF endings to CRLF, change their bytes, break the SRI hashes, and make the browser refuse both libraries. The problem was found while staging the commit. A simulated fresh checkout (`git checkout-index`) was then verified: no CRLF, and both SRI hashes match. The file is configuration only; no executable code changed, so the validation runs above stand. |
| `docs/DEPLOYMENT.md` (new), `docs/USER_GUIDE.md`, `docs/TESTING.md` | Deployment documentation. |
| `tests/test_security_hardening.py` | **New**, 177 tests. |
| `tests/test_csrf_protection.py` | One case moved and one function case added (see *PK-5 Interaction*). |
| `tests/e2e_ui.py` | Its five `wait_for_function` predicates are now arrow functions. Playwright `eval()`s a bare-expression predicate inside the page, which the new CSP correctly refuses. No check changed. |

**Not touched:** `execution.py`, `risk_engine.py`, `routes/tickets.py`, `order_monitor.py`, `trailing_engine.py`, `reconciliation.py`, `strategy_engine.py`, `journal.py`, `routes/ws.py` and `routes/auth.py`. `docs/OPUS55_AUDIT_REPORT.md` was not modified.

# Configuration Added

All settings are environment variables. The defaults are safe.

| Variable | Default | Purpose |
|---|---|---|
| `KITE_TERMINAL_HOST` | `127.0.0.1` | Launcher bind address |
| `KITE_TERMINAL_PORT` | `8000` | Launcher port |
| `KITE_TERMINAL_ALLOW_REMOTE_BIND` | `0` | Required for a non-loopback bind (launcher and in-app) |
| `KITE_TERMINAL_ALLOWED_HOSTS` | empty | Extra allowed Host names (no wildcards) |
| `KITE_TERMINAL_TRUSTED_PROXY_IPS` | empty | Enables uvicorn proxy headers for these proxy IPs only |
| `KITE_TERMINAL_DEV_RELOAD` | `0` | Development reload |
| `KITE_TERMINAL_WORKERS` | `1` | Must be 1 |
| `KITE_TERMINAL_RUNTIME_*` | (set by launcher) | Internal: what the launcher resolved, for the in-app check and summary |

There is no DB setting and no schema change.

# Tests Added

`tests/test_security_hardening.py` holds 177 tests, all offline (FakeKite, a temporary DB and a fake `KiteConnect`).

| Area | Tests |
|---|---|
| Host (A) | Loopback forms allowed (5). `parse_host` matrix (22, including IPv6, CR/LF, a Cyrillic homoglyph, a trailing dot and port range). IPv6 loopback on a raw scope. Hostile Host on 9 read paths with no broker call. Hostile Host even with a terminal Origin. Ticket POST ×4 header/Origin combinations with no insert and no broker write. Live confirm places nothing. The DNS-rebinding write. Forwarded-header bypass ×3 (GET and POST). The forwarded host is not echoed. Duplicate and malformed Hosts end to end. A raw-ASGI proof that the app is never called (×8, HTTP and WS). The configured allowed host (read and write). Wildcards refused ×4. Loopback-only default. Login and callback on approved and hostile Hosts. |
| WebSocket (E) | Approved Host and Origin ×3, including no-Origin. Hostile Host ×3, including a loopback Origin. Hostile forwarded host. Hostile Origin ×3. No credentials in the WS URL. |
| Runtime (B) | Defaults. Loopback forms ×5. Remote bind needs opt-in ×6 (env and CLI). Reload opt-in only. Reload with remote refused. Workers ×3. Bad values ×4. Trusted proxies. Launcher default invocation (`uvicorn.run` kwargs). Launcher refusal. Launcher proxy. In-app refusal for a bare uvicorn command line ×4. `WEB_CONCURRENCY`. Summary and warnings with no secret. Launcher-exported values. |
| Secrets (C) | Route-table crawl of every GET `/api/*` with sentinel credentials. Login stores the token only encrypted. `get_kite` uses the encrypted token, needs a login when it is missing, and needs a login when it cannot be decrypted. Logout clears. Migration backfills, blanks and is idempotent. The migration keeps an existing encrypted token. The redactor ×10 forms. Idempotency ×4. Innocent fields ×4. Redaction by value. The uvicorn access formatter. Installed loggers. Traceback. The unhandled 500. The `HTTPException` detail. `.env.example`. The repository secret scan. `.gitignore` coverage ×11. |
| Headers (F) | UI page CSP, XFO, nosniff, referrer, permissions, COOP and no-cache. No-store on 8 sensitive APIs. Auth redirects no-store. Static caching kept. Rejections carry headers and never echo a hostile Host. Docs pages are not served. |
| Dependencies (D) | No external or inline scripts. No inline handlers or `javascript:`. Every `/static/` reference resolves. Vendored scripts are pinned and SRI-matched. No external origin in `app.js`, `theme-init.js` or `style.css`. |

# Mutation Results

A scratchpad runner applied each mutant to one file and ran `test_security_hardening.py` plus `test_csrf_protection.py`. It then restored the original bytes in a `finally` block and verified the SHA-256.

| # | Mutant | File | Tests failing | Examples |
|---|---|---|---|---|
| 1 | Host guard not registered | `main.py` | 34 | `test_hostile_host_read_blocked`, `test_callback_blocked_on_hostile_host`, `test_dns_rebinding_write_is_rejected_by_host` |
| 2 | Host allow-list check removed | `security.py` | 30 | `test_hostile_host_*`, `test_forwarded_host_cannot_bypass`, `test_configured_allowed_host` |
| 3 | `X-Forwarded-Host` trusted over `Host` | `security.py` | 4 | `test_forwarded_host_cannot_bypass`, `test_forwarded_host_is_not_echoed`, `test_ws_hostile_forwarded_host_refused` |
| 4 | Hostile WebSocket Host accepted (Host guard skips websocket scopes) | `security.py` | 7 | `test_ws_hostile_host_refused`, `test_guard_never_calls_the_app_for_bad_host[websocket-*]` |
| 5 | `/api/session/status` leaks the access token | `routes/auth.py` | 1 | `test_no_get_endpoint_returns_a_secret` |
| 6 | Secret redaction removed (`redact_text` returns its input) | `redaction.py` | 15 | `test_redact_text`, `test_error_detail_is_redacted`, `test_exception_traceback_is_redacted`, `test_installed_redaction_*` |
| 7 | Default bind restored to `0.0.0.0` | `server.py` | 6 | `test_runtime_defaults_are_local_and_safe`, `test_launcher_default_invocation` |
| 8 | Reload on by default | `server.py` | 9 | `test_reload_is_explicit_dev_opt_in_only`, `test_runtime_defaults_are_local_and_safe` |
| 9 | Remote-bind opt-in check removed | `server.py` | 9 | `test_remote_bind_requires_explicit_opt_in`, `test_launcher_refuses_unsafe_start`, `test_in_app_policy_refuses_unsafe_bare_uvicorn` |
| 10 | Worker-count check removed | `server.py` | 5 | `test_unsafe_worker_count_is_refused`, `test_in_app_policy_reads_web_concurrency` |
| 11 | Launcher trusts proxy headers by default | `server.py` | 1 | `test_launcher_default_invocation` |
| 12 | Clickjacking protection removed (`frame-ancestors` and XFO) | `security.py` | 10 | `test_security_headers_on_the_ui_page`, `test_sensitive_api_responses_are_no_store`, `test_rejections_carry_security_headers` |
| 13 | CSP allows inline script | `security.py` | 1 | `test_security_headers_on_the_ui_page` |
| 14 | `no-store` removed from API responses | `security.py` | 10 | `test_sensitive_api_responses_are_no_store`, `test_auth_redirects_are_no_store` |
| 15 | Unpinned external JS (`cdn.jsdelivr.net/npm/lightweight-charts/…`, no version, no SRI) added | `index.html` | 1 | `test_no_external_or_inline_scripts` |
| 16 | Token-scrub migration not run | `db.py` | 1 | `test_scrub_migration_backfills_then_blanks_and_is_idempotent` |
| 17 | Login stores the plaintext token again | `kite_client.py` | 1 | `test_login_stores_token_only_encrypted` |

**All 17 mutants were killed.** Every mutated file was restored byte for byte (SHA-256 verified), and the working-tree diff stats afterwards were identical to those before the run.

**Earlier aborted attempt.** The first attempt stopped before its first write. `main.py` has CRLF line endings, and the runner's pattern did not match them. That was fixed and the run repeated; no file was modified by the aborted attempt.

**Code changed after the mutation run:** none. The real-server smoke test found the redaction idempotency bug *before* the mutation run, and that fix was already in place.

# Security Repeat Results

| Selection | Runs | Result, every run |
|---|---|---|
| Security suite: `test_security_hardening.py` + `test_csrf_protection.py` + `test_live_safety.py` | 10 | **412 passed**, 0 failed (≈83 s each) |
| Host/Origin/WS subset: the first two files with `-k "host or origin or ws or websocket or forwarded or rebinding or callback or login"` | 20 | **209 passed**, 98 deselected, 0 failed (≈40 s each) |

There were no flaky results. All of these runs used the final code, after the mutation run and its restore.

# Browser Validation

**Setup.** `tests/demo_server.py --port 8765` (FakeKite, a throwaway DB, no Zerodha), driven by Playwright with Microsoft Edge headless. **CSP was enforced**: no `bypass_csp`. A `securitypolicyviolation` listener was installed before any page script ran.

**PK-18 script: 33/33 checks passed.**

- **Wire level:**
  - approved read 200, with `no-store` and CSP;
  - hostile Host 400 `INVALID_HOST`;
  - `X-Forwarded-Host` bypass 400;
  - a hostile-Host write with a valid header and loopback Origin gets 400;
  - a PK-5 cross-origin write still gets 403;
  - a malformed raw Host gets 400;
  - duplicate raw Hosts get 400;
  - the WebSocket gives 101 for an approved Host, with or without Origin;
  - the WebSocket gives 403 for a hostile Host, a hostile Host with a loopback Origin, or a hostile Origin.
- **Real UI:**
  - the page is served with CSP;
  - the vendored Sortable and LightweightCharts globals are loaded;
  - `theme-init.js` applied the theme;
  - the tick WebSocket connected, with no query string;
  - the approval cards render;
  - the Signal Detail chart canvas renders under CSP;
  - the ticket history view renders;
  - the **mode switch to LIVE and back to DRY RUN** works, so writes are accepted;
  - **zero CSP violations, zero console errors, zero failed requests, zero 4xx/5xx responses**.
- **DNS rebinding for real.** Edge was launched with `--host-resolver-rules=MAP evil.example 127.0.0.1`.
  - `http://evil.example:8765/` gets 400 `INVALID_HOST`.
  - Its same-origin `fetch('/api/holdings')` gets 400.
  - Its WebSocket is refused.
- **Clickjacking.** A foreign page (`attacker.test`, served through Playwright routing) that iframes the terminal gets `chrome-error://`, so the frame is refused. Its `no-cors` POST to `cancel-all-pending` cancelled nothing: 4 pending tickets before, 4 after.

**Existing `tests/e2e_ui.py`: 53/54.**

- The single failure, "risk-blocked signal listed separately", is **pre-existing**. The untouched `backup-pre-pk18` code, run in a separate worktree against its own demo server on port 8766, fails the same check (53/54).
- That check depends on the demo data, and it does not involve security.
- Every other check passes, including "no JavaScript errors".

**Launcher boot, real process.**

- **Setup.** The scratch DB and scratch working directory were used, the real `.env` was not read, and no network calls were made.
- `python -m backend.server` equivalent on port 8767:
  - the summary line shows `started_by=launcher`;
  - there is no `Server` header;
  - `/api/session/status` returns `no-store`;
  - a hostile Host gets 400;
  - `X-Forwarded-Proto: https` is ignored (the https Origin is refused with 403, as intended with no trusted proxy);
  - the access log redacts `request_token`.
- `KITE_TERMINAL_HOST=0.0.0.0` through the launcher: "Refusing to start", exit code 2, no DB created.
- A bare `uvicorn backend.main:app --host 0.0.0.0`: `RuntimeConfigError`, "Application startup failed", exit code 3, **no DB created**. `init_db` never ran.

# Full Suite ×3

| Run | Result |
|---|---|
| Full suite, run 1 | **986 passed**, 0 failed, 0 xfailed, 0 xpassed (257.8 s) |
| Full suite, run 2 | **986 passed**, 0 failed, 0 xfailed, 0 xpassed (259.7 s) |
| Full suite, run 3 | **986 passed**, 0 failed, 0 xfailed, 0 xpassed (257.2 s) |

The three runs were consecutive, on the final code, after the mutation run.

- **Count.** 986 = the 809 baseline + 177 new tests in `test_security_hardening.py`. The PK-5 file's count is unchanged: one case moved out and one function case was added.
- **Offline.** No test reached Zerodha or the network. `get_kite` is the FakeKite throughout, and the login tests use a fake `KiteConnect` class.
- **Live DB.** Its SHA-256 after the three runs is still `1378f8b47cd752bad016b5d5eed35b4e1317f529d32e388368b980add003dacf`, identical to the value recorded before the first edit.

# Database Validation If Applicable

The one data migration, `_scrub_plaintext_session_tokens`, was run twice on a **copy** of `kite_terminal.db.pre_pk18_bak`, using the real encryption key. No value was printed.

| | Before | After run 1 | After run 2 |
|---|---|---|---|
| sessions | 15 | 15 | 15 |
| sessions with plaintext token | **15** | **0** | 0 |
| active sessions | 1 | 1 | 1 |
| accounts with encrypted token | 1 | 1 (existing ciphertext kept, byte-identical) | 1 |
| trade_tickets / ohlc_daily | 18 / 50 556 | 18 / 50 556 | 18 / 50 556 |
| `integrity_check` / FK violations | ok / 0 | ok / 0 | ok / 0 |

**Second run.** It changed **no data row**. A full `iterdump` diff shows only two `sqlite_sequence` counters advancing (`risk_settings`, `strategy_configs`). Those come from pre-existing seed functions in `init_db`, not from PK-18, and the same happens on every startup today.

**Live DB.** Its SHA-256 is `1378f8b47cd752bad016b5d5eed35b4e1317f529d32e388368b980add003dacf`, identical before the first edit and after all validation. The scratch copies were deleted afterwards, because they contained real account data.

**On the next real start** the live DB's 15 plaintext session tokens will be blanked. The active login stays valid through the existing encrypted copy. Every such token is a past daily token; Kite tokens expire each day.

# Known Limitations

- **No application authentication.** Remote use depends entirely on the network: an SSH tunnel, VPN or firewall, plus TLS. Public exposure is unsupported, and the app cannot detect whether a firewall exists.
- **Bare-uvicorn detection is best-effort.** It parses the command line. Programmatic `uvicorn.run(app, host=…)` calls (such as `tests/demo_server.py`) report `started_by=unverified` with default values. Only the launcher is enforced by construction.
- **No second-instance detection.** A second process on another port against the same DB is not detected.
- **`style-src 'unsafe-inline'`.** It remains, because `app.js` builds `style=` attributes. Scripts are fully locked down.
- **Log redaction and late handlers.** Only handlers present at import or startup get the filter; handlers added later by other code do not. Logger-level filters on root and uvicorn still apply to records logged directly to those loggers.
- **Stored error messages.** Broker exception text that earlier packets stored in ticket or event rows is not re-redacted when read back through GET routes. Kite exception texts do not carry credentials (authentication is by header), and the GET crawl found none.
- **Old backups.** `.pre_ph*` and `.pre_pk*_bak` taken before PK-18 still contain plaintext past tokens. They are documented as sensitive and were not modified.
- **Key storage.** The encryption key can still live in `.env` next to the secret. Moving it to the service environment is documented but not enforced.
- **IPv6 end to end.** IPv6 Host handling is verified on the parser and a raw scope, because the TestClient cannot address IPv6.
- **Stale tabs.** A browser tab opened before the upgrade still has the old `index.html`, which refers to CDN scripts that are now blocked by CSP. Reload the tab after upgrading.

# Deferred Security Work

- Real authentication, if remote access without a tunnel is ever required: a session cookie that is HttpOnly, Secure and SameSite=Strict, on every route and the WebSocket.
- A login-CSRF `state` nonce on the Kite round trip.
- A single-instance lock, such as a DB-level lease, to detect a second process.
- Moving the encryption key to the OS keyring or a secret manager, and rotating it.
- Removing `style-src 'unsafe-inline'`, which needs the frontend to stop using `style=` in `innerHTML`.
- Rotating logs to files with the redaction filter (audit P2-9).
- Scrubbing or re-encrypting old DB backups.
- These are candidates for an explicit later packet. None was started.

# Deviations From Audit

| Audit / plan said | PK-18 did | Why |
|---|---|---|
| P1-12: "drop the plaintext token column" | Blanked the column and stopped writing it; no `DROP COLUMN` | The packet asks to avoid schema changes. A data-only scrub removes the secret with no table rebuild. The column is legacy, NOT NULL and always `''`. |
| P1-12: "move the key location" | Documented supplying `KITE_TERMINAL_ENCRYPTION_KEY` from the service environment; not enforced | The packet says not to build a large key-management system. `load_dotenv` already never overrides the process environment. |
| P1-12: "add SRI or vendor" | Did both: vendored, and SRI on the local files | Local serving is the preferred policy. SRI on local files also catches tampering, and a test keeps the hashes current. |
| P1-12: "remove `--reload` from docs" | Removed it, **and** made reload an explicit dev opt-in that is refused with a remote bind and warned on at startup | Documentation alone does not stop the operator. |
| Plan: wrap `KITE_API_ERROR` strings in three route files | A single `HTTPException` handler redacts **every** error detail | About 20 sites quote broker exceptions; one central point covers them all, including future ones, with no route edits. |
| Security Assessment: "authentication on every route" before any VM deployment | Not implemented; network-layer protection is required and public exposure is unsupported | The packet says not to invent a weak custom password system. |
| PK-5 test `test_e_non_terminal_origins_are_rejected[evil.example]` | Moved to the Host suite, with its Origin verdict kept as a function case | The request is now correctly refused earlier, with `INVALID_HOST`. |
| `tests/e2e_ui.py` | Its `wait_for_function` string predicates became arrow functions | The string form needs `eval` in the page, which the new CSP correctly blocks. The same checks are kept. |
