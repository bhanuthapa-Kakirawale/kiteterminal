# P2-G security audit: the Kite Terminal authentication and access boundary

**Scope.** The P2-G item of the cross-repository remediation plan. First pass: the DMB–Kite readiness sprint (2026-11-03, commit `93f5e5b`, which fixed F1 and documented four residual findings R-F1 to R-F4). Second pass, this document: the remediation sprint of the same day, which addresses R-F1 to R-F4 and re-verifies F1. It covers login and logout, the per-launch credential, Host and Origin validation, read and write routes, the WebSocket, cookies, network binding and the DMB inbox routes.

**Test conditions.** FakeKite and throwaway databases only. No real Kite login, broker call, account database or production data was used. A real Kite login is required to close one item (R-F1, see below) and is outside this sprint.

## Verdict

| Finding | Severity (before) | Status | Evidence |
|---|---|---|---|
| F1 — login CSRF on `/callback` | Medium | **Fixed, re-verified** | `tests/test_security_p2g.py` (F1 tests), unchanged negative assertions |
| R-F1 — callback accepted while the app's own login is pending | Low | **Fixed in code; one real-login confirmation outstanding** | 9 tests; protocol per Kite Connect docs |
| R-F2 — sensitive reads relied on Host checks | Medium | **Fixed** | route-inventory sweep (53 sensitive GET routes), Origin / Fetch-Metadata / CORS tests, browser checks |
| R-F3 — credential cookie shared across ports on 127.0.0.1 | Medium | **Mitigated: the cookie is no longer sufficient; residual documented** | backend tests + a real second server in headless Edge |
| R-F4 — price WebSocket survives logout | Low | **Fixed** | 5 backend tests + a browser check |
| §4 item 8 — no absolute credential lifetime (the R-F3 two-factor credential never expired) | Low | **Fixed (2026-11-04)**: absolute lifetime + inactivity timeout, enforced server-side on every check, revoking and closing sockets | `tests/test_session_expiry.py` (26 tests, mutation-checked) |

**Security verdict:** the four findings are closed or mitigated with regression evidence; no finding is marked resolved without a test. The remaining limitations (§4) are inherent to a local browser application and are accepted risks, not open defects, **except** R-F1's real-login confirmation, which must be done on the first real login.

**Operational use:** the security boundary no longer blocks *dry-run* operation of the real terminal. It does **not** by itself approve real-data handoff (blocked by source rights, `docs/DMB_KITE_SOURCE_RIGHTS_MATRIX.md`) or live broker execution (never validated against the real broker). See the decision table in `docs/DMB_KITE_OPERATIONAL_RUNBOOK.md` §7.

## 1. How access works now (map)

| Layer | Where | Applies to | Effect |
|---|---|---|---|
| Bind | `backend/server.py` | process | 127.0.0.1 only; a non-loopback bind needs `KITE_TERMINAL_ALLOW_REMOTE_BIND=1`. One worker, no reload. |
| Security headers | `SecurityHeadersMiddleware` | every HTTP response | Strict CSP, `frame-ancestors 'none'`, `no-store` on `/api/*`, `/login`, `/callback`. **No CORS middleware**: no `Access-Control-Allow-*` header is ever sent. |
| Host | `TrustedHostGuard` | every request and WS handshake | Exact allowed names (`localhost`, `127.0.0.1`, `::1`, plus exact `KITE_TERMINAL_ALLOWED_HOSTS` entries; wildcards refused). **New (R-F3):** when started by the launcher, the Host must name exactly the served port (`KITE_TERMINAL_RUNTIME_PORT`). |
| Origin (writes, WS) | `TerminalOriginGuard` | non-GET requests, WS handshake | `X-Kite-Terminal: 1` and a terminal Origin (or Referer); none refused. |
| Origin (reads) | `TerminalOriginGuard` | `/api/` GET | **New (R-F2):** refused (403) when `Sec-Fetch-Site` is `same-site` (another port on 127.0.0.1) or `cross-site`, or when a present `Origin` is not a terminal origin. |
| Credential | `TerminalCredentialGuard` | writes, WS, **and every non-public `/api/` read (new, R-F2)** | **Two factors (new, R-F3):** the `kt_session` cookie (HttpOnly, SameSite=Strict, host-only) **and** the terminal token (`X-Kite-Terminal-Token` header, or the `kt-token.<token>` WS subprotocol). Both held as SHA-256 digests in memory; restart forgets them, logout revokes them, a new login rotates them. |
| Session | `require_kite`, `require_trading_account`, `require_inbox_session` | most API routes | An active, fresh Kite session and the trading account. |
| Login | `GET /login` → Kite → `GET /callback` | top-level navigation | One pending, single-use, 10-minute state, cookie-bound **and** echoed by Kite (new, R-F1). |
| WebSocket lifetime | `backend/routes/ws.py` | `/ws/ticks` | **New (R-F4):** closed with code 4401 when the credential is revoked or replaced. **2026-11-04:** also at the credential's expiry (a per-socket deadline timer). |
| Session expiry | `backend/security.py` (`_TerminalCredential`) | every credential check | **New (2026-11-04):** the credential dies at the earlier of an absolute lifetime (default 480 min, max 720) and an inactivity timeout (default 30 min, max 120); see "Session expiry" below. |

