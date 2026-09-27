# PK-3 — Execution Mode Belongs to the Position: Implementation Report

| | |
|---|---|
| Packet | PK-3 from `docs/OPUS55_AUDIT_REPORT.md` (fixes P0-3) |
| Base revision | `13ae3f8` on `feature/signal-detail-and-strategy-revision`, plus the uncommitted PK-0, PK-1 and PK-2 work |
| Backups taken first | tag `backup-pre-pk3` and branch `backup/pre-pk3` (both at `13ae3f8`); tag `backup-pre-pk3-worktree` (commit `7c29fb0`: a snapshot of the whole working tree *including* the uncommitted and untracked PK-0..PK-2 files, made through a temporary index so the real index and working tree were not touched); `backend/kite_terminal.db.pre_pk3_bak` |
| Committed | No |
| Audit report | Not modified |

---

## 1. Summary

**Primary invariant.** A LIVE broker position is never modified, closed or adjusted by DRY RUN execution, and it is never shown as if it had been. The reverse also holds: a DRY RUN position never causes a real broker order.

**Rules.** The global Dry Run / Live toggle now decides **new entries only**. The execution mode of an existing position belongs to that position:

- **Exits inherit the mode of the position they close.** Every exit-creating path stamps the mode when it creates the ticket: the strategy exit scan, Emergency Exit All, and the linked alert-action full close. The trailing engine's synthetic exit already did this.
- **Confirmation re-reads the referenced position.**
  - If the exit's mode differs from the position's mode, confirmation refuses with HTTP 409 `EXIT_MODE_MISMATCH`. Nothing is written and no broker call is made.
  - If the exit has no mode yet, the claim assigns the position's mode. This covers the legacy case: exits created before PK-3, of which the real DB may still hold some.
- **The mode is fixed in the atomic claim.** It is resolved *before* the PK-1/PK-2 compare-and-set `pending_confirmation → confirmed` and written in that same UPDATE, together with PK-2's order tag and `submitting` state. After that nothing writes it again.

**Breakeven.** `resolve_stop_adjustment` now acts on `ticket["mode"]`:

- a live position's broker GTT is always modified;
- a dry-run position only has its recorded stop moved, with no broker call.

**Emergency Exit All.** It no longer reads the global mode. It covers live positions only; that was already true in practice and is now stated explicitly. It sells them for real whatever the toggle says. The dialog wording that claimed it "respects the current Dry Run mode" is gone.

**Tests.** Before: 165 passed, 13 xfailed. After: **180 passed, 10 xfailed, 0 failed, 0 xpassed**, in each of 3 consecutive full runs.

- All three P0-3 strict xfails were converted into passing regression tests.
- 12 new PK-3 test cases were added.
- With the old global-mode behaviour temporarily put back, 9 of the 15 PK-3 tests fail (section 13), which shows they detect the defect.

## 2. Files changed

| File | Change |
|---|---|
| `backend/routes/tickets.py` | New `_claim_mode()` (L469). `confirm_and_execute_ticket` uses it instead of `get_execution_mode()` (L512), before the PK-2 submission metadata is built. The `confirmed` event names where the mode came from. `create_alert_action_sell` stamps `mode` on a linked full close (L275). |
| `backend/routes/kill_switch.py` | The global mode is no longer read. The exit ticket INSERT stamps `position["mode"]` (L176). The unfilled-entry cancel is gated on `pos["mode"]` (L104). The system event reports a per-mode count (L133). |
| `backend/strategy_engine.py` | The strategy exit-ticket INSERT stamps `position["mode"]` (L471). `resolve_stop_adjustment` uses `ticket["mode"]` (L619). The docstring is updated. |
| `backend/signal_chart.py` | `execution_mode` in the Signal Detail payload is the ticket's own mode once stamped, and the global mode otherwise. |
| `backend/routes/strategies.py`, `backend/todays_actions.py` | The stop-adjustment proposal lists also return the position's `t.mode`, which the breakeven dialog needs. |
| `frontend/index.html` | Emergency Exit dialog wording. The `emergency-exit-mode-label` span was removed. |
| `frontend/app.js` | New `ticketExecMode(t)` helper, used by `executionNote` and both approve buttons (Approvals card, Signal Detail). The breakeven dialog wording comes from `proposal.mode`. The dead `emergencyExitModeLabel` binding and assignment were removed. |
| `tests/test_live_safety.py` | P0-3 section rewritten: 3 xfails converted, 12 new cases, and 6 small helpers. |
| `docs/OPUS55_PK3_IMPLEMENTATION_REPORT.md` | This report. |

