# P2-G security audit: the Kite Terminal authentication and access boundary

**Scope.** This is the P2-G item of the cross-repository remediation plan, done in the DMB–Kite readiness sprint (2026-11-03). It is a focused review of how the terminal authenticates and what it exposes. It covers:

- login, logout and the per-launch credential;
- Host and Origin validation;
- read and write routes;
- the WebSocket;
- cookies;
- network binding;
- the DMB inbox routes.

**Test conditions.** FakeKite and throwaway databases only. No real Kite session, broker call or account database was used.

**Result.**

- **F1, login CSRF:** confirmed and **fixed**.
- **Residual risks:** four are documented below (R-F1 to R-F4). None is silently marked resolved.
- **R-F3:** this needs an owner decision before real live use.

## 1. How access works today (map)

| Layer | Where | Applies to | Effect |
|---|---|---|---|
| Bind | `backend/server.py` | process | Binds 127.0.0.1 only. A non-loopback bind is refused unless `KITE_TERMINAL_ALLOW_REMOTE_BIND=1`. Exactly one worker; reload is off. |
| Security headers | `SecurityHeadersMiddleware` | every HTTP response | Sets a strict CSP and `frame-ancestors 'none'`. Uses `no-store` on `/api/*`, `/login` and `/callback`. |
| Host | `TrustedHostGuard` | every request and WS handshake | Allows only `localhost`, `127.0.0.1`, `::1` and any configured names. This defeats DNS rebinding. |
| Origin | `TerminalOriginGuard` | every non-GET request, and the WS handshake | Requires `X-Kite-Terminal: 1` and an Origin (or Referer) from a terminal origin. A missing Origin is refused. |
| Credential (P1-F) | `TerminalCredentialGuard` | every non-GET request, and the WS handshake | Requires this launch's `kt_session` cookie: HttpOnly, SameSite=Strict, held only as a SHA-256 digest in memory. A restart forgets it, logout revokes it, a new login rotates it. |
| Session | `require_kite` / `require_trading_account`, `require_inbox_session` | most API routes | Requires an active Kite session from today and the trading account. |
| Login | `GET /login` → Kite → `GET /callback` | top-level navigation | **New (F1):** the callback must carry this browser's pending login state. |

Route inventory, from the AST sweep in `tests/test_security_p2g.py`:

- **GET routes with a side effect:** only two.
  - `/callback`, now guarded (F1).
  - `/api/tickets/{id}/chart`. It caches a resolved chart URL on first view. This is not an economic field and not part of the P1-D digest. Informational.
- **Every other GET is read-only.** Writes (POST/PUT/DELETE) pass all three write layers before any route runs.
- **DMB inbox:**
  - Reads (`/api/dmb-inbox/bundles|imports|items`) need the trading account only.
  - Writes (import, dismiss, note, research watchlist, Start ticket, ticket link) also need the credential and a fresh Kite session.

## 2. Findings

### F1 — Login CSRF on `/callback` (Medium) — **FIXED**