## 2. Findings and changes

### F1 — Login CSRF on `/callback` (fixed in `93f5e5b`, re-verified)

A forced `/callback?request_token=<attacker's>` used to replace the user's Kite session. Since F1, `/callback` exchanges nothing unless this browser holds a pending login state from `/login`. Re-verified this sprint: `test_a_forced_callback_without_a_started_login_exchanges_nothing`, `test_a_forced_callback_never_rotates_the_existing_credential`, replay, expiry, cancel and wrong-state tests all pass. None of their negative assertions was relaxed.

### R-F1 — Callback binding to the login initiation

**Before.** The state cookie bound a callback to "a login this browser started". A forced callback that landed *while the user's own login was pending* passed (`test_residual_a_callback_inside_a_login_this_browser_started_passes`, now removed and inverted).

**Protocol check (not invented).** Kite Connect documents, at `https://kite.trade/docs/connect/v3/user/`: *"An optional `redirect_params` param can be appended to public Kite login endpoint, that will be sent back to the redirect URL"*, as a URL-encoded query string (`redirect_params=some%3DX%26more%3DY`). That is the mechanism used. No other parameter was added.

**Change** (`backend/security.py`, `backend/routes/auth.py`, `backend/kite_client.py`):
- `/login` creates a cryptographically random state (`secrets.token_urlsafe(32)`), keeps only its SHA-256 in memory, sets it in the HttpOnly `kt_login_state` cookie (SameSite=Lax, Path=/callback, 10 min), and sends it to Kite as `redirect_params=kt_state%3D<state>`.
- `/callback` consumes the pending state first, then requires that the `kt_state` Kite echoes equals this browser's cookie exactly (constant-time). Otherwise nothing is exchanged and nothing issued.
- **One pending login at a time:** a new `/login` supersedes every earlier one, so a stale Kite tab from an older attempt can never complete.
- **Single use:** every callback (success, cancel, failure, mismatch, replay) consumes the state.

**Tests** (`tests/test_security_p2g.py`): login sends the state in `redirect_params`; own login completes once; replay refused (same and other browser); **forced callback inside the user's pending login refused, with no echo and with a guessed echo** (the old residual); echo without the browser's cookie refused; a new login supersedes a stale one; an echo of another value refused; expired state refused; cancel consumes; only one pending login; the downgrade below is explicit and logged.

**Limitation (honest).** Whether Kite actually echoes `redirect_params` can only be observed with a real Kite login, which no sprint has performed. Zerodha's documentation says the value "will be sent back to the redirect URL"; a Zerodha developer-forum answer (2019) shows the observed form: the decoded pairs appended as ordinary query parameters (`…?request_token=…&action=login&status=success&user_id=abcd`), which is exactly what `/callback` reads (`kt_state`). That is documentary, not observed. **Required before real use:** one user-operated real login confirming the echo (`docs/DMB_KITE_OPERATIONAL_RUNBOOK.md` §1.6), recorded in the runbook.

