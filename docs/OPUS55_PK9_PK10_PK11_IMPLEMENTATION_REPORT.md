# OPUS55 PK-9 / PK-10 / PK-11 Implementation Report: cancellation, Emergency Exit isolation, approval mode binding

| | |
|---|---|
| Packets | PK-9 (P1-1), PK-10 (P1-2) and PK-11 (P1-7), implemented one after another with a checkpoint between each. PK-12 and later packets were not started. |
| Baseline before | **455 passed**, 0 xfailed, 0 failed, 0 xpassed. Re-measured on the untouched code before the first edit. |
| Result after | **515 passed, 0 xfailed, 0 failed, 0 xpassed**, in 3 identical full runs on the final code. 515 = 455 + 60 new tests. |
| Backups taken first | Tag `backup-pre-pk9-pk11` and branch `backup/pre-pk9-pk11`, both at `6a99630`. Tag `backup-pre-pk9-pk11-worktree` (commit `e518a5c`) is a whole-working-tree snapshot that includes the uncommitted PK-0 to PK-5 files and untracked files. It was built through a temporary index, so the real index, HEAD and working tree were untouched. `backend/kite_terminal.db.pre_pk9_pk11_bak` was taken with the SQLite online-backup API (`integrity_check` = ok, 18 tickets). |
| Schema | Unchanged. No new column, status, `ticket_events` type, `system_events` type, index or migration. `backend/db.py` was not touched. |
| Live DB | Untouched. SHA-256 `1378f8b4…` and mtime are identical before and after. All tests use a per-test temporary DB and FakeKite, with no network. |

# Executive Summary

- **PK-9 (manual cancel).**
  - **Before:** `POST /api/orders/{id}/cancel` marked the linked ticket `cancelled` as soon as Kite *accepted* the cancel request.
  - **Now:** it only requests the cancellation and records a "Cancellation requested at broker" event. The ticket is left exactly as it was.
  - `order_monitor` remains the only authority on the order's terminal state:
    - CANCELLED with 0 filled: entry → `cancelled` and its GTT removed; exit → `failed`, with the position still open and protected;
    - COMPLETE: the fill wins;
    - partial: `partial`.
  - An EXITING exit's position stays open, protected by its GTT, until the monitor sees the outcome. The PK-8 reopen workaround was not reintroduced.
- **PK-10 (Emergency Exit).**
  - Each position is handled by `_exit_one` inside its own `try/except Exception`. One failing position (a broker error, a DB error or a bug) never stops the others.
  - The result per position is one of `exit_submitted`, `already_exiting`, `exit_outcome_unknown`, `refused`, `failed`, `entry_cancelled`, `risk_check_unavailable` or `closed`. The response also carries a `summary` of counts.
  - A failure *after* the exit was claimed is reported as `exit_outcome_unknown`, never `failed`, because an order may exist.
  - The `emergency_exit_all` system event is always written with per-outcome counts, and it never says "exited".
- **PK-11 (mode binding).**
  - A NEW-entry confirm must carry `expected_mode`, the mode the user was shown. The server compares it with the global mode *before* the atomic claim.
  - A missing or different mode gets 409 (`EXECUTION_MODE_REQUIRED` / `EXECUTION_MODE_CHANGED`) with nothing written and nothing sent.
  - The compared value is the value the claim stamps and execution runs on, so a switch after the check cannot change what executes.
  - Exits ignore `expected_mode` and keep their position's mode (PK-3).
  - A real mode change broadcasts `mode_changed`. Open tabs update, and an open approval of a new entry is closed.
  - A browser check against the demo server confirmed:
    - the broadcast updates a stale tab;
    - that tab's still-open DRY RUN dialog is refused by the server, with the warning shown and no order placed.
- **Validation.**
  - 60 new tests.
  - All 4 focused mutations were caught, and every mutated file was restored with its hash checked.
  - Combined targeted suite ×10: 348 passed each run.
  - Race subset ×20: 23 passed each run.
  - Full suite ×3: 515 passed each run.

# PK-9 Manual Cancel

## Previous behavior

`routes/orders.py:cancel_order` called `kite.cancel_order`. On success it ran:

- `UPDATE trade_tickets SET status = 'cancelled'` on the linked ticket, whatever its role;
- a `ticket_events` row of type `rejected`.

The order monitor only follows `executed`/`pending` tickets. So after that write:

