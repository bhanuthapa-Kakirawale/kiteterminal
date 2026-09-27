# OPUS55 PK-5 Implementation Report: cross-origin write protection (P0-8)

| | |
|---|---|
| Packet | PK-5 only. It fixes **P0-8**. PK-6 and later packets were not started. |
| Baseline before | 212 passed, 8 xfailed (P0-5, P0-6, P0-7 and P0-8, two each), 0 failed, 0 xpassed |
| Result after | **340 passed, 6 xfailed, 0 failed, 0 xpassed** (3 identical full runs) |
| Backups taken first | tag `backup-pre-pk5` and branch `backup/pre-pk5` (both at `13ae3f8`). Tag `backup-pre-pk5-worktree` (commit `a392dd0`) is a snapshot of the whole working tree, including the uncommitted and untracked PK-0 to PK-4 files. It was made through a temporary index, so the real index, HEAD and the working tree were not touched. `backend/kite_terminal.db.pre_pk5_bak` was taken with the SQLite online-backup API; `integrity_check` = ok. |
| Committed | No. Nothing was committed, as instructed. |

## 1. Executive summary

Before PK-5, any web page open in the user's browser could approve a trade or delete a protective GTT. It only had to send a body-less "simple" POST, such as `fetch(url, {method: "POST", mode: "no-cors"})` or an auto-submitting form. Such a request needs no CORS preflight, and the server executed it.

PK-5 adds one small pure-ASGI middleware, `backend/security.py:TerminalOriginGuard`, registered once in `backend/main.py`. It runs before routing and does two things:

- **HTTP writes.** It rejects every non-GET/HEAD/OPTIONS request that either lacks `X-Kite-Terminal: 1` or carries an `Origin` (or, when Origin is absent, a `Referer`) that is not the terminal's own loopback origin.
- **WebSocket.** It applies the same Origin check to the `/ws/ticks` handshake.

A rejected request gets HTTP 403 with `error_code: CSRF_PROTECTION_FAILED` and never reaches a route.

The frontend sends the header from its single write helper, `fetchJsonMutate`. The shared test client now behaves like a same-origin browser page.

The two P0-8 xfails are now ordinary passing tests. A new file with 126 tests covers the policy matrix, and the whole write surface is tested automatically. A real browser, Edge through Playwright, confirmed the result against the demo server. The UI's own writes succeed. A page on another origin cannot confirm a ticket, cancel a GTT or open the tick socket.

No trading logic was touched.

## 2. Files changed

PK-5's own diff, measured against `backup-pre-pk5-worktree`, is exactly six files:

| File | Change |
|---|---|
| `backend/security.py` | **New**, 119 lines. It holds the policy constants, `is_terminal_origin()` and `rejection_reason()`, and the `TerminalOriginGuard` middleware. |
| `backend/main.py` | +6 lines: the import, `app.add_middleware(TerminalOriginGuard)`, and a comment explaining that the absence of CORS middleware is part of the defence. |
| `frontend/app.js` | `fetchJsonMutate` sends `"X-Kite-Terminal": "1"`, plus a one-line comment. There are no other frontend changes. |
| `tests/conftest.py` | Adds `TERMINAL_ORIGIN`, `TERMINAL_HEADERS` and `terminal_client()`. `env.client` now uses that client. |
| `tests/test_live_safety.py` | `unsafe_client` uses `terminal_client`. The P0-8 section was converted from xfail (see §11). |
| `tests/test_csrf_protection.py` | **New**, 126 tests (see §12). |

The following files were **not** touched: `execution.py`, `risk_engine.py`, `routes/tickets.py`, `order_monitor.py`, `trailing_engine.py`, `journal.py`, `strategy_engine.py` and `routes/ws.py`. This means no ticket-lifecycle, exit, trailing, GTT, strategy or portfolio code changed. `docs/OPUS55_AUDIT_REPORT.md` was not modified either.

## 3. Mutating endpoints inventoried

These were inventoried from the live app's route table, by `APIRoute.methods ∩ {POST, PUT, PATCH, DELETE}`. There are **31 routes**: 27 POST, 4 PUT and 3 DELETE. No route uses PATCH.

