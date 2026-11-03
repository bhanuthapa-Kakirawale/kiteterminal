# DMB → Kite: operational runbook

**Status (2026-11-03).** Software readiness work is complete for synthetic integration and dry-run operation. **Real-data handoff is BLOCKED** (source rights, `docs/DMB_KITE_SOURCE_RIGHTS_MATRIX.md`). **Live broker execution is NOT approved**: the real broker path, including Emergency Exit, has never been validated. Decisions: §7.

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
| P2-G R-F1 login binding (state via `redirect_params`) | yes | n/a | **one real login needed to confirm Kite echoes it** |
| P2-G R-F2 / R-F3 / R-F4 (reads, port isolation, WS revocation) | yes | yes (incl. a real second server on another port) | n/a |
| Emergency Exit | yes (6 scenarios) | yes (FakeKite) | **NOT VALIDATED** |
| Calendar session semantics, DMB/Kite parity | yes (761 days, 0 differences) | — | n/a |
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
   - **First real login after this sprint (R-F1 confirmation):** check that you land on the dashboard. If you land on `/?login_error=1` and the log says *"Kite callback refused: not the pending login started from this browser"*, Kite did not echo the state. Stop the terminal, set `KITE_TERMINAL_LOGIN_STATE_ECHO=cookie-only` in `.env` (an explicit, logged downgrade; `docs/SECURITY_P2G_AUDIT.md` R-F1), restart, and log in again. Record the outcome in this runbook.
5. Verify the session: the header shows connected, `GET /api/session/status` shows `terminal_authorized: true`, and execution mode is **DRY RUN**.

### 1.3 Handoff directory
- Kite imports only from `KITE_DMB_HANDOFF_DIR`, listing `<dir>\<YYYY-MM-DD>\<64-hex>.json`. Unset means import is off.
- DMB writes bundles only on an explicit `python -m private_desk.handoff export --session D` into `output\private_desk\handoff\<D>\`. **For real data this is RIGHTS_BLOCKED (exit 20) and writes nothing.** Only the synthetic fixture can be exported today (§1.5).

### 1.4 Freshness, import and the inbox (synthetic bundles today)
1. **Freshness:** `session_date` is a completed **regular** NSE session; `review_session_date` is the next regular session; the packet expires at **15:30 IST on the review session**. Special sessions (Budget weekend, Muhurat) never describe a packet and never shorten the window (`docs/NSE_CALENDAR_MONITORING.md` §5). Packets needing 2027 are refused (`CALENDAR_UNCOVERED`).
2. **Import:** DMB Research Inbox → pick the listed bundle → Import. The whole bundle is validated first. Result `IMPORTED`, `DUPLICATE` or `REJECTED <code>`.
3. **Review each item:** provenance, evidence time, quality, rights, and the SYNTHETIC label.
4. **Inbox actions:** Dismiss, Add note, Research watchlist. The research watchlist is separate from the strategy scan universe.
5. **Manual ticket:** Start ticket prefills only symbol, exchange and rationale. Side, order type, product, quantity, entry, stop and targets are typed by you; the form refuses to submit without them.
6. **P1-D review:** the approval dialog shows the review; approve from it (it echoes the token it showed). If the review is not current (after 09:15, after 15:30, or after a new login), use **Re-review**.
7. **Dry-run verification:** the approved ticket shows mode `dry_run`. Positions shows "Paper broker / fill: accepted · awaiting fill", separately from the fill. Nothing reaches the broker.

### 1.5 Isolated synthetic test instance (never the real terminal)
`.venv\Scripts\python.exe tests\acceptance_dmb_kite.py --serve-only` → `http://localhost:8790/demo/authorize` in a **separate browser profile** (e.g. Edge InPrivate). FakeKite, throwaway DB, LIVE impossible, its own cookie name `kt_test_session`. See `docs/DMB_KITE_ACCEPTANCE_TEST.md`. **Do not run the automated acceptance runner while this instance is up**: the runner resets the scratch environment.

## 2. Restart and login behaviour (verified)