- a cancelled-but-filled order was never reconciled;
- an exit's position was never re-evaluated;
- a partial fill read "cancelled".

## New behavior

1. `kite.cancel_order` is called through the existing `_call` wrapper. A `KiteException` gives 502 and a `TokenException` gives 401, both **before any DB write**. Any other exception also propagates before any write.
2. On acceptance only:
   - a `ticket_events` row of type `modified` (an existing type): "Cancellation requested at broker for order X via Orders & Trades. Kite accepting the request is not a cancellation — the order may still fill; the order monitor records the broker's final state.";
   - the `system_events` row (existing type `order_cancelled_manually`), whose detail now reads `cancel requested: order_id=…, ticket=…`.
3. **No change to the ticket's status or any other column.**
4. Response: `{cancel_requested: true, order_id, linked_ticket_id, message}`. The misleading `cancelled: true` was removed.
5. UI: the toast reads "Cancellation requested for order X. Kite decides the outcome; the ticket updates when the order monitor sees it." It was previously a success toast saying "cancelled".

No monitor change was needed. The ticket stays in `order_monitor.pending_tickets` and resolves through the existing PK-8 paths.

## Entry cancellation

An OPEN entry stays `executed`/`pending`, with its GTT set. Its row is byte-identical to the row before the request.

- When the monitor sees CANCELLED with 0 filled: `cancelled`/`cancelled`, and the GTT is deleted.
- The Emergency Exit's own unfilled-entry cancel (`kill_switch._cancel_unfilled_entry`) already left the ticket alone. Its event wording was corrected to "cancellation requested at broker".

## Exit cancellation

An EXITING exit stays EXITING:

- the original position keeps `closed_at` NULL and its active GTT;
- there is no journal row;
- `exit_in_flight_ticket_id` is still set.

When the monitor sees CANCELLED, the exit becomes `failed`/`cancelled` and the position is still open and protected. A new exit can then own it (see the cross-packet section).

## Fill/cancel race

- **Kite accepts the cancel, but the exchange had already filled the order** (the test sets COMPLETE after the accepted request):
  - exit: the monitor finalizes it once, at the broker fill price and time, then removes the GTT;
  - entry: the monitor records the fill and keeps the GTT.
- **A monitor cycle runs *inside* the cancel call,** before Kite applies it (a FakeKite `on_call` hook): it sees OPEN and leaves the exit EXITING. The next cycle sees CANCELLED and fails it.
- **The cancel is accepted but not yet processed** (the order is still OPEN): the exit stays EXITING. A later COMPLETE closes it exactly once.

## Tests

`tests/test_cancel_order.py` has 12 tests.

| # | Test | Required case |
|---|---|---|
| 1 | `test_cancel_open_entry_leaves_ticket_for_the_monitor` | cancel an OPEN entry: no immediate cancellation; row identical; one event; system event says "cancel requested" |
| 2 | `test_broker_cancelled_entry_is_resolved_by_the_monitor` | the broker later reports CANCELLED; the monitor resolves it and removes the GTT |
| 3 | `test_cancel_exiting_exit_keeps_position_open_and_protected` | cancel an EXITING exit: position open and protected before and after the monitor |
| 4 | `test_exit_fill_during_cancel_race_wins` | the exit fills during the cancel race: COMPLETE wins, one journal row |
| 5 | `test_entry_fill_during_cancel_race_wins` | the entry fills during the cancel race: COMPLETE wins |
| 6 | `test_partially_filled_entry_cancel_is_not_reported_as_cancelled`, `test_partially_filled_exit_cancel_is_not_reported_as_cancelled` | partial fill then cancel: `partial`, never `cancelled`; the exit still owns the position |
| 7 | `test_repeated_cancel_request_is_safe` | repeated cancel is safe; a broker refusal of the third gives 502, ticket unchanged |
| 8 | `test_broker_cancel_failure_leaves_ticket_untouched` ×4 | `OrderException` → 502, `NetworkException` → 502, `TokenException` → 401, raw `ReadTimeout` → 500. Row identical, no event, no system event, order still OPEN |

Restated: `test_baseline_api::test_order_and_gtt_books_and_cancel` now asserts `cancel_requested` and that the ticket is still `executed`, with a comment saying why.

## Limitations