| Area | Routes |
|---|---|
| Tickets | `POST /api/tickets`, `POST /api/tickets/alert-action/buy`, `POST /api/tickets/alert-action/sell`, `POST /api/tickets/{id}/confirm`, `POST /api/tickets/{id}/reject`, `POST /api/tickets/{id}/revert-rejection`, `POST /api/tickets/{id}/start_trailing`, `POST /api/tickets/cancel-all-pending`, `PUT /api/tickets/{id}`, `DELETE /api/tickets/{id}` |
| Broker orders / GTT | `POST /api/orders/{id}/cancel`, `POST /api/gtts/{id}/cancel` |
| Kill switch | `POST /api/kill-switch/emergency-exit-confirm` |
| Execution | `POST /api/execution/mode` |
| Strategies | `POST /api/strategies/scan-now`, `POST /api/strategies/stop-adjustments/{id}/confirm`, `POST /api/strategies/stop-adjustments/{id}/reject`, `POST /api/strategies/backtests`, `PUT /api/strategies/{name}` |
| Automation | `POST /api/automation/run-now` |
| Risk | `POST /api/risk/settings`, `POST /api/risk/sector-mappings`, `DELETE /api/risk/sector-mappings/{symbol}` |
| Watchlist | `POST /api/watchlist`, `DELETE /api/watchlist/{id}`, `PUT /api/watchlist/{id}/tag` |
| Misc | `POST /api/instruments/refresh`, `POST /api/reconciliation/{id}/mark-reviewed`, `POST /api/ui-prefs`, `PUT /api/dashboard-panels`, `POST /api/logout` |

The plan estimated 29 routes. The route table has 31, so the report uses the measured number.

Other surfaces were reviewed as well:

- **GET routes.** No GET handler performs a broker write. `place_order`, `cancel_order` and `delete_gtt` appear only under POST handlers or their helpers. The one GET with a state change is `/callback`, covered in §7.
- **WebSocket.** `/ws/ticks` is read-only: it pushes ticks to the client.
- **Static files.** `/` and `/static/*` are GET only.
- **Frontend.** Every write goes through `fetchJsonMutate`. The only raw `fetch()` calls are three plain GETs (`fetchJson`, `API.status` and `API.config`). The `<form>`s have no `action` or `method` and are handled in JS. Login is `location.href = "/login"`, a GET. There is no `XMLHttpRequest` and no `sendBeacon`.
- **Test clients.** There were two: `conftest.env.client` and `test_live_safety.unsafe_client`.

## 4. Header policy

- **Name and value.** The header is `X-Kite-Terminal`, and the value must be exactly `1`. Values such as `0`, `true`, the empty string and `1 ` (with a trailing space) are rejected; tests cover each.
- **When it is required.** It is mandatory for every HTTP request whose method is **not** in the safe allow-list `{GET, HEAD, OPTIONS}`. Because safe methods are an allow-list, PATCH and any unknown method are guarded too.
- **Why it works.** The header is not CORS-safelisted. A cross-origin page therefore cannot attach it without a preflight OPTIONS. The app has no CORS middleware, so the preflight gets no `Access-Control-Allow-*` headers and the browser never sends the real request. A test (`test_preflight_gets_no_cors_grant`) and the browser smoke test both confirm this.
- **Why the header alone is not enough.** The header is never sufficient on its own. A foreign Origin is rejected even when the header is present (test F).

## 5. Origin / Referer policy

For a mutating HTTP request:

1. If `Origin` is **present**, it must be a *terminal origin*. A present but empty value, `null` (sandboxed iframes, `file://`) and unparsable values are all rejected.
2. If `Origin` is absent and `Referer` is present, the Referer's origin must be a terminal origin.
3. Origin takes precedence. A foreign Origin with a same-origin Referer is rejected.

A **terminal origin** must satisfy all three conditions:

