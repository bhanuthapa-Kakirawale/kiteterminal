# DMB → Kite: operational runbook

**Status (2026-11-04).** Software readiness work is complete for synthetic integration and FakeKite operation. **Real-data handoff is BLOCKED** (source rights, `docs/DMB_KITE_SOURCE_RIGHTS_MATRIX.md` §5). **Real login is unconfirmed** (R-F1 needs one user-operated login, §1.6). **Live broker execution is NO-GO**: the real broker write path, including GTT and Emergency Exit, has never been validated, and no non-executing, broker-approved method can validate the terminal's actual order path (§7.2). Decisions: §7.

This runbook changes no operating decision. It records how to run, restart, exit and recover, and what has and has not been tested.

## 0. Validation status

| Area | pytest (FakeKite, scratch DBs) | Isolated processes / headless Edge | Real broker / real login |
|---|---|---|---|
| DMB export → Kite import, inbox actions, Start ticket | yes | yes (acceptance, browser) | n/a (no broker) |
| Synthetic isolation (test profile vs production) | yes | yes | n/a |
| P1-D review, token binding, dry-run approval | yes | yes | **not tested** |
| Positions broker / fill display | yes | yes (paper broker) | **not tested** |
| Post-claim tampering audit | yes | yes | **not tested** |
| Restart / logout / fresh login, credential invalidation | yes | yes (real process restart) | **not tested** (real Kite login) |
| P2-G R-F1 login binding (state via `redirect_params`; no cookie-only fallback since 2026-11-04) | yes | n/a | **one user-operated real login needed to confirm Kite echoes it (§1.6)** |
| P2-G R-F2 / R-F3 / R-F4 (reads, port isolation, WS revocation) | yes | yes (incl. a real second server on another port) | n/a |
| Session expiry: absolute lifetime + inactivity timeout, WS closed at expiry (2026-11-04) | yes (26 tests, mutation-checked) | browser runners unaffected | n/a |
| Read-only broker switch `KITE_TERMINAL_BROKER_WRITES=disabled` (2026-11-04) | yes (every write refused before any request) | — | **to be used in §1.6** |
| Emergency Exit | yes (6 scenarios) | yes (FakeKite) | **NOT VALIDATED** |
| Calendar session semantics, DMB/Kite parity, packet expiry (every official session counts; the packet names its review session, 2026-11-04) | yes (761 days: review sessions, expiry instants, and the named review session at 18 creation instants per day, 0 differences) | — | n/a |
| Real DMB evidence handoff | — | — | **BLOCKED by source rights** |
| 2027 sessions | fail-closed tested | — | **BLOCKED: NSE 2027 circular not published** |

## 1. Normal startup

### 1.1 DMB Private Desk (optional, read-only research)
- From `D:\Claude_WS\daily_byte`: `scripts\run_private_desk.bat`, which serves `http://127.0.0.1:8765` (loopback only, GET only).
- The desk never writes DMB stores and never uploads. **Do not run DMB production pipelines or scheduler jobs as part of Kite operation.**
- Note: the desk is "another local web server on 127.0.0.1". The browser sends it Kite's cookie (cookies have no port), but since P2-G that cookie is useless without the terminal token, which the desk cannot read (§6).