**No downgrade (sprint of 2026-11-04).** The earlier `KITE_TERMINAL_LOGIN_STATE_ECHO=cookie-only` fallback was removed on the owner's instruction: the setting is ignored and reported as an error, and a callback without the echoed state never completes (`test_there_is_no_cookie_only_downgrade`). If a real login shows that Kite does not echo the state, the login keeps failing closed and real-account operation stays NO-GO until a secure, documented alternative is verified.

**Logs.** The callback query string reaches the uvicorn access log. `request_token` was already redacted; `kt_state` (and the `kt_token`, `kt_session`, `kt_login_state` names) are now redacted too (`test_the_callback_access_log_line_carries_no_login_secret`). The state is single-use and consumed before the log line is written, so its earlier exposure there was low-impact, but it was a login secret in a log.

### R-F2 — Sensitive reads

**Before.** Any local process could read account data with an allowed Host header; the P1-F owner decision was "reads are unchanged".

**Decision applied.** The readiness brief requires authentication for sensitive reads, so P1-F's "reads are unchanged" is reversed. This is a deliberate, operator-visible change: after a restart or logout, account data is hidden until a fresh login.

**Inventory** (enumerated from the running app, `test_r_f2_the_read_inventory_is_classified`):

| Class | Routes | Policy |
|---|---|---|
| Public / non-sensitive | `/` and `/static/*` (the UI), `/login`, `/callback` (each guarded on its own), FastAPI's schema `/openapi.json`; `/api/health` (status, DB ok, version), `/api/config` (static alert thresholds) | No credential. |
| Session status | `/api/session/status` | Public, but returns `user_name` / `login_time` **only** to an authorized browser; to anyone else only `connected`, `account_type`, `terminal_authorized`. |
| Session-sensitive | `/api/ready`, `/api/diagnostics`, `/api/automation/status`, `/api/account-safety/status`, `/api/live-readiness`, `/api/execution/mode`, `/api/ui-prefs`, `/api/dashboard-panels`, `/api/data/history-health`, `/api/reconciliation/*`, `/api/kill-switch/emergency-exit-preview` | Both credential factors. |
| Account / trading-sensitive | `/api/profile`, `/api/holdings`, `/api/positions`, `/api/mf_holdings`, `/api/margins`, `/api/orders`, `/api/trades`, `/api/gtts`, `/api/tickets*`, `/api/app-positions`, `/api/todays-actions`, `/api/journal/*`, `/api/risk/*`, `/api/watchlist*`, `/api/strategies*`, `/api/dmb-inbox/*`, research and score routes (`/api/research/*`, `/api/volume-intelligence/*`, `/api/smart-money-score/*`), `/api/instruments/search` | Both credential factors. |

The rule is **default-deny**: every `/api/` GET needs the credential unless it is in `PUBLIC_API_READS` (three routes). A new route is protected without anyone remembering to list it; a new *public* route makes the inventory test fail.

**Origin, Host and CORS.** An `/api/` read is refused before routing when `Sec-Fetch-Site` is `same-site` (exactly what another port on 127.0.0.1 is) or `cross-site`, or when a present `Origin` is foreign. This is defence in depth on top of the credential. CORS stays absent: no `Access-Control-Allow-*` on reads, writes or preflights. DNS rebinding stays refused by the Host check. Host validation is no longer the only boundary.

**UI.** `frontend/app.js` reads `/api/session/status` first; a connected but unauthorized browser sees **"Log in to unlock this terminal"** (no account data) instead of the read-only dashboard. Account reads are not even attempted while unauthorized or after logout. The CSV export link is fetched with the token.

**Tests:** every one of the 53 sensitive GET routes refuses an anonymous caller (401 `TERMINAL_SESSION_REQUIRED`); cookie-only, token-only, swapped and wrong-token clients read nothing; a revoked/restarted session reads nothing and a fresh login restores reads; legitimate UI reads work; public reads disclose nothing account-specific; `same-site` and `cross-site` reads refused even with both factors; foreign Origins refused; no CORS on preflight or response; DNS rebinding refused. In the browser: the page before login shows the locked gate; a cross-origin `fetch` from another port cannot read account data.