- A user-requested exit cancel still raises the monitor's existing **urgent** "exit order cancelled — nothing was sold, position still OPEN" alert. It is accurate, but alarming for a deliberate action. Softening it for a user-initiated cancel belongs to the lifecycle UX work (PK-19).
- Partial fills keep the PK-8 `partial` / NEEDS REVIEW model. PK-13 was not started.
- FakeKite's `cancel_order` still marks any order CANCELLED, even a COMPLETE one; the real Kite refuses that. The race tests model "fill wins" by setting the broker's final state after the accepted request, which is the realistic ordering. FakeKite was not changed.

# PK-10 Emergency Exit

## Previous behavior (current PK-8 code, reconciled before editing)

PK-8 had already added `already_exiting` and `exit_outcome_unknown`, and it caught `HTTPException` from `confirm_and_execute_ticket`, reporting it as `refused`. It did **not** isolate any other exception from:

- the `_exit_in_flight` query;
- `_cancel_unfilled_entry`'s DB write;
- `_create_emergency_exit_ticket`;
- `run_risk_checks_and_transition`, beyond `RiskCheckUnavailableError`;
- a post-broker DB error inside confirm.

Any of these aborted the loop with a 500. The remaining positions were not attempted, and the system event was never written. The audit's recommendation (wrap confirm in try/except) was applied on top of the PK-8 structure, not as a replacement for it.

## New per-position isolation

- The loop body is `_exit_one(conn, kite, account, pos, ltp_data, attempt)`. It holds the unchanged PK-2/PK-4/PK-8 logic, and records the id of any exit ticket it creates in `attempt`.
- The loop wraps each call in `try/except Exception`. On failure it:
  1. rolls back the open transaction;
  2. logs the exception;
  3. appends `_isolated_failure(...)`;
  4. continues with the next position.
- `_isolated_failure` never guesses:
  - **No exit ticket was created:** `failed` / `EMERGENCY_EXIT_ERROR`, "no order was sent for this position. The position is still OPEN — exit it in Kite."
  - **The ticket is still `draft`/`pending_confirmation`:** it is withdrawn with the PK-1 compare-and-set (`_transition` → `rejected`, plus an event), so no stale emergency SELL remains approvable. Result: `failed`.
  - **The ticket was already claimed** (`confirmed`/`executed`): `exit_outcome_unknown`, "An order may exist — check Kite; the order monitor follows it up. Not resubmitted." The claimed ticket is left for `order_monitor` (PK-2).
- If `list_open_positions` itself fails, the response is 503 `EMERGENCY_EXIT_UNAVAILABLE` and the system event is still written.

## Outcome model

| Status | Meaning | Order sent by this click? |
|---|---|---|
| `exit_submitted` | accepted by Kite; the position stays open (EXITING) until the fill | yes |
| `already_exiting` | an EXITING or partial exit already owns the position (PK-8) | no |
| `exit_outcome_unknown` | a claimed or UNKNOWN exit exists (PK-2), this click's submission was ambiguous, or it failed after the claim | possibly; never resubmitted |
| `refused` | refused at confirmation (PK-4 `linked_exit_problem`), or **new**: the exit failed creation-time validation (`EXIT_VALIDATION_FAILED`; previously the raw status such as `risk_rejected` was returned) | no |
| `failed` | the broker definitively rejected the order, or **new**: an unexpected error before any claim (`EMERGENCY_EXIT_ERROR`) | no |
| `entry_cancelled` | the entry was unfilled and a cancel was requested at the broker (the key is kept for compatibility; the message now says "cancellation requested") | cancel request only |
| `risk_check_unavailable` | unchanged | no |
| `closed` | a proven fill already (not reachable for live positions) | — |

No position is ever `closed` on submission. `_result_status` still maps an accepted live order to `exit_submitted`, and `closed_at` is only set by the PK-8 monitor path.

## System-event changes

- The event is written after every position has been attempted. It goes through `_log_summary_event`, which catches its own failure, so a broken audit write still returns the per-position results.
- The detail looks like this:

  `Emergency Exit: 3 position(s) attempted, each in its own execution mode (live: 3, dry_run: 0) — exit_submitted 2, already_exiting 0, exit_outcome_unknown 0, refused 0, failed 1, entry_cancelled 0, risk_check_unavailable 0, closed 0: TCS=exit_submitted, INFY=failed, SUNPHARMA=exit_submitted`