There are no schema changes and no migrations.

## 3. Previous mode flow (traced before editing)

| Path | Where mode came from | Consequence |
|---|---|---|
| Manual entry, alert-action BUY, strategy entry | Global mode at the claim (`confirm_and_execute_ticket`: `mode = execution.get_execution_mode()`, written by `_transition(... mode=mode)`) | Correct for new entries. |
| Strategy exit (`strategy_engine._create_exit_ticket`) | Created with `mode` NULL, so the global mode at the claim | **P0-3.** A live position exited under DRY RUN is simulated and then marked closed. |
| Explicit exit confirm (`POST /api/tickets/{id}/confirm`) | Same as above | Same defect. A dry-run position confirmed under LIVE would send a **real SELL**. No path creates such an exit today, so this was latent. |
| Emergency Exit All | Exit tickets created with NULL mode, so the global mode at the claim. Also `mode = get_execution_mode()` gated the unfilled-entry cancel, and fed the log text. | **P0-3.** Under DRY RUN every live position is "exited" by simulation, and an unfilled live entry order is left alive at the broker. |
| Alert-action SELL, linked full close | NULL, so the global mode | Same defect as the strategy exit. |
| Alert-action SELL, unlinked (untracked holding) | NULL, so the global mode | A new action, not the exit of a tracked position. See section 18. |
| Trailing engine synthetic exit (GTT closure) | `ticket["mode"]`, inherited | Already correct. |
| Trailing ratchet (`trailing_engine._tick_once`) | Query `mode = 'live'` | VERIFIED SAFE (section 8). |
| Breakeven (`resolve_stop_adjustment`) | Global mode | **P0-3.** Under DRY RUN the DB stop of a live position moves and the broker GTT does not. Under LIVE, a dry-run position fails with "no active protective GTT". |
| Direct GTT routes (`/api/gtts/...`) | No mode involvement; always the broker | Unchanged. They act on broker objects, not tickets. |
| UNKNOWN recovery (`order_monitor`) | Stamped `mode = 'live'` on the row (`status='confirmed'` query) | Never read the global mode. Already correct. |

`risk_engine.list_open_positions` returns **live positions only** (`mode = 'live'`). As a result, the exit scan, the breakeven candidate check and Emergency Exit have never seen a dry-run position. `test_baseline_api.test_emergency_exit` already pins this.

## 4. New mode ownership rules

1. **The global mode applies only to tickets with `closes_ticket_id IS NULL`.** These are new entries, plus alert-action sells of holdings that no ticket tracks. It is read once, inside the claim.
2. **A ticket with `closes_ticket_id` set runs in the referenced position's mode.** The global mode is never consulted.
3. **The claim stamps the mode and it never changes afterwards.** The claim is the compare-and-set out of `pending_confirmation`. No code path writes `mode` after that (section 10).
4. **Everything after the claim reads the ticket's own `mode`.** This covers execution (`execution.execute_ticket`), result recording (`_record_execution_result`), the order monitor, the trailing engine, breakeven and the journal. None of them read the global mode.

## 5. New-entry mode resolution

For a new entry, `_claim_mode` returns `execution.get_execution_mode()`, called exactly where the old code called it: after the status pre-check and before the conditional UPDATE. The PK-2 flow is unchanged:

- for a live claim, `order_tag`, `submission_state='submitting'` and `submitted_at` are computed and written in the **same** `_transition` UPDATE as `status='confirmed'` and `mode`;
- a lost race is still a 409 with no broker call;
- a ticket is never moved back to `pending_confirmation`.

The PK-1 concurrency test still holds its barrier inside `get_execution_mode`. It passes unchanged, which confirms that the entry path still reads the mode at the same point.

## 6. Exit inheritance behaviour

**At creation (primary mechanism).**