### R-F3 — Credential cookie visible to other ports on 127.0.0.1

**Constraint stated plainly.** Browser cookies are not port-scoped. A cookie set by `127.0.0.1:8000` is sent to every server on `127.0.0.1`. No cookie attribute changes this: not `HttpOnly`, not `SameSite=Strict`, not a `__Host-` prefix (which also needs `Secure`, unavailable on plain-http loopback), and not a different port.

**Architecture investigated.**

| Option | Port isolation? | Assessment |
|---|---|---|
| Different cookie attributes / prefix / port | No | Cookies have no port. Rejected as a fix. |
| Dedicated application hostname (e.g. `kite-terminal.localhost`, host-only cookie, exact Host allowlist) | Partial | The cookie is no longer sent to `127.0.0.1:*` or `localhost:*`, so other local web apps on the generic names never see it. But any process can listen on `kite-terminal.localhost:<other port>` (it resolves to loopback) and lure the browser there; the cookie goes with it. `Secure` needs https, which plain loopback lacks. Supported (exact `KITE_TERMINAL_ALLOWED_HOSTS` entry, tested) as optional defence in depth, not as the fix. |
| One-time pickup endpoint for a header token | Yes, but **racy** | Rejected after analysis: a page on another port polling its own server receives the cookie the moment it is set and could claim the token before the real page. |
| **Second factor in origin-scoped storage, delivered in the login redirect's fragment** | **Yes** | **Implemented.** |

**Change.** Each login now issues two independent secrets: the HttpOnly cookie, as before, and a *terminal token*. The `/callback` redirect sends the browser to `/#kt_token=<token>`. That response reaches only the browser that passed the login-state check. A URL fragment is never sent to any server or put in a `Referer`, and another origin cannot read this page's URL. `frontend/terminal_auth.js`, loaded first, moves the token into `localStorage`, which is scoped by **origin, port included**, and strips it from the address bar. It also handles a fragment-only landing. It then sends the token as `X-Kite-Terminal-Token` on every same-origin request, and as the `kt-token.<token>` subprotocol on the tick WebSocket (the server answers `kt.v1`). The server requires both factors on every write, every non-public read and the WS handshake.

The cookie keeps its attributes (HttpOnly, SameSite=Strict, Path=/, host-only: no Domain; Secure over https). It is still sent to other ports, but it is now useless on its own. Additionally, **exact authority**: started by the launcher (or the demo server), the Host must name the served port, so a request addressed to another port's authority is refused.

**Tests, against each scenario the brief names.**

| Scenario | Result | Evidence |
|---|---|---|
| Another port on 127.0.0.1 | A real HTTP server on `port+1` **receives** the cookie (demonstrated), **cannot read** the token, and replaying the captured cookie with forged Host/Origin reads nothing (401) and writes nothing (401) | `e2e_dmb_inbox.py` R-F3 checks; `test_r_f3_the_cookie_alone_replayed_from_another_port_cannot_read_write_or_stream`; P1-F sweep `cookie_without_token` over every mutating route |
| Another local hostname | `localhost:<port>` (the same server under another name) gets neither factor; the terminal stays locked; a non-allowlisted name (`other.localhost`) gets 400 | browser check; `test_r_f3_a_dedicated_hostname_is_exact_not_wildcard` |
| Unexpected Origin | Writes refused (403) even with both factors; reads refused (403) | `test_r_f3_an_unexpected_origin_never_writes_even_with_both_factors`, `test_r_f2_a_read_with_a_foreign_origin_is_refused` |
| Unexpected Host | 400, including a wrong port under the exact-authority rule | `test_r_f3_the_host_must_name_this_terminals_exact_port`, browser check |
| Browser navigation and cross-origin requests | Cross-origin `fetch` from another port cannot read; `same-site`/`cross-site` Fetch Metadata refused; the login lands with the token in the fragment only, never in a cookie, body, log or `Location` without the fragment | browser checks; `test_r_f3_the_terminal_token_travels_only_in_the_landing_fragment`; P1-F leakage test |

