# DMB → Kite: operational runbook

**Status.** The local synthetic integration test is ready. **Real-data handoff is blocked** until private-handoff rights are confirmed.

This runbook covers the *isolated test* operation that is possible today, and the procedures that will apply to the real terminal. It changes no operating decision.

## 0. Validation status

| Area | Tested with FakeKite and scratch DBs (pytest) | Isolated local browser / process | Real broker |
|---|---|---|---|
| DMB export → Kite import, inbox actions, Start ticket | yes | yes (acceptance A, browser C) | n/a (no broker) |
| Synthetic isolation (test profile vs production) | yes | yes | n/a |
| P1-D review, token binding, dry-run approval | yes | yes | **not tested** |
| Positions broker / fill display | yes | yes (paper broker) | **not tested** |
| Post-claim tampering audit | yes | yes | **not tested** |
| Restart / logout / fresh login | yes | yes (real process restart) | **not tested** (real Kite login) |
| Emergency Exit | yes | yes (FakeKite) | **NOT VALIDATED** — the real broker path is outside this sprint |
| Login CSRF guard (P2-G F1) | yes | n/a | **not tested** against a real Kite redirect |
| Real DMB evidence handoff | — | — | **BLOCKED by source rights** (DMB: every real source UNRESOLVED; Kite: no confirmed source) |
| 2027 sessions | fail-closed tested | — | **BLOCKED by the missing official NSE 2027 calendar** |

## 1. Normal startup (isolated synthetic test)

1. **Start DMB's Private Desk (optional, read-only research view).** From `D:\Claude_WS\daily_byte`, follow DMB's own Private Desk instructions. For the integration test, DMB is used only through its exporter, which the acceptance runner calls on the synthetic fixture. **Do not run DMB production pipelines or scheduler jobs.**
2. **Start the isolated Kite instance.** Run `.venv\Scripts\python.exe tests\acceptance_dmb_kite.py --serve-only` from `D:\Claude_WS\kite-terminal`. See `docs/DMB_KITE_ACCEPTANCE_TEST.md`.
   - The console prints `SYNTHETIC TEST PROFILE active (FakeKite, throwaway DB, LIVE impossible)` and the scratch handoff directory.
   - **Never** start the real terminal (`python -m backend.server`) for this test.
3. **Check the handoff directory.** The inbox lists only `<dir>\<YYYY-MM-DD>\<64-hex>.json` entries.
   - In the test, `<dir>` is `%TEMP%\dmb_kite_acceptance\handoff`.
   - In the real terminal it is `KITE_DMB_HANDOFF_DIR`. When that is unset, import is off.
4. **Check session and freshness.**
   - The bundle's `session_date` must be a completed regular NSE session.
   - `review_session_date` must be the next regular session.
   - Packets expire at **15:30 IST on the review session**.
   - The test instance pins its clock to 2026-11-03 11:00 IST, inside the review session.
5. **Import and review.** Import the synthetic bundle, then open each packet: provenance, evidence time, quality, rights, and the SYNTHETIC label.
6. **Use the inbox.**
   - **Dismiss**, **Add note** and **research watchlist** are recorded inbox events.
   - The research watchlist is **separate** from the strategy watchlist (the scan universe). It never changes what strategies scan.
7. **Start a manual ticket.** Only **symbol, exchange and rationale** are prefilled. **Side, order type, product, quantity, entry, stop and targets must be typed in by you.** The form refuses to submit without them.
8. **P1-D human review.** Creating the ticket stamps the review. The approval dialog shows the review and its state. Approve from that dialog; it echoes the token it showed. If the review is not current (after 09:15, after 15:30, or after a new login), use **Re-review**.

## 2. Restart and login behaviour (verified)

