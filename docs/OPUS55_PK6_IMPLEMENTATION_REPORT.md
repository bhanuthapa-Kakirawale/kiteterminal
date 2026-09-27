# OPUS55 PK-6 Implementation Report: GTT watcher for every live protected position (P0-5)

| | |
|---|---|
| Packet | PK-6 only. It fixes **P0-5** and includes the low-risk half of **P1-9** (run the cycle off the event loop, isolate failures per ticket), as the brief allowed. PK-7 and PK-8 were not started. |
| Baseline before | 340 passed, 6 xfailed (P0-5, P0-6 and P0-7, two each), 0 failed, 0 xpassed. Re-measured before any edit. |
| Result after | **370 passed, 4 xfailed, 0 failed, 0 xpassed** (3 identical full runs) |
| Backups taken first | Tag `backup-pre-pk6` and branch `backup/pre-pk6` (both at `13ae3f8`). Tag `backup-pre-pk6-worktree` (commit `7d4392a`) is a snapshot of the whole working tree, including the uncommitted and untracked PK-0 to PK-5 files. It was built through a temporary index, so the real index, HEAD and working tree were not touched. `backend/kite_terminal.db.pre_pk6_bak` was taken with the SQLite online-backup API; `integrity_check` = ok. |
| Committed | No. Nothing was committed, as instructed. |

## 1. Executive summary

Before PK-6, only trailing positions were checked for a fired protective GTT. The trailing engine's query required `trailing_enabled = 1`. Two kinds of position are not trailing:

- manual tickets, where no trailing is the form default;
- every `rsi_mean_reversion` ticket.

These positions carry an OCO GTT with a stop leg and a target leg. When either leg filled at Kite, the app kept the position open indefinitely. It still used an open-position slot, still counted toward total risk, was still offered to EXIT ALL, and was never journaled.

PK-6 splits the **GTT watcher** from the **trailing ratchet** inside `trailing_engine.py`. There is still one engine and one per-position path, and the closure code is unchanged. What changed:

- **Who is watched.** Every live, filled, unclosed position that has a GTT is now checked each cycle, trailing or not.
- **What happens on a fired GTT.** Both kinds of position go through the same existing closure logic.
- **Trailing.** The ratchet still runs only for trailing positions.
- **Event loop.** The cycle now runs in a worker thread (`asyncio.to_thread`), like the order monitor and reconciliation.
- **Failures.** Each position is its own transaction. One position's broker or database error is rolled back, logged, and escalated after three failures in a row. It no longer stops the other positions being checked.

The two P0-5 xfails are now ordinary passing tests. Twenty-eight new tests cover the brief's cases A to K plus a broker-call budget. The P0-6 and P0-7 xfails are unchanged and still xfail.

## 2. Files changed

This is PK-6's own diff, measured against `backup-pre-pk6-worktree`.

| File | Change |
|---|---|
| `backend/trailing_engine.py` | The only logic change (+101/−36). It adds `monitored_positions()`, `_tick_sync()` (the old `_tick_once` body), a thin async `_tick_once()` that runs `_tick_sync` through `asyncio.to_thread`, `_trailing_ltps()`, `_note_ticket_error()` and the `_consecutive_errors` dict. The per-ticket loop now has try/commit/rollback. `_process_ticket` has a trailing-only early return after the GTT branch. |
| `backend/reconciliation.py` | A comment in `_check_gtt_state` now says the trailing engine's watcher covers every live protected position. No behaviour change. |
| `backend/ticker.py` | The `broadcast()` docstring now says callers may be worker threads, and that it is safe because `_broadcast` uses `call_soon_threadsafe`. No behaviour change. |
| `tests/test_live_safety.py` | Removed the `@_xfail("P0-5", …)` marker and added a 3-line section comment. |
| `tests/test_gtt_watcher.py` | **New**, 28 tests (§14). |
| `docs/OPUS55_PK6_IMPLEMENTATION_REPORT.md` | This report. |

The following were **not** touched:

- `execution.py`, `order_monitor.py`, `routes/tickets.py`, `routes/orders.py`, `journal.py`, `db.py` and `risk_engine.py`;
- the frontend;
- `tests/fake_kite.py` and `tests/conftest.py`;
- `docs/OPUS55_AUDIT_REPORT.md`.

The closure functions `_find_matching_fill`, `_handle_gtt_closure`, `_handle_owned_closure`, `_handle_protection_lost`, `_try_activate` and `_try_ratchet` are byte-for-byte unchanged.

## 3. Previous GTT monitoring flow

This was traced in the code before editing.

**A. Selected.** The engine selected tickets with all of the following: the account, `mode='live'`, `status='executed'`, **`trailing_enabled=1`**, `closed_at IS NULL`, and `COALESCE(entry_fill_status,'filled') != 'pending'`.

**B. Ignored.**
- Every non-trailing position. This is P0-5.
- Entries still waiting for their fill, including adopted entries that are pending.
- UNKNOWN, submitting and needs_review submissions, because their status is `confirmed`.
- Closed positions and dry-run positions.
- Exits. Exits never carry a `kite_gtt_id`, because `_execute_exit` returns `kite_gtt_id=None`.

**C. Detection.** Each cycle made one `get_gtts()` call and built a map of GTT id to status. Any status other than `active` went to `_find_matching_fill`, which makes one `orders()` call per such ticket. If the GTT was missing (`None`) or `triggered`, or if a fill matched, the ticket went to `_handle_gtt_closure`. Otherwise it went to `_handle_protection_lost`, which sets `kite_gtt_id` to NULL, logs a reconciliation row and broadcasts an alert.

**D. Closure synthesis.** `_handle_gtt_closure` runs these steps in order:
1. The PK-4 owner check. If another exit already owns the position, `_handle_owned_closure` flags "POSSIBLE DOUBLE SELL".
2. It inserts a synthetic executed exit. An `IntegrityError` from the unique index also leads to the flag.
3. It stamps `closed_at` on the position.
4. It calls `supersede_sibling_exits`.
5. It writes `journal.record_closed_trade`.

**E. General versus trailing-specific.**
- **General position-lifecycle logic:** the GTT-status branch and every function it calls (C and D above). Nothing in it reads a trailing field, except that `_handle_gtt_closure` uses `current_stop_loss or stop_loss`, which is valid for both kinds of position.
- **Trailing-specific:** the LTP lookup, `last_trailing_check_at`, `_try_activate` and `_try_ratchet`.

**How the cycle ran (P1-9).** `_tick_once` was `async` but called `get_gtts`, `ltp`, `orders` and `modify_gtt` synchronously on the event loop. It made one batched `ltp()` call before processing anything, so a failed LTP aborted all closure checks. `_try_ratchet` caught only `OrderException`, `InputException` and `GeneralException`. A `NetworkException` or a raw `requests` error therefore aborted the whole cycle, so later positions were never checked. There was a single commit at the end of the cycle.

## 4. New all-position monitoring flow

```
run_trailing_loop (event loop)
  └─ await _tick_once()  ──►  asyncio.to_thread(_tick_sync)      (worker thread)
        tickets = monitored_positions(conn, account)              (§5)
        gtts    = kite.get_gtts()            # once per cycle; failure ends the cycle
        ltps    = _trailing_ltps(...)        # one batch, trailing + active GTT only; failure → {}
        for ticket in tickets (ORDER BY id):
            try:
                _process_ticket(...)         # GTT watcher → (trailing only) ratchet
                conn.commit()
            except TokenException: rollback; re-raise
            except Exception:      rollback; log; count; alert after 3 in a row
  heartbeat broadcast + sleep stay on the event loop
```

## 5. Query and eligibility rules

```sql
SELECT * FROM trade_tickets
WHERE account_id = ? AND mode = 'live' AND status = 'executed'
  AND closed_at IS NULL AND kite_gtt_id IS NOT NULL
  AND COALESCE(entry_fill_status, 'filled') != 'pending'
ORDER BY id
```