| Path | Mode stamped |
|---|---|
| `strategy_engine._create_exit_ticket` | `position["mode"]` |
| `kill_switch._create_emergency_exit_ticket` | `position["mode"]` |
| `create_alert_action_sell`, only when it links `closes_ticket_id` to exactly one open position of the same quantity | That position's `mode` |
| `trailing_engine` synthetic exit | `ticket["mode"]` (already the case) |

Stamping a mode on a *pending* ticket is invisible to every limit and count. Every query that filters `mode = 'live'` also requires `status IN ('executed','confirmed')`. This was checked in `risk_engine`, `order_monitor`, `trailing_engine` and `automation`.

**At confirmation (backstop, `_claim_mode`).**

1. The referenced ticket is re-read (same account).
2. If it is missing or its `mode` is NULL, for example a BUY that never executed: **409 `EXIT_MODE_UNRESOLVED`**.
3. If `ticket.mode` is set and differs from the position's mode: **409 `EXIT_MODE_MISMATCH`**. Nothing is claimed, no event is written, the broker is not called, and the position and its GTT are untouched. The user can reject the ticket.
4. If `ticket.mode` is NULL (a legacy exit created before PK-3), the position's mode is assigned in the claim. The `confirmed` event says so: `Confirmed in live mode — inherited from position #N (assigned at claim)`. A stamped exit reads `… (stamped at creation)`.

**Why the pre-claim read is race-free.** The referenced position is already executed, and an executed ticket's mode is never rewritten (section 10). The value read cannot change between the read and the UPDATE.

**Outcomes.**

| Situation | Result |
|---|---|
| Live position, global `dry_run`, exit confirmed | A real broker SELL, PK-2 tag and `acknowledged` state, exit `mode='live'`. The position closes only through the real exit path. |
| Dry-run position, global `live`, exit confirmed | No broker call of any kind. The exit is simulated, `mode='dry_run'`, has no submission metadata, and is journaled as `dry_run`. |

## 7. Emergency Exit behaviour

- **Scope: live positions only.** `list_open_positions` already returned only live positions; PK-3 keeps that and states it. It is the simplest and safest product behaviour: the real emergency control is about real money, and simulated positions are left alone. No redesign was needed.
- **Exits.** Each exit ticket is created with the position's mode, which is always `live`. It then goes through the same `confirm_and_execute_ticket` path, so a real MARKET SELL is placed even when the toggle is on DRY RUN.
- **Unfilled entries.** An unfilled live entry is now cancelled at the broker under a DRY RUN toggle too. It is gated on `pos["mode"]`.
- **PK-2 behaviour is intact.** If an exit for a position is in flight or UNKNOWN, it is reported as `exit_outcome_unknown` and not resubmitted.
- **System event.** It now reads `N position(s) exited, each in its own execution mode (live: n, dry_run: m): …`.
- **Converted test.** The PK-0 scenario "global dry_run + live open position + Emergency Exit = fake close" is now the regression test `test_p0_3_emergency_exit_in_dry_run_sells_live_position`.

## 8. Breakeven behaviour and trailing engine

**`resolve_stop_adjustment`** now uses `mode = ticket["mode"]`:

- **Live position** (any toggle): the existing path runs unchanged. It fetches the LTP, runs the through-the-stop guard, then calls `kite.modify_gtt` on the position's GTT. The DB stop is written only after the broker accepts. The DB stop always equals the broker trigger.
- **Dry-run position** (any toggle): only `current_stop_loss` and the `stop_adjusted_breakeven` event are written. There is no LTP fetch and no broker call. The response and the event say `dry_run`.

`check_breakeven_candidates` still proposes only for live positions. The dry-run branch is a backstop, not a new paper-mode feature.

**Trailing engine: VERIFIED SAFE, unchanged.**

- `_tick_once` selects only `mode = 'live'`, executed, trailing, open and filled tickets, and acts on the broker GTT.
- Its GTT-closure path writes the synthetic exit with `ticket["mode"]`.
- It never reads the global mode.
- Dry-run lifecycle simulation belongs to PK-20 and was not added.

## 9. UNKNOWN / recovery mode behaviour

