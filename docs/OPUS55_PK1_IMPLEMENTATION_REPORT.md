# PK-1 — Atomic Ticket State Transitions: Implementation Report

| | |
|---|---|
| Packet | PK-1 from `docs/OPUS55_AUDIT_REPORT.md` (fixes P0-1 and P0-4A) |
| Base revision | `13ae3f8` on `feature/signal-detail-and-strategy-revision`, plus the uncommitted PK-0 work |
| Backups taken first | tag `backup-pre-pk1-atomic-confirm` and branch `backup/pre-pk1-atomic-confirm` (both at `13ae3f8`); tag `backup-pre-pk1-worktree` (commit `e452dc6`: a snapshot of the working tree *including* the uncommitted PK-0 files, made through a temporary index so the real index and working tree were not touched); `backend/kite_terminal.db.pre_pk1_bak` (SQLite online-backup API; `integrity_check` = ok) |
| Committed | No |
| Audit report | Not modified |

---

## 1. Summary

Every ticket status change that PK-1 covers is now a database compare-and-set:

- **Confirm.** Only a `pending_confirmation` ticket can be confirmed. The claim is `UPDATE … SET status='confirmed', mode=? WHERE id=? AND account_id=? AND status='pending_confirmation'`. A request whose UPDATE changes 0 rows gets **409 `CONFLICT_INVALID_STATE`** and never reaches `execution.execute_ticket`.
- **Draft tickets.** A draft is no longer confirmable. It never passed risk checks.
- **Reject.** Reject is conditional on the ticket still being `draft` or `pending_confirmation`. It can no longer overwrite a ticket that was confirmed concurrently.
- **Cancel All Pending.** Each cancellation is its own conditional UPDATE. The response, the ticket events and the system event list only the tickets this request actually cancelled.
- **Events.** A `confirmed` or `rejected` ticket event is written in the same transaction as the winning UPDATE. It exists exactly when the transition happened.

Three PK-0 strict xfails are now normal passing tests. Eleven new regression tests were added. The full suite ran green three times in a row: **133 passed, 17 xfailed, 0 failed, 0 xpassed**. The 17 remaining xfails all belong to PK-2 through PK-8.

## 2. Files changed

| File | Change |
|---|---|
| `backend/routes/tickets.py` | `_OPEN_STATUSES` is split into `_CONFIRMABLE_STATUSES` and `_REJECTABLE_STATUSES`. New helpers `_transition()` (compare-and-set) and `_raise_invalid_state()` (the 409). `confirm_and_execute_ticket`, `reject_ticket` and `cancel_all_pending` now use them. |
| `frontend/app.js` | One line in `sdRenderActions`: the Signal Detail **Approve** button now shows only for `pending_confirmation`. Reject still shows for a draft. |
| `tests/test_live_safety.py` | Three xfail markers removed. The concurrent-confirm and draft tests were strengthened. Two helpers (`_event_types`, `_call`) and eleven new test cases were added. |
| `docs/OPUS55_PK1_IMPLEMENTATION_REPORT.md` | New. This report. |

Nothing else was changed. In particular, `execution.py`, `kill_switch.py`, `risk_engine.py`, `order_monitor.py`, `trailing_engine.py`, `db.py` and `tests/fake_kite.py` are untouched.

## 3. Exact state transitions changed

| Endpoint | Allowed from (before) | Allowed from (after) | Write (before) | Write (after) |
|---|---|---|---|---|
| `POST /api/tickets/{id}/confirm` (and Emergency Exit All, which shares the same function) | `draft`, `pending_confirmation` | **`pending_confirmation` only** | `UPDATE … WHERE id=?` | `UPDATE … WHERE id=? AND account_id=? AND status IN ('pending_confirmation')` |
| `POST /api/tickets/{id}/reject` | `draft`, `pending_confirmation` | `draft`, `pending_confirmation` (unchanged) | `UPDATE … WHERE id=?` | `UPDATE … WHERE id=? AND account_id=? AND status IN ('draft','pending_confirmation')` |
| `POST /api/tickets/cancel-all-pending` | `draft`, `pending_confirmation` | unchanged | one unconditional `UPDATE … WHERE id=?` per row from the SELECT | one conditional UPDATE per candidate, as for reject |

The resulting confirm matrix, with each case covered by a test:

| From | → `confirmed` |
|---|---|
| `pending_confirmation` | allowed |
| `draft`, `risk_rejected`, `rejected`, `confirmed`, `executed`, `failed`, `cancelled` | 409, no broker call, status unchanged, no event |

Two things are deliberately **unchanged**:

- **Reject and cancel still accept a draft.** Withdrawing an unchecked ticket is always the safe direction, and both the UI ("Cancel every draft/pending ticket") and the existing tests rely on it.
- **The 409 bodies are unchanged.** They still carry `error_code: CONFLICT_INVALID_STATE` and the message `Cannot confirm|reject a ticket in status '<status>'`. On a lost race, the status in the message is re-read, so it shows what the ticket actually became.