| Rule | Why |
|---|---|
| `trailing_enabled = 1` **removed** | This is the P0-5 fix. |
| `kite_gtt_id IS NOT NULL` **added** | With no GTT there is nothing to watch. This covers `protection_failed` and a GTT cancelled through `/api/gtts/{id}/cancel`, which sets the id to NULL on purpose. It also skips the `get_gtts()` call entirely when no position has a GTT. The `None` guard in `_process_ticket` stays, because `routes/orders.py` documents relying on it. |
| `status = 'executed'` | Keeps out PK-2 UNKNOWN, submitting and needs_review submissions, whose status is `confirmed`. Also keeps out cancelled and rejected entries. |
| `entry_fill_status != 'pending'` | An entry that has not filled, including an entry adopted after an UNKNOWN submission, has no position yet. These statuses remain eligible: `partial`, `unconfirmed` (the order dropped off the book and is treated as open), `filled`, and legacy NULL. This is the same as before. |
| `closed_at IS NULL` | A historical GTT id alone never makes a ticket eligible. |
| `mode = 'live'` | Dry-run positions have no broker GTT. |
| `ORDER BY id` | Deterministic processing order. |

Tests E, F and the five cases of G pin each exclusion (§14).

## 6. Separation of the GTT watcher from the trailing ratchet

`_process_ticket` now reads:

1. **GTT lifecycle, for every monitored position.** If the GTT status is not `active`, run the existing closure or protection-lost branch and return. This code is unchanged.
2. `if not ticket["trailing_enabled"]: return`. A fixed stop and target (OCO) has nothing to ratchet.
3. **Trailing only.** Get the LTP, stamp `last_trailing_check_at`, then run `_try_activate` or `_try_ratchet`. This code is unchanged.

There is no second engine and no duplicated closure logic. The trailing-only rule is enforced twice:

- by the early return in step 2;
- by `_trailing_ltps`, which fetches LTPs only for trailing positions whose GTT is active.

The second guard alone is not enough when a trailing position and an OCO position share a symbol, because the LTP for that symbol is then fetched anyway. `test_oco_is_not_ratcheted_when_a_trailing_position_shares_its_symbol` covers that case (mutant M4b in §15).

## 7. OCO behaviour

A non-trailing OCO position now reaches `_handle_gtt_closure` exactly as a trailing position does:

| Event | Outcome |
|---|---|
| Stop leg or target leg triggered and **filled** | Closed. A synthetic SELL is recorded at the real fill price, with `matched_order_id` set to the Kite order and `closer_to` set to `stop_loss` or `target`. One `closed_trades` row is written, and `tickets_changed/gtt_closed` is broadcast. |
| GTT `deleted`, `cancelled`, `disabled` or `expired`, with no fill | Still open. `kite_gtt_id` is set to NULL, a reconciliation row is written, and an "UNPROTECTED" `trailing_error` is broadcast once. |
| GTT triggered but **unfilled**, or vanished | Today: closed as "estimated", which is P0-6. See §19. |
| GTT active | Nothing happens. No `ltp`, no `modify_gtt`, no events and no row change. |

`test_non_trailing_position_gets_the_same_gtt_handling_as_a_trailing_one` asserts that, for each of these events, the non-trailing outcome equals the trailing outcome. It compares `closed_at`, `kite_gtt_id`, the exit tickets, journal rows, reconciliation flags and alerts. The comparison is relative on purpose, so it stays valid when PK-7 changes what the unfilled and vanished events do.

## 8. Interaction with PK-4 exit ownership

PK-6 does not change `_handle_gtt_closure`, so every PK-4 guard still applies, now for more positions:

- The owner check runs before any synthetic exit is inserted. An owner is an exit that is `confirmed` (in flight, UNKNOWN or needs_review) or `executed`.
- The `idx_trade_tickets_one_exit_owner` unique index is the storage backstop. An `IntegrityError` becomes a flag, not a second exit.
- A normal closure calls `supersede_sibling_exits` for any pending exits.

Two tests cover this:

- **H, `test_explicit_exit_confirmed_mid_cycle_is_not_duplicated`.** The watcher has already read the open position. The user's exit is then approved inside the cycle: a `get_gtts` hook runs `confirm_ticket`, which sells, deletes the GTT and closes the position. Results:
  - exactly one exit, the user's;
  - one SELL;
  - one journal row, which belongs to the user's exit;
  - no synthetic exit;
  - the PK-4 "owns the position" flag is raised, preserved as-is.
- **I (§9).**

## 9. Interaction with PK-2 UNKNOWN

- **UNKNOWN entries.** Tickets whose status is `confirmed` with submission state `unknown` or `needs_review` are never watched (G). Adopted entries that are still `pending` are not watched either (E).
- **UNKNOWN exits.** `test_gtt_firing_while_an_unknown_exit_owns_the_position_creates_no_sibling` sets up a non-trailing position, then an exit confirm that hits a `ReadTimeout` and becomes UNKNOWN, then the OCO stop fires and fills. Across two cycles:
  - no sibling exit is created;
  - the UNKNOWN exit is left `unknown`;
  - the position is not closed and not journaled;
  - it is flagged once, with a single "POSSIBLE DOUBLE SELL" alert;
  - nothing is re-sent.

## 10. Threading and event-loop change

- `run_trailing_loop` still awaits `_tick_once()`. The heartbeat broadcast and the sleep stay on the loop.
- `_tick_once()` is now `await asyncio.to_thread(_tick_sync)`. This is the pattern `order_monitor` and `reconciliation` already use. The SQLite connection is opened, used and closed inside the worker thread.
- `ticker_manager.broadcast` is safe from a worker thread, because `_broadcast` uses `loop.call_soon_threadsafe`. The order monitor already broadcasts from its thread. The misleading docstring was corrected.
- Only one cycle runs at a time: the loop awaits each cycle before sleeping. The module-level counters are therefore never touched concurrently.
- Existing tests call `asyncio.run(_tick_once())`, so every trailing test now exercises the threaded path.

Two tests prove the change (K):

- `test_broker_calls_run_off_the_event_loop` hooks `get_gtts`, `ltp`, `orders` and `modify_gtt`, which all fire in the test. It asserts that none of these calls sees a running event loop, and that each runs on a thread other than the loop's.
- `test_event_loop_stays_responsive_while_a_cycle_is_blocked` holds the cycle inside `get_gtts` until a coroutine **on the loop** releases it. If the cycle ran on the loop, that coroutine could never run, and the hold would time out. The 5 s timeouts are safety bounds only and are never reached on the correct code.

## 11. Per-ticket failure isolation

- Each position is processed and committed as its own transaction.
- On any `Exception`, the engine rolls back that position's uncommitted writes, logs `GTT watcher failed for ticket N (SYMBOL); continuing with the rest` with the traceback, and moves to the next position. Earlier positions are already committed. A crash in the middle of a closure no longer leaves a half-written synthetic exit; the closure is retried next cycle.
- After **3 failures in a row** for the same position, a `trailing_error` alert is broadcast. This matches the existing ratchet escalation threshold. One success clears the count.
- **`TokenException`** rolls back the current position and is re-raised. An expired session makes every later call fail too. The loop logs it once, as it did before PK-6, and earlier positions stay committed.
- **The `ltp()` batch** is separate. A failure is logged and treated as "no LTP", so the ratchet is skipped for this cycle and every GTT check still runs. A `TokenException` there is re-raised.
- **A failed `get_gtts()`** still ends the cycle, as before. Without the GTT book nothing can be judged.

## 12. Broker-call count and performance impact

The table shows calls per cycle.

| Call | Before | After |
|---|---|---|
| `get_gtts()` | 1, only if a trailing position existed | 1, if any live protected position exists. None if there is none. |
| `ltp()` | 1 batch over **all** watched positions | 1 batch over trailing positions whose GTT is active only. **0** for an OCO-only book. |
| `orders()` | 1 per non-active GTT (inside `_find_matching_fill`) | Unchanged. It is called only when a GTT has left `active`, which is rare. |
| `ltp(key)` in `_handle_gtt_closure` | Only for an unmatched closure | Unchanged |
| `modify_gtt()` | Per ratchet move | Unchanged |

