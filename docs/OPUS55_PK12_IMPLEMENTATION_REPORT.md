# OPUS55 PK-12 Implementation Report: account-level trading safety controls

| | |
|---|---|
| Packet | PK-12 (audit P1-6), implemented as 8 sub-packets, 12A to 12H, each with its own checkpoint. PK-13 and later packets were not started. |
| Baseline before | **515 passed**, 0 xfailed, 0 failed, 0 xpassed. Re-measured on the untouched code before the first edit. |
| Result after | **643 passed, 0 xfailed, 0 failed, 0 xpassed**, in 3 identical full runs on the final code (see Full Suite ×3). |
| Where the 128 new tests come from | 643 = 515 + 4 + 124. The **+4** are `test_csrf_protection::test_every_mutating_route_rejects_cross_origin`, which enumerates every mutating route: the 2 new POST routes × 2 attack variants. They were collected and passed with no test change. The **124** are the new PK-12 tests: 105 in `test_account_safety.py` and 19 in `test_account_safety_integration.py`. |
| Backups taken first | Tag `backup-pre-pk12` and branch `backup/pre-pk12` at `1aab626`. Tag `backup-pre-pk12-worktree` (commit `9f8ef11`) is a whole-working-tree snapshot that includes the then-uncommitted PK-0 to PK-5 files. It was built through a temporary `GIT_INDEX_FILE`, so the real index was untouched. `backend/kite_terminal.db.pre_pk12_bak` was taken with the SQLite online-backup API (`integrity_check` = ok, 18 tickets). |
| Pre-commit (user decision) | The PK-0 to PK-5 work was found **uncommitted**, contrary to the brief: `backend/security.py` was untracked, and the hunks in `db.py`, `risk_engine.py`, `main.py`, `config.py` and others were unstaged. Before any PK-12 edit, it was committed on its own, as it stood, in `17344b3` ("fix: commit PK-0..PK-5 safety work ..."; 515 passed). The PK-12 commit therefore holds only PK-12. |
| Live DB | Untouched. SHA-256 `1378f8b4…` and mtime are identical before and after. Tests use a per-test temporary DB and FakeKite. Migrations were validated on a scratch copy. |

# Executive Summary

A server-side **account safety gate** now sits above the per-ticket risk rules, in the single execution path `routes/tickets.confirm_and_execute_ticket`. It applies only to **NEW entries**, meaning every ticket that is not `execution.is_closing_sell`.

**Where the gate runs:**

```
Strategy / manual ticket -> risk engine -> pending_confirmation
  confirm:
    status check -> mode (PK-3) -> expected_mode (PK-11)
    -> ACCOUNT SAFETY PRE-CHECK   (DB/request only; no broker call; nothing written on refusal)
    -> ATOMIC CLAIM (PK-1)        (+ PK-12C daily-entry predicate in the same UPDATE)
    -> fresh risk revalidation (PK-4)   (portfolio context built once, shared with the gate)
    -> ACCOUNT SAFETY FINAL GATE  (re-check lock/breaker/loss, then ONE LTP call for price checks)
    -> broker (variety decided by the gate, never re-derived from the clock)
    -> breaker bookkeeping (never changes the recorded outcome)
```

**The controls:**

| | Control | Mode | Where |
|---|---|---|---|
| 12A | Trading lock (DB-persisted, explicit, logged, broadcast) | dry run and live | pre-check + final gate |
| 12B | Absolute max order value (`max_order_notional`) | live | pre-check |
| 12C | Max new live entries per IST day, atomic | live | pre-check (informative) + **claim (binding)** |
| 12D | Price deviation vs LTP, and stop already crossed | live | final gate (one LTP call) |
| 12E | Stale strategy signal | dry run and live | pre-check |
| 12F | Daily realized live loss (₹ and/or % of portfolio) | live | pre-check (₹) + final gate (%) |
| 12G | Broker/API circuit breaker (persisted, manual reset) | live | pre-check + final gate |
| 12H | Market-hours / explicit AMO consent (`allow_amo`) | live | pre-check; variety bound to execution |

**What is never blocked:** exits, Emergency Exit, protective GTT upkeep, reconciliation, order monitoring, UNKNOWN recovery and cancellations. Integration scenarios S9, S10 and S11 prove this while every entry control is tripped at once.

**Validation:**

- 124 new tests;
- 9/9 focused mutations caught;
- targeted suite ×10: 243 passed each run;
- concurrency subset ×20: 15 passed each run;
- full suite ×3: 643 passed each run;
- migrations run twice on a scratch copy of the live DB;
- browser checks: the existing `tests/e2e_ui.py` 55/55, plus a PK-12 UI script 21/21.