- **Scheme.** It has the request's scheme, with `ws` mapped to `http` and `wss` to `https`.
- **Hostname.** Its hostname is loopback: `127.0.0.1`, `localhost` or `::1`, compared case-insensitively.
- **Port.** Its port equals the port in the request's `Host` header, with 80 or 443 as the default.

The port is **derived from the request's Host header**, not hard-coded. `127.0.0.1:8000`, `127.0.0.1:8765` (the demo server) and any `uvicorn --port` all work. `localhost:P` and `127.0.0.1:P` are both accepted for the same port P, because they address the same server. A different local port, such as another dev server on `localhost:3000`, is rejected.

**Why loopback names rather than "Origin == Host":** a naive same-host comparison is defeated by **DNS rebinding**. A page at `http://evil.example:8000` whose name resolves to 127.0.0.1 would send `Origin` and `Host` both equal to `evil.example:8000`. The loopback hostname allow-list rejects it. This is tested, and a mutation test that replaces the allow-list with "Origin host == Host host" is caught (§13).

## 6. Non-browser-client policy

A request that carries the header and has **neither Origin nor Referer** is accepted. Tests and future local API tooling can therefore write with just `X-Kite-Terminal: 1`.

The reasoning is as follows:

- Browsers always attach `Origin` to cross-origin POST, PUT and DELETE requests, including `no-cors` fetches and form submissions.
- The one way a page can end up with no Origin *and* no Referer on a write is a same-origin request made under a no-referrer policy. Even then, a foreign page still cannot add the custom header without a failed preflight.
- So a header-bearing request with no Origin cannot come from a foreign web page.

The security property is therefore: **a random browser page cannot perform the mutation.** A local process that can send arbitrary headers is a different threat, a trusted-host compromise, and it is out of PK-5's scope (§17 and §18).

A request with no Origin and **no header** is rejected (test G).

## 7. Exempted routes and rationale

**There is no path-based exemption list.** The check is keyed on method only, which keeps the rule simple and makes it impossible to forget a new route.

- **`GET /login`** redirects to Kite. It is a top-level navigation and a GET, so it is not checked. The test confirms it redirects to `kite.zerodha.com` without the header.
- **`GET /callback`** is the Kite redirect back to the app. It is a top-level navigation *from `kite.zerodha.com`*, so it carries neither the application header nor a same-origin Origin. It exchanges the request token, which is a state change over GET. Because it is a GET, the guard does not see it, and it must stay that way or Zerodha login breaks. Two tests cover it: a success redirect to `/` with a cross-site Kite Referer and no header, and the failure path, which redirects to `/?login_error=1`.
- **`GET /`, `/static/*` and `/api/session/status`** are served without the header (tested).
- **All `GET /api/*` reads** are not checked by this guard. Cross-origin *reads* are left to the browser's same-origin policy; the app never emits CORS headers (tested). See §17 for the DNS-rebinding caveat on reads.

Login CSRF on `/callback` (no `state` or nonce) was **not** addressed. It is not needed to fix P0-8 and was explicitly out of scope; see §17.

## 8. WebSocket policy

- **Where the check runs.** The check runs in the same middleware, on `websocket` scopes, **before** the route can call `accept()`. `routes/ws.py` and ticker semantics are unchanged.
- **Foreign origins.** A present `Origin` must be a terminal origin (the same rules as §5). Otherwise the middleware sends `websocket.close` with code 1008 before accepting, which the server turns into a refused handshake: HTTP 403 under uvicorn, and `WebSocketDisconnect(1008)` in the TestClient.
- **No header required.** The browser `WebSocket` API cannot set custom headers, so the application header cannot be required here.
- **Absent Origin.** An absent Origin is accepted. Browsers always send Origin on a WebSocket handshake, so an absent one means a non-browser client, and the socket is read-only market data.

The tests cover four foreign origins, which are refused. They also cover same-origin, the `localhost` alias for the same port, and no-Origin, which are all accepted and receive a pushed tick. In the real browser, the UI's socket opened and the attacker page's socket was refused.

## 9. Frontend changes

There is one change: `fetchJsonMutate` in `frontend/app.js` now sends `headers: {"Content-Type": "application/json", "X-Kite-Terminal": "1"}`.

