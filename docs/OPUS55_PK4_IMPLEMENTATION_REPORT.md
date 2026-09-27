# PK-4 — Confirmation Is a Fresh Execution Gate: Implementation Report

| | |
|---|---|
| Packet | PK-4 from `docs/OPUS55_AUDIT_REPORT.md` (fixes P0-4B and P0-4C; P0-4A was closed by PK-1) |
| Base revision | `13ae3f8` on `feature/signal-detail-and-strategy-revision`, plus the uncommitted PK-0..PK-3 work |
| Backups taken first | tag `backup-pre-pk4` and branch `backup/pre-pk4` (both at `13ae3f8`); tag `backup-pre-pk4-worktree` (commit `21ad54a`: a snapshot of the whole working tree *including* the uncommitted and untracked PK-0..PK-3 files, made through a temporary index so the real index and working tree were not touched); `backend/kite_terminal.db.pre_pk4_bak` (SQLite online-backup API; `integrity_check` = ok) |
| Committed | No |
| Audit report | Not modified |

---

## 1. Executive summary

A ticket that was valid when it was created is no longer assumed valid when it is approved. Confirmation re-checks everything that can have changed in between, and it does so before any broker call.

**New entries** (and unlinked alert-action sells):
- The ticket first wins the PK-1 atomic claim.
- All risk checks are then re-run against the broker's portfolio *as it is now*: holdings and margins are fetched fresh, and the `risk_check_results` saved at creation are never trusted.
- Only if every check still passes may the order be placed.
- **If a check fails**, the ticket goes to `risk_rejected` with the audit line "Passed creation-time risk checks but failed confirmation-time revalidation", and the request gets 409.
- **If the checks cannot run**, it fails closed: the ticket goes to `rejected` with "Risk could not be revalidated; no order was sent", and the request gets 503.
- **Entries that are claimed but unresolved now count toward every limit.** These are in flight, UNKNOWN or needs_review. Without that, two approvals racing each other would each see room that only one of them has.

**Exits** (tickets with `closes_ticket_id`):
- Before the claim, the referenced position is re-read. The exit must reference an existing executed BUY of the same account, and the PK-3 mode check still runs first.
- The position must also:
  - not be closed;
  - have no other exit that has executed;
  - have no other exit that is claimed (in flight / UNKNOWN / needs_review);
  - have a quantity equal to the exit's.
- Otherwise the request gets 409 with a specific error code and the broker is never called.
- **A permanent problem** (position closed, not an executed BUY, quantity mismatch) also rejects the stale exit, so it stops being offered for approval.
- **A merely unresolved sibling** leaves the ticket untouched.

**One exit owns a position.**
- A partial unique index enforces it at the storage layer: at most one exit per position may be `confirmed` or `executed`.
- When an exit executes, whether acknowledged by the broker, adopted by the monitor, or recorded as a GTT closure, every other exit of that position still awaiting approval is **superseded** (`rejected`, "Superseded by exit ticket X").

**Background writers can no longer resurrect a withdrawn ticket.** The risk engine's `draft → pending_confirmation / risk_rejected / cancelled` transition is a compare-and-set on `draft`. So are:
- edit;
- revert;
- delete and archive;
- strategy signal expiry.

A result arriving after a cancel is discarded.

**Tests.** 2 PK-0 strict xfails were converted and 30 new test cases added. The full suite gives **212 passed, 8 xfailed, 0 failed, 0 xpassed** in each of 3 consecutive runs.

## 2. Files changed