- The PK-3 substring "each in its own execution mode (live: N, dry_run: M)" is preserved, and an existing test still asserts it.
- **UI:**
  - the toast title reads "Emergency exit: A submitted, B not exited";
  - a sticky error toast lists **every** non-submitted position as `SYMBOL [status]: message`;
  - it never says "closed".

## Tests

`tests/test_emergency_exit_isolation.py` has 15 tests.

| # | Test | Required case |
|---|---|---|
| 1 | `test_middle_position_raising_does_not_stop_the_others[ticket_creation / risk_transition]` | three positions, the middle one throws: first and third submitted, exactly 2 SELLs; no approvable INFY ticket is left |
| 2 | `test_already_exiting_position_gets_no_duplicate_sell` | one position already EXITING: no duplicate SELL |
| 3 | `test_unknown_exit_position_gets_no_duplicate_sell` | one position UNKNOWN: no SELL; the ticket is untouched |
| 4 | `test_position_refused_by_lifecycle_validation_does_not_stop_the_others`, `test_exit_failing_validation_at_creation_is_refused_not_raw_status` | a lifecycle refusal: the others continue |
| 5 | `test_broker_rejection_on_one_position_is_a_per_position_failure`, `test_exception_after_the_claim_is_unknown_never_failed` | broker exception → per-position `failed`; a post-claim exception → `exit_outcome_unknown`, with exactly one SELL |
| 6 | `test_system_event_written_after_partial_failure`, `test_system_event_write_failure_still_returns_results` | the event is written with correct counts; a failed event write does not lose the results |
| 7 | `test_result_lists_every_position_once_with_its_outcome` | an accurate per-position list |
| 8 | `test_no_position_is_reported_closed_on_submission` | never `closed` on submission; closed only after the monitor sees the fill |
| 9 | `test_empty_portfolio_is_handled_cleanly`, `test_listing_failure_is_logged_and_reported` | empty portfolio: 200, zero counts, event written; a listing failure gives 503 and an event |
| 10 | `test_mixed_outcomes_in_one_click` | five positions: `already_exiting`, `exit_outcome_unknown`, `exit_submitted`, `failed`, `refused`; one new SELL |

## Limitations

- **A second Emergency Exit after an entry-cancel request.** If Kite refuses the re-cancel of an order it already cancelled, `_cancel_unfilled_entry` returns None. The loop then falls back to a MARKET SELL of shares that were never bought. Kite's CNC holdings check would normally reject that order, and the result is then reported as `failed`. This behaviour predates this bundle and is left for PK-12/PK-13; the SELL quantity is also still the requested quantity after a partial fill (P1-3).
- The per-position result table appears in toasts. A dedicated result dialog is PK-19 UI work.

# PK-11 Mode Binding

## expected_mode protocol

- `POST /api/tickets/{id}/confirm` accepts an optional JSON body, `TicketConfirmRequest {expected_mode: "dry_run" | "live" | null}`.
- An invalid value gets 422 before any handler code runs.
- **A NEW entry** (`closes_ticket_id IS NULL`, which includes an alert-action sell of an untracked holding) **must** send it. This was decided with the user: the server fails closed.
- **An exit** may omit it, and anything it sends is ignored.

## Server validation

Order of operations in `confirm_and_execute_ticket`:

1. **Status check.** A `confirmed`, UNKNOWN or `executed` ticket gives `CONFLICT_INVALID_STATE`, unchanged, whatever the mode sent.
2. **`_claim_mode`.** For an entry, it returns the global mode read once.
3. **New: `_require_displayed_mode(ticket, mode, expected_mode)`, entries only:**
   - missing → 409 `EXECUTION_MODE_REQUIRED`, with `current_mode`;
   - different → 409 `EXECUTION_MODE_CHANGED`: "Execution mode changed (you saw X, the server is now Y). Review the ticket again before approving. Nothing was executed.", with `expected_mode` and `current_mode`;
   - no DB write, no event, no broker call. The ticket stays `pending_confirmation` and can be approved again with the correct mode.
4. `_refuse_stale_exit`, exits only (PK-4).
5. The PK-1 atomic claim, which stamps `mode`, the very value checked in step 3.
6. `execute_ticket(kite, ticket)`, which runs on the stamped `ticket["mode"]`.