Every legitimate write goes through that helper, as the §3 review showed, so no other call site needed changing, and no networking refactor was done. `test_frontend_mutations_send_the_application_header` keeps this true. It asserts that the helper sends the header, that every raw `fetch()` outside the helper is a bare `fetch(url)` with no options (so it cannot carry a method), and that there is no XHR and no `sendBeacon`.

**Operational note.** A browser tab that was opened *before* the upgrade still runs the old `app.js`. Its writes will be refused with the message "Request rejected: not from the terminal UI." until the page is reloaded. `StaticFiles` serves with ETags, so a normal reload picks up the new file.

## 10. Test-fixture changes

- **`TERMINAL_ORIGIN` and `TERMINAL_HEADERS`.** `tests/conftest.py` gains `TERMINAL_ORIGIN = "http://127.0.0.1:8000"` and `TERMINAL_HEADERS = {"X-Kite-Terminal": "1", "Origin": TERMINAL_ORIGIN}`.
- **`terminal_client(app, **kw)`.** This new helper returns `TestClient(app, base_url=TERMINAL_ORIGIN, headers=TERMINAL_HEADERS, **kw)`. The client addresses the terminal at a real loopback origin and sends exactly what a same-origin page running `app.js` sends on a write.
- **Who uses it.** `env.client` and `unsafe_client` both use it now. **No individual test call was edited to add the header.** About 220 existing POST, PUT and DELETE calls inherit it.
- **Security tests.** These build a *bare* `TestClient` so they can omit or alter exactly one input. The defaults were not relaxed to make anything pass. The mutation tests in §13 show that dropping the header from the fixture fails 182 tests.

## 11. Tests converted from xfail

| Test | Before | After |
|---|---|---|
| `test_live_safety.py::test_p0_8_cross_site_post_is_rejected[confirm]` | xfail (strict) | **pass** |
| `test_live_safety.py::test_p0_8_cross_site_post_is_rejected[gtt_cancel]` | xfail (strict) | **pass** |

The `@_xfail` marker was removed. The test now sends the request through a bare client with no application header, `Origin: https://evil.example` and `Content-Type: text/plain`, which is exactly what a foreign page's simple request carries. It was also **strengthened**. It now requires all of the following:

- a 403 with `CSRF_PROTECTION_FAILED`, where the old check accepted any status of 400 or above;
- no `place_order` or `delete_gtt` call;
- the ticket still `pending_confirmation`, in the confirm case;
- the GTT still `active`, in the gtt_cancel case.

`test_p0_8_json_endpoints_reject_text_plain_body` was an "already safe" test that asserted 422 for a cross-site `text/plain` body. The middleware now answers such a request with 403 first. The test was adjusted to keep its original purpose while asserting more:

- A request that **passes** the guard but sends a `text/plain` body still gets 422 from FastAPI's JSON layer. That layer is kept as an independent second barrier.
- The cross-site form of the same request now gets 403.
- No order is placed in either case.

## 12. New security tests

`tests/test_csrf_protection.py` holds 126 tests.