- A live exit whose submission becomes UNKNOWN was claimed with `mode='live'` in the PK-2 claim. For a legacy exit, that live mode was inherited in that same claim.
- `order_monitor` selects live claims by the stamped `mode = 'live' AND status = 'confirmed'`. When it adopts an exit, it closes the position through `journal.close_position_for_exit`, whose mode check compares the stamped modes of the buy and the sell.
- A later global toggle has no effect on any of this. It is pinned by `test_p0_3_unknown_live_exit_recovered_after_toggle_to_dry_run_stays_live`: an UNKNOWN live exit, then a toggle to `dry_run`, then monitor, trailing and reconciliation cycles. The exit ends `adopted`, still `live`, with exactly one broker SELL and no `dry_run` journal row.
- PK-2's unknown-submission guarantees were not modified: no retry, forward-only resolution, conditional writes, and `needs_review`.

## 10. Mode immutability audit

A search of `backend/` found every statement that writes `trade_tickets.mode`:

| Writer | When | Value |
|---|---|---|
| `routes/tickets.py::_transition` via `confirm_and_execute_ticket` (L528) | Only the compare-and-set out of `pending_confirmation` | `_claim_mode()`: global for new entries, the position's mode for exits |
| `routes/kill_switch.py::_create_emergency_exit_ticket` INSERT | Creation | `position["mode"]` |
| `strategy_engine.py::_create_exit_ticket` INSERT | Creation | `position["mode"]` |
| `routes/tickets.py::create_alert_action_sell` INSERT | Creation | The linked position's mode, or NULL if unlinked |
| `trailing_engine.py` synthetic exit INSERT | Creation (already `executed`) | `ticket["mode"]` |
| `db.py` table-rebuild migrations (L562, L1194) | Startup schema migration | Copy the existing column through unchanged |

Paths that do **not** write `mode`:

- **Entry INSERTs** (`_create_ticket_internal`, strategy entry `strategy_engine` L373): mode stays NULL until the claim.
- **All `UPDATE trade_tickets SET …`**, including:
  - `run_risk_checks_and_transition`
  - `revert_rejection`
  - `edit_ticket`
  - `expire_stale_signals`
  - `update_ticket_if` and every `_record_execution_result` branch
  - every `order_monitor` resolution
  - the breakeven stop update
  - the trailing ratchet
  - `journal.close_position_for_exit`

**Conclusion.** Once a ticket has left `pending_confirmation`, nothing overwrites its mode, and in particular nothing overwrites it from global settings. `test_p0_3_stamped_mode_survives_global_toggles` pins this: it toggles through the real `POST /api/execution/mode` four times and runs every background loop after each toggle.

## 11. UI wording changes

**Emergency Exit dialog.** The old text was "…closed with a MARKET SELL order…, respecting the current Dry Run mode." It now reads: "Every open **live** position below will be closed with a real MARKET SELL order (an unfilled entry is cancelled instead). Existing positions are exited in their own execution mode — the Dry Run / Live toggle does not apply here. Simulated (dry-run) positions are not included."

**Approval dialogs.** The execution note in the confirm dialog and the approve buttons (Approvals card and Signal Detail) now use `ticketExecMode(t) = t.mode || state.executionMode`. A pending exit carries its inherited mode, so a live exit shows "LIVE — … real money" / "Place order" even when the toggle is on Dry Run. A new entry still follows the toggle.

**Signal Detail mode pill and warning.** These come from the payload's `execution_mode`, which is now the ticket's own stamped mode when it has one.

**Breakeven confirm dialog.** "LIVE position — the protective GTT at the broker is modified" / "Dry-run position — only the app's record is updated", taken from the proposal's position mode. If the mode is missing, it shows the live wording as the cautious default.

**Unchanged.**

- The ticket mode chips (`modeChipHtml(t.mode)`) already showed the stamped mode.
- The global banner and the new-ticket form preview describe the global mode, which governs new entries, so they are correct as they are.
- No other UI work was done.

## 12. Tests converted from xfail