| File | Change |
|---|---|
| `backend/risk_engine.py` | `_IN_FLIGHT_ENTRY_WHERE` (L196), folded into `_symbol_exposure`, `_open_position_count` and `_open_risk_sum` with self-exclusion. `ExitProblem` + `linked_exit_problem()` (L385), used by `_check_exit_or_reduction`. `_transition_from_draft()` (L510): `run_risk_checks_and_transition` (L537) is now compare-and-set. |
| `backend/routes/tickets.py` | `_refuse_stale_exit()` (L503), `_update_if_still_claimed()` (L529), `_revalidate_at_confirm()` (L549), `_end_claim()`, `_raise_lost_claim()`. `confirm_and_execute_ticket`: stale-exit refusal before the claim; `IntegrityError` → 409 at the claim (L643); revalidation after the claim. `revert_rejection`, `edit_ticket` (exit tickets refused; conditional UPDATE) and `delete_ticket` (conditional delete/archive) all tightened. |
| `backend/execution.py` | `supersede_sibling_exits()` (L136). |
| `backend/journal.py` | `close_position_for_exit` supersedes siblings (covers the confirm path and the monitor's adoption). |
| `backend/trailing_engine.py` | `_handle_gtt_closure`: commits earlier writes first; refuses to record a second exit when one already owns the position (pre-check + `IntegrityError` backstop, L217) → `_handle_owned_closure()` (L239); supersedes siblings after a closure (L229). |
| `backend/strategy_engine.py` | `expire_stale_signals` (L485): conditional cancel. |
| `backend/routes/kill_switch.py` | A confirmation refused for one position is reported as `status: "refused"` and Emergency Exit continues with the rest (L134). |
| `backend/db.py` | `_ensure_single_exit_owner_index()` (L872), called in `init_db` after the last `trade_tickets` rebuild (L1334). `logger` added. |
| `frontend/app.js` | Edit hidden for exit/reduction tickets. Emergency Exit shows a sticky error toast listing positions that were **not** exited (refused, exit outcome unknown, risk unavailable), which were previously hidden inside "N position(s) processed". |
| `tests/test_live_safety.py` | P0-4 section: 2 xfails converted, 30 new cases, helpers. |
| `docs/OPUS55_PK4_IMPLEMENTATION_REPORT.md` | This report. |

Two stray, empty SQLite sidecar files (`kite_terminal.db.pre_pk3_bak-shm/-wal`) were deleted. They were created by this session's read-only inspection of that backup before the work started, and they appear only in the worktree snapshot commit.

## 3. Entry revalidation flow

`confirm_and_execute_ticket` for a ticket with `closes_ticket_id IS NULL`:

1. Status pre-check: only `pending_confirmation` (PK-1).
2. `_claim_mode`: the global mode for a new entry (PK-3), read at the same point as before.
3. **Atomic claim** `pending_confirmation → confirmed`, with the mode and PK-2's `order_tag` / `submitting` / `submitted_at`, and the `confirmed` event, committed. A loser gets 409 and never proceeds.
4. **`_revalidate_at_confirm`.** It runs `risk_engine.run_checks(..., ctx=None)`: fresh `holdings()` + `margins()`, plus holdings+positions for an alert-action sell. The six checks are:
   - minimum R:R;
   - capital per trade;
   - single-stock exposure;
   - sector exposure;
   - max open positions;
   - total portfolio risk.

   It never reads the stored `risk_check_results`.
   - **Pass:** a compare-and-set writes the fresh report into `risk_check_results` plus one `modified` event: "Risk re-validated at confirmation against the current portfolio: all 6 check(s) passed." The compare-and-set requires the ticket to still be `confirmed` + `submitting` (live) or `submission_state IS NULL` (dry run). If it lost, nothing is sent (409).
   - **Fail:** a compare-and-set moves it to `risk_rejected` with the fresh report, and clears `order_tag`, `submission_state` and `submitted_at`, because nothing was sent. The `risk_rejected` event reads "Passed creation-time risk checks but failed confirmation-time revalidation: <failed messages>. No order was sent." The response is **409 `RISK_REVALIDATION_FAILED`** with `failed_checks`.
   - **Unavailable:** see section 12.
5. Only now: `execution.execute_ticket`. The rest is unchanged.

**State choice.** No new status value is needed.
- `risk_rejected` already means "evaluated and failed a limit".
- The `confirmed` event followed by the `risk_rejected` event shows the exact history.
- `mode` stays as the claim stamped it (PK-3: never rewritten after the claim). It is invisible to every limit, because all of those queries require `executed`/`confirmed`.

**Why this ordering.** Revalidating *after* winning the claim means two concurrent approvals are serialised by the claim. Each re-validates knowing the other is claimed, because claimed entries count (section 1).
- Worst case: both claim before either re-validates, and both are refused. That fails safe.
- A crash between the claim and revalidation leaves a `submitting` claim. PK-2 turns it into `unknown` after 120 s and then `not_found` after the window. That is correct, since nothing was sent.

**Timing.** Revalidation adds one `holdings()` and one `margins()` call before `place_order`: at most about 14 s with kiteconnect's 7 s timeouts. That is well inside `SUBMISSION_STALE_SECONDS = 120`.

## 4. Exit revalidation flow

For a ticket with `closes_ticket_id` set:

1. Status pre-check.
2. `_claim_mode` (PK-3, unchanged and first): `EXIT_MODE_UNRESOLVED` or `EXIT_MODE_MISMATCH`, both with no writes.
3. **`_refuse_stale_exit` → `risk_engine.linked_exit_problem`**, a DB-only re-read of the position that checks, in order:

   | Check | Code | Kind |
   |---|---|---|
   | referenced ticket exists for this account, is a BUY, is not itself an exit, `status='executed'` | `EXIT_POSITION_INVALID` | permanent |
   | no other exit of the position is `executed` | `EXIT_POSITION_CLOSED` | permanent |
   | position `closed_at IS NULL` | `EXIT_POSITION_CLOSED` | permanent |
   | no other exit of the position is `confirmed` (in flight / UNKNOWN / needs_review) | `EXIT_ALREADY_IN_PROGRESS` | transient |
   | exit `quantity` == position `quantity` | `EXIT_QUANTITY_MISMATCH` | permanent |

   - **Permanent:** compare-and-set `pending_confirmation → rejected`, with the event "Stale exit refused at confirmation (CODE): … No order was sent.", then 409 with the code.
   - **Transient:** 409 with no write.
4. **Claim**, wrapped in `except sqlite3.IntegrityError` → 409 `EXIT_ALREADY_IN_PROGRESS` (the storage backstop, section 13).
5. `execute_ticket` and the rest are unchanged.

There is no post-claim broker revalidation for linked exits. Their only check is DB state, which the pre-claim check plus the index make atomic.

**Same rule at creation.** `_check_exit_or_reduction` uses the same `linked_exit_problem`. An exit created, or reverted, for a closed, owned or mismatched position becomes `risk_rejected` at once, and is never approvable only to be refused at confirmation.

## 5. Duplicate / stale exit prevention

| Scenario | What prevents a second SELL |
|---|---|
| Strategy exit pending, then Emergency Exit sells the position | EE's exit executes → `close_position_for_exit` supersedes the strategy exit → approving it is 409. Pinned by the converted P0-4B test. |
| Quick Action (linked) exit + strategy exit | Whichever executes first supersedes the other. A later creation of either is `risk_rejected` by `linked_exit_problem`. |
| Stale pending exit after a GTT closure | The trailing engine's synthetic closure supersedes it. If a pending exit somehow survives (legacy data), confirmation refuses it: `EXIT_POSITION_CLOSED`. |
| Two independently created exits | Same as above. Approved concurrently: the second claim hits the unique index (409). |
| One exit UNKNOWN, another approved | `EXIT_ALREADY_IN_PROGRESS` (transient, no write). The index also blocks it if the application check is bypassed. |
| Emergency Exit while an exit is UNKNOWN | PK-2's guard (`exit_outcome_unknown`) is kept unchanged. |

## 6. Sibling exit handling

`execution.supersede_sibling_exits(conn, position_id, owner_id)` runs a per-row compare-and-set `draft|pending_confirmation → rejected`, each with the event "Superseded by exit ticket X — position N is closed; no order was sent." It is called only when an exit has **executed**:

- the confirm path, when this request won the outcome (`journal.close_position_for_exit`), live or dry run;
- `order_monitor._adopt`, for an adopted exit (the same function). It is not called when the adopted SELL was already REJECTED/CANCELLED, because the position is still open;
- `trailing_engine._handle_gtt_closure`, after the synthetic exit.

**UNKNOWN-safe.** A mere claim supersedes nothing. While an exit is UNKNOWN its siblings stay `pending_confirmation`, and approving one is refused (`EXIT_ALREADY_IN_PROGRESS`); nothing is opened or executed.
- If the owner is **adopted**, the siblings are superseded.
- If it ends **`not_found`** (nothing was placed), the siblings are still pending and valid, so the user can approve one and it sells once.

All three branches are tested.

`risk_rejected` siblings are left alone, since they are not approvable anyway. A superseded exit can be reverted, and Revert re-runs `linked_exit_problem`, which rejects it again for as long as the position is closed.

## 7. Quantity validation

- An exit's `quantity` must **equal** the referenced position's `quantity`. That is the best representation of the open quantity available today.
- It is checked at creation, at revert and at confirmation.
- **A larger exit** would oversell. With CNC it would sell shares held outside the app (audit P0-4).
- **A smaller linked exit** is refused too. The current model has no partial exits: `_execute_exit` deletes the whole position's GTT, and the journal closes the full quantity, so a smaller linked exit would leave the remainder unprotected and mis-journaled.
- Partial reductions keep going through the unlinked alert-action sell path, which is validated against broker-held quantity.

**Known limitation (PK-13).**
- A position whose entry filled only partially is still represented by its *requested* quantity: `filled_quantity` is not stored. An exit of the full requested quantity is therefore accepted and could oversell the unfilled part.
- An exit that partially filled leaves the position `closed_at`-stamped, so an exit for the remainder is refused (`EXIT_POSITION_CLOSED`).

Both belong to PK-13's partial-fill model and were deliberately not solved here.

## 8. Risk-engine race / resurrection fix

`run_risk_checks_and_transition` writes through `_transition_from_draft`:

- the pass/fail result: `UPDATE … SET status=?, risk_check_results=? WHERE id=? AND status='draft'`;
- the unavailable path: `… SET status='cancelled' WHERE id=? AND status='draft'`, and the `rejected` event is written only if that won.

On rowcount 0 the engine:
1. rolls back;
2. writes **no** `risk_rejected` / `rejected` event;
3. re-reads the current status;
4. logs one `modified` event: "Risk check finished after the ticket had become '<status>'; its result (<outcome>) was discarded and the ticket left unchanged."

The ticket keeps its current state.

**Every caller already sets `draft` before calling:**
- create inserts `draft`;
- revert moves `rejected → draft`;
- edit writes `draft`;
- strategy entry and exit creation insert `draft`;
- Emergency Exit inserts `draft`.

So the expected state is always `draft`.

## 9. Edit / revert / delete state hardening

| Handler | Before | After |
|---|---|---|
| `edit_ticket` | Unconditional `UPDATE … status='draft'` | Exit/reduction tickets (`is_closing_sell`) → **409 `EXIT_TICKET_NOT_EDITABLE`**. UPDATE `… WHERE id=? AND status='draft'`; if it lost: rollback, 409, no `modified` event. |
| `revert_rejection` | Unconditional `→ draft` | `_transition(("rejected",) → "draft")`; if it lost: 409, no event |
| `delete_ticket` (draft) | Unconditional delete | Events deleted, then `DELETE … WHERE id=? AND status='draft'`; if not exactly one row: rollback (restores the events), 409 |
| `delete_ticket` (archive) | Event, then unconditional `archived=1` | `UPDATE … archived=1 WHERE id=? AND status IN (soft-deletable)` first; event only if that won |
| `strategy_engine.expire_stale_signals` | Unconditional `→ cancelled` | `… AND status IN ('draft','pending_confirmation')`; the event and the returned id only if it won |

`expire_stale_signals` is included because it could overwrite a ticket approved between its SELECT and UPDATE, including one with an UNKNOWN broker outcome, with `cancelled`. Order monitor recovery would then never find that ticket again (Part I).

There was no general state-machine rewrite. `reject`, Cancel All Pending and the confirm claim were already conditional (PK-1).

## 10. PK-2 two-writer interaction

Every write PK-4 adds or touches, and its condition:

| Write | Condition | Can overwrite UNKNOWN / needs_review / adopted / filled / cancelled / rejected? |
|---|---|---|
| Revalidation pass/fail/unavailable (`_update_if_still_claimed`) | `status='confirmed'` AND `submission_state='submitting'` (live) / `IS NULL` (dry run) | No. If the monitor marked a stale claim `unknown` meanwhile, the write loses, nothing is sent, and the monitor resolves it (to `not_found`: correct, nothing was placed). |
| Stale-exit auto-reject | `status='pending_confirmation'` | No |
| Supersede siblings | `status IN ('draft','pending_confirmation')` | No |
| Risk-engine transition | `status='draft'` | No |
| Edit / revert / delete / archive / signal expiry | Section 9 | No |
| Trailing synthetic exit | Pre-check plus the unique index | Cannot record a second exit for a position an exit already owns |

**The revalidation compare-and-set deliberately does not go through `execution.update_ticket_if`.** That function is PK-2's post-broker-call outcome write, and PK-2's race tests hook its first call. A separate helper keeps both meanings clean. All of PK-2's race tests pass unchanged.

The PK-1/PK-2 note about `conn.rollback()` on Emergency Exit's shared connection still holds. Every new rollback runs only where nothing of Emergency Exit's own is uncommitted, because it commits each exit ticket's creation and risk check first.

## 11. PK-3 mode interaction

- `_claim_mode` is unchanged and runs **before** the stale-exit check, so `EXIT_MODE_MISMATCH` / `EXIT_MODE_UNRESOLVED` behave exactly as in PK-3 (no writes). All PK-3 tests pass unchanged.
- A matching mode is necessary, not sufficient. `test_p0_4_exit_of_closed_position_is_refused_even_with_matching_mode` has a live exit of a live but closed position: it is refused and sends nothing.
- Revalidation never re-resolves the mode. It runs after the claim stamped it, and it never writes `mode`, including when it moves the ticket to `risk_rejected` or `rejected`. The global mode is read only in the claim of a new entry, as before.
- Exits use the position's mode, never the global one. The exit validator reads nothing mode-related from global settings.
- In-flight counting uses `mode = 'live'`, consistent with every other limit query. Dry-run tickets never count toward real limits.

## 12. Risk-check-unavailable policy

**RISK CHECK FAILED** means the checks ran and at least one failed. The ticket goes to `risk_rejected`, with a `risk_rejected` event, and the response is 409 `RISK_REVALIDATION_FAILED`.

**RISK CHECK UNAVAILABLE** means the portfolio could not be read. This covers:
- `RiskCheckUnavailableError` (Kite `TokenException` / `KiteException`);
- any other exception from `holdings()`, `margins()` or `positions()`, including raw `requests` errors, which the creation-time path lets escape (P0-4A's origin).

It **fails closed**:
- the ticket goes to `rejected`, with the submission fields cleared;
- a `rejected` event records "Risk could not be revalidated (<reason>); no order was sent.";
- the response is **503 `RISK_REVALIDATION_UNAVAILABLE`** with the same message;
- no order is placed;
- it is not classed as a risk rejection;
- it is never reopened automatically.

**Why `rejected` rather than `cancelled`**, which is what creation-time unavailability uses: `rejected` is the one terminal state the user can **Revert**. Revert re-runs the full risk checks, so a transient Kite outage does not irrecoverably destroy a pending approval, yet nothing happens without a deliberate user action. The test covers the recovery: after the outage, Revert → `pending_confirmation`.

## 13. Database / index changes

One new index, created by the idempotent migration `_ensure_single_exit_owner_index`:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_tickets_one_exit_owner ON trade_tickets(closes_ticket_id)
WHERE closes_ticket_id IS NOT NULL AND status IN ('confirmed', 'executed')
```

**Owning set = `confirmed` + `executed`.** These are the states in which an exit owns its position per PK-1/PK-2: claimed (in flight, UNKNOWN, needs_review) or executed. Every non-owning state leaves the set:
- `failed` (a broker-rejected exit; the monitor reopens the position), `not_found`;
- `rejected` (including superseded), `cancelled`, `risk_rejected`, `draft`, `pending_confirmation`.

So a failed exit frees the position for a new one. The only transitions *into* the set are the confirm claim (409 on violation) and the trailing engine's synthetic INSERT (pre-check + `IntegrityError` handling, section 5). The monitor's adoption and every outcome write keep the same row inside the set.

**Placement.** The migration runs after `_ensure_submission_tracking_columns`, and therefore after every `trade_tickets` rebuild. Those rebuilds drop indexes; `CREATE … IF NOT EXISTS` then recreates it each startup.

**Pre-existing duplicates.** If existing data already has two owning exits for a position, the index is **not** created and a `logger.warning` names the positions. Startup is never blocked, and the application checks still apply. Once the data is corrected, the next startup creates the index. This is tested.

**Validation on real data.** A scratch copy of `backend/kite_terminal.db.pre_pk4_bak` was placed in a temporary directory, with `db.DB_PATH` and `crypto.ENV_PATH` redirected. `init_db()` was run twice:
- `integrity_check` = ok and `foreign_key_check` empty after both runs;
- the index was created by run 1;
- the schema hash was identical after runs 1 and 2 (idempotent);
- row counts of **every** table were unchanged: `trade_tickets` 18, `ticket_events` 49, `ohlc_daily` 50,556, `instrument_cache` 10,133, `closed_trades` 0, `reconciliation_log` 0, …;
- the live database's SHA-256 and mtime were unchanged. It was never opened for writing; tests run on per-test temp databases.

The real DB holds **0** exit tickets, so creating the index cannot fail on it. The scratch copy was deleted afterwards.

## 14. Tests converted from xfail

| PK-0 xfail | Now | Strengthened with |
|---|---|---|
| `test_p0_4b_stale_exit_cannot_sell_already_closed_position` | passing | Still exactly 1 SELL. Confirm → 409. The stale exit is `rejected` with the "Superseded by exit ticket <EE exit>" event and was never `confirmed`. |
| `test_p0_4c_pending_entries_cannot_jointly_exceed_open_position_limit` | passing | Still exactly 1 BUY. 409 `RISK_REVALIDATION_FAILED` with `failed_checks == ["max_open_positions"]`; `risk_rejected`; submission fields cleared; mode kept; fresh report stored; `confirmed` → `risk_rejected` events with the revalidation wording; a second confirm → 409. |

No assertion was weakened. The original assertion of each test is still there, unchanged.

## 15. New tests

All are in `tests/test_live_safety.py`, P0-4 section. They use FakeKite only, with no sleeps and no network. Races run the competitor to completion on the same thread from a one-shot hook: FakeKite `on_call("holdings")`, the instruments lookup, a stale first read, or a proxy connection. The one cross-thread test holds a `threading.Event` inside `place_order`.

| # (brief) | Test | Scenario |
|---|---|---|
| — | `revalidation_success_is_recorded_before_the_order` | `confirmed → modified("Risk re-validated…") → executed`; `holdings()` re-read before `place_order` |
| 3 | `concurrent_approval_counts_the_entry_still_in_flight` | A held inside `place_order` (claimed, not executed); B refused (`max_open_positions`); 1 BUY |
| 4 | `total_portfolio_risk_revalidated_at_confirmation` | 0.049% + 0.025% vs limit 0.07% |
| 5 | `exposure_revalidated_at_confirmation[single_stock\|sector]` | Two TCS at a 2% single-stock limit; TCS + INFY mapped to IT at a 2% sector limit |
| L | `risk_unavailable_at_confirmation_fails_closed[ReadTimeout\|NetworkException]` | 503, `rejected`, no submission fields, no order, ignored by all loops; Revert recovers |
| 1, 8 | `exit_of_closed_position_is_refused_even_with_matching_mode[closed_at_stamped\|sibling_exit_executed]` | Live exit, live position, already closed: 409 `EXIT_POSITION_CLOSED`, no broker call, GTT active, auto-rejected with the stale event |
| 2 | `duplicate_exit_is_superseded_and_cannot_sell_again` | Two pending exits; the first executes; the second is superseded, 409, 1 SELL |
| 9 | `exit_quantity_must_match_the_position[exceeds\|partial]` | 409 `EXIT_QUANTITY_MISMATCH`, position open, GTT active; Revert → `risk_rejected` |
| 12 | `exit_is_refused_while_a_sibling_exit_outcome_is_unknown[still_unknown\|owner_adopted\|owner_not_found]` | 409 `EXIT_ALREADY_IN_PROGRESS` ("DO NOT RESUBMIT"), no write; adoption supersedes; `not_found` makes the sibling approvable, 1 SELL total |
| D | `database_refuses_a_second_owning_exit` | Application check patched out: the claim hits the index → 409, no SELL; a direct UPDATE raises `IntegrityError` |
| Q | `exit_owner_index_migration_is_idempotent_and_tolerates_legacy_duplicates` | `init_db` twice; duplicates → index skipped with a warning, startup OK; created after cleanup |
| 6 | `risk_check_cannot_resurrect_a_ticket_cancelled_mid_check[would_pass\|would_fail]` | Cancel All inside `holdings()`: stays `rejected`, events `created, rejected, modified(discarded)`, no `risk_rejected`, confirm 409 |
| 7 | `reverted_ticket_rejected_mid_check_stays_rejected[would_pass\|would_fail]` | Reject inside the reverted ticket's risk check: no resurrection, no misleading event |
| 10 | `exit_tickets_cannot_be_edited[linked_exit\|alert_action_reduction]` | 409 `EXIT_TICKET_NOT_EDITABLE`, row byte-identical, no events |
| 11 | `stale_edit_cannot_turn_a_cancelled_ticket_back_into_a_draft` | Cancel All between the edit's read and write: 409, stays `rejected` with the original quantity |
| H | `stale_read_cannot_revert_delete_or_archive[revert\|delete_draft\|archive]` | The handler's read is stale; the conditional write refuses; nothing changed, events intact |
| I | `signal_expiry_cannot_overwrite_a_ticket_confirmed_meanwhile` | Approved (UNKNOWN) between expiry's SELECT and UPDATE: left `confirmed/unknown` |
| D | `gtt_firing_while_an_exit_is_unresolved_is_flagged_not_recorded` | TCS GTT fires while its exit is UNKNOWN: no second exit, position open, one mismatch + one urgent broadcast over two ticks. INFY in the same tick closes normally and its pending exit is superseded. |
| — | `emergency_exit_continues_past_a_refused_position` | TCS refused → reported `refused`; INFY still sold |

30 new cases in all. Items 7 and 8 of the brief are also covered by the section 12 and section 4 rows respectively.

## 16. Mutation-test results

A scratch script handled each mutation: it copied the file, applied the mutation, ran `pytest tests -k p0_`, restored the file and **verified its SHA-256**. `git diff --stat` was identical before and after the whole run.

| Mutation | Tests that fail |
|---|---|
| M1 revalidation at confirm removed | 8: `p0_4c`, concurrent, total risk, exposure ×2, unavailable ×2, success-event |
| M2 stale-exit check (`_refuse_stale_exit`) removed | 8: closed position ×2, quantity ×2, unknown sibling ×3, EE-continues |
| M3 risk-engine `AND status='draft'` removed | 4: resurrection ×2, revert race ×2 |
| M4 exit-owner index not created | 2: DB backstop, migration |
| M5 sibling supersession removed | 4: `p0_4b`, duplicate exit, unknown sibling `[owner_adopted]`, GTT closure |
| M6 in-flight entries not counted | 1: concurrent approval |
| M7 edit/revert/delete/archive unconditional | 4: stale edit, stale read ×3 |
| M8 signal expiry unconditional | 1 |
| M9 exit quantity not checked | 2 |
| M10 edit allows exit tickets | 2 |
| M11 trailing owner pre-check removed | 1 (GTT closure). The flag loses the owner's id, **but the `IntegrityError` backstop still prevents the second exit** (defence in depth). |
| M12 trailing pre-check *and* `IntegrityError` handler removed | GTT closure test crashes with `UNIQUE constraint failed`. This shows the handler is required: without it the index would abort the trailing tick for every other position. |

`p0_4b` does **not** fail under M2: supersession alone already protects that scenario. The confirmation check is pinned by the eight tests that do fail.

## 17. Full-suite results

`.venv/Scripts/python -m pytest` (the repository `pytest.ini` adds `-q`):

| Run | passed | xfailed | failed | xpassed |
|---|---|---|---|---|
| Baseline (before any edit) | 180 | 10 | 0 | 0 |
| Backend change only, before test edits | 180 | 8 | **2 (strict XPASS: exactly P0-4B and P0-4C)** | — |
| 1 | 212 | 8 | 0 | 0 |
| 2 | 212 | 8 | 0 | 0 |
| 3 | 212 | 8 | 0 | 0 |

180 + 2 converted + 30 new = 212. The backend-only run shows the change was scoped: no other test changed outcome.

**Timing.** Later runs took about 67 s instead of about 30 s. `--durations` shows no slow test (the slowest call is 0.77 s): the extra time is spread across per-test setup and is environmental, with no test waiting on a timeout.

## 18. Repeated targeted results

| Set | Runs | Result every run |
|---|---|---|
| PK-4 (`-k p0_4`, 34 tests) | 10 | 34 passed |
| Races PK-1/PK-2/PK-4 (`-k "p0_1 or p0_2 or p0_4"`, 80 tests) | 20 | 80 passed |

## 19. Remaining P0 xfails (8, all strict, none XPASS)

| Finding | Test(s) | Packet |
|---|---|---|
| P0-5 OCO closure | `test_p0_5_oco_stop_or_target_fill_closes_and_journals_position[stop, target]` | PK-6 |
| P0-6 GTT triggered without fill | `test_p0_6_triggered_gtt_without_fill_keeps_position_open[unfilled, partial]` | PK-7 |
| P0-7 exit placed vs filled | `test_p0_7_unfilled_exit_keeps_protection_and_position_open[limit_market_hours, market_amo_after_hours]` | PK-8 |
| P0-8 CSRF | `test_p0_8_cross_site_post_is_rejected[confirm, gtt_cancel]` | PK-5 |

## 20. Remaining limitations intentionally deferred

- **Partial fills (PK-13).** See section 7. Quantity is the requested quantity. A partially filled entry can still be exited for its full requested quantity. A partially filled exit blocks a follow-up exit.
- **No reservation for pending tickets.** Several pending tickets are not limited jointly until they are approved. That is sufficient per the brief: each approval re-validates against executed and claimed entries.
- **`needs_review` entries count toward limits indefinitely**, until the operator resolves them in Kite. This is conservative, and consistent with PK-2 (they also block strategy re-proposal).
- **An in-flight entry's exposure is same-day only** (`COALESCE(submitted_at, created_at)` ≥ IST midnight), matching `_symbol_exposure`'s existing same-day rule. The open-position count and total risk count in-flight entries regardless of age.
- **An unfilled live entry can still be exited** (`entry_fill_status='pending'`). Emergency Exit relies on this for its fallback when cancelling the entry fails because it has just filled. This was not changed.
- **`routes/orders.cancel_order`** still writes `status='cancelled'` unconditionally on the ticket linked to a cancelled order (P1-1). It was not touched by PK-4.
- **Dry-run entries are revalidated too** (a holdings/margins read, which is not a broker mutation). If Kite is unreachable, a dry-run approval fails closed as well. That is consistent with creation.
- **Sector exposure** still depends on `sector_mapping` rows (P1-4). The sector test maps TCS/INFY explicitly.

## 21. Dependencies that change PK-5/PK-6/PK-7/PK-8 assumptions

1. **(PK-7/PK-8) The unique index defines which statuses own a position:** `confirmed` and `executed`. A new status in which an exit owns its position (for example `exiting`) **must be added to the index predicate** and to `linked_exit_problem`'s owner query. Otherwise the storage backstop silently stops covering it.
2. **(PK-7/PK-8/any) A `trade_tickets` rebuild drops the index.** Such a migration must run *before* `_ensure_single_exit_owner_index` in `init_db`, as the existing ones do.
3. **(PK-6/PK-7) The trailing engine no longer records a GTT closure when another exit owns the position.** It flags `position_state` "GTT … fired while exit ticket X owns the position — POSSIBLE DOUBLE SELL" and waits for that exit to resolve. The GTT watcher that PK-6/PK-7 introduce must keep this rule and insert its synthetic exit only for an unowned position, or handle `IntegrityError`.
4. **(PK-8) Supersession happens when an exit is recorded `executed`**, which today means *placed*, not *filled* (P0-7). If PK-8 splits exit placement from fill, it must decide whether siblings are superseded on placement or on fill. It must also keep "an unresolved owner supersedes nothing".
5. **(PK-13) Partial exits** need the index relaxed (for example, a sum-of-quantities invariant instead of uniqueness) and `linked_exit_problem`'s equality rule changed to "≤ remaining filled quantity".
6. **(PK-5) New response codes the UI/CSRF layer will see:**
   - on confirm: 409 `RISK_REVALIDATION_FAILED`, `EXIT_POSITION_INVALID`, `EXIT_POSITION_CLOSED`, `EXIT_ALREADY_IN_PROGRESS`, `EXIT_QUANTITY_MISMATCH`; 503 `RISK_REVALIDATION_UNAVAILABLE`;
   - on edit: 409 `EXIT_TICKET_NOT_EDITABLE`.

## 22. Deviations from the audit recommendation

1. **The index covers `confirmed` as well as `executed`.** The audit suggested `WHERE status='executed'`. That would not stop two *different* exit tickets being claimed concurrently: both would reach the broker before either became `executed`. Claimed exits own the position under PK-1/PK-2, so they are in the owning set.
2. **Sibling exits are superseded when an exit executes, not when it is claimed.** The audit says "when an exit executes", and this matches. It is stated explicitly because it is what keeps an UNKNOWN exit from closing out its siblings.
3. **Exit quantity must equal the position, not merely not exceed it.** The brief's minimum is to reject an exit that exceeds the position. Equality is required because a smaller linked exit is also unsafe under today's model (section 7).
4. **Revalidation happens after the claim**, as the brief orders, rather than "immediately before `execute_ticket`" with no claim in between. The claim serialises concurrent approvals, and claimed entries count toward limits.
5. **Unavailable at confirmation → `rejected` (revertable)**, not `cancelled` as at creation (section 12).
6. **Entries claimed but unresolved now count toward limits, at creation and at confirmation.** This was PK-2's finding #2 and is needed for the concurrent case. `list_open_positions` (exit scan, EE, breakeven) is unchanged.
7. **Small additions beyond the audit's PK-4 row, each required by the new invariants:**
   - Emergency Exit continues past a refused position;
   - Emergency Exit's result toast lists non-exited positions;
   - the trailing engine handles an owned position;
   - `expire_stale_signals` became conditional.