Because steps 3, 5 and 6 use the same value, a mode switch between the check and the claim cannot change what executes. `test_mode_switch_after_the_check_cannot_change_what_executes` flips the global mode to LIVE right after a passing DRY RUN check; the ticket executes as a dry run with no broker calls.

## WebSocket broadcast

- `execution.set_execution_mode` broadcasts `{"type": "mode_changed", "mode": ..., "previous_mode": ...}` through `ticker_manager.broadcast`, the existing channel.
- It does so only when the mode actually changed and the write was committed. A no-op set, or a refused LIVE switch with no real-money confirmation, broadcasts nothing.
- `backend.ticker` imports nothing from `backend`, so the module-level import creates no cycle.

## Frontend behavior (`frontend/app.js`)

- **`API.confirmTicket(id, expectedMode)`** sends `{expected_mode}` only when it is given.
- **Each approval path captures the mode its view showed, before the user decides:**
  - the ticket dialog (`state.activeConfirmShownMode`, set in `openConfirmDialog`);
  - the Approvals card;
  - Signal Detail.

  `approveTicket(id, shownMode)` sends that captured value. It never re-reads the mode at send time, so a broadcast that arrives while a dialog is open cannot silently upgrade the approval to LIVE. Exits send nothing.
- **`ticketExecMode`:** an entry awaiting approval (`draft`/`pending_confirmation`/`risk_rejected`) always shows the global mode. This fixes a latent display bug: a reverted entry could still carry a `mode` stamped by an earlier refused claim, which the UI showed while the server would use the global mode.
- **On `EXECUTION_MODE_CHANGED` / `EXECUTION_MODE_REQUIRED`:**
  1. the UI re-fetches the mode and re-renders the banner and views;
  2. it shows a sticky warning, **"Execution mode changed. Review the ticket again before approving."**;
  3. there is no retry and no auto-approve.
- **On a `mode_changed` WebSocket message:**
  1. the UI updates `state.executionMode` and the banner;
  2. it closes an open ticket dialog for a new entry;
  3. it refreshes the views;
  4. it shows a sticky "Execution mode changed to LIVE/DRY RUN in another session" warning.

  The tab that made the switch itself (`state.ownModeSwitch`) gets no "another session" toast.

## Tests

`tests/test_mode_binding.py` has 24 tests.

| # | Test | Required case |
|---|---|---|
| 1, 2 | `test_mismatched_mode_is_refused_without_any_broker_call[dry_run-live / live-dry_run]` | 409, no `place_order`/`place_gtt`, row and events unchanged |
| — | `test_missing_expected_mode_on_entry_is_refused` ×3 (no body, `{}`, `null`), `test_invalid_expected_mode_is_rejected_before_anything` | fail-closed and 422 |
| 3 | `test_matching_live_follows_the_normal_live_path` | executed, order tag, GTT, one order |
| 4 | `test_matching_dry_run_is_simulated` | no broker calls |
| — | `test_stale_stamped_mode_on_a_reverted_entry_does_not_decide` | the global mode decides; a stale stamp does not |
| 5 | `test_live_position_exit_ignores_expected_mode_and_global_mode` ×3, `test_dry_run_position_exit_ignores_expected_mode_and_global_mode` ×3 | exits follow `position.mode` |
| 6 | `test_mode_switch_broadcasts_mode_changed`, `test_refused_live_switch_broadcasts_nothing` | the `mode_changed` event is emitted only on a real change |
| 7 | `test_stale_tab_cannot_approve_live_while_showing_dry_run` | stale-tab simulation, then approval after a refresh |
| 8 | `test_mismatch_is_refused_before_the_atomic_claim` | `_transition` is never called; no `mode`, `order_tag` or `submission_state` is written |
| 9 | `test_ticket_stays_approvable_after_a_mismatch` | three refusals, then one approval, one `confirmed` event |
| — | `test_mode_switch_after_the_check_cannot_change_what_executes` | the checked value binds the execution |
| 10 | `test_unknown_and_executed_tickets_are_unaffected` ×3 | `CONFLICT_INVALID_STATE`, rows unchanged, no broker calls |

**Existing-test migration.** Entry confirms now send the mode a freshly loaded tab shows. A new conftest helper, `displayed_mode(db)`, reads the mode directly from the DB, so it never interacts with race hooks such as the barrier on `get_execution_mode`. Only the call changed; no assertion changed.