## 4. The database compare-and-set implementation

```python
def _transition(conn, ticket_id, account_id, from_statuses, to_status, **columns) -> bool:
    # UPDATE trade_tickets SET status = ?, <columns> = ?
    #  WHERE id = ? AND account_id = ? AND status IN (<from_statuses>)
    return cursor.rowcount == 1
```

**Confirm now runs in this order:**

1. `_get_ticket_or_404`, then an early status check. This keeps the 404 and gives a quick, informative 409. Safety does not depend on it.
2. `mode = execution.get_execution_mode()`. This is unchanged; PK-3 owns mode semantics.
3. `_transition(… ('pending_confirmation',) → 'confirmed', mode=mode)`.
   - If it returns **False**: `conn.rollback()`, re-read the ticket, return 409. `execute_ticket` is never called.
   - If it returns **True**: `_log_event('confirmed')` and `commit()`, which is the same transaction as the UPDATE. Then re-fetch the ticket and continue exactly as before.

**Reject** follows the same shape. **Cancel All Pending** treats its SELECT as a candidate list only. It runs `_transition` for each candidate and only logs, reports and counts the tickets whose UPDATE returned True.

**Why this is a real lock, not a hope.**

- **One writer at a time.** SQLite allows one writer per database. Each connection is in WAL mode with Python's implicit deferred transactions and the default 5 s busy timeout. The first request's UPDATE takes the write lock. The second request's UPDATE blocks in the busy handler until the first commits.
- **The loser sees the winner's commit.** The loser has no read transaction open when its UPDATE starts: its earlier `SELECT … fetchone()` has completed and been reset. SQLite therefore starts the write with a fresh snapshot after the lock frees. The loser re-evaluates `status = 'pending_confirmation'` against the committed `'confirmed'`, matches 0 rows, and returns False.
- **No other lock is involved.** There is no Python lock, process-local mutex, disabled button or timing assumption.

**Why roll back on a loss.** The 0-row UPDATE still opened an implicit write transaction. Rolling it back releases the write lock at once, instead of holding it until the connection closes. For Emergency Exit, which shares the connection, nothing is pending at that point: `run_risk_checks_and_transition` has just committed. See §10.

## 5. The P0-1 race, including the corrected window

**The audit's description.** The audit described the window as the length of a Kite round trip, with slow-response retries also able to double-place.

**What PK-0 found.** `confirm_and_execute_ticket` already committed `status='confirmed'` *before* calling the broker. So:

- a second click or retry that arrives while the first confirm waits on Kite was already refused with 409;
- `test_p0_1_second_confirm_while_first_in_flight_is_rejected` pins this, and it still passes.

**The real window.** It was the local read → check → UPDATE → commit gap: milliseconds of SQLite work. The failing sequence was:

1. Request A reads the ticket (`pending_confirmation`) and passes the check.
2. Request B reads the ticket (still `pending_confirmation`) and passes the check.
3. A runs its unconditional UPDATE to `confirmed`, commits, and places BUY #1 and GTT #1.
4. B runs its unconditional UPDATE, which succeeds again, then places BUY #2 and GTT #2 and overwrites the ticket's `kite_order_id`. BUY #1 is live and untracked.

**Why the fix targets the transition, not the timing.** The window is small but real: for example, two tabs, or a double-submit that lands within the same few milliseconds. Its size does not matter, because any read-then-write gap is a race. PK-1 therefore removes the gap instead of shrinking it: the check and the write are now the same SQL statement. Step 4 now matches 0 rows, and B returns 409 before any broker call.

## 6. Tests converted from xfail to passing

| Test | Change beyond removing the marker |
|---|---|
| `test_p0_1_concurrent_confirms_place_exactly_one_order` | Keeps the 2-thread `Barrier` on `get_execution_mode()`. That point is after the check and before the compare-and-set, so both threads genuinely race the UPDATE. A precondition asserts the barrier was not broken. Now asserts all of: responses `[200, 409]`; the 409 carries `CONFLICT_INVALID_STATE`; exactly 1 `place_order` and 1 `place_gtt`; the broker order book equals the one order the ticket tracks (no orphan); the GTT book equals the ticket's `kite_gtt_id`; the ticket is `executed`; exactly one `confirmed` event. |
| `test_p0_4a_draft_ticket_cannot_be_confirmed[risk_check_crashed]` | Also asserts the ticket is still `draft` and has no `confirmed` event. |
| `test_p0_4a_draft_ticket_cannot_be_confirmed[inserted_draft]` | Same. |

## 7. Tests added or modified