Non-trailing positions add no per-ticket broker calls. They share the one GTT book. `test_one_gtt_book_per_cycle_and_ltp_only_for_trailing` pins this:

- with 3 OCO positions: 1 `get_gtts`, 0 `ltp`, 0 `orders`;
- after adding one trailing position: 1 more `get_gtts` and exactly one `ltp` call, for that symbol only.

## 13. Tests converted from xfail

| Test | Before | After |
|---|---|---|
| `test_live_safety.py::test_p0_5_oco_stop_or_target_fill_closes_and_journals_position[stop]` | xfail (strict) | **pass** |
| `test_live_safety.py::test_p0_5_oco_stop_or_target_fill_closes_and_journals_position[target]` | xfail (strict) | **pass** |

The first full run after the engine change showed exactly these two as `XPASS(strict)`, and nothing else moved. The marker was then removed. The assertions were not changed or weakened.

## 14. New tests (`tests/test_gtt_watcher.py`, 28)

| Req. | Test(s) | Asserts |
|---|---|---|
| A, B | `test_non_trailing_oco_leg_fill_is_closed_and_journaled[stop, target]` | A real manual entry (create, confirm live, fill), trailing off, with an OCO GTT. Either leg fills. Results: closed; one synthetic SELL at the real fill price with `matched_order_id` and `closer_to`; one journal row; the `gtt_closed` broadcast; no `modify_gtt`. A second cycle is a no-op. |
| C | `test_rsi_mean_reversion_position_oco_closure_is_detected` | The ticket is built by the **real** `strategy_engine._create_entry_ticket` with the **real** `rsi_mean_reversion` module, so `trailing_enabled=0`. It is confirmed live, `_execute_live` places an OCO GTT, and the order monitor fills it. The stop then fills, and the position is closed and journaled. |
| parity | `test_non_trailing_position_gets_the_same_gtt_handling_as_a_trailing_one[filled, triggered_unfilled, deleted_no_fill, vanished]` | Non-trailing outcome == trailing outcome (§7). |
| — | `test_non_trailing_gtt_deleted_without_fill_is_flagged_unprotected_not_closed` | Open, `kite_gtt_id` NULL, one "UNPROTECTED" alert, one flag, not re-flagged next cycle. |
| D | `test_trailing_ratchets_and_oco_is_left_alone_in_the_same_cycle`, `test_oco_is_not_ratcheted_when_a_trailing_position_shares_its_symbol` | The trailing position ratchets as before (`modify_gtt` on its GTT, new stop). The OCO row is completely unchanged, even with its LTP beyond target 1 or with the same symbol. LTP is fetched only for the trailing symbol. |
| E | `test_entry_awaiting_its_fill_is_not_watched[placed, adopted_after_unknown]` | A pending entry whose GTT fired, with a matching COMPLETE SELL in the book: not closed, no exit, no journal, no `get_gtts`, watched count 0. |
| F | `test_position_without_a_gtt_is_ignored` | No `get_gtts`, not closed, no alert. |
| G | `test_ineligible_ticket_with_a_historical_gtt_id_is_ignored[closed, dry_run, entry_cancelled, entry_unknown, entry_needs_review]` | Row unchanged, no exit, no journal, no `get_gtts`. |
| H | `test_explicit_exit_confirmed_mid_cycle_is_not_duplicated` | §8 |
| I | `test_gtt_firing_while_an_unknown_exit_owns_the_position_creates_no_sibling` | §9 |
| J | `test_one_failing_position_does_not_stop_the_others[NetworkException, ConnectionError]` | Position A is processed first and its `modify_gtt` raises. B and C (OCO, filled) are still closed and journaled at their fill prices. A is unchanged, and the failure is logged. |
| J | `test_repeated_failure_of_one_position_is_escalated_then_cleared` | No alert on failures 1 and 2. An alert on failure 3. The next success ratchets and clears the counter. |
| J | `test_ltp_outage_postpones_trailing_but_not_gtt_checks` | `ltp()` fails: no ratchet, and both OCO closures still happen. |
| J | `test_expired_session_ends_the_cycle_and_keeps_earlier_positions` | A `TokenException` on B ends the cycle. A is already closed, C waits, and C is closed in the next cycle. |
| K | `test_broker_calls_run_off_the_event_loop`, `test_event_loop_stays_responsive_while_a_cycle_is_blocked` | §10 |
| perf | `test_one_gtt_book_per_cycle_and_ltp_only_for_trailing` | §12 |