| Event | What happens | How to verify |
|---|---|---|
| **Kite Terminal restarts** | The per-launch credential exists only in process memory, so it is gone. Reads keep working. **Every write is refused** with `TERMINAL_SESSION_REQUIRED` until a fresh login, Emergency Exit included. The worker and reconciliation state starts empty. | The banner "Changes are locked … log in to Zerodha again"; `GET /api/session/status` shows `terminal_authorized: false` |
| **Logout** | The session is deactivated, the stored access token is cleared, LIVE goes back to dry run, the credential is revoked and the cookie cleared. The old cookie is refused afterwards. A credential alone is not enough without a Kite session (`NO_SESSION`). | `connected: false` |
| **Fresh login** (`/login` → Zerodha → `/callback`) | P2-G: the callback must belong to a login this browser started within 10 minutes. A new credential is issued and the old one rotated. LIVE must be re-armed through Live Readiness. | `terminal_authorized: true`; the banner is gone |
| **Why a fresh login is required before writes** | The credential rests on the user's Kite password and 2FA. Nothing else can prove to a restarted process that the browser is the user's (owner decision, SEC-1 P1-F). | — |
| **Stale or expired inbox items** | They stay visible for research, marked "research only" with a reason (`EXPIRED`, `SUPERSEDED`, `DISMISSED`, `CALENDAR_UNCOVERED`, …). Start ticket is refused. A ticket already created is unaffected and follows its own P1-D review window. | Item reasons in the inbox |

## 3. Emergency Exit

The operating decision is unchanged: **after a restart or logout, a fresh Kite login is required before any write, including Emergency Exit.**

These results come from FakeKite and a scratch database (`tests/test_emergency_exit_restart.py`, acceptance stage 19):

| Case | Expected result |
|---|---|
| While authenticated | One exit order per **live** open position, typed `EXIT ALL` required. Dry-run positions are not touched. Results are reported per position. |
| After a restart, before re-login | **401 `TERMINAL_SESSION_REQUIRED`. Nothing is sent.** Use a fallback (§4). |
| After logout, credential only | 401 `NO_SESSION`, nothing sent |
| After a fresh login | Exits are submitted |
| Reconciliation or worker health critical | **Not held back.** Exits are submitted; unresolved critical findings are attached as warnings. |
| Calendar coverage missing (2027 clock) | **Not held back.** Exits are submitted. Only new entries fail closed. |
| Wrong confirmation phrase | 400, nothing sent |
| A post-claim tampered exit row | That exit is refused, not sent unverified (`TICKET_TAMPERED_AFTER_CLAIM`). The other positions continue. |

**The real broker Emergency Exit path has NOT been validated.** Real broker validation is outside this sprint.

## 4. Fallback procedures (when the terminal cannot exit)

1. **Broker-side stop orders.** A position opened with "Protective GTT" (`use_gtt`, the default) has a stop-loss GTT at Zerodha. It stays active at the broker whatever happens to the terminal, including a restart, a crash or a logout. The Positions page shows each position's protection status; a position without one needs a manual stop in Kite.
2. **Kite web or app.**
   - Log in at kite.zerodha.com.
   - Go to Positions / Holdings → Exit, and Orders → cancel any open entry.
   - Then check GTTs: delete or adjust the protective GTT of anything you exited by hand. Otherwise it can trigger against shares you no longer hold.
3. **Terminal unavailable — manual emergency procedure:**
   1. Exit in Kite web (step 2).
   2. Write down every order id.
   3. When the terminal is back, log in.
   4. Reconciliation flags the positions closed outside the app. Review each finding.
   5. Never resubmit an order whose outcome is UNKNOWN; check it in Kite first.
4. **Calling the broker.** Zerodha's dealing desk (call-and-trade) is the last resort if both the terminal and Kite web are unavailable.

## 5. Operational verdict

- **Tested with FakeKite:** import, inbox, Start ticket, P1-D, fill display, the tampering audit, restart and login, Emergency Exit.
- **Tested in the isolated local browser environment:** headless Edge, 31 checks, and an automated 42-check acceptance run with a real process restart.
- **Not tested with a real broker:** every broker write, including Emergency Exit, and the real Kite login redirect with the P2-G state.
- **Blocked by source rights:** real DMB evidence handoff. Kite's `PRODUCTION_PERMITTED_SOURCES` is empty, and DMB marks every real source UNRESOLVED.
- **Blocked by missing official calendar data:** any session or packet in 2027, until NSE publishes its circular (`docs/NSE_CALENDAR_MONITORING.md`).
- **Open owner decisions before real live use:** P2-G R-F3 (cookie port scope) and R-F2 (Host-only reads). See `docs/SECURITY_P2G_AUDIT.md`.

**Verdict:** LOCAL SYNTHETIC INTEGRATION TEST READY — REAL-DATA HANDOFF BLOCKED PENDING RIGHTS CONFIRMATION. This is not a production-readiness statement.