**Residual threat (not solved, by nature).** Processes running as the **same Windows user** can read the browser profile, including `localStorage`, the cookie store, `.env` and the database, or drive the browser itself. Browser extensions with access to this origin can read the page. Administrators and debuggers are outside the boundary too. Localhost has no TLS, so a privileged local packet capture sees everything. The token in `localStorage` is readable by script on this origin: an XSS on the terminal's own page would expose it. That risk is limited by the CSP (`script-src 'self'`, no inline script, `connect-src 'self'`, `img-src 'self' data:`), but it did not exist for the HttpOnly cookie alone. The cookie remains required, so a stolen token alone is also useless. The fragment briefly appears in the browser's history entry for the landing URL until it is replaced, and browser history is same-user data.

### R-F4 — WebSocket survives logout

**Change** (`backend/security.py`, `backend/routes/ws.py`): the credential keeps a generation counter and listeners. Every issue or revoke (login, logout, restart-equivalent) notifies listeners. Each `/ws/ticks` handler registers a listener **before** re-checking its own credential, so a revoke landing between the handshake guard and the handler is not missed. On notification it closes the socket with code **4401** ("Terminal session revoked") and sends nothing more. Client messages are never answered. The listener is released with the socket. The page treats 4401 as final: no reconnect, and the authorize banner is shown.

**Tests:** the browser handshake uses the token subprotocol (no token → 1008); logout closes an open socket (4401); a new login closes the old socket and its old factors can never reconnect, while the new login's socket works; an event queued after revocation is never delivered and a message sent is not answered; the listener is released. In the browser, a logout closes the page's open socket with 4401 and the page shows the banner.

## 3. P1-F and P0 controls

Not weakened. The credential guard, Origin guard, Host guard and live-readiness gates still apply exactly as before. The changes only add requirements: a second factor, reads gated, an exact port, an echoed login state, and WS closure. Every pre-existing negative test still passes unchanged. Positive-path tests now present both factors as the browser does: `test_terminal_credential.py`, `test_csrf_protection.py`, `test_security_hardening.py`, `test_emergency_exit_restart.py`, `test_live_preflight.py` (one fake WebSocket gained an authorized scope). Three tests that pinned the *old* behaviour were inverted deliberately:
- `test_reads_need_no_credential` → `test_reads_need_the_credential_except_the_public_ones`;
- `test_reads_are_not_blocked_by_the_guard` → `test_reads_from_a_foreign_origin_are_refused`;
- the restart test now expects reads refused.

The every-mutating-route sweep gained a fourth case, cookie without token.

## 4. Remaining limitations

1. **R-F1 real-login confirmation** (above). Until done, real-terminal login is unproven. There is no fallback (removed 2026-11-04).
8. **No absolute credential lifetime: CLOSED (2026-11-04).** Found in the 2026-11-04 review (Low): the credential lived until logout, a new login or a restart. Fixed as described in "Session expiry" below. Residual: the absolute lifetime is counted from the login, not tied to the Kite session's own end (Kite access tokens expire around 06:00 IST the next day); with the 8-hour default and 12-hour cap the credential cannot outlive a session started after 18:00 by more than a few hours, and broker calls stop at Kite's expiry anyway (`NO_SESSION`).
2. **Same-user malware, extensions, admin/debugger, local packet capture** (R-F3 residual above). Outside any local web app's boundary.
3. **XSS on the terminal origin would expose the terminal token** (still not the cookie). Mitigated by the strict CSP; no XSS is known.
4. **Fragment in history** until the page replaces it (same-user data).
5. **Operator-visible change:** after restart or logout, account data is hidden until a fresh Zerodha login, and Emergency Exit already needed one. Kite web and the broker-side GTTs remain the fallback.
6. **The `/api/tickets/{id}/chart` GET** caches a chart URL on first view. It is not an economic field and is now credential-gated like every read.
7. **Real broker paths unvalidated:** every security test here uses FakeKite.

## Session expiry (closes §4 item 8, 2026-11-04)