All tests are in `tests/test_live_safety.py`, in the P0-1 section. None use sleeps.

**How the race tests stay deterministic.** They monkeypatch `tickets._transition`, the one place every covered transition goes through. The first time the losing request tries its UPDATE, the wrapper runs the competing request to completion first: on its own connection, on the same thread. The interleaving is therefore fixed: "loser read and checked → winner committed → loser's conditional UPDATE". No threads or timing are involved.

| # | Test | Requirement |
|---|---|---|
| 1 | `test_p0_1_sequential_second_confirm_is_rejected` | B. Second confirm → 409, 1 order, 1 `confirmed` event |
| 2–7 | `test_p0_1_confirm_only_from_pending_confirmation[risk_rejected, rejected, confirmed, executed, failed, cancelled]` | §2 matrix. 409, no `place_order` or `place_gtt`, status unchanged, no event |
| 8 | `test_p0_1_reject_cannot_overwrite_ticket_confirmed_after_its_read` | D, confirm wins. Reject → 409; ticket `executed` with its order id; 1 order; no `rejected` event |
| 9 | `test_p0_1_confirm_cannot_execute_ticket_rejected_after_its_read` | D, reject wins. Confirm → 409; no `place_order` or `place_gtt`; ticket `rejected`; no `confirmed` event |
| 10 | `test_p0_1_cancel_all_pending_skips_ticket_confirmed_concurrently` | E. Pending TCS and INFY; TCS is confirmed after Cancel All's SELECT. Response is exactly `{cancelled_ticket_ids: [INFY], count: 1, symbols: ["INFY"]}`. TCS is `executed` with no `rejected` event. INFY is `rejected` with exactly one. The system event reads `Cancelled 1 ticket(s): INFY`. |
| 11 | `test_p0_1_second_reject_is_rejected_without_a_second_event` | A second reject → 409, and there is still exactly one `rejected` event |

New helpers: `_event_types(env, ticket_id)` and `_call(fn, …)`. `_call` calls a route function directly and returns `(200, data)` or `(status, detail)`. It replaces the inner closure the concurrent test used before.

**Mutation check.** The status condition in `_transition`'s WHERE clause was temporarily neutralised (`AND (1 OR status IN …)`), which reproduces the old unconditional write. Four tests then **failed**: tests 8, 9, 10 and the converted concurrent-confirm test. Restoring the clause made them pass again. The race tests therefore detect the defect; they do not pass by construction. The file was restored from a copy, and the restore was verified.

**Unchanged pre-existing tests that still pass:**

- `test_p0_1_second_confirm_while_first_in_flight_is_rejected`
- `test_baseline_api.py::test_cancel_all_pending`
- every reject and revert-rejection test in `test_baseline_api.py` and `test_review_changes.py`
- the Emergency Exit tests

## 8. Full pytest results

Command: `.venv/Scripts/python -m pytest` (the repository `pytest.ini` adds `-q`).

| Run | passed | xfailed | failed | xpassed | time |
|---|---|---|---|---|---|
| 1 | 133 | 17 | 0 | 0 | 19.38 s |
| 2 | 133 | 17 | 0 | 0 | 19.22 s |
| 3 | 133 | 17 | 0 | 0 | 18.94 s |

**How the counts reconcile.** Before PK-1 the suite was 119 passed and 20 xfailed. PK-1 turns 3 xfails into passes and adds 11 new passing tests: 119 + 3 + 11 = 133, and 20 − 3 = 17.

**Extra flake check.** The 15 P0-1 and P0-4A tests (`-k "p0_1 or p0_4a"`) were run **20 times back to back**. Every run gave 15 passed, with no failures.

## 9. Remaining P0 xfails (17, all strict)

| Finding | Packet | Tests |
|---|---|---|
| P0-2 unknown broker outcome | PK-2 | `entry_raw_network_error_strands_ticket[ReadTimeout]`, `[ConnectionError]`, `entry_accepted_but_response_lost`, `exit_accepted_but_response_lost` |
| P0-3 dry-run mode applied to a live position | PK-3 | `emergency_exit_in_dry_run…`, `exit_ticket_confirmed_in_dry_run…`, `breakeven_in_dry_run…` |
| P0-4B stale exit | PK-4 | `stale_exit_cannot_sell_already_closed_position` |
| P0-4C joint limit | PK-4 | `pending_entries_cannot_jointly_exceed_open_position_limit` |
| P0-5 OCO closure | PK-6 | `oco_stop_or_target_fill…[stop]`, `[target]` |
| P0-6 GTT trigger without fill | PK-7 | `triggered_gtt_without_fill…[unfilled]`, `[partial]` |
| P0-7 exit lifecycle | PK-8 | `unfilled_exit_keeps_protection…[limit_market_hours]`, `[market_amo_after_hours]` |
| P0-8 CSRF | PK-5 | `cross_site_post_is_rejected[confirm]`, `[gtt_cancel]` |