**User decisions applied:**

- defaults: 3 entries a day, 3 % deviation, and a breaker at 3 failures in 10 minutes; every ₹ limit is unset;
- the daily loss limit uses realized live P&L only;
- the lock blocks both modes;
- PK-0 to PK-5 were committed first.

# PK-12A Trading Lock

- **Storage.** `account_safety_state.trading_locked` (with `trading_locked_at` and `trading_lock_reason`) is keyed by `account_id`. It lives in the database, never in process memory. `init_db` seeds an unlocked row for each trading account and never resets an existing one: `test_12a_lock_persists_across_restart` re-runs `init_db`.
- **Changing it.** Only `POST /api/account-safety/trading-lock {locked, reason}` changes it, through `account_safety.set_trading_lock`.
  - The write is a conditional UPDATE, so exactly one `trading_locked` / `trading_unlocked` system event is written, with the reason, and one `account_safety_changed` broadcast is sent.
  - Setting the state the lock is already in is a no-op (`changed: false`) and writes no event.
  - The endpoint is CSRF-guarded (PK-5).
- **What it blocks.** Every new-entry confirmation in both modes (user decision): manual, strategy, alert-action BUY and approval of pending entries.
  - Ticket creation and strategy scans still work: a signal can be drafted, but not approved.
  - The lock is checked before the claim, so a refusal writes nothing to the ticket and makes no broker call.
  - It is checked again at the final gate, so a lock set while a confirmation is inside risk revalidation still stops it (`test_12a_lock_set_during_revalidation_is_honoured_before_the_broker`). The claim is withdrawn and the ticket returns to `pending_confirmation`.
- **What it never blocks:** exits, Emergency Exit, GTT lifecycle, reconciliation, order monitoring and cancellations.
- **Code:** `TRADING_LOCKED` (409).

# PK-12B Absolute Max Notional

- **Setting.** `risk_settings.max_order_notional`, REAL, with `CHECK (NULL OR > 0)`. NULL means disabled and is the default, because no rupee amount is hard-coded.
- **Rule.** `entry_price × quantity ≤ cap`. Exactly at the cap is allowed; one share or one tick (₹0.05) over is refused (`MAX_NOTIONAL_EXCEEDED`, 409, ticket left approvable).
- **Validation:**
  - pydantic `gt=0` gives 422 for a negative, zero or non-numeric value;
  - the DB CHECK rejects a negative value written directly.
- **Save semantics.** A PK-12 field the Settings request *omits* keeps its value; only an explicitly sent value, including `null` to disable, is written. This stops an older tab that does not know the field from silently switching a cap off. Changes go through `risk_engine.update_settings`, so they are history-tracked.
- **Exits:** never checked (`test_12b_exit_is_not_blocked_by_the_entry_notional_cap`).
- **Limitation.** For a MARKET entry, `entry_price` is the reference price. The fill can differ from it by at most the 12D deviation bound.

# PK-12C Daily Entry/Order Limit

**Setting.** `risk_settings.max_new_entries_per_day`, INTEGER, default **3**. NULL means disabled.

**Counting rule** (`account_safety.COUNTED_SUBMISSION_STATES`). A ticket counts when **all** of these hold:

- it is on the same account;
- `mode = 'live'`;
- it is not a closing sell;
- `submitted_at` is on or after IST midnight of the trading date;
- `submission_state` is one of:

| Counted | Why |
|---|---|
| `submitting` | the claim is the reservation; the order may be in flight |
| `acknowledged` | the broker accepted it; this includes entries later cancelled or rejected by the exchange, because they did reach the broker |
| `unknown` | **counted**: a real order may exist |
| `adopted` | recovered by the order monitor |
| `needs_review` | contradictory evidence; may exist |

**Not counted:**

- dry-run entries;
- exits;
- GTT maintenance;
- claims withdrawn before any broker call (revalidation or account-safety refusals clear the submission fields to NULL);
- definite broker rejections (`rejected`);
- `not_found`.

**Atomicity.** `daily_entry_claim_condition` returns an extra predicate, `(SELECT COUNT(*) … counted … AND id != ?) < limit`. It is added to the **same conditional UPDATE** that performs the PK-1 claim (`_transition(..., extra_condition=...)`).