| Event | What happens | How to verify |
|---|---|---|
| **Kite Terminal restarts** | The per-launch credential (cookie digest + terminal token digest) exists only in memory, so it is gone. **Account data is hidden** (P2-G R-F2) and **every write is refused** (`TERMINAL_SESSION_REQUIRED`) until a fresh login, Emergency Exit included. Worker and reconciliation state start empty. | Screen "Log in to unlock this terminal"; `/api/session/status` → `terminal_authorized: false`, `user_name: null` |
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
| **Stale packet** | After 15:30 IST on its review session an item stays visible as "research only" (`EXPIRED`) and cannot start a ticket. An already-expired bundle is refused at import. Wait for the next session's export; never change dates. |
| **Database backup** | App running: `.venv\Scripts\python.exe -m backend.db_maintenance backup` (online backup → `kite_terminal.db.manual_<time>_bak`). Check with `... integrity` and `... status`. Every migration also leaves a verified `pre_schema_v<N>_bak`. Backups are secret (older ones contain plaintext daily tokens). |
| **Database restore** | Stop the terminal. Keep the current file aside (rename it, do not delete it). Copy the chosen `_bak` to `kite_terminal.db`. Run `... integrity`, then `... migrate` (idempotent; a second run must report no migrations and no row changes). Start, log in, and review reconciliation findings before any action. Restoring loses everything after the backup: check open orders and GTTs in Kite web first. |
| **Application restart and authentication recovery** | Restart `python -m backend.server` → log in to Zerodha → verify `terminal_authorized: true` → Re-review pending manual tickets → re-arm LIVE only through Live Readiness (never in this sprint's scope). If login keeps failing with `login_error=1`, see §1.2 step 4 (R-F1). If the UI stays locked after login, clear this site's storage for `127.0.0.1:8000` and log in again (a stale terminal token is harmless but useless). |

## 6. Security notes for operators

- Use one browser profile for the real terminal and a **separate** profile for the isolated test instance.
- Other local web apps (DMB's desk, dev servers) receive the terminal's cookie, because cookies have no port, but cannot read the terminal token. Optional hardening: serve the terminal under a dedicated host name, e.g. `KITE_TERMINAL_ALLOWED_HOSTS=kite-terminal.localhost` with the Kite redirect URL on that name (whether Kite Connect accepts a `.localhost` redirect URL is unverified). Then the generic `127.0.0.1:*` and `localhost:*` servers never even receive the cookie.
- Not protected: malware running as your Windows user, browser extensions with access to the terminal's page, administrators/debuggers (`docs/SECURITY_P2G_AUDIT.md` §4).

## 7. Operational use decisions

| | Scenario | Decision | Conditions | Evidence | Remaining risks | Approvals required |
|---|---|---|---|---|---|---|
| **A** | Synthetic integration testing | **GO** | FakeKite, throwaway DBs, isolated test profile, separate browser profile; never with the interactive instance up during the runner | Acceptance 44/44; browser 42/42; P1-D browser 13/13; full suites (§8) | Synthetic success says nothing about real data or the real broker | None beyond this sprint |
| **B** | DMB private research using real market data | **GO, limited (existing operating decision, rights basis unresolved)** | Private desk only; nothing exported, published or uploaded; production pipelines run only under DMB's own runbooks | DMB full suite (§8); rights registry unchanged | Source rights for even private automated collection/storage are **unresolved** (matrix Category 1); calendar for 2027 missing | Owner to record the Category-1 basis per source (matrix §3) |
| **C** | Real-data DMB → Kite handoff | **NO-GO** | — | Every real source `UNRESOLVED`; real export `RIGHTS_BLOCKED`; Kite allowlist empty | — | Written permission per provider for private cross-app transfer (matrix §3), owner-recorded and reviewed, then a deliberate registry change |
| **D** | Kite Terminal dry-run operation | **GO, limited** | DRY RUN only; LIVE not armed; real Kite login for read access and dry-run tickets; **first real login confirms R-F1** (§1.2) or uses the logged `cookie-only` fallback; operator accepts the §6 / audit §4 residual risks | P2-G closed/mitigated with tests; P1-D/P1-E/P1-F/P0 tests; restart/EE scenarios (FakeKite) | Real Kite login and broker reads not exercised in tests; same-user malware/extensions out of scope; Kite Connect data-use terms not reviewed | Owner's informed acceptance of the audit's residual risks; R-F1 confirmation on first login |
| **E** | Live broker execution | **NO-GO** | — | **Real-broker order, GTT and Emergency Exit paths never validated**; out of this sprint | Every broker write path unproven against Zerodha | Separately authorized real-broker validation plan (small, supervised), then Live Readiness, then explicit owner approval. Passing software tests is not authorization. |

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