- 49 HTTP entry-confirm call sites, updated by a line-exact script that fails loudly on an unexpected line. They are in `test_alert_action` 2, `test_baseline_api` 6, `test_csrf_protection` 4, `test_exit_lifecycle` 1, `test_gtt_watcher` 1, `test_live_safety` 30 and `test_review_changes` 5.
- 5 direct `tickets.confirm_ticket(...)` calls in `test_live_safety` now pass `req=_displayed(env)`.
- Tests asserting a 409 for another reason (a draft, rejected, risk_rejected or UNKNOWN ticket) now send the correct mode, so they still fail for their original reason rather than a missing mode.
- Exit confirms and the attacker-shaped CSRF requests were left as they were.

## Stale-tab protection (browser check)

**Setup.** A scratch Playwright script (Edge, headless) ran against `tests/demo_server.py` (FakeKite and a throwaway DB). It is not committed. **13/13 checks passed**:

1. Tab A starts in DRY RUN and opens the Approvals dialog for a new entry: "Approve this trade (dry run)?".
2. Another session POSTs mode=live. Tab A's `#mode-pill` flips to **LIVE** via `mode_changed`, and the "Execution mode changed" warning toast appears.
3. The user clicks the stale dialog's Approve anyway. The dialog is still titled dry run, and its captured `expected_mode` is `dry_run`. The server refuses it, the UI shows "Execution mode changed. Review the ticket again before approving.", the ticket stays `pending_confirmation`, and the broker order book is unchanged (0 → 0).
4. An artificially stale `fetch` with `expected_mode: "dry_run"` returns `[409, "EXECUTION_MODE_CHANGED"]`.
5. Switching back to DRY RUN also reaches tab A, and there are no page errors.

The existing `tests/e2e_ui.py` also passed **55/55** against a fresh demo server. It includes a real dry-run approval through the UI, which now sends `expected_mode`.

# Cross-Packet Interactions

These are in `tests/test_pk9_pk11_cross.py`, 9 tests.

| Interaction | Test | Result |
|---|---|---|
| Cancellation + EXITING + Emergency Exit (+ cancelled exit) | `test_cancel_requested_exit_then_emergency_exit_never_doubles_the_sell` | Before the monitor runs: `already_exiting`, no second SELL, position protected. After CANCELLED: the exit `failed` and the position is unchanged. A second Emergency Exit sends exactly one new SELL. |
| Emergency Exit + UNKNOWN | `test_emergency_exit_with_unknown_exit_sends_nothing_for_it` | `exit_outcome_unknown`, zero SELLs, GTT active, ticket untouched |
| Cancellation + order-monitor race | `test_monitor_cycle_during_the_cancel_request_resolves_consistently`, `test_cancel_not_yet_applied_then_fill_closes_once` | A monitor cycle inside the cancel call sees OPEN and stays EXITING; the next cycle fails the exit. Cancel not yet processed, then a fill: closed once, one journal row, one `delete_gtt`. |
| Mode binding + PK-1 atomic confirmation | `test_two_correct_mode_confirms_still_place_exactly_one_order`, `test_stale_and_correct_confirm_race_executes_once_in_the_shown_mode` | Both threads meet at a barrier inside the mode read. Two correct confirms → one order, the loser `CONFLICT_INVALID_STATE`. Stale + correct → the stale one `EXECUTION_MODE_CHANGED`, one live execution. |
| Mode binding + PK-5 CSRF | `test_valid_expected_mode_never_bypasses_the_csrf_guard` ×3 | A foreign Origin with the header, a same Origin without the header, and no Origin with no header all get 403, even with a valid `expected_mode`. The ticket is untouched and no order is placed. |

Global invariants re-checked by the full suite:

- the PK-1 atomic claim;
- PK-2 UNKNOWN recovery;
- PK-3 immutable position mode;
- PK-4 fresh revalidation and stale/duplicate-exit refusal;
- PK-5 CSRF;
- PK-6 all-position GTT monitoring;
- PK-7 broker-proven GTT closure;
- PK-8 EXITING until an explicit fill, with the journal only after proven closure.

No PK-1 to PK-8 test was weakened. The only edits to existing tests are the call-site migration above and the one restated cancel assertion.

# Files Changed

The diff is against HEAD `6a99630`. Every file below was clean in git before this work, so none of the pre-existing uncommitted hunks are mixed in.