None of these depended on `draft` being confirmable or on the confirm race, so none XPASSed.

## 10. Regression risks

| Risk | Assessment |
|---|---|
| A workflow confirmed drafts on purpose | None found. Emergency Exit only calls `confirm_and_execute_ticket` for a ticket it has just seen as `pending_confirmation`. The strategy engine never confirms. The Trade Queue and ticket-detail dialog never offered Approve on a draft. The Signal Detail view did, and that button is now hidden for drafts (§2). Any other client that confirms a draft now gets a clear 409 instead of an unchecked order. **Low.** |
| `conn.rollback()` on a lost compare-and-set, on a shared connection | The Emergency Exit loop passes its own connection in. At the moment of the rollback nothing of its own is uncommitted, because `run_risk_checks_and_transition` commits and `_cancel_unfilled_entry` commits. If a later packet adds uncommitted writes to that loop before the confirm call, the rollback would discard them. That would be a latent trap. **Low; noted for PK-3 and PK-4.** |
| Write-lock contention | Each compare-and-set holds the write lock only for one UPDATE, one INSERT and a commit. The loser waits at most for that, well under the 5 s busy timeout. No broker call happens while a write transaction is open. **Negligible.** |
| Cancel All Pending ordering | Candidates are now `ORDER BY id`. This affects only the order of `cancelled_ticket_ids`. The frontend uses `count` and `symbols`. **None.** |
| Behaviour change visible to users | A confirm that loses a race now returns 409 where the old code double-placed. Reject and cancel return 409 or skip where the old code overwrote an executed ticket. Both changes are intended. |

## 11. Deviations from the audit recommendation

1. **A shared helper instead of inline SQL.** The audit asked for a conditional UPDATE in three places. PK-1 puts that UPDATE in one `_transition()` helper. There is one implementation to review, and it gives the race tests one deterministic hook point that lands exactly between the read/check and the write.
2. **The early status check is kept.** It is kept only for the 404 and for a clear 409 without taking the write lock. The compare-and-set is the authority, and the 409 message after a lost race is built from a fresh re-read.
3. **One-line frontend change.** This is outside the audit's file list for PK-1 (`routes/tickets.py`), but it follows directly from dropping `draft`: otherwise the Signal Detail view would offer an Approve button that can only return 409.
4. **Emergency Exit is not modified.** It inherits the compare-and-set because it calls the same function.

## 12. Issues discovered that should inform PK-2 and later packets

These were found while working. None was fixed here.

1. **(PK-2) `confirmed` now means "execution was claimed".** After PK-1, a ticket in `confirmed` is one whose compare-and-set was won and whose broker call may be in flight or ambiguous. For PK-2, that means:
   - the new `outcome_unknown` handling should start from that state;
   - any recovery or retry path must **never** move a ticket back into `pending_confirmation` just to reuse the confirm endpoint, because that would re-open the double-order race through a side door;
   - the compare-and-set must stay *before* any broker call when PK-2 adds the order tag.
2. **(PK-2) The post-execution write is unconditional.** The write that records the broker result (`UPDATE … SET status=?, kite_order_id=?, … WHERE id=?`) has no status condition. That is safe today, because only the compare-and-set winner reaches it. PK-2 adds a second writer to these tickets: `order_monitor` resolving unknown outcomes by tag. Both writers should then be conditional (`WHERE status='confirmed'`, or on the new unknown state), so the monitor and a late-returning confirm cannot overwrite each other.
3. **(PK-4) The risk engine can resurrect a cancelled ticket.** `risk_engine.run_risk_checks_and_transition` writes `draft → pending_confirmation / risk_rejected / cancelled` without a status condition.
   - **How it happens.** Cancel All Pending (or a reject) lands while a ticket's risk checks are running. Those checks call Kite for holdings, margins and LTP, which takes hundreds of ms. The risk engine's later UPDATE then overwrites `rejected` back to `pending_confirmation`, so a ticket the user cancelled reappears as approvable.
   - **Consequence.** A human must still confirm it, so this is not an order-placement defect by itself. It does weaken the kill switch.
   - **Recommendation.** This file belongs to PK-4 ("re-validate at confirm"). The fix is `… WHERE id=? AND status='draft'` plus a rowcount check. It was left alone here to keep PK-1's scope to `routes/tickets.py`.
4. **Other read-then-write handlers remain.** `edit_ticket` (writes `status='draft'` unconditionally), `revert_rejection` (`rejected → draft`) and `delete_ticket` (deletes a draft) still use read-then-unconditional-write. None of them can cause an order to be placed. However, `edit_ticket` racing a cancel could turn a just-cancelled ticket back into a draft. These are worth applying `_transition` to when PK-4 touches this file.