No test sleeps. Threads hand over through `threading.Event`, with timeouts used only as safety bounds. Nothing reaches the network; `get_kite` is FakeKite throughout.

## 15. Mutation-test results

A scratchpad script applied each mutant to `backend/trailing_engine.py` and ran `tests/test_gtt_watcher.py` together with the P0-5 tests. It restored the original bytes in a `finally` block and compared SHA-256 hashes afterwards: **restored = True**, on both runs.

| Mutant | Killed by |
|---|---|
| **M0** The whole pre-PK-6 engine, from the snapshot | 20 tests: both P0-5 tests, A/B, C, all 4 parity cases, the unprotected-flag test, H, I, J ×5, K ×2 and perf. The 9 tests that still pass there are the exclusion guards (E, F, G ×5) and the D ratchet, which must hold both before and after PK-6. |
| **M1** `AND trailing_enabled = 1` restored in the query | 19 tests, including both P0-5 tests, A/B, C, all parity cases, H, I, J and K |
| **M2** No per-ticket try/except (plain loop, single commit) | `test_one_failing_position_does_not_stop_the_others[×2]`, `test_repeated_failure_…` |
| **M3** Cycle run on the event loop (`_tick_sync()` instead of `to_thread`) | `test_broker_calls_run_off_the_event_loop`, `test_event_loop_stays_responsive_…` |
| **M4** OCO ratcheted: no early return, and LTP fetched for all | `test_trailing_ratchets_and_oco_is_left_alone_…`, `test_one_gtt_book_…` |
| **M4b** Only the early return removed | Survived at first, because `_trailing_ltps` also keeps OCO symbols out. `test_oco_is_not_ratcheted_when_a_trailing_position_shares_its_symbol` was added and **kills** it. |
| **M5** LTP fetched for every position | `test_one_gtt_book_…`, `test_trailing_ratchets_and_oco_is_left_alone_…` |
| **M6** Pending-entry guard dropped | `test_entry_awaiting_its_fill_is_not_watched[×2]` |
| **M7** `closed_at IS NULL` dropped | `test_ineligible_…[closed]` |
| **M8** `TokenException` swallowed per ticket | `test_expired_session_ends_the_cycle_…` |
| **M9** A failed `ltp()` aborts the cycle | `test_ltp_outage_postpones_trailing_…` |
| **M10** No escalation after 3 failures | `test_repeated_failure_…` |

Every mutant was killed, and the code was restored. The brief's required mutants are M1 (the trailing-only filter), M2 (no isolation) and M3 (the worker-thread path).

## 16. Full-suite results

| Run | Result |
|---|---|
| Baseline, before any edit | 340 passed, 6 xfailed |
| After the engine change, before converting P0-5 | 340 passed, 4 xfailed, **2 failed = the two P0-5 `XPASS(strict)`**. Nothing else moved. |
| Full suite, run 1 | **370 passed, 4 xfailed** (44.7 s) |
| Full suite, run 2 | **370 passed, 4 xfailed** (53.3 s) |
| Full suite, run 3 | **370 passed, 4 xfailed** (55.1 s) |

There were 0 failed and 0 xpassed in every final run. 370 = 340 baseline + 2 converted P0-5 tests + 28 new tests.

## 17. Repeated targeted results

| Selection | Runs | Result |
|---|---|---|
| `tests/test_gtt_watcher.py` + both P0-5 tests | 10 | 30 passed, every run |
| Threading and isolation: K ×2, J ×5 and H | 20 | 8 passed, every run |
| `tests/test_live_safety.py -rxX` | 1 | 101 passed, 4 xfailed; the xfails are listed in §18 |