| Req. | Test(s) | Asserts |
|---|---|---|
| inventory | `test_every_mutating_route_rejects_cross_origin` (31 routes × 2 = 62), `test_inventory_covers_post_put_delete`, `test_unknown_mutating_method_is_rejected` | **Every** mutating route in the live route table, including any route added later, returns a CSRF 403 to a foreign Origin, whether or not the header is present, and no broker write happens. PATCH is also guarded. |
| A | `test_a_cross_origin_confirm_places_nothing` | A live-mode confirm from a foreign Origin: 403, no `place_order`, ticket unchanged, no `kite_order_id`. |
| B | `test_b_cross_origin_gtt_cancel_leaves_protection` | 403, no `delete_gtt`, GTT `active`. |
| — | `…order_cancel_leaves_order`, `…stop_adjustment_confirm_leaves_stop`, `…cancel_all_pending_leaves_tickets`, `…logout_keeps_session`, `…triggers_do_not_run[scan-now, run-now, instruments/refresh]` | The rest of the audit's P0-8 list: 403, and the broker call, stop, ticket, session or handler is untouched. The handlers were spied on to prove they never ran. |
| C | `test_c_same_origin_without_header_is_rejected`, `test_c_wrong_header_value_is_rejected` (×4) | Same origin without the header → 403, ticket unchanged. Wrong header values → 403. |
| D | `test_d_same_origin_with_header_behaves_normally`, `test_d_same_origin_put_and_delete_behave_normally`, `test_route_errors_pass_through_unchanged` | Dry-run confirm → `executed`. PUT on a draft and DELETE (archive) → 200. The route's own 404 is not masked. |
| E | `test_e_loopback_origins_are_accepted` (×8), `test_e_non_terminal_origins_are_rejected` (×10), `test_e_is_terminal_origin` (×8) | **Accepted:** 127.0.0.1 and localhost on the same port in either direction, a custom port (8765), default ports, case-insensitive names, and `[::1]`. **Rejected:** another port, an https/http mismatch, `null`, empty, garbage, a LAN IP, a look-alike host, DNS rebinding, and a missing Host. The `[::1]` case is checked on the function because Starlette's TestClient cannot address an IPv6 host. |
| F | `test_f_foreign_origin_with_header_is_still_rejected` | Correct header plus a foreign Origin → 403, no order. |
| G | `test_g_no_origin_with_header_is_accepted`, `test_g_no_origin_without_header_is_rejected`, `test_g_referer_is_checked_when_origin_is_absent`, `test_g_origin_takes_precedence_over_referer` | The §6 policy and the Referer fallback. |
| — | `test_reads_are_not_blocked_by_the_guard`, `test_preflight_gets_no_cors_grant` | GETs work, and neither a GET response nor an OPTIONS preflight ever carries a CORS grant. |
| H | `test_h_websocket_foreign_origin_is_refused` (×4) | Close code 1008 before accept. |
| I | `test_i_websocket_terminal_origin_is_accepted` (×3) | Same-origin, the localhost alias and no-Origin connect and receive a tick. |
| J | `test_j_login_redirects_without_header`, `test_j_kite_callback_navigation_is_not_blocked`, `test_j_callback_failure_path_is_unchanged`, `test_j_frontend_is_served_without_header` | The login and callback flow is intact. |
| frontend | `test_frontend_mutations_send_the_application_header` | See §9. |

## 13. Mutation-test results

Each mutant was applied to the file, the tests were run, and the file's original bytes were restored in a `finally` block. SHA-256 hashes of `security.py`, `conftest.py` and `app.js` were verified afterwards (all OK). `main.py`'s diff was confirmed to be only the intended 6 lines.

| Mutant | Tests failing | Examples |
|---|---|---|
| Header check removed | 6 | `test_c_*` (×5), `test_g_no_origin_without_header_is_rejected` |
| Origin/Referer check removed (HTTP) | 45 | every `…[foreign_origin_with_header-…]` inventory case, F, E-rejected, G-referer |
| WebSocket Origin check removed | 4 | all `test_h_websocket_foreign_origin_is_refused` cases |
| Loopback allow-list replaced by "Origin host == Host host" | 5 | DNS-rebinding case, localhost↔127.0.0.1 aliases, WS alias |
| Middleware not registered | 98 | whole inventory, A, B, F, C, H, both converted P0-8 tests |
| Header dropped from the test-fixture defaults | 182 | ordinary tests across `test_alert_action`, `test_baseline_api`, `test_live_safety` and `test_review_changes` |
| Header dropped from `app.js` `fetchJsonMutate` | 1 | `test_frontend_mutations_send_the_application_header`. The browser smoke test (§15) independently checks that every UI write carried the header. |

Every mutant was killed, and all code was restored.

## 14. Full-suite results