- SQLite's single-writer lock serialises claims, so the count and the reservation are one atomic step.
- If the claim loses while the ticket is still `pending_confirmation`, the response is `DAILY_ENTRY_LIMIT` (409) with nothing written.
- The pre-claim copy of the check only gives an early, broker-call-free refusal.

**Race tests.** They put a barrier after the pre-check, so every thread has seen a free slot before any claims:

- two approvals for one slot → exactly 1 `place_order`;
- three approvals for two slots → exactly 2;
- a race with an UNKNOWN row already holding a slot;
- a race started while another entry is inside `place_order` (its `submitting` claim holds its slot);
- dry-run races, which are never limited.

**IST day.** `ist_day_start_utc(now)` gives IST midnight in UTC text, and the gate uses one clock (`account_safety.now_ist`). `test_12c_next_ist_day_resets_the_count` moves that clock one day forward.

**Deviation.** There is no separate raw API-order cap. Exits and GTT operations must never be limited, and the entry count covers the runaway-loop and over-trading cases.

# PK-12D Price-Deviation Guard

**Setting.** `risk_settings.max_entry_price_deviation_pct`, REAL in (0, 100], default **3**. NULL disables the deviation check only; the stop check always runs.

**When.** At the final gate, after revalidation, with **one** `kite.ltp` call for the ticket's symbol. The call is skipped entirely if the lock, breaker or daily-loss re-check already failed (`test_12f_percentage_limit_is_checked_at_the_final_gate` asserts no LTP call).

**What it checks:**

| Code | Condition | Ticket afterwards |
|---|---|---|
| `PRICE_DEVIATION_EXCEEDED` | \|LTP − entry_price\| / entry_price × 100 > threshold (a 1e-9 tolerance, so exactly ±threshold is allowed and one tick beyond is refused) | back to `pending_confirmation` |
| `STOP_ALREADY_INVALID` | BUY with LTP ≤ stop, or SELL (short) entry with LTP ≥ stop | `rejected` (permanent) |
| `ACCOUNT_SAFETY_UNAVAILABLE` (503) | the LTP cannot be fetched, or is 0, negative, None or NaN — fail closed | back to `pending_confirmation`; no order |

- For MARKET entries, the reference `entry_price` is compared, and the message says "reference price".
- Both failures are reported when both apply.
- A transport failure of the LTP call counts toward the breaker (12G). An `InputException` or a malformed value does not.

**Never rewritten:** the entry price, stop and target (asserted in the tests).

**Out of scope:** exits and dry-run entries. A dry-run entry makes no LTP call.

# PK-12E Stale-Signal Guard

**Rule.** A **strategy** entry is valid only while `COALESCE(signal_date, created_at date) ≥ market.last_complete_session_date(now)`. It is the same rule `strategy_engine.expire_stale_signals` applies at the next scan, but now it is enforced at confirmation, so a missed autopilot run cannot leave yesterday's signal approvable.

**Scope.** Both modes: it is a property of the signal, not of money. Manual and alert-action tickets keep their existing semantics, and an old manual ticket is still approvable.

> **Superseded by SEC-1 P1-D** (`docs/SEC1_P1D_REPORT.md`). A manual or Quick Action BUY entry is now approvable only while its human review is current: same NSE session window, same Kite login, unchanged content. A contradictory strategy signal (dated after today, or disagreeing with its evaluation record) is now also a permanent `STALE_SIGNAL`.

**On refusal:** `STALE_SIGNAL` (409). The ticket moves from `pending_confirmation` to `rejected` with a ticket event, using the PK-4 permanent-refusal pattern, and there is no broker call. The signal is never refreshed: its levels and dates are asserted unchanged.

**Weekend boundaries**, checked at the unit level for a Friday signal:

| When | Result |
|---|---|
| Friday evening | valid |
| Saturday, Sunday | valid |
| Monday in session, up to 15:29 | valid |
| Monday 15:30 (Monday's candle is complete) | stale |
| Tuesday | stale |

A Thursday signal is stale on Saturday.

**Limitation.** There is no NSE holiday calendar (`market.is_trading_day` covers weekends only, audit P2-6 / PK-22). On a holiday the "latest session" is off by one, and a signal could be refused one day early after a holiday. The failure is conservative: it refuses; it never allows a stale signal.

# PK-12F Daily Loss Limit

**P&L data trusted today.** `closed_trades` is the single source of realized P&L. PK-8 writes a live row only on broker-proven fills, and every row carries `mode`.

Open MTM was **not** used (user decision). Broker holdings MTM mixes long-term investment holdings with app positions, and the day-change figures are not a clean "today's trading loss".

**Measure.** `realized_live_pnl_today` is the net `SUM(realized_pnl)` where `mode='live'` and `exit_date` is today's IST date. `exit_date` is the UTC date of the fill, and every fill falls inside NSE hours, where the UTC and IST dates agree.

**Settings.** Both default to NULL (disabled; the UI shows "NOT SET"):

- `max_daily_loss_amount` (₹);
- `max_daily_loss_pct` (% of the broker-derived portfolio value).

**Precedence.** When both are set, the **tighter** (smaller rupee) limit applies. The gate blocks when today's net realized live loss **≥** the limit, so exactly-at blocks.

**Evaluation:**

- the ₹ part runs pre-claim;
- the % part runs at the final gate, using the portfolio context revalidation already fetched, so there is no second broker call;
- if the % part is enabled and the portfolio value is not usable (≤ 0, non-finite), or the P&L query fails, the result is `ACCOUNT_SAFETY_UNAVAILABLE` (503): fail closed.

**Interaction with the lock.** The limit is a **derived** state (`daily_loss_limit_hit` in the status). It never changes the manual trading lock.

**Tests cover:**

- below the limit, exactly at it, and beyond it;
- net P&L, where wins offset losses;
- exits still allowed;
- dry-run P&L excluded;
- the next day resetting;
- % evaluated at the final gate;
- the tighter limit winning in both directions;
- failing closed on an unusable portfolio value or a broken P&L query;
- disabled by default.

Integration S6 runs a real stop-out exit filled by `order_monitor`, then refuses the next entry.

# PK-12G Broker Circuit Breaker

**Settings.** Setting the threshold to NULL disables tripping; failures are still logged.

- `broker_breaker_threshold`, default **3**;
- `broker_breaker_window_minutes`, default **10**, NOT NULL.

**Counted** (`record_broker_failure`, persisted in `broker_failure_log`):

- a `submission_unknown` outcome of any live order, entry or exit, which covers network, timeout, token, 5xx and unparseable-reply failures;
- a risk-revalidation `RiskCheckUnavailableError` (`KITE_API_ERROR` / `SESSION_EXPIRED`), or a `requests` / non-definite `KiteException` during revalidation;
- a transport failure of the gate's LTP call.

**Not counted:**

- definite rejections (`OrderException`, `InputException`, `PermissionException`: validation or RMS);
- risk rejections;
- 409 lifecycle and mode refusals;
- dry-run activity.

**Policy:**

- **Trip:** the breaker opens when the failures inside the window reach the threshold. The window is the last *M* minutes **and** after the last broker success or manual reset.
  - The "after" part uses an **id watermark** (`account_safety_state.failure_watermark_id`), not a timestamp, so it is exact within one second.
  - The trip is a conditional UPDATE, so exactly one `broker_breaker_tripped` event is written; a broadcast follows.
- **Success:** a live order the broker accepted moves the watermark (failures before it stop counting). It does **not** close an open breaker.
- **Close:** only `POST /api/account-safety/breaker/reset {reason}`. It is logged as `broker_breaker_reset`; a reset while the breaker is closed is a no-op.
- **Restart:** the open state survives a restart, because it is stored in the DB.

**Fault isolation.** `record_broker_failure` and `record_broker_success` use their own connection and never raise. A bookkeeping failure cannot change an order's recorded outcome (`test_12g_bookkeeping_failure_never_changes_an_order_outcome`, for both the accepted and the UNKNOWN case).

**Never blocked:** order monitoring, reconciliation, exits, Emergency Exit and UNKNOWN recovery. Integration S11 adopts an UNKNOWN entry while the breaker is open.

**Code:** `BROKER_CIRCUIT_OPEN` (409).

# PK-12H Market-Hours / AMO Safety

**Before.** `execution.resolve_order_variety` turned any live order approved outside 09:15–15:30 into an AMO, silently.

**Now, for a live NEW entry:**

| Market | `allow_amo` | Result |
|---|---|---|
| open | anything | `regular` |
| closed | `true` | `amo`, and an `amo_entry_approved` event after the broker accepts it |
| closed | false or missing | `AMO_CONFIRMATION_REQUIRED` (409): nothing claimed, nothing sent |
| closed | `true`, with `config.USE_AMO_WHEN_MARKET_CLOSED=False` | `AMO_DISABLED` |

**Binding.** The gate decides the variety once, and `execution.execute_ticket(..., entry_variety=...)` uses it. `_execute_live` no longer reads the clock.

- If the market closes between the gate and `place_order`, the regular order goes out and Kite rejects it: a definite, safe outcome (`test_12h_variety_decided_by_the_gate_is_bound_to_execution`).
  - **Changed by SEC-1 P1-D for manual / Quick Action BUY entries.** Their final gate re-checks the review window, so an NSE open (09:15) or close (15:30) between the pre-check and the broker call withdraws the claim. The ticket returns to `pending_confirmation` (`TICKET_REVIEW_REQUIRED`) and nothing is sent. Strategy entries keep the behaviour above. See `docs/SEC1_P1D_REPORT.md`.
- It can never be silently re-routed into an AMO.
- A live entry with no decided variety raises before any broker call (backstop).

**Request.** `TicketConfirmRequest.allow_amo: bool = False`, validated by pydantic (422 for a non-boolean).

**Stale tab.** A tab that believed the market was open sends `allow_amo=false` and is refused.

**Exits: unchanged, reviewed.**

- An exit approved outside hours still goes out as an AMO without extra consent. Reducing risk must stay possible, and since PK-8 the position keeps its protective GTT until the exit fills, so an overnight AMO exit is not a naked window.
- Emergency Exit is unaffected (tested outside hours).

**Dry run.** There is no AMO question, because no broker order exists. A dry-run entry outside hours is simulated as before, with `order_variety` NULL.

**Limitation.** There is no NSE holiday calendar: on a holiday the server believes the market is open, and a regular order would be rejected by Kite. That is a safe failure. PK-22 is not built here.

# Account Safety Evaluation Order

**`precheck`** runs before the claim. It reads only the DB and the request, and returns **all** deterministic failures, in this order:

1. `TRADING_LOCKED`;
2. `DAILY_LOSS_LIMIT` (₹);
3. `DAILY_ENTRY_LIMIT` (informative);
4. `MAX_NOTIONAL_EXCEEDED`;
5. `STALE_SIGNAL`;
6. `BROKER_CIRCUIT_OPEN`;
7. `AMO_CONFIRMATION_REQUIRED` / `AMO_DISABLED`.

Checks 2–4, 6 and 7 are live only. Integration S9 and S10 assert that the full list comes back in exactly this order when everything is tripped.

**Claim.** The PK-1 claim runs with the binding 12C predicate.

**`final_gate`** runs after revalidation:

1. re-check `TRADING_LOCKED` (both modes);
2. re-check `BROKER_CIRCUIT_OPEN` (live);
3. `DAILY_LOSS_LIMIT` with the % part (live);
4. **only if nothing failed**: one `kite.ltp` call, then `STOP_ALREADY_INVALID` and `PRICE_DEVIATION_EXCEEDED`.

**PK-11 runs first.** A stale-mode tab gets `EXECUTION_MODE_CHANGED`, not a PK-12 refusal (`test_mode_binding_is_checked_before_the_account_gate`).

**Error model:**

```json
409 | 503  {"error_code": "<first code>", "message": "<all messages>",
            "failures": [{"code": "...", "message": "..."}, ...]}
```

The codes are:

- `TRADING_LOCKED`
- `DAILY_LOSS_LIMIT`
- `DAILY_ENTRY_LIMIT`
- `MAX_NOTIONAL_EXCEEDED`
- `STALE_SIGNAL`
- `PRICE_DEVIATION_EXCEEDED`
- `STOP_ALREADY_INVALID`
- `BROKER_CIRCUIT_OPEN`
- `AMO_CONFIRMATION_REQUIRED`
- `AMO_DISABLED`
- `ACCOUNT_SAFETY_UNAVAILABLE`, which is 503

There is no generic "risk failed".

**Ticket state after a refusal:**

- **pre-claim:** unchanged (`pending_confirmation`), except `STALE_SIGNAL`, which moves it to `rejected`;
- **final gate:** the claim is withdrawn through the existing `_end_claim` / `_update_if_still_claimed` compare-and-set, and `order_tag`, `submission_state` and `submitted_at` are cleared. The ticket goes back to `pending_confirmation` (a `modified` event), or to `rejected` for `STOP_ALREADY_INVALID`.

The code is in `backend/account_safety.py`: one small function per control plus the two orchestrators. The HTTP translation and the claim wiring are in `routes/tickets.py`.

# Database / Migration Changes

Every migration is idempotent and runs from `init_db`.

- **`risk_settings`** gains 7 columns via ALTER TABLE ADD COLUMN, each with `CHECK (col IS NULL OR …)`:

  | Column | Default |
  |---|---|
  | `max_order_notional` | NULL |
  | `max_new_entries_per_day` | 3 |
  | `max_entry_price_deviation_pct` | 3 |
  | `max_daily_loss_amount` | NULL |
  | `max_daily_loss_pct` | NULL |
  | `broker_breaker_threshold` | 3 |
  | `broker_breaker_window_minutes` | 10, NOT NULL |

  The changes are history-tracked by the existing `risk_settings_history`.
- **New `account_safety_state`.** One row per account: the lock fields, the breaker fields, `last_success_at`, `last_reset_at`, `failure_watermark_id` and `updated_at`. It is seeded for trading accounts.
- **New `broker_failure_log`.** Columns `id`, `account_id`, `occurred_at`, `source` and `detail`, with an index on `(account_id, occurred_at)`.
- **`system_events` rebuild** (`_ensure_account_safety_event_types`). It follows the existing `_ensure_kite_chart_event_type` pattern: a sentinel check, `foreign_keys` OFF, copy the rows, run the FK check, and keep `kite_chart_unresolved`. It adds these event types:
  - `trading_locked`
  - `trading_unlocked`
  - `account_safety_refusal`
  - `broker_breaker_tripped`
  - `broker_breaker_reset`
  - `amo_entry_approved`

**Scratch validation.** `migration_check.py` in the scratchpad (not committed) made an online-backup copy of the live DB and ran `init_db` **twice**:

- `integrity_check` = ok after each run, and there are 0 FK violations;
- the second run is identical (schema and row counts);
- no existing table's row count changed, and the 8 `system_events` rows were preserved;
- `risk_settings` holds the documented defaults and `account_safety_state` is seeded unlocked;
- the live DB SHA-256 and mtime are unchanged.

# UI Changes

The UI is functional and informational only; the server enforces everything. The changes are in `frontend/index.html`, `app.js` and `style.css`.

- **Account-safety bar** under the mode banner, always visible:
  - a persistent red **"TRADING LOCKED — no new entries (reason)"** pill;
  - an amber **"BROKER CIRCUIT BREAKER OPEN"** pill, with the reason as its tooltip, and a **Reset breaker** button;
  - a summary line: entries today X/limit · realized live P&L today, with the loss limit or "NOT SET" and "HIT" · max order value or "NOT SET" · market open/closed ("live entries need explicit AMO") · recent broker failures;
  - a **Lock/Unlock trading** button that asks for a confirmation and a reason;
  - the bar turns red-tinted while entries are blocked.
- **Refresh.** `account_safety_changed` over WebSocket, `tickets_changed`, every approval and every settings save.
- **Confirm dialog:**
  - for a live new entry while the market is closed, an unticked **"Send as AMO"** checkbox appears, and the execution note explains that the server refuses the approval without it;
  - `allow_amo` is sent only when the visible box is ticked;
  - the Approvals-card and Signal-Detail quick-approve paths route such a ticket to this dialog instead of approving directly.
- **Errors.** Every account-safety code has a specific title, and the sticky toast lists **every** failure message; there is no retry or auto-approve. An `AMO_CONFIRMATION_REQUIRED` refusal re-opens the dialog, with the box unticked.
- **Settings → Risk Settings** has an "Account safety" group with the 7 fields. An empty field means disabled, and every field is sent explicitly on save.

**Browser checks.** They ran against `tests/demo_server.py` (Edge, headless; FakeKite and a throwaway DB).

- The existing `tests/e2e_ui.py` passed **55/55**.
- A scratch PK-12 script (not committed) passed **21/21**:
  1. lock from the UI with a reason; the pill persists across a reload;
  2. another session switches to LIVE, the market is closed (a Sunday), and approving from Approvals opens the dialog with the AMO box;
  3. a locked approval is refused with its specific toast, and no broker order is placed;
  4. after unlocking, approval without ticking is refused and the dialog re-opens, still with no order;
  5. ticking the box sends it as an AMO; entries today becomes 1/3 in the bar;
  6. Settings show the defaults, a max order value is saved, and the bar shows it;
  7. no overflow at 820 px;
  8. no JS errors besides the 2 deliberately provoked 409 responses.

PK-19 redesign work was not started.

# Event / Audit Changes

| Event | When |
|---|---|
| `trading_locked` / `trading_unlocked` | on a real change only, with the reason |
| `account_safety_refusal` | every hard refusal: pre-check, the claim's daily limit, or the final gate. It records the ticket, side, quantity, symbol, mode, phase, and every code with its message. |
| `broker_breaker_tripped` | once per trip, with the count, window, threshold and the latest failure |
| `broker_breaker_reset` | a manual reset, with the reason |
| `amo_entry_approved` | a live entry the broker accepted as an AMO with explicit consent |

Ticket events add a `modified` event on a final-gate withdrawal, and a `rejected` event for a stale signal or a crossed stop.

Routine passes are not logged.

# Concurrency Protection

- **Daily entries.** The count and the reservation happen in one conditional UPDATE: the PK-1 claim with the extra predicate. This is not a separate "SELECT count, then place the order". The four race tests, driven by barriers with no sleeps, prove that one slot gives one order, and that UNKNOWN and in-flight claims hold their slots. The mutation that drops the predicate (M12C) is caught **only** by these race tests, which confirms that they, not the sequential pre-check, test the binding guarantee.
- **Lock and breaker transitions** are conditional UPDATEs, so there are no duplicate events.
- **PK-1 ownership is unchanged.** Two confirms of the same ticket still produce one claim; the existing PK-11 × PK-1 race tests still pass.
- **Residual window.** The lock and breaker are re-checked at the final gate. A change made in the milliseconds between that re-check and `place_order` is not seen by that request. There is no lock across the broker call, by design (PK-1).

# Multi-Account Future Compatibility

- Every PK-12 **limit** is a column of the per-account `risk_settings`. Every piece of PK-12 **state** is keyed by `account_id` (`account_safety_state`, `broker_failure_log`, `system_events`). The entry count and the P&L query filter by `account_id`.
- The limits belong to the execution-enabled trading account (`get_trading_account()`), never to a consolidated family portfolio. The % daily loss uses that account's broker portfolio value.
- **Current single-account assumptions** (documented, not changed):
  - `execution_settings` (the global mode) is a single row;
  - `get_state` creates a row on demand for any account, but seeding covers only `account_type='trading'` accounts;
  - the broker client is the single trading session.

  Multi-account support was not implemented.

# Tests Added

| File | Tests | Content |
|---|---|---|
| `tests/test_account_safety.py` | 105 | 12A: 9 · 12B: 10 · 12C: 10 · 12D: 20 · 12E: 14 · 12F: 13 · 12G: 15 · 12H: 14 |
| `tests/test_account_safety_integration.py` | 19 | 5 concurrency races, the 12 required scenarios (S1–S12), mode binding before the gate, CSRF on the new endpoints |

**The 12 required scenarios:**

1. a strategy ticket that passed risk is blocked by the lock;
2. the daily count blocks;
3. the notional cap blocks;
4. the stale-signal guard blocks;
5. a price that moved too far blocks;
6. the daily loss is breached after a real exit fill;
7. mixed broker errors (revalidation, LTP, UNKNOWN submission) trip the breaker;
8. an outside-hours approval needs explicit AMO;
9. an exit works while **every** entry control blocks;
10. Emergency Exit works while every control blocks;
11. UNKNOWN recovery is adopted while the breaker is open;
12. dry-run activity is excluded from every live counter.

**Existing tests migrated (no assertion weakened):**

- **The new shared `market_open` fixture in `conftest.py`** pins the NSE clock open. It was added to 5 `test_baseline_api` and 4 `test_review_changes` tests that place live entries on the wall clock. They previously became AMOs at night; now they would be refused without consent. Modules with their own clock fixtures keep them.
- **`test_review_changes::test_live_entry_off_grid_prices_are_snapped_and_sent_as_amo`** now sends `allow_amo: true`, because its purpose is an after-hours AMO entry.
- **CSRF:** the route-enumerating test covers the new routes automatically (+4 cases).

# Mutation Results

`pk12_mutations.py` in the scratchpad ran once on the final code. For each mutant it applied one exact-anchor edit, ran the two PK-12 files (124 tests), restored the file and compared SHA-256 hashes.

| # | Mutation | Result |
|---|---|---|
| M12A | trading-lock check always passes | **caught**, 9 failures |
| M12B | notional check always passes | **caught**, 5 |
| M12C | daily-count predicate removed from the atomic claim | **caught**, 4 (all four race tests) |
| M12D | price-deviation check never fails | **caught**, 7 |
| M12D2 | stop-sanity check never fails | **caught**, 5 |
| M12E | stale-signal check always passes | **caught**, 7 |
| M12F | daily-loss check always passes | **caught**, 8 |
| M12G | circuit-breaker check always passes | **caught**, 5 |
| M12H | AMO consent assumed | **caught**, 6 |

After restore, the hashes matched: `backend/account_safety.py` `3d7e5df989f8…` and `backend/routes/tickets.py` `f47904a05812…`. No code changed after the mutation run. Only this report was written afterwards.

# Targeted Repeated Results

- **Targeted PK-12 suite ×10.** It covers `test_account_safety`, `test_account_safety_integration`, `test_baseline_api`, `test_review_changes`, `test_mode_binding`, `test_pk9_pk11_cross` and `test_emergency_exit_isolation`. Every run gave **243 passed**, 0 failed.
- **Concurrency subset ×20.** It is `-k "race or concurrent"` over `test_account_safety_integration`, `test_live_safety` and `test_pk9_pk11_cross`: the PK-12C races plus the PK-1, PK-2 and PK-11 races. Every run gave **15 passed**, 0 failed.

No flakes were seen. Synchronisation uses barriers and FakeKite call hooks; there are no sleeps.

# Full Suite ×3

These are three runs on the final code, after the mutation run.

| Run | passed | xfailed | failed | xpassed | time |
|---|---|---|---|---|---|
| 1 | 643 | 0 | 0 | 0 | 90.4 s |
| 2 | 643 | 0 | 0 | 0 | 95.2 s |
| 3 | 643 | 0 | 0 | 0 | 88.1 s |

643 = 515 (baseline) + 4 (new routes in the CSRF route enumeration) + 124 (PK-12).

# Known Limitations

1. **No NSE holiday calendar** (PK-22 / P2-6). On a holiday the server believes the market is open (12H), and the stale-signal session date is off by one (12E). Both fail safe: Kite rejects the regular order, and a signal may be refused a day early.
2. **Daily loss is realized-only.** Open MTM losses on live positions are not counted until they are closed.
3. **A claimed entry holds its daily slot** from the claim until it either reaches the broker or is withdrawn. A concurrent approval can therefore be refused `DAILY_ENTRY_LIMIT` by an entry that the final gate later withdraws. This is conservative, by design.
4. **There is no raw per-day order cap** for exits or GTT operations; they must never be limited. Only new live entries are counted.
5. **`protection_failed` is not counted by the breaker.** The result does not distinguish a GTT validation rejection from a transport failure. Order monitoring and PK-6/PK-7 still surface it urgently.
6. **Notional for a MARKET entry** uses the reference price, bounded by the 12D deviation limit.
7. **Lock/breaker residual window.** A change made between the final-gate re-check and `place_order` (milliseconds) is not seen by an already-running confirmation.
8. **Single-account assumptions**, as listed under Multi-Account Future Compatibility.

# Deferred Work

These dependencies were discovered while working; they are documented only.

- **PK-22:** the NSE holiday list, needed by 12E and 12H.
- **PK-13:** partial fills. The entry count treats a partially filled entry as one entry (correct), but the notional and loss figures use the requested quantity.
- **PK-19:** a lifecycle UI for per-failure detail; today the failures are listed in a toast.
- **Breaker signals from background loops** (`order_monitor` / `reconciliation` polling failures) could feed the breaker. They were deliberately not wired, to keep the breaker small and tied to the order path.
- **The Emergency Exit re-cancel fallback** (PK-10 limitation) is unaffected. The trading lock now at least stops new entries during such an incident.

# Deviations From Audit

1. **The circuit breaker does not "auto-set the trading lock"** as the audit suggested. It is a separate, persisted, derived state with its own manual reset, per the brief ("do not necessarily alter the user-configured trading_locked"). Both block new entries, and the manual lock's meaning stays exactly what the user set.
2. **Daily loss uses realized P&L only.** The audit said "realised P&L plus open MTM"; the user chose realized only (see 12F).
3. **The notional cap is disabled by default** rather than being a fixed ₹ amount; no rupee value is hard-coded. The UI warns "NOT SET".
4. **The trading lock also blocks dry-run approvals** (user decision); the audit was silent on this.
5. **Two-phase gate.** A pre-claim DB-only check plus a final pre-broker gate, instead of one check "in the confirm gate". This avoids broker calls, claims and revalidation for a locked or breaker-open account, and still gives the lock the last word right before the broker.
6. **The daily-count limit is atomic inside the PK-1 claim,** instead of "a count of ticket_events executed today, checked at confirm", which could race.
7. **Exits outside hours keep the automatic AMO.** The audit's AMO opt-in is applied to new entries only; reducing risk stays frictionless, and PK-8 keeps the GTT until the fill.
8. **Pre-commit of PK-0 to PK-5.** It was not part of PK-12, but was done at the user's request so that the PK-12 commit contains only PK-12.
