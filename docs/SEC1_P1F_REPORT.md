# SEC-1 P1-F: per-launch terminal credential

**Scope.** P1-F of the cross-repo audit remediation plan. No real broker,
Kite session or account database was used: FakeKite and temporary databases
only.

## Finding closed

The Host and Origin checks, and the static `X-Kite-Terminal: 1` header, stop
browser-borne attacks. Any local program can forge all three. Before P1-F,
any process that could reach the loopback port could therefore approve
tickets, change the mode or trigger Emergency Exit. That includes processes
of other Windows users, WSL and containers with host networking.

## Design (owner decisions)

- **Issued only when a Kite login completes.** `/callback` issues it after a
  successful request-token exchange, so it rests on the user's password and
  2FA. It is set as cookie `kt_session`: `HttpOnly; SameSite=Strict;
  Path=/`, plus `Secure` over https. It is never in a URL, page, static
  asset, JSON body or GET response, and JavaScript cannot read it.
- **Per launch.** It is `secrets.token_urlsafe(32)`, held only as a SHA-256
  digest in process memory (the server runs one worker). A restart forgets
  it, a new login replaces it, and logout revokes it.
- **Checked on every write and the WebSocket handshake** by a new pure-ASGI
  `TerminalCredentialGuard`, which runs after the unchanged Host and Origin
  guards. Failure is 401 `TERMINAL_SESSION_REQUIRED`, or a refused handshake
  (close 1008). The comparison is `hmac.compare_digest` on digests.
- **No leakage.** The value is registered with the log redactor, and the
  guard logs only the reason for a refusal.
- **Emergency Exit requires it too** (owner decision). A local process must
  never be able to liquidate positions. After a restart, the fallback while
  you log in again is Kite web and the protective GTTs.
- **Reads (GET) are unchanged.** They are protected by the Host check only.

### Why not a launch URL or a readable endpoint

Two ways of delivering the credential to the browser were rejected:
- **A one-time launch code in a URL** would sit in browser history and the
  process command line.
- **Any GET or page that returns the credential** would be readable by the
  very local processes it is meant to stop.

The Kite login is the one event that only the user can complete. Binding
the credential to it gives a real boundary rather than a decorative one.

## Threat model

| Stops | Does not stop |
|---|---|
| Other Windows accounts on the machine | Malware running as **the same Windows user**: it can read the browser cookie store, `.env` and the DB, or drive the browser |
| Sandboxed or less-privileged processes, containers/WSL with host networking but no access to the user's profile | A browser extension with access to this host |
| A request replayed from a previous launch, or after a logout | A debugger or an administrator |
| Forged Origin/header writes from a non-browser client | Reads (GET), which are Host-checked only, as before |
| A web page (already stopped by Origin; this is defence in depth) | Login CSRF (P2-G, no `state`): a forced callback can rotate the credential, but never hands it to the attacker |

## Changes

- `backend/security.py`: the `_TerminalCredential` store
  (issue / revoke / valid), `TerminalCredentialGuard`, cookie helpers, and the
  module documentation.
- `backend/main.py`: the guard is registered inside the Origin guard.
- `backend/routes/auth.py`:
  - `/callback` sets the cookie;
  - `/api/logout` revokes the credential and clears the cookie;
  - `/api/session/status` reports `terminal_authorized` (never the value).
- `frontend/app.js` / `index.html`:
  - A non-blocking banner, "Changes are locked … log in to Zerodha again",
    appears when Kite is connected but this browser holds no credential, or
    when a write gets `TERMINAL_SESSION_REQUIRED`.
  - The tick WebSocket is not attempted without the credential.
  - The page never reads or writes the cookie.
- `tests/demo_server.py` (demo only, never in `backend/`):
  - `GET /demo/authorize` stands in for the Kite login and issues the
    credential. This is acceptable only because the demo holds synthetic
    data and a FakeKite.
  - `GET /demo/kite-login` (`--review-controls`) mirrors `/callback`.
- Docs: `DEPLOYMENT.md` (lifecycle, threat model, restart re-login),
  `USER_GUIDE.md` (troubleshooting), `TESTING.md`.

## Tests

- **New `tests/test_terminal_credential.py`** (142 tests):
  - every mutating route × {missing, wrong, previous launch} → 401 with no
    broker write;
  - the current credential is accepted;
  - a forged Origin without the credential is refused;
  - a valid credential never overrides a foreign Origin or a missing app
    header;
  - reads need no credential;
  - WebSocket: missing or wrong credential refused, current one accepted,
    foreign Origin refused even with the credential;
  - `/callback` sets HttpOnly + SameSite=Strict (+ Secure over https) and
    never puts the value in the redirect URL;
  - a failed, cancelled or token-less callback, or a failed exchange, issues
    nothing;
  - a new login rotates the credential; a restart invalidates it; logout
    revokes it and clears the cookie;
  - status reports authorization without the value;
  - trading lock, dry-run switch, Emergency Exit and logout work with the
    credential and only with it;
  - re-authorizing after a logout restores writes;
  - no leakage into logs, bodies, HTML, `app.js` or the redirect, and the
    value is registered for redaction;
  - no frontend file names the cookie;
  - the comparison is constant-time.
- **Mutation check:** unregistering the guard fails 126 of the 142 tests.
- **Test infrastructure:**
  - `conftest` issues one credential per test (the "browser's login").
    `terminal_client` reuses it, and an autouse fixture resets it around each
    test.
  - `reauthorize(client)` stands in for a fresh login after a logout.
- **Deliberately changed tests** (positive paths only; no negative
  assertion changed):
  - 13 in `test_csrf_protection.py`: same-origin writes, loopback origins,
    Referer, and the WebSocket accepted case now also carry the credential.
  - 3 in `test_security_hardening.py`: configured allowed host, and the
    approved-host WebSocket.
  - 1 in `test_manual_ticket_review.py`: logout, then login, then
    `reauthorize`.
- **Browser** (`tests/e2e_p1d.py` check 13, demo + Edge):
  - without the credential, the UI loads read-only with the banner;
  - a same-origin write with the right headers is refused (401
    `TERMINAL_SESSION_REQUIRED`);
  - authorization sets an HttpOnly, SameSite=Strict cookie;
  - once authorized the banner is gone and `document.cookie` cannot see the
    credential.
  - All 13 checks of the script pass, every declared refusal is observed,
    and there is no other error.
  - `tests/e2e_ui.py --pin-clock` scores 53/54. The one failure is the
    pre-existing demo-data check.

## Operator impact

- After every app restart, and after logout, log in to Zerodha again from
  the browser before making any change. Reading keeps working.
- Open the terminal on the address registered as the Kite redirect URL
  (`127.0.0.1:8000`).