| File | ± | Change |
|---|---|---|
| `backend/routes/orders.py` | +23/−6 | PK-9: `cancel_order` makes no status write; adds the requested-cancel event, system-event detail and `cancel_requested` response; docstring |
| `backend/routes/kill_switch.py` | +159/−70 | PK-10: `_exit_one`, per-position isolation, `_isolated_failure`, summary and event helpers, listing-failure 503, `EXIT_VALIDATION_FAILED` mapping, entry-cancel wording |
| `backend/routes/tickets.py` | +41/−5 | PK-11: `_require_displayed_mode`; `confirm_and_execute_ticket(expected_mode=…)`; `confirm_ticket` body |
| `backend/models.py` | +7 | PK-11: `TicketConfirmRequest` |
| `backend/execution.py` | +5 | PK-11: `mode_changed` broadcast on a real change |
| `frontend/app.js` | +84/−20 | PK-9 cancel toast; PK-10 Emergency Exit toasts; PK-11 `expected_mode` capture and send, mode-error handling, `mode_changed` handler, `ticketExecMode` for pending entries |
| `tests/conftest.py` | +13/−1 | `displayed_mode(db)` helper |
| `tests/test_cancel_order.py` | new, 203 lines | PK-9, 12 tests |
| `tests/test_emergency_exit_isolation.py` | new, 355 lines | PK-10, 15 tests |
| `tests/test_mode_binding.py` | new, 272 lines | PK-11, 24 tests |
| `tests/test_pk9_pk11_cross.py` | new, 170 lines | cross-packet, 9 tests |
| `tests/test_live_safety.py`, `test_baseline_api.py`, `test_review_changes.py`, `test_csrf_protection.py`, `test_alert_action.py`, `test_exit_lifecycle.py`, `test_gtt_watcher.py` | small | the PK-11 call-site migration; the restated cancel assertion in `test_baseline_api` |
| `docs/OPUS55_PK9_PK10_PK11_IMPLEMENTATION_REPORT.md` | new | this report |

Not changed:

- `backend/db.py`, `order_monitor.py`, `journal.py`, `trailing_engine.py`, `risk_engine.py`, `reconciliation.py`;
- `frontend/index.html`, `frontend/style.css`;
- `tests/fake_kite.py`;
- `docs/OPUS55_AUDIT_REPORT.md`.

# Focused Mutation Results

One scratch script ran once on the final code. For each mutant it:

1. applied one exact-anchor edit;
2. ran the 8 targeted files (348 tests);
3. restored the file;
4. compared SHA-256 hashes.

| # | Mutation | Result |
|---|---|---|
| M9 | restore the immediate `UPDATE … SET status = 'cancelled'` in `cancel_order` | **caught**, 12 failures (8 in `test_cancel_order`, 3 cross-packet, the restated baseline test) |
| M10 | re-raise inside the per-position `except` (no isolation) | **caught**, 5 failures (both middle-position cases, post-claim unknown, event after partial failure, mixed) |
| M11a | remove the `expected_mode != mode` comparison | **caught**, 7 failures (both mismatch directions, stale stamp, stale tab, before-claim, still-approvable, stale/correct race) |
| M11b | remove the `mode_changed` broadcast | **caught**, 1 failure (`test_mode_switch_broadcasts_mode_changed`) |

After restore, every hash matched: `backend/execution.py` `b92cef17…`, `backend/routes/kill_switch.py` `614de1d3…`, `backend/routes/orders.py` `f22dcc1c…`, `backend/routes/tickets.py` `0d7a4455…`. No code changed after the mutation run.

# Targeted Repeat Results

- **Combined PK-9/10/11 targeted suite ×10.** It covers the 4 new files plus `test_exit_lifecycle`, `test_live_safety`, `test_baseline_api` and `test_csrf_protection`. Every run gave **348 passed**, 0 failed.
- **Concurrency/race subset ×20.** It is `-k "race or concurrent or confirm_while or during or cross"` over `test_live_safety`, `test_gtt_watcher`, `test_pk9_pk11_cross` and `test_cancel_order`: the PK-1 and PK-2 barrier and thread races, the PK-7 watcher races, the cancel/fill races and all cross-packet tests. Every run gave **23 passed**, 0 failed.

No flakes were seen. There are no sleeps; synchronization uses barriers, events and FakeKite call hooks.

# Full Suite ×3

These are three runs on the final code, after the mutation run.