### 1.2 Kite Terminal (real terminal, dry run)
1. From `D:\Claude_WS\kite-terminal`: `.venv\Scripts\python.exe -m backend.server`. It serves `127.0.0.1:8000` with one worker, no reload, loopback only. Check the startup "Security summary" line. The Host header must name port 8000 exactly.
2. Open `http://127.0.0.1:8000/` in **your normal browser profile** (the one you will keep using; the terminal token lives in that profile's storage for this origin).
3. Before login you see **"Log in to unlock this terminal"**. No account data is shown and nothing can be changed.
4. Press **Log in to Zerodha** → Zerodha password and 2FA → back to the terminal.
   - **First real login (R-F1 confirmation):** follow §1.6. If you land on `/?login_error=1`, **stop**. There is no fallback: the former `KITE_TERMINAL_LOGIN_STATE_ECHO=cookie-only` switch is ignored (owner decision, 2026-11-04).
5. Verify the session **in the UI**: the header shows connected with your name, the "Log in to unlock" gate is gone, and execution mode is **DRY RUN**. (Opening `/api/session/status` in the address bar shows `terminal_authorized: false` by design: the address bar does not send the terminal token header.)

### 1.3 Handoff directory
- Kite imports only from `KITE_DMB_HANDOFF_DIR`, listing `<dir>\<YYYY-MM-DD>\<64-hex>.json`. Unset means import is off.
- DMB writes bundles only on an explicit `python -m private_desk.handoff export --session D` into `output\private_desk\handoff\<D>\`. **For real data this is RIGHTS_BLOCKED (exit 20) and writes nothing.** Only the synthetic fixture can be exported today (§1.5).

### 1.4 Freshness, import and the inbox (synthetic bundles today)
1. **Freshness (owner rule, 2026-11-04; explicit review session):** `session_date` is a completed **regular** NSE session. The packet **names** its `review_session_date`: the first official session after `session_date` (special sessions included) that was still open, or yet to open, when the packet was created. It expires at **that session's official close**. Special sessions (Budget weekend, Muhurat) never describe a packet, but they **do end** the window of a packet created before they close: Friday 2026-11-06's packet built on Saturday expires at the Muhurat close, 15:30 IST on Sunday 2026-11-08. The same packet built after that close names Monday 2026-11-09 and is eligible until Monday 15:30. A packet naming a session already closed when it was built, or built after a later regular session closed, is stale (`STALE_EVIDENCE`). Any other disagreement is `REVIEW_SESSION_MISMATCH` (`docs/NSE_CALENDAR_MONITORING.md` §5). Packets needing 2027 are refused (`CALENDAR_UNCOVERED`).
2. **Import:** DMB Research Inbox → pick the listed bundle → Import. The whole bundle is validated first. Result `IMPORTED`, `DUPLICATE` or `REJECTED <code>`.
3. **Review each item:** provenance, evidence time, quality, rights, and the SYNTHETIC label.
4. **Inbox actions:** Dismiss, Add note, Research watchlist. The research watchlist is separate from the strategy scan universe.
5. **Manual ticket:** Start ticket prefills only symbol, exchange and rationale. Side, order type, product, quantity, entry, stop and targets are typed by you; the form refuses to submit without them.
6. **P1-D review:** the approval dialog shows the review; approve from it (it echoes the token it showed). If the review is not current (after 09:15, after 15:30, or after a new login), use **Re-review**.
7. **Dry-run verification:** the approved ticket shows mode `dry_run`. Positions shows "Paper broker / fill: accepted · awaiting fill", separately from the fill. Nothing reaches the broker.

### 1.5 Isolated synthetic test instance (never the real terminal)
`.venv\Scripts\python.exe tests\acceptance_dmb_kite.py --serve-only` → `http://localhost:8790/demo/authorize` in a **separate browser profile** (e.g. Edge InPrivate). FakeKite, throwaway DB, LIVE impossible, its own cookie name `kt_test_session`. See `docs/DMB_KITE_ACCEPTANCE_TEST.md`. **Do not run the automated acceptance runner while this instance is up**: the runner resets the scratch environment.

### 1.6 R-F1: user-operated real-login verification (read-only)

Purpose: observe, once, that Kite returns this browser's login state (`kt_state`) on the callback, and that the session, logout and restart behave as tested. **Only you operate the browser and type credentials, on kite.zerodha.com only.** Nobody else needs, or should be given, your password, PIN, TOTP, API secret, request token or access token. No assistant or script will ask for them; if anything does, stop. Do not paste URLs from the login flow anywhere: the callback URL carries a single-use request token. Do not share screenshots of the callback address bar or of `logs\kite_terminal.log` lines around the callback.

**Why the broker-write switch (2026-11-04 review).** Pressing nothing is not enough to make this read-only. A real login starts the terminal's background workers. The trailing engine, order monitor, position-quantity sync and strategy breakeven can modify or delete **GTTs at the broker** for any **live-mode** position they find. The LIVE-0 audit (2026-09-30) found no such position: the only live tickets were four orders Kite rejected (*"No IPs configured for this app"*), so no GTT was ever placed. That is a record, not a guarantee, and the account database is not inspected for this procedure. `KITE_TERMINAL_BROKER_WRITES=disabled` makes the guarantee in code. Every REST client the process builds can then log in and read, but every order, GTT, cancel, modify, conversion or token-invalidation call is refused inside the process before anything is sent (`tests/test_broker_writes_switch.py`). A refused write is recorded as a definite rejection, never as an UNKNOWN outcome.

Preconditions: a trading day or not, it does not matter (no order is involved). The Kite developer console redirect URL is `http://127.0.0.1:8000/callback`. `.env` contains no `KITE_TERMINAL_LOGIN_STATE_ECHO` (it would be ignored anyway). LIVE is not armed. Take a backup first: `.venv\Scripts\python.exe -m backend.db_maintenance backup` (the app may be stopped or running). Optionally, in Kite web, look at GTTs and Positions first: anything you did not place by hand there means a live position exists, so stop and ask for a review before continuing.

1. Start, in a PowerShell window in `D:\Claude_WS\kite-terminal`: `$env:KITE_TERMINAL_BROKER_WRITES = "disabled"; .venv\Scripts\python.exe -m backend.server`. The startup "Security summary" must name `127.0.0.1:8000` **and end with `broker_writes=DISABLED`**. If it says `enabled`, stop (Ctrl+C) and start again with the variable set.
2. Open `http://127.0.0.1:8000/` in your normal profile. Expect **"Log in to unlock this terminal"** and no account data.
3. Press **Log in to Zerodha** and complete the login on kite.zerodha.com.
4. **Pass:** you land on the dashboard at `http://127.0.0.1:8000/` (the `#kt_token` fragment is removed at once), the header shows connected with your name, and execution mode is **DRY RUN**.
   **Fail:** you land on `/?login_error=1`. Open `logs\kite_terminal.log` and look for *"Kite callback refused: not the pending login started from this browser"*. In the browser history, look at the `/callback?...` entry and note **only** whether a parameter named `kt_state` is present (yes or no). Do not copy the URL. Stop the terminal. R-F1 stays open and real-account operation stays NO-GO. Do not look for a workaround.
5. Read-only checks (pass only): Holdings, Positions and Funds/margins load; quotes show timestamps; Diagnostics shows the Kite session VALID; Reconciliation findings are listed. **Do not** approve, execute or cancel anything, place or change a GTT, press Emergency Exit, or arm LIVE.
6. Logout: press Log out. Expect the locked gate; the price feed stops (socket closed 4401).
7. Restart: log in again, stop the server (Ctrl+C), start it again **in the same window, so the variable stays set** (check `broker_writes=DISABLED` again) and reload. Expect "Log in to unlock this terminal" with no account data until a fresh login.
8. Optional, session expiry (closes P2-G §4 item 8): with `$env:KITE_TERMINAL_SESSION_IDLE_MINUTES = "2"` also set before starting, log in and leave the page untouched for about 3 minutes. Expect the locked gate with *"Your terminal session expired after 2 minutes without activity"* and the price feed stopped. Log in again to continue.
9. If any log line reads `event=broker_write.refused`, note its `route` (not the surrounding lines) in the Notes column: a background worker tried a broker write and the switch stopped it. Then ask for a review before any non-read-only run.
10. Stop the server and close that PowerShell window (the variable lives only in that window). Record the result below.

| Date | Operator | `broker_writes=DISABLED` shown? | Landed on dashboard? | `kt_state` present on callback? | Logout locked? | Restart locked? | Expiry locked? (optional) | `broker_write.refused` lines? | Notes |
|---|---|---|---|---|---|---|---|---|---|
| — | — | — | not yet performed | — | — | — | — | — | — |

## 2. Restart and login behaviour (verified)

| Event | What happens | How to verify |
|---|---|---|
| **Kite Terminal restarts** | The per-launch credential (cookie digest + terminal token digest) exists only in memory, so it is gone. **Account data is hidden** (P2-G R-F2) and **every write is refused** (`TERMINAL_SESSION_REQUIRED`) until a fresh login, Emergency Exit included. Worker and reconciliation state start empty. | Screen "Log in to unlock this terminal"; `/api/session/status` → `terminal_authorized: false`, `user_name: null` |
| **Session expiry** (2026-11-04) | After `KITE_TERMINAL_SESSION_IDLE_MINUTES` (default 30) without user activity, or `KITE_TERMINAL_SESSION_ABSOLUTE_MINUTES` (default 480) after the login, whichever is first: the credential is revoked server-side, **open price WebSockets close (4401)**, every read and write is refused, and the page shows "Log in to unlock this terminal" with the reason. Background polling and ticks do not count as activity; nothing extends the absolute limit. The Kite session, LIVE arming and broker-side GTTs are untouched (as after a restart). | Locked gate with "expired after N minutes…"; `/api/session/status` → `session_expired` for that browser |
| **Logout** | Session deactivated, stored access token cleared, LIVE back to dry run, credential revoked, cookie cleared, terminal token dropped from the page, **open price WebSockets closed (4401)**. A credential alone is not enough without a Kite session (`NO_SESSION`). | Connect gate; `connected: false` |
| **Fresh login** (`/login` → Zerodha → `/callback`) | The callback must carry this browser's pending state, echoed by Kite. A new cookie and terminal token are issued; the old ones die; sockets opened under the old credential are closed. LIVE must be re-armed through Live Readiness. | Dashboard; `terminal_authorized: true` |
| **Login from another browser/tab** | It replaces this browser's credential: this page's socket closes (4401) and the "Changes are locked" banner appears. | Banner; feed shows stale |
| **Existing inbox items after restart** | Unchanged in the database; visible again after the fresh login. Expired ones become "research only" (`EXPIRED`); Start ticket is refused for them. | Item reasons |
| **Open tickets after restart** | Unchanged. A pending manual ticket's P1-D review is invalidated by the new login ("needs re-review"); approve only after **Re-review**. Executed tickets and their GTTs continue at the broker. | Approvals queue |

Why a fresh login before any write: the credential rests on your Kite password and 2FA. Nothing else can prove to a restarted process that the browser is yours (owner decision SEC-1 P1-F, unchanged).

## 3. Emergency Exit

**Decision unchanged:** after a restart or logout, a fresh Kite login is required before any write, including Emergency Exit.

Tested with FakeKite and scratch databases (`tests/test_emergency_exit_restart.py`, `tests/test_emergency_exit_isolation.py`, acceptance stage 19, P1-F sweep):

| Case | Result (FakeKite) |
|---|---|
| While authenticated | One exit per **live** open position; typed `EXIT ALL` required; dry-run positions untouched; per-position results |
| After restart, before login | **401 `TERMINAL_SESSION_REQUIRED`, nothing sent.** The preview is refused too (reads locked). Use a fallback (§4). |
| After logout, cookie only | 401 (`TERMINAL_SESSION_REQUIRED`; with a credential but no Kite session, `NO_SESSION`), nothing sent |
| After a fresh login | Exits submitted |
| Critical reconciliation findings (e.g. `GTT_MISSING`) | **Not held back**: exits submitted, findings reported |
| Unhealthy / not-running workers | **Not held back** |
| Calendar coverage missing (2027 clock) | **Not held back**: only new entries fail closed |
| Cookie without the terminal token (e.g. collected by another port) | 401, nothing sent |
| Wrong confirmation phrase | 400, nothing sent |
| A post-claim tampered exit row | That exit refused (`TICKET_TAMPERED_AFTER_CLAIM`); the others continue |

**Real-broker Emergency Exit behaviour has NOT been validated.** Real broker validation is outside this sprint and needs separate authorization.

## 4. Fallback (when the terminal cannot exit)

1. **Broker-side stop orders.** A position opened with "Protective GTT" (the default) has a stop-loss GTT at Zerodha. It survives terminal restarts, crashes and logouts. Positions shows each position's protection status; a position without one needs a manual stop in Kite.
2. **Kite web / app.** Log in at kite.zerodha.com → Positions/Holdings → Exit; Orders → cancel open entries. Then **GTTs**: delete or adjust the protective GTT of anything exited by hand, or it can trigger against shares you no longer hold.
3. **Terminal unavailable: manual procedure.**
   1. Exit in Kite web.
   2. Write down every order id.
   3. When the terminal is back, log in.
   4. Reconciliation flags positions closed outside the app; review each finding.
   5. Never resubmit an order whose outcome is UNKNOWN; check it in Kite first.
4. **Zerodha dealing desk (call-and-trade)**: last resort if both the terminal and Kite web are unavailable.

## 5. Recovery

**Never use or modify the real account database (`backend\kite_terminal.db*`) for tests.** All tests and runners use throwaway databases, and the acceptance runner fingerprints the real files (size + mtime, never opened) before and after.

| Situation | Procedure |
|---|---|
| **Test scratch reset** | `.venv\Scripts\python.exe tests\acceptance_dmb_kite.py --reset`. It deletes only a folder carrying the `.dmb_kite_acceptance` marker (`%TEMP%\dmb_kite_acceptance`). The browser runner's folder is `%TEMP%\dmb_kite_browser`. Stop any `--serve-only` instance first. |
| **Failed import** (`REJECTED <code>`) | Nothing was stored; the refusal is recorded with its code (`docs/DMB_RESEARCH_INBOX.md`). Calendar codes (`INVALID_SESSION_DATE`, `REVIEW_SESSION_MISMATCH`, `CALENDAR_UNCOVERED`) mean the bundle's sessions disagree with Kite's calendar: check both calendars (parity test), never edit the bundle. Integrity codes (`PACKET_ID_MISMATCH`, `BUNDLE_ID_MISMATCH`, `CORRUPT_JSON`, `FILE_SUBSTITUTION`, `PATH_REFUSED`) mean the file was altered or replaced: delete nothing in Kite, re-export from DMB into a clean folder. `RIGHTS_NOT_APPROVED` is expected for any real bundle (§7 C). |
| **Duplicate bundle** | Importing the same packets again returns `DUPLICATE` and adds nothing. DMB's export is immutable: re-exporting the same session gives the same `bundle_id` (`ALREADY_EXISTS`); a different file at that name is `INTEGRITY_ERROR`. Never overwrite a bundle by hand. |
| **Stale packet** | After its named review session's official close an item stays visible as "research only" (`EXPIRED`) and cannot start a ticket. An already-expired bundle, or one naming a session that had closed when it was built, is refused at import (`STALE_EVIDENCE`). Wait for the next export; DMB names the right session from the creation time. Never change dates. |
| **Database backup** | App running: `.venv\Scripts\python.exe -m backend.db_maintenance backup` (online backup → `kite_terminal.db.manual_<time>_bak`). Check with `... integrity` and `... status`. Every migration also leaves a verified `pre_schema_v<N>_bak`. Backups are secret (older ones contain plaintext daily tokens). |
| **Database restore** | Stop the terminal. Keep the current file aside (rename it, do not delete it). Copy the chosen `_bak` to `kite_terminal.db`. Run `... integrity`, then `... migrate` (idempotent; a second run must report no migrations and no row changes). Start, log in, and review reconciliation findings before any action. Restoring loses everything after the backup: check open orders and GTTs in Kite web first. |
| **Application restart and authentication recovery** | Restart `python -m backend.server` → log in to Zerodha → verify the UI is unlocked (§1.2 step 5) → Re-review pending manual tickets → re-arm LIVE only through Live Readiness (NO-GO, §7). If login keeps failing with `login_error=1`, stop and see §1.6 (R-F1; there is no fallback). If the UI stays locked after login, clear this site's storage for `127.0.0.1:8000` and log in again (a stale terminal token is harmless but useless). |

## 6. Security notes for operators

- Use one browser profile for the real terminal and a **separate** profile for the isolated test instance.
- Other local web apps (DMB's desk, dev servers) receive the terminal's cookie, because cookies have no port, but cannot read the terminal token. Optional hardening: serve the terminal under a dedicated host name, e.g. `KITE_TERMINAL_ALLOWED_HOSTS=kite-terminal.localhost` with the Kite redirect URL on that name (whether Kite Connect accepts a `.localhost` redirect URL is unverified). Then the generic `127.0.0.1:*` and `localhost:*` servers never even receive the cookie.
- Not protected: malware running as your Windows user, browser extensions with access to the terminal's page, administrators/debuggers (`docs/SECURITY_P2G_AUDIT.md` §4).

## 7. Operational use decisions

| | Scenario | Decision | Conditions | Evidence | Remaining risks | Approvals required |
|---|---|---|---|---|---|---|
| **A** | Synthetic integration testing | **GO** | FakeKite, throwaway DBs, isolated test profile, separate browser profile; never with the interactive instance up during the runner | Acceptance 44/44; browser 42/42; P1-D browser 13/13; full suites (§8) | Synthetic success says nothing about real data or the real broker | None beyond this sprint |
| **B** | DMB private research using real market data | **NO-GO on documented rights (2026-11-04)** | — (the software works; the rights basis does not) | Yahoo ToS §2(d)(9) and NSE Terms of Use expressly prohibit automated collection without permission (matrix §5) | Running it anyway is an owner decision outside this record | Yahoo's express prior permission or a licensed feed; NSE written permission / NSE Data licence (matrix §3, §5.3) |
| **C** | Real-data DMB → Kite handoff | **NO-GO** | — | Every real source `UNRESOLVED`; real export `RIGHTS_BLOCKED`; Kite allowlist empty | — | Written permission per provider for private cross-app transfer (matrix §3), owner-recorded and reviewed, then a deliberate registry change |
| **D** | Kite Terminal dry-run operation with a real login | **NO-GO until §1.6 passes, then GO, limited** | DRY RUN only; LIVE not armed; the user-operated R-F1 verification (§1.6, with `KITE_TERMINAL_BROKER_WRITES=disabled`) passes first (no fallback exists); session expiry on (defaults); operator accepts the §6 / audit §4 residual risks | P2-G closed/mitigated with tests; P1-D/P1-E/P1-F/P0 tests; restart/EE scenarios (FakeKite) | Real Kite login and broker reads not exercised in tests; same-user malware/extensions out of scope; Kite Connect data-use terms not reviewed | Owner's informed acceptance of the audit's residual risks; R-F1 confirmation on first login |
| **E** | Live broker execution | **NO-GO** | — | **Real-broker order, GTT and Emergency Exit paths never validated**; Zerodha's sandbox cannot validate GTT, margins or MARKET orders (§7.2) | Every broker write path unproven against Zerodha | Separately authorized real-broker validation plan (small, supervised), then Live Readiness, then explicit owner approval. Passing software tests is not authorization. |

### 7.1 Operational status matrix (2026-11-04)

| # | Mode | Decision | Basis |
|---|---|---|---|
| 1 | DMB synthetic operation | **GO** | Synthetic fixture, scratch outputs; DMB full suite (§8.1) |
| 2 | DMB private real-data research | **NO-GO** (rights) | Provider terms prohibit automated collection without permission (matrix §5) |
| 3 | DMB → Kite synthetic handoff | **GO** | Acceptance and browser runners in the isolated test profile (§8.1) |
| 4 | DMB → Kite real-data handoff | **NO-GO** | No source permitted for cross-app transfer; `RIGHTS_BLOCKED` / `RIGHTS_NOT_APPROVED` |
| 5 | Kite Terminal local operation with FakeKite | **GO** | Full suite, P2-G, P1-D/E/F, restart / logout / Emergency Exit scenarios (§8.1) |
| 6 | Kite Terminal real login and read-only operation | **NO-GO until §1.6 passes** | R-F1 echo never observed; no fallback |
| 7 | Kite Terminal live order execution | **NO-GO** | Broker write path unvalidated (§7.2) |
| 8 | One manually initiated live trade through Kite Terminal | **NO-GO** | Follows from 6 and 7 (§7.2) |

### 7.2 One-trade readiness assessment (2026-11-04)

**Decision: do not place the first live order through Kite Terminal.** The first live order must not be the test of an unverified order path. Whether to place a trade through Zerodha's own Kite web or app instead is the owner's independent decision; this terminal makes no recommendation about any trade.

| Condition | Status | Evidence / blocker |
|---|---|---|
| Secure real login and R-F1 state verification | **Not met** | §1.6 not performed; no fallback |
| Authentication, session revocation, session expiry, WebSocket protections | Met (FakeKite) | `test_security_p2g.py`, `test_session_expiry.py`, P1-F sweeps, browser runners |
| Data source authorized for the use | Met for Kite Connect quotes shown to the account holder; **not met** for any DMB data | Matrix §5.2 |
| Instrument identity, exchange, segment, calendar | Met (FakeKite) | Instrument cache tests; calendar parity; market-session gate models regular sessions only |
| Fresh market data with visible timestamps | Met (FakeKite); unobserved with the real feed | Requires §1.6 |
| Order preview shows side, quantity, product, type, price, trigger | Met (UI tests) | `e2e_ui.py`, `e2e_p1d.py` |
| Margin/funds and risk checks visible | Margins: unobserved with the real account; risk checks met | Requires §1.6 |
| Position sizing and maximum loss reviewable | Met (UI) | Ticket form, P1-D review dialog |
| P1-D review and risk gates enforced | Met (FakeKite) | `e2e_p1d.py`, risk-engine tests |
| Duplicate-submission protection and audit log | Met (FakeKite) | Approval claim/race tests (`test_account_safety_integration.py`), no-duplicate-exit tests (`test_emergency_exit_isolation.py`), post-claim tamper audit (`docs/POST_CLAIM_TAMPERING_AUDIT.md`) |
| No unresolved critical reconciliation / health findings | Unknown for the real account | Requires §1.6 |
| **Broker write path verified by a documented, non-executing, broker-approved method** | **Not met: cannot be met today** | Zerodha documents a sandbox (`https://kite.trade/docs/connect/v3/sandbox/`: shared demo app, `sandbox.kite.trade`, LIMIT orders only). It does **not** support GTT (the terminal's default protective stop), margin calculation, or MARKET orders (used by exits and Emergency Exit). The terminal also has no sandbox mode: it would need a different API root, a route patch and a different login. FakeKite proves nothing about Zerodha. Earlier live attempts from this terminal were rejected by Kite (no static IP, `docs/FIRST_LIVE_TRADE.md`), so the real write path has never succeeded. |

Earlier live attempts, precisely: four live entry orders, all rejected by Kite with *"No IPs configured for this app"* (`docs/OPUS55_AUDIT_REPORT.md`, from the account database audit of 2026-09-30). That message means the developer account had **no** static IP whitelisted at all. It does not indicate a wrong IP. See §9.

What would be needed before this could change (each separately authorized): (1) §1.6 passes; (2) a sandbox-mode integration reviewed and run against `sandbox.kite.trade` for LIMIT place/modify/cancel, with GTT and MARKET paths explicitly still unverified; (3) a written plan for a supervised first live order with a tiny notional, broker-side stop placed by hand in Kite, and Live Readiness; (4) explicit owner approval. Passing software tests is not authorization.

## 9. Static IP: findings and safe setup checklist (2026-11-04)

**Why the earlier live attempts failed.** All four live orders were rejected by Kite with *"No IPs configured for this app. Add allowed IPs on the Kite developer console"*. The full wording is the one other developers report on the Kite Connect forum; the account database audit recorded the first sentence. No static IP had been whitelisted on the developer account. Nothing reached the exchange, and no protective GTT was placed (the terminal places it only after an accepted entry). Kite does not report the whitelisted IP through the API, so the terminal cannot check it (LIVE-0 report, Static IP Readiness).

**Zerodha's documented requirements** (support article *"What is a static IP and how to add one to your developer account?"*, support.zerodha.com, read 2026-11-04; Kite Connect forum discussion 15934, April 2026):
- Effective **1 April 2026**, under the NSE/SEBI retail algorithmic-trading framework, a static IP is mandatory for **API-based order placement**. The WebSocket market-data stream and other APIs (order book, positions) can still be used from any IP.
- The whitelist is set on the **developer account**, not per app: developers.kite.trade → **Profile** (top right) → **IP Whitelist** → enter the IP → **Update**.
- At least one **primary** IP; up to **two** in total (one per line), the secondary optional.
- It must be a **public static** IP. Private/LAN addresses and dynamic (ISP-rotating) addresses lead to rejections.
- **One modification per calendar week** (exchange rule), so plan changes.
- The IP may be shared only by you and/or your immediate family (an attestation in the console).
- Sources of a static IP: an ISP static-IP plan, a cloud VM (e.g. AWS/GCP), or a VPN/VPC service with a dedicated IP.
- Not stated by Zerodha: whether GTT placement/modification counts as "order placement". Treat GTT as **requiring** the static IP (conservative): the terminal's protective stop is a GTT.

**Safe setup checklist** (each step is the owner's. No step needs this terminal to place, modify or cancel anything, and none needs a credential shared with anyone):
1. Decide the route. Either (a) this PC behind an ISP static IP, or (b) Kite REST through the NET-1 static-IP proxy (`docs/DEPLOYMENT.md`, "Mode A+"): only REST goes through the proxy, the ticker stays direct.
2. Find the public egress IP of that route from the route itself (e.g. your ISP's account page, the VM's console, or `curl https://api.ipify.org` run on the VM for route b). A private address (10.x, 172.16-31.x, 192.168.x) is never valid.
3. Log in to developers.kite.trade yourself → Profile → IP Whitelist → add it as primary (optionally a secondary) → Update. Remember the once-a-week limit.
4. In the terminal (when real login is allowed, §1.6 passed): Live Readiness → Static IP → enter the same IP, tick the confirmation, Confirm. Optionally *Check this machine's outbound IP* (one request to api.ipify.org): it must read VERIFIED, never FAILED. With the NET-1 proxy, the readiness page also requires the proxy route to be reachable.
5. Do **not** test the whitelist with a real order. Whitelisting proves nothing about the terminal's order path; live execution stays NO-GO (§7.2) until a separately authorized validation plan exists.
6. Re-check the console after any change of ISP plan, router, VPN or VM, and before any future authorized live step.

Nothing in this sprint changed a broker setting, read the account database, or sent a request to Kite.

## 8.2 Sprint verification results (2026-11-04, session expiry / explicit review session)

Run sequentially, FakeKite and throwaway databases only. No real login, broker request, broker-setting change, real account database, production pipeline, scheduler job or publication.

| Run | Result |
|---|---|
| Kite full suite | **2,666 passed, 3 skipped, 0 failed** (2,590 + 76 new: session expiry 26, broker-write switch 24, explicit review session 24 contract + 1 inbox, parity at creation instants 1). Skips: 3 symlink tests (Windows privilege). A first run had 1 failure, `test_14d_only_the_factory_constructs_kiteconnect` (NET-1), caused by the switch's first draft hiding `KiteConnect(...)` behind a variable. The code was restructured; the test is unchanged. |
| DMB full suite | **3,408 passed, 4 skipped, 0 failed** (29 new explicit-review-session tests; every earlier expiry test passes unchanged). Skips: 2 symlink (Windows privilege), 2 fixture-content |
| Acceptance `acceptance_dmb_kite.py` | **44/44** (stage 20: real account database and backups unchanged) |
| Browser `e2e_dmb_inbox.py` | **42/42** |
| Browser `e2e_p1d.py` | **13/13** |
| Browser `e2e_ui.py --pin-clock` | **54/54**. The blocked-list check now also requires the blocked set to be exactly the demo's designed cases {TCS, NEWLISTCO} |
| Session-expiry mutation check | 4 mutants (no expiry; activity extends the absolute deadline; every request is activity; no socket timer): each fails tests |
| DMB/Kite parity | 0 differences: records, review sessions, expiry instants, and the named review session at 18 creation instants per day (2025-2026) |
| Frozen PK-25 files (hash-pinned + `research/pk25/*`, 45 files) | byte-identical |
| DMB's 17 pre-existing modified/untracked paths | byte-identical, unstaged diff identical, nothing staged from them |
| Source-rights registries | unchanged (Kite `PRODUCTION_PERMITTED_SOURCES` empty; DMB only the synthetic source PERMITTED) |

## 8.1 Sprint verification results (2026-11-04)

Run sequentially, FakeKite and throwaway databases only. No real login, broker request, real account database, production pipeline, scheduler job or publication.

| Run | Result |
|---|---|
| Kite full suite, before changes (baseline) | 2,571 passed, 3 skipped, 0 failed |
| Kite full suite, after changes | **2,590 passed, 3 skipped, 0 failed** (+19: packet-expiry, parity of expiry instants, no-downgrade, log redaction). Skips: 3 symlink tests (Windows privilege) |
| DMB full suite | **3,378 passed, 4 skipped, 0 failed** (re-run after inverting one test that pinned the old expiry rule; the first run had that 1 failure). Skips: 2 symlink (Windows privilege), 2 fixture-content. DMB commit `1fc8c25` |
| Acceptance runner `acceptance_dmb_kite.py` | **44/44** (incl. stage 20: real account database and backups unchanged) |
| Browser integration `e2e_dmb_inbox.py` | **42/42** |
| Browser P1-D `e2e_p1d.py` | **13/13**, every refusal declared and observed |
| Browser UI `e2e_ui.py --pin-clock` | **54/54**: the former failure was a stale count (see commit), now checked against the backend |
| Kite P2-G / credential / CSRF / hardening / Emergency Exit restart and isolation | 737 passed (within the full suite) |
| Route inventory (live app object) | 59 GET routes: 53 credential-gated `/api` reads, 3 public `/api` reads, `/`, `/login`, `/callback`; 45 mutating routes; 1 WebSocket. Both sweeps enumerate `app.routes` at test time |
| Frozen PK-25 files (13 hash-pinned + `research/pk25/*`) | byte-identical |
| DMB's 17 pre-existing modified/untracked paths | byte-identical, unstaged diff identical, nothing staged from them |

## 8. Sprint verification results (2026-11-03)

Run sequentially, FakeKite and throwaway databases only; no real broker request, real database, production pipeline or scheduler job.

| Run | Result |
|---|---|
| DMB calendar / handoff / Private Desk / dependent suites | 729 passed |
| DMB full suite | **3,360 passed, 4 skipped, 0 failed** (baseline 3,347 + 4; +13 calendar session-record tests) |
| Kite P2-G (`test_security_p2g.py`) | 42 passed |
| Kite P1-F / CSRF / hardening / restart / Emergency Exit / preflight | all passed |
| Kite calendar parity + trading calendar + NSE monitor | all passed (parity: 761 days, 0 differences) |
| Kite full suite | **2,571 passed, 3 skipped, 0 failed** (baseline 2,492 + 3) |
| Synthetic acceptance runner (real processes, real restart) | **44/44** (the 42 earlier checks, stage 17 inverted per R-F2, + 2 R-F3) |
| Browser integration runner `e2e_dmb_inbox.py` (headless Edge) | **42/42** (the 31 earlier checks + 11 P2-G) |
| Browser P1-D runner `e2e_p1d.py` | 13/13, every refusal declared and observed, no other error |
| Browser UI runner `e2e_ui.py --pin-clock` | 53/54, the one pre-existing demo-data failure ("risk-blocked signal listed separately") unchanged |
| NSE 2027 monitor (live, read-only) | `NOT_PUBLISHED` at 2026-11-03T11:26Z |
| Frozen PK-25 files (13 hash-pinned + `research/pk25/*`) | byte-identical |
| DMB's 17 pre-existing modified/untracked paths | byte-identical, unstaged diff identical, nothing staged from them |