| PK-0 xfail | Now (regression test) | Asserts |
|---|---|---|
| `test_p0_3_exit_ticket_confirmed_in_dry_run_does_not_close_live_position` | **A** `test_p0_3_exit_ticket_confirmed_in_dry_run_sells_live_position` | Exactly one real SELL of 5 TCS. Exit `mode='live'`, `executed`, order id in the broker book. PK-2 `acknowledged` state and tag equal to the broker order's tag. Not "Simulated". No mode leak. `confirmed` event names the inherited mode. |
| `test_p0_3_emergency_exit_in_dry_run_does_not_close_live_position` | **B** `test_p0_3_emergency_exit_in_dry_run_sells_live_position` | Exactly one real SELL. The EE ticket is stamped `live` at creation (event says "stamped at creation"). No mode leak. The system event reports `live: 1, dry_run: 0`. |
| `test_p0_3_breakeven_in_dry_run_keeps_db_stop_equal_to_broker_gtt` | **D** `test_p0_3_breakeven_in_dry_run_modifies_live_position_gtt` | One `modify_gtt` on the position's GTT. DB stop == broker trigger == 3000.05. Response mode `live`. |

## 13. New tests

All are in `tests/test_live_safety.py`, P0-3 section.

| Test | Scenario |
|---|---|
| **C** `test_p0_3_dry_run_position_exit_under_global_live_sends_nothing[stamped_at_creation / unset_legacy]` | Dry-run position, global live. No broker call of any kind. Exit `dry_run`, simulated, no submission metadata. Journal row `dry_run`. |
| `test_p0_3_exit_whose_mode_differs_from_its_position_is_refused[live-dry_run / dry_run-live]` | Backstop. 409 `EXIT_MODE_MISMATCH`. Ticket still `pending_confirmation` with its mode and no submission metadata. No events, no broker calls. Position still open, GTT still active. |
| `test_p0_3_exit_whose_position_mode_is_unknown_is_refused` | Referenced ticket never executed (mode NULL): 409 `EXIT_MODE_UNRESOLVED`, nothing executed. |
| `test_p0_3_exit_tickets_inherit_position_mode_at_creation` | The real strategy exit path and the linked alert-action sell, both under global dry_run, create `pending_confirmation` exits with `mode='live'`. |
| `test_p0_3_unlinked_alert_action_sell_follows_global_mode` | An unlinked alert-action sell stays unstamped and takes the global mode (dry_run: no broker call). |
| `test_p0_3_emergency_exit_in_dry_run_cancels_unfilled_live_entry_at_broker` | Under global dry_run, an unfilled live entry order is really cancelled at the broker. No SELL. |
| `test_p0_3_emergency_exit_leaves_dry_run_positions_alone` | Emergency Exit sells the live position. The dry-run position gets no exit ticket and is not closed. |
| **E** `test_p0_3_breakeven_on_dry_run_position_under_global_live_touches_no_broker` | No `modify_gtt`, no broker mutation, no `ltp` call. DB stop moved to 3000.05. Event and response say `dry_run`. |
| **F** `test_p0_3_stamped_mode_survives_global_toggles` | Live entry (real create → confirm → fill), dry-run entry, and a pending strategy exit. Four toggles through the real API, with every background loop run after each. The modes never change and the pending exit stays pending. |
| **G** `test_p0_3_unknown_live_exit_recovered_after_toggle_to_dry_run_stays_live` | UNKNOWN live exit, then toggle to dry_run, then monitor adoption. `adopted`, `live`, one SELL, no dry-run journal row. |

**Mutation check.** The fix was temporarily reverted: `_claim_mode` replaced by `get_execution_mode()` in the confirm path, and `resolve_stop_adjustment` given back the global mode. The PK-3 set then showed **9 failed, 6 passed**. A, B, C×2, mismatch×2, unresolved, D and E all fail. The 6 that still pass are the creation-stamping tests (that code was not reverted) and behaviour that was already safe: G, EE leaving dry-run positions alone, and the unlinked alert-action sell. The fix was then restored.

**Helpers added:**

- `_dry_run_position`
- `_broker_calls(env, since=0)`: state-changing broker calls after the scenario's own setup
- `_db_account`
- `_strategy_exit_for`: uses the real `strategy_engine._create_exit_ticket`
- `_toggle_mode`: the real `POST /api/execution/mode`
- `_confirmed_detail`

## 14. Full-suite results

`python -m pytest tests -q` (no sleeps; FakeKite only; no network):