| Run | passed | xfailed | failed | xpassed |
|---|---|---|---|---|
| 1 | 515 | 0 | 0 | 0 |
| 2 | 515 | 0 | 0 | 0 |
| 3 | 515 | 0 | 0 | 0 |

515 = 455 (baseline) + 12 (PK-9) + 15 (PK-10) + 24 (PK-11) + 9 (cross-packet). All P0 regression tests pass.

# Remaining P1 Findings

These are not addressed by this bundle; the packet numbers follow the audit's plan.

| Finding | Packet |
|---|---|
| P1-3 partial fills not modelled (`filled_quantity`, GTT resize) | PK-13 |
| P1-4 sector check effectively disabled | PK-14 |
| P1-5 exposure basis (`t1_quantity`, positions) | PK-15 |
| P1-6 account-level controls (trading lock, daily loss limit, order caps, notional cap, price deviation, stale-signal guard) | PK-12 |
| P1-10 orphan and oversized GTT reconciliation | PK-16 |
| P1-11 manual entries beyond BUY+CNC | PK-17 |
| P1-12 secrets and deployment hardening | PK-18 |
| P1-13 dry run cannot rehearse the lifecycle | PK-20 |
| P1-14 backtest realism | PK-21 |

P1-8 (the GTT-closure / explicit-exit race) was largely covered by the PK-4 owner index and the PK-7/PK-8 conditional closure; it was not re-audited here. P1-9 was partly addressed in PK-6.

# Deferred Work

These dependencies were discovered while working and are documented only:

- **Emergency Exit re-cancel fallback.** After an entry-cancel request, a later Emergency Exit whose re-cancel Kite refuses falls back to a MARKET SELL of the requested quantity (PK-10 limitations). This needs PK-13's fill model, and ideally the PK-12 trading lock.
- **User-initiated exit cancel alert.** It still produces the urgent "exit cancelled" alert, which should be softened in PK-19.
- **`set_execution_mode` atomicity.** It reads the previous mode and writes the new one on separate connections. The worst case is a missing or duplicated `mode_changed` / `mode_switched` note under a concurrent double switch. Binding is unaffected, because confirm checks the mode it actually stamps.
- **Stale tabs from before deploy.** Such a tab (old JavaScript) sends no `expected_mode` and is refused with `EXECUTION_MODE_REQUIRED`, which is the fail-closed choice. Its generic error toast asks for a refresh; after a reload it works normally.
- **Uncommitted PK-0 to PK-5 work.** The working tree still holds `backend/security.py` (untracked), `db.py`, `risk_engine.py`, `main.py`, `config.py`, `strategy_engine.py`, the other `M` files and the PK-0 to PK-5 reports. They are captured in `backup-pre-pk9-pk11-worktree` but not in any branch commit. They were deliberately **not** included in this bundle's commit. Committing them is a separate decision for the owner.

# Deviations From Audit

1. **`expected_mode` is required, not optional, for new entries.** The audit said "the client sends `expected_mode`; the server returns 409 on mismatch". With the user's agreement, a missing value is also refused (`EXECUTION_MODE_REQUIRED`), so a tab running pre-PK-11 JavaScript cannot approve LIVE while showing DRY RUN. The cost was the mechanical test migration described above.
2. **`mode_changed` is broadcast only on an actual change,** not on every call to `set_execution_mode`, matching the existing `mode_switched` system-event rule.
3. **The mode check is placed between `_claim_mode` and the claim,** and compares the exact value the claim stamps. A separate re-read of the global mode could have left a check-then-act gap.
4. **Emergency Exit reports a post-claim exception as `exit_outcome_unknown`, not `failed`.** The audit's "record failure for that position" would have told the user no order existed when one might. A pre-claim failure is `failed`, and its unexecuted emergency ticket is withdrawn so it cannot be approved later.
5. **Validation refusals at creation are reported as `refused`** (`EXIT_VALIDATION_FAILED`) rather than the raw ticket status. No existing test asserted the raw status.
6. **No new event types.** The cancel request is logged as a `modified` ticket event and the existing `order_cancelled_manually` system event, with corrected wording, avoiding a `ticket_events` / `system_events` table rebuild.
7. **Frontend `ticketExecMode`** now shows the global mode for entries awaiting approval. This goes slightly beyond the audit and was necessary: otherwise a stale stamp from a refused claim would be displayed and sent, and permanently refused.