## 18. Remaining P0 xfails

| Finding | Tests | Packet |
|---|---|---|
| P0-6 | `test_p0_6_triggered_gtt_without_fill_keeps_position_open[unfilled, partial]` | PK-7 |
| P0-7 | `test_p0_7_unfilled_exit_keeps_protection_and_position_open[limit_market_hours, market_amo_after_hours]` | PK-8 |

That makes 4 xfails, all strict, and none XPASS.

- The P0-6 tests use trailing positions, whose behaviour PK-6 does not change.
- The P0-7 tests close the position by explicit exit placement, which deletes the GTT and stamps `closed_at`. The watcher therefore does not select them.

Neither was weakened.

## 19. Limitations intentionally deferred to PK-7 and PK-8

- **P0-6 now also reaches OCO positions (PK-7).** "Triggered or missing" is still treated as "closed", with an estimated price when no fill matches. Before PK-6, a non-trailing position whose stop fired stayed wrongly **open**. After PK-6, if the stop's LIMIT order did not fill (for example on a gap-down), it is wrongly **closed**, exactly as a trailing position already was. This is the direct consequence of the requested boundary: PK-6 widens who is watched, and PK-7 changes the evidence required. The parity test keeps the two kinds of position in step, so PK-7 fixes both at once.
- **The cancel-GTT window (PK-7).** `/api/gtts/{id}/cancel` deletes the GTT at the broker *before* it sets `kite_gtt_id` to NULL. A cycle that lands between the two steps sees the GTT missing. At real Kite a deleted GTT may be absent from `get_gtts()` rather than listed as `deleted`, and would then be treated as closed. This race existed for trailing positions and now also reaches OCO ones. PK-7's evidence rule closes it.
- **Explicit-exit race flag (P1-8, PK-8).** In test H, the watcher defers to the user's exit, so there is no duplicate. It still raises the PK-4 "POSSIBLE DOUBLE SELL" flag, although the only SELL is the user's own. This is a false alarm but a safe one. Removing it needs the conditional `closed_at` or re-read logic of P1-8 and PK-8's exit lifecycle.
- **Explicit exits (P0-7, PK-8).** Exit placement still deletes the GTT, stamps `closed_at` and journals before any fill. This is unchanged, as required.
- **`last_trailing_check_at`** is still stamped only for trailing positions. No "last GTT check" column was added.
- **The UI heartbeat** reads "Trailing engine active — watching N ticket(s)". N now counts all protected positions. The wording is still accurate, so the frontend was not changed.

## 20. Deviations from the audit recommendation

| Audit said | PK-6 did | Why |
|---|---|---|
| PK-6 = P0-5 + P1-8 + P1-9, including a "conditional `closed_at` update" | P0-5 plus the low-risk half of P1-9. **P1-8's conditional `closed_at` was not done.** | The brief said "PK-6 fixes P0-5 ONLY" and allowed only the P1-9 threading and isolation part. The duplicate exit that P1-8 describes is already prevented by PK-4's owner check and unique index; test H proves this for the widened watcher. |
| "Combine this with P0-6" | Not combined | The brief keeps P0-6 for PK-7, and its xfails remain. |
| Run closure detection "for every live, executed, unclosed ticket with a `kite_gtt_id`" | Done, and the pending-entry exclusion is kept | Brief §3: unfilled entries have no position yet. |
| `asyncio.to_thread(_tick_sync)` + per-ticket `try/except Exception` | Done, plus: a per-ticket commit and rollback; `TokenException` re-raised; `ltp()` failure isolated from GTT checks; alert after 3 consecutive failures | Brief §6 and §7 ("preserve TokenException behaviour", "log / alert as appropriate"). A per-ticket transaction avoids half-written closures. |
| — | `ltp()` narrowed to trailing positions with an active GTT | Brief §14: no redundant broker calls. OCO positions never need an LTP. |
| — | `orders()` not hoisted to once per cycle | It is called only when a GTT has left `active`, which is rare. Hoisting it would be premature optimisation (brief §14), and it sits inside PK-7's evidence code. |