- **Component:** `backend/routes/auth.py` (`/login`, `/callback`).
- **Before:** `/callback` exchanged any `request_token` it was given. The navigation is a top-level GET, so no Origin check applies.
- **Exploit:** the user's browser has the terminal open, or simply visits a hostile page. That page navigates to `http://127.0.0.1:8000/callback?request_token=<attacker's>&status=success`. The terminal then:
  - exchanged the attacker's request token;
  - replaced the user's Kite session with the attacker's;
  - disarmed LIVE (as any new login does);
  - issued this browser a fresh write credential bound to the attacker's account.
- **Preconditions:** the attacker needs a valid request token for this app's `api_key`, and the user's browser must reach the terminal. The attacker never receives the user's credential (HttpOnly, and the response goes to the user's browser).
- **Effect:** the user may afterwards act on the wrong account, and a deliberate session switch can disrupt them.
- **Evidence (before):** `test_a_forced_callback_without_a_started_login_exchanges_nothing`. Without the fix the exchange is called with `reqATTACKER123` and a `kt_session` cookie is set.
- **Fix:**
  - `/login` creates a **single-use, 10-minute login state**. It is 32 random bytes, held only as a digest in process memory (at most 8 pending).
  - The state is bound to the browser by the cookie `kt_login_state`: HttpOnly, SameSite=Lax, Path=/callback, Max-Age=600.
  - `/callback` consumes that state first. Without a pending state from this browser it exchanges nothing, issues nothing and redirects to `/?login_error=1`.
  - Every callback consumes the state: success, cancel, failure or replay.
  - Why Lax and not Strict: the callback is a top-level navigation from kite.zerodha.com, on which a Strict cookie is not sent.
- **Tests:** `tests/test_security_p2g.py`, 11 tests:
  - a forced callback is refused;
  - a callback after this browser's own login works once;
  - replay is refused;
  - a wrong or foreign state is refused;
  - an expired state is refused;
  - a cancel consumes the state;
  - a forced callback never rotates the existing credential;
  - pending states are bounded;
  - plus the pinned residuals below.
- **Deliberately changed tests:** 13 positive callback paths in `test_terminal_credential.py`, `test_csrf_protection.py` and `test_security_hardening.py` now go through `start_kite_login(client)` first, as a browser does. No negative assertion was changed.
- **Operator impact:** none. The Login button already goes through `/login`.

### R-F1 — Residual window of F1 (Low) — documented

The state binds the callback to *a login this browser started*, not to the request token Kite issues for that login. A forced callback that lands while the user's own login is still pending would be accepted. That window is at most 10 minutes, and the state is single use. This is pinned by `test_residual_a_callback_inside_a_login_this_browser_started_passes`.

**Remediation:** pass the state through Kite's `redirect_params` and require it to come back unchanged. This was **not** done today, because whether Kite echoes `redirect_params` can only be checked with a real Kite login, which is outside this sprint. Steps:

1. Log in once with a real account.
2. Confirm the callback URL carries `state=`.
3. Then make it mandatory.

### R-F2 — Reads are Host-checked only (Medium for multi-user machines, Low for a single-user PC) — documented, owner decision

- **What is exposed:** any local process can read account data with `Host: 127.0.0.1`: holdings, positions, tickets, the DMB inbox, the journal and session status. This includes other Windows accounts, WSL or containers with host networking, and the user's own other programs.
- **Not exposed:** a *foreign web page* cannot read any of it:
  - there are no CORS headers, so the browser withholds the response;
  - DNS rebinding is refused by the Host check.
- **Evidence:** `test_residual_reads_are_host_checked_only`. A GET with a foreign Origin returns 200 with no `Access-Control-Allow-Origin`, and a rebinding Host gets 400.
- **Why not fixed today:** the P1-F owner decision is that "reads (GET) are unchanged" and that the UI keeps showing data after a restart, before re-login. Requiring the credential for account-data GETs would change that operator-visible behaviour.
- **Remediation plan (owner decision required):**
  1. Gate `/api/*` reads, except a short public list (`/api/session/status`, static assets), on the same `kt_session` credential.
  2. After a restart the UI shows "log in to view account data" until the fresh login.
  3. Add a credential-gated test for each read route, mirroring `test_terminal_credential.py`.

### R-F3 — The credential cookie is host-scoped, not port-scoped (Medium) — documented, **owner decision before real live use**

- **The mechanism:** cookies have no port. A cookie set by `127.0.0.1:8000` is sent by the browser to *every* server on `127.0.0.1`.
- **Concrete exploit:** another local account can bind, say, `127.0.0.1:9999` and get the user's browser to open it (a link, or a hostile page that navigates there). It then receives `kt_session` on the next same-site request, and can replay it with forged Host and Origin from a non-browser client. That bypasses the P1-F protection against other Windows accounts.
- **Preconditions:** a live credential, and user interaction (luring the browser to the attacker's port).
- **Evidence:**
  - `tests/e2e_dmb_inbox.py`, check "evidence R-F3": headless Edge reports the credential cookie for `http://127.0.0.1:<port+1>`.
  - `test_the_credential_cookie_is_host_scoped_not_port_scoped`.
- **Mitigation already applied today:** the synthetic test profile uses its own cookie name, `kt_test_session`. The isolated test instance therefore never overwrites or uses the real terminal's `kt_session`. The runbook also says to open the test instance in a separate browser profile.
- **Remediation plan:** keep the HttpOnly cookie only as a one-time hand-off. After `/callback`, the page exchanges it once for a per-launch write token kept in `sessionStorage`. That storage is origin-scoped, port included, so other ports cannot see it. The page sends the token in a header on every write and WS handshake, and the cookie is cleared.
- **Why not done today:** this changes the P1-F design ("JavaScript cannot read it"), so it needs the owner's decision.

### R-F4 — Open WebSocket survives a credential rotation (Low) — documented

The handshake is checked (Host, Origin and credential), but a socket that is already open stays open after logout or a new login, until it closes. After logout the ticker is stopped, so it carries nothing. The stream is market ticks only, with no account data and no write capability.

**Remediation:** close all `/ws/ticks` sockets on revoke or rotation.

### Checked and found sound

- **Logout:**
  - deactivates the session;
  - clears the stored access token;
  - disarms LIVE;
  - revokes the credential and clears the cookie;
  - the old cookie is refused afterwards (`test_emergency_exit_restart.py`).
- **Restart:** the in-memory credential is gone, so old cookies are refused. This is tested both in-process and with a real process restart (acceptance stages 17–19).
- **Cookie attributes:** HttpOnly, SameSite=Strict, Path=/, Secure over https. The value is never in a URL, body, page or log.
- **Origin and Host:** the earlier `test_csrf_protection.py` and `test_security_hardening.py` suites were re-run unchanged, apart from the start-login step.
- **Network binding:** loopback only by default (`backend/server.py`, PK-18 tests).
- **DMB inbox:** no route builds a broker client. Every write passes all layers. Import names a date and a bundle id, never a path.

## 3. P1-F and P0 controls

Not weakened. The credential guard, Origin guard, Host guard and live-readiness gates are unchanged. Only `/login` and `/callback` gained a check.

## 4. Test evidence

| Suite | Result |
|---|---|
| `tests/test_security_p2g.py` | 11 passed |
| `test_terminal_credential.py`, `test_csrf_protection.py`, `test_security_hardening.py` | all passed (re-run after F1) |
| `tests/e2e_dmb_inbox.py` (headless Edge) | 31/31, including the R-F3 evidence check |
| Full Kite suite | see the sprint report |