| Run | Result |
|---|---|
| Full suite, run 1 | 340 passed, 6 xfailed (90.3 s) |
| Full suite, run 2 | 340 passed, 6 xfailed (91.2 s) |
| Full suite, run 3 | 340 passed, 6 xfailed (91.0 s) |
| Security-related selection (`test_csrf_protection.py` + `test_live_safety.py`), 10 runs | 225 passed, 6 xfailed, every run |
| Final full run after the report (see note below) | 340 passed, 6 xfailed |

There were 0 failures and 0 XPASS in every run. The count is 340 = 212 baseline + 2 converted P0-8 tests + 126 new tests. No test reached Zerodha or the network; `get_kite` is the FakeKite throughout.

The final full run followed the one change made after the mutation tests: `security.py`'s log line was reworded to "CSRF guard blocked …", because the old "Blocked cross-origin …" wording was misleading for missing-header rejections.

## 15. Browser smoke-test result

This ran against `tests/demo_server.py --port 8765`, which uses FakeKite and a throwaway database, with no Zerodha connection. Playwright drove Microsoft Edge headless. The script is a scratch file and was not added to the repo.

**Custom PK-5 smoke test: 10/10 checks passed.**

Checks on the real UI:

- The UI's tick WebSocket opened (`ws://127.0.0.1:8765/ws/ticks`).
- Real UI clicks worked:
  - the theme toggle (`POST /api/ui-prefs`) returned 200;
  - approving a signal in the Approvals inbox (`POST /api/tickets/3/confirm`) returned 200, as a dry-run execution.
- Every UI write carried `X-Kite-Terminal: 1`, and none was rejected.

A page served from `http://127.0.0.1:8799`, which is a different origin, then tried four attacks:

| Attack | Outcome |
|---|---|
| `no-cors` POST to `/api/tickets/4/confirm` | server 403 |
| `no-cors` POST to `/api/gtts/9001/cancel` | server 403 |
| Auto-submitted `text/plain` form POST to confirm | server 403 |
| `fetch` with the custom header | blocked by the browser: "TypeError: Failed to fetch", a failed preflight, never sent |
| `new WebSocket(.../ws/ticks)` | refused |

After the attacks, the targeted ticket was still `pending_confirmation` and the targeted GTT was still `active`. The terminal still accepted its own writes afterwards.

**The existing end-to-end script `tests/e2e_ui.py`: 55/55 checks passed** on a fresh demo server. It drives approve, reject, stop-adjustment approve, scan-now, theme persistence, autopilot run-now and the emergency-exit gating through the UI. The server log showed **zero** guard rejections during that run, so no legitimate UI flow is affected.

## 16. Remaining P0 xfails

| Finding | Tests | Packet |
|---|---|---|
| P0-5 | `test_p0_5_oco_stop_or_target_fill_closes_and_journals_position[stop, target]` | PK-6 |
| P0-6 | `test_p0_6_triggered_gtt_without_fill_keeps_position_open[unfilled, partial]` | PK-7 |
| P0-7 | `test_p0_7_unfilled_exit_keeps_protection_and_position_open[limit_market_hours, market_amo_after_hours]` | PK-8 |

That makes 6 xfails, all strict, and none XPASS.

## 17. Known limitations

- **Local processes are trusted.** Any process on the machine can send the header and omit the Origin. PK-5 protects against *browser pages*, not against malware on the host. Real authentication is PK-18.
- **Reads under DNS rebinding.** The guard does not check GET requests or the `Host` header. A rebinding page (for example `evil.example:8000` resolving to 127.0.0.1) is same-origin *to itself*, so it could **read** GET responses such as holdings, tickets and GTT ids. It still cannot write, and that is tested. Rejecting non-loopback `Host` headers on every request would close this. It was not done here because P0-8 concerns writes, and Host validation belongs with the PK-18 deployment and host policy. This is recommended as the first PK-18 item.
- **Login CSRF on `/callback`.** There is no `state` or nonce, so a site could, in principle, log the terminal into another Kite session. This is low impact locally, it was already noted in the audit's Security Assessment, and it is deferred.
- **Loopback-only writes.** Opening the UI through a LAN IP or any non-loopback hostname (for example `http://192.168.1.10:8000`) will have its **writes refused**. This is deliberate for the single-user local terminal. A remote or reverse-proxy deployment needs a configured allowed-origin list, handled in PK-18.
- **HTTPS behind a proxy.** Behind a TLS-terminating reverse proxy, the ASGI scheme would be `http` while the browser Origin is `https`, and writes would be refused. The deployment will need `uvicorn --proxy-headers` or an explicit configured origin (PK-18).
- **No-Origin WebSocket.** A non-browser local client with no Origin can subscribe to ticks. That data is read-only.
- **Stale tabs.** A tab opened before the upgrade gets 403 on writes until it is reloaded (§9).
- **IPv6 is tested only on the function.** The `[::1]` form is verified through `is_terminal_origin()` rather than end to end, because Starlette's TestClient cannot address an IPv6 host.