| Run | passed | xfailed | failed | xpassed |
|---|---|---|---|---|
| Baseline (before PK-3) | 165 | 13 | 0 | 0 |
| 1 | 180 | 10 | 0 | 0 |
| 2 | 180 | 10 | 0 | 0 |
| 3 | 180 | 10 | 0 | 0 |

The count goes from 165 to 180: 3 converted plus 12 new cases.

Before the tests were rewritten, the backend-only change produced exactly **3 strict XPASS** (the three P0-3 xfails) and nothing else. This confirmed the change was scoped to P0-3.

## 15. Repeated targeted-test results

| Set | Runs | Result each run |
|---|---|---|
| PK-3 (`-k p0_3`, 15 tests) | 5 | 15 passed |
| PK-1/PK-2 (`-k "p0_1 or p0_2 or p0_4a"`, 48 tests) | 2 | 48 passed |

## 16. Remaining P0 xfails (10, all strict, none XPASS)

| Finding | Test(s) | Packet |
|---|---|---|
| P0-4B | `test_p0_4b_stale_exit_cannot_sell_already_closed_position` | PK-4 |
| P0-4C | `test_p0_4c_pending_entries_cannot_jointly_exceed_open_position_limit` | PK-4 |
| P0-5 | `test_p0_5_oco_stop_or_target_fill_closes_and_journals_position[stop, target]` | later |
| P0-6 | `test_p0_6_triggered_gtt_without_fill_keeps_position_open[unfilled, partial]` | later |
| P0-7 | `test_p0_7_unfilled_exit_keeps_protection_and_position_open[limit_market_hours, market_amo_after_hours]` | later |
| P0-8 | `test_p0_8_cross_site_post_is_rejected[confirm, gtt_cancel]` | later |

P0-4B still fails on its assertion, as it should. The stale exit correctly inherits `live` and still sends a second SELL, because PK-3 checks only the *mode* of the referenced position, not whether it is still open.

## 17. PK-4 dependencies discovered

1. **The stale-exit check belongs next to `_claim_mode`.** PK-4's check that the position is still open, has the same quantity and has no executed sibling exit should re-read the referenced position in the same place. `_claim_mode` already fetches that row; PK-4 can widen the SELECT and add its conditions there, before the claim.
2. **Editing a draft exit.** `edit_ticket` accepts any `draft` ticket and can change `transaction_type`/`quantity` while `closes_ticket_id` and a stamped `mode` stay. An exit is `draft` only for the instant between INSERT and its risk check, so this is practically unreachable, but PK-4's edit/revert tightening should forbid editing closing tickets outright.
3. **`revert_rejection` of an exit.** It keeps the inherited mode, which is correct. PK-4's stale-exit validation will cover a reverted exit whose position has since closed.
4. **Confirm now has two new 409 codes** (`EXIT_MODE_MISMATCH`, `EXIT_MODE_UNRESOLVED`). PK-4's new refusals should follow the same pattern: refuse before the claim, write nothing.

## 18. Deviations from the audit recommendation

1. **Legacy exits with no mode get it assigned at the claim.** The audit says to set the mode at ticket creation and refuse a mismatch at confirm. PK-3 does both. It also assigns the position's mode in the claim for an exit whose mode is still NULL. Exits created before PK-3 are like that (the real DB can hold pending ones), and refusing them would make those pending exits un-approvable. This is the "creation path left it unset" case allowed by the packet brief. The event log records it.
2. **Emergency Exit is not refused in dry run.** The audit's PK-3 row says "Emergency Exit refuses in dry run when live positions exist" (see also UX note 3). That was the interim recommendation from before the root cause was fixed. With exits inheriting the position's mode, Emergency Exit under a DRY RUN toggle now performs the **real** exit of live positions. For an emergency control that is the safer outcome, and it is what the packet brief requires. So there is no refusal; the dialog explains the behaviour instead.
3. **Unlinked alert-action sells keep following the global mode.** A sell of a holding that no ticket tracks (a partial reduction, or an ambiguous link) has no position whose mode it could inherit. It is treated as a new action. Under DRY RUN it is simulated, and it does not record any tracked position as closed. If the product later wants every sell of a real broker holding to be real, that is a separate decision, outside P0-3.
4. **Breakeven's dry-run branch is kept.** It is a backstop only: the candidate check still proposes only for live positions. Paper-mode lifecycle work is left to PK-20.