| Property | Implementation |
|---|---|
| Absolute lifetime | `KITE_TERMINAL_SESSION_ABSOLUTE_MINUTES`, default **480** (8 h), capped at 720. Counted from the login that issued the credential. Nothing extends it: only a fresh Zerodha login (password + 2FA) issues a new credential with a new lifetime. |
| Inactivity timeout | `KITE_TERMINAL_SESSION_IDLE_MINUTES`, default **30**, capped at 120, never more than the absolute lifetime. Only requests marked `X-Kite-Terminal-Activity: user` push it. `frontend/terminal_auth.js` adds the marker only within 60 s of a trusted keyboard, pointer, wheel or touch event, so background polling and streamed ticks never keep a session alive. The marker is not authentication. Anyone holding both factors could send it, which is why the absolute lifetime is the bound on a stolen credential. |
| Settings | Read when a credential is issued, so a later change never lengthens a live session. A non-positive or non-integer value falls back to the default (never "no limit"); values above the cap are clamped. |
| Enforcement | Every credential check enforces both deadlines on a monotonic clock: each protected read and write, the session check, the WebSocket handshake, and a once-per-second timer inside every open socket. An expired credential is revoked on the spot (digests cleared, generation bumped, listeners notified), so every socket opened under it closes with 4401, exactly as after a logout. |
| Logout / restart | Unchanged: immediate revocation (logout) and an empty in-memory credential (restart). |
| Cookie | `kt_session` now carries `Max-Age` = the absolute lifetime, so the browser also drops it; the server enforces regardless. |
| User-facing behaviour | A refused request keeps `error_code: TERMINAL_SESSION_REQUIRED` (every client handles it as before) and adds `reason` (`SESSION_IDLE_TIMEOUT` / `SESSION_LIFETIME_EXCEEDED`) and a message naming the limit. `/api/session/status` returns `session_expired` and `session_expired_message`, but only to the browser whose own cookie expired. The page shows **"Log in to unlock this terminal"** with that reason, and a 60-second heartbeat locks an idle page without waiting for a click. |
| Logs | `event=terminal_session.expired reason=...`; no secret (tested). |
| Not changed | The Kite access token and LIVE arming are untouched by expiry, as by a restart. Server-side protective workers keep following live positions, and a new login disarms LIVE as before. |

Tests (`tests/test_session_expiry.py`, 26): defaults; invalid, oversized and inconsistent settings; settings changed after login; idle expiry for reads and writes; background requests are not activity; user activity extends; heartbeat only with both factors; no revival after expiry; absolute expiry despite continuous activity; only a fresh login renews; an expired pair never works again; an expired session changes nothing; only the holder is told why; expiry logged without secrets; logout and restart; socket closed (4401) on idle and on absolute expiry; expired pair refused at the handshake; a socket follows the moving idle deadline. Four mutants were run: expiry disabled, activity extends the absolute deadline, every request counts as activity, no socket timer. Each one fails tests.

## 5. Test evidence (this sprint)

| Suite | Result |
|---|---|
| `tests/test_security_p2g.py` | 42 passed (F1, R-F1 to R-F4, inventory, audit sweep) |
| `test_terminal_credential.py`, `test_csrf_protection.py`, `test_security_hardening.py` | all passed |
| `test_emergency_exit_restart.py`, `test_emergency_exit_isolation.py` | all passed |
| `tests/acceptance_dmb_kite.py` (real processes, real restart) | 44/44 (42 earlier checks, stage 17 inverted per R-F2, + 2 R-F3) |
| `tests/e2e_dmb_inbox.py` (headless Edge) | 42/42 (31 earlier + 11 P2-G browser checks) |
| `tests/e2e_p1d.py` (headless Edge) | 13/13; every refusal declared and observed; no other console/page/HTTP error |
| `tests/e2e_ui.py --pin-clock` | 53/54 (the one pre-existing demo-data failure, unchanged) |
| Full Kite suite | 2,571 passed, 3 skipped, 0 failed (`docs/DMB_KITE_OPERATIONAL_RUNBOOK.md` §8) |