## 18. Cloud-deployment security items intentionally deferred to PK-18

The following are deferred to PK-18:

- **Authentication.** Real authentication on every route and on the WebSocket: an HttpOnly, Secure, `SameSite=Strict` session cookie, and ideally network restriction through an SSH tunnel, WireGuard or Tailscale. The middleware slot in `main.py` is where an auth middleware would be stacked. `TerminalOriginGuard` is independent of it and stays useful as defence in depth.
- **Host header validation.** An allow-list for `Host`, which closes the DNS-rebinding read gap.
- **Configurable origins.** A configured allowed-origin list for non-loopback deployments, and proxy-header handling for TLS termination.
- **TLS.** TLS through a reverse proxy.
- **Login CSRF.** A `state` parameter on the Kite login round trip.
- **The audit's remaining secrets hardening (P1-12):**
  - drop the plaintext token column;
  - move the key location;
  - add SRI to CDN scripts;
  - remove `--reload` from the docs.

## 19. Interactions discovered with PK-6, PK-7 and PK-8

- **PK-6 and PK-7** change background loops in `trailing_engine.py` and `reconciliation.py`. Those are not HTTP requests and are unaffected by the guard.
- **Tests that drive those packets over HTTP** will get the header automatically through `env.client` and `unsafe_client`.
- **New routes in PK-8.** PK-8 (the exit lifecycle) touches `routes/tickets.py` and `app.js`. Any new endpoint it adds is protected automatically, because the check is keyed on method rather than path. The parametrised inventory test also picks up any new route with no edit.
- **New UI actions in PK-8.** Any new UI action must go through `fetchJsonMutate`. `test_frontend_mutations_send_the_application_header` fails if a raw `fetch()` with options is added instead.
- **Security tests that send writes.** A future security test that deliberately sends a write through `env.client` will carry the header and the same-origin Origin. To test rejection, it must build a bare client, as `test_csrf_protection.py` does.

No PK-5 test exposed a bug in unrelated trading code.

## 20. Deviations from the audit recommendation

| Audit said | PK-5 did | Why |
|---|---|---|
| Reject non-GET unless it has the header **and** an Origin/Referer matching the server's host | The header is always required. Origin/Referer, **if present**, must match. **Absent** Origin and Referer is accepted when the header is present | This is the explicitly recommended PK-5 policy (§6). It keeps tests and local tooling workable without weakening the browser guarantee, because browsers always send Origin on cross-origin writes. |
| Match "the server's own host" | Matches the scheme, a **loopback hostname allow-list**, and the Host header's **port** | A naive host match is defeated by DNS rebinding. Deriving the port from Host supports any `--port`. |
| Put the WS Origin check in `routes/ws.py` | Put it in the same central middleware, on `websocket` scopes | One place for the policy. `ws.py` and ticker semantics stay unchanged, and the socket is refused before `accept()`. |
| "any non-GET" | Also treats HEAD and OPTIONS as safe | Both are read-only by HTTP semantics. OPTIONS must pass for the preflight to be answered, and the answer carries no CORS grant, so the preflight fails. |
| Tests: "cross-origin POST → 403; existing tests with header" | Did both, and added an automatic whole-route-table test, WebSocket, login and callback tests, a frontend contract test, mutation tests and a real-browser attack page | For stronger evidence. |
