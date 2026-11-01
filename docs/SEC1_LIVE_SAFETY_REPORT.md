# SEC-1: Live-trading safety gates (P0-A, P0-B, P0-C)

**Scope.** The three P0 packets of the cross-repo audit remediation plan.
- **Not started:** P1-D (manual-ticket re-review), P1-E (acknowledged vs
  filled API fields), P1-F (per-launch session credential) and P2-G
  (callback `state`, loosening audit).
- **Out of scope:** the DMB candidate inbox.
- **Never done:** no real broker order was placed or tested. Everything runs
  against FakeKite and temporary databases.

## Findings closed

| Audit finding | Before | After |
|---|---|---|
| 1. Static header accepted without Origin | `rejection_reason` accepted a write that had `X-Kite-Terminal: 1` and no Origin or Referer, and a WebSocket handshake with no Origin | Both are refused (`CSRF_PROTECTION_FAILED`). Browsers always send Origin, so the UI is unaffected. |
| 2. Live switch ignored readiness | `set_execution_mode` checked `confirm_real_money` only | The switch to live runs `live_preflight.evaluate(broker=True, refresh=True)` and arms only on READY, or on NOT READY caused by nothing but the trading lock. It is armed for one Kite session. Login and logout disarm it. |
| 3. Critical reconciliation did not block entries | Display-only, on the Live Readiness page | Re-checked at confirmation: before the claim and again right before the broker call. Uses the same check builders as the page. |

## Changes

**P0-A: Origin required.**
- `backend/security.py`: a missing Origin and Referer is a rejection, for
  HTTP writes and for the WebSocket handshake.
- `docs/DEPLOYMENT.md`: new section on what the request checks do *not* stop
  (any non-browser local process; same-user malware).

**P0-B: confirm-time live critical-state gate.**
- `backend/live_preflight.py`: the reconciliation, worker and calendar checks
  are factored into shared builders, plus `confirm_time_blockers()` (local,
  read-only, no broker call). `RECON_LAST_CYCLE` is tightened for both the
  page and the gate: a cycle with `failed_checks`, or a timestamp more than
  120 s in the future, fails. The same rule applies to worker timestamps.
- `backend/account_safety.py`: `check_live_critical_state()` runs in
  `precheck` and in `final_gate`, for live new entries only. Failures:
  - stale, missing or inconsistent state (reconciliation, workers, calendar)
    → `LIVE_STATE_UNVERIFIED`, 503;
  - a critical finding or broken paper/live isolation →
    `RECONCILIATION_CRITICAL`, 409, never permanent.
  An exception while evaluating fails closed.
- `backend/routes/reconciliation.py` + `ReconciliationReviewRequest`:
  mark-reviewed requires a reason and logs `reconciliation_reviewed` once.
  `frontend/app.js` asks for the reason.
- `backend/routes/kill_switch.py`: Emergency Exit adds a `warning` to the
  results of positions with an unresolved critical finding. This runs after
  every exit attempt, and any failure there is swallowed. It never refuses an
  exit.

**P0-C: live arming.**
- New `backend/live_arming.py`:
  - `set_mode` is the only mode writer;
  - `disarm_live` / `broadcast_mode_reset` handle a session change;
  - `live_armed_for_active_session` reports the arming state.
- `backend/routes/orders.py`: the readiness check runs only when arming is
  actually needed. A refusal returns 409 `LIVE_READINESS_NOT_MET` with the
  `blocking_codes`.
- `backend/kite_client.exchange_request_token` (in the same transaction that
  replaces the session) and `POST /api/logout` disarm LIVE.
- `account_safety.check_live_armed` is the backstop
  (`LIVE_NOT_ARMED_FOR_SESSION`).
- The readiness page's `EXECUTION_MODE` check reports whether LIVE is armed.
- Migration **v6 `sec1_live_safety`**: adds three `system_events` types
  (rebuild of the CHECK allow-list) and
  `execution_settings.live_armed_session_id`.

## Deviations from the plan

- **Arming lives in `backend/live_arming.py`, not `execution.py`.**
  `backend/execution.py` is listed in the PK-25 research freeze
  (`research/pk25/validation_manifest.json`, enforced by
  `test_26_risk_execution_and_backtest_are_unchanged` and `test_36`). The
  file is left byte-identical rather than superseding or editing the freeze.
  Its old `set_execution_mode` stays in place but uncalled, and an AST test
  enforces that.
- **The confirm-time blockers leave out `UNKNOWN_LIVE_ORDERS`.** The LIVE-0
  prerequisite check already refuses those, with its own message, so it is
  not duplicated.

## Tests

- **Suite:** 1916 passed (baseline 1732). New files:
  `tests/test_live_critical_gate.py` (48) and `tests/test_live_arming.py`
  (20). `test_csrf_protection.py` grew to cover every mutating route ×
  {no Origin, empty Origin, `null` Origin}.
- **Deliberately changed tests:**
  - `test_g_no_origin_with_header_is_accepted` is inverted to
    `..._is_rejected`, live mode, no broker write.
  - WebSocket no-Origin cases now expect a refusal.
  - Schema version pins moved from 5 to 6.
  - Mark-reviewed calls now send a reason.
  - Four "fresh process" tests call `clear_healthy_live_state`.
  - `test_9_trading_lock_survives_a_restart` accepts 409/503, as `test_8`
    already did: right after a restart the live-state gate refuses too.
  - Tests that switch to LIVE through the API first build a genuinely READY
    state with `make_live_ready` (real evaluation, no stub).
- **No gate was weakened.** The new defaults (`seed_healthy_live_state`, and
  `set_mode` stamping the arming) follow the LIVE-0
  `seed_live_prerequisites` precedent.

## Not verified

- No real browser pass. The claim that Chrome, Edge and Firefox send Origin
  on same-origin POST/PUT/DELETE fetches and WebSocket handshakes is
  standard Fetch behaviour, but untested here.
- No real Kite session or order.
- `FakeKite.profile` has no failure hook. The arming test patches it
  directly.

## Operator impact

- LIVE must be switched on again after every Kite login, and only once Live
  Readiness passes with its broker check.
- After a restart, live entries are refused until the first reconciliation
  cycle completes.
- Marking a finding reviewed asks for a reason.
- Exits, Emergency Exit and switching to dry run are unaffected.
